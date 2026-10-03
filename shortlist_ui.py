#!/usr/bin/env python3
"""Shortlist UI — the researched buy-list, one page, on 127.0.0.1:8644.

    python shortlist_ui.py            # then open http://127.0.0.1:8644

A viewer, not a pipeline: it reads what the scans already produced (eval memory
+ realsmart cache + listings DB via shortlist.collect) and renders it. Nothing
here scrapes, scores or spends an agent run, so it is safe to leave open and
refresh.

THE ONE THING THIS PAGE EXISTS TO SAY
-------------------------------------
Across this batch the algo grade (score_1000) and the share of a project's
resales that actually sold at a profit run OPPOSITE ways (r ~ -0.7). MMR rewards
"cheap versus district peers", and a project is often cheap precisely because the
market has learned it underperforms. So a high algo score sitting next to a poor
profitability record is a VALUE TRAP.

That pairing is *computed* into one READ chip per row — TRAP / HOLDS / WEAK /
UNPROVEN — and the page opens with the two or three projects actually worth
driving to this weekend. Everything else is in service of that shortlist.

DELIBERATELY NOT HERE
---------------------
An earlier pass added a scatter plot, a five-box KPI strip, seven filters, six
sort orders and a rank-divergence metric. One reader, seventeen rows: none of it
changed which condo he drives to, and all of it sat between him and the answer.
The rule now is that a thing earns its pixels only if removing it would change a
viewing decision. When in doubt, cut it — the numbers are all still in
/api/shortlist.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import statistics
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import bed_bands
import realsmart
import shortlist

DEFAULT_PORT = 8644
DEFAULT_SINCE = "2026-07-28"

# Below this many resales a profit percentage is decoration, not evidence.
THIN = shortlist.MIN_RESALES_FOR_TRUST

# Record bands. 95% is the "clean exit book" line the shortlist already used;
# under 90% roughly one owner in ten took a loss, which no algo score buys back.
CLEAN_PCT = 95.0
MIXED_PCT = 90.0

# The buyer's underwriting window (5-7yr hold). A project whose owners
# historically needed materially longer to get out is a liquidity warning.
HOLD_YEARS_PLAN = 7.0

_VERDICT_CLASS = {"strong buy": "buy", "buy": "buy",
                  "neutral": "neutral", "avoid": "avoid"}

_READ_LABEL = {"trap": "VALUE TRAP", "holds": "RECORD HOLDS",
               "weak": "WEAK RECORD", "unproven": "UNPROVEN"}


# ---------------------------------------------------------------- derivations

def record_band(r: dict) -> str:
    """clean | mixed | poor | thin | none — what the exit record is worth.

    `thin` and `none` are deliberately NOT folded into `poor`: "we don't know"
    and "we know it's bad" lead to different next actions.
    """
    pct = r.get("pct_profitable")
    n = r.get("resale_txns") or 0
    if pct is None:
        return "none"
    if n < THIN:
        return "thin"
    if pct >= CLEAN_PCT:
        return "clean"
    if pct >= MIXED_PCT:
        return "mixed"
    return "poor"


def algo_cutoff(rows: list[dict]) -> float:
    """The line between "the algo likes it" and "it doesn't".

    Deliberately the batch median, not a fixed number: every row here already
    clears the repo's own 650 "recommended tier", so an absolute threshold would
    call all fourteen algo favourites and split nothing. The median is printed on
    the page so the split is never mysterious.
    """
    scores = [r["score_1000"] for r in rows if r.get("score_1000")]
    return float(statistics.median(scores)) if scores else 0.0


def classify(r: dict, cut: float) -> str:
    """trap | holds | weak | unproven — the algo/record pairing, decided.

    The whole point of the page: `trap` is a project the algo puts in the top
    half of the batch while its owners' actual exits say otherwise.
    """
    band = record_band(r)
    if band == "clean":
        return "holds"
    if band in ("poor", "mixed"):
        return "trap" if (r.get("score_1000") or 0) >= cut else "weak"
    return "unproven"


def read_blurb(r: dict, cut: float) -> str:
    """One plain sentence explaining a READ chip — used as its tooltip."""
    kind = classify(r, cut)
    pct, n, algo = r.get("pct_profitable"), r.get("resale_txns"), r.get("score_1000")
    if kind == "trap":
        return (f"Value trap: algo {algo} is in the top half of this batch "
                f"(median {cut:.0f}) but only {_pct(pct)}% of {n} resales sold at a "
                f"profit. Cheap versus peers because the market has learned it "
                f"underperforms.")
    if kind == "holds":
        return (f"The exit record holds up: {_pct(pct)}% of {n} resales sold at a "
                f"profit. Algo {algo} — worth a viewing whatever the algo says.")
    if kind == "weak":
        return (f"Weak record: only {_pct(pct)}% of {n} resales sold at a profit, and "
                f"algo {algo} is below this batch's median too — nothing here "
                f"disagrees.")
    if pct is not None:
        return (f"Unproven: {_pct(pct)}% looks clean but rests on only {n} resales; "
                f"under {THIN} the number is noise.")
    return "Unproven: no resale profitability record for this project at all."


def bed_check(r: dict) -> dict:
    """Per-project bedroom sanity, guarded — a viewer must not die on bad data."""
    try:
        return bed_bands.check(r.get("condo"), r.get("beds"), r.get("sqft")) or {}
    except Exception:  # noqa: BLE001
        return {}


def pearson(xs, ys):
    """Correlation, or None when there is not enough spread to claim one."""
    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = sum((x - mx) ** 2 for x in xs)
    sy = sum((y - my) ** 2 for y in ys)
    if sx <= 0 or sy <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy) ** 0.5


def visit_list(rows: list[dict], limit: int = 3) -> list[dict]:
    """The projects actually worth driving to — the page's reason to exist.

    Gate, in order: not an Avoid; a trustworthy CLEAN exit record; and the
    bedroom count survives the per-project band check (a "3BR" that measures as a
    2BR fails the buyer's screen outright, however good the project is).
    """
    out = [r for r in rows
           if (r.get("rating") or "").lower() != "avoid"
           and record_band(r) == "clean"
           and bed_check(r).get("verdict") != "mismatch"]
    out.sort(key=lambda r: (-(r.get("pct_profitable") or 0),
                            -(r.get("resale_txns") or 0)))
    return out[:limit]


def blocked_by_beds(rows: list[dict]) -> list[dict]:
    """Clean record, not an Avoid — but the bedroom count fails its own band."""
    return [r for r in rows
            if record_band(r) == "clean"
            and (r.get("rating") or "").lower() != "avoid"
            and bed_check(r).get("verdict") == "mismatch"]


def _mandate_label(label: str, max_price: int, beds) -> str:
    bed_txt = "-".join(str(b) for b in sorted(beds))
    return f"{label.upper()} · {bed_txt}BR ≤${max_price / 1e6:.1f}M"


# Derived from weekly.MANDATES (the owner's his/hers, or a fork's profile).
MANDATE_LABEL = {label: _mandate_label(label, price, beds)
                 for label, price, beds, _ in shortlist.weekly.MANDATES}


def mandate_coverage(rows: list[dict], picks: list[dict], cut: float) -> list[dict]:
    """Per buyer: how many researched, how many survive to a viewing, and why not.

    Two buyers share one search, so a combined shortlist can quietly leave one of
    them with nothing to do on Saturday. Stating it per mandate is the difference
    between "we have three to see" and "he has none".
    """
    out = []
    for m in MANDATE_LABEL:
        in_m = [r for r in rows if r.get("mandate") == m]
        if not in_m:
            continue
        got = [r for r in picks if r.get("mandate") == m]
        chosen = {id(r) for r in got}
        reasons = {}
        for r in in_m:
            if id(r) in chosen:
                continue
            if bed_check(r).get("verdict") == "mismatch" and record_band(r) == "clean":
                key = "bed-count mislabel"
            elif (r.get("rating") or "").lower() == "avoid":
                key = "rated Avoid"
            else:
                key = {"trap": "value trap", "weak": "weak record",
                       "unproven": "unproven"}.get(classify(r, cut), "screened out")
            reasons[key] = reasons.get(key, 0) + 1
        out.append({"mandate": m, "label": MANDATE_LABEL[m], "researched": len(in_m),
                    "picks": got, "reasons": reasons})
    return out


# --------------------------------------------------------------- html helpers

def _e(x) -> str:
    return html.escape(str(x if x is not None else ""))


def _money(v) -> str:
    if not isinstance(v, (int, float)):
        return "—"
    return f"${v / 1e6:.2f}M" if v >= 1e6 else f"${v:,.0f}"


def _pct(v) -> str:
    """100.0 -> '100', 98.6 -> '98.6' — trailing .0 is noise in a dense table."""
    return f"{v:g}" if isinstance(v, (int, float)) else "?"


def _psf(r: dict):
    p, s = r.get("price"), r.get("sqft")
    if isinstance(p, (int, float)) and isinstance(s, (int, float)) and s:
        return p / s
    return None


def _spec(r: dict) -> str:
    """district · beds · size · psf — the one line that identifies a unit."""
    psf = _psf(r)
    return (f'{_e(r.get("district") or "")} · {_e(r.get("beds") or "?")}BR · '
            f'{int(r["sqft"]) if r.get("sqft") else "?"} sqft'
            + (f" · ${psf:,.0f} psf" if psf else ""))


def _flag_html(f) -> str:
    """Bold the lead clause of a red flag so fourteen of them can be skimmed.

    Split only — never rewritten. Most flags are already written head-first
    ("realsmart: only 71.5% profitable — 235 of 819 exits sold at a LOSS"), so
    promoting the head turns a wall of prose into a list without touching words.
    """
    s = str(f).strip()
    head, rest = s, ""
    for sep in (" — ", " – ", " - ", ": "):
        h, found, t = s.partition(sep)
        if found and t and 8 <= len(h) <= 110:
            head, rest = h, t
            break
    if rest:
        return (f"<li><b>{_e(head)}</b> "
                f'<span class="dim">{_e(rest)}</span></li>')
    # No lead clause to promote. A short flag still reads as a headline; a long
    # one set entirely in bold would be a wall, which is what we came to fix.
    return (f"<li><b>{_e(s)}</b></li>" if len(s) <= 90 else f"<li>{_e(s)}</li>")


def _profit_cell(r: dict) -> str:
    """The exit record: rate, sample size, and the loss count in bodies.

    A depth badge fires only for THIN. "100% of 6" must never look like "98% of
    600" — but "of 603 resales" already says that on its own, so DEEP/OK badges
    were decoration on the rows that needed no warning at all.
    """
    pct, n = r.get("pct_profitable"), r.get("resale_txns")
    if pct is None:
        return ('<span class="dim">no realsmart record</span>'
                '<div class="dim sub">nothing to trust or distrust</div>')
    band = record_band(r)
    # The meter is anchored at 60-100%, not 0-100: every project sits inside that
    # band, so a full-range bar renders them all as near-identical full bars and
    # hides the only difference that matters.
    fill = max(0.0, min(1.0, (pct - 60) / 40)) * 100
    cls = {"clean": "good", "mixed": "mid", "thin": "thin"}.get(band, "bad")
    lost = round((n or 0) * (100 - pct) / 100)
    thin = '<span class="depth">THIN</span>' if band == "thin" else ""
    return (f'<div class="meter"><i class="{cls}" style="width:{fill:.0f}%"></i></div>'
            f'<div class="pct">{_pct(pct)}%'
            f'<span class="dim"> of {n if n is not None else "?"} resales</span>'
            f"{thin}</div>"
            + (f'<div class="dim sub">{lost:,} owner{"s" if lost != 1 else ""} '
               f"sold at a loss</div>" if n else ""))


def _bed_chip(r: dict) -> str:
    """The mislabel, or a contested size. `oversize` is explicitly not a wrong
    bed count, and `undersize` matches no smaller band either — neither fails his
    screen, so neither is worth a chip.

    `contested` is the softer sibling: the size passes its own band but fits a
    different one better, which is usually why the psf looks cheap. It is drawn
    differently because it is a question, not a finding — the unit may well be
    what it says it is, in a project whose rental filings are simply coarse."""
    chk = bed_check(r)
    if chk.get("verdict") == "mismatch":
        return (f'<span class="chip" title="{_e(chk.get("reason") or "")}">'
                f'⚠ really a {_e(chk.get("looks_like"))}BR</span>')
    if (con := chk.get("contested")):
        return (f'<span class="chip q" title="{_e(con.get("reason") or "")}">'
                f'? size fits {_e(con.get("looks_like"))}BR better</span>')
    return ""


def _lens_chip(r: dict) -> str:
    """Which lenses currently vouch for this listing.

    Shown because "the algo likes it" stopped being one fact when the gate went
    multi-lens. A row endorsed only by `mmr` is the old signal — cheap versus
    district peers, the one shortlist.py's header warns correlates about -0.7
    with actually exiting whole. A row a SECOND lens vouches for is a different
    and better-supported claim, and the reader cannot tell them apart from the
    algo number alone. Nothing renders while mmr is the only registered lens:
    a chip that is always identical is noise.
    """
    endorsed = r.get("surfaced_by") or []
    if not endorsed or endorsed == ["mmr"]:
        return ""
    return "".join(
        f'<span class="chip lens" title="{_e(name)} vouches for this listing">'
        f'{_e(name)}</span>' for name in endorsed)


def _facts(r: dict) -> str:
    """The two or three numbers that a viewing decision needs and a column can't
    afford. Anything already printed on the row (psf, resale count) is not
    repeated here, and a second composite grade (REALSCORE) is exactly the kind
    of number this page argues against trusting."""
    out = ""
    if r.get("evaluated_at"):
        out += f'<span class="fact">evaluated <b>{_e(r["evaluated_at"])}</b></span>'
    hold = r.get("avg_holding_yrs")
    if isinstance(hold, (int, float)) and hold > HOLD_YEARS_PLAN:
        out += (f'<span class="fact warnfact">owners needed <b>{hold} yrs</b> on '
                f"average to get out — the plan underwrites 5-7</span>")
    band = bed_check(r).get("band")
    if band:
        out += (f'<span class="fact">URA rental filings put this project\'s '
                f'{_e(r.get("beds"))}BR at <b>{band["lo"]}-{band["hi"]} sqft</b> '
                f'({band["contracts"]} contracts)</span>')
    return f'<div class="facts">{out}</div>' if out else ""


# -------------------------------------------------------------------- the row

def _row_html(r: dict, i: int, cut: float) -> str:
    v = (r.get("rating") or "?").strip()
    vcls = _VERDICT_CLASS.get(v.lower(), "neutral")
    kind = classify(r, cut)
    rs_url = realsmart.url_for(r.get("condo") or "")[0]
    flags = list(r.get("red_flags") or [])

    detail = ""
    if r.get("summary") or r.get("rationale") or flags:
        head = "".join(_flag_html(f) for f in flags[:4])
        tail = "".join(_flag_html(f) for f in flags[4:])
        detail = (
            f'<tr class="detail" id="d{i}"><td colspan="8"><div class="dbox">'
            + _facts(r)
            + (f'<p class="sum">{_e(r["summary"])}</p>' if r.get("summary") else "")
            + ((f'<h4>Red flags <span class="dim">({len(flags)})</span></h4>'
                f'<ul class="flags">{head}</ul>')
               + (f"<details><summary>{len(flags) - 4} more red flags</summary>"
                  f'<ul class="flags">{tail}</ul></details>' if tail else "")
               if flags else "")
            + (f"<details><summary>Why this rating (long)</summary>"
               f'<p class="why">{_e(r["rationale"])}</p></details>'
               if r.get("rationale") else "")
            + "</div></td></tr>")

    links = (f'<a href="{_e(r.get("url"))}" target="_blank" rel="noopener">listing ↗</a>'
             if r.get("url") else '<span class="dim">no listing</span>')
    links += f'<a href="{_e(rs_url)}" target="_blank" rel="noopener">realsmart ↗</a>'

    return (
        f'<tr class="r read-{kind}" id="r{i}" data-i="{i}" data-rank="{i}" '
        f'data-mandate="{_e(r.get("mandate") or "")}" '
        f'data-price="{r.get("price") or 0}" onclick="tog({i})">'
        f'<td class="idx">{i + 1}</td>'
        f'<td><span class="read {kind}" title="{_e(read_blurb(r, cut))}">'
        f"{_READ_LABEL[kind]}</span></td>"
        f'<td><span class="verdict {vcls}">{_e(v)}</span></td>'
        f'<td class="name">{_e(r.get("condo"))} {_bed_chip(r)}{_lens_chip(r)}'
        f'<div class="sub"><span class="tag '
        f'{"his" if r.get("mandate") == "his" else "hers"}">'
        f'{_e((r.get("mandate") or "—").upper())}</span> '
        f'<span class="dim">{_spec(r)}</span></div></td>'
        f'<td class="num">{_money(r.get("price"))}</td>'
        f'<td class="num algo">{_e(r.get("score_1000") or "—")}</td>'
        f'<td class="prof">{_profit_cell(r)}</td>'
        f'<td class="links" onclick="event.stopPropagation()">{links}</td>'
        f"</tr>" + detail)


# ------------------------------------------------------------------- the page

_CSS = """
:root { --bg:#0f1419; --panel:#1a2129; --line:#2b3543; --text:#dce3ea;
        --dim:#8b98a5; --gold:#e3b341; --green:#3fb950; --red:#f85149; --blue:#58a6ff; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text);
       font:14px/1.45 -apple-system,"Segoe UI",Helvetica,Arial,sans-serif; }
a { color:var(--blue); }
header { padding:20px 24px 0; }
h1 { margin:0 0 6px; font-size:20px; letter-spacing:-.01em; }
h4 { margin:14px 0 6px; font-size:11px; text-transform:uppercase; letter-spacing:.07em;
     color:var(--dim); font-weight:600; }
.sub, .dim { color:var(--dim); }
.sub { font-size:11.5px; }
.note { margin:0; color:var(--dim); font-size:12.5px; max-width:96ch; }
.note b { color:var(--text); }

/* the call ---------------------------------------------------------------- */
.call { margin:16px 24px 0; background:var(--panel); border:1px solid var(--line);
        border-left:3px solid var(--gold); border-radius:8px; padding:16px 18px; }
.call.hasbuy { border-left-color:var(--green); }
.callh { font-size:19px; font-weight:600; margin:0 0 6px; }
.callp { margin:0; color:var(--dim); font-size:13px; max-width:96ch; }
.callp b { color:var(--text); }
.cards { display:flex; gap:10px; flex-wrap:wrap; }
.card { flex:1 1 250px; background:#141b23; border:1px solid var(--line);
        border-left:3px solid var(--green); border-radius:6px; padding:11px 13px;
        cursor:pointer; }
.card:hover { background:#18212b; }
.cardh { font-weight:600; font-size:15px; display:flex;
         justify-content:space-between; gap:8px; }
.cardn { color:var(--dim); font-variant-numeric:tabular-nums; }
.cardm { font-size:13px; margin-top:5px; color:var(--green); }
.covs { margin:14px 0 0; display:flex; flex-direction:column; gap:6px; }
.cov { font-size:12.5px; color:var(--dim); border-left:2px solid var(--line);
       padding:2px 0 2px 9px; }
.cov.has { border-left-color:var(--green); }
.cov.none { border-left-color:var(--red); }
.cov b { color:var(--text); }
.covgot { color:var(--green); }
.covnone { color:var(--red); font-weight:600; }
.blocked { margin:12px 0 0; font-size:12.5px; color:var(--dim); max-width:110ch;
           border-top:1px dashed var(--line); padding-top:10px; }
.blocked b { color:var(--red); }

/* controls ---------------------------------------------------------------- */
.bar { position:sticky; top:0; z-index:5; background:var(--bg);
       border-bottom:1px solid var(--line); margin:20px 0 0; padding:10px 24px;
       display:flex; gap:8px 18px; flex-wrap:wrap; align-items:center;
       font-size:12px; color:var(--dim); }
.grp { display:flex; gap:6px; align-items:center; }
.grp b { font-weight:600; text-transform:uppercase; letter-spacing:.06em;
         font-size:10.5px; margin-right:2px; }
.bar button { background:var(--panel); color:var(--dim); border:1px solid var(--line);
              border-radius:5px; padding:6px 13px; font-size:12.5px; cursor:pointer; }
.bar button:hover { color:var(--text); }
.bar button.on { background:#25303c; color:var(--text); border-color:#48586a; }
#count { margin-left:auto; font-variant-numeric:tabular-nums; }

/* table ------------------------------------------------------------------- */
.wrap { margin:0 24px 40px; overflow-x:auto; }
table { border-collapse:collapse; width:100%; min-width:980px; }
th { text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.06em;
     color:var(--dim); font-weight:600; padding:12px 10px 8px; white-space:nowrap; }
td { border-top:1px solid var(--line); padding:13px 10px; vertical-align:top; }
tr.r { cursor:pointer; }
tr.r:hover td { background:#151c24; }
tr.r td:first-child { border-left:3px solid transparent; }
tr.r.read-trap td:first-child { border-left-color:var(--red); }
tr.r.read-holds td:first-child { border-left-color:var(--green); }
tr.r.read-weak td:first-child { border-left-color:var(--gold); }
tr.r.flash td { background:#1e2a36; }
.idx { color:var(--dim); font-variant-numeric:tabular-nums; font-size:12px;
       width:36px; padding-left:12px; }
.name { font-weight:600; font-size:15px; }
.num { text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums;
       font-size:15px; }
th.num { font-size:11px; }
.algo { color:var(--dim); }
.read { display:inline-block; padding:4px 9px; border-radius:4px; font-size:11px;
        font-weight:700; letter-spacing:.04em; white-space:nowrap; cursor:help; }
.read.trap { background:rgba(248,81,73,.16); color:var(--red);
             box-shadow:inset 0 0 0 1px rgba(248,81,73,.45); }
.read.holds { background:rgba(63,185,80,.16); color:var(--green);
              box-shadow:inset 0 0 0 1px rgba(63,185,80,.45); }
.read.weak { background:rgba(227,179,65,.14); color:var(--gold); }
.read.unproven { background:#222b35; color:var(--dim); }
.verdict { display:inline-block; padding:3px 10px; border-radius:99px;
           font-size:12px; font-weight:600; }
.verdict.buy { background:rgba(63,185,80,.15); color:var(--green); }
.verdict.neutral { background:rgba(227,179,65,.14); color:var(--gold); }
.verdict.avoid { background:rgba(248,81,73,.14); color:var(--red); }
.tag { font-size:10px; padding:1px 6px; border-radius:4px; border:1px solid var(--line);
       color:var(--dim); }
.tag.his { border-color:#2f5d8a; color:var(--blue); }
.tag.hers { border-color:#6b4a86; color:#c08fe8; }
.chip { display:inline-block; font-size:10.5px; padding:2px 7px; border-radius:4px;
        margin-left:4px; font-weight:700; vertical-align:2px; cursor:help;
        background:rgba(248,81,73,.16); color:var(--red); }
/* A contested size is a question, not a finding — amber, not red, so it reads
   as "check this" rather than "this is wrong". */
.chip.q { background:rgba(210,153,34,.16); color:#d29922; }
/* A second lens vouching is good news, and the only chip here that is. */
.chip.lens { background:rgba(63,185,80,.16); color:var(--green); font-weight:600; }
.prof { min-width:215px; }
.meter { height:5px; background:#243040; border-radius:99px; overflow:hidden;
         max-width:190px; }
.meter i { display:block; height:100%; border-radius:99px; }
.meter .good { background:var(--green); }
.meter .mid { background:var(--gold); }
.meter .bad { background:var(--red); }
.meter .thin { background:#4a5764; }
.pct { font-size:14px; margin-top:5px; font-variant-numeric:tabular-nums; }
.depth { font-size:10px; font-weight:700; letter-spacing:.05em; margin-left:7px;
         padding:1px 6px; border-radius:3px; background:rgba(248,81,73,.16);
         color:var(--red); }
.links a { display:block; color:var(--blue); text-decoration:none; font-size:12.5px;
           white-space:nowrap; }
.links a:hover { text-decoration:underline; }

/* row detail -------------------------------------------------------------- */
tr.detail { display:none; }
tr.detail.open { display:table-row; }
.dbox { background:#141b23; border-left:2px solid var(--line); padding:14px 18px 16px;
        margin:2px 0 8px; border-radius:0 6px 6px 0; }
.dbox p { margin:0; }
.sum { color:var(--text); font-size:13.5px; line-height:1.6; max-width:100ch;
       margin-top:10px !important; }
.why { color:var(--dim); font-size:12.5px; line-height:1.65; max-width:100ch;
       margin-top:6px !important; }
.facts { display:flex; flex-wrap:wrap; gap:6px; }
.fact { font-size:11px; color:var(--dim); background:#1a2129; border:1px solid var(--line);
        border-radius:4px; padding:3px 8px; }
.fact b { color:var(--text); font-variant-numeric:tabular-nums; }
.warnfact { border-color:rgba(227,179,65,.4); color:var(--gold); }
.warnfact b { color:var(--gold); }
ul.flags { margin:4px 0 0; padding:0; list-style:none; }
ul.flags li { font-size:12.5px; line-height:1.55; padding:5px 0 5px 12px;
              max-width:114ch; border-left:2px solid #2b3543; margin-bottom:5px; }
ul.flags li b { color:var(--text); font-weight:600; }
details { margin-top:10px; }
details summary { cursor:pointer; color:var(--blue); font-size:12px; }
footer { margin:0 24px 40px; color:var(--dim); font-size:12px; max-width:100ch; }
"""

_JS = """
var ROWS = [], WHO = '';
function tog(i) {
  var d = document.getElementById('d' + i);
  if (d) d.classList.toggle('open');
}
function focusRow(i) {
  var tr = document.getElementById('r' + i);
  if (!tr) return;
  if (WHO && tr.dataset.mandate !== WHO) who('');   // never scroll to a hidden row
  var d = document.getElementById('d' + i);
  if (d) d.classList.add('open');
  tr.classList.add('flash');
  tr.scrollIntoView({block: 'center'});
  setTimeout(function () { tr.classList.remove('flash'); }, 1600);
}
function press(sel, key, val) {
  var b = document.querySelectorAll(sel);
  for (var i = 0; i < b.length; i++)
    b[i].className = (b[i].getAttribute(key) === val) ? 'on' : '';
}
function who(m) {
  WHO = m;
  press('#who button', 'data-m', m);
  var n = 0;
  for (var i = 0; i < ROWS.length; i++) {
    var tr = ROWS[i], ok = !m || tr.dataset.mandate === m;
    tr.style.display = ok ? '' : 'none';
    var d = document.getElementById('d' + tr.dataset.i);
    if (d && !ok) d.classList.remove('open');
    if (ok) n++;
  }
  var c = document.getElementById('count');
  if (c) c.textContent = n + ' of ' + ROWS.length + ' shown';
}
/* Two orders only. "evidence" is the server's own ranking (verdict, then exit
   record, then algo last); "price" is the one re-order a budget actually asks
   for. Both are "smaller key first", so a missing price parks at the bottom
   rather than at the top where it would look cheap. */
function sortBy(k) {
  press('#sort button', 'data-k', k);
  var tb = document.getElementById('tb');
  if (!tb) return;
  var arr = ROWS.slice();
  arr.sort(function (a, b) {
    return (k === 'price'
            ? (+a.dataset.price || 1e12) - (+b.dataset.price || 1e12)
            : a.dataset.rank - b.dataset.rank);
  });
  for (var i = 0; i < arr.length; i++) {
    tb.appendChild(arr[i]);
    var d = document.getElementById('d' + arr[i].dataset.i);
    if (d) tb.appendChild(d);
  }
}
document.addEventListener('DOMContentLoaded', function () {
  ROWS = Array.prototype.slice.call(document.querySelectorAll('#tb tr.r'));
  who('');
});
"""


def call_panel(rows: list[dict], cut: float, stats: str) -> str:
    """The empty state done honestly — and the actual "go see these" list.

    0-of-17-rated-Buy is a real finding about the market, not a rendering bug, so
    it gets stated in words. Underneath it sits the only thing the owner asked
    for: which two or three to physically visit, chosen on the exit record.
    """
    n_buy = sum(1 for r in rows
                if (r.get("rating") or "").lower() in ("buy", "strong buy"))
    n_avoid = sum(1 for r in rows if (r.get("rating") or "").lower() == "avoid")
    n_neutral = len(rows) - n_buy - n_avoid
    picks, blocked = visit_list(rows), blocked_by_beds(rows)

    if not rows:
        # Distinct from "nothing clears the bar": there is nothing to clear it.
        return ('<div class="call"><div class="callh">Nothing researched in this '
                'window.</div><p class="callp">No evaluation lands on or after the '
                '<code>--since</code> date, so there is nothing to rank. Widen '
                '<code>--since</code>, or run a scan first — this page only ever '
                'reads what the scans already produced.</p></div>')

    if n_buy:
        headline = f"{n_buy} listing{'s' if n_buy != 1 else ''} clears the Buy bar."
        lede = (f"{n_buy} Buy · {n_neutral} Neutral · {n_avoid} Avoid across "
                f"{len(rows)} researched in-mandate listings. {stats}")
        cls = "call hasbuy"
    else:
        headline = "Nothing clears the Buy bar."
        lede = (f"0 Buy · {n_neutral} Neutral · {n_avoid} Avoid across {len(rows)} "
                f"researched in-mandate listings. <b>That is the finding, not a "
                f"broken page:</b> nothing in this batch is mispriced enough to be "
                f"an edge, so treat what follows as a viewing list, not a buy list. "
                f"{stats}")
        cls = "call"

    if picks:
        cards = []
        idx = {id(r): i for i, r in enumerate(rows)}
        for r in picks:
            i = idx[id(r)]
            cards.append(
                f'<div class="card" onclick="focusRow({i})">'
                f'<div class="cardh"><span>{i + 1}. {_e(r.get("condo"))}</span>'
                f'<span class="cardn">{_money(r.get("price"))}</span></div>'
                f'<div class="sub"><span class="tag '
                f'{"his" if r.get("mandate") == "his" else "hers"}">'
                f'{_e((r.get("mandate") or "—").upper())}</span> {_spec(r)}</div>'
                f'<div class="cardm">{_pct(r.get("pct_profitable"))}% of '
                f'{r.get("resale_txns")} resales sold at a profit</div></div>')
        picks_html = ("<h4>Go and see these — ranked on exit record, not on algo "
                      "score</h4>"
                      f'<div class="cards">{"".join(cards)}</div>')
    else:
        picks_html = ('<h4>Go and see these</h4><p class="callp">Nothing here has '
                      "both a trustworthy clean exit record and a bedroom count that "
                      "survives its own project's size bands. Nothing is worth the "
                      "drive on this data.</p>")

    cov_rows = []
    for c in mandate_coverage(rows, picks, cut):
        why = ", ".join(f"{v} {k}" for k, v in sorted(c["reasons"].items(),
                                                      key=lambda kv: -kv[1]))
        got = len(c["picks"])
        names = ", ".join(_e(r.get("condo")) for r in c["picks"])
        cov_rows.append(
            f'<div class="cov {"has" if got else "none"}">'
            f'<b>{_e(c["label"])}</b> — '
            + (f'<span class="covgot">{got} to view: {names}</span>'
               if got else '<span class="covnone">nothing to view</span>')
            + f'<span class="dim"> · {c["researched"]} researched'
            + (f" ({why})" if why else "") + "</span></div>")
    cov_html = f'<div class="covs">{"".join(cov_rows)}</div>' if cov_rows else ""

    blocked_html = ""
    if blocked:
        items = "; ".join(
            f'<b>{_e(r.get("condo"))}</b> ({_pct(r.get("pct_profitable"))}% of '
            f'{r.get("resale_txns")}) — its "{_e(r.get("beds"))}BR" measures '
            f'{int(r["sqft"]) if r.get("sqft") else "?"} sqft, which URA rental '
            f"filings put in this project's "
            f'{_e(bed_check(r).get("looks_like"))}BR band'
            for r in blocked)
        blocked_html = (f'<p class="blocked">Kept off the shortlist despite a clean '
                        f"record: {items}. A mislabelled bedroom count fails the "
                        f"screen outright — it is the wrong product to resell into "
                        f"the family-buyer bid that is the whole exit plan.</p>")

    return (f'<div class="{cls}"><div class="callh">{headline}</div>'
            f'<p class="callp">{lede}</p>{picks_html}{cov_html}{blocked_html}</div>')


def render(rows: list[dict], since: str) -> str:
    rows = [r for r in rows if r.get("mandate")]
    rows.sort(key=shortlist.rank_key)
    cut = algo_cutoff(rows)

    trusted = [r for r in rows if record_band(r) in ("clean", "mixed", "poor")]
    clean = sum(1 for r in trusted if record_band(r) == "clean")
    traps = sum(1 for r in rows if classify(r, cut) == "trap")
    corr_rows = [r for r in trusted if r.get("score_1000")]
    r_corr = pearson([float(r["score_1000"]) for r in corr_rows],
                     [float(r["pct_profitable"]) for r in corr_rows])

    # The old five-box KPI strip said all of this and repeated the headline
    # twice; as one sentence inside the call it is read rather than scanned past.
    stats = (f"Of the {len(trusted)} with a trustworthy resale record, "
             f"<b>{clean} {'is' if clean == 1 else 'are'} clean</b> and "
             f"<b>{traps} {'is a value trap' if traps == 1 else 'are value traps'}"
             f"</b>."
             if trusted else "No project here has a trustworthy resale record.")

    body = "".join(_row_html(r, i, cut) for i, r in enumerate(rows))
    if not body:
        body = ('<tr><td colspan="8" class="dim" style="padding:28px">Nothing '
                "evaluated in this window. Run a scan, or widen --since.</td></tr>")

    # No parenthetical at all when there is too little spread to claim an r —
    # an empty "(  )" would read as a rendering bug rather than as honesty.
    r_txt = "" if r_corr is None else f" (r &asymp; {r_corr:+.2f})"
    mandate_buttons = "\n    ".join(
        f'<button data-m="{_e(m)}" onclick="who(\'{_e(m)}\')">'
        f'{_e(lbl.split(" · ", 1)[0].lower())} &middot; '
        f'{_e(lbl.split(" · ", 1)[1]).replace("≤", "&le;")}</button>'
        for m, lbl in MANDATE_LABEL.items())
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:,">
<title>Condo shortlist — Property Finder</title>
<style>{_CSS}</style></head><body>
<header>
  <h1>Condo shortlist — which do we go and look at?</h1>
  <p class="note"><b>The algo and the record disagree, and the record wins.</b>
  ALGO is the repo's MMR grade; the EXIT RECORD is the share of that project's
  resales that actually sold at a gain &mdash; in this batch they run
  <i>opposite</i> ways{r_txt}, because a project is often cheap versus its peers
  precisely for reasons the market has already learned. The <b>READ</b> chip does
  that pairing for you; a percentage measured on fewer than {THIN} resales is marked
  <b>THIN</b> and means nothing. Click any row for the reasoning.</p>
</header>
{call_panel(rows, cut, stats)}
<div class="bar">
  <span class="grp" id="who"><b>show</b>
    <button data-m="" class="on" onclick="who('')">all</button>
    {mandate_buttons}
  </span>
  <span class="grp" id="sort"><b>sort</b>
    <button data-k="rank" class="on" onclick="sortBy('rank')">best evidence</button>
    <button data-k="price" onclick="sortBy('price')">cheapest</button>
  </span>
  <span id="count"></span>
</div>
<div class="wrap"><table>
<thead><tr><th>#</th><th>Read</th><th>Verdict</th><th>Project</th>
<th class="num">Price</th><th class="num">Algo</th><th>Exit record</th>
<th>Links</th></tr></thead>
<tbody id="tb">{body}</tbody></table></div>
<footer>Viewer only &mdash; reads eval memory, the realsmart cache and the listings DB;
it never scrapes, scores or spawns a run. Bedroom-count checks come from URA rental
contracts via <code>bed_bands</code>, which is why they are per project rather than a
global sqft table. Evaluations on/after {_e(since)}; refresh after a scan.
Everything, including the fields not shown here, is at
<a href="/api/shortlist">/api/shortlist</a>.</footer>
<script>{_JS}</script>
</body></html>"""


# ----------------------------------------------------------------------- serve

class Handler(BaseHTTPRequestHandler):
    since = DEFAULT_SINCE

    def do_GET(self):
        if self.path.startswith("/api/shortlist"):
            rows = shortlist.collect(self.since)
            rows.sort(key=shortlist.rank_key)
            payload = json.dumps(rows, ensure_ascii=False, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path not in ("/", "/index.html"):
            self.send_error(404)
            return
        try:
            page = render(shortlist.collect(self.since), self.since).encode()
        except Exception as e:  # noqa: BLE001 — a viewer must not 500 on bad data
            page = (f"<pre>shortlist failed: {html.escape(type(e).__name__)}: "
                    f"{html.escape(str(e))}</pre>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def log_message(self, *args):
        pass          # keep the console clean


def main() -> int:
    ap = argparse.ArgumentParser(description="Local shortlist viewer")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--since", default=DEFAULT_SINCE,
                    help="only show evaluations on/after this date")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    Handler.since = args.since
    url = f"http://127.0.0.1:{args.port}"
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Shortlist UI: {url}  (evaluations since {args.since}; Ctrl-C to stop)")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
