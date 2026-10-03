"""Generate raw analysis JSON for AI agent review.

Outputs a structured intermediate file with algo-computed scores and
empty agent fields for the AI to fill in during the review step.
"""

import json
import os
from datetime import datetime
from typing import Any, Optional

from scoring.models import ScoredListing
from utils.geo import normalize_district

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
_DISTRICT_PROFILES_FILE = os.path.join(_DATA_DIR, "district_profiles.json")
_DISTRICT_MEDIANS_FILE = os.path.join(_DATA_DIR, "district_medians.json")
_district_profiles_cache: Optional[dict] = None
_district_medians_cache: Optional[dict] = None


def _load_district_profiles() -> dict:
    global _district_profiles_cache
    if _district_profiles_cache is None:
        if os.path.exists(_DISTRICT_PROFILES_FILE):
            with open(_DISTRICT_PROFILES_FILE) as f:
                _district_profiles_cache = json.load(f)
        else:
            _district_profiles_cache = {"districts": {}}
    return _district_profiles_cache


def _load_district_medians() -> dict:
    global _district_medians_cache
    if _district_medians_cache is None:
        if os.path.exists(_DISTRICT_MEDIANS_FILE):
            with open(_DISTRICT_MEDIANS_FILE) as f:
                _district_medians_cache = json.load(f)
        else:
            _district_medians_cache = {"medians": {}}
    return _district_medians_cache


