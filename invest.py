#!/usr/bin/env python3
"""Unified property investment analysis CLI with URA integration.

AI-friendly interface for the complete investment analysis flow:
1. Auto-discover best districts
2. Scrape listings from PropertyGuru
3. Enrich with URA appreciation data (5 years historical)
4. Score with MMR (v3.5 — continuous, backtest-calibrated, /1000 display;
   the legacy /100 breakdown is retained for report context only)
5. Generate investment reports

The MMR components and weights live in config.py / scoring/mmr.py; weight
changes must be justified by backtest_ext.py (see docs/RELEASES.md).

Usage:
    # AUTOMATIC MODE (NEW - recommended)
    python invest.py --auto --beds 2,3 --top 10

    # Discovery only (see district rankings)
    python invest.py --discover-districts

    # Manual district selection
    python invest.py --districts 14 --beds 2,3

    # Multi-district analysis
    python invest.py --districts 3,5,14,15 --min-price 2000000 --max-price 2500000

    # Score existing listings file with URA data
    python invest.py --input output/listings.json --top 20

    # Fetch URA data by district (fastest - all projects in one go)
    python invest.py --fetch-ura-districts 3,5,14,15

    # Build URA cache from downloaded CSV files
    python invest.py --build-ura-cache data/ura_*.csv

    # List all districts for AI reference
    python invest.py --list-districts
"""

import argparse
import json
import os
import re
import sys

import config
from datetime import datetime
from pathlib import Path
from glob import glob as file_glob
from typing import Optional

# Project imports
from scoring.full_scorer import score_and_filter
from scoring.models import ScoredListing
from scoring.district_scorer import DistrictScorer
from scoring.raw_output import save_raw_analysis, load_reviewed_analysis
from utils.markdown import save_report, generate_csv_export
from utils.geo import normalize_district
from config import SCORE_TIER1_MIN, SCORE_TIER2_MIN
try:
    from config import DATA_FRESHNESS_THRESHOLDS_DAYS
except ImportError:
    DATA_FRESHNESS_THRESHOLDS_DAYS = {}


# Paths
DATA_DIR = Path(__file__).parent / "data"
URA_CACHE_FILE = DATA_DIR / "ura_cache.json"
OUTPUT_DIR = Path(__file__).parent / "output"


