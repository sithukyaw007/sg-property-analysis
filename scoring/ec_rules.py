"""Executive Condominium rules — where an EC is in its life, and who may buy it.

An EC is public housing until its Minimum Occupation Period (MOP) ends, then a
Citizen/PR-only resale market, then a fully private condo. The repo scored ECs
as if they were condos; this module adds the facts that decide whether an EC is
even buyable for a given household and how far it is from privatisation (the
milestone its resale price tends to re-rate on).

Rules come from data/ec_rules.json (dated, sourced) — policy, not calibration,
so it never touches config.py / score_version. Two regimes (MND, May 2026):

  * land tender closed before 8 May 2026 -> MOP 5 years, privatised after 10;
  * closed on/after 8 May 2026          -> MOP 10 years, privatised after 15.

Inputs are deliberately coarse and every proxy is labelled:
  * TOP year (the listing's built_year) stands in for key collection, which is
    when the MOP clock really starts — a unit in its boundary year is flagged;
  * the land award year comes from URA's "99 yrs lease commencing from YYYY".

Nothing here is a score. It is factual context and a hard eligibility gate.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date
from typing import Optional

_RULES_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "ec_rules.json")
_rules_cache: Optional[dict] = None

# A typical EC completes 3-4 years after the land award; used only when the
# listing carries no built (TOP) year.
TOP_YEARS_AFTER_AWARD = 4


def load_rules(path: str | None = None) -> dict:
    global _rules_cache
    if path:
        with open(path) as f:
            return json.load(f)
    if _rules_cache is None:
        with open(_RULES_FILE) as f:
            _rules_cache = json.load(f)
    return _rules_cache


def lease_start_year(tenure: str | None) -> int | None:
    """'99 yrs lease commencing from 2010' -> 2010 (the land award year)."""
    m = re.search(r"commencing\s+from\s+(\d{4})", tenure or "", re.I)
    return int(m.group(1)) if m else None


def _regime(award_year: int | None, rules: dict) -> tuple[dict | None, bool]:
    """(regime, certain). Only the award YEAR is known, so a 2026 award is
    ambiguous (the cut-off is 8 May 2026) unless the TOP year rules it out."""
    regimes = {("before" if "tender_closed_before" in r else "after"): r
               for r in rules["regimes"]}
    if award_year is None:
        return None, False
    cutoff = date.fromisoformat(regimes["before"]["tender_closed_before"]).year
    if award_year < cutoff:
        return regimes["before"], True
    if award_year > cutoff:
        return regimes["after"], True
    # Same year as the cut-off: conservative = the stricter (newer) regime.
    return regimes["after"], False


def ec_status(top_year: int | None = None, tenure: str | None = None,
              household_citizenship: str | None = None,
              today: date | None = None, rules: dict | None = None) -> dict:
    """Lifecycle stage and buyer eligibility for one EC.

    Returns a dict that is safe to drop into factual_data:
      stage: "new_or_within_mop" | "post_mop" | "privatised" | "unknown"
      regime, mop_years, privatised_after_years, regime_certain
      top_year (+ top_year_estimated), land_award_year, age_years
      mop_ends_year, privatised_year, years_to_privatisation
      boundary_year: True when this calendar year is the MOP/privatisation year
      eligible_citizenship: who may buy at this stage
      household_eligible: True/False for the given household (None if not given)
      as_of / sources: the rules' provenance
    """
    rules = rules or load_rules()
    today = today or date.today()
    award = lease_start_year(tenure)
    regime, certain = _regime(award, rules)

    top_est = False
    if not top_year and award:
        top_year, top_est = award + TOP_YEARS_AFTER_AWARD, True
    if regime is None and top_year:
        # No tender year: every EC completed so far was tendered under the old
        # regime (new-regime sites launch ~2027+, TOP ~2030+).
        regime, certain = _regime(top_year - TOP_YEARS_AFTER_AWARD, rules)
        certain = False

    out: dict = {
        "regime": regime["name"] if regime else None,
        "regime_certain": certain,
        "mop_years": regime["mop_years"] if regime else None,
        "privatised_after_years": regime["privatised_after_years"] if regime else None,
        "land_award_year": award,
        "top_year": top_year,
        "top_year_estimated": top_est,
        "as_of": rules.get("as_of"),
        "sources": rules.get("sources", []),
    }
    if not (regime and top_year):
        out.update(stage="unknown", eligible_citizenship=None, household_eligible=None,
                   boundary_year=False)
        return out

    mop_end = top_year + regime["mop_years"]
    priv = top_year + regime["privatised_after_years"]
    yr = today.year
    if yr < mop_end:
        stage = "new_or_within_mop"
    elif yr < priv:
        stage = "post_mop"
    else:
        stage = "privatised"
    elig = rules["eligible_household_citizenship"][stage]
    out.update(
        stage=stage,
        age_years=yr - top_year,
        mop_ends_year=mop_end,
        privatised_year=priv,
        years_to_privatisation=max(0, priv - yr),
        boundary_year=yr in (mop_end, priv),
        eligible_citizenship=list(elig),
        household_eligible=(household_citizenship in elig) if household_citizenship else None,
    )
    return out


def describe(status: dict) -> str:
    """One line for reports: 'post-MOP (privatises 2027, 1 yr) — SC/PR may buy'."""
    stage = status.get("stage")
    if stage == "unknown":
        return "EC stage unknown (no TOP or tenure year) — verify MOP/privatisation dates"
    label = {"new_or_within_mop": "within MOP", "post_mop": "post-MOP",
             "privatised": "privatised"}[stage]
    who = "/".join(status.get("eligible_citizenship") or [])
    tail = ""
    if stage != "privatised":
        tail = (f" (privatises {status['privatised_year']}, "
                f"{status['years_to_privatisation']} yr)")
    flag = " — boundary year, verify exact date" if status.get("boundary_year") else ""
    return f"{label}{tail} — {who} may buy{flag}"
