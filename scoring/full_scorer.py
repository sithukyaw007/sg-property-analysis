"""Full scoring engine for property investment analysis.

Enrichment + component scoring for a Singapore condo listing. This module
computes the per-category BREAKDOWN that everything downstream reads; it is
not itself the headline score.

What `FullScorer.score()` produces, in order:
1. Enrichment — district normalization, project-units/centroid backfill,
   remaining lease, MRT distance, appreciation rate (URA cache, new-launch
   bias adjusted), rental estimate.
2. The legacy 100-point category breakdown (rental yield 15 / capital
   appreciation 30 / future potential 20 / liquidity 25 / cost efficiency 10 /
   red flags -10). Since v3.0 this total is **display-only** — it is reported
   but does not rank listings. Its real job is to populate `score_breakdown`,
   which is the INPUT to MMR.
3. Trust signals used by the MMR layer: `psf_premium_pct`, `psf_cohort_txns`,
   tight same-size stack comps (`stack_premium_pct`, `stack_low_floor_share`,
   `ask_above_own_stack_prints` / `ask_below_stack_prints`), EC comp join.
4. `relative_value` (age-adjusted vs district peers), then **MMR** — the
   actual ranking axis (`scoring/mmr.py`, uncapped Elo-style, shown as
   `score_1000`).
5. The v3.11 three-score axes (valuation / livability / overall) and ROI
   projections + sensitivity.

Current calibration, weights and release rationale live in `config.py` and
`docs/RELEASES.md` — not here.
"""

import json
import os
import re
import math
from bisect import bisect_left
from datetime import datetime
from typing import Any, Optional

from scoring.costs import CostCalculator
from scoring.models import ScoredListing
from scoring.quick_scorer import QuickScorer
from scoring.rental_estimator import RentalEstimator
from scoring.roi import ROICalculator
from scoring.future_scorer import FutureScorer
from utils.geo import get_mrt_distance, get_tenant_pool_score, normalize_district

# New launch bias adjustment config
try:
    from config import (
        REGIONAL_APPRECIATION_BASELINES,
        DEFAULT_REGIONAL_APPRECIATION,
        NEW_LAUNCH_DISCOUNT_CURVE,
        SCORE_WEIGHT_CAPITAL_APPRECIATION,
        ROI_SENSITIVITY_RENT_DELTA_PCT,
        ROI_SENSITIVITY_APPRECIATION_DELTA_PCT,
    )
except ImportError:
    # Fallbacks mirror config.py (v3.4 de-inverted baselines; sync if config changes).
    REGIONAL_APPRECIATION_BASELINES = {"CCR": 0.028, "RCR": 0.037, "OCR": 0.042}
    DEFAULT_REGIONAL_APPRECIATION = 0.035
    NEW_LAUNCH_DISCOUNT_CURVE = {1: 0.60, 3: 0.45, 5: 0.30, 7: 0.15}
    SCORE_WEIGHT_CAPITAL_APPRECIATION = 30
    ROI_SENSITIVITY_RENT_DELTA_PCT = 0.10
    ROI_SENSITIVITY_APPRECIATION_DELTA_PCT = 0.015

# Load district data
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
_DISTRICT_MEDIANS_FILE = os.path.join(_DATA_DIR, "district_medians.json")


_district_data_cache: dict | None = None


def _load_district_data() -> dict:
    """Load district median data (module-level cache)."""
    global _district_data_cache
    if _district_data_cache is None:
        if os.path.exists(_DISTRICT_MEDIANS_FILE):
            with open(_DISTRICT_MEDIANS_FILE) as f:
                _district_data_cache = json.load(f)
        else:
            _district_data_cache = {"medians": {}, "district_info": {}}
    return _district_data_cache


# ---------------------------------------------------------------------------
# Raw same-size comps (v3.8): per-district URA print lookup.
# The size-band aggregates in ura_cache are too coarse for structurally-unique
# stacks — a 600-800sf band pools ground-floor PES 1BRs (~$1,161 at Coco Palms)
# with normal higher-floor units (~$1,700), so the PES ask reads as a deep fake
# discount. The raw district CSVs hold the true comps; load them lazily, once
# per district per process.
# ---------------------------------------------------------------------------
# Keys are _pu_normalize()d project names (audit #4: the raw exact-upper join
# missed 14.7% of the DB — 'SUITES @ KATONG' never matched URA's
# 'SUITES@ KATONG', silently disarming the v3.8/v3.9 print-trust rules).
# Print tuples: (sqft, psf, floor, sale_dt, sale_type, is_ec).
# v3.10 (audit #4 EC blind spot): Executive Condominium prints live in a
# separate URA file (ura_district_D{NN}_EC.csv, fetched by
# fetch_ura_districts.py --property-types ec) because URA's postal-district
# search splits "Apartments & Condominiums" from "Executive Condominium". The
# 250 EC listings in the DB had NO prints at all, so they benchmarked against
# condo medians — the v3.6 strata/format artifact, at scale. Both files load
# into the SAME per-district dict; each print carries is_ec so the join can
# match an EC listing to EC prints and a condo listing to condo prints.
_raw_prints_cache: dict = {}  # "D18" -> {normalized name: [(sqft, psf, floor, sale_dt, sale_type, is_ec)]}


def _read_prints_csv(path: str, is_ec: bool, out: dict) -> None:
    """Parse one URA district CSV into `out` (normalized name -> [print tuple]),
    tagging every print with `is_ec`. Missing file is a no-op (graceful: when
    the EC file hasn't been fetched yet, behavior is exactly the condo-only
    pre-fix path)."""
    if not os.path.exists(path):
        return
    import csv as _csv
    with open(path, encoding="utf-8", errors="replace") as f:
        for r in _csv.DictReader(f):
            try:
                sqft = float(str(r.get("Area (SQFT)") or "").replace(",", ""))
                psf = float(str(r.get("Unit Price ($ PSF)") or "").replace(",", ""))
            except ValueError:
                continue
            if sqft <= 0 or psf <= 0:
                continue
            # Print hygiene (audit P2): bulk multi-unit rows and land-area
            # rows are not unit comps — a 4-unit block sale or a landed
            # plot print would distort the tight median / p10.
            try:
                n_units = int(float(str(r.get("Number of Units") or "1").replace(",", "")))
            except ValueError:
                n_units = 1
            if n_units != 1:
                continue
            if (r.get("Type of Area") or "").strip().lower() == "land":
                continue
            try:
                sale_dt = datetime.strptime(r.get("Sale Date") or "", "%b-%y")
            except ValueError:
                sale_dt = None
            name = _pu_normalize(r.get("Project Name") or "")
            if name:
                out.setdefault(name, []).append(
                    (sqft, psf, (r.get("Floor Level") or "").strip(), sale_dt,
                     (r.get("Type of Sale") or "").strip(), is_ec))


def _load_district_prints(dcode: str) -> dict:
    if dcode in _raw_prints_cache:
        return _raw_prints_cache[dcode]
    out: dict = {}
    _read_prints_csv(os.path.join(_DATA_DIR, f"ura_district_{dcode}.csv"), False, out)
    _read_prints_csv(os.path.join(_DATA_DIR, f"ura_district_{dcode}_EC.csv"), True, out)
    _raw_prints_cache[dcode] = out
    return out


def _listing_is_ec(listing: dict) -> bool:
    """Whether a listing is an Executive Condominium (so it must benchmark
    against EC prints, not condo medians). ECs are a distinct URA product type
    (subsidized, MOP, then privatised) that trades at its own PSF level.

    Two signals: the scraped `property_type`, and URA's own record of the
    project. PropertyGuru labels many ECs — especially privatised ones such as
    Yew Mei Green or Bellewaters — as "Condominium" (442 such listings in the
    DB when EC prints were first exported). When every URA print for the
    project in its district is an EC print, the listing is an EC whatever the
    portal says. URA never downgrades an EC label: mixed or absent prints fall
    back to the label.
    """
    if (listing.get("property_type") or "").strip().lower() == "executive condominium":
        return True
    return _project_is_ec_by_ura(listing.get("project_name") or listing.get("title"),
                                 listing.get("district"))


def _project_is_ec_by_ura(project_name: str | None, district) -> bool:
    """True when URA files ALL of the project's prints in that district as EC."""
    dcode = normalize_district(str(district or ""))
    name = _pu_normalize(project_name or "")
    if not (dcode and name):
        return False
    prints = _load_district_prints(dcode).get(name)
    return bool(prints) and all(t[5] for t in prints)


def listing_is_ec(listing: dict) -> bool:
    """Public alias for callers outside the scorer (my_shortlist, reports)."""
    return _listing_is_ec(listing)


# Reserved key inside a district's prints dict for memoized index ratios.
# Normalized project names are [a-z0-9 ]-only, so it can never collide; tying
# the cache to the dict itself means replacing the district entry (tests,
# cache rebuilds) automatically invalidates the derived ratios.
_STALE_INDEX_CACHE_KEY = "__stale_index_ratios__"


def _district_psf_index_ratio(dcode: str, anchor_ym: int, want_ec: bool = False) -> "float | None":
    """District price-index ratio for time-indexing stale prints (v3.8.1).

    ratio = median PSF of the district's prints in the trailing 24mo (all
    sizes pooled) ÷ median PSF of the district's prints within
    ±STALE_INDEX_WINDOW_MONTHS of `anchor_ym` (also all sizes), clamped to
    STALE_INDEX_RATIO_CLAMP. Multiplying a stale print's PSF by it re-values
    the print in today's terms. Returns None when either window holds fewer
    than STALE_INDEX_MIN_DISTRICT_PRINTS prints — no index, no comps.
    `anchor_ym` is an integer month index (year*12 + month). `want_ec` keeps
    the index within one product: EC prints index EC prints, condo prints
    index condo prints (the two move at different levels and paces).
    """
    import config
    import statistics
    dprints = _load_district_prints(dcode)
    cache = dprints.setdefault(_STALE_INDEX_CACHE_KEY, {})
    now = datetime.now()
    now_ym = now.year * 12 + now.month
    key = (anchor_ym, now_ym, bool(want_ec))
    if key in cache:
        return cache[key]
    cutoff = now_ym - FullScorer.TIGHT_COMP_WINDOW_MONTHS
    cur, then = [], []
    for name, plist in dprints.items():
        if name == _STALE_INDEX_CACHE_KEY:
            continue
        for t in plist:
            if t[3] is None or bool(t[5]) != bool(want_ec):
                continue
            ym = t[3].year * 12 + t[3].month
            if ym >= cutoff:
                cur.append(t[1])
            if abs(ym - anchor_ym) <= config.STALE_INDEX_WINDOW_MONTHS:
                then.append(t[1])
    ratio = None
    if (len(cur) >= config.STALE_INDEX_MIN_DISTRICT_PRINTS
            and len(then) >= config.STALE_INDEX_MIN_DISTRICT_PRINTS):
        lo, hi = config.STALE_INDEX_RATIO_CLAMP
        ratio = min(hi, max(lo, statistics.median(cur) / statistics.median(then)))
    cache[key] = ratio
    return ratio


_PROJECT_UNITS_FILE = os.path.join(_DATA_DIR, "project_units.json")
_project_units_cache: dict | None = None
_project_units_norm_index: dict | None = None