def _run_slug(text: str) -> str:
    """Sanitize text into a short run-dir suffix ([a-z0-9-], max 40 chars)."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:40]


def url_run_slug(url: str) -> Optional[str]:
    """Derive a run-dir suffix from a PropertyGuru URL so concurrent runs on
    different listings get distinct directories.

    Listing/project URLs end in a numeric ID (.../for-sale-foo-500134933) —
    use that ID. Results pages have no single ID — return None.
    """
    path = url.split("?", 1)[0].rstrip("/#")
    last = path.rsplit("/", 1)[-1]
    m = re.search(r"(\d{4,})$", last)
    if m:
        return m.group(1)
    return None


def next_run_dir(slug: Optional[str] = None) -> Path:
    """Atomically create and return the next sequential run directory.

    Names are output/run_NNN or output/run_NNN_<slug> when a slug is given
    (e.g. the listing ID from a --url run), so concurrent runs on different
    listings land in different directories. Creation uses exist_ok=False and
    retries on collision, so two concurrent processes can never share a dir.
    """
    OUTPUT_DIR.mkdir(exist_ok=True)
    suffix = f"_{_run_slug(slug)}" if slug else ""
    while True:
        max_num = 0
        for d in OUTPUT_DIR.glob("run_*"):
            try:
                num = int(d.name.split("_")[1])
                max_num = max(max_num, num)
            except (IndexError, ValueError):
                continue
        run_dir = OUTPUT_DIR / f"run_{max_num + 1:03d}{suffix}"
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
            return run_dir
        except FileExistsError:
            continue  # another process took this number — rescan and retry


# All Singapore districts with descriptions for AI context
DISTRICTS = {
    1: "Raffles Place, Marina Bay (CCR)",
    2: "Tanjong Pagar, Chinatown (CCR)",
    3: "Queenstown, Alexandra (RCR) ★",
    4: "Harbourfront, Telok Blangah (RCR)",
    5: "Clementi, West Coast (RCR) ★",
    6: "City Hall, Clarke Quay (CCR)",
    7: "Beach Road, Bugis (CCR)",
    8: "Farrer Park, Serangoon Rd (RCR)",
    9: "Orchard, River Valley (CCR)",
    10: "Tanglin, Holland (CCR)",
    11: "Newton, Novena (CCR)",
    12: "Toa Payoh, Balestier (RCR)",
    13: "Macpherson, Potong Pasir (RCR)",
    14: "Eunos, Geylang, Paya Lebar (RCR) ★",
    15: "East Coast, Marine Parade (RCR) ★",
    16: "Bedok, Upper East Coast (OCR)",
    17: "Changi, Loyang (OCR)",
    18: "Pasir Ris, Tampines (OCR)",
    19: "Punggol, Sengkang (OCR)",
    20: "Ang Mo Kio, Bishan (OCR)",
    21: "Clementi Park, Upper Bukit Timah (OCR)",
    22: "Boon Lay, Jurong (OCR)",
    23: "Bukit Batok, Bukit Panjang (OCR)",
    25: "Woodlands, Admiralty (OCR)",
    26: "Mandai, Upper Thomson (OCR)",
    27: "Sembawang, Yishun (OCR)",
    28: "Seletar, Yio Chu Kang (OCR)",
}


def load_ura_cache() -> dict:
    """Load URA appreciation data cache.

    Migrates old entries missing v2.2 bias fields by setting defaults.
    """
    if not URA_CACHE_FILE.exists():
        return {}
    with open(URA_CACHE_FILE) as f:
        data = json.load(f)
    projects = data.get("projects", {})

    # Migrate old entries missing v2.2 bias fields
    migrated = 0
    for key, entry in projects.items():
        if "has_new_launch_bias" not in entry:
            entry.setdefault("new_sale_count", 0)
            entry.setdefault("new_sale_proportion", 0.0)
            entry.setdefault("resale_transaction_count", 0)
            entry.setdefault("resale_annualized_appreciation", None)
            entry.setdefault("has_new_launch_bias", False)
            migrated += 1
    if migrated > 0:
        print(f"  URA cache: migrated {migrated} old entries (missing bias fields)", file=sys.stderr)

    return projects


def save_ura_cache(projects: dict):
    """Save URA appreciation data to cache."""
    cache = {
        "last_updated": datetime.now().strftime("%Y-%m-%d"),
        "projects": projects,
    }
    URA_CACHE_FILE.parent.mkdir(exist_ok=True)
    with open(URA_CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)
    print(f"URA cache saved: {len(projects)} projects", file=sys.stderr)


def warn_if_stale_data():
    """Warn if key local datasets are stale based on last_updated."""
    if not DATA_FRESHNESS_THRESHOLDS_DAYS:
        return

    warnings = []
    for filename, max_days in DATA_FRESHNESS_THRESHOLDS_DAYS.items():
        path = DATA_DIR / filename
        if not path.exists():
            warnings.append(f"{filename}: missing")
            continue
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception:
            warnings.append(f"{filename}: unreadable")
            continue

        last_updated = data.get("last_updated")
        if not last_updated:
            warnings.append(f"{filename}: missing last_updated")
            continue

        dt = None
        try:
            dt = datetime.fromisoformat(last_updated)
        except ValueError:
            try:
                dt = datetime.strptime(last_updated, "%Y-%m-%d")
            except ValueError:
                warnings.append(f"{filename}: invalid last_updated '{last_updated}'")
                continue

        days_old = (datetime.now() - dt).days
        if days_old > max_days:
            warnings.append(f"{filename}: {days_old} days old (>{max_days})")

    if warnings:
        print("\nData freshness warnings:", file=sys.stderr)
        for w in warnings:
            print(f"  - {w}", file=sys.stderr)


def build_ura_cache_from_csv(csv_patterns: list[str]) -> dict:
    """Build URA cache from downloaded CSV files (supports glob patterns)."""
    from scrapers.ura_scraper import load_ura_csv_grouped

    cache = load_ura_cache()

    # Expand glob patterns
    csv_files = []
    for pattern in csv_patterns:
        expanded = file_glob(pattern)
        if expanded:
            csv_files.extend(expanded)
        elif os.path.exists(pattern):
            csv_files.append(pattern)
        else:
            print(f"Warning: No files match: {pattern}", file=sys.stderr)

    if not csv_files:
        print("No CSV files found", file=sys.stderr)
        return cache

    print(f"\nProcessing {len(csv_files)} CSV file(s)...", file=sys.stderr)

    for csv_path in csv_files:
        print(f"  {csv_path}", file=sys.stderr)
        # Executive Condominium prints live in their own *_EC.csv files (a
        # separate URA product). Tag their projects so cross-project consumers
        # (district peers, district rates, lookups) never mix ECs with condos.
        is_ec_file = os.path.basename(csv_path).upper().endswith("_EC.CSV")
        try:
            # One history PER PROJECT — district CSVs hold many projects; the
            # old single-history path mixed their PSF under one arbitrary name.
            histories = load_ura_csv_grouped(csv_path)
            # Floor-tier PSF multipliers, computed once across the whole district
            # (each CSV is one district) and denormalized onto each project — the
            # listing-side floor coverage is thin, so a stable district curve
            # beats noisy per-project floor premia.
            from scrapers.ura_scraper import district_floor_factors, project_floor_factors
            floor_factors = district_floor_factors(histories)
            for history in histories:
                if not history.transactions:
                    continue
                project_key = history.project_name.lower()

                # Project metadata from its transactions
                from collections import Counter as _Counter
                districts = _Counter(t.district for t in history.transactions if t.district)
                segments = _Counter(t.market_segment for t in history.transactions if t.market_segment)
                tenures = _Counter(t.tenure for t in history.transactions if t.tenure)
                district = f"D{districts.most_common(1)[0][0]:02d}" if districts else None
                tenure = tenures.most_common(1)[0][0] if tenures else None
                lease_start_year = None
                if tenure:
                    m = re.search(r"commencing [fF]rom (\d{4})", tenure)
                    if m:
                        lease_start_year = int(m.group(1))

                cache_entry = {
                    "project_name": history.project_name,
                    "district": district,
                    "market_segment": segments.most_common(1)[0][0] if segments else None,
                    "tenure": tenure,
                    "lease_start_year": lease_start_year,
                    "transaction_count": history.transaction_count,
                    "avg_psf_current": history.avg_psf_current_year,
                    "median_psf": history.avg_psf_current_year,  # yearly figures are medians since v2.4
                    "avg_psf_5yr_ago": history.avg_psf_5yr_ago,
                    "appreciation_1yr": history.appreciation_1yr,
                    "appreciation_3yr": history.appreciation_3yr,
                    "appreciation_5yr": history.appreciation_5yr,
                    "annualized_appreciation": history.annualized_appreciation,
                    "source": "ura_5yr_cagr" if history.annualized_appreciation else "ura_data",
                    "updated": datetime.now().strftime("%Y-%m-%d"),
                    # New launch bias detection fields
                    "new_sale_count": history.new_sale_count,
                    "new_sale_proportion": round(history.new_sale_proportion, 3),
                    "resale_transaction_count": history.resale_transaction_count,
                    "resale_annualized_appreciation": history.resale_annualized_appreciation,
                    "has_new_launch_bias": history.has_new_launch_bias,
                    # v2.2: Momentum and data quality
                    "appreciation_momentum": (
                        round(history.appreciation_momentum, 3)
                        if history.appreciation_momentum is not None else None
                    ),
                    "data_coverage": history.data_coverage,
                    # Size-band medians: compare a listing against same-size
                    # transactions instead of the project-pooled median (which
                    # mixes 1BRs with penthouses and mis-prices both ends).
                    "by_size": history.size_band_metrics(),
                    # Floor-tier multipliers (size-confound removed) — a low-floor
                    # unit is benchmarked below the all-floor band median, a high
                    # one above. Per-project FE curve when the project has enough
                    # cross-tier contrast (v3.5b), else the district FE curve.
                    # Applied in _check_psf_overpricing.
                    "floor_factors": project_floor_factors(history) or floor_factors,
                }
                if is_ec_file:
                    cache_entry["is_ec"] = True
                cache[project_key] = cache_entry

                # Log with new-launch bias info
                bias_tag = " [NEW LAUNCH BIAS]" if history.has_new_launch_bias else ""
                if history.annualized_appreciation:
                    resale_info = ""
                    if history.resale_annualized_appreciation is not None:
                        resale_info = f", resale-only: {history.resale_annualized_appreciation:+.1f}%/yr"
                    print(f"    -> {history.project_name}: {history.annualized_appreciation:+.1f}%/yr "
                          f"({history.transaction_count} txns, "
                          f"{history.new_sale_count} new sale){resale_info}{bias_tag}",
                          file=sys.stderr)
                else:
                    print(f"    -> {history.project_name}: insufficient data for CAGR", file=sys.stderr)
            print(f"    {len(histories)} project(s) in file", file=sys.stderr)
        except Exception as e:
            print(f"    -> Error: {e}", file=sys.stderr)

    save_ura_cache(cache)
    export_ura_cache_csv(cache)
    return cache


def export_ura_cache_csv(cache: dict, path: str = os.path.join("data", "ura_cache.csv")) -> str:
    """Export the URA cache as a flat CSV (the inspectable data backbone)."""
    import csv as _csv
    cols = ["project_name", "district", "market_segment", "tenure", "lease_start_year",
            "transaction_count", "median_psf", "avg_psf_5yr_ago",
            "appreciation_1yr", "appreciation_3yr", "appreciation_5yr",
            "annualized_appreciation", "resale_annualized_appreciation",
            "new_sale_proportion", "has_new_launch_bias", "appreciation_momentum",
            "data_coverage", "source", "updated"]
    rows = sorted(
        (v for v in cache.values() if isinstance(v, dict)),
        key=lambda v: (v.get("district") or "", -(v.get("transaction_count") or 0)),
    )
    with open(path, "w", newline="") as f:
        writer = _csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def _format_price_filter(min_price: int | None, max_price: int | None) -> str:
    """Human-readable price filter description for scrape logs."""
    if min_price is None and max_price is None:
        return "any"
    lo = f"${min_price:,}" if min_price is not None else "any"
    hi = f"${max_price:,}" if max_price is not None else "any"
    return f"{lo} - {hi}"


def scrape_listings(
    districts: list[int],
    min_price: int | None,
    max_price: int | None,
    beds: list[int],
    max_pages: int = 5,
    headless: bool = True,
    enrich_top: int = 0,
    allow_extend: bool = False,
) -> list[dict]:
    """Scrape listings from PropertyGuru with per-district scraping.

    `allow_extend` lets a scope run past `max_pages` to its real end — the
    deep-verification crawl wants that; a routine poll does not (see
    scrapers.propertyguru.scrape, where extending used to be the silent default
    and tripled the request budget).
    """
    from scrapers.browser import BrowserManager
    from scrapers.propertyguru import PropertyGuruScraper
    from models import SearchParams

    params = SearchParams(
        property_type="C",  # Condo
        listing_type="sale",
        min_price=min_price,
        max_price=max_price,
        beds=beds,
        districts=districts,
    )

    print(f"\n{'='*60}", file=sys.stderr)
    print(f"SCRAPING PROPERTYGURU (per-district mode)", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    print(f"  Districts: {districts} ({len(districts)} districts)", file=sys.stderr)
    print(f"  Price: {_format_price_filter(min_price, max_price)}", file=sys.stderr)
    print(f"  Beds: {beds}", file=sys.stderr)
    print(f"  Max pages per district: {max_pages}", file=sys.stderr)
    if enrich_top > 0:
        print(f"  Enrich top: {enrich_top} listings", file=sys.stderr)

    with BrowserManager(headless=headless) as context:
        scraper = PropertyGuruScraper(context)
        listings, stats = scraper.scrape_multi_district(
            params, max_pages=max_pages, allow_extend=allow_extend)

        # Enrich top listings if requested
        if enrich_top > 0 and listings:
            scraper.enrich_listings(listings, top_n=enrich_top)
            stats = scraper.stats  # refresh stats after enrichment

    # Convert Listing objects to dicts
    result = [l.to_dict() if hasattr(l, 'to_dict') else l for l in listings]

    # Print scraping stats
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"SCRAPING STATS", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    print(stats.summary(), file=sys.stderr)
    print(f"  Final result: {len(result)} listings", file=sys.stderr)

    return result


def scrape_condo_listings(
    condo_name: str,
    min_price: int | None,
    max_price: int | None,
    beds: list[int],
    max_pages: int = 5,
    headless: bool = True,
    enrich_top: int = 0,
    fuzzy: bool = True,
    max_results: int = 80,
) -> list[dict]:
    """Scrape listings by condo name (fuzzy search via freetext)."""
    from scrapers.browser import BrowserManager
    from scrapers.propertyguru import PropertyGuruScraper
    from models import SearchParams
    from difflib import SequenceMatcher

    def _norm(text: str) -> str:
        text = re.sub(r"[^a-z0-9 ]", " ", text.lower())
        return " ".join(text.split())

    def _score(title: str, project: str) -> float:
        target = _norm(condo_name)
        cand = _norm(project or title)
        if not target or not cand:
            return 0.0
        if target == cand:
            return 1.0
        return SequenceMatcher(None, target, cand).ratio()

    def _passes(listing) -> bool:
        """Check fuzzy match on a Listing object or dict."""
        if hasattr(listing, 'title'):
            title = listing.title or ""
            project = listing.project_name or ""
        else:
            title = listing.get("title", "")
            project = listing.get("project_name", "")
        score = _score(title, project)
        if not fuzzy:
            return score >= 1.0
        return score >= 0.65

    params = SearchParams(
        property_type="C",
        listing_type="sale",
        min_price=min_price,
        max_price=max_price,
        beds=beds,
        freetext=condo_name,
    )

    print(f"\n{'='*60}", file=sys.stderr)
    print("SCRAPING PROPERTYGURU (condo-name mode)", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    print(f"  Condo: {condo_name}", file=sys.stderr)
    print(f"  Price: {_format_price_filter(min_price, max_price)}", file=sys.stderr)
    print(f"  Beds: {beds}", file=sys.stderr)
    print(f"  Max pages: {max_pages}", file=sys.stderr)
    print(f"  Fuzzy matching: {'ON' if fuzzy else 'OFF'}", file=sys.stderr)
    if enrich_top > 0:
        print(f"  Enrich top: {enrich_top} listings", file=sys.stderr)

    with BrowserManager(headless=headless) as context:
        scraper = PropertyGuruScraper(context)
        listings = scraper.scrape(params, max_pages=max_pages)

        # Filter by condo name BEFORE enrichment to avoid wasting time
        # on detail pages for non-matching listings
        pre_filter_count = len(listings)
        listings = [l for l in listings if _passes(l)]
        skipped = pre_filter_count - len(listings)

        if enrich_top > 0 and listings:
            scraper.enrich_listings(listings, top_n=enrich_top)

        stats = scraper.stats

    result = [l.to_dict() if hasattr(l, 'to_dict') else l for l in listings]

    # Add match scores to the dicts and apply max_results
    for item in result:
        title = item.get("title", "")
        project = item.get("project_name", "")
        item["condo_match_score"] = round(_score(title, project), 3)

    result.sort(key=lambda x: x.get("condo_match_score", 0), reverse=True)
    if max_results and len(result) > max_results:
        result = result[:max_results]

    print(f"\n{'='*60}", file=sys.stderr)
    print("SCRAPING STATS", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    print(stats.summary(), file=sys.stderr)
    print(f"  Fuzzy-filtered out: {skipped} non-matching listings", file=sys.stderr)
    print(f"  Final matched: {len(result)} listings", file=sys.stderr)

    return result


def score_condo(inputs: dict, ura_data: dict | None = None) -> dict:
    """Score a condo from a dict of its key value-drivers and return the technical breakdown.

    This is the AI-callable entry point: feed the researched facts that drive a
    condo's investment value and get back a calibrated 100-point technical score
    plus a per-metric breakdown. The AI uses this as ONE input to its own
    qualitative Buy/Neutral/Avoid judgement — it is not the verdict itself.

    Recognised inputs (all optional except price):
        price (required), sqft, psf, beds, baths, district (e.g. "D15"),
        tenure ("Freehold"/"99-year leasehold"), built_year, total_units,
        project_name, latitude, longitude, mrt_info, floor_level, facing.
    AI overrides (use your own researched numbers instead of the cache):
        appreciation_rate_pct  — annual appreciation %, injected as project data
        monthly_rent           — overrides the district-average rental estimate

    Returns a JSON-serialisable dict: technical_score, tier, weighted breakdown,
    the factual figures used, and notes about data sources.
    """
    from scoring.full_scorer import FullScorer
    from config import (
        SCORE_WEIGHT_RENTAL_YIELD, SCORE_WEIGHT_CAPITAL_APPRECIATION,
        SCORE_WEIGHT_FUTURE_POTENTIAL, SCORE_WEIGHT_LIQUIDITY,
        SCORE_WEIGHT_COST_EFFICIENCY, SCORE_WEIGHT_RED_FLAGS,
    )

    ura_data = dict(ura_data) if ura_data else {}
    notes: list[str] = []

    listing = {k: v for k, v in inputs.items()
               if k not in ("appreciation_rate_pct", "monthly_rent", "gross_yield_pct")}
    listing.setdefault("title", inputs.get("project_name") or "Manual scoring")

    # Inject an AI-provided appreciation rate as if it were project data.
    project_name = (listing.get("project_name") or listing.get("title") or "").strip()
    apr_override = inputs.get("appreciation_rate_pct")
    if apr_override is not None and project_name:
        ura_data[project_name.lower()] = {
            "project_name": project_name,
            "annualized_appreciation": float(apr_override),
            # v3.4: source 'agent_override' makes the appreciation component trust the
            # ASSERTED rate (confidence 1.0) deliberately — that is the tool's purpose.
            # We no longer inject a fake transaction_count (was 999), which silently
            # also saturated the LIQUIDITY (txn_volume) and PSF-premium confidence as
            # if 999 real sales backed the unit. Liquidity now reflects the real (none)
            # transaction data for a manual score, instead of a spurious +10.
            "source": "agent_override",
        }
        notes.append(
            f"Appreciation: using AI-provided {float(apr_override):.1f}%/yr at face value "
            "(confidence 1.0) — it validates internal consistency, NOT the rate itself. "
            "Liquidity reflects the actual transaction data (none, for a manual score). "
            "Verify the rate against URA/resale transaction data."
        )

    # Inject an AI-provided rent as same-condo rental data so the yield score
    # and ROI actually use it (not just the displayed figures).
    condo_rental_data = None
    monthly_rent = inputs.get("monthly_rent")
    if monthly_rent is not None:
        sqft = listing.get("sqft")
        if sqft:
            if not listing.get("project_name"):
                listing["project_name"] = listing["title"]
            condo_rental_data = {
                listing["project_name"].lower().strip(): float(monthly_rent) / float(sqft)
            }
            notes.append(f"Rental: using AI-provided ${float(monthly_rent):,.0f}/mo")
        else:
            notes.append("Rental: monthly_rent ignored — provide sqft for it to apply")

    scorer = FullScorer(condo_rental_data=condo_rental_data, ura_data=ura_data)
    scored = scorer.score(listing)

    if scored.appreciation_source == "regional_baseline":
        notes.append("Appreciation: no project data — regional baseline used (research this).")

    return {
        "technical_score": round(scored.total_score, 1),
        "mmr": round(scored.mmr, 1) if scored.mmr is not None else None,
        "score_1000": scored.score_1000,
        "mmr_components": scored.mmr_components or None,
        "tier": scored.final_tier,
        "tier_label": scored.final_tier_label,
        "score_is_technical_only": True,
        "note": "Technical score only — combine with your qualitative research for the Buy/Neutral/Avoid call.",
        "breakdown": {
            "rental_yield": {"score": round(scored.rental_yield_score, 1), "max": SCORE_WEIGHT_RENTAL_YIELD},
            "capital_appreciation": {"score": round(scored.capital_appreciation_score, 1), "max": SCORE_WEIGHT_CAPITAL_APPRECIATION},
            "future_potential": {"score": round(scored.future_potential_score, 1), "max": SCORE_WEIGHT_FUTURE_POTENTIAL},
            "liquidity": {"score": round(scored.liquidity_score, 1), "max": SCORE_WEIGHT_LIQUIDITY},
            "cost_efficiency": {"score": round(scored.cost_efficiency_score, 1), "max": SCORE_WEIGHT_COST_EFFICIENCY},
            "red_flag_deductions": {"score": -round(scored.red_flag_deductions, 1), "max": SCORE_WEIGHT_RED_FLAGS},
        },
        "factual": {
            "appreciation_rate_pct": round(scored.appreciation_rate * 100, 2),
            "appreciation_source": scored.appreciation_source,
            "relative_value": scored.score_breakdown.get("relative_value"),
            "estimated_monthly_rent": round(scored.estimated_monthly_rent) if scored.estimated_monthly_rent else None,
            "gross_yield_pct": scored.estimated_gross_yield or None,
            "nearest_mrt": scored.nearest_mrt,
            "mrt_distance_m": scored.mrt_distance_m,
            "remaining_lease": scored.remaining_lease,
            "red_flags": scored.score_breakdown.get("red_flags", {}).get("flags", []),
            "roi_5yr_annualized_pct": round(scored.roi_5yr.annualized_roi, 2) if scored.roi_5yr else None,
            "roi_7yr_annualized_pct": round(scored.roi_7yr.annualized_roi, 2) if scored.roi_7yr else None,
        },
        "notes": notes,
    }


def scrape_url_listings(
    url: str,
    max_pages: int = 5,
    headless: bool = True,
    enrich_top: int = 0,
    min_price: int | None = None,
    max_price: int | None = None,
    beds: list[int] | None = None,
) -> list[dict]:
    """Scrape listings from an arbitrary PropertyGuru URL.

    Handles a single listing detail page or a search/results page directly.
    A project/condo-directory page falls back to the condo-name search flow.
    """
    from scrapers.browser import BrowserManager
    from scrapers.propertyguru import PropertyGuruScraper, ProjectPageNeedsNameFallback

    print(f"\n{'='*60}", file=sys.stderr)
    print("SCRAPING PROPERTYGURU (url mode)", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    print(f"  URL: {url}", file=sys.stderr)

    with BrowserManager(headless=headless) as context:
        scraper = PropertyGuruScraper(context)
        try:
            listings = scraper.scrape_from_url(url, max_pages=max_pages)
        except ProjectPageNeedsNameFallback as e:
            print(f"  Project/condo page detected -> condo-name search: '{e.project_name}'",
                  file=sys.stderr)
            return scrape_condo_listings(
                condo_name=e.project_name or "",
                min_price=min_price,
                max_price=max_price,
                beds=beds or [],
                max_pages=max_pages,
                headless=headless,
                enrich_top=enrich_top or 20,
            )

        if enrich_top > 0 and listings:
            scraper.enrich_listings(listings, top_n=enrich_top)
        stats = scraper.stats

    result = [l.to_dict() if hasattr(l, 'to_dict') else l for l in listings]

    print(f"\n{'='*60}", file=sys.stderr)
    print("SCRAPING STATS", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    print(stats.summary(), file=sys.stderr)
    print(f"  Final result: {len(result)} listings", file=sys.stderr)

    return result


def scored_json_payload(scored: list[ScoredListing]) -> dict:
    """The `scored.json` / `--json` document for a scoring run."""
    return {
        "analyzed_at": datetime.now().isoformat(),
        "total": len(scored),
        "ura_data_used": sum(1 for s in scored if "ura" in s.appreciation_source.lower()),
        "listings": [s.to_dict() for s in scored],
    }


def deduplicate_by_project(scored: list[ScoredListing]) -> list[ScoredListing]:
    """Group listings from the same condo into one entry.

    Keeps the highest-scoring listing as the primary and appends
    other listing URLs to its additional_urls field.
    Listings are assumed to be pre-sorted by `rank_score` descending (raw MMR
    when available, else the legacy /100 total).
    """
    seen: dict[str, ScoredListing] = {}  # project_key -> primary listing
    result = []

    for listing in scored:
        key = (listing.project_name or listing.title or "").strip().lower()
        if not key:
            result.append(listing)
            continue

        if key not in seen:
            seen[key] = listing
            result.append(listing)
        else:
            primary = seen[key]
            if listing.agent_rating and not primary.agent_rating:
                # Never let an unrated sibling hide a rated one: the agent-rated
                # listing becomes the primary; the old primary's URLs are kept.
                merged = [u for u in [primary.url, *primary.additional_urls]
                          if u and u != listing.url and u not in listing.additional_urls]
                listing.additional_urls = listing.additional_urls + merged
                result[result.index(primary)] = listing
                seen[key] = listing
            elif listing.url and listing.url not in primary.additional_urls:
                # Append this listing's URL to the primary entry
                primary.additional_urls.append(listing.url)

    return result


def group_condo_by_unit_type(scored: list[ScoredListing]) -> list[ScoredListing]:
    """Group condo listings by (project_name, beds) for per-unit-type analysis.

    Instead of collapsing all units into one entry, this keeps one
    representative per bedroom type with full unit variant data.
    The median-priced listing is chosen as representative (within the
    same condo, scores are similar — price is the differentiator).
    """
    from collections import defaultdict

    groups: dict[tuple[str, int], list[ScoredListing]] = defaultdict(list)

    for listing in scored:
        key_name = (listing.project_name or listing.title or "").strip().lower()
        key_beds = listing.beds or 0
        groups[(key_name, key_beds)].append(listing)

    result = []
    for (proj_key, beds), members in groups.items():
        if not proj_key:
            result.extend(members)
            continue

        # Sort by price to pick median (lower median for even-sized groups)
        members.sort(key=lambda s: s.price)
        median_idx = (len(members) - 1) // 2
        representative = members[median_idx]

        # Build unit_variants list
        variants = []
        for m in members:
            variant = {
                "price": m.price,
                "psf": m.psf,
                "sqft": m.sqft,
                "floor_level": m.floor_level,
                "facing": m.facing,
                "url": m.url,
            }
            variants.append(variant)

        representative.unit_variants = variants

        # Build unit_summary
        prices = [m.price for m in members]
        psfs = [m.psf for m in members if m.psf]
        floors = [m.floor_level for m in members if m.floor_level]
        representative.unit_summary = {
            "count": len(members),
            "price_range": [min(prices), max(prices)] if prices else [],
            "psf_range": [min(psfs), max(psfs)] if psfs else [],
            "floors": sorted(set(floors)) if floors else [],
        }

        # Collect additional URLs (all URLs except the representative's)
        representative.additional_urls = [
            m.url for m in members if m.url and m.url != representative.url
        ]

        result.append(representative)

    # Sort by (beds, -rank_score)
    result.sort(key=lambda s: (s.beds or 0, -s.rank_score))
    return result


def print_results(scored: list[ScoredListing], top_n: int = 10, verbose: bool = False):
    """Print analysis results to console (v2.2 format)."""
    print(f"\n{'='*70}")
    print("INVESTMENT ANALYSIS RESULTS (v2.2)")
    print(f"{'='*70}")

    # Calculate stats (prefer the /1000 MMR scale when available)
    use_1000 = any(s.score_1000 is not None for s in scored)
    if use_1000:
        scores = [s.score_1000 for s in scored if s.score_1000 is not None]
    else:
        scores = [s.total_score for s in scored]
    avg_score = sum(scores) / len(scores) if scores else 0
    min_score = min(scores) if scores else 0
    max_score = max(scores) if scores else 0

    # Count URA data usage
    ura_count = sum(1 for s in scored if s.has_ura_data)

    # Count tiers with new thresholds
    tier1 = [s for s in scored if s.final_tier == 1]
    tier2 = [s for s in scored if s.final_tier == 2]
    tier3 = [s for s in scored if s.final_tier == 3]

    print(f"  Total Analyzed: {len(scored)}")
    scale_label = "/1000" if use_1000 else "/100"
    print(f"  Score Range ({scale_label}): {min_score:.0f} - {max_score:.0f} (avg: {avg_score:.0f})")
    if use_1000:
        from config import SCORE1000_TIER1_MIN, SCORE1000_TIER2_MIN
        print(
            f"  Tier 1 (>= {SCORE1000_TIER1_MIN}): {len(tier1)} | "
            f"Tier 2 ({SCORE1000_TIER2_MIN}-{SCORE1000_TIER1_MIN - 1}): {len(tier2)} | "
            f"Tier 3 (< {SCORE1000_TIER2_MIN}): {len(tier3)}"
        )
    else:
        print(
            f"  Tier 1 (>= {SCORE_TIER1_MIN}): {len(tier1)} | "
            f"Tier 2 ({SCORE_TIER2_MIN}-{SCORE_TIER1_MIN - 1}): {len(tier2)} | "
            f"Tier 3 (< {SCORE_TIER2_MIN}): {len(tier3)}"
        )
    print(f"  Real Transaction Data: {ura_count}/{len(scored)} properties")
    print()

    # Top listings
    show_n = min(top_n, len(scored))
    print(f"TOP {show_n} PROPERTIES:")
    print("-" * 70)

    for i, s in enumerate(scored[:show_n], 1):
        apr_pct = s.appreciation_rate * 100
        ura_tag = "[URA]" if s.has_ura_data else "     "

        # Truncate title
        title = s.title[:28] + ".." if len(s.title) > 30 else s.title

        # Future potential indicator
        future_str = ""
        if s.future_score_details:
            if s.future_score_details.nearest_future_mrt:
                future_str = f"MRT:{s.future_score_details.nearest_future_mrt[:8]}"
            elif s.future_score_details.govt_zones:
                future_str = s.future_score_details.govt_zones[0][:12]

        extra = f" (+{len(s.additional_urls)} more)" if s.additional_urls else ""
        score_str = f"{s.score_1000:4d}" if s.score_1000 is not None else f"{s.total_score:4.1f}"
        print(f"#{i:2d} [{score_str}]{ura_tag} {title:<30} ${s.price/1e6:.2f}M  "
              f"{apr_pct:+.0f}%/yr  {future_str}{extra}")

    if verbose:
        print(f"\n\n{'='*70}")
        print("DETAILED ANALYSIS (v2.2 Scoring)")
        print(f"{'='*70}")

        for i, s in enumerate(scored[:min(5, top_n)], 1):
            print(f"\n--- #{i} {s.title} ---")
            print(f"  Price: ${s.price:,} | PSF: ${s.psf:,.0f}" if s.psf else f"  Price: ${s.price:,}")
            district_label = normalize_district(s.district or "")
            print(f"  District: {district_label} | Beds: {s.beds} | Sqft: {s.sqft:,.0f}" if s.sqft else "")
            print(f"  Tenure: {s.tenure} | Built: {s.built_year}")
            if s.nearest_mrt:
                print(f"  MRT: {s.nearest_mrt} ({s.mrt_distance_m}m)")
            print()

            # Score breakdown
            if s.score_1000 is not None:
                print(f"  SCORE: {s.score_1000}/1000 (MMR {s.mmr:.0f}) | legacy {s.total_score:.1f}/100")
            else:
                print(f"  TOTAL SCORE: {s.total_score:.1f}/100")
            print(f"    Rental Yield:      {s.rental_yield_score:5.1f}/15")
            print(f"    Appreciation:      {s.capital_appreciation_score:5.1f}/30")
            print(f"    Future Potential:  {s.future_potential_score:5.1f}/20")
            print(f"    Liquidity:         {s.liquidity_score:5.1f}/25")
            print(f"    Cost Efficiency:   {s.cost_efficiency_score:5.1f}/10")
            if s.red_flag_deductions > 0:
                print(f"    Red Flags:        -{s.red_flag_deductions:5.1f}")

            # Future details
            if s.future_score_details:
                fd = s.future_score_details
                print(f"\n  Future Catalysts:")
                if fd.nearest_future_mrt:
                    print(f"    MRT: {fd.nearest_future_mrt} ({fd.future_mrt_distance_m}m) - {fd.future_mrt_line}")
                if fd.govt_zones:
                    print(f"    Govt Zones: {', '.join(fd.govt_zones)}")

            print()
            print(f"  Rental: ${s.estimated_monthly_rent:,.0f}/mo = {s.estimated_gross_yield:.2f}% yield")

            apr_pct = s.appreciation_rate * 100
            print(f"  Appreciation: {apr_pct:+.1f}%/yr ({s.appreciation_source})")

            if s.roi_5yr:
                print(f"\n  ROI Projections:")
                print(f"    5yr: Exit ${s.roi_5yr.estimated_exit_price:,} -> {s.roi_5yr.annualized_roi:.2f}%/yr")
                if s.roi_7yr:
                    print(f"    7yr: Exit ${s.roi_7yr.estimated_exit_price:,} -> {s.roi_7yr.annualized_roi:.2f}%/yr")

            print(f"\n  URL: {s.url}")
            if s.additional_urls:
                for extra_url in s.additional_urls:
                    print(f"  URL: {extra_url}")


def discover_districts(top_n: int = 5, exclude_ccr: bool = True, region: str = None) -> list[int]:
    """Use DistrictScorer to find top investment districts.

    Args:
        top_n: Number of districts to return
        exclude_ccr: Exclude CCR (high entry cost) by default
        region: Optional filter for specific region

    Returns:
        List of district numbers
    """
    scorer = DistrictScorer()
    return scorer.get_top_districts(top_n=top_n, exclude_ccr=exclude_ccr, region_filter=region)


def print_district_discovery():
    """Print district discovery/ranking results."""
    scorer = DistrictScorer()
    scores = scorer.score_all_districts()

    print("\n" + "=" * 80)
    print("AI-RECOMMENDED DISTRICTS FOR INVESTMENT (v2.2)")
    print("=" * 80)
    print("\nScoring: 30% Historical + 20% Liquidity + 25% Future Infra + 20% Govt Priority + 5% Supply")
    print()

    # Print by region
    for region in ["RCR", "OCR", "CCR"]:
        region_scores = [s for s in scores if s.region == region][:5]
        if not region_scores:
            continue

        region_label = {
            "RCR": "REST OF CENTRAL REGION (Best Value)",
            "OCR": "OUTSIDE CENTRAL REGION (Growth Potential)",
            "CCR": "CORE CENTRAL REGION (Premium)"
        }[region]

        print(f"\n{region_label}:")
        print("-" * 80)
        print(f"{'Rank':<5}{'District':<35}{'Score':<8}{'Key Catalysts'}")
        print("-" * 80)

        for i, s in enumerate(region_scores, 1):
            district_str = f"D{s.district:02d} - {s.name}"[:33]
            catalysts = ", ".join(s.key_catalysts[:3]) if s.key_catalysts else "-"
            print(f"{i:<5}{district_str:<35}{s.total_score:<8.1f}{catalysts}")

    print("\n" + "=" * 80)
    print("Tip: Use --auto to automatically select top districts and scrape listings")
    print("=" * 80)


def main():
    parser = argparse.ArgumentParser(
        description="Property investment analyzer with URA data integration (v2.2)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
SCORING SYSTEM v2.2 (no URA bias):
  Rental Yield: 15 pts | Capital Appreciation: 30 pts | Future Potential: 20 pts
  Liquidity: 25 pts | Cost Efficiency: 10 pts | Red Flags: -10 pts

Examples:
  # AUTOMATIC MODE (recommended) - discovers best districts automatically
  python invest.py --auto --beds 2,3 --top 10

  # Discovery only - see AI district rankings
  python invest.py --discover-districts

  # Manual district selection
  python invest.py --districts 3,5,14,15 --beds 2,3

  # Analyze OCR districts
  python invest.py --districts 16,18,19 --min-price 1500000 --max-price 2000000

  # Fetch URA data by district (fastest way to get all transaction data)
  python invest.py --fetch-ura-districts 3,5,14,15
  python invest.py --fetch-ura-districts all

  # Build URA cache from downloaded CSVs
  python invest.py --build-ura-cache data/ura_*.csv

  # Score existing listings with URA data
  python invest.py --input output/listings_*.json --top 20
        """
    )

    # NEW: Auto mode
    parser.add_argument("--auto", action="store_true",
                       help="Automatic mode: discover top districts, scrape, score (recommended)")
    parser.add_argument("--discover-districts", action="store_true",
                       help="Show AI district rankings without scraping")
    parser.add_argument("--auto-districts", type=int, default=5,
                       help="Number of districts for auto mode (default: 5)")
    parser.add_argument("--include-ccr", action="store_true",
                       help="Include CCR districts in auto mode (default: exclude)")

    # Scraping options
    parser.add_argument("--districts", "-d", type=str,
                       help="Comma-separated district numbers (e.g., 3,5,14,15)")
    parser.add_argument("--condo", type=str,
                       help="Condo/project name to search for (fuzzy matching by default)")
    parser.add_argument("--condo-max-pages", type=int, default=5,
                       help="Max pages to scrape for condo search (default: 5)")
    parser.add_argument("--condo-max-results", type=int, default=80,
                       help="Max listings to keep for condo search (default: 80)")
    parser.add_argument("--condo-fuzzy", dest="condo_fuzzy", action="store_true", default=True,
                       help="Enable fuzzy condo-name matching (default: enabled)")
    parser.add_argument("--condo-exact", dest="condo_fuzzy", action="store_false",
                       help="Exact match only (disables fuzzy matching)")
    parser.add_argument("--score", type=str, metavar="JSON",
                       help="Score a condo from a JSON dict of key value-drivers (AI-callable technical scorer). "
                            "Accepts inline JSON or @file.json. Prints the technical score breakdown as JSON.")
    parser.add_argument("--fight", action="store_true",
                       help="Condo arena: pairwise round-robin value tournament over the listings DB "
                            "(respects --districts/--beds/--min-price/--max-price filters). "
                            "Outputs Elo ranking, Pareto frontier, and champion.")
    parser.add_argument("--score-db", action="store_true",
                       help="Batch-score every usable listing in the DB (MMR + score_1000), "
                            "write scores back into the DB, and re-export the sheet.")
    parser.add_argument("--from-db", type=str, metavar="IDS",
                       help="Gather WITHOUT scraping: build a run dir + raw_analysis.json from "
                            "listings already in the DB (comma-separated listing ids). Used by "
                            "the daily scan, and any time PropertyGuru is unreachable.")
    parser.add_argument("--url", type=str,
                       help="PropertyGuru URL: a single listing, a results/list page, or a project page")
    parser.add_argument("--url-max-pages", type=int, default=5,
                       help="Max pages to scrape when --url is a results page (default: 5)")
    parser.add_argument("--min-price", type=int, default=None,
                       help="Minimum price filter for scraping (optional, no default)")
    parser.add_argument("--max-price", type=int, default=None,
                       help="Maximum price filter for scraping (optional, no default)")
    parser.add_argument("--beds", "-b", type=str, default=None,
                       help="Bedroom counts (default: 2,3 for scraping; ALL for --fight)")
    parser.add_argument("--max-pages", type=int, default=5,
                       help="Max pages to scrape per district (default: 5)")
    parser.add_argument("--enrich-top", type=int, default=0,
                       help="Enrich top N listings by visiting detail pages (default: 0 = disabled)")

    # Input/output options
    parser.add_argument("--input", "-i", type=str,
                       help="Use existing listings JSON file (supports glob)")
    parser.add_argument("--output", "-o", type=str,
                       help="Output path for markdown report (without extension)")
    parser.add_argument("--csv", type=str,
                       help="Output path for CSV export")
    parser.add_argument("--json", type=str,
                       help="Output path for scored JSON")
    parser.add_argument("--raw", type=str, metavar="PATH",
                       help="Output raw analysis JSON for AI agent review")
    parser.add_argument("--from-review", type=str, metavar="PATH",
                       help="Generate final report from AI-reviewed analysis JSON")

    # URA data options
    parser.add_argument("--build-ura-cache", nargs="+", metavar="CSV",
                       help="Build URA cache from CSV files (supports glob patterns)")
    parser.add_argument("--fetch-ura-districts", type=str, metavar="DISTRICTS",
                       help="Fetch URA data by postal district (e.g., '3,5,14,15' or 'all')")

    # Evaluation memory (git-tracked past evaluations — may be stale, always re-verify)
    parser.add_argument("--recall", type=str, metavar="NAME",
                       help="Recall past evaluations for a condo by name (fuzzy)")
    parser.add_argument("--list-evals", action="store_true",
                       help="List all stored evaluations (condo, rating, date)")
    parser.add_argument("--eval-stats", action="store_true",
                       help="Rating/confidence distribution audit vs calibration bands")
    parser.add_argument("--no-save-eval", action="store_true",
                       help="Do not auto-save evaluations during --from-review")

    # Condo profiles (git-tracked physical facts: stacks / facings / layouts)
    parser.add_argument("--profile", type=str, metavar="NAME",
                       help="Show the researched stack/layout profile for a condo (fuzzy)")
    parser.add_argument("--list-profiles", action="store_true",
                       help="List all researched condo profiles")
    parser.add_argument("--save-profile", type=str, metavar="PROFILE_JSON",
                       help="Save a condo profile from a JSON file into profiles/")

    # Listings sheet/database (continuously-updated, AI-searchable PropertyGuru inventory)
    parser.add_argument("--search-db", type=str, metavar="QUERY",
                       help="Search the master listings sheet by condo name")
    parser.add_argument("--list-db", action="store_true",
                       help="Show listings sheet stats (counts, price drops, by district)")
    parser.add_argument("--export-sheet", action="store_true",
                       help="Re-export the listings sheet CSV from the database")
    parser.add_argument("--update-db", action="store_true",
                       help="Scrape mode that only refreshes the listings sheet (skips scoring/report)")
    parser.add_argument("--no-db", action="store_true",
                       help="Do not upsert scraped listings into the master sheet")

    # Display options
    parser.add_argument("--top", "-n", type=int, default=10,
                       help="Number of top properties to show (default: 10)")
    parser.add_argument("--verbose", "-v", action="store_true",
                       help="Show detailed score breakdowns")
    parser.add_argument("--no-headless", action="store_true",
                       help="Show browser window (default: headless)")
    parser.add_argument("--list-districts", action="store_true",
                       help="List all districts and exit")

    args = parser.parse_args()
    headless = not args.no_headless

    # Handle --discover-districts (NEW)
    if args.discover_districts:
        print_district_discovery()
        return

    # Handle --list-districts
    if args.list_districts:
        print("\nSingapore Districts for Property Investment:\n")
        print("RCR Prime (★ = best for investment):")
        for d in [3, 5, 14, 15]:
            print(f"  D{d:02d}: {DISTRICTS[d]}")
        print("\nRCR Other:")
        for d in [4, 8, 12, 13]:
            print(f"  D{d:02d}: {DISTRICTS[d]}")
        print("\nOCR (Higher yield, more affordable):")
        for d in [16, 18, 19, 20]:
            print(f"  D{d:02d}: {DISTRICTS[d]}")
        print("\nCCR (Premium, lower yield):")
        for d in [1, 2, 6, 7, 9, 10, 11]:
            print(f"  D{d:02d}: {DISTRICTS[d]}")
        print("\nTip: Use --discover-districts to see AI-ranked districts")
        return

    # --- AI-callable technical scorer ---
    if args.score:
        warn_if_stale_data()  # this path consumes the cached datasets too
        raw = args.score
        if raw.startswith("@"):
            with open(raw[1:]) as f:
                raw = f.read()
        try:
            score_inputs = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"Error: --score expects valid JSON (or @file.json): {e}", file=sys.stderr)
            sys.exit(1)
        if not isinstance(score_inputs, dict) or "price" not in score_inputs:
            print("Error: --score JSON must be an object containing at least 'price'.", file=sys.stderr)
            sys.exit(1)
        result = score_condo(score_inputs, ura_data=load_ura_cache())
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    # --- Batch-score the listings DB (MMR backlog) ---
    if args.score_db:
        warn_if_stale_data()
        import listings_db
        from scoring.full_scorer import FullScorer, build_cohort_stats

        db = listings_db.load_db()
        records = list(db["listings"].values())
        usable = [r for r in records if r.get("price") and r.get("sqft") and r.get("psf")]
        print(f"\nScoring {len(usable)}/{len(records)} usable listings for the MMR backlog...", file=sys.stderr)

        from scoring import lenses

        ura = load_ura_cache()
        scorer = FullScorer(ura_data=ura, cohort_stats=build_cohort_stats(usable))
        today = datetime.now().strftime("%Y-%m-%d")
        scored_vals = []
        errors = 0
        # Lens entries merge key-by-key AFTER the flat fields (an un-run lens's
        # stored score must survive), keyed by object identity because DB
        # records have no other stable handle inside this loop.
        lens_entries: dict[int, dict] = {}
        for r in usable:
            try:
                s = scorer.score(r)
            except Exception:
                errors += 1
                continue
            r["mmr"] = s.mmr
            r["score_1000"] = s.score_1000
            r["scored_at"] = today
            r["score_version"] = config.score_version()
            r.update(s.three_score_fields())  # valuation / livability / overalls
            lens_entries[id(r)] = lenses.score_record(s, r, today=today)
            if s.score_1000 is not None:
                scored_vals.append(s.score_1000)

        # rank_norm needs the whole freshly-scored book, so it is stamped in a
        # second pass: merge every lens score first, then read the books off
        # the merged records, then rank only the entries THIS run produced
        # (older vintages keep the rank they earned against their own book).
        for r in usable:
            lenses.merge_lens_scores(r, lens_entries.get(id(r)))
        lens_books = lenses.book_scores(usable)
        for r in usable:
            for name, e in (lens_entries.get(id(r)) or {}).items():
                e["rank_norm"] = lenses.rank_norm(e["score"], lens_books.get(name) or [])

        listings_db.save_db(db)
        path = listings_db.export_sheet(db=db)

        # Append to the MMR history CSV — scores over time accumulate so the
        # sheet always shows latest while history preserves every run. The
        # poller appends to the same file from another process — share its lock.
        import csv as _csv
        hist_path = os.path.join("data", "mmr_history.csv")
        with listings_db.file_lock(hist_path + ".lock"):
            write_header = not os.path.exists(hist_path)
            with open(hist_path, "a", newline="") as f:
                writer = _csv.writer(f)
                if write_header:
                    writer.writerow(["scored_at", "id", "project_name", "district", "beds",
                                     "price", "psf", "mmr", "score_1000", "score_version"])
                for r in usable:
                    if r.get("mmr") is None:
                        continue
                    writer.writerow([today, r.get("id"), r.get("project_name"), r.get("district"),
                                     r.get("beds"), r.get("price"), r.get("psf"),
                                     r.get("mmr"), r.get("score_1000"), config.score_version()])

        # Per-lens history — its OWN file with a `lens` column. Never rows in
        # mmr_history.csv above: calibrate_forward.py cohorts off that file and
        # mixed-lens rows would pollute the registered cohorts.
        lenses.append_history([
            {"scored_at": today, "lens": name, "id": r.get("id"),
             "project_name": r.get("project_name"), "district": r.get("district"),
             "beds": r.get("beds"), "price": r.get("price"), "psf": r.get("psf"),
             "score": e["score"], "rank_norm": e.get("rank_norm"),
             "lens_version": e["version"]}
            for r in usable
            for name, e in (lens_entries.get(id(r)) or {}).items()])

        if scored_vals:
            scored_vals.sort()
            import statistics as _st
            print(f"  Scored: {len(scored_vals)} (errors: {errors})")
            print(f"  score_1000: mean={_st.mean(scored_vals):.0f} sd={_st.stdev(scored_vals):.0f} "
                  f"min={scored_vals[0]} p50={scored_vals[len(scored_vals)//2]} max={scored_vals[-1]}")
        print(f"  Sheet updated: {path}")
        print(f"  MMR history appended: {hist_path}")
        return

    # --- Gather from the listings DB (no network) — daily-scan / offline flows ---
    if args.from_db:
        import listings_db
        from scoring.full_scorer import FullScorer, build_cohort_stats

        wanted = [k.strip() for k in args.from_db.split(",") if k.strip()]
        db = listings_db.load_db()
        store = db["listings"]
        # Accept either a DB key or a bare listing id (they coincide today, but
        # don't bet the flow on it).
        by_id = {str(r.get("id")): k for k, r in store.items() if r.get("id")}
        records, missing = [], []
        for w in wanted:
            key = w if w in store else by_id.get(w)
            if key is None:
                missing.append(w)
                continue
            records.append(store[key])
        if missing:
            print(f"  ⚠ not in DB (skipped): {', '.join(missing)}", file=sys.stderr)
        usable = [r for r in records if r.get("price") and r.get("sqft") and r.get("psf")]
        if not usable:
            print("Error: --from-db matched no scorable listing "
                  "(need price + sqft + psf).", file=sys.stderr)
            sys.exit(1)

        # Cohort stats come from the FULL usable DB — same as a --score-db /
        # poll run — so the raw analysis carries the same MMR the DB (and the
        # daily gate) already recorded, not a one-listing cohort's number.
        ura = load_ura_cache()
        all_usable = [r for r in store.values()
                      if r.get("price") and r.get("sqft") and r.get("psf")
                      and r.get("status") != "stale"]
        scorer = FullScorer(ura_data=ura, cohort_stats=build_cohort_stats(all_usable))
        scored = []
        for r in usable:
            try:
                scored.append(scorer.score(r))
            except Exception as e:  # noqa: BLE001 — one bad record must not kill the batch
                print(f"  ⚠ scoring failed for {r.get('id')}: {type(e).__name__}: {e}",
                      file=sys.stderr)
        if not scored:
            print("Error: --from-db scored nothing.", file=sys.stderr)
            sys.exit(1)

        slug = url_run_slug(usable[0].get("url") or "") if len(usable) == 1 else None
        run_dir = next_run_dir(slug=slug)
        raw_path = save_raw_analysis(
            scored, str(run_dir / "raw_analysis.json"), ura,
            {"from_db": wanted, "beds": sorted({r["beds"] for r in usable if r.get("beds")})},
        )
        print(f"Run directory: {run_dir}", file=sys.stderr)
        print(f"Raw analysis saved: {raw_path}")
        print(f"  -> {len(scored)} listing(s) scored from the DB (no scrape)")
        print(f"  -> Then: python invest.py --from-review {raw_path} "
              f"--output {run_dir / 'final'}")
        return

    # --- Condo arena: pairwise value tournament over the listings DB ---
    if args.fight:
        warn_if_stale_data()
        from scoring.full_scorer import FullScorer, build_cohort_stats
        from scoring.arena import run_arena

        with open(os.path.join("data", "listings_db.json")) as f:
            db = json.load(f)
        raw_listings = db.get("listings", {})
        if isinstance(raw_listings, dict):
            raw_listings = list(raw_listings.values())

        # Filters (reuse standard args)
        want_districts = None
        if args.districts:
            want_districts = {f"D{int(d):02d}" for d in args.districts.split(",")}
        want_beds = {int(b) for b in args.beds.split(",")} if args.beds else None

        usable = []
        for l in raw_listings:
            if not (l.get("price") and l.get("sqft") and l.get("psf")):
                continue
            if want_districts and (l.get("district") or "").upper() not in want_districts:
                continue
            if want_beds and l.get("beds") not in want_beds:
                continue
            if args.min_price and l["price"] < args.min_price:
                continue
            if args.max_price and l["price"] > args.max_price:
                continue
            usable.append(l)

        if len(usable) < 2:
            print(f"Arena needs >=2 usable listings after filters (got {len(usable)})", file=sys.stderr)
            return

        print(f"\nArena: scoring {len(usable)} listings...", file=sys.stderr)
        ura = load_ura_cache()
        scorer = FullScorer(ura_data=ura, cohort_stats=build_cohort_stats(usable))
        scored_all = [scorer.score(l) for l in usable]

        from scoring.arena import (
            run_brackets,
            format_brackets_report,
            build_brackets_referee_packet,
        )

        # Weight-class brackets (2BR vs 2BR, 3BR vs 3BR…) — like-for-like
        # fights — plus the open division (all types) as the secondary view.
        brackets = run_brackets(scored_all)
        open_fighters = run_arena(scored_all)

        # Agent-flow join: attach the AI's prior evaluations (git-tracked
        # eval memory) so technical ranks are read alongside past judgment.
        # Bed-aware: a condo's 2BR and 3BR can carry different ratings; only
        # fall back to another unit type's rating with an explicit annotation.
        import eval_memory
        eval_index = eval_memory.load_index().get("condos", {})
        eval_lookup: dict = {}  # (name, beds) and (name, None) -> (rating, date, beds)
        for slug in eval_index:
            data = eval_memory.load_condo(slug)
            if not data:
                continue
            name_key = (data.get("condo") or "").strip().lower()
            for h in data.get("history", []):
                rating, date = h.get("rating"), h.get("evaluated_at")
                if not rating:
                    continue
                h_beds = (h.get("as_of") or {}).get("beds")
                for key in [(name_key, h_beds), (name_key, None)]:
                    cur = eval_lookup.get(key)
                    if cur is None or (date or "") >= (cur[1] or ""):
                        eval_lookup[key] = (rating, date, h_beds)
        for fighter_set in list(brackets.values()) + [open_fighters]:
            for fl in fighter_set:
                name_key = fl.name.strip().lower()
                hit = eval_lookup.get((name_key, fl.beds))
                if hit:
                    fl.agent_rating, fl.agent_eval_date = hit[0], hit[1]
                else:
                    fallback = eval_lookup.get((name_key, None))
                    if fallback:
                        # A DIFFERENT unit type was evaluated, not this contender.
                        # Mark it clearly as referential so a 2BR's "Strong Buy"
                        # is never read as the verdict on this (e.g. 1BR) unit.
                        beds_note = f"{fallback[2]}BR" if fallback[2] else "other type"
                        fl.agent_rating = f"(no {fl.beds}BR eval; {beds_note}: {fallback[0]})" if fl.beds \
                            else f"({beds_note} only: {fallback[0]})"
                        fl.agent_eval_date = fallback[1]

        report = format_brackets_report(brackets, open_fighters)

        out_dir = Path("output")
        out_dir.mkdir(exist_ok=True)
        out_path = out_dir / "arena_latest.md"
        out_path.write_text(report)
        # Timestamped archive too — arena_latest.md is overwritten per run,
        # which would otherwise destroy appended referee verdicts.
        archive_path = out_dir / f"arena_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        archive_path.write_text(report)

        # Referee packet: the arena is purely algorithmic — the AI referee
        # must verify the stats behind the top ranks before results are trusted.
        packet = build_brackets_referee_packet(brackets)
        packet_path = out_dir / "arena_referee_packet.json"
        with open(packet_path, "w") as f:
            json.dump(packet, f, indent=2, ensure_ascii=False)

        # Append full results to the CSV history (the data backbone).
        # Timestamped (not just dated) so multiple same-day runs stay distinct.
        import csv as _csv
        run_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        arena_csv = Path("data") / "arena_results.csv"
        write_header = not arena_csv.exists()
        with open(arena_csv, "a", newline="") as f:
            writer = _csv.writer(f)
            if write_header:
                writer.writerow(["run_date", "bracket", "rank", "project_name", "beds", "elo",
                                 "wins", "losses", "draws", "win_rate_pct", "on_frontier",
                                 "mmr", "score_1000", "price", "psf", "district",
                                 "agent_rating", "agent_eval_date", "url"])
            for bracket_name, fighters in list(brackets.items()) + [("open", open_fighters)]:
                for rank, fl in enumerate(fighters, 1):
                    s = fl.listing
                    writer.writerow([run_date, bracket_name, rank, fl.name, fl.beds or "",
                                     round(fl.elo), fl.wins, fl.losses, fl.draws,
                                     round(fl.win_rate * 100), int(fl.on_frontier),
                                     s.mmr, s.score_1000, s.price, s.psf,
                                     s.district or "", fl.agent_rating or "",
                                     fl.agent_eval_date or "", s.url])

        print(report)
        print(f"\nArena report saved: {out_path}", file=sys.stderr)
        print(f"Arena history appended: {arena_csv}", file=sys.stderr)
        print(f"⚠ REFEREE REQUIRED: review {packet_path} (auto_flags: "
              f"{len(packet['auto_flags'])}) and verify top contenders' stats "
              f"before trusting this ranking.", file=sys.stderr)
        return

    # --- Condo profile handlers (git-tracked physical facts) ---
    if args.profile:
        import profile_memory
        profile_memory.print_profile(args.profile)
        return

    if args.list_profiles:
        import profile_memory
        profile_memory.print_index()
        return

    if args.save_profile:
        import profile_memory
        slug = profile_memory.save_profile_from_file(args.save_profile)
        print(f"Saved condo profile: profiles/{slug}.json")
        return

    # --- Evaluation memory handlers (git-tracked past evaluations) ---
    if args.recall:
        import eval_memory
        eval_memory.print_recall(args.recall)
        return

    if args.list_evals:
        import eval_memory
        eval_memory.print_index()
        return

    if args.eval_stats:
        import eval_memory
        eval_memory.print_eval_stats()
        return

    # --- Listings sheet/database handlers ---
    if args.search_db:
        import listings_db
        records = listings_db.search(args.search_db)
        print(f"\nListings sheet — results for '{args.search_db}':\n")
        print(listings_db.format_results(records))
        return

    if args.list_db:
        import listings_db
        s = listings_db.stats()
        print(f"\nListings sheet ({listings_db.SHEET_FILE})")
        print(f"  Total: {s['total']} | Active: {s['active']} | Stale: {s['stale']} | "
              f"Price drops: {s['price_drops']}")
        print(f"  Last updated: {s['updated_at']}")
        print("  By district:", ", ".join(f"{k}:{v}" for k, v in s['by_district'].items()))
        return

    if args.export_sheet:
        import listings_db
        path = listings_db.export_sheet()
        print(f"Listings sheet exported: {path}")
        return

    # Handle --from-review (standalone mode: generate report from reviewed JSON)
    if args.from_review:
        warn_if_stale_data()  # final report consumes cached datasets too
        print(f"\nLoading reviewed analysis: {args.from_review}", file=sys.stderr)
        report_level, reviewed_listings = load_reviewed_analysis(args.from_review)
        print(f"  Loaded {len(reviewed_listings)} listings", file=sys.stderr)

        # Reconstruct ScoredListing objects from reviewed data
        scored = []
        for entry in reviewed_listings:
            listing = ScoredListing(
                id=str(entry.get("rank", "")),
                title=entry.get("title", ""),
                price=entry.get("price", 0),
                url=entry.get("url", ""),
                district=entry.get("district"),
                beds=entry.get("beds"),
                sqft=entry.get("sqft"),
                psf=entry.get("psf"),
                tenure=entry.get("tenure"),
                built_year=entry.get("built_year"),
                project_name=entry.get("project_name"),
            )

            # Restore the algo's legacy /100 component scores. `algo_reference`
            # is written by scoring/raw_output._build_algo_breakdown as a flat
            # dict of rounded points (the report and print_results render them).
            ab = entry.get("algo_reference") or {}
            # Restore MMR (v3) if present
            if ab.get("mmr") is not None:
                listing.mmr = ab["mmr"]
                listing.score_1000 = ab.get("score_1000")
                listing.mmr_components = ab.get("mmr_components", {})
            listing.rental_yield_score = ab.get("rental_yield", 0)
            listing.capital_appreciation_score = ab.get("capital_appreciation", 0)
            listing.future_potential_score = ab.get("future_potential", 0)
            listing.liquidity_score = ab.get("liquidity", 0)
            listing.cost_efficiency_score = ab.get("cost_efficiency", 0)
            listing.red_flag_deductions = ab.get("red_flag_deductions", 0)

            # Restore remaining lease and MRT
            listing.remaining_lease = entry.get("remaining_lease")
            # nearest_mrt is normally a bare station name with the distance in
            # its own field, but hand-written entries use "Station (123m)".
            if entry.get("nearest_mrt"):
                mrt_str = entry["nearest_mrt"]
                m = re.match(r"(.+?)\s*\((\d+)m\)", mrt_str)
                if m:
                    listing.nearest_mrt = m.group(1)
                    listing.mrt_distance_m = int(m.group(2))
                else:
                    listing.nearest_mrt = mrt_str
            if entry.get("nearest_mrt_distance_m"):
                listing.mrt_distance_m = entry["nearest_mrt_distance_m"]

            # Restore rental & appreciation from factual_data
            fd = entry.get("factual_data", {})
            rental_fd = fd.get("rental", {})
            listing.estimated_monthly_rent = rental_fd.get("estimated_monthly_rent") or 0
            listing.estimated_gross_yield = rental_fd.get("gross_yield_pct") or 0
            listing.rent_source = rental_fd.get("rent_source") or ""

            cap_fd = fd.get("appreciation", {})
            # Explicit None check: 0% (or negative) is a legitimate rate and
            # must not be coerced to the fallback by `or`-chaining.
            rate_pct = cap_fd.get("annual_rate_pct")
            if rate_pct is None:
                # v3.4: regional fallback (was a hardcoded 2%, inconsistent with the
                # scorer's regional baselines and below every realized regional return).
                from config import REGIONAL_APPRECIATION_BASELINES, DEFAULT_REGIONAL_APPRECIATION
                _d = (entry.get("district") or "").upper().replace("D", "").strip()
                try:
                    _n = int(_d)
                    _region = ("CCR" if _n in {1, 2, 6, 7, 9, 10, 11}
                               else "RCR" if _n in {3, 4, 5, 8, 12, 13, 14, 15} else "OCR")
                except ValueError:
                    _region = None
                rate_pct = 100 * REGIONAL_APPRECIATION_BASELINES.get(
                    _region, DEFAULT_REGIONAL_APPRECIATION)
            listing.appreciation_rate = rate_pct / 100
            listing.appreciation_source = cap_fd.get("data_source") or "default"
            if entry.get("roi_sensitivity"):
                listing.roi_sensitivity = entry.get("roi_sensitivity")

            # Restore ROI projections
            from scoring.models import ROIResult
            roi_data = entry.get("roi_projections", {})
            for period_key, attr in [("5yr", "roi_5yr"), ("6yr", "roi_6yr"), ("7yr", "roi_7yr")]:
                roi_entry = roi_data.get(period_key)
                if roi_entry:
                    roi = ROIResult(
                        hold_years=int(period_key[0]),
                        purchase_price=listing.price,
                        estimated_exit_price=roi_entry.get("exit_price", 0),
                        total_rental_income=0,
                        total_holding_costs=0,
                        total_upfront_costs=listing.price,
                        total_exit_costs=0,
                        gross_rental_yield=0,
                        net_rental_yield=0,
                        capital_gain=roi_entry.get("exit_price", 0) - listing.price,
                        total_return=roi_entry.get("total_return", 0),
                        roi_percent=roi_entry.get("roi_pct", 0),
                        annualized_roi=roi_entry.get("annualized_roi", 0),
                    )
                    setattr(listing, attr, roi)

            # Apply agent fields (supports both old flat format and new nested format)
            ae = entry.get("agent_evaluation", {})
            listing.agent_rating = ae.get("rating") or entry.get("agent_rating")
            listing.agent_rating_rationale = ae.get("rating_rationale") or entry.get("agent_rating_rationale")
            listing.agent_summary = ae.get("summary") or entry.get("agent_summary")
            listing.agent_red_flags = ae.get("red_flags") or entry.get("agent_red_flags", [])
            listing.agent_catalysts = ae.get("catalysts") or entry.get("agent_catalysts", [])
            listing.agent_adjustment_reason = entry.get("agent_adjustment_reason")
            listing.agent_rental_assessment = ae.get("rental_assessment") or entry.get("agent_rental_assessment")
            listing.agent_appreciation_assessment = ae.get("appreciation_assessment") or entry.get("agent_appreciation_assessment")
            listing.agent_stack_notes = ae.get("stack_notes") or entry.get("agent_stack_notes")
            listing.agent_confidence = ae.get("confidence") or entry.get("agent_confidence")
            # None check (not `or`): an explicit 0% override must be honored.
            listing.agent_appreciation_rate_pct = ae.get("appreciation_rate_override_pct")
            if listing.agent_appreciation_rate_pct is None:
                listing.agent_appreciation_rate_pct = entry.get("agent_appreciation_rate_pct")
            listing.agent_appreciation_source = ae.get("appreciation_rate_source") or entry.get("agent_appreciation_source")

            # Apply AI appreciation override (if provided)
            if listing.agent_appreciation_rate_pct is not None:
                try:
                    agent_rate_pct = float(listing.agent_appreciation_rate_pct)
                except (TypeError, ValueError):
                    agent_rate_pct = None
                if agent_rate_pct is not None:
                    listing.appreciation_rate = agent_rate_pct / 100
                    listing.appreciation_source = "agent_override"

                    # Recompute MMR with the agent's appreciation rate.
                    # v3.4: carry the v3.2 cohort-depth damping through the override —
                    # an asserted rate doesn't make a thin size-cohort trade more, so
                    # a thin-cohort unit must not get the full project-pooled credit.
                    if listing.mmr is not None:
                        from scoring.mmr import apply_appreciation_override
                        from config import MIN_BAND_TXNS, COHORT_FLOOR_APPRECIATION
                        _cohort_txns = fd.get("psf_cohort_txns")
                        if _cohort_txns is None:
                            _cohort_factor = 1.0
                        else:
                            _cohort_factor = max(
                                COHORT_FLOOR_APPRECIATION, min(1.0, _cohort_txns / MIN_BAND_TXNS))
                        mmr_result = apply_appreciation_override(
                            {"mmr": listing.mmr, "components": listing.mmr_components},
                            agent_rate_pct,
                            cohort_appr_factor=_cohort_factor,
                        )
                        listing.mmr = mmr_result["mmr"]
                        listing.score_1000 = mmr_result["score_1000"]
                        listing.mmr_components = mmr_result["components"]

                    # Recompute the legacy /100 capital-appreciation component:
                    # swap the pre-override rate points out for the override's,
                    # keeping every non-rate sub-component (momentum, psf vs
                    # median, tenure, age) already folded into the stored score.
                    # The pre-override rate lives in factual_data.appreciation
                    # (annual_rate_pct); without it prev_rate_points is None and
                    # the recompute falls back to the rate points alone.
                    from scoring.full_scorer import FullScorer
                    rate_points = FullScorer.score_appreciation_rate_points(
                        agent_rate_pct,
                        listing.appreciation_source,
                    )
                    prev_rate_pct = cap_fd.get("annual_rate_pct")
                    prev_source = cap_fd.get("source") or listing.appreciation_source
                    try:
                        prev_rate_points = (
                            FullScorer.score_appreciation_rate_points(float(prev_rate_pct), prev_source)
                            if prev_rate_pct is not None
                            else None
                        )
                    except (TypeError, ValueError):
                        prev_rate_points = None
                    if prev_rate_points is not None:
                        listing.capital_appreciation_score = (
                            listing.capital_appreciation_score - prev_rate_points + rate_points)
                    else:
                        listing.capital_appreciation_score = rate_points

                    # Recompute ROI projections
                    from scoring.roi import ROICalculator
                    roi_calc = ROICalculator()
                    listing_dict = {
                        "price": listing.price,
                        "sqft": listing.sqft,
                        "district": listing.district,
                    }
                    roi_results = roi_calc.calculate_multiple_periods(
                        listing_dict,
                        periods=[5, 6, 7],
                        monthly_rent=listing.estimated_monthly_rent if listing.estimated_monthly_rent > 0 else None,
                        appreciation_rate=listing.appreciation_rate,
                    )
                    listing.roi_5yr = roi_results.get(5)
                    listing.roi_6yr = roi_results.get(6)
                    listing.roi_7yr = roi_results.get(7)
                    sensitivity = roi_calc.calculate_sensitivity(
                        listing_dict,
                        periods=[5, 7],
                        monthly_rent=listing.estimated_monthly_rent if listing.estimated_monthly_rent > 0 else None,
                        appreciation_rate=listing.appreciation_rate,
                    )
                    try:
                        from config import (
                            ROI_SENSITIVITY_RENT_DELTA_PCT,
                            ROI_SENSITIVITY_APPRECIATION_DELTA_PCT,
                        )
                    except ImportError:
                        ROI_SENSITIVITY_RENT_DELTA_PCT = 0.10
                        ROI_SENSITIVITY_APPRECIATION_DELTA_PCT = 0.015

                    def _roi_summary(roi):
                        return {
                            "exit_price": roi.estimated_exit_price,
                            "total_return": round(roi.total_return, 2),
                            "roi_pct": round(roi.roi_percent, 2),
                            "annualized_roi": round(roi.annualized_roi, 2),
                        }

                    listing.roi_sensitivity = {
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

            scored.append(listing)

        # Re-sort by MMR when available (includes agent overrides), else legacy score
        scored.sort(key=lambda s: s.rank_score, reverse=True)

        # Deduplicate: group listings from the same condo
        pre_dedup = len(scored)
        scored = deduplicate_by_project(scored)
        if len(scored) < pre_dedup:
            print(f"  Deduplicated: {pre_dedup} -> {len(scored)} unique condos", file=sys.stderr)

        # Count agent-reviewed listings
        reviewed_count = sum(1 for s in scored if s.agent_summary is not None)
        print(f"  Agent-reviewed: {reviewed_count}/{len(scored)}", file=sys.stderr)

        # Generate output
        output_base = args.output or str(Path(args.from_review).with_suffix(""))
        Path(output_base).parent.mkdir(parents=True, exist_ok=True)
        report_path = f"{output_base}_report.md"
        save_report(scored, report_path, report_level=report_level)
        print(f"\nFinal report saved: {report_path}")

        # Also save re-ranked JSON
        json_path = f"{output_base}_final.json"
        json_data = {
            "analyzed_at": datetime.now().isoformat(),
            "total": len(scored),
            "agent_reviewed": reviewed_count,
            "report_level": report_level,
            "listings": [s.to_dict() for s in scored],
        }
        with open(json_path, "w") as f:
            json.dump(json_data, f, indent=2, ensure_ascii=False)
        print(f"Final JSON saved: {json_path}")

        # Auto-save evaluations into git-tracked memory (unless opted out)
        if not args.no_save_eval:
            import eval_memory
            saved = eval_memory.save_evaluations_from_review(args.from_review, run_dir=str(Path(args.from_review).parent))
            if saved:
                print(f"Evaluation memory: saved {saved} evaluation(s) to {eval_memory.EVAL_DIR}")
                print(f"  (commit the evaluations/ folder to share them — note: past evaluations may be stale/wrong)")

        # Print results
        print_results(scored, args.top, args.verbose)
        return


    # Handle --build-ura-cache
    if args.build_ura_cache:
        print("\nBuilding URA cache from CSV files...")
        build_ura_cache_from_csv(args.build_ura_cache)
        return

    # Handle --fetch-ura-districts
    if args.fetch_ura_districts:
        from fetch_ura_districts import main as fetch_districts_main
        import sys as _sys
        # Build argv for fetch_ura_districts
        fetch_args = []
        if args.fetch_ura_districts.lower() != "all":
            fetch_args.extend(["--districts", args.fetch_ura_districts])
        # Pass through and let fetch_ura_districts handle it
        _sys.argv = ["fetch_ura_districts.py"] + fetch_args
        fetch_districts_main()
        return

    run_dir = None

    # Handle --url mode (scrape from a PropertyGuru URL: listing, results, or project page)
    if args.url:
        beds = [int(b.strip()) for b in (args.beds or "2,3").split(",")]
        url_enrich = args.enrich_top if args.enrich_top > 0 else 20
        listings = scrape_url_listings(
            url=args.url,
            max_pages=args.url_max_pages,
            headless=headless,
            enrich_top=url_enrich,
            # Don't price-gate a pasted URL: results pages already encode the
            # user's filters, and a project page should surface all unit types.
            min_price=None,
            max_price=None,
            beds=beds,
        )

        if not listings:
            print("No listings found", file=sys.stderr)
            return

        run_dir = next_run_dir(slug=url_run_slug(args.url))
        listings_file = run_dir / "listings_url.json"
        with open(listings_file, "w") as f:
            json.dump(listings, f, indent=2, ensure_ascii=False)
        print(f"Run directory: {run_dir}", file=sys.stderr)
        print(f"Saved: {listings_file}", file=sys.stderr)

    # Handle --condo mode (scrape by condo name)
    elif args.condo:
        beds = [int(b.strip()) for b in (args.beds or "2,3").split(",")]
        # Default enrich_top=20 in condo mode to get floor_level/facing
        condo_enrich = args.enrich_top if args.enrich_top > 0 else 20
        listings = scrape_condo_listings(
            condo_name=args.condo,
            min_price=args.min_price,
            max_price=args.max_price,
            beds=beds,
            max_pages=args.condo_max_pages,
            headless=headless,
            enrich_top=condo_enrich,
            fuzzy=args.condo_fuzzy,
            max_results=args.condo_max_results,
        )

        if not listings:
            print("No listings found", file=sys.stderr)
            return

        safe_condo = "_".join(args.condo.lower().split())
        run_dir = next_run_dir(slug=safe_condo)
        listings_file = run_dir / f"listings_{safe_condo}.json"
        with open(listings_file, "w") as f:
            json.dump(listings, f, indent=2, ensure_ascii=False)
        print(f"Run directory: {run_dir}", file=sys.stderr)
        print(f"Saved: {listings_file}", file=sys.stderr)

    # Handle --auto mode (NEW)
    elif args.auto:
        print("\n" + "=" * 60, file=sys.stderr)
        print("AUTOMATIC DISTRICT DISCOVERY (v2.2)", file=sys.stderr)
        print("=" * 60, file=sys.stderr)

        districts = discover_districts(
            top_n=args.auto_districts,
            exclude_ccr=not args.include_ccr,
        )

        print(f"  AI-selected districts: {districts}", file=sys.stderr)
        print(f"  Criteria: 50% historical + 50% future potential", file=sys.stderr)

        # Continue with scraping using discovered districts
        beds = [int(b.strip()) for b in (args.beds or "2,3").split(",")]

        listings = scrape_listings(
            districts=districts,
            min_price=args.min_price,
            max_price=args.max_price,
            beds=beds,
            max_pages=args.max_pages,
            headless=headless,
            enrich_top=args.enrich_top,
        )

        if not listings:
            print("No listings found", file=sys.stderr)
            return

        # Save scraped listings into run directory
        run_dir = next_run_dir()
        district_str = "_".join(str(d) for d in districts)
        listings_file = run_dir / f"listings_D{district_str}.json"
        with open(listings_file, "w") as f:
            json.dump(listings, f, indent=2, ensure_ascii=False)
        print(f"Run directory: {run_dir}", file=sys.stderr)
        print(f"Saved: {listings_file}", file=sys.stderr)

    # Get listings (manual mode)
    elif args.input:
        listings = []
        # Load from file (support glob patterns)
        input_files = file_glob(args.input) if '*' in args.input else [args.input]
        if not input_files:
            print(f"No files match: {args.input}", file=sys.stderr)
            sys.exit(1)

        for input_file in input_files:
            print(f"Loading: {input_file}...", file=sys.stderr)
            with open(input_file) as f:
                data = json.load(f)
            file_listings = data if isinstance(data, list) else data.get("listings", [])
            listings.extend(file_listings)

        print(f"Total loaded: {len(listings)} listings", file=sys.stderr)

    elif args.districts:
        # Scrape new listings (manual district selection)
        districts = [int(d.strip()) for d in args.districts.split(",")]
        beds = [int(b.strip()) for b in (args.beds or "2,3").split(",")]

        listings = scrape_listings(
            districts=districts,
            min_price=args.min_price,
            max_price=args.max_price,
            beds=beds,
            max_pages=args.max_pages,
            headless=headless,
            enrich_top=args.enrich_top,
        )

        if not listings:
            print("No listings found", file=sys.stderr)
            return

        # Save scraped listings into run directory
        run_dir = next_run_dir()
        district_str = "_".join(str(d) for d in districts)
        listings_file = run_dir / f"listings_D{district_str}.json"
        with open(listings_file, "w") as f:
            json.dump(listings, f, indent=2, ensure_ascii=False)
        print(f"Run directory: {run_dir}", file=sys.stderr)
        print(f"Saved: {listings_file}", file=sys.stderr)

    else:
        # No mode specified
        parser.print_help()
        print("\n\nError: Must specify --url, --condo, --auto, --districts, or --input",
              file=sys.stderr)
        return

    # --- Update the master listings sheet (continuously-updated, AI-searchable) ---
    if listings and not args.no_db:
        import listings_db
        if args.url:
            src = {"flow": "url", "url": args.url}
        elif args.condo:
            src = {"flow": "condo", "condo": args.condo}
        elif args.auto:
            src = {"flow": "auto", "districts": districts}
        elif args.districts:
            src = {"flow": "districts", "districts": args.districts}
        else:
            src = {"flow": "input"}
        db_stats = listings_db.upsert_listings(listings, source=src)
        print(
            f"\nListings sheet updated: +{db_stats['added']} new, "
            f"{db_stats['updated']} refreshed, {db_stats['price_changes']} price changes "
            f"(total {db_stats['total']}) -> {listings_db.SHEET_FILE}",
            file=sys.stderr,
        )

    # --- --update-db: refresh the sheet only, skip scoring/report ---
    if args.update_db:
        print("\n--update-db: listings sheet refreshed; skipping scoring/report.", file=sys.stderr)
        return

    # Load URA cache
    ura_data = load_ura_cache()
    if ura_data:
        print(f"\nURA cache loaded: {len(ura_data)} projects", file=sys.stderr)

    # Check which projects need URA data
    unique_projects = set()
    for listing in listings:
        project = listing.get("title") or listing.get("project_name", "")
        if project:
            unique_projects.add(project)

    cached_count = sum(1 for p in unique_projects if p.lower() in ura_data)
    stale_count = 0
    for p in unique_projects:
        pk = p.lower()
        if pk in ura_data:
            updated = ura_data[pk].get("updated", "")
            if updated:
                try:
                    days_old = (datetime.now() - datetime.strptime(updated, "%Y-%m-%d")).days
                    if days_old > 7:
                        stale_count += 1
                except ValueError:
                    stale_count += 1

    missing = len(unique_projects) - cached_count
    print(f"\n  Projects: {len(unique_projects)} unique", file=sys.stderr)
    print(f"  URA cached: {cached_count} ({stale_count} stale >7 days)", file=sys.stderr)
    print(f"  Missing URA data: {missing} projects", file=sys.stderr)
    if missing > 0 or stale_count > 0:
        uncached = [p for p in unique_projects if p.lower() not in ura_data]
        if uncached:
            print(f"  Projects without data: {', '.join(sorted(uncached)[:10])}", file=sys.stderr)
            if len(uncached) > 10:
                print(f"    ... and {len(uncached) - 10} more", file=sys.stderr)

        # Suggest URA fetch based on districts found in listings
        listing_districts = set()
        for listing in listings:
            d = listing.get("district") or ""
            d_num = str(d).replace("D", "").replace("d", "").strip()
            if d_num.isdigit():
                listing_districts.add(int(d_num))
        if listing_districts:
            district_str = ",".join(str(d) for d in sorted(listing_districts))
            print(f"\n  ** Fetch URA data by district (fastest - gets ALL projects): **", file=sys.stderr)
            print(f"  **   python invest.py --fetch-ura-districts {district_str} **", file=sys.stderr)
        else:
            print(f"\n  ** Fetch URA data for all districts: **", file=sys.stderr)
            print(f"  **   python invest.py --fetch-ura-districts all **", file=sys.stderr)
        print(f"  ** Or build cache from existing CSVs: **", file=sys.stderr)
        print(f"  **   python invest.py --build-ura-cache data/ura_*.csv **", file=sys.stderr)

    # Data freshness warning (best-effort)
    warn_if_stale_data()

    # Score listings with pre-filtering (skip full scoring on obvious rejects).
    print(f"\nScoring {len(listings)} listings...", file=sys.stderr)
    result = score_and_filter(listings, min_quick_score=40, ura_data=ura_data)
    scored = result["scored"]
    rejected = result["rejected"]
    if rejected:
        print(f"  Pre-filtered: {len(rejected)} low-scoring listings skipped", file=sys.stderr)

    if not scored:
        print("No scoreable listings", file=sys.stderr)
        return

    # Deduplicate / group listings
    pre_dedup = len(scored)
    if args.condo:
        scored = group_condo_by_unit_type(scored)
        if len(scored) < pre_dedup:
            print(f"  Grouped by unit type: {pre_dedup} -> {len(scored)} unit types", file=sys.stderr)
    else:
        scored = deduplicate_by_project(scored)
        if len(scored) < pre_dedup:
            print(f"  Deduplicated: {pre_dedup} -> {len(scored)} unique condos", file=sys.stderr)

    # Generate outputs — always produce report + raw JSON into run directory
    if run_dir:
        # Always save markdown report
        report_path = str(run_dir / "report.md")
        if args.condo:
            report_title = f"Condo Analysis: {args.condo}"
        elif args.url:
            report_title = "Listing Analysis (from PropertyGuru URL)"
        else:
            report_title = "Property Investment Analysis"
        save_report(scored, report_path, title=report_title)
        print(f"\nReport saved: {report_path}")

        # Always save scored JSON
        json_path = str(run_dir / "scored.json")
        with open(json_path, "w") as f:
            json.dump(scored_json_payload(scored), f, indent=2, ensure_ascii=False)
        print(f"JSON saved: {json_path}")

        # Always save raw analysis for agent review
        run_config = {
            "beds": [int(b.strip()) for b in (args.beds or "2,3").split(",")],
        }
        if args.min_price is not None or args.max_price is not None:
            run_config["price_range"] = [args.min_price, args.max_price]
        if args.condo:
            run_config["condo"] = args.condo
            run_config["condo_fuzzy"] = args.condo_fuzzy
            run_config["condo_max_pages"] = args.condo_max_pages
            run_config["condo_max_results"] = args.condo_max_results
        if args.url:
            run_config["url"] = args.url
        if args.districts:
            run_config["districts"] = [f"D{d.strip()}" for d in args.districts.split(",")]
        elif args.auto:
            run_config["districts"] = [f"D{d}" for d in districts]

        raw_path = save_raw_analysis(scored, str(run_dir / "raw_analysis.json"), ura_data, run_config)
        print(f"Raw analysis saved: {raw_path}")

        # CSV if requested
        if args.csv:
            csv_content = generate_csv_export(scored)
            csv_path = str(run_dir / "export.csv")
            with open(csv_path, "w") as f:
                f.write(csv_content)
            print(f"CSV saved: {csv_path}")

        print(f"\n  All outputs in: {run_dir}")
        print(f"  -> {len(scored)} listings scored")
        print(f"  -> To do agent review: fill agent_* fields in {raw_path}")
        print(f"  -> Then: python invest.py --from-review {raw_path} --output {run_dir / 'final'}")

    else:
        # --input mode (no run_dir) — use explicit output flags
        if args.output:
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            report_path = f"{args.output}.md"
            save_report(scored, report_path)
            print(f"\nReport saved: {report_path}")

        if args.csv:
            csv_content = generate_csv_export(scored)
            with open(args.csv, "w") as f:
                f.write(csv_content)
            print(f"CSV saved: {args.csv}")

        if args.json:
            with open(args.json, "w") as f:
                json.dump(scored_json_payload(scored), f, indent=2, ensure_ascii=False)
            print(f"JSON saved: {args.json}")

        if args.raw:
            run_config = {
                "beds": [int(b.strip()) for b in (args.beds or "2,3").split(",")],
            }
            if args.min_price is not None or args.max_price is not None:
                run_config["price_range"] = [args.min_price, args.max_price]
            if args.districts:
                run_config["districts"] = [f"D{d.strip()}" for d in args.districts.split(",")]
            raw_path = save_raw_analysis(scored, args.raw, ura_data, run_config)
            print(f"\nRaw analysis saved: {raw_path}")

    # Print results
    print_results(scored, args.top, args.verbose)


if __name__ == "__main__":
    main()