def _build_factual_data(listing: ScoredListing) -> dict:
    """Extract factual/computed data from a scored listing for AI evaluation.

    This separates objective facts (distances, rates, costs, dates) from
    the algorithm's opinionated point scores. The AI uses these facts to
    form its own evaluation.
    """
    facts: dict[str, Any] = {}

    # --- Rental data ---
    roi5 = listing.roi_5yr
    facts["rental"] = {
        "estimated_monthly_rent": round(listing.estimated_monthly_rent) if listing.estimated_monthly_rent else None,
        "gross_yield_pct": round(listing.estimated_gross_yield, 2) if listing.estimated_gross_yield else None,
        # Net of holding costs (pre income tax) — the carry the gross number hides
        "net_yield_pct": round(roi5.net_rental_yield, 2) if roi5 and roi5.net_rental_yield else None,
        "rent_source": listing.rent_source or None,
    }
    # Audit #5: rent evidence — auditable the same way as PSF comps (source,
    # URA contract depth, serving window, numeric confidence, sqft-cap flag).
    facts["rental"]["rent_evidence"] = {
        "source": listing.rent_source or None,
        "monthly_rent": round(listing.estimated_monthly_rent) if listing.estimated_monthly_rent else None,
        "contracts": getattr(listing, "rent_contracts", None),
        "window_months": getattr(listing, "rent_window_months", None),
        "confidence": getattr(listing, "rent_confidence", None),
        "capped": bool(getattr(listing, "rent_capped", False)),
    }
    if getattr(listing, "rent_capped", False):
        facts["rental"]["rent_evidence"]["capped_note"] = (
            "Rent sqft was capped at 1.25x the bed count's typical size — the "
            "strata sqft is oversized for the bed count (PES/patio/void or "
            "mis-scrape); verify the unit format before trusting the yield."
        )
    if (listing.rent_source or "").startswith("fallback"):
        facts["rental"]["warning"] = (
            "Rent is a generic fallback estimate (no district/condo data) — research "
            "actual asking rents before weighing the yield."
        )

    # --- Capital appreciation data ---
    appreciation: dict[str, Any] = {
        "annual_rate_pct": round(listing.appreciation_rate * 100, 2) if listing.appreciation_rate else None,
        "data_source": listing.appreciation_source,
        "needs_further_research": listing.needs_ai_appreciation,
    }
    sb_cap = listing.score_breakdown.get("capital_appreciation", {})
    if sb_cap:
        appreciation["transaction_count"] = sb_cap.get("appreciation_rate", {}).get("transaction_count")
        appreciation["data_confidence"] = sb_cap.get("appreciation_rate", {}).get("confidence")
        appreciation["momentum"] = sb_cap.get("momentum", {}).get("value")
        appreciation["momentum_data_coverage"] = sb_cap.get("momentum", {}).get("data_coverage")
        appreciation["psf_vs_district_median"] = sb_cap.get("psf_vs_median", {}).get("ratio")
        appreciation["district_median_psf"] = sb_cap.get("psf_vs_median", {}).get("median")
    if listing.appreciation_adjustment != 0:
        appreciation["raw_rate_before_bias_adjustment_pct"] = round(listing.raw_appreciation_rate * 100, 2)
        appreciation["bias_adjustment_pct"] = round(listing.appreciation_adjustment * 100, 2)
        appreciation["bias_adjustment_reason"] = listing.adjustment_reason
    facts["appreciation"] = appreciation

    # --- Future infrastructure ---
    future: dict[str, Any] = {"nearest_future_mrt": None, "government_zones": []}
    if listing.future_score_details:
        fd = listing.future_score_details
        if fd.nearest_future_mrt:
            future["nearest_future_mrt"] = {
                "station": fd.nearest_future_mrt,
                "distance_m": fd.future_mrt_distance_m,
                "line": fd.future_mrt_line,
            }
        future["government_zones"] = fd.govt_zones
    facts["future_infrastructure"] = future

    # --- Transaction liquidity ---
    sb_liq = listing.score_breakdown.get("liquidity", {})
    facts["liquidity"] = {
        "ura_transaction_count": sb_cap.get("appreciation_rate", {}).get("transaction_count") if sb_cap else None,
        "buyer_pool_depth": sb_liq.get("buyer_pool_depth", {}).get("depth") if sb_liq else None,
        "total_units": (sb_liq.get("dev_size", {}).get("total_units") if sb_liq else None) or listing.total_units,
    }

    # --- Red flags detected ---
    sb_flags = listing.score_breakdown.get("red_flags", {})
    facts["red_flags_detected"] = sb_flags.get("flags", [])

    # --- PSF overpricing check ---
    psf_premium = sb_flags.get("psf_premium_pct")
    if psf_premium is not None:
        facts["psf_premium_vs_ura_median_pct"] = round(psf_premium, 1)
    # Same-size cohort depth behind that premium (None = no size data). Surfaced so
    # the AI can judge how much to trust a small-unit "premium"/"discount", and so an
    # appreciation override in --from-review can carry the same cohort damping.
    cohort_txns = sb_flags.get("psf_cohort_txns")
    if cohort_txns is not None:
        facts["psf_cohort_txns"] = cohort_txns

    # --- Age-adjusted relative value vs district peers ---
    rel_value = listing.score_breakdown.get("relative_value")
    if rel_value:
        facts["relative_value"] = rel_value

    # --- Executive Condominium lifecycle (scoring/ec_rules.py) ---
    ec_status = listing.score_breakdown.get("ec_status")
    if ec_status:
        facts["ec_status"] = ec_status

    return facts


def _build_algo_breakdown(listing: ScoredListing) -> dict:
    """Build the algorithm's point-score breakdown (reference only).

    The AI should use factual_data for evaluation. This breakdown shows
    how the algorithm scored the property for reference/comparison.
    """
    result = {
        "total_score": round(listing.total_score, 1),
        "rental_yield": round(listing.rental_yield_score, 1),
        "capital_appreciation": round(listing.capital_appreciation_score, 1),
        "future_potential": round(listing.future_potential_score, 1),
        "liquidity": round(listing.liquidity_score, 1),
        "cost_efficiency": round(listing.cost_efficiency_score, 1),
        "red_flag_deductions": round(listing.red_flag_deductions, 1),
    }
    if listing.mmr is not None:
        result["mmr"] = round(listing.mmr, 1)
        result["score_1000"] = listing.score_1000
        result["mmr_components"] = listing.mmr_components
        result["mmr_note"] = (
            "MMR: continuous uncapped rating, base 1500; score_1000 is its 0-1000 "
            "normalization (500 = market-typical). Larger spread than the legacy /100 score."
        )
    return result


