"""Age-adjusted relative value — is this condo cheap or dear *for its age*?

Comparing a 10-year-old condo's PSF directly against a new launch is unfair:
new launches carry a freshness premium that decays with age.

v3.7: the decay is MEASURED and PIECEWISE (district-FE hedonic on 16.6k recent
leasehold resales — see config.AGE_PSF_DECAY_SEGMENTS): ~3%/yr (≈$50/psf/yr)
for the first 10 years, a 10–15yr plateau, a second leg down at 15–20, then a
slow drift. Adjustment is multiplicative in log space, so it scales with each
peer's own PSF level (no per-region $ constants needed).

This module normalizes every peer project's PSF to the subject's age using
that curve, then asks two questions:

1. vs the area: is the subject priced above or below the age-adjusted median
   of its district peers? (premium_vs_age_adjusted_median_pct — negative
   means cheap for its age)
2. vs new launches: given what new launches transact at in this district,
   what *should* the subject cost after age depreciation, and how does its
   asking PSF compare? (premium_vs_new_launch_implied_pct)

Peer data comes from the URA cache (per-project median transacted PSF +
lease-start year + district), i.e. real transactions, not asking prices.

⚠ The freehold factor is still a heuristic (freehold age isn't derivable from
URA tenure strings). Freehold peers WITHOUT a derivable age are included at
their raw PSF (no age normalization, flagged via freehold_unadjusted_peer_count)
rather than dropped — excluding them biased freehold-heavy districts (Jun-2026
audit). Re-measure the curve via backtest_ext PART 1c.
"""

import math
from statistics import median
from typing import Any, Optional

try:
    from config import (
        AGE_PSF_DECAY_SEGMENTS,
        FREEHOLD_SLOPE_FACTOR,
        MIN_BAND_TXNS,
        NEW_SALE_PROPORTION_THRESHOLD,
        size_band_key,
    )
except ImportError:
    # Fallbacks mirror config.py (v3.7 measured piecewise curve; sync if config changes).
    AGE_PSF_DECAY_SEGMENTS = [
        (0, 10, 0.030), (10, 15, 0.005), (15, 20, 0.026), (20, 30, 0.010), (30, 99, 0.017),
    ]
    FREEHOLD_SLOPE_FACTOR = 0.6
    MIN_BAND_TXNS = 5
    NEW_SALE_PROPORTION_THRESHOLD = 0.20

    def size_band_key(sqft):
        return None

# Lease start (land acquisition) precedes TOP by ~3 years — same offset the
# lease calculators use.
_LEASE_TO_TOP_OFFSET = 3
# Beyond this age difference, even the piecewise curve is extrapolating across
# too many vintage cohorts to be credible (curve measured to ~45yr).
_MAX_ADJUST_YEARS = 35
_MIN_PEERS = 5
_MIN_NEW_LAUNCH_PEERS = 2
_NEW_LAUNCH_MAX_AGE = 3

_CCR = {1, 2, 6, 7, 9, 10, 11}
_RCR = {3, 4, 5, 8, 12, 13, 14, 15}


def _region_for_district(district: str) -> str:
    try:
        d = int(str(district).upper().replace("D", "").strip() or 0)
    except ValueError:
        return "OCR"
    if d in _CCR:
        return "CCR"
    if d in _RCR:
        return "RCR"
    return "OCR"


def _is_freehold(tenure: Optional[str]) -> bool:
    return bool(tenure and ("freehold" in tenure.lower() or "999" in tenure))


def _canon_name(name: Optional[str]) -> Optional[str]:
    """Casing/punctuation-insensitive project-name key ('SUITES@ KATONG' ==
    'Suites @ Katong') — used to keep the subject out of its own peer set."""
    if not name:
        return None
    import re
    s = re.sub(r"[^a-z0-9]", "", str(name).lower())
    return s or None


def _cum_decay(age: float) -> float:
    """Cumulative log-PSF decay from age 0 to `age` (measured piecewise curve)."""
    if age <= 0:
        return 0.0
    total = 0.0
    for lo, hi, rate in AGE_PSF_DECAY_SEGMENTS:
        total += rate * max(0.0, min(age, hi) - lo)
    return total