def _pu_normalize(name: str) -> str:
    """Conservative name normalization for the project-units join: apostrophe
    variants, '@' vs ' at ', punctuation/whitespace. Recovers portal-vs-URA
    spelling drift ('Skysuites @ Anson' vs 'SKYSUITES@ANSON') without fuzzy
    matching — distinct projects still normalize to distinct strings."""
    s = (name or "").lower().strip()
    s = re.sub(r"[’'`]", "", s)
    s = re.sub(r"\s*@\s*", " at ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s


def _project_units_lookup(project_name: str | None) -> dict | None:
    """Per-project total dwelling units + WGS84 centroid from the URA GIS layer
    (data/project_units.json, built by build_project_units.py).

    v3.5: scraped listings almost never carry total_units (6,348/6,349 missing —
    detail-page enrichment is off by default) or lat/lng, which left the
    dev_size component silently 0 and the future-MRT proximity sub-score blind.
    Both signals were validated on the URA panel (PART 5f: mrt std_β −0.16,
    dev_size +0.05 with controls), so this fallback turns them on for real.
    Exact normalized-name match only — no fuzzy join (a wrong project would
    poison coords + units)."""
    global _project_units_cache, _project_units_norm_index
    if _project_units_cache is None:
        if os.path.exists(_PROJECT_UNITS_FILE):
            with open(_PROJECT_UNITS_FILE) as f:
                _project_units_cache = json.load(f).get("projects", {})
        else:
            _project_units_cache = {}
        _project_units_norm_index = {}
        for k, v in _project_units_cache.items():
            _project_units_norm_index.setdefault(_pu_normalize(k), v)
    if not project_name:
        return None
    entry = _project_units_cache.get(project_name.strip().lower())
    if entry is None:
        entry = (_project_units_norm_index or {}).get(_pu_normalize(project_name))
    if entry and not entry.get("landed_only"):
        return entry
    return None


def build_cohort_stats(listings: list[dict]) -> dict:
    """
    Build cohort-level distributions for percentile-based scoring.

    Uses the current batch of listings to compute:
    - PSF distributions per district
    - MRT distance distribution (overall)
    - Age distributions per district
    """
    psf_by_district: dict[str, list[float]] = {}
    age_by_district: dict[str, list[int]] = {}
    mrt_distances: list[int] = []
    current_year = datetime.now().year

    # Same-unit dedupe: ingestion stamps a `unit_group` on records it has
    # identified as the same physical unit listed by multiple agents (audit:
    # 18.5% of rows were duplicates, distorting cohort percentiles). Count
    # each group once; records without the field behave exactly as before.
    seen_unit_groups: set = set()

    for listing in listings:
        unit_group = listing.get("unit_group")
        if unit_group is not None:
            if unit_group in seen_unit_groups:
                continue
            seen_unit_groups.add(unit_group)

        district = normalize_district(listing.get("district", ""))
        if district:
            psf = listing.get("psf")
            if psf:
                psf_by_district.setdefault(district, []).append(float(psf))

            built_year = listing.get("built_year")
            if built_year:
                age = current_year - built_year
                if age < 0:
                    age = 0
                age_by_district.setdefault(district, []).append(int(age))

        mrt_result = get_mrt_distance(listing)
        if mrt_result:
            _, distance = mrt_result
            mrt_distances.append(distance)

    for d in psf_by_district:
        psf_by_district[d].sort()
    for d in age_by_district:
        age_by_district[d].sort()
    mrt_distances.sort()

    return {
        "psf_by_district": psf_by_district,
        "age_by_district": age_by_district,
        "mrt_distances": mrt_distances,
    }


_default_cohort_cache: dict | None = None


def _default_cohort_stats() -> dict:
    """Cohort distributions from the full listings DB (module-level cache).

    v3.5b (audit 4.12): single-listing flows used to score with
    cohort_stats={}, silently degrading every percentile-based comparison to
    its threshold fallback. The 6k-listing DB is a far better cohort than an
    empty one — seed from it whenever the caller doesn't supply a batch."""
    global _default_cohort_cache
    if _default_cohort_cache is None:
        db_path = os.path.join(_DATA_DIR, "listings_db.json")
        listings: list[dict] = []
        if os.path.exists(db_path):
            try:
                with open(db_path) as f:
                    listings = list(json.load(f).get("listings", {}).values())
            except (OSError, ValueError):
                listings = []
        _default_cohort_cache = build_cohort_stats(listings) if listings else {}
    return _default_cohort_cache


class FullScorer:
    """
    Complete scoring engine for property investment analysis.

    Emits the per-category breakdown (rental yield / capital appreciation /
    future potential / liquidity / cost efficiency / red flags) that MMR
    consumes, plus the trust signals (`psf_premium_pct`, `psf_cohort_txns`,
    stack-print flags) the v3.6-v3.12 data-trust rules key on. The 100-point
    total it also produces is display-only — MMR (`score_1000`) is the
    ranking axis. See the module docstring for the full pipeline.

    Assumes an INVESTMENT purpose (5-7yr hold); own-stay uses the livability /
    overall axes instead.
    """

    # Buyer pool depth scoring (replaces static district popularity)
    # Derived from district_profiles.json: condo density + HDB upgrader pool + employment centers
    BUYER_POOL_SCORES = {
        "very_deep": 7,  # Dense condo belt + large HDB upgrader pool (D14, D15, D19)
        "deep": 5,       # Substantial condo stock + good upgrader catchment
        "moderate": 3,   # Decent but smaller buyer pool
        "shallow": 1,    # Few condos, niche market
    }

    # Price sweet spot for liquidity
    LIQUIDITY_SWEET_SPOT = (1_800_000, 2_500_000)

    def __init__(
        self,
        condo_rental_data: Optional[dict] = None,
        transaction_data: Optional[dict] = None,
        ura_data: Optional[dict] = None,
        cohort_stats: Optional[dict] = None,
    ):
        """
        Initialize full scorer.

        Args:
            condo_rental_data: Dict mapping project_name -> median rent_psf
            transaction_data: Dict mapping project_name -> transaction history data.
                Optional PropertyGuru-side appreciation/txn-count fallback, consulted
                only when the URA cache has no entry for the project. Nothing in the
                repo populates it today (the URA cache is the appreciation source);
                it remains a supported injection point.
            ura_data: Dict mapping project_name -> URA appreciation data (from official govt source)
            cohort_stats: Percentile distributions for this batch. Omitted ->
                seeded from the full listings DB (see _default_cohort_stats).
        """
        self._current_year = datetime.now().year
        self.quick_scorer = QuickScorer()
        self.rental_estimator = RentalEstimator(condo_rental_data)
        self.roi_calculator = ROICalculator()
        self.cost_calculator = CostCalculator()
        self._current_is_ec = False  # set per listing in score()
        self.future_scorer = FutureScorer()  # NEW
        self.district_data = _load_district_data()
        self.district_profiles = self._load_district_profiles()
        self.transaction_data = transaction_data or {}
        self.ura_data = ura_data or {}  # URA official data takes priority
        self._appreciation_cache: dict[str, tuple[float, str]] = {}
        # No batch supplied (single-listing flows) -> seed from the listings DB
        self.cohort_stats = cohort_stats or _default_cohort_stats()

    @staticmethod
    def _load_district_profiles() -> dict:
        """Load district profiles (buyer pool depth, transaction volume, etc.)."""
        profiles_file = os.path.join(_DATA_DIR, "district_profiles.json")
        if os.path.exists(profiles_file):
            with open(profiles_file) as f:
                return json.load(f).get("districts", {})
        return {}

    @staticmethod
    def _percentile_rank(sorted_values: list[float], value: float) -> Optional[float]:
        """Return percentile rank (0-1) of value within sorted_values."""
        if not sorted_values or value is None:
            return None
        n = len(sorted_values)
        if n == 1:
            return 0.5
        idx = bisect_left(sorted_values, value)
        return max(0.0, min(1.0, idx / (n - 1)))

    @staticmethod
    def _percentile_to_points(quality_pct: float, max_points: int) -> int:
        """Bucket percentile quality into points (higher quality_pct is better)."""
        if quality_pct is None:
            return 0
        if max_points == 5:
            if quality_pct >= 0.80:
                return 5
            if quality_pct >= 0.60:
                return 4
            if quality_pct >= 0.40:
                return 3
            if quality_pct >= 0.20:
                return 1
            return 0
        if max_points == 4:
            if quality_pct >= 0.80:
                return 4
            if quality_pct >= 0.60:
                return 3
            if quality_pct >= 0.40:
                return 2
            if quality_pct >= 0.20:
                return 1
            return 0
        # Fallback: scale linearly
        return int(round(quality_pct * max_points))

    @staticmethod
    def score_appreciation_rate_points(apr_pct: float, appreciation_source: str) -> int:
        """Score appreciation rate (0-14) based on annual % rate."""
        apr_score = 0
        if apr_pct >= 6.0:
            apr_score = 14
        elif apr_pct >= 5.0:
            apr_score = 12
        elif apr_pct >= 4.0:
            apr_score = 10
        elif apr_pct >= 3.0:
            apr_score = 7
        elif apr_pct >= 2.0:
            apr_score = 5
        elif apr_pct >= 1.0:
            apr_score = 2
        elif apr_pct >= 0:
            apr_score = 0
        else:
            apr_score = -3

        if appreciation_source in {"default", "regional_baseline"}:
            apr_score = min(apr_score, 4)

        return apr_score

    def _fuzzy_ura_lookup(self, project_key: str) -> Optional[dict]:
        """Look up URA data with fuzzy matching.

        Tries exact match first, then normalized token overlap matching
        to reduce false positives from naive substring checks. Only entries of
        the listing's own product are eligible (EC ↔ EC, condo ↔ condo).
        """
        want_ec = bool(getattr(self, "_current_is_ec", False))
        data = self._ura_by_kind(want_ec)

        # Exact match
        if project_key in data:
            return data[project_key]

        if not project_key:
            return None

        def normalize(text: str) -> str:
            text = re.sub(r"\s*\(.*?\)", "", text).lower()
            text = re.sub(r"[^a-z0-9]+", " ", text)
            return re.sub(r"\s+", " ", text).strip()

        stopwords = {
            "the", "at", "by", "of", "and",
            "residence", "residences", "residential",
            "condominium", "condo", "apartments", "apartment",
            "home", "homes",
        }

        def tokenize(text: str) -> set[str]:
            return {t for t in normalize(text).split() if t and t not in stopwords}

        normalized = normalize(project_key)
        if not normalized:
            return None

        # Try normalized exact match against normalized cache keys
        for cache_key in data:
            if normalize(cache_key) == normalized:
                return data[cache_key]

        # Token overlap match (prefer strongest overlap, avoid loose substring)
        target_tokens = tokenize(project_key)
        if len(target_tokens) < 2:
            return None

        best_key = None
        best_score = 0.0

        for cache_key in data:
            if cache_key == project_key:
                return data[cache_key]

            cache_norm = normalize(cache_key)
            if cache_norm == normalized:
                return data[cache_key]

            cache_tokens = tokenize(cache_key)
            if len(cache_tokens) < 2:
                continue

            overlap_count = len(target_tokens & cache_tokens)
            if overlap_count < 2:
                continue

            coverage = overlap_count / max(len(target_tokens), 1)
            cand_coverage = overlap_count / max(len(cache_tokens), 1)
            score = (coverage + cand_coverage) / 2

            if coverage >= 0.6 and cand_coverage >= 0.5 and score > best_score:
                best_score = score
                best_key = cache_key

        if best_key:
            return data[best_key]

        return None

    def _ura_by_kind(self, want_ec: bool) -> dict:
        """self.ura_data restricted to one product (EC or condo), memoized.

        Rebuilt if ura_data is replaced. Entries without `is_ec` are condos —
        the whole cache before the EC export existed — so condo lookups see
        exactly what they always saw.
        """
        memo = getattr(self, "_ura_kind_memo", None)
        if memo is None or memo[0] is not self.ura_data:
            ec = {k: v for k, v in self.ura_data.items()
                  if isinstance(v, dict) and v.get("is_ec")}
            condo = {k: v for k, v in self.ura_data.items() if k not in ec}
            memo = (self.ura_data, condo, ec)
            self._ura_kind_memo = memo
        return memo[2] if want_ec else memo[1]

    def _get_appreciation_rate(
        self,
        listing: dict,
    ) -> tuple[float, str]:
        """
        Get appreciation rate for a listing.

        Priority order:
        1. URA resale-only data (if available and property has new-launch bias)
        2. URA official data (5 years history, most reliable)
        3. Pre-fetched transaction data (only if `transaction_data` was injected)
        4. Regional baseline fallback (flagged for AI follow-up)

        Returns:
            Tuple of (annual_rate_decimal, source)
            source: "ura_resale_only", "ura_5yr_cagr", "ura_3yr_avg", "transaction_data", "regional_baseline"
        """
        project_name = listing.get("project_name") or listing.get("title", "")
        district = listing.get("district", "")
        cache_key = f"{project_name}_{district}".lower()
        project_key = project_name.lower()

        # Check cache first
        if cache_key in self._appreciation_cache:
            return self._appreciation_cache[cache_key]

        # Priority 1: URA official data (most reliable - 5 years govt data)
        data = self._fuzzy_ura_lookup(project_key)
        if data is not None:

            # NEW: Prefer resale-only appreciation if new-launch bias detected
            if data.get("has_new_launch_bias") and data.get("resale_annualized_appreciation") is not None:
                rate = data["resale_annualized_appreciation"] / 100
                rate = max(-0.05, min(0.15, rate))
                source = "ura_resale_only"
                result = (rate, source)
                self._appreciation_cache[cache_key] = result
                return result

            if data.get("annualized_appreciation") is not None:
                rate = data["annualized_appreciation"] / 100  # Convert % to decimal
                rate = max(-0.05, min(0.15, rate))  # Cap between -5% and 15%
                source = data.get("source", "ura_data")
                result = (rate, source)
                self._appreciation_cache[cache_key] = result
                return result

        # Priority 2: Pre-fetched transaction data (PropertyGuru)
        if project_name and cache_key in self.transaction_data:
            data = self.transaction_data[cache_key]
            if data.get("annualized_appreciation") is not None:
                rate = data["annualized_appreciation"] / 100  # Convert % to decimal
                rate = max(-0.05, min(0.10, rate))  # Cap between -5% and 10%
                result = (rate, "transaction_data")
                self._appreciation_cache[cache_key] = result
                return result

        # Fallback: Regional baseline appreciation (flag for AI follow-up)
        baseline = self._get_regional_baseline(listing)
        return (baseline, "regional_baseline")

    def _get_regional_baseline(self, listing: dict) -> float:
        """Get regional baseline appreciation rate for a listing's location."""
        district = listing.get("district", "")
        d = district.upper().replace("D", "").strip()
        try:
            d_num = int(d)
        except ValueError:
            return DEFAULT_REGIONAL_APPRECIATION

        # Map district to region (CCR/RCR/OCR)
        ccr_districts = {1, 2, 6, 7, 9, 10, 11}
        rcr_districts = {3, 4, 5, 8, 12, 13, 14, 15}
        # Everything else is OCR

        if d_num in ccr_districts:
            return REGIONAL_APPRECIATION_BASELINES.get("CCR", DEFAULT_REGIONAL_APPRECIATION)
        elif d_num in rcr_districts:
            return REGIONAL_APPRECIATION_BASELINES.get("RCR", DEFAULT_REGIONAL_APPRECIATION)
        else:
            return REGIONAL_APPRECIATION_BASELINES.get("OCR", DEFAULT_REGIONAL_APPRECIATION)

    def _apply_new_launch_adjustment(
        self,
        raw_rate: float,
        listing: dict,
        appreciation_source: str,
        has_new_launch_bias: bool,
    ) -> tuple[float, float, str]:
        """
        Adjust appreciation rate to account for new-launch bias.

        New launches show inflated appreciation in their first ~5-7 years
        because transaction data mixes developer pricing with market pricing.
        This method discounts the "excess" appreciation above the regional
        baseline for young properties.

        Args:
            raw_rate: Raw appreciation rate (decimal, e.g., 0.06 = 6%/yr)
            listing: Listing dict with built_year, district, etc.
            appreciation_source: Source of the raw rate

        Returns:
            Tuple of (adjusted_rate, adjustment_amount, reason)
            adjustment_amount is negative (how much was discounted)
        """
        built_year = listing.get("built_year")
        current_year = self._current_year

        # "ura_resale_only" is exempt from the discount ONLY for mature
        # projects. For very young projects (<5yr since TOP) the "resales"
        # are sub-sales/early flips bought at developer prices, so even the
        # resale-only CAGR captures the launch ramp and must be discounted.
        resale_only_mature = (
            appreciation_source == "ura_resale_only"
            and built_year is not None
            and (current_year - built_year) >= 5
        )

        # No adjustment if:
        # - No built_year known (can't determine age)
        # - Source is resale-only AND the project is mature (genuinely clean)
        # - Source is "regional_baseline" (nothing to adjust)
        # - Rate is negative (no excess to discount)
        # - URA data indicates no new-launch bias
        if (
            built_year is None
            or resale_only_mature
            or appreciation_source in {"default", "regional_baseline"}
            or raw_rate <= 0
            or not has_new_launch_bias
        ):
            return raw_rate, 0.0, ""

        age = current_year - built_year
        if age < 0:
            age = 0

        # Find applicable discount factor from the decay curve
        discount_factor = 0.0
        for age_max in sorted(NEW_LAUNCH_DISCOUNT_CURVE.keys()):
            if age <= age_max:
                discount_factor = NEW_LAUNCH_DISCOUNT_CURVE[age_max]
                break

        if discount_factor <= 0:
            return raw_rate, 0.0, ""  # Property is mature, no adjustment

        # Get regional baseline
        regional_baseline = self._get_regional_baseline(listing)

        # Only discount the "excess" above regional baseline
        excess = raw_rate - regional_baseline
        if excess <= 0:
            return raw_rate, 0.0, ""  # Rate is at or below baseline, no excess

        adjustment = -(excess * discount_factor)
        adjusted_rate = raw_rate + adjustment

        # Never adjust below the regional baseline
        adjusted_rate = max(adjusted_rate, regional_baseline)
        actual_adjustment = adjusted_rate - raw_rate

        reason = (
            f"New launch bias adj: {age}yr old property, "
            f"{discount_factor:.0%} discount on {excess*100:.1f}% excess "
            f"above {regional_baseline*100:.1f}% regional baseline"
        )

        return adjusted_rate, actual_adjustment, reason

    def _ec_status(self, listing: dict, ura_entry: Optional[dict]) -> dict:
        """EC lifecycle stage + eligibility for the active buyer profile.

        The land award year comes from URA's tenure string (the EC's own cache
        entry); the portal's tenure ("99-year Leasehold") carries no year.
        """
        from scoring import ec_rules
        import buyer_profile
        try:
            prof = buyer_profile.active()
        except Exception:
            prof = None
        return ec_rules.ec_status(
            top_year=listing.get("built_year"),
            tenure=(ura_entry or {}).get("tenure") or listing.get("tenure"),
            household_citizenship=prof.household_citizenship if prof else None,
        )

    def _apply_ec_lifecycle_adjustment(
        self, rate: float, adjustment: float, reason: str, listing: dict,
        appreciation_source: str, ec_status: dict,
    ) -> tuple[float, float, str]:
        """Cap an EC's trailing appreciation at the regional baseline while its
        5-year window still contains lifecycle re-rating.

        An EC's trailing CAGR (median 7.0%/yr resale-only vs 4.7% for OCR
        condos, 2021-26) is mostly the EC lifecycle, not market momentum: the
        subsidised launch price unwinding, then the MOP and privatisation
        re-ratings. A buyer entering now captures at most what is left of that
        path, and the forward model was never validated on ECs. So — like the
        new-launch bias, and for the same reason — the excess above the
        regional baseline is discounted until the project privatised more than
        five years ago (then its window is ordinary condo trading). The raw
        rate stays visible in factual_data for the agent to judge.
        """
        if appreciation_source in {"default", "regional_baseline"}:
            return rate, adjustment, reason
        priv_year = ec_status.get("privatised_year")
        if priv_year is not None and priv_year <= self._current_year - 5:
            return rate, adjustment, reason
        baseline = self._get_regional_baseline(listing)
        if rate <= baseline:
            return rate, adjustment, reason
        delta = baseline - rate
        why = (f"EC lifecycle adj: trailing {rate * 100:.1f}%/yr capped at the "
               f"{baseline * 100:.1f}% regional baseline (stage {ec_status.get('stage')}, "
               f"privatises {priv_year or 'unknown'}) — launch subsidy and MOP/privatisation "
               f"re-rating are not repeatable forward returns")
        return baseline, adjustment + delta, (f"{reason}; {why}" if reason else why)

    def score(self, listing: dict[str, Any]) -> ScoredListing:
        """
        Perform full scoring of a listing.

        Args:
            listing: Listing dictionary from scraper

        Returns:
            ScoredListing with complete scoring and ROI analysis
        """
        # Normalize district early for consistent scoring logic
        if listing.get("district"):
            listing = dict(listing)
            listing["district"] = normalize_district(listing.get("district", "")) or listing.get("district")
        # Which URA product this listing is: _fuzzy_ura_lookup only ever joins an
        # EC to EC project stats and a condo to condo stats (ECs carry `is_ec`
        # in the cache since the EC export landed).
        self._current_is_ec = _listing_is_ec(listing)

        # v3.5: fill total_units + coordinates from the URA dwelling-units GIS
        # layer when the scrape didn't provide them (it almost never does) —
        # activates the dev_size component and the future-MRT proximity score.
        pu = _project_units_lookup(listing.get("project_name") or listing.get("title"))
        if pu and (not listing.get("total_units")
                   or not listing.get("latitude") or not listing.get("longitude")):
            listing = dict(listing)
            if not listing.get("total_units"):
                listing["total_units"] = pu["total_units"]
            if not listing.get("latitude") or not listing.get("longitude"):
                listing["latitude"] = pu["lat"]
                listing["longitude"] = pu["lng"]
                listing["coords_source"] = "project_centroid"

        # Start with quick score
        quick_score = self.quick_scorer.score(listing)

        # Create scored listing
        scored = ScoredListing(
            id=str(listing.get("id", "")),
            title=listing.get("title", ""),
            price=listing.get("price", 0),
            url=listing.get("url", ""),
            address=listing.get("address"),
            district=listing.get("district"),
            district_name=listing.get("district_name"),
            beds=listing.get("beds"),
            baths=listing.get("baths"),
            sqft=listing.get("sqft"),
            psf=listing.get("psf"),
            tenure=listing.get("tenure"),
            built_year=listing.get("built_year"),
            project_name=listing.get("project_name"),
            total_units=listing.get("total_units"),
            mrt_info=listing.get("mrt_info"),
            latitude=listing.get("latitude"),
            longitude=listing.get("longitude"),
            image_url=listing.get("image_url"),
            listing_date=listing.get("listing_date"),
            floor_level=listing.get("floor_level"),
            facing=listing.get("facing"),
            quick_score=quick_score.score,
            quick_tier=quick_score.tier,
        )

        # Calculate remaining lease
        scored.remaining_lease = self._calculate_remaining_lease(listing)

        # Get MRT distance
        mrt_result = get_mrt_distance(listing)
        if mrt_result:
            scored.nearest_mrt, scored.mrt_distance_m = mrt_result

        # Get appreciation rate FIRST (needed for capital appreciation scoring)
        raw_rate, appreciation_source = self._get_appreciation_rate(listing)

        # Apply new-launch bias adjustment
        project_name = listing.get("project_name") or listing.get("title", "")
        ura_entry = self._fuzzy_ura_lookup(project_name.lower()) if project_name else None
        has_new_launch_bias = bool(ura_entry and ura_entry.get("has_new_launch_bias"))
        adjusted_rate, adjustment, adj_reason = self._apply_new_launch_adjustment(
            raw_rate, listing, appreciation_source, has_new_launch_bias,
        )

        # Executive Condominiums: lifecycle facts + appreciation de-bias.
        ec_status = None
        if self._current_is_ec:
            ec_status = self._ec_status(listing, ura_entry)
            adjusted_rate, adjustment, adj_reason = self._apply_ec_lifecycle_adjustment(
                adjusted_rate, adjustment, adj_reason, listing, appreciation_source, ec_status)

        scored.raw_appreciation_rate = raw_rate
        scored.appreciation_rate = adjusted_rate  # Use adjusted rate everywhere
        scored.appreciation_source = appreciation_source
        scored.appreciation_adjustment = adjustment
        scored.adjustment_reason = adj_reason

        # Use the adjusted rate for scoring
        appreciation_rate = adjusted_rate

        # Score each category
        breakdown = {}
        if ec_status is not None:
            breakdown["ec_status"] = ec_status

        # A. Rental Yield Potential (15 pts - reduced from 30)
        rental_scores = self._score_rental_yield(listing, scored)
        scored.rental_yield_score = rental_scores["total"]
        breakdown["rental_yield"] = rental_scores

        # B. Capital Appreciation (30 pts - uses actual URA data, bias-adjusted)
        cap_scores = self._score_capital_appreciation(listing, scored, appreciation_rate, appreciation_source)
        scored.capital_appreciation_score = cap_scores["total"]
        breakdown["capital_appreciation"] = cap_scores

        # C. Future Potential (20 pts - NEW)
        future_score = self.future_scorer.score(listing)
        scored.future_potential_score = future_score.total
        scored.future_score_details = future_score
        breakdown["future_potential"] = future_score.to_dict()

        # D. Liquidity & Exit Risk (25 pts)
        liquidity_scores = self._score_liquidity(listing, scored)
        scored.liquidity_score = liquidity_scores["total"]
        breakdown["liquidity"] = liquidity_scores

        # E. Cost Efficiency (10 pts)
        cost_scores = self._score_cost_efficiency(listing, scored)
        scored.cost_efficiency_score = cost_scores["total"]
        breakdown["cost_efficiency"] = cost_scores

        # F. Red Flags (-10 pts max)
        red_flag_scores = self._score_red_flags(listing, scored)
        scored.red_flag_deductions = red_flag_scores["total"]
        breakdown["red_flags"] = red_flag_scores

        scored.score_breakdown = breakdown

        # Age-adjusted relative value vs district peers (None when data thin).
        # An EC benchmarks against EC peers only, a condo against condos only.
        from scoring.relative_value import relative_value_for_listing
        rel_value = relative_value_for_listing(scored, self.ura_data, self._current_year,
                                               subject_is_ec=_listing_is_ec(listing))
        if rel_value:
            breakdown["relative_value"] = rel_value

        # MMR (v3): continuous uncapped rating + /1000 normalization
        from scoring.mmr import compute_mmr
        mmr_result = compute_mmr(scored)
        scored.mmr = mmr_result["mmr"]
        scored.score_1000 = mmr_result["score_1000"]
        scored.mmr_components = mmr_result["components"]
        breakdown["mmr"] = mmr_result

        # 3-score system (v3.11): Valuation (priced-right-today, backtested) and
        # Livability (own-stay home quality, heuristic) beside the MMR investment
        # axis, combined into purpose-weighted Overalls. Defensive — a new-module
        # error must never break the core score.
        try:
            from scoring.valuation import score_valuation
            from scoring.livability import score_livability
            from scoring.overall import compute_overall
            _val = score_valuation(scored)
            _liv = score_livability(listing if isinstance(listing, dict) else {})
            _ov = compute_overall(valuation=_val["score"], livability=_liv["score"],
                                  score_1000=scored.score_1000)
            scored.valuation_score = _val["score"]
            scored.valuation_detail = _val
            scored.livability_score = _liv["score"]
            scored.livability_components = _liv["components"]
            scored.overall_own_stay = _ov["own_stay"]
            scored.overall_investment = _ov["investment"]
            breakdown["three_score"] = {"valuation": _val, "livability": _liv, "overall": _ov}
        except Exception:
            pass

        # Calculate ROI for different periods
        if scored.sqft and scored.price:
            roi_results = self.roi_calculator.calculate_multiple_periods(
                listing,
                periods=[5, 6, 7],
                monthly_rent=scored.estimated_monthly_rent if scored.estimated_monthly_rent > 0 else None,
                appreciation_rate=appreciation_rate,
            )
            scored.roi_5yr = roi_results.get(5)
            scored.roi_6yr = roi_results.get(6)
            scored.roi_7yr = roi_results.get(7)

            # ROI sensitivity scenarios (downside/base/upside)
            sensitivity = self.roi_calculator.calculate_sensitivity(
                listing,
                periods=[5, 7],
                monthly_rent=scored.estimated_monthly_rent if scored.estimated_monthly_rent > 0 else None,
                appreciation_rate=appreciation_rate,
            )
            def _roi_summary(roi):
                return {
                    "exit_price": roi.estimated_exit_price,
                    "total_return": round(roi.total_return, 2),
                    "roi_pct": round(roi.roi_percent, 2),
                    "annualized_roi": round(roi.annualized_roi, 2),
                }
            scored.roi_sensitivity = {
                "assumptions": {
                    "rent_delta_pct": ROI_SENSITIVITY_RENT_DELTA_PCT,
                    "appreciation_delta_pct": ROI_SENSITIVITY_APPRECIATION_DELTA_PCT,
                },
                "5yr": {
                    "downside": _roi_summary(sensitivity["downside"][5]),
                    "upside": _roi_summary(sensitivity["upside"][5]),
                },
                "7yr": {
                    "downside": _roi_summary(sensitivity["downside"][7]),
                    "upside": _roi_summary(sensitivity["upside"][7]),
                },
            }

        return scored

    def _score_rental_yield(self, listing: dict, scored: ScoredListing) -> dict:
        """
        Score rental yield potential (15 pts max - reduced from 30).

        v2.0: Weight reduced because yields are mechanically low at $2M+ prices.

        Components:
        - Gross Yield vs Market: 0-6 pts (was 0-12)
        - MRT Proximity: 0-4 pts (was 0-8)
        - Unit Config: 0-3 pts (was 0-5)
        - Tenant Pool: 0-2 pts (was 0-5)
        """
        scores = {"total": 0}

        # Estimate rental
        rental_data = self.rental_estimator.estimate_yield_score(listing)
        scored.estimated_monthly_rent = rental_data["monthly_rent"]
        scored.estimated_gross_yield = rental_data["gross_yield"]
        scored.rent_source = rental_data["source"]
        # Cross-agent contract: the rental estimator may carry a numeric
        # `confidence` on the estimate — surface it on the scored listing when
        # present (defensive .get: older estimator versions don't emit it).
        rent_confidence = rental_data.get("confidence")
        if rent_confidence is not None:
            scored.rent_confidence = rent_confidence
            scores["rent_confidence"] = rent_confidence
        # Audit #5: the EVIDENCE behind that confidence — URA contract depth,
        # the serving window those contracts came from, and whether the v3.6.2
        # sqft cap engaged. raw_output surfaces them as factual_data.rental
        # .rent_evidence so a yield can be audited the same way a PSF comp is
        # (a >5.5% gross yield is a verify-first artifact signal, and "90 real
        # contracts" vs "no contracts at all" is the difference that decides
        # it). Metadata ONLY — nothing here feeds a component or the MMR; the
        # rent basis itself was already fixed upstream in the estimator.
        # Defensive .get for the same reason as confidence: an older estimator
        # may not emit them, and absent evidence must read as None/False.
        scored.rent_contracts = rental_data.get("contracts")
        scored.rent_window_months = rental_data.get("window_months")
        scored.rent_capped = bool(rental_data.get("capped", False))

        # Gross Yield Score (0-6 pts) - was 0-12
        raw_yield_score = rental_data["gross_yield_score"]
        # Scale down from 12 to 6 without banker's rounding
        yield_score = min(6, math.ceil(raw_yield_score / 2))
        scores["gross_yield"] = {
            "yield_pct": rental_data["gross_yield"],
            "points": yield_score,
        }
        scores["total"] += yield_score

        # MRT Proximity (0-4 pts) - was 0-8
        mrt_score = 0
        mrt_distance = scored.mrt_distance_m
        method = "thresholds"
        percentile_quality = None
        cohort_distances = self.cohort_stats.get("mrt_distances", [])
        if mrt_distance is not None and len(cohort_distances) >= 5:
            rank = self._percentile_rank(cohort_distances, mrt_distance)
            if rank is not None:
                percentile_quality = round(1 - rank, 2)
                mrt_score = self._percentile_to_points(percentile_quality, 4)
                method = "percentile"

        if method == "thresholds" and mrt_distance is not None:
            if mrt_distance <= 300:
                mrt_score = 4
            elif mrt_distance <= 500:
                mrt_score = 3
            elif mrt_distance <= 800:
                mrt_score = 2
            elif mrt_distance <= 1000:
                mrt_score = 1
        scores["mrt_proximity"] = {
            "distance_m": mrt_distance,
            "station": scored.nearest_mrt,
            "points": mrt_score,
            "method": method,
            "percentile_quality": percentile_quality,
        }
        scores["total"] += mrt_score

        # Unit Configuration (0-3 pts) - was 0-5
        beds = listing.get("beds")
        config_score = 0
        if beds == 2:
            config_score = 3  # Most rentable
        elif beds == 3:
            config_score = 2  # Good for families
        elif beds == 1:
            config_score = 2  # Smaller tenant pool
        elif beds == 4:
            config_score = 1
        scores["unit_config"] = {"beds": beds, "points": config_score}
        scores["total"] += config_score

        # Tenant Pool (0-2 pts) - was 0-5
        lat = listing.get("latitude")
        lon = listing.get("longitude")
        pool_score = 1  # Default
        pool_reason = "Unknown location"
        if lat and lon:
            raw_pool_score, pool_reason = get_tenant_pool_score(lat, lon)
            pool_score = min(2, round(raw_pool_score / 2))  # Scale down
        scores["tenant_pool"] = {"reason": pool_reason, "points": pool_score}
        scores["total"] += pool_score

        return scores

    def _get_transaction_count(self, listing: dict) -> int:
        """Get transaction count for a listing from URA or transaction data."""
        project_name = listing.get("project_name") or listing.get("title", "")
        project_key = project_name.lower()
        district = listing.get("district", "")

        # Check URA data first (fuzzy)
        ura_entry = self._fuzzy_ura_lookup(project_key)
        if ura_entry:
            return ura_entry.get("transaction_count", 0)

        # Check transaction data (compound key)
        txn_key = f"{project_name}_{district}".lower()
        txn_data = self.transaction_data.get(txn_key) or self.transaction_data.get(project_key)
        if isinstance(txn_data, dict):
            return txn_data.get("transaction_count", 0)

        return 0

    def _get_momentum(self, listing: dict) -> tuple[Optional[float], str]:
        """Get appreciation momentum and data coverage for a listing.

        Prefers an explicit appreciation_momentum field; otherwise derives it
        from the cache's 1yr vs annualized 5yr rates (recent above long-term
        trend = accelerating). Without the fallback this signal was always
        None — the cache builder never writes appreciation_momentum.
        """
        project_name = listing.get("project_name") or listing.get("title", "")
        project_key = project_name.lower()

        data = self._fuzzy_ura_lookup(project_key)
        if data:
            momentum = data.get("appreciation_momentum")
            if momentum is not None:
                return momentum, data.get("data_coverage", "none")

            # Derive: relative gap between recent (1yr) and long-term rate
            recent = data.get("appreciation_1yr")
            long_term = data.get("annualized_appreciation")
            if recent is not None and long_term is not None and abs(long_term) >= 0.5:
                derived = (recent - long_term) / abs(long_term)
                derived = max(-1.0, min(1.0, derived))
                return derived, "derived_1yr_vs_5yr"

        return None, "none"

    def _score_capital_appreciation(
        self,
        listing: dict,
        scored: ScoredListing,
        appreciation_rate: float,
        appreciation_source: str,
    ) -> dict:
        """
        Score capital appreciation potential (30 pts max).

        v2.2: Includes confidence scaling based on transaction count,
        and momentum scoring based on appreciation trend direction.

        Components:
        - Actual Appreciation Rate: 0-14 pts (reduced from 17, freed pts for momentum)
        - Momentum (trend direction): -2 to +3 pts (NEW)
        - PSF vs District Median: 0-5 pts
        - Tenure: 0-4 pts
        - Property Age: 0-4 pts
        """
        scores = {"total": 0}
        current_year = self._current_year

        # --- Actual Appreciation Rate (0-14 pts, was 0-17) ---
        # Reduced by 3 pts to make room for momentum scoring.
        apr_pct = appreciation_rate * 100
        apr_score = self.score_appreciation_rate_points(apr_pct, appreciation_source)

        # Confidence scaling: reduce score for low transaction counts
        # Full confidence at 50+ txns, scaling down to 60% at <10 txns
        txn_count = self._get_transaction_count(listing)
        confidence = 1.0
        if txn_count > 0 and appreciation_source not in {"default", "regional_baseline"}:
            if txn_count >= 50:
                confidence = 1.0
            elif txn_count >= 20:
                confidence = 0.9
            elif txn_count >= 10:
                confidence = 0.8
            else:
                confidence = 0.6  # Very low confidence
            apr_score = round(apr_score * confidence)

        scores["appreciation_rate"] = {
            "rate_pct": round(apr_pct, 2),
            "source": appreciation_source,
            "points": apr_score,
            "confidence": round(confidence, 2),
            "transaction_count": txn_count,
        }
        scores["total"] += apr_score

        # --- Momentum Scoring (-2 to +3 pts, NEW) ---
        # Rewards accelerating appreciation, penalizes deceleration.
        # A property gaining momentum (recent > long-term) is more attractive
        # than one losing steam (recent < long-term).
        momentum, data_coverage = self._get_momentum(listing)
        momentum_score = 0
        if momentum is not None:
            if momentum >= 0.4:
                momentum_score = 3   # Strong acceleration
            elif momentum >= 0.15:
                momentum_score = 2   # Moderate acceleration
            elif momentum >= 0.0:
                momentum_score = 1   # Stable/slight acceleration
            elif momentum >= -0.2:
                momentum_score = 0   # Mild deceleration (acceptable)
            elif momentum >= -0.5:
                momentum_score = -1  # Significant deceleration
            else:
                momentum_score = -2  # Severe deceleration (warning)

        scores["momentum"] = {
            "value": round(momentum, 3) if momentum is not None else None,
            "data_coverage": data_coverage,
            "points": momentum_score,
        }
        scores["total"] += momentum_score

        # PSF vs District Median (0-5 pts) - was 0-10
        psf = listing.get("psf")
        district = normalize_district(listing.get("district", ""))
        psf_score = 0
        psf_details = {"psf": psf, "median": None, "points": psf_score, "method": "median_ratio"}

        psf_values = self.cohort_stats.get("psf_by_district", {}).get(district)
        if psf and psf_values and len(psf_values) >= 5:
            rank = self._percentile_rank(psf_values, psf)
            if rank is not None:
                percentile_quality = round(1 - rank, 2)
                psf_score = self._percentile_to_points(percentile_quality, 5)
                psf_details = {
                    "psf": psf,
                    "points": psf_score,
                    "method": "percentile",
                    "percentile_quality": percentile_quality,
                }
        else:
            medians = self.district_data.get("medians", {})
            if psf and district in medians:
                median_psf = medians[district].get("psf", 2000)
                ratio = psf / median_psf if median_psf > 0 else 1
                if ratio <= 0.85:
                    psf_score = 5  # 15%+ below median
                elif ratio <= 0.95:
                    psf_score = 4  # 5-15% below median
                elif ratio <= 1.00:
                    psf_score = 3  # At or below median
                elif ratio <= 1.10:
                    psf_score = 1  # Up to 10% above
                psf_details = {
                    "psf": psf,
                    "median": median_psf,
                    "ratio": round(ratio, 2),
                    "points": psf_score,
                    "method": "median_ratio",
                }
            else:
                # Fallback scoring
                if psf:
                    if psf < 1800:
                        psf_score = 5
                    elif psf < 2000:
                        psf_score = 4
                    elif psf < 2200:
                        psf_score = 2
                    elif psf < 2400:
                        psf_score = 1
                psf_details = {"psf": psf, "median": None, "points": psf_score, "method": "absolute"}

        scores["psf_vs_median"] = psf_details
        scores["total"] += psf_score

        # Tenure (0-4 pts) - was 0-5
        tenure = (listing.get("tenure") or "").lower()
        remaining = scored.remaining_lease
        tenure_score = 0

        if "freehold" in tenure or "999" in tenure:
            tenure_score = 2  # FH has premium, less upside
        elif "99" in tenure:
            if remaining and remaining >= 90:
                tenure_score = 4
            elif remaining and remaining >= 80:
                tenure_score = 3
            elif remaining and remaining >= 70:
                tenure_score = 1
        scores["tenure"] = {
            "tenure": tenure,
            "remaining_years": remaining,
            "points": tenure_score,
        }
        scores["total"] += tenure_score

        # Property Age (0-4 pts)
        # v2.2: This now scores PHYSICAL CONDITION value only.
        # Data reliability bias is handled separately by:
        #   - Confidence scaling (transaction count)
        #   - New-launch discount (appreciation adjustment)
        #   - Default cap (no-data penalty)
        # So age score reflects: newer = better maintained, less MCST risk.
        built_year = listing.get("built_year")
        age_score = 0
        age = None
        age_method = "thresholds"
        percentile_quality = None
        if built_year:
            age = current_year - built_year
            if age < 0:
                age = 0  # Future TOP — treat as brand new

            age_values = self.cohort_stats.get("age_by_district", {}).get(district)
            if age_values and len(age_values) >= 5:
                rank = self._percentile_rank(age_values, age)
                if rank is not None:
                    percentile_quality = round(1 - rank, 2)
                    age_score = self._percentile_to_points(percentile_quality, 4)
                    age_method = "percentile"

            if age_method == "thresholds":
                # v2.2 sweet spot is 3-7yr: brand-new units (<3yr) score slightly
                # below max so they aren't double-rewarded on top of (possibly
                # residual) new-launch appreciation inflation.
                if age <= 2:
                    age_score = 3  # Brand new — premium pricing risk
                elif age <= 7:
                    age_score = 4  # Sweet spot: modern but past launch premium
                elif age <= 12:
                    age_score = 3  # Good condition
                elif age <= 17:
                    age_score = 2  # Aging but maintained
                elif age <= 21:
                    age_score = 1
        scores["property_age"] = {
            "built_year": built_year,
            "age_years": age,
            "points": age_score,
            "method": age_method,
            "percentile_quality": percentile_quality,
        }
        scores["total"] += age_score

        # Clamp total to valid range [0, SCORE_WEIGHT_CAPITAL_APPRECIATION]
        scores["total"] = max(0, min(scores["total"], SCORE_WEIGHT_CAPITAL_APPRECIATION))

        return scores

    def _score_liquidity(self, listing: dict, scored: ScoredListing) -> dict:
        """
        Score liquidity and exit risk (25 pts max).

        Components:
        - Transaction Volume (total): 0-8 pts
        - Buyer Pool Depth: 0-7 pts (condo density + HDB upgrader pool)
        - Development Size: 0-5 pts
        - Price Appeal: 0-5 pts
        """
        scores = {"total": 0}

        # Transaction Volume (0-8 pts)
        project_name = listing.get("project_name", "")
        title = project_name or listing.get("title") or ""
        district = listing.get("district", "")
        # transaction_data is keyed by f"{project}_{district}".lower() from get_batch_appreciation
        txn_cache_key = f"{title}_{district}".lower()
        txn_data = self.transaction_data.get(txn_cache_key) or self.transaction_data.get(project_name.lower())
        txn_count = txn_data.get("transaction_count", 0) if isinstance(txn_data, dict) else (txn_data or 0)
        # Also check URA data for transaction count (fuzzy)
        project_key = title.lower()
        ura_entry = self._fuzzy_ura_lookup(project_key)
        if ura_entry:
            ura_txn = ura_entry.get("transaction_count", 0)
            txn_count = max(txn_count, ura_txn)
        txn_score = 0
        if txn_count >= 50:
            txn_score = 8
        elif txn_count >= 20:
            txn_score = 6
        elif txn_count >= 10:
            txn_score = 4
        elif txn_count >= 5:
            txn_score = 2
        scores["transaction_volume"] = {"count": txn_count, "points": txn_score}
        scores["total"] += txn_score

        # Buyer Pool Depth (0-7 pts) — replaces static district popularity
        # Uses condo density + HDB upgrader pool data from district_profiles.json
        district = normalize_district(listing.get("district", ""))
        d_num = district.upper().replace("D", "").strip().lstrip("0") or "0"
        profile = self.district_profiles.get(d_num, {})
        # Missing district/profile → depth None, NOT "moderate": MMR's
        # _BUYER_POOL_PTS lookup maps an unknown depth to 0 (neutral), so a
        # listing with no district no longer earns +1.5 for missing data
        # (audit #9). The legacy /100 default of 3 pts is kept unchanged.
        buyer_pool = profile.get("buyer_pool_depth")
        pool_score = self.BUYER_POOL_SCORES.get(buyer_pool, 3)
        scores["buyer_pool_depth"] = {"district": district, "depth": buyer_pool, "points": pool_score}
        scores["total"] += pool_score

        # Development Size (0-5 pts)
        total_units = listing.get("total_units")
        size_score = 0
        if total_units:
            if total_units >= 600:
                size_score = 5
            elif total_units >= 400:
                size_score = 4
            elif total_units >= 200:
                size_score = 3
            elif total_units >= 100:
                size_score = 1
        scores["dev_size"] = {"total_units": total_units, "points": size_score}
        scores["total"] += size_score

        # Price Appeal (0-5 pts)
        price = listing.get("price", 0)
        price_score = 0
        min_sweet, max_sweet = self.LIQUIDITY_SWEET_SPOT
        if min_sweet <= price <= max_sweet:
            price_score = 5  # Sweet spot
        elif price < min_sweet:
            price_score = 4  # Below sweet spot, still liquid
        elif price <= 3_000_000:
            price_score = 2  # Above sweet spot
        else:
            price_score = 1  # Premium segment
        scores["price_appeal"] = {
            "price": price,
            "sweet_spot": f"${min_sweet:,}-${max_sweet:,}",
            "points": price_score,
        }
        scores["total"] += price_score

        return scores

    def _score_cost_efficiency(self, listing: dict, scored: ScoredListing) -> dict:
        """
        Score cost efficiency (10 pts max).

        Components:
        - MCST Estimate: 0-4 pts
        - Property Tax Band: 0-3 pts
        - Efficiency Ratio: 0-3 pts

        Missing-data neutrality (audit #9): each sub-score contributes its
        NEUTRAL MIDPOINT when its input is absent (MCST 2.0, tax 1.5,
        efficiency 1.5), so an all-missing listing totals 5.0 and MMR's
        `cost = score - 5` lands at 0 instead of -5 raw (~-43 display) —
        missing data is neutral, never penalized.
        """
        scores = {"total": 0.0}

        sqft = listing.get("sqft", 0)

        # MCST Estimate (0-4 pts; 2.0 neutral when sqft unknown)
        mcst_rate = self.cost_calculator.params.get("mcst_rate_per_sqft", 0.35)
        mcst_monthly = sqft * mcst_rate if sqft else 0
        if mcst_monthly > 0:
            mcst_score = 0
            if mcst_monthly < 350:
                mcst_score = 4
            elif mcst_monthly < 450:
                mcst_score = 3
            elif mcst_monthly < 550:
                mcst_score = 2
            elif mcst_monthly < 650:
                mcst_score = 1
        else:
            mcst_score = 2.0  # missing sqft → neutral midpoint
        scores["mcst"] = {"monthly_estimate": round(mcst_monthly), "points": mcst_score}
        scores["total"] += mcst_score

        # Property Tax Band (0-3 pts; 1.5 neutral when rent unknown)
        # Based on estimated rental
        monthly_rent = scored.estimated_monthly_rent
        annual_value = monthly_rent * 12 if monthly_rent else 0
        if annual_value > 0:
            # Lower quartile roughly < $50k AV
            if annual_value < 50000:
                tax_score = 3
            elif annual_value < 70000:
                tax_score = 2
            else:
                tax_score = 1
        else:
            tax_score = 1.5  # no rent estimate → neutral midpoint
        scores["property_tax"] = {
            "annual_value_estimate": round(annual_value),
            "points": tax_score,
        }
        scores["total"] += tax_score

        # Efficiency Ratio (0-3 pts; 1.5 neutral when sqft/beds unknown)
        beds = listing.get("beds", 0)
        eff_score = 1.5  # missing sqft or beds → neutral midpoint
        if sqft and beds:
            sqft_per_bed = sqft / beds
            # Optimal efficiency varies by bed count
            if beds == 1:
                optimal = 500 <= sqft <= 650
            elif beds == 2:
                optimal = 400 <= sqft_per_bed <= 500
            elif beds == 3:
                optimal = 350 <= sqft_per_bed <= 420
            else:
                optimal = sqft_per_bed <= 400

            if optimal:
                eff_score = 3
            elif beds == 2 and 350 <= sqft_per_bed <= 550:
                eff_score = 2
            elif beds == 3 and 300 <= sqft_per_bed <= 480:
                eff_score = 2
            else:
                eff_score = 1
        scores["efficiency_ratio"] = {
            "sqft": sqft,
            "beds": beds,
            "sqft_per_bed": round(sqft / beds, 1) if sqft and beds else None,
            "points": eff_score,
        }
        scores["total"] += eff_score

        return scores

    def _score_red_flags(self, listing: dict, scored: ScoredListing) -> dict:
        """
        Calculate red flag deductions (-10 pts max).

        Red flags:
        - West-facing: -1
        - Small dev (<100 units): -2
        - Lease <70 years: -3 (auto-reject if <60)
        - Old property (>15 years): -2 to -4
        - Very low PSF (<$1,300): -2
        - Oversized unit: -2
        - PSF overpriced vs URA transactions: -2 to -3 (NEW v2.3)
        - Bedroom/sqft mismatch: -2 (NEW v2.3)
        """
        flags = []
        total_penalty = 0
        result: dict[str, Any] = {}

        # West-facing (-1)
        facing = (listing.get("facing") or "").lower()
        if "west" in facing:
            flags.append({"flag": "west_facing", "penalty": 1, "reason": "West-facing unit (hot afternoons)"})
            total_penalty += 1

        # Small development (-2)
        total_units = listing.get("total_units")
        if total_units and total_units < 100:
            flags.append({"flag": "small_dev", "penalty": 2, "reason": f"Small development ({total_units} units)"})
            total_penalty += 2

        # Low remaining lease (-3)
        remaining = scored.remaining_lease
        if remaining is not None and 0 < remaining < 70:
            flags.append({"flag": "low_lease", "penalty": 3, "reason": f"Low remaining lease ({remaining} years)"})
            total_penalty += 3

        # Old property (-2 to -4)
        built_year = listing.get("built_year")
        current_year = self._current_year
        if built_year:
            age = current_year - built_year
            if age > 20:
                flags.append({"flag": "very_old_property", "penalty": 4, "reason": f"Very old property ({age} years) - maintenance risk"})
                total_penalty += 4
            elif age > 15:
                flags.append({"flag": "old_property", "penalty": 2, "reason": f"Older property ({age} years)"})
                total_penalty += 2

        # Suspiciously low PSF (-2) - may indicate problems
        psf = listing.get("psf")
        if psf and psf < 1300:
            flags.append({"flag": "very_low_psf", "penalty": 2, "reason": f"Unusually low PSF (${psf:,.0f}) - investigate condition"})
            total_penalty += 2

        # Inefficient large unit (-2)
        sqft = listing.get("sqft")
        beds = listing.get("beds")
        if sqft and beds:
            sqft_per_bed = sqft / beds
            if beds == 2 and sqft_per_bed > 700:
                flags.append({"flag": "oversized_unit", "penalty": 2, "reason": f"Large 2BR ({sqft_per_bed:.0f} sqft/bed) - harder to rent"})
                total_penalty += 2
            elif beds == 3 and sqft_per_bed > 550:
                flags.append({"flag": "oversized_unit", "penalty": 2, "reason": f"Large 3BR ({sqft_per_bed:.0f} sqft/bed) - harder to rent"})
                total_penalty += 2

        # --- NEW v2.3: PSF overpricing vs URA transaction data ---
        # Compare listing PSF against URA median PSF for the same project.
        # Flags listings priced significantly above recent market transactions.
        psf_premium_pct, psf_cohort_txns = self._check_psf_overpricing(listing)
        if psf_premium_pct is not None:
            result["psf_premium_pct"] = round(psf_premium_pct, 1)
            # Number of same-size comparable transactions behind the premium —
            # MMR uses this to weight the psf signal and damp borrowed
            # project-wide signals when a unit's own size-cohort is thin.
            result["psf_cohort_txns"] = psf_cohort_txns
            if psf_premium_pct > 15:
                flags.append({
                    "flag": "psf_overpriced",
                    "penalty": 3,
                    "reason": f"Asking PSF {psf_premium_pct:.0f}% above URA transaction median - likely overpriced",
                })
                total_penalty += 3
            elif psf_premium_pct > 10:
                flags.append({
                    "flag": "psf_above_market",
                    "penalty": 2,
                    "reason": f"Asking PSF {psf_premium_pct:.0f}% above URA transaction median",
                })
                total_penalty += 2

        # --- v3.8: the unit's OWN stack prints (tight ±7% same-size comps) ---
        # Exported so MMR can distrust district-level "cheapness" the stack's
        # own prints contradict, and so the agent sees the stack's floor
        # reality (low_floor_share ≥ 0.7 ⇒ the stack IS ground/low floor).
        import config
        tight_med, n_tight, low_share, p10, prints_latest, prints_stale = \
            self._tight_size_comps(listing)
        # Audit #4: absence-of-comps must be VISIBLE, not a silent skip. When
        # the project joins to zero URA prints in its district CSV (EC/strata-
        # landed blind spots, residual name drift), say so explicitly.
        project_prints = self._project_prints(listing)
        if project_prints is not None and not project_prints:
            result["no_ura_prints"] = True
        # v3.10 EC join: surface WHICH comp universe an EC listing landed in.
        # `ec_benchmark` confirms it matched real EC prints (not condo medians);
        # `ec_no_ec_prints` marks an EC listing with zero EC prints to compare
        # against (EC file unfetched, or fetched but the project has none) — a
        # visible verify-first state instead of a silent condo-median benchmark.
        if _listing_is_ec(listing) and project_prints is not None:
            if project_prints:
                result["ec_benchmark"] = True
            else:
                result["ec_no_ec_prints"] = True
        # Audit #5: comp freshness — newest same-size print (YYYY-MM), exported
        # even when the window leaves too few comps (that's exactly when the
        # agent needs to see how stale the print set is).
        if prints_latest:
            result["stack_prints_latest"] = prints_latest
        if tight_med and n_tight >= 2 and listing.get("psf") and prints_stale:
            # --- v3.8.1 LOW-TRUST branch: INDEXED stale prints ---
            # The comp set is the stack's own stale prints re-valued by the
            # district price index — directionally right (it re-arms the v3.8
            # PES protection for stacks that stopped trading) but noisy, so
            # every trigger runs at a raised threshold vs the fresh path.
            stack_premium = (listing["psf"] - tight_med) / tight_med * 100
            result["stack_prints_n"] = n_tight
            result["stack_prints_stale"] = True
            result["stack_premium_pct_indexed"] = round(stack_premium, 1)
            if low_share is not None:
                result["stack_low_floor_share"] = round(low_share, 2)
            # `stack_premium_pct` is what arms mmr's >+5 print-contradiction
            # suspect damp. For an indexed set the damp must only fire above
            # the +8 noise margin, and mmr's threshold is fixed at >5 — so the
            # field is exported only past the margin (the raw indexed number
            # stays visible in stack_premium_pct_indexed either way).
            if stack_premium > config.STALE_PREMIUM_SUSPECT_MIN_PCT:
                result["stack_premium_pct"] = round(stack_premium, 1)
            if stack_premium > config.STALE_ABOVE_PRINTS_PCT:
                floor_note = (f", a low-floor stack ({low_share:.0%} of prints at 01-05)"
                              if (low_share or 0) >= 0.7 else "")
                flags.append({
                    "flag": "ask_above_own_stack_prints",
                    "penalty": 2,
                    "reason": (f"Asking {stack_premium:.0f}% above the unit's own "
                               f"same-size prints, district-indexed to today "
                               f"(n={n_tight}, latest {prints_latest}{floor_note})"),
                })
                total_penalty += 2
            elif (p10 is not None
                    and listing["psf"] < p10 * config.STALE_BELOW_P10_FACTOR):
                flags.append({
                    "flag": "ask_below_stack_prints",
                    "penalty": 2,
                    "reason": (f"Asking ${listing['psf']:,.0f} psf is >10% below "
                               f"every one of the {n_tight} same-size prints "
                               f"district-indexed to today (min ${p10:,.0f}) — "
                               f"bait price, double-volume/PES format, or sqft "
                               f"error; verify before crediting"),
                })
                total_penalty += 2
        elif tight_med and n_tight >= 2 and listing.get("psf"):
            stack_premium = (listing["psf"] - tight_med) / tight_med * 100
            result["stack_prints_n"] = n_tight
            result["stack_premium_pct"] = round(stack_premium, 1)
            if low_share is not None:
                result["stack_low_floor_share"] = round(low_share, 2)
            if stack_premium > 10:
                floor_note = (f", a low-floor stack ({low_share:.0%} of prints at 01-05)"
                              if (low_share or 0) >= 0.7 else "")
                flags.append({
                    "flag": "ask_above_own_stack_prints",
                    "penalty": 2,
                    "reason": (f"Asking {stack_premium:.0f}% above the unit's own "
                               f"same-size prints (n={n_tight}{floor_note})"),
                })
                total_penalty += 2
            # v3.9: ask BELOW (nearly) the entire recent print distribution.
            # With a deep comp set, real sellers don't price 15-20% under every
            # recent print — such asks are bait prices, void-format units
            # (double-volume lofts whose strata sqft includes the void, so the
            # paper PSF deflates exactly like ground-floor PES patios), or sqft
            # errors. The scan's ranking SELECTS for these artifacts, so they
            # must be verify-first, never auto-credited as value.
            # Audit #4-corridor: a 5-7 print set escaped BOTH this rule (needed
            # n≥8) and the thin-cohort damp (fires at n<5) — a bait ask 20%
            # under all 6 prints got full credit. For that corridor, an ask
            # below the MINIMUM in-window print fires the same flag (note: for
            # n ≤ 10 the stored p10 IS the minimum print, vals[0]).
            elif (p10 is not None and listing["psf"] < p10 * 0.97
                    and n_tight >= 8):
                flags.append({
                    "flag": "ask_below_stack_prints",
                    "penalty": 2,
                    "reason": (f"Asking ${listing['psf']:,.0f} psf is below the "
                               f"10th percentile (${p10:,.0f}) of {n_tight} recent "
                               f"same-size prints — bait price, double-volume/PES "
                               f"format, or sqft error; verify before crediting"),
                })
                total_penalty += 2
            elif (p10 is not None and config.MIN_BAND_TXNS <= n_tight < 8
                    and listing["psf"] < p10):
                flags.append({
                    "flag": "ask_below_stack_prints",
                    "penalty": 2,
                    "reason": (f"Asking ${listing['psf']:,.0f} psf is below every "
                               f"one of the {n_tight} recent same-size prints "
                               f"(min ${p10:,.0f}) — bait price, double-volume/PES "
                               f"format, or sqft error; verify before crediting"),
                })
                total_penalty += 2

        # --- NEW v2.3: Bedroom/sqft mismatch validation ---
        # Flag listings where sqft doesn't match expected range for bedroom count
        mismatch = self._check_bedroom_sqft_mismatch(listing)
        # Cross-agent contract: ingestion may pre-flag the same artifact in the
        # record's `ingest_flags`; honor it exactly like our own detection
        # (same flag name → same MMR suspect-damp semantics).
        if not mismatch and "bedroom_sqft_mismatch" in (listing.get("ingest_flags") or []):
            mismatch = "Bedroom/sqft mismatch flagged at ingest (scraper-side validation)"
        if mismatch:
            flags.append({
                "flag": "bedroom_sqft_mismatch",
                "penalty": 2,
                "reason": mismatch,
            })
            total_penalty += 2

        # Cap at 10
        total_penalty = min(total_penalty, 10)

        result["flags"] = flags
        result["total"] = total_penalty
        result["uncapped_total"] = sum(f["penalty"] for f in flags)
        return result

    def _size_aware_median_psf(self, ura_entry: dict, sqft) -> tuple:
        """Benchmark PSF for a listing, compared against SIMILAR-SIZE units.

        A project's pooled median mixes 1BRs with penthouses, so a small unit
        always looks "overpriced" and a large one "cheap" against it. Prefer the
        median PSF of the project's matching size band; fall back to the recent
        pooled median (thin band), then the all-time pooled median (no size data).

        Returns (median_psf, cohort_txns, basis):
          - cohort_txns is the number of same-size transactions behind the chosen
            benchmark — int when size data exists (0 = no same-size sales), or
            None when the project has no size-band data at all. Downstream uses it
            to set confidence (thin cohort → weak signal, not a strong verdict).
        """
        import config
        pooled = ura_entry.get("median_psf") or ura_entry.get("avg_psf_current")
        by_size = ura_entry.get("by_size") or {}
        if not by_size:
            return pooled, None, "pooled"  # un-rebuilt project: neutral confidence
        bands = by_size.get("bands") or {}
        recent_pooled = by_size.get("recent_median_psf") or pooled
        key = config.size_band_key(sqft) if sqft else None
        if key and key in bands:
            band = bands[key]
            cnt = band.get("txn_count") or 0
            if cnt >= config.MIN_BAND_TXNS and band.get("median_psf"):
                return band["median_psf"], cnt, f"size_band:{key}"
            # Thin band: too few same-size sales to fully anchor a benchmark —
            # but DISCARDING them is worse. Structurally-unique stacks (ground-
            # floor PES units run oversized for their bed count) have permanently
            # thin bands that trade far below the pooled median, so the pooled
            # fallback manufactured a fake discount: a Jun-2026 audit found 13 of
            # the top-50 /1000 ranks were low-floor stacks, 10 of them asking
            # ABOVE their own band's prints. Shrink the band median toward the
            # pooled median by sample size instead (n/MIN_BAND_TXNS), so even 2-3
            # real same-size prints anchor most of the benchmark.
            if band.get("median_psf") and cnt > 0 and recent_pooled:
                w = cnt / config.MIN_BAND_TXNS
                blended = w * band["median_psf"] + (1 - w) * recent_pooled
                return blended, cnt, f"thin_band:{key}"
            return recent_pooled, cnt, f"thin_band:{key}"
        # Unknown sqft, or a band with no recorded sales: treat as thin cohort.
        return recent_pooled, 0, "no_band_match"

    # Tight-comp matching: ±7% sqft isolates a stack's true peers (PES units
    # have distinctive floor areas); 24mo window keeps the prints current.
    TIGHT_COMP_SQFT_TOL = 0.07
    TIGHT_COMP_WINDOW_MONTHS = 24

    def _project_prints(self, listing: dict) -> "list | None":
        """The listing's project URA prints (normalized-name join, audit #4).

        Returns None when the lookup is impossible (no name or district),
        [] when the lookup ran but the project has zero MATCHING prints — the
        caller exports that as an explicit `no_ura_prints` flag instead of a
        silent skip (strata-landed projects structurally have none).

        v3.10 EC join: an EC listing benchmarks ONLY against EC prints and a
        condo listing ONLY against condo prints — never the other type's
        medians (the v3.6 cross-format artifact). When an EC listing has no EC
        prints (EC file not fetched, or fetched but the project has none) this
        returns [] rather than silently borrowing the project's condo prints,
        so the caller flags a visible verify-first state (`ec_no_ec_prints`).
        """
        name = (listing.get("project_name") or listing.get("title") or "").strip()
        dcode = normalize_district(str(listing.get("district") or ""))
        if not (name and dcode):
            return None
        prints = _load_district_prints(dcode).get(_pu_normalize(name)) or []
        want_ec = _listing_is_ec(listing)
        return [t for t in prints if bool(t[5]) == want_ec]

    def _tight_size_comps(self, listing: dict) -> tuple:
        """Median PSF of the project's recent prints within ±7% of the listing's
        sqft — the unit's true comp set, immune to band pooling.

        Returns (median_psf, n, low_floor_share, p10_psf, latest_print_ym,
        stale); (None, n_in_window, None, None, latest, False) without ≥2
        usable prints. low_floor_share is the fraction of comps printed at
        floors 01-05 — ≥0.7 means the whole stack IS low floor (e.g.
        ground-floor PES), so its pricing already embeds the floor discount.
        p10_psf is the 10th-percentile print — an ask meaningfully below it
        sits under (nearly) the whole recent distribution, which real sellers
        don't do: it flags bait pricing, a void/PES format hiding in the same
        total sqft, or a data error (v3.9). latest_print_ym ("YYYY-MM") is the
        newest same-size print, exported for comp-freshness even when the
        window leaves no usable comps. stale=True marks an INDEXED stale comp
        set (see below) — callers must treat it as low-trust.

        Audit-hardening (Jun 2026):
        - #5 stale window: the 24mo cutoff anchors to TODAY, not the project's
          own latest print — raw stale prints never accuse a fairly-priced
          2026 ask (the Archipelago lesson, reproduced in the old
          implementation). Undated prints never anchor a benchmark.
        - v3.8.1 stale-print indexing: discarding stale sets outright (the
          first #5 fix) disarmed the v3.8 PES protection exactly where it
          matters — PES stacks rarely re-trade, so their own prints go stale
          while the project stays liquid (live regression: Coco Palms 624sf
          PES, last same-size print 2022, rode the coarse band back to #1).
          With <2 in-window but ≥2 dated all-time same-size prints, the stale
          prints are re-valued in today's terms via the district price index
          (_district_psf_index_ratio) and served with stale=True so callers
          halve the blend weight, force the damping cohort thin, and raise
          flag thresholds (index noise). No computable index → neutral.
        - P2 hygiene: when ≥TIGHT_COMP_MIN_RESALE_PRINTS in-window resale
          prints exist, the comp set restricts to them (developer New Sale
          pricing pollutes a resale's "own prints" distribution).
        - #6 floor basis: when the listing's floor tier is known, each print
          is adjusted to the listing's tier via the project/district
          floor_factors (psf × f_listing / f_print) before the median/p10 —
          so a fairly-priced high-floor unit no longer reads "above its own
          prints" just because the prints are floor-mixed. Without factors,
          fall back to same-tier prints when ≥TIGHT_COMP_SAME_TIER_MIN exist,
          else the floor-mixed set unadjusted (pre-fix behavior). PES-stack
          detection is untouched: a low/unknown-floor listing against its
          low-floor-dominated prints sees no adjustment, and low_floor_share
          still reports the comp set's floor mix.
        """
        import config
        sqft = listing.get("sqft")
        dcode = normalize_district(str(listing.get("district") or ""))
        prints = self._project_prints(listing)
        if not (sqft and dcode and prints):
            return None, 0, None, None, None, False
        same = [t for t in prints if abs(t[0] - sqft) / sqft <= self.TIGHT_COMP_SQFT_TOL]
        if not same:
            return None, 0, None, None, None, False
        dated = [t for t in same if t[3] is not None]
        latest_dt = max((t[3] for t in dated), default=None)
        latest = latest_dt.strftime("%Y-%m") if latest_dt else None
        now = datetime.now()
        cutoff = now.year * 12 + now.month - self.TIGHT_COMP_WINDOW_MONTHS
        recent = [t for t in dated if t[3].year * 12 + t[3].month >= cutoff]
        if len(recent) < 2:
            # <2 fresh prints. v3.8.1: time-index the stale set instead of
            # discarding it (raw stale prints still never serve as comps).
            return self._indexed_stale_comps(dated, dcode, latest, len(recent))
        same = recent
        # Resale-only restriction (developer pricing is not a resale comp).
        resales = [t for t in same if (t[4] or "").strip().lower() == "resale"]
        if len(resales) >= config.TIGHT_COMP_MIN_RESALE_PRINTS:
            same = resales
        # Floor-basis adjustment / restriction (audit #6).
        adj_vals = None
        listing_tier = config.normalize_floor_tier(listing.get("floor_level"))
        if listing_tier:
            factors = {}
            if getattr(self, "ura_data", None):
                pname = (listing.get("project_name") or listing.get("title") or "")
                entry = self._fuzzy_ura_lookup(pname.lower())
                factors = (entry or {}).get("floor_factors") or {}
            tier_f = {t: factors.get(t) for t in ("low", "mid", "high")}
            if any(tier_f.values()):
                f_listing = tier_f.get(listing_tier) or 1.0
                adj_vals = [
                    t[1] * f_listing / (tier_f.get(config.normalize_floor_tier(t[2])) or 1.0)
                    for t in same
                ]
            else:
                same_tier = [t for t in same
                             if config.normalize_floor_tier(t[2]) == listing_tier]
                if len(same_tier) >= config.TIGHT_COMP_SAME_TIER_MIN:
                    same = same_tier
        import statistics
        vals = sorted(adj_vals) if adj_vals is not None else sorted(t[1] for t in same)
        med = statistics.median(vals)
        p10 = vals[max(0, int(0.10 * (len(vals) - 1)))]
        low_share = sum(1 for t in same if t[2] == "01 to 05") / len(same)
        return med, len(vals), low_share, p10, latest, False

    def _indexed_stale_comps(self, dated: list, dcode: str, latest,
                             n_recent: int) -> tuple:
        """LOW-TRUST comp set from stale same-size prints, re-valued in today's
        terms via the district price index (v3.8.1 — the audit's recommended
        alternative to both the all-time fallback and the discard).

        Each stale print's PSF is multiplied by one district index ratio
        (trailing-24mo district median ÷ district median around the stale
        prints' median date). Returns the _tight_size_comps tuple with
        stale=True; the neutral tuple (None, n_recent, None, None, latest,
        False) when <2 dated prints exist or no index is computable.

        Same resale-only hygiene as the fresh path. No floor adjustment: the
        target artifact is a low-floor PES listing against its own low-floor
        prints (no adjustment in the fresh path either), and an indexed set is
        too low-trust to compound with a second correction — the raised
        thresholds the callers apply absorb floor-mix noise instead.
        """
        import config
        import statistics
        if len(dated) < 2:
            return None, n_recent, None, None, latest, False
        resales = [t for t in dated if (t[4] or "").strip().lower() == "resale"]
        if len(resales) >= config.TIGHT_COMP_MIN_RESALE_PRINTS:
            dated = resales
        months = sorted(t[3].year * 12 + t[3].month for t in dated)
        mid = len(months) // 2
        anchor_ym = (months[mid] if len(months) % 2
                     else round((months[mid - 1] + months[mid]) / 2))
        ratio = _district_psf_index_ratio(dcode, anchor_ym,
                                          want_ec=bool(dated and dated[0][5]))
        if ratio is None:
            return None, n_recent, None, None, latest, False
        vals = sorted(t[1] * ratio for t in dated)
        med = statistics.median(vals)
        p10 = vals[max(0, int(0.10 * (len(vals) - 1)))]
        low_share = sum(1 for t in dated if t[2] == "01 to 05") / len(dated)
        return med, len(vals), low_share, p10, latest, True

    def _check_psf_overpricing(self, listing: dict) -> tuple:
        """
        Check if listing PSF is above the URA median for SIMILAR-SIZE units.

        Returns:
            (premium_pct, cohort_txns) — premium as a percentage above the
            size-matched median (negative = discount); cohort_txns is the number
            of same-size comparable transactions (None when no URA/size data).
            Returns (None, None) when no URA data is available.
        """
        psf = listing.get("psf")
        if not psf:
            return None, None

        project_name = listing.get("project_name") or listing.get("title", "")
        if not project_name:
            return None, None

        ura_entry = self._fuzzy_ura_lookup(project_name.lower())
        if not ura_entry:
            return None, None

        median_psf, cohort_txns, _basis = self._size_aware_median_psf(
            ura_entry, listing.get("sqft"))
        if not median_psf or median_psf <= 0:
            return None, None

        # Floor adjustment: the size-band median pools all floors, so a low-floor
        # unit looks dear and a high-floor unit cheap against it. Scale the
        # benchmark by the district floor-tier factor (size-confound already
        # removed) so the premium reflects price-vs-comparable-floor-AND-size.
        # Neutral (no shift) when floor is unknown or the tier factor is absent.
        import config
        tier = config.normalize_floor_tier(listing.get("floor_level"))
        factor = (ura_entry.get("floor_factors") or {}).get(tier) if tier else None
        if factor:
            median_psf *= factor

        # v3.8 ground-floor-stack fix: blend in TIGHT same-size comps from raw
        # URA prints, weighted by print count. The band benchmark above pools a
        # stack's true peers with differently-floored/sized units (Jun-2026
        # audit: 13 of the top-50 /1000 ranks were low-floor PES stacks whose
        # band made them look 15-20% "cheap" while asking ABOVE their own
        # prints). The tight median embeds the stack's real floor mix, and
        # reporting its count as the cohort lets the v3.2 thin-cohort damping
        # fire for stacks that rarely trade.
        tight_med, n_tight, _low_share, _p10, _latest, prints_stale = \
            self._tight_size_comps(listing)
        if tight_med and n_tight >= 2:
            if prints_stale:
                # v3.8.1: indexed stale prints are a LOW-TRUST benchmark — they
                # blend at HALF a fresh set's weight, and the reported cohort
                # is forced thin (min(n, MIN_BAND_TXNS-1)) so the v3.2 damping
                # and the v3.6 thin-cohort+deep-discount suspect rule stay
                # armed no matter how many stale prints back the index.
                w = (config.STALE_COMP_BLEND_FACTOR
                     * min(1.0, n_tight / config.MIN_BAND_TXNS))
                median_psf = w * tight_med + (1 - w) * median_psf
                cohort_txns = min(n_tight, config.MIN_BAND_TXNS - 1)
            else:
                w = min(1.0, n_tight / config.MIN_BAND_TXNS)
                median_psf = w * tight_med + (1 - w) * median_psf
                # Blend-consistent cohort (audit P2): the old `cohort_txns = n_tight`
                # let 2-3 tight prints demote a deep band cohort to "thin" even
                # while the benchmark stayed 60% band-derived. Mix the counts with
                # the same weight as the medians (pure-tight at w=1 keeps n_tight).
                band_cnt = cohort_txns or 0
                cohort_txns = round(w * n_tight + (1 - w) * band_cnt)

        premium_pct = ((psf - median_psf) / median_psf) * 100
        return premium_pct, cohort_txns

    # Expected sqft ranges per bedroom count (Singapore condo market)
    _BEDROOM_SQFT_RANGES = {
        1: (400, 850),
        2: (600, 1200),
        3: (850, 1800),
        4: (1200, 2500),
        5: (1600, 3500),
    }

    def _check_bedroom_sqft_mismatch(self, listing: dict) -> Optional[str]:
        """
        Check if sqft is inconsistent with bedroom count.

        Returns:
            Warning message string if mismatch detected, None otherwise.
        """
        beds = listing.get("beds")
        sqft = listing.get("sqft")
        if not beds or not sqft:
            return None

        expected = self._BEDROOM_SQFT_RANGES.get(beds)
        if not expected:
            return None

        min_sqft, max_sqft = expected
        if sqft < min_sqft:
            return (
                f"{beds}BR at {sqft:.0f} sqft is below expected minimum "
                f"({min_sqft} sqft) - possible misclassification or micro unit"
            )
        if sqft > max_sqft:
            return (
                f"{beds}BR at {sqft:.0f} sqft is above expected maximum "
                f"({max_sqft} sqft) - possible misclassification"
            )
        return None

    # Typical gap between lease commencement and TOP (years).
    # Leases start when the land is purchased by the developer, which is
    # typically 3-4 years before TOP. Using built_year (TOP year) alone
    # overstates remaining lease by this amount.
    _LEASE_START_OFFSET = 3

    def _calculate_remaining_lease(self, listing: dict) -> Optional[int]:
        """Calculate remaining lease years.

        Uses lease_start_year if available, otherwise estimates lease
        commencement as built_year minus a standard offset (typically 3 years)
        since 99-year leases start when the developer acquires the land,
        not when the building reaches TOP.
        """
        tenure = (listing.get("tenure") or "").lower()
        current_year = self._current_year

        if "freehold" in tenure or "999" in tenure:
            return 999

        lease_match = re.search(r"(\d+)[\s-]?year", tenure)
        if lease_match:
            lease_years = int(lease_match.group(1))
        elif "99" in tenure:
            lease_years = 99
        else:
            return None

        # Prefer explicit lease_start_year if available
        lease_start = listing.get("lease_start_year")
        if lease_start:
            elapsed = current_year - lease_start
            return max(0, lease_years - elapsed)

        # Fall back to built_year/top_year with offset for 99-year leases
        start_year = listing.get("built_year") or listing.get("top_year")
        if not start_year:
            if lease_years == 99:
                return 90
            return None

        # For 99-year leases, subtract offset to approximate lease commencement
        if lease_years == 99:
            estimated_lease_start = start_year - self._LEASE_START_OFFSET
            elapsed = current_year - estimated_lease_start
        else:
            elapsed = current_year - start_year

        return max(0, lease_years - elapsed)

def score_listings(
    listings: list[dict],
    condo_rental_data: Optional[dict] = None,
    transaction_data: Optional[dict] = None,
    ura_data: Optional[dict] = None,
) -> list[ScoredListing]:
    """
    Score a list of listings.

    Args:
        listings: List of listing dictionaries
        condo_rental_data: Optional condo rental data
        transaction_data: Optional transaction history data
        ura_data: Optional URA appreciation data (official govt source, highest priority)

    Returns:
        List of ScoredListing objects, sorted by rank_score descending
    """
    cohort_stats = build_cohort_stats(listings)
    scorer = FullScorer(
        condo_rental_data,
        transaction_data,
        ura_data,
        cohort_stats=cohort_stats,
    )
    scored = [scorer.score(listing) for listing in listings]
    scored.sort(key=lambda x: x.rank_score, reverse=True)
    return scored


def score_and_filter(
    listings: list[dict],
    min_quick_score: int = 40,
    condo_rental_data: Optional[dict] = None,
    transaction_data: Optional[dict] = None,
    ura_data: Optional[dict] = None,
) -> dict[str, list[ScoredListing]]:
    """
    Quick filter then full score remaining listings.

    Uses QuickScorer (scraped data only) to pre-filter obvious rejects,
    then runs FullScorer on everything above the threshold.

    Args:
        listings: List of listing dictionaries
        min_quick_score: Minimum quick score to keep for full scoring.
            Listings with score=0 (hard-rejected by lease) are always excluded.
            Default 40 is intentionally lenient since quick scorer lacks URA data.
        condo_rental_data: Optional condo rental data
        transaction_data: Optional transaction history data
        ura_data: Optional URA appreciation data (official govt source, highest priority)

    Returns:
        Dict with "scored" (full analysis) and "rejected" (quick filtered) lists
    """
    # Phase 1: Quick filter using min_quick_score threshold
    quick_scorer = QuickScorer()
    to_score = []
    rejected = []

    for listing in listings:
        # Audit P2 (quick-filter survivorship): the project-units backfill used
        # to run only inside FullScorer.score(), AFTER this gate — so 907
        # listings were rejected for missing total_units the pipeline could
        # have filled (skewing survivors away from freehold/older projects).
        # Run the same backfill (units + centroid coords) on every candidate
        # BEFORE it is gated.
        pu = _project_units_lookup(listing.get("project_name") or listing.get("title"))
        if pu and (not listing.get("total_units")
                   or not listing.get("latitude") or not listing.get("longitude")):
            listing = dict(listing)
            if not listing.get("total_units"):
                listing["total_units"] = pu["total_units"]
            if not listing.get("latitude") or not listing.get("longitude"):
                listing["latitude"] = pu["lat"]
                listing["longitude"] = pu["lng"]
                listing["coords_source"] = "project_centroid"

        qs = quick_scorer.score(listing)
        # score=0 means hard-rejected (lease too short)
        if qs.score == 0 or qs.score < min_quick_score:
            rejected.append(listing)
        else:
            to_score.append(listing)

    # Phase 2: Full score everything that passed the quick filter
    cohort_stats = build_cohort_stats(to_score)
    scorer = FullScorer(
        condo_rental_data,
        transaction_data,
        ura_data,
        cohort_stats=cohort_stats,
    )
    scored = [scorer.score(listing) for listing in to_score]
    scored.sort(key=lambda x: x.rank_score, reverse=True)

    return {
        "scored": scored,
        "rejected": rejected,
    }