def _build_roi_projections(listing: ScoredListing) -> dict:
    """Build ROI projection data."""
    projections = {}
    for years, roi in [(5, listing.roi_5yr), (6, listing.roi_6yr), (7, listing.roi_7yr)]:
        if roi:
            projections[f"{years}yr"] = {
                "exit_price": roi.estimated_exit_price,
                "total_return": round(roi.total_return),
                "roi_pct": round(roi.roi_percent, 2),
                "annualized_roi": round(roi.annualized_roi, 2),
            }
    if projections:
        # v3.5b: the model basis was previously undisclosed and easy to misread
        # as a leveraged equity return. Audit #11 + income-tax fix (Jun 2026):
        # entry costs are now deducted from the return and net letting profit
        # is taxed — surface both assumptions.
        first_roi = next((r for r in (listing.roi_5yr, listing.roi_6yr, listing.roi_7yr) if r), None)
        tax_rate = getattr(first_roi, "marginal_income_tax_rate", 0) if first_roi else 0
        projections["basis"] = (
            "ALL-CASH (no mortgage): returns are on full purchase price + entry "
            "costs (BSD/ABSD/legal — deducted from the return, not just the "
            "denominator). Rent compounds at ~2%/yr with rent-linked holding "
            f"costs scaled to match; net letting profit is taxed at {tax_rate:.0%} "
            "marginal income tax. Appreciation rate decays 3%/yr toward mean. "
            "A 75% LTV buyer's equity returns scale very differently — do not "
            "compare these numbers to leveraged ROI."
        )
        if first_roi:
            projections["assumptions"] = {
                "marginal_income_tax_rate": tax_rate,
                "entry_costs": getattr(first_roi, "entry_costs", 0),
                "ltv": getattr(first_roi, "ltv", 0),
            }
    return projections


def _build_context(listing: ScoredListing, ura_data: dict) -> dict:
    """Build extra context for AI consumption."""
    context: dict[str, Any] = {}

    # District info
    district_str = (listing.district or "").upper().replace("D", "").strip()
    try:
        d_num = int(district_str)
    except ValueError:
        d_num = None

    # Region mapping
    ccr = {1, 2, 6, 7, 9, 10, 11}
    rcr = {3, 4, 5, 8, 12, 13, 14, 15}
    if d_num:
        if d_num in ccr:
            context["region"] = "CCR"
        elif d_num in rcr:
            context["region"] = "RCR"
        else:
            context["region"] = "OCR"
    else:
        context["region"] = None

    # District appreciation and medians (profile data)
    district_code = normalize_district(listing.district or "")
    profiles = _load_district_profiles().get("districts", {})
    d_key = district_code.replace("D", "").lstrip("0") or "0" if district_code else ""
    profile = profiles.get(d_key, {}) if d_key else {}
    context["district_appreciation"] = profile.get("historical_appreciation")

    # District median PSF from score breakdown or medians fallback
    sb_cap = listing.score_breakdown.get("capital_appreciation", {})
    median_psf = sb_cap.get("psf_vs_median", {}).get("median")
    if median_psf is None and district_code:
        medians = _load_district_medians().get("medians", {})
        median_psf = medians.get(district_code, {}).get("psf")
    context["district_median_psf"] = median_psf

    # New launch bias flag
    context["new_launch_bias"] = listing.appreciation_adjustment != 0

    # Sample projects from URA cache (district proximity unknown)
    sample = []
    project_lower = (listing.project_name or listing.title or "").lower()
    for key, entry in ura_data.items():
        if key == project_lower:
            continue
        entry_name = entry.get("project_name", key)
        sample.append(entry_name)
        if len(sample) >= 5:
            break
    if sample:
        context["sample_projects_in_ura"] = sample
        context["sample_projects_note"] = "Sample of projects in URA cache; district proximity unknown."

    return context


