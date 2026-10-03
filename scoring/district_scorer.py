"""District scorer for automatic district discovery.

Scores all 28 Singapore districts for investment potential using:
- Historical appreciation (30% weight)
- Liquidity/transaction volume (20% weight)
- Future infrastructure (25% weight)
- Government development priority (20% weight)
- Supply constraint (5% weight)

This enables automatic district selection instead of hardcoded D03/05/14/15.
"""

import json
import os
from datetime import datetime
from typing import Optional

from scoring.models import DistrictScore

# Data directory path
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")


class DistrictScorer:
    """Scores all 28 districts for investment potential.

    Scoring weights (total 100):
    - Historical Appreciation: 30%
    - Liquidity (transaction volume): 20%
    - Future Infrastructure: 25%
    - Government Development Priority: 20%
    - Supply Constraint: 5%
    """

    # Weights
    WEIGHT_HISTORICAL = 0.30
    WEIGHT_LIQUIDITY = 0.20
    WEIGHT_FUTURE_INFRA = 0.25
    WEIGHT_GOVT_PRIORITY = 0.20
    WEIGHT_SUPPLY = 0.05

    def __init__(self):
        self.district_profiles = self._load_district_profiles()
        self.infra_data = self._load_infrastructure_data()
        self.zone_data = self._load_government_zones()
        self.ura_district_rates = self._derive_ura_district_rates()

    @staticmethod
    def _derive_ura_district_rates() -> dict:
        """Median URA-measured appreciation per district, where coverage allows.

        The static profile `historical_appreciation` values are regional
        baselines restated (post-v3.4: every RCR district = 3.7%, OCR = 4.0%), so
        the 30% 'historical' axis was a disguised region prior that double-
        counted region. Where the URA cache and listings DB overlap on >=3
        projects, use measured per-district data instead.
        """
        import statistics
        try:
            with open(os.path.join(_DATA_DIR, "ura_cache.json")) as f:
                ura = json.load(f)
            ura = ura.get("projects", ura)
        except (OSError, json.JSONDecodeError):
            return {}

        # Cache entries carry their district directly (since the v2.4 grouped
        # cache build). Fall back to listings_db project->district mapping for
        # older cache entries without it.
        proj_to_district = {}
        try:
            with open(os.path.join(_DATA_DIR, "listings_db.json")) as f:
                db = json.load(f)
            listings = db.get("listings", {})
            if isinstance(listings, dict):
                listings = list(listings.values())
            for l in listings:
                pn = (l.get("project_name") or "").lower()
                d = str(l.get("district") or "").upper().replace("D", "").lstrip("0")
                if pn and d:
                    proj_to_district[pn] = d
        except (OSError, json.JSONDecodeError):
            pass

        by_district: dict[str, list[float]] = {}
        for key, entry in ura.items():
            if not isinstance(entry, dict) or entry.get("annualized_appreciation") is None:
                continue
            if entry.get("is_ec"):
                continue  # district rates describe the condo market (EC prices move separately)
            d = str(entry.get("district") or "").upper().replace("D", "").lstrip("0") or proj_to_district.get(key)
            if d:
                by_district.setdefault(d, []).append(entry["annualized_appreciation"])

        return {
            d: {"rate": statistics.median(vals) / 100, "projects": len(vals)}
            for d, vals in by_district.items()
            if len(vals) >= 3
        }

    def _load_district_profiles(self) -> dict:
        """Load district profiles data (reuses FutureScorer's module cache)."""
        from scoring.future_scorer import FutureScorer
        fs = FutureScorer.__new__(FutureScorer)
        return FutureScorer._load_district_profiles(fs)

    def _load_infrastructure_data(self) -> dict:
        """Load future MRT infrastructure data (reuses FutureScorer's module cache)."""
        from scoring.future_scorer import FutureScorer
        fs = FutureScorer.__new__(FutureScorer)
        return FutureScorer._load_infrastructure_data(fs)

    def _load_government_zones(self) -> dict:
        """Load government development zones data (reuses FutureScorer's module cache)."""
        from scoring.future_scorer import FutureScorer
        fs = FutureScorer.__new__(FutureScorer)
        return FutureScorer._load_government_zones(fs)

    def score_all_districts(self, region_filter: Optional[str] = None) -> list[DistrictScore]:
        """Score all districts and return ranked list.

        Args:
            region_filter: Optional filter for "CCR", "RCR", or "OCR"

        Returns:
            List of DistrictScore sorted by total_score descending
        """
        results = []
        districts = self.district_profiles.get("districts", {})

        for district_num, profile in districts.items():
            region = profile.get("region", "")
            if region_filter and region != region_filter:
                continue

            score = self._score_district(int(district_num), profile)
            results.append(score)

        # Sort by total score descending
        results.sort(key=lambda x: x.total_score, reverse=True)
        return results

    def _score_district(self, district_num: int, profile: dict) -> DistrictScore:
        """Score a single district."""
        future_mrt = self._filter_future_mrt_lines(profile.get("future_mrt", []))
        result = DistrictScore(
            district=district_num,
            name=profile.get("name", f"District {district_num}"),
            region=profile.get("region", "OCR"),
            total_score=0,
        )

        # Store raw data
        result.historical_appreciation = profile.get("historical_appreciation", 0.02)
        result.median_psf = profile.get("median_psf", 1500)
        result.future_mrt_lines = future_mrt
        result.govt_zones = profile.get("govt_zones", [])

        # A. Historical Appreciation Score (0-100)
        # Prefer URA-measured per-district data over the profile's regional prior.
        derived = self.ura_district_rates.get(str(district_num))
        if derived:
            result.historical_appreciation = derived["rate"]
        result.historical_score = self._score_historical_rate(result.historical_appreciation)

        # B. Liquidity Score (0-100)
        result.liquidity_score = self._score_liquidity(profile)

        # C. Future Infrastructure Score (0-100)
        result.future_infra_score = self._score_future_infra(future_mrt)

        # D. Government Priority Score (0-100)
        result.govt_priority_score = self._score_govt_priority(profile)

        # E. Supply Constraint Score (0-100)
        result.supply_score = self._score_supply(profile)

        # Calculate weighted total
        result.total_score = (
            result.historical_score * self.WEIGHT_HISTORICAL +
            result.liquidity_score * self.WEIGHT_LIQUIDITY +
            result.future_infra_score * self.WEIGHT_FUTURE_INFRA +
            result.govt_priority_score * self.WEIGHT_GOVT_PRIORITY +
            result.supply_score * self.WEIGHT_SUPPLY
        )

        # Build key catalysts list
        result.key_catalysts = self._build_catalysts(profile, result, future_mrt)
        if derived:
            result.key_catalysts.append(
                f"URA-measured appreciation {derived['rate']*100:.1f}%/yr ({derived['projects']} projects)"
            )

        return result

    @staticmethod
    def _score_historical_rate(rate: float) -> float:
        """Score an appreciation rate (decimal) on the 0-100 axis.

        Rate mapping:
        - >= 4%: 100
        - >= 3.5%: 85
        - >= 3%: 70
        - >= 2.5%: 55
        - >= 2%: 40
        - < 2%: 25
        """
        if rate >= 0.04:
            return 100
        elif rate >= 0.035:
            return 85
        elif rate >= 0.03:
            return 70
        elif rate >= 0.025:
            return 55
        elif rate >= 0.02:
            return 40
        else:
            return 25

    def _score_liquidity(self, profile: dict) -> float:
        """Score liquidity/transaction volume (0-100)."""
        volume = profile.get("transaction_volume", "medium")

        scores = {
            "very_high": 100,
            "high": 80,
            "medium": 50,
            "low": 20,
        }

        return scores.get(volume, 50)

    def _score_future_infra(self, future_mrt: list[str]) -> float:
        """Score future infrastructure (0-100).

        Based on number of upcoming MRT lines.
        """
        if len(future_mrt) >= 2:
            return 100  # Multiple new MRT lines
        elif len(future_mrt) == 1:
            # Check if it's a major line
            major_lines = {"TEL", "CRL", "JRL"}
            if any(line in major_lines for line in future_mrt):
                return 80
            return 60
        else:
            return 20  # No upcoming MRT

    def _score_govt_priority(self, profile: dict) -> float:
        """Score government development priority (0-100)."""
        govt_zones = profile.get("govt_zones", [])
        zone_data = self.zone_data.get("development_zones", {})

        if not govt_zones:
            return 20  # No govt zones

        # Find highest priority zone
        best_score = 0
        for zone_id in govt_zones:
            zone_info = zone_data.get(zone_id, {})
            priority = zone_info.get("priority", "low")

            priority_scores = {
                "high": 100,
                "medium": 70,
                "low": 40,
            }

            score = priority_scores.get(priority, 40)
            if score > best_score:
                best_score = score

        # Bonus for multiple zones
        if len(govt_zones) >= 2:
            best_score = min(100, best_score + 10)

        return best_score

    def _score_supply(self, profile: dict) -> float:
        """Score supply constraint (0-100).

        High constraint = higher score (price support).
        """
        supply = profile.get("supply_constraint", "medium")

        scores = {
            "very_high": 100,
            "high": 80,
            "medium": 50,
            "low": 30,
        }

        return scores.get(supply, 50)

    def _build_catalysts(
        self,
        profile: dict,
        score: DistrictScore,
        future_mrt: list[str],
    ) -> list[str]:
        """Build list of key investment catalysts."""
        catalysts = []

        # Add MRT lines
        for mrt in future_mrt:
            mrt_name = self._get_mrt_line_name(mrt)
            catalysts.append(mrt_name)

        # Add govt zones
        zone_data = self.zone_data.get("development_zones", {})
        for zone_id in profile.get("govt_zones", []):
            zone_info = zone_data.get(zone_id, {})
            zone_name = zone_info.get("short_name", zone_id)
            catalysts.append(zone_name)

        # Add region-based catalyst
        region = profile.get("region", "")
        if region == "RCR":
            if score.historical_score >= 85:
                catalysts.append("RCR Prime")
        elif region == "OCR":
            if score.total_score >= 70:
                catalysts.append("High-potential OCR")

        return catalysts[:4]  # Limit to top 4 catalysts

    def _get_mrt_line_name(self, code: str) -> str:
        """Get MRT line short name from code."""
        mrt_lines = self.infra_data.get("mrt_lines", {})
        line_info = mrt_lines.get(code, {})
        return line_info.get("name", code)

    # v3.4: mirror future_scorer.RECENT_OPERATIONAL_YEARS — a just-opened line is
    # still a connectivity catalyst. Kept in sync with scoring/future_scorer.py.
    RECENT_OPERATIONAL_YEARS = 2

    def _filter_future_mrt_lines(self, lines: list[str]) -> list[str]:
        """Filter MRT lines to those completing soon or opened within the last ~2yr.

        v3.4: matches future_scorer — a recently-operational line (e.g. TEL 2025)
        still counts as a connectivity catalyst rather than being dropped, so district
        discovery doesn't undercount areas that just got a new line.
        """
        current_year = datetime.now().year
        filtered = []
        for code in lines:
            line_info = self.infra_data.get("mrt_lines", {}).get(code, {})
            completion = line_info.get("completion", "")
            completion_year = None
            try:
                completion_year = int(str(completion)[:4])
            except (ValueError, TypeError):
                completion_year = None

            # recently-operational lines still count (no status auto-exclude)
            if completion_year is not None and completion_year < current_year - self.RECENT_OPERATIONAL_YEARS:
                continue
            filtered.append(code)
        return filtered

    def get_top_districts(
        self,
        top_n: int = 5,
        region_filter: Optional[str] = None,
        exclude_ccr: bool = False,
    ) -> list[int]:
        """Get top N districts for investment.

        Args:
            top_n: Number of districts to return
            region_filter: Optional filter for specific region
            exclude_ccr: Exclude CCR (high entry cost)

        Returns:
            List of district numbers
        """
        all_scores = self.score_all_districts(region_filter)

        if exclude_ccr:
            all_scores = [s for s in all_scores if s.region != "CCR"]

        return [s.district for s in all_scores[:top_n]]
