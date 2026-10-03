#!/usr/bin/env python3
"""Join what we know into one ranked buy-list.

Three sources, none sufficient alone:
  * eval memory   — the agent's researched verdict and reasoning
  * realsmart     — whether owners of that project have historically exited whole
  * listings DB   — the algo grade and the live ask

Ranks on EVIDENCE OF EXIT, not on the algo score. That ordering is deliberate:
across this batch the correlation between score_1000 and % of resales sold at a
profit came out around -0.7, i.e. the algo's favourites have historically been
the worst places to have been an owner. MMR rewards "cheap versus district
peers", and a project is often cheap against its peers precisely because the
market has learned it underperforms — so the score selects value traps. Until
that is properly measured across the full book, transaction history is the more
trustworthy sort key.

    python shortlist.py                 # everything evaluated since --since
    python shortlist.py --mandate his
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import listings_db
import realsmart
import realsmart_cache as rc
import weekly

# A project needs this many resales before its profit rate means anything.
# 100% across six sales is noise; 72.8% across 1,281 is a warning.
MIN_RESALES_FOR_TRUST = 150


def collect(since: str) -> list[dict]:
    """Every evaluation since `since`, joined to realsmart + the live listing."""
    cache = rc.load_cache()
    db = listings_db.load_db()["listings"]
    by_url = {r.get("url"): r for r in db.values() if r.get("url")}

    out: list[dict] = []
    for path in glob.glob(os.path.join(BASE, "evaluations", "*.json")):
        if path.endswith("index.json"):
            continue
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        for h in data.get("history") or []:
            if not isinstance(h, dict):
                continue
            if str(h.get("evaluated_at") or "") < since:
                continue
            url = (h.get("source") or {}).get("url")
            rec = by_url.get(url, {})
            as_of = h.get("as_of") if isinstance(h.get("as_of"), dict) else {}
            price = rec.get("price") or as_of.get("price")
            beds = rec.get("beds") or as_of.get("beds")
            district = rec.get("district")
            rs = cache.get(realsmart.normalize(data.get("condo") or "")) or {}
            out.append({
                "condo": data.get("condo"),
                "rating": (h.get("rating") or "").strip(),
                "confidence": h.get("confidence"),
                "summary": h.get("summary"),
                "rationale": h.get("rating_rationale"),
                "red_flags": h.get("red_flags") or [],
                "evaluated_at": h.get("evaluated_at"),
                "price": price, "beds": beds, "district": district,
                "sqft": rec.get("sqft") or as_of.get("sqft"),
                "score_1000": rec.get("score_1000"),
                # Which lenses currently vouch for the live listing (weekly's
                # gate is any-lens now, so "the algo likes it" is per-lens).
                "surfaced_by": weekly.lens_endorsements(rec) if rec else [],
                "url": url,
                "mandate": weekly.mandate_for(price, beds, district),
                "realscore": rs.get("realscore"),
                "pct_profitable": rs.get("pct_profitable"),
                "resale_txns": rs.get("resale_txns"),
                "avg_holding_yrs": rs.get("avg_holding_yrs"),
            })

    # One row per condo AND bed count — keep the newest verdict.
    #
    # Keying on the condo alone collapsed genuinely different products into one
    # row and let the newest verdict speak for all of them. Rivervale Crest's
    # 3BR at $1.268M (Neutral, 82% of 571 resales profitable) vanished behind a
    # 1,668 sqft penthouse marketed as a 4BR (Avoid), and Palm Gardens' 3BR
    # behind its mislabelled "4BR" — the buy list was hiding the better product
    # of the two. The bed count is what the mandates are written in terms of
    # (3BR<=$1.8M vs 3-4BR<=$2.5M) and what the re-eval cooldown already treats
    # as a distinct call, so it belongs in this key too.
    best: dict[tuple, dict] = {}
    for r in out:
        k = (r["condo"], r.get("beds"))
        if k not in best or (r["evaluated_at"] or "") >= (best[k]["evaluated_at"] or ""):
            best[k] = r
    return list(best.values())


_RATING_ORDER = {"strong buy": 0, "buy": 1, "neutral": 2, "avoid": 3}


def rank_key(r: dict):
    """Verdict first, then evidence of exit, then the algo grade last."""
    trusted = (r.get("resale_txns") or 0) >= MIN_RESALES_FOR_TRUST
    pct = r.get("pct_profitable")
    return (
        _RATING_ORDER.get((r.get("rating") or "").lower(), 9),
        -(pct if (pct is not None and trusted) else -1),
        -(r.get("realscore") or 0),
        -(r.get("score_1000") or 0),
    )


def render(rows: list[dict], mandate: str | None) -> str:
    rows = [r for r in rows if r.get("mandate")] if mandate is None else \
        [r for r in rows if r.get("mandate") == mandate]
    rows.sort(key=rank_key)

    L = ["| Verdict | For | Project | Type | Price | District | Algo | REALSCORE | Profitable |",
         "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        pct = r.get("pct_profitable")
        n = r.get("resale_txns")
        prof = "—" if pct is None else (
            f"{pct}% of {n}" + ("" if (n or 0) >= MIN_RESALES_FOR_TRUST else " ⚠thin"))
        price = f"${r['price']:,}" if r.get("price") else "—"
        L.append(
            f"| {r['rating'] or '?'} ({r.get('confidence') or '?'}) | "
            f"{(r.get('mandate') or '—').upper()} | {r['condo']} | "
            f"{r.get('beds') or '?'}BR {int(r['sqft']) if r.get('sqft') else '?'} sqft | "
            f"{price} | {r.get('district') or '—'} | {r.get('score_1000') or '—'} | "
            f"{r.get('realscore') or '—'} | {prof} |")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-07-28")
    ap.add_argument("--mandate", choices=[m[0] for m in weekly.MANDATES], default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rows = collect(args.since)
    if args.json:
        rows.sort(key=rank_key)
        print(json.dumps(rows, indent=1, ensure_ascii=False))
        return 0
    print(render(rows, args.mandate))
    return 0


if __name__ == "__main__":
    sys.exit(main())