def _build_stack_profile(listing: ScoredListing) -> Optional[dict]:
    """Join a listing to its stack/layout in the researched condo profile.

    Returns the matched stack (facing/view/flags + development known-issues), or a
    {"status": "not_researched"} signal when the condo has no profile yet — so the
    analyze/scan flows know to run /research-development. Fully optional and
    defensive: never raises into the scoring path.
    """
    name = listing.project_name or listing.title or ""
    if not name:
        return None
    try:
        import profile_memory
        prof = profile_memory.find_profile(
            name, district=getattr(listing, "district", None))
        if not prof:
            return {
                "status": "not_researched",
                "hint": (f"no profile for '{name}' — run /research-development to add "
                         "stacks/facings/layouts (the 'perfect info' join)"),
            }
        out = profile_memory.match_stack(
            {"sqft": listing.sqft, "beds": listing.beds,
             "facing": listing.facing, "floor_level": listing.floor_level}, prof) or {}
        out["profile_confidence"] = prof.get("confidence")
        issues = (prof.get("development") or {}).get("known_issues")
        if issues:
            out["development_known_issues"] = issues
        if not out.get("matched"):
            out["status"] = "profile_exists_no_unit_match"
        return out or None
    except Exception:
        return None  # profile store is optional — never block scoring on it


def _listing_to_raw(
    listing: ScoredListing,
    rank: int,
    ura_data: dict,
) -> dict:
    """Convert a single ScoredListing to the raw analysis format.

    Structure: property basics → factual data for AI → ROI projections →
    context → algo reference scores → agent fields to fill.
    """
    # --- Property basics ---
    entry: dict[str, Any] = {
        "rank": rank,
        "title": listing.title,
        "project_name": listing.project_name or listing.title,
        "url": listing.url,
        "price": listing.price,
        "psf": listing.psf,
        "sqft": listing.sqft,
        "beds": listing.beds,
        "district": listing.district,
        "tenure": listing.tenure,
        "remaining_lease": listing.remaining_lease,
        "built_year": listing.built_year,
        "total_units": listing.total_units,
    }

    # MRT info
    if listing.nearest_mrt:
        entry["nearest_mrt"] = listing.nearest_mrt
        if listing.mrt_distance_m:
            entry["nearest_mrt_distance_m"] = listing.mrt_distance_m
    elif listing.mrt_distance_m:
        entry["nearest_mrt_distance_m"] = listing.mrt_distance_m

    # Floor/facing info
    if listing.floor_level:
        entry["floor_level"] = listing.floor_level
    if listing.facing:
        entry["facing"] = listing.facing

    # Stack profile join — "perfect info" about this unit's stack/facing/view from
    # the researched condo profile (profiles/), matched by sqft (+ beds/facing).
    # When no profile exists yet, signal it so the flow can /research-development.
    _sp = _build_stack_profile(listing)
    if _sp:
        entry["stack_profile"] = _sp

    # --- Factual computed data (for AI evaluation) ---
    entry["factual_data"] = _build_factual_data(listing)

    # --- ROI projections (factual math given inputs) ---
    entry["roi_projections"] = _build_roi_projections(listing)
    if listing.roi_sensitivity:
        entry["roi_sensitivity"] = listing.roi_sensitivity

    # --- District/market context ---
    entry["context"] = _build_context(listing, ura_data)

    # --- Algorithm reference scores (for comparison, not authoritative) ---
    entry["algo_reference"] = _build_algo_breakdown(listing)

    # Additional listing URLs (same condo, different units)
    if listing.additional_urls:
        entry["additional_urls"] = listing.additional_urls

    # Condo mode: per-unit-type variant data
    if listing.unit_variants:
        entry["unit_variants"] = listing.unit_variants
    if listing.unit_summary:
        entry["unit_summary"] = listing.unit_summary

    # --- Agent evaluation fields (to be filled by AI) ---
    entry["agent_evaluation"] = {
        "summary": None,
        "rating": None,
        "rating_rationale": None,
        "red_flags": [],
        "catalysts": [],
        "rental_assessment": None,
        "appreciation_assessment": None,
        "stack_notes": None,
        "confidence": None,
        "appreciation_rate_override_pct": None,
        "appreciation_rate_source": None,
        # realsmart.sg project-level facts (owner-preferred source). Structured
        # rather than buried in prose so scans can surface them in a column.
        "realscore": None,
        "realsmart_pct_profitable": None,
        "realsmart_annual_return_pct": None,
    }

    return entry