def _decay_factor(from_age: float, to_age: float, tenure: Optional[str]) -> float:
    """Multiplicative PSF factor for aging a price from `from_age` to `to_age`.

    > 1 when normalizing to a younger age, < 1 to an older age. Freehold decays
    at FREEHOLD_SLOPE_FACTOR of the measured leasehold curve (heuristic).
    """
    gap = _cum_decay(to_age) - _cum_decay(from_age)
    if _is_freehold(tenure):
        gap *= FREEHOLD_SLOPE_FACTOR
    return math.exp(-gap)


def _local_slope_per_year(age: float, psf: float, tenure: Optional[str]) -> float:
    """$/psf/yr at this age and PSF level (reporting only)."""
    rate = AGE_PSF_DECAY_SEGMENTS[-1][2]
    for lo, hi, r in AGE_PSF_DECAY_SEGMENTS:
        if lo <= age < hi:
            rate = r
            break
    if _is_freehold(tenure):
        rate *= FREEHOLD_SLOPE_FACTOR
    return rate * psf


def compute_relative_value(
    subject_psf: float,
    subject_age: float,
    district: str,
    tenure: Optional[str],
    ura_data: dict,
    current_year: int,
    subject_sqft: Optional[float] = None,
    subject_name: Optional[str] = None,
    subject_is_ec: bool = False,
) -> Optional[dict[str, Any]]:
    """Age-adjusted relative value of a subject unit vs its district peers.

    v3.4 size-aware: when `subject_sqft` is given, each peer is compared on its
    SAME-SIZE-BAND median PSF (a 1BR vs other small units), not its size-mixed
    project median — so a small unit no longer reads as artificially "cheap"
    against a district median dominated by larger formats. Peers without a deep
    enough same-band cohort fall back to their pooled median; `band_peer_count`
    reports how many peers were true like-for-like.

    Jun-2026 audit fixes: `subject_name` (when given) excludes the subject's
    own project from the peer median (self-inclusion pulled the benchmark
    toward the subject's own prints), and freehold peers without a derivable
    age are INCLUDED at their raw PSF instead of dropped — the old
    `if not lease_start: continue` silently excluded ~93% of freehold projects,
    making freehold subjects read ~5% structurally dear in freehold districts.

    Returns None when inputs or peer coverage are insufficient (missing data
    is neutral — the caller must not penalize a None).
    """
    if not subject_psf or subject_psf <= 0 or subject_age is None or not district:
        return None

    district = str(district).upper()
    if not district.startswith("D"):
        district = f"D{int(district):02d}" if district.isdigit() else district
    region = _region_for_district(district)
    band = size_band_key(subject_sqft) if subject_sqft else None

    # --- Collect peers: same district, with transacted PSF and derivable age ---
    target_name = _canon_name(subject_name)
    peers = []
    band_used = 0
    freehold_unadjusted = 0
    for entry in ura_data.values():
        if not isinstance(entry, dict) or entry.get("district") != district:
            continue
        # One product per comparison: an EC's peers are ECs, a condo's are
        # condos. Cache entries without `is_ec` are condos.
        if bool(entry.get("is_ec")) != bool(subject_is_ec):
            continue
        if target_name and _canon_name(entry.get("project_name")) == target_name:
            continue  # the subject is not its own peer (self-inclusion bias)
        lease_start = entry.get("lease_start_year")
        age_known = True
        if lease_start:
            peer_age = current_year - (lease_start + _LEASE_TO_TOP_OFFSET)
        elif _is_freehold(entry.get("tenure")):
            # Freehold tenure strings carry no commencement year and the URA
            # cache has no TOP/first-print field, so a freehold peer's age is
            # usually not derivable. Derive what the entry does support:
            #   - a project still selling a meaningful share of New Sale units
            #     is an active launch → age ≈ 1 (post-TOP selling window);
            #   - otherwise include the peer UNADJUSTED (treated as the
            #     subject's age, decay factor 1.0). Freehold decays at only
            #     FREEHOLD_SLOPE_FACTOR of the (mostly mild) measured curve, so
            #     the unadjusted error is far smaller than the systematic bias
            #     of dropping the peer entirely.
            if (entry.get("new_sale_proportion") or 0) >= NEW_SALE_PROPORTION_THRESHOLD:
                peer_age = 1.0
            else:
                peer_age = subject_age
                age_known = False
        else:
            continue  # leasehold without a commencement year — truly underivable
        # Size-aware: prefer the peer's same-size-band median; fall back to pooled.
        psf = None
        used_band = False
        if band:
            bands = (entry.get("by_size") or {}).get("bands") or {}
            b = bands.get(band)
            if b and (b.get("txn_count") or 0) >= MIN_BAND_TXNS and b.get("median_psf"):
                psf = b["median_psf"]
                used_band = True
        if psf is None:
            psf = entry.get("median_psf") or entry.get("avg_psf_current")
        if not psf or psf <= 0:
            continue
        if peer_age < 0:
            peer_age = 0
        age_gap = subject_age - peer_age
        if abs(age_gap) > _MAX_ADJUST_YEARS:
            continue  # too far apart to age-normalize credibly
        # Normalize the peer's PSF to the subject's age along the measured
        # piecewise curve: a NEWER peer is discounted (it would be cheaper at
        # the subject's age), an OLDER peer is marked up. Multiplicative, so
        # it scales with the peer's own PSF level.
        adjusted_psf = psf * _decay_factor(peer_age, subject_age, entry.get("tenure"))
        if adjusted_psf <= 0:
            continue
        if used_band:
            band_used += 1
        if not age_known:
            freehold_unadjusted += 1
        peers.append({
            "name": entry.get("project_name"),
            "raw_psf": psf,
            "age": peer_age,
            "age_known": age_known,
            "adjusted_psf": adjusted_psf,
            "txns": entry.get("transaction_count", 0),
        })

    if len(peers) < _MIN_PEERS:
        return None

    adjusted_median = median(p["adjusted_psf"] for p in peers)
    premium_pct = (subject_psf / adjusted_median - 1) * 100
    basis = (f"size_band:{band}" if band and band_used else "pooled")
    subject_slope = _local_slope_per_year(subject_age, subject_psf, tenure)

    result: dict[str, Any] = {
        "district": district,
        "region": region,
        "subject_psf": round(subject_psf),
        "subject_age_years": round(subject_age, 1),
        "peer_count": len(peers),
        "band_peer_count": band_used,
        "freehold_unadjusted_peer_count": freehold_unadjusted,
        "comparison_basis": basis,
        "age_adjusted_district_median_psf": round(adjusted_median),
        "premium_vs_age_adjusted_median_pct": round(premium_pct, 1),
        "psf_age_slope_per_year": round(subject_slope, 1),
        "note": (
            "Peers' transacted PSF normalized to the subject's age along the "
            "measured piecewise decay curve (~3%/yr to 10yr, plateau 10-15, "
            "~2.6%/yr 15-20, then slow drift"
            f"{'; freehold ×0.6' if _is_freehold(tenure) else ''}); local slope at "
            f"subject age ≈ ${subject_slope:.0f}/psf/yr"
            + (f", same-size band {band} for {band_used}/{len(peers)} peers" if band and band_used
               else ", project-pooled (size-mixed)")
            + ". Negative premium = cheap for its age."
        ),
    }

    # --- vs new launches: what should this unit cost given new-launch pricing? ---
    # Unknown-age freehold peers carry the subject's age as a placeholder, so
    # they must never masquerade as new launches for a young subject.
    new_launches = [p for p in peers if p["age_known"] and p["age"] <= _NEW_LAUNCH_MAX_AGE]
    if len(new_launches) >= _MIN_NEW_LAUNCH_PEERS:
        nl_median = median(p["raw_psf"] for p in new_launches)
        nl_median_age = median(p["age"] for p in new_launches)
        implied_fair = nl_median * _decay_factor(nl_median_age, subject_age, tenure)
        result["new_launch_median_psf"] = round(nl_median)
        result["new_launch_count"] = len(new_launches)
        if implied_fair > 0:
            result["implied_fair_psf_from_new_launch"] = round(implied_fair)
            result["premium_vs_new_launch_implied_pct"] = round(
                (subject_psf / implied_fair - 1) * 100, 1
            )

    return result


def relative_value_for_listing(scored: Any, ura_data: dict, current_year: int,
                               subject_is_ec: bool = False) -> Optional[dict]:
    """Convenience wrapper taking a ScoredListing."""
    if not scored.psf or not scored.built_year or not scored.district:
        return None
    age = max(0, current_year - scored.built_year)
    return compute_relative_value(
        subject_psf=scored.psf,
        subject_age=age,
        district=scored.district,
        tenure=scored.tenure,
        ura_data=ura_data,
        current_year=current_year,
        subject_sqft=scored.sqft,
        subject_name=getattr(scored, "project_name", None) or getattr(scored, "title", None),
        subject_is_ec=subject_is_ec,
    )
