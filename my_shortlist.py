#!/usr/bin/env python3
"""My shortlist — own-stay-first candidate developments for the active buyer profile.

Read-only: no scraping, no agents. Joins what is already on disk — the
listings DB, URA project stats (condo AND EC), the exit-demand lens, cached
realsmart facts, EC rules, bedroom bands and the personal-fit lens — and
answers "which developments are worth researching for THIS household?".

Order of operations mirrors the rubric's own-stay rule (an intersection, not a
substitution): the EXIT screen runs first and can disqualify; livability and
personal fit only order the survivors.

  1. Scope    — active, recently seen, in the profile's districts and mandate
                (price, beds, condo/EC). EC identity comes from URA's records,
                not the portal label.
  2. Gates    — every rejection carries a reason: stale, incomplete, bedroom
                relabel, marketing headline, EC not buyable for this household,
                thin resale market, poor exit record.
  3. Exit tier — A proven / B adequate / C thin-or-unproven.
  4. Rank     — within a tier: 0.35 personal fit + 0.25 livability
                + 0.25 valuation + 0.15 exit record. score_1000 is shown for
                information only (it has not been shown to pick winners).

    python my_shortlist.py                      # markdown to stdout + files
    python my_shortlist.py --top 25 --json
    python my_shortlist.py --max-age-days 30    # only listings seen in 30 days
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from statistics import median

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import bed_bands  # noqa: E402
import buyer_profile  # noqa: E402
import listings_db  # noqa: E402

OUT_DIR = os.path.join(BASE, "output", "my_shortlist")

# Exit-screen thresholds. Policy for this list, not scoring calibration.
MIN_RESALES = 10            # 5-yr URA resale prints below this = no real resale market
ADEQUATE_RESALES = 30       # at or above: tier B possible
PROVEN_RESALES = 50         # at or above (with a strong record): tier A
# "Poor exit record" is judged on the RAW share of repeat sales that exited at a
# profit, and only with enough pairs to mean something: the lens's smoothed
# record shrinks small samples toward 50%, so even a perfect 5-for-5 would read
# 78%. The smoothed record is only used to AWARD tier A (where shrinkage is the
# conservative direction).
POOR_RAW_SHARE = ((20, 0.80), (10, 0.70))   # (min pairs, share below this = poor)
STRONG_PAIR_RECORD = 0.90   # smoothed record for tier A
REALSMART_TRUST = 150       # realsmart % profitable only counts past this many resales
REALSMART_POOR_PCT = 90.0
REALSMART_STRONG_PCT = 95.0
LOW_LEASE_YEARS = 60        # remaining lease below this now = flagged (financing/exit pool)
DEFAULT_MAX_AGE_DAYS = 45

WEIGHTS = {"personal_fit": 0.35, "livability": 0.25, "valuation": 0.25, "exit": 0.15}
TIER_ORDER = {"A": 0, "B": 1, "C": 2}


# --------------------------------------------------------------------------- #
# Data access (each helper degrades to None rather than raising)
# --------------------------------------------------------------------------- #
class Context:
    """Lazily-built shared lookups for one run."""

    def __init__(self):
        import invest
        from scoring.full_scorer import FullScorer
        self.scorer = FullScorer(ura_data=invest.load_ura_cache(), cohort_stats={})
        try:
            import realsmart
            import realsmart_cache
            self._rs_norm = realsmart.normalize
            self._rs = realsmart_cache.load_cache()
        except Exception:
            self._rs_norm, self._rs = None, {}
        try:
            import weekly
            self._known = weekly._known_projects()
            self._headline = weekly.looks_like_marketing_title
        except Exception:
            self._known, self._headline = set(), None
        from scoring import personal_fit
        self.pf = personal_fit
        self.pf_cache = personal_fit.load_cache()

    def is_ec(self, rec: dict) -> bool:
        from scoring.full_scorer import listing_is_ec
        return listing_is_ec(rec)

    def ura_entry(self, name: str, is_ec: bool) -> dict | None:
        self.scorer._current_is_ec = is_ec
        try:
            return self.scorer._fuzzy_ura_lookup((name or "").lower())
        finally:
            self.scorer._current_is_ec = False

    def realsmart(self, name: str) -> dict | None:
        if not self._rs_norm:
            return None
        hit = self._rs.get(self._rs_norm(name) or "")
        return hit if isinstance(hit, dict) and hit.get("ok") else None

    def headline(self, name: str) -> bool:
        return bool(self._headline and self._headline(name, self._known))


def exit_stats(name: str, district, ura: dict | None, rs: dict | None) -> dict:
    from scoring import exit_demand
    ed = exit_demand.score(None, {"project_name": name, "district": district})
    comps = ed.get("components") or {}
    profit, loss = comps.get("pair_profit") or 0, comps.get("pair_loss") or 0
    out = {
        "resales_5y": (ura or {}).get("resale_transaction_count"),
        "pair_record": comps.get("pair_record"),
        "pairs": profit + loss,
        "raw_share": (profit / (profit + loss)) if (profit + loss) else None,
        "exit_lens": ed.get("score"),
        "realsmart_pct_profitable": None,
        "realsmart_resales": None,
        "realscore": None,
    }
    if rs:
        n = rs.get("resale_txns") or ((rs.get("profitable_txns") or 0) + (rs.get("unprofitable_txns") or 0))
        out.update(realsmart_pct_profitable=rs.get("pct_profitable"),
                   realsmart_resales=int(n) if n else None, realscore=rs.get("realscore"))
    return out


def exit_tier(x: dict) -> tuple[str | None, str | None]:
    """(tier, rejection reason). Tier None means the project is rejected."""
    resales = x.get("resales_5y")
    pr, share, pairs = x.get("pair_record"), x.get("raw_share"), x.get("pairs") or 0
    rs_pct, rs_n = x.get("realsmart_pct_profitable"), x.get("realsmart_resales") or 0
    if share is not None and any(pairs >= n and share < cut for n, cut in POOR_RAW_SHARE):
        return None, f"poor exit record ({share:.0%} of {pairs} repeat sales at a profit)"
    if rs_pct is not None and rs_n >= REALSMART_TRUST and rs_pct < REALSMART_POOR_PCT:
        return None, f"poor exit record (realsmart {rs_pct:.0f}% profitable over {rs_n} resales)"
    if resales is not None and resales < MIN_RESALES:
        return None, f"thin resale market ({resales} URA resales in 5 years)"
    strong = ((pr is not None and pr >= STRONG_PAIR_RECORD)
              or (rs_pct is not None and rs_n >= REALSMART_TRUST and rs_pct >= REALSMART_STRONG_PCT))
    if resales is not None and resales >= PROVEN_RESALES and strong:
        return "A", None
    if resales is not None and resales >= ADEQUATE_RESALES:
        return "B", None
    return "C", None


def remaining_lease(tenure: str | None, today: date) -> int | None:
    from scoring.ec_rules import lease_start_year
    t = (tenure or "").lower()
    if "freehold" in t or "999" in t:
        return None
    start = lease_start_year(tenure)
    yrs = 99
    if "103" in t:
        yrs = 103
    return start + yrs - today.year if start else None


def upfront_taxes(price: int, profile) -> dict:
    from scoring.costs import CostCalculator
    c = CostCalculator()
    bsd = c.calculate_bsd(price)
    absd = c.calculate_absd(price, profile.buyer_type, profile.property_count)
    legal = int(c.params.get("legal_fee_buy", 3500))
    return {"bsd": bsd, "absd": absd, "legal": legal, "total": bsd + absd + legal}


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
def build(profile, records: list[dict], ctx: Context | None = None,
          max_age_days: int = DEFAULT_MAX_AGE_DAYS, today: date | None = None) -> dict:
    from scoring import ec_rules
    today = today or date.today()
    ctx = ctx or Context()
    seen_cutoff = (today - timedelta(days=max_age_days)).isoformat()

    rejected: list[dict] = []
    scope_out = Counter()
    by_project: dict = defaultdict(list)

    def reject(rec, reason, kind=None):
        rejected.append({"project": rec.get("project_name"), "district": rec.get("district"),
                         "price": rec.get("price"), "beds": rec.get("beds"), "sqft": rec.get("sqft"),
                         "kind": kind, "reason": reason, "url": rec.get("url")})

    for rec in records:
        if rec.get("status") != "active":
            scope_out["not active"] += 1
            continue
        if not profile.in_region(rec.get("district")):
            scope_out["outside profile districts"] += 1
            continue
        if not (rec.get("price") and rec.get("sqft") and rec.get("beds") is not None):
            scope_out["incomplete (price/sqft/beds)"] += 1
            continue
        if (rec.get("property_type") or "").strip().lower() not in (
                "condominium", "apartment", "executive condominium", "walk-up apartment"):
            scope_out["not a condo/EC format"] += 1
            continue
        kind = "ec" if ctx.is_ec(rec) else "condo"
        ptype = "Executive Condominium" if kind == "ec" else "Condominium"
        if not profile.mandate_for(rec["price"], rec["beds"], rec.get("district"), ptype):
            scope_out["outside mandate (price/beds/type)"] += 1
            continue
        # In scope from here on: every exclusion is a recorded rejection.
        if (rec.get("last_seen") or "") < seen_cutoff:
            reject(rec, f"not seen since {rec.get('last_seen')} (may be sold)", kind)
            continue
        name = (rec.get("project_name") or "").strip()
        if not name or ctx.headline(name):
            reject(rec, "no usable project name (listing headline)", kind)
            continue
        bb = bed_bands.check(name, rec["beds"], rec["sqft"])
        if bb.get("verdict") == "mismatch":
            reject(rec, f"bedroom relabel: {rec['sqft']:.0f} sqft looks like a "
                        f"{bb.get('looks_like')}BR in this project", kind)
            continue
        by_project[(name, rec.get("district"), kind)].append(rec)

    projects, proj_rejects = [], []
    for (name, district, kind), recs in by_project.items():
        ura = ctx.ura_entry(name, kind == "ec")
        flags: list[str] = []
        ec = None
        if kind == "ec":
            built = Counter(r.get("built_year") for r in recs if r.get("built_year")).most_common(1)
            ec = ec_rules.ec_status(top_year=built[0][0] if built else None,
                                    tenure=(ura or {}).get("tenure"),
                                    household_citizenship=profile.household_citizenship,
                                    today=today)
            if ec.get("household_eligible") is False:
                for r in recs:
                    reject(r, f"EC {ec['stage'].replace('_', ' ')}: only "
                              f"{'/'.join(ec['eligible_citizenship'])} households may buy "
                              f"(MOP ends {ec.get('mop_ends_year')})", kind)
                continue
            if ec.get("stage") == "unknown":
                flags.append("EC stage unknown — verify MOP/privatisation")
            elif ec.get("boundary_year"):
                flags.append("EC boundary year — verify exact MOP/privatisation date")
        ex = exit_stats(name, district, ura, ctx.realsmart(name))
        tier, why = exit_tier(ex)
        if tier is None:
            for r in recs:
                reject(r, why, kind)
            continue
        if ex["resales_5y"] is None:
            flags.append("no URA resale history (new or unmatched project)")
        if ex["raw_share"] is not None and ex["raw_share"] < 0.80:
            flags.append(f"weak exit record on few repeat sales "
                         f"({ex['raw_share']:.0%} of {ex['pairs']})")

        # One row per unit (multi-agent duplicates collapse to the cheapest ask).
        units: dict = {}
        for r in recs:
            k = r.get("unit_group") or r.get("id")
            if k not in units or (r.get("price") or 0) < (units[k].get("price") or 0):
                units[k] = r
        uniq = list(units.values())
        rep = max(uniq, key=lambda r: ((r.get("valuation_score") or 0), -(r.get("price") or 0)))

        fit = ctx.pf.personal_fit(name, district, listing=rep, profile=profile, cache=ctx.pf_cache)
        lease_left = remaining_lease((ura or {}).get("tenure"), today)
        if lease_left is not None and lease_left < LOW_LEASE_YEARS:
            flags.append(f"{lease_left} yrs lease left")
        liv = [r.get("livability_score") for r in uniq if r.get("livability_score") is not None]
        val = rep.get("valuation_score")
        exit_score = (ex["pair_record"] * 100) if ex["pair_record"] is not None else 50.0
        rank = (WEIGHTS["personal_fit"] * fit["score"]
                + WEIGHTS["livability"] * (median(liv) if liv else 50)
                + WEIGHTS["valuation"] * (val if val is not None else 50)
                + WEIGHTS["exit"] * exit_score)
        projects.append({
            "tier": tier,
            "rank_score": round(rank, 1),
            "project": name,
            "district": district,
            "kind": kind,
            "ec": ec,
            "listings": len(uniq),
            "ask_min": min(r["price"] for r in uniq),
            "ask_max": max(r["price"] for r in uniq),
            "rep": {k: rep.get(k) for k in ("id", "price", "psf", "sqft", "beds", "built_year",
                                            "floor_level", "url", "valuation_score",
                                            "livability_score", "score_1000")},
            "upfront_taxes": upfront_taxes(int(rep["price"]), profile),
            "livability_median": round(median(liv)) if liv else None,
            "personal_fit": fit,
            "exit": ex,
            "lease_left": lease_left,
            "tenure": (ura or {}).get("tenure"),
            "flags": flags,
        })

    projects.sort(key=lambda p: (TIER_ORDER[p["tier"]], -p["rank_score"]))
    return {
        "profile": profile.summary(),
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "max_age_days": max_age_days,
        "out_of_scope": dict(scope_out),
        "projects": projects,
        "rejected": rejected,
    }


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #
def _m(v) -> str:
    return f"${v / 1e6:.2f}M" if v else "—"


def _kind(p: dict) -> str:
    if p["kind"] != "ec":
        return "Condo"
    from scoring.ec_rules import describe
    return "EC · " + describe(p["ec"])


def _fit_txt(p: dict) -> str:
    f = p["personal_fit"]
    s = f.get("schools") or {}
    parts = []
    if s:
        r1 = next((k for k in s if k.startswith("within_") and k.endswith("m")), None)
        parts.append(f"{s.get(r1, 0)} pri ≤{r1[7:-1]}m" if r1 else "")
    c = f.get("commute") or {}
    if c.get("basis") == "onemap_pt":
        parts.append(f"{c['minutes']:.0f} min PT")
    elif c.get("basis") == "straight_line":
        parts.append(f"{c['km']} km (straight)")
    return f"{f['score']} ({', '.join(x for x in parts if x)})" if parts else str(f["score"])


def _exit_txt(p: dict) -> str:
    e = p["exit"]
    parts = [f"{e['resales_5y']} resales" if e["resales_5y"] is not None else "no URA resales"]
    if e["raw_share"] is not None:
        parts.append(f"{e['raw_share']:.0%} of {e['pairs']} exits profitable")
    if e["realsmart_pct_profitable"] is not None:
        parts.append(f"realsmart {e['realsmart_pct_profitable']:.0f}%/{e['realsmart_resales']}")
    return ", ".join(parts)


def render_md(res: dict, top: int) -> str:
    ps = res["projects"]
    L = [f"# My shortlist — {res['generated']}", "",
         f"**Profile:** {res['profile']}", "",
         f"{len(ps)} developments survive the exit screen "
         f"({sum(p['listings'] for p in ps)} unique listings); "
         f"{len(res['rejected'])} in-scope listings rejected with a reason. "
         f"Only listings seen in the last {res['max_age_days']} days are used.", "",
         "Exit tier first (A proven · B adequate · C thin/unproven), then own-stay rank "
         "(0.35 personal fit + 0.25 livability + 0.25 valuation + 0.15 exit record). "
         "`score_1000` is information only. Every candidate still needs "
         "`/analyze-development` or `/analyze-listing` before a viewing.", "",
         "| # | Tier | Development | D | Type | Asks (n) | Rep ask · sqft · psf | Val | Liv | "
         "Fit | Exit | Lease | PR taxes | score | Flags |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, p in enumerate(ps[:top], 1):
        r = p["rep"]
        L.append(
            f"| {i} | {p['tier']} | [{p['project']}]({r.get('url') or ''}) | {p['district']} | "
            f"{_kind(p)} | {_m(p['ask_min'])}–{_m(p['ask_max'])} ({p['listings']}) | "
            f"{_m(r['price'])} · {r['sqft']:.0f} · ${r['psf']:,.0f} | "
            f"{r.get('valuation_score') if r.get('valuation_score') is not None else '—'} | "
            f"{p['livability_median'] if p['livability_median'] is not None else '—'} | "
            f"{_fit_txt(p)} | {_exit_txt(p)} | "
            f"{p['lease_left'] if p['lease_left'] is not None else ('FH' if 'freehold' in (p['tenure'] or '').lower() else '—')} | "
            f"${p['upfront_taxes']['total'] / 1000:,.0f}k | {r.get('score_1000') or '—'} | "
            f"{'; '.join(p['flags']) or ''} |")
    L += ["", "## Rejected (in scope)", ""]
    reasons = Counter(_reason_key(x["reason"]) for x in res["rejected"])
    for reason, n in reasons.most_common():
        L.append(f"- **{reason}** — {n} listing(s)")
    by_proj = defaultdict(list)
    for x in res["rejected"]:
        by_proj[(x["project"], x["reason"])].append(x)
    L += ["", "<details><summary>Per development</summary>", ""]
    for (proj, reason), xs in sorted(by_proj.items(), key=lambda kv: -len(kv[1])):
        L.append(f"- {proj or '(no name)'} ({len(xs)}): {reason}")
    L += ["", "</details>", "", "## Out of scope", ""]
    for k, n in sorted(res["out_of_scope"].items(), key=lambda kv: -kv[1]):
        L.append(f"- {k}: {n}")
    L += ["", "## Caveats", "",
          "- School counts are straight-line from each development's centroid; confirm P1 "
          "distance on OneMap SchoolQuery for a specific block.",
          "- EC stages use TOP year as a proxy for key collection (when the MOP clock starts); "
          "verify the exact MOP date before committing.",
          "- Exit records are 2021–26 URA history (one rising market). They show who could "
          "exit without loss, not what the next five years will do.",
          "- Listings come from the scraped DB, so some may already be sold; confirm with "
          "the agent."]
    return "\n".join(L) + "\n"


def _reason_key(reason: str) -> str:
    for prefix in ("not seen since", "bedroom relabel", "poor exit record", "thin resale market",
                   "EC "):
        if reason.startswith(prefix):
            return {"EC ": "EC not buyable for this household"}.get(prefix, prefix)
    return reason


def write_outputs(res: dict, top: int) -> tuple[str, str]:
    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    md_path = os.path.join(OUT_DIR, f"{stamp}.md")
    csv_path = os.path.join(OUT_DIR, f"{stamp}.csv")
    with open(md_path, "w") as f:
        f.write(render_md(res, top))
    cols = ["rank", "tier", "rank_score", "project", "district", "kind", "ec_stage",
            "years_to_privatisation", "listings", "ask_min", "ask_max", "rep_price", "rep_sqft",
            "rep_psf", "valuation", "livability", "personal_fit", "schools_1km", "commute",
            "resales_5y", "pair_record", "realsmart_pct", "lease_left", "pr_upfront_taxes",
            "score_1000", "flags", "url"]
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for i, p in enumerate(res["projects"], 1):
            r, f_, e = p["rep"], p["personal_fit"], p["exit"]
            c = f_.get("commute") or {}
            w.writerow([i, p["tier"], p["rank_score"], p["project"], p["district"], p["kind"],
                        (p["ec"] or {}).get("stage"), (p["ec"] or {}).get("years_to_privatisation"),
                        p["listings"], p["ask_min"], p["ask_max"], r["price"], r["sqft"],
                        round(r["psf"] or 0), r.get("valuation_score"), p["livability_median"],
                        f_["score"], (f_.get("schools") or {}).get("within_1000m"),
                        c.get("minutes") or c.get("km"), e["resales_5y"], e["pair_record"],
                        e["realsmart_pct_profitable"], p["lease_left"],
                        p["upfront_taxes"]["total"], r.get("score_1000"), "; ".join(p["flags"]),
                        r.get("url")])
    return md_path, csv_path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS,
                    help="ignore listings not seen within this many days (default 45)")
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    ap.add_argument("--no-write", action="store_true", help="don't write output/my_shortlist/")
    a = ap.parse_args()
    try:
        profile = buyer_profile.active()
    except buyer_profile.ProfileError as e:
        print(f"profile error: {e}", file=sys.stderr)
        return 1
    if profile is None:
        print("No buyer profile. Run `python buyer_profile.py --init`, edit "
              "profile.local.json, then re-run.", file=sys.stderr)
        return 1
    records = list(listings_db.load_db()["listings"].values())
    ctx = Context()
    res = build(profile, records, ctx, max_age_days=a.max_age_days)
    ctx.pf.save_cache(ctx.pf_cache)
    if a.json:
        print(json.dumps(res, indent=1, default=str))
    else:
        print(render_md(res, a.top))
    if not a.no_write:
        md, cs = write_outputs(res, a.top)
        print(f"\nwrote {md}\n      {cs}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