def generate_raw_analysis(
    scored: list[ScoredListing],
    ura_data: dict,
    run_config: Optional[dict] = None,
) -> dict:
    """Generate the raw analysis JSON structure for AI agent review.

    Args:
        scored: List of scored listings (sorted by score descending).
        ura_data: URA cache data dict.
        run_config: Optional dict with districts, price_range, beds, etc.

    Returns:
        Complete raw analysis dict ready to be written as JSON.
    """
    config = run_config or {}
    result: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(),
        "run_config": config,
        "report_level": {
            "agent_market_commentary": None,
            "agent_executive_summary": None,
            "agent_methodology_notes": None,
        },
        "listings": [],
    }

    # Add AI review instructions — adapt to the *altitude* of the analysis.
    condo_name = config.get("condo")
    has_url = bool(config.get("url"))
    has_variants = any(getattr(l, "unit_variants", None) for l in scored)

    # Determine evaluation mode:
    #   development   -> a single project (by name or project page): judge the
    #                    development as a whole + best stacks/facings/units.
    #   single_listing-> one specific unit/listing: a clean Buy/Neutral/Avoid call.
    #   market_scan   -> many candidates across districts: per-listing Buy/Neutral/Avoid.
    if condo_name or has_variants:
        mode = "development"
    elif has_url and len(scored) == 1:
        mode = "single_listing"
    else:
        mode = "market_scan"

    mode_notes = {
        "development": (
            "DEVELOPMENT-LEVEL ANALYSIS. The question is whether this DEVELOPMENT is good "
            "to invest in. Assess the project as a whole, then go granular: which STACKS, "
            "FACINGS and FLOORS will perform best for investment, and which to avoid. Each "
            "entry here is one unit type (bedroom count) — rate each, and give an overall "
            "development verdict in the executive summary. Put stack/facing/floor guidance "
            "in stack_notes. Still give a Buy/Neutral/Avoid rating per unit type."
        ),
        "single_listing": (
            "INDIVIDUAL LISTING. The question is: should I buy THIS specific unit? Give a "
            "clear Buy / Neutral / Avoid rating with confidence, grounded in this unit's "
            "price, PSF vs the development, floor, facing, and the development's fundamentals."
        ),
        "market_scan": (
            "MARKET SCAN across many candidates. For each shortlisted listing give a clear "
            "Buy / Neutral / Avoid rating with confidence, so the user can compare. Order the "
            "executive summary by rating strength — and if nothing merits a Buy, say so plainly; "
            "'no buys in this batch' is a valid and useful outcome."
        ),
    }

    steps_by_mode = {
        "development": [
            f"1. Web search '{condo_name or 'the project'} Singapore review' — build quality, developer, defects",
            f"2. Web search '{condo_name or 'the project'} price trend / transactions' — recent resale PSF and direction",
            f"3. Web search '{condo_name or 'the project'} floor plan site plan facing' — identify best/worst stacks, facings, pool/road-facing units",
            "4. Judge the DEVELOPMENT as a whole (location, tenure, size, liquidity, catalysts)",
            "5. For each unit type, set a rating and put stack/facing/floor guidance in stack_notes",
            "6. Optionally call the technical scorer (invest.py --score) with your researched numbers to sanity-check",
            "7. Fill report_level: executive summary = overall development verdict + best units to target",
        ],
        "single_listing": [
            "1. Web search the project for reviews, build quality, developer reputation",
            "2. Web search recent transactions to judge if THIS unit's price/PSF is fair vs the development",
            "3. Assess this unit's floor, facing, and stack relative to the best in the project",
            "4. Review factual_data — appreciation source, yield, red flags",
            "5. Give a clear Buy / Neutral / Avoid rating with confidence and rationale",
            "6. Fill report_level executive summary with the one-line verdict",
        ],
        "market_scan": [
            "1. Web search project reviews, issues, transaction history — cover ALL shortlisted listings, not just the algo's top-ranked (its ordering is an input, not the answer)",
            "2. Research area trends, upcoming developments, supply, and market conditions",
            "3. For each, assess unit quality and stack information where known",
            "4. Review factual_data per listing — appreciation reliability, yield, red flags",
            "5. Give each a Buy / Neutral / Avoid rating with confidence",
            "6. Fill report_level: executive summary ordered by rating strength ('no buys' is a valid outcome)",
        ],
    }

    result["ai_review_instructions"] = {
        "mode": mode,
        "purpose_check": (
            "All computed metrics (yield, ROI, liquidity, MMR) assume an INVESTMENT purpose "
            "(5-7yr hold). If the user's request suggests OWN-STAY — or is ambiguous between "
            "the two — ask the user before rating. Own-stay shifts the rubric: livability, "
            "layout, facing, noise, schools and commute outweigh yield and exit liquidity."
        ),
        "note": (
            "YOU are the evaluator. factual_data holds objective metrics; algo_reference is the "
            "algorithm's technical score (one input, not the answer). You can also call the "
            "technical scorer directly: `python invest.py --score '{...key value-drivers...}'`. "
            "Form your own rating from the data + web research. See CLAUDE.md for the full rubric. "
            "Anti-anchoring: form your view from factual_data and your research FIRST; consult "
            "algo_reference and past evaluations to pressure-test it, not to start from it. "
            "Research lower-ranked listings too — the algo ordering is an input, not a shortlist. "
            + mode_notes[mode]
        ),
        "forward_signal_priors": (
            "Backtest-calibrated (v3.5, full 28-district URA panel — see "
            "docs/evaluation-rubric.md 'What the data actually predicts'): the robust forward "
            "signals are CHEAP-VS-DISTRICT-PEERS (factual_data.relative_value, "
            "psf_premium_vs_ura_median_pct), REGION (realized forward OCR +3.7% > RCR +3.1% "
            "≫ CCR +1.9% — the OPPOSITE of old CCR-leads priors), and district buyer-pool "
            "depth. Trailing appreciation_rate has little forward power (~+0.03 marginal) and "
            "momentum is unstable across samples — use them for the STORY/catalyst, not as the "
            "forward number. Freehold is NOT a forward edge (it does carry a ~5% PSF level "
            "premium — already in the price, not a return driver). The future-infrastructure "
            "score has NO measured forward power — treat 'transformation upside' as narrative, "
            "not edge. Absolute low PSF is mostly the region effect — don't double-count it. "
            "The model explains <10% of forward variance: small score_1000 gaps are noise — "
            "reserve HIGH confidence for decisive physical/catalyst evidence, treat near-ties "
            "as Neutral."
        ),
        "data_trust": (
            "Extreme metrics are VERIFY-FIRST signals, not findings (v3.6/v3.6.1): in a "
            "scraped DB the deepest 'discounts' and fattest 'yields' are usually artifacts — "
            "mis-scraped sqft/beds, strata/dual-key/combined formats benchmarked against "
            "apartment medians, stale or bait prices. A discount beyond ~-25% vs comps, a "
            "gross yield above ~5.5%, or psf_cohort_txns=0 behind either ⇒ verify the ask "
            "against the project's own 12-month URA prints and confirm the unit format/sqft "
            "before crediting it. The scorer damps these channels, but the displayed "
            "gross_yield_pct and premium percentages are raw."
        ),
        "steps": steps_by_mode[mode],
        "agent_field_guide": {
            "summary": "2-3 sentence investment thesis"
                       + (" for this development" if mode == "development" else " for this unit"),
            "rating": "Strong Buy / Buy / Neutral / Avoid  (Avoid = no-buy)",
            "rating_rationale": "Key reasons for this rating (what makes it good or bad)",
            "red_flags": "List of specific risks from your research",
            "catalysts": "List of specific growth drivers from your research",
            "rental_assessment": "Rental demand outlook for this unit type and area",
            "appreciation_assessment": "Price trajectory view based on data and research",
            "stack_notes": ("Best/worst STACKS, FACINGS and FLOORS for investment, and which to avoid"
                            if mode == "development" else "This unit's stack/floor/facing vs the development's best"),
            "confidence": ("high/medium/low. Anchor to evidence, not the score: HIGH only when "
                           "physical/catalyst facts are decisive (model R²<0.10 — score gaps are noise)."),
            "appreciation_rate_override_pct": ("Your researched forward rate ONLY if backed by resale-to-resale "
                                               "evidence + a dated catalyst (e.g. 4.5 = 4.5%/yr). Scored at face "
                                               "value (confidence 1.0) but still cohort-damped; don't re-introduce "
                                               "new-launch CAGR inflation. Leave null to keep the data rate."),
            "appreciation_rate_source": "Source for your override rate (resale-to-resale basis)",
            "realscore": ("realsmart.sg's 0-5 profitability rank for the PROJECT, from "
                          "https://realsmart.sg/p/<slug> (public page, plain fetch, no login). "
                          "A high REALSCORE means few or no unprofitable resale transactions — "
                          "i.e. how reliably owners have exited in the black. Read it as "
                          "DOWNSIDE evidence (has this project ever lost people money?), not as "
                          "an appreciation forecast: it is backward-looking and, like trailing "
                          "CAGR, carries little forward power on its own. A low score on a deep "
                          "transaction history is a real warning; a high score on a thin one is "
                          "close to meaningless — always report it alongside the transaction "
                          "count you saw."),
            "realsmart_pct_profitable": "% of that project's resale transactions sold at a profit (number, e.g. 100)",
            "realsmart_annual_return_pct": "realsmart's avg annualized profit, past 1y (number, e.g. 5.4)",
        },
        "before_you_start": (
            "Check past evaluations first: `python invest.py --recall \"<condo>\"`. "
            "Treat any prior rating as a fallible reference — re-verify the current price and conditions."
        ),
    }
    if condo_name:
        result["ai_review_instructions"]["condo_name"] = condo_name

    for rank, listing in enumerate(scored, 1):
        entry = _listing_to_raw(listing, rank, ura_data)
        result["listings"].append(entry)

    return result


def save_raw_analysis(
    scored: list[ScoredListing],
    output_path: str,
    ura_data: dict,
    run_config: Optional[dict] = None,
) -> str:
    """Generate and save raw analysis JSON to file.

    Returns:
        Path to saved file.
    """
    data = generate_raw_analysis(scored, ura_data, run_config)
    with open(output_path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return output_path


def load_reviewed_analysis(path: str) -> tuple[dict, list[dict]]:
    """Load a reviewed analysis JSON (after AI fills in agent fields).

    Returns:
        Tuple of (report_level dict, list of listing dicts with agent fields).
    """
    with open(path) as f:
        data = json.load(f)

    report_level = data.get("report_level", {})
    listings = data.get("listings", [])
    return report_level, listings
