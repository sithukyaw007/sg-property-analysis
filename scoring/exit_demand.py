"""The exit-demand lens — "always think about exit first: who buys from you,
why, at what price" (Eric Chiew's framework, distilled from 15 transcribed
videos), scored per project from URA resale history.

WHAT IT MEASURES. A project-level composite of the three Chiew criteria the
validation harness could test (validation/algos.py, exit_* entries):

  * pair record (weight .5) — Laplace-smoothed share of the project's pseudo
    repeat-sales pairs that exited at a profit. His #1 criterion, and the
    DE-CORRELATED core: rank rho vs psf_vs_dist is -0.06 on the DEV panel,
    and this whole lens correlates only +0.18 with stored score_1000 across
    the 5,078 live listings it can score — it ranks on an axis MMR does not
    see, which is the reason a second lens exists (scoring/lenses.py).
    External cross-check: rho +0.77 (n=21) against realsmart's true
    pct_profitable for the projects both sides cover.
  * family mix (weight .3) — share of resale stock at >= 900 sqft; penalises
    the 1-2BR "investor projects" he flags as exit-thin.
  * boutique flag (weight .2) — his worst combination: < 250 units AND
    freehold AND sub-750sqft median unit.

VALIDATION VERDICT — read before trusting a score. Measured 2026-08-01
(validation/run.py, ledger data/validation_results.csv): DEV rho +0.159
[+0.045,+0.255] significant; VAL +0.070 [-0.063,+0.232] not significant,
against a psf_vs_dist bar of +0.221/+0.066. It does NOT beat single-feature
cheapness in-split and nothing (bar included) is significant on VAL — the
lens is registered as NEUTRAL-AND-DECORRELATED, never as "better": its job
is to nominate exit records MMR structurally dislikes, not to outrank MMR.

WHAT IS DELIBERATELY LEFT OUT.
  * Recent-volume (his #2): measured null as a raw count (DEV -0.017,
    VAL +0.098, ns) and significantly INVERTED per unit (-0.092 DEV) —
    reported in components as context, paid no weight.
  * Listing scarcity (his #3): no historical ground truth exists to validate
    it, and our live listing counts come from a scrape scope that most
    recently covered OCR 3-4BR only — "listings per 100 units" off that book
    is systematically undercounted for 1-2BR-heavy projects. An unvalidatable
    number with a known bias does not belong in a scored composite.
  * Age-adjusted comparable psf (his #5): measured null (DEV +0.043, ns).

VERSIONING. Calibration lives HERE and nowhere else — never in config.py,
which is sha-hashed into score_version and would restamp all 9,029 stored
scores (see scoring/lenses.py VERSIONING). version() fingerprints this file;
bump _VERSION_PREFIX on intentional recalibration.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Optional

import backtest as bt
import backtest_ext as ext
from validation import panel

_VERSION_PREFIX = "exit-1.0"

# Nomination bar: top 10% of the scored book. Tighter than mmr's ~top-15%
# gate on purpose — the validated evidence is "neutral and de-correlated",
# not "superior", so the lens earns only its highest-conviction nominations.
GATE_PERCENTILE = 0.90

# The validated calibration — byte-for-byte the arithmetic of
# validation.algos.exit_demand_composite, pinned by a cross-check test
# (tests/test_exit_demand.py). Change them together or the lens ships
# something the harness never measured.
MIN_PAIRS = 3         # fewer pairs is an anecdote, not a record
PAIR_ALPHA = 2.0      # Laplace prior centered at 0.5 (NOT the 84% bull-market
                      # base rate; see validation/algos.py for why)
FAMILY_SQFT = 900
BOUTIQUE_UNITS = 250
BOUTIQUE_SQFT = 750
W_PAIR, W_FAMILY, W_BOUTIQUE = 0.5, 0.3, 0.2

# Project table cache. Built lazily on the first score() that survives the
# input guards — importing this module must stay free (poller imports the
# registry at startup), and a full build reads ~107k URA rows once (~2s).
_TABLE: Optional[dict] = None


def _build_table(txns=None, units=None) -> dict:
    """norm name -> {district -> project exit stats}, whole URA history.

    The live analog of validation.panel._attach_exit_demand with T = today:
    same pairing, same family/boutique inputs, no split. Keyed by normalized
    name with a per-district layer because URA project names can collide
    across districts and a wrong-district join would hand one project
    another's exit record.
    """
    # Condo panel + EC prints. Pairs, family share and volume are all keyed
    # per (project, district), so EC prints only ever build EC projects' exit
    # records — condo scores are unchanged. Caveat: the lens was validated on
    # the condo panel only; for an EC its record is evidence, not a tested signal.
    if txns is None:
        import glob as _glob
        txns = ext.load_with_floor(sorted(_glob.glob(bt.DATA_GLOB))
                                   + sorted(_glob.glob(bt.EC_DATA_GLOB)))
    units = panel._load_units() if units is None else units

    pair_pl = defaultdict(lambda: [0, 0])
    for p in panel.build_pairs(txns):
        pair_pl[(p["project"], p["district"])][
            0 if p["psf1"] > p["psf0"] else 1] += 1

    fam = defaultdict(lambda: [0, 0])
    sqfts = defaultdict(list)
    vol6 = defaultdict(int)
    tenure_counts = defaultdict(lambda: defaultdict(int))
    t_max = max((x["t"] for x in txns), default=0.0)
    for x in txns:
        if x["sale_type"] not in ("Resale", "Sub Sale"):
            continue
        k = (x["project"], x["district"])
        if x["sqft"]:
            fam[k][0] += 1
            if x["sqft"] >= FAMILY_SQFT:
                fam[k][1] += 1
            sqfts[k].append(x["sqft"])
        if x["t"] > t_max - 0.5:
            vol6[k] += 1
        if x["tenure"]:
            tenure_counts[k][x["tenure"]] += 1

    table: dict = {}
    for k in set(pair_pl) | set(fam):
        proj, dist = k
        n, f = fam.get(k, (0, 0))
        modal = (max(tenure_counts[k], key=tenure_counts[k].get)
                 if tenure_counts.get(k) else "")
        kind, _, _ = panel.parse_tenure(modal, t_max)
        p, u = pair_pl.get(k, (0, 0))
        table.setdefault(panel._pu_norm(proj), {})[dist] = {
            "project": proj, "district": dist,
            "pair_profit": p, "pair_loss": u,
            "family_share": (f / n) if n else None,
            "median_sqft": bt._median(sqfts.get(k) or []),
            "total_units": units.get(panel._pu_norm(proj)),
            "tenure_kind": kind,
            "resale_vol_6m": vol6.get(k, 0),
        }
    return table


def _table_lookup(name: str, district) -> Optional[dict]:
    global _TABLE
    if _TABLE is None:
        _TABLE = _build_table()
    entry = _TABLE.get(panel._pu_norm(name))
    if not entry:
        return None
    # Listing districts arrive as "D19"/"19"/19; URA's column is "19".
    d = str(district or "").upper().lstrip("D").lstrip("0")
    if d and d in {k.lstrip("0") for k in entry}:
        return next(v for k, v in entry.items() if k.lstrip("0") == d)
    # No usable district on the listing: only an unambiguous name may match.
    return next(iter(entry.values())) if len(entry) == 1 and not d else None


def _compose(stats: dict) -> Optional[dict]:
    """The validated composite over one project's stats, or None if thin."""
    p, u = stats["pair_profit"], stats["pair_loss"]
    fam = stats["family_share"]
    if p + u < MIN_PAIRS or fam is None:
        return None
    pair_record = (p + PAIR_ALPHA) / (p + u + 2 * PAIR_ALPHA)
    boutique_ok = 1.0
    if stats["total_units"] is not None and stats["median_sqft"] is not None:
        if (stats["total_units"] < BOUTIQUE_UNITS
                and stats["tenure_kind"] == "freehold"
                and stats["median_sqft"] < BOUTIQUE_SQFT):
            boutique_ok = 0.0
    score = 100.0 * (W_PAIR * pair_record + W_FAMILY * fam
                     + W_BOUTIQUE * boutique_ok)
    return {
        "score": round(score, 1),
        "components": {
            "pair_record": round(pair_record, 4),
            "pair_profit": p, "pair_loss": u,
            "family_share": round(fam, 4),
            "boutique_ok": boutique_ok,
            "total_units": stats["total_units"],
            "median_sqft": stats["median_sqft"],
            "tenure_kind": stats["tenure_kind"],
            # context only — measured null, carries no weight (module doc)
            "resale_vol_6m": stats["resale_vol_6m"],
            "project": stats["project"], "district": stats["district"],
        },
    }


def score(scored: Any, record: Optional[dict] = None) -> dict:
    """Lens entry point (scoring/lenses.py contract): {"score", "components"}.

    Scored from the listing's project identity alone — everything else is URA
    project history, so two listings in one project score identically. That is
    the design, not a shortcut: exit demand is a property of the project a
    buyer must eventually resell into, not of the unit's asking price. A
    listing whose project cannot be matched, or whose project has fewer than
    MIN_PAIRS matched exits, gets score None ("this lens cannot judge it"),
    never a guess.
    """
    name = (getattr(scored, "project_name", None)
            or (record or {}).get("project_name") or "").strip()
    if not name:
        return {"score": None, "components": {}}
    district = (getattr(scored, "district", None)
                or (record or {}).get("district"))
    stats = _table_lookup(name, district)
    if stats is None:
        return {"score": None, "components": {}}
    return _compose(stats) or {"score": None, "components": {}}


def version() -> str:
    # Late import: lenses.py imports this module to register the lens, and a
    # top-level import back into lenses would be a cycle.
    from scoring.lenses import module_fingerprint
    return module_fingerprint(__file__, _VERSION_PREFIX)
