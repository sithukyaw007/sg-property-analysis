#!/usr/bin/env python3
"""Local system dashboard — the whole MMR engine state on one page.

Zero dependencies (stdlib http.server), same idiom as ui.py. Computes live
stats from the data files at startup (score distribution, coverage, regime
analysis, district benchmarks, config weights) and stamps the latest backtest
headline numbers (those take minutes to recompute — re-run
`python backtest_ext.py --split-sample` and update BACKTEST below if config
or the panel changes).

Usage:
    python dashboard.py                # http://127.0.0.1:8643 + open browser
    python dashboard.py --port 9000
    python dashboard.py --no-browser
"""

import argparse
import csv
import glob
import html
import json
import os
import re
import secrets
import statistics
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
DEFAULT_PORT = 8643

sys.path.insert(0, BASE)
import config  # noqa: E402 — single source of truth for version + weights
from eval_memory import load_condo, load_index, slugify  # noqa: E402

# CSRF guard (audit #13): POST /analyze spawns a claude agent, so it must not
# be reachable by a random webpage fetch()-ing 127.0.0.1. Per-process token,
# embedded in the served page, required as an X-Csrf-Token header — the custom
# header forces a CORS preflight that cross-origin pages fail.
CSRF_TOKEN = secrets.token_hex(16)


def _csrf_ok(headers) -> bool:
    """Token must match; when the browser sends an Origin it must match Host."""
    if not secrets.compare_digest(headers.get("X-Csrf-Token") or "", CSRF_TOKEN):
        return False
    origin = headers.get("Origin")
    if origin and origin != "null":
        if urllib.parse.urlparse(origin).netloc != (headers.get("Host") or "").strip():
            return False
    return True

# ---------------------------------------------------------------------------
# Headline numbers from the latest backtest run (backtest_ext.py --split-sample).
# These take ~2 min to recompute, so they're stamped here with their run date.
# ---------------------------------------------------------------------------
BACKTEST = {
    "run_date": "2026-06-11",
    "composite_rho": 0.288,
    "composite_n": 1460,
    "optimal_rho": 0.241,
    "model_r2": "0.06–0.13",
    "tests": "122/122",
    "signals": [
        # (name, std_beta, note)
        ("Cheap vs district peers", -0.25, "strongest robust signal — cheap wins"),
        ("Region (CCR vs OCR)", -0.19, "structural: OCR>CCR in every regime since 2004"),
        ("MRT distance", -0.16, "closer wins — suppressed univariately by CCR"),
        ("Real-rent gross yield", -0.16, "carry is priced in: high yield → lower price growth"),
        ("Buyer-pool depth", +0.16, "real content beyond region"),
        ("Dev size (units)", +0.05, "mildly positive"),
        ("Trailing appreciation", +0.03, "story, not signal"),
        ("Freehold", 0.00, "forward-neutral (level premium ~+5% is in the price)"),
        ("Future-infra score", 0.00, "no measured forward power — narrative only"),
        ("Momentum", 0.00, "sign flips across samples — not robust"),
    ],
    "region_fwd": [("OCR", 3.7), ("RCR", 3.1), ("CCR", 1.9)],
}

def config_weights() -> list:
    """MMR component rows with every number read off the live config module.

    The dashboard must never hand-copy weights (audit #6: a hardcoded table
    still showed v3.7 values after later configs shipped). Rows whose shape
    lives in mmr.py rather than config.py (age curve, mrt/dev_size tanh,
    buyer_pool/future point tables) say so explicitly.
    """
    c = config
    return [
        ("age_value (cheap-for-age vs district)",
         f"tanh, cap ±{c.MMR_RELVALUE_CAP:g}",
         f"strongest forward signal — leads · trust knee at -{c.MMR_DISCOUNT_TRUST_KNEE_PCT:g}% "
         f"(excess credit ×{c.MMR_DISCOUNT_EXCESS_CREDIT:g}) · piecewise age-PSF curve"),
        ("psf_value (vs same-size cohort)",
         f"{c.MMR_RELVALUE_SLOPE:g} pts/% × conf, tanh cap ±{c.MMR_RELVALUE_CAP:g}",
         f"tight stack prints blend the benchmark; prints contradicting the discount "
         f"⇒ damp ×{c.MMR_SUSPECT_VALUE_FACTOR:g}"),
        ("age", "0→5 ramp to 7yr, plateau 7-30, decline after (curve in mmr.py)",
         "measured: 0-5yr cohort UNDERperforms; 7-30 flat; only 30+ slows"),
        ("appreciation",
         f"{c.MMR_APPRECIATION_SLOPE:g} pts/pp × conf, center {c.MMR_APPRECIATION_CENTER_PCT:g}%/yr",
         "de-emphasized: trailing ≈ no forward power"),
        ("yield (real rents where matched)",
         f"{c.MMR_YIELD_SLOPE_PTS_PER_PP:g} pts/pp × conf, tanh cap ±{c.MMR_YIELD_CAP:g}",
         "carry only — price drag −0.75pp/yr per +1pp"),
        ("txn_volume (liquidity)", f"{c.MMR_TXN_VOLUME_WEIGHT:g}·tanh(n/40)",
         "exit-risk insurance, not a return signal"),
        ("mrt proximity", "9·tanh (mmr.py)", "validated forward signal (β −0.16)"),
        ("buyer_pool", "−3 .. +7 (mmr.py)", "evidence-backed (β +0.16)"),
        ("dev_size", "6·tanh (mmr.py)", "live via project_units.json"),
        ("future (catalyst)", "0 .. +7, upside-only (mmr.py)",
         "no measured power — kept small"),
        ("lease", "+1 freehold / − short lease", "tenure is not a forward edge"),
        ("momentum", f"{c.MMR_MOMENTUM_WEIGHT:g}", "killed by backtest"),
    ]


# ---------------------------------------------------------------------------
# Live stats from data files
# ---------------------------------------------------------------------------
def gather_stats() -> dict:
    s: dict = {}

    # listings DB + score distribution
    db = {}
    if os.path.exists(os.path.join(DATA, "listings_db.json")):
        with open(os.path.join(DATA, "listings_db.json")) as f:
            db = json.load(f).get("listings", {})
    scores = [l["score_1000"] for l in db.values() if l.get("score_1000") is not None]
    s["n_listings"] = len(db)
    s["n_scored"] = len(scores)
    s["score_mean"] = round(statistics.fmean(scores)) if scores else 0
    s["score_sd"] = round(statistics.pstdev(scores)) if scores else 0
    s["scored_at"] = max((l.get("scored_at") or "" for l in db.values()), default="")
    # histogram, 25 buckets of 40
    hist = [0] * 25
    for v in scores:
        hist[min(24, max(0, int(v // 40)))] += 1
    s["hist"] = hist
    s["tier_counts"] = {
        "rec": sum(1 for v in scores if v >= 650),
        "mid": sum(1 for v in scores if 450 <= v < 650),
        "low": sum(1 for v in scores if v < 450),
    }

    # URA cache
    cache = {}
    if os.path.exists(os.path.join(DATA, "ura_cache.json")):
        with open(os.path.join(DATA, "ura_cache.json")) as f:
            cache = json.load(f).get("projects", {})
    s["n_cache_projects"] = len(cache)
    s["floor_basis"] = Counter(
        (p.get("floor_factors") or {}).get("basis") or "none" for p in cache.values())
    s["n_districts_panel"] = len(glob.glob(os.path.join(DATA, "ura_district_D[0-9][0-9].csv")))

    # panel size (line counts are cheap enough)
    n_txn = 0
    for p in glob.glob(os.path.join(DATA, "ura_district_D[0-9][0-9].csv")):
        with open(p, "rb") as f:
            n_txn += max(0, sum(1 for _ in f) - 1)
    s["n_txns"] = n_txn

    # rentals
    n_rent = 0
    for p in glob.glob(os.path.join(DATA, "ura_rental_D[0-9][0-9].csv")):
        with open(p, "rb") as f:
            n_rent += max(0, sum(1 for _ in f) - 1)
    s["n_rent_contracts_current"] = n_rent
    rc = {}
    if os.path.exists(os.path.join(DATA, "rental_cache.json")):
        with open(os.path.join(DATA, "rental_cache.json")) as f:
            rc = json.load(f).get("projects", {})
    s["n_rental_projects"] = len(rc)

    # project units
    pu = {}
    if os.path.exists(os.path.join(DATA, "project_units.json")):
        with open(os.path.join(DATA, "project_units.json")) as f:
            pu = json.load(f).get("projects", {})
    s["n_unit_projects"] = len(pu)

    # coverage: listings joining real rents / units (same normalization as scorer)
    try:
        import sys
        sys.path.insert(0, BASE)
        from scoring.full_scorer import _project_units_lookup
        from utils.geo import normalize_district
        rent_hit = unit_hit = 0
        for l in db.values():
            pn = (l.get("project_name") or "").strip().lower()
            d = (normalize_district(l.get("district") or "") or "").replace("D", "").lstrip("0")
            if f"{pn}|{d}" in rc:
                rent_hit += 1
            if _project_units_lookup(l.get("project_name")):
                unit_hit += 1
        s["rent_cov"] = rent_hit
        s["unit_cov"] = unit_hit
    except Exception:
        s["rent_cov"] = s["unit_cov"] = 0

    # regime analysis (live from the PPI csv)
    s["regime"] = regime_rows()

    # district benchmarks
    s["districts"] = []
    if os.path.exists(os.path.join(DATA, "district_medians.json")):
        with open(os.path.join(DATA, "district_medians.json")) as f:
            dm = json.load(f)
        for d in sorted(dm.get("medians", {})):
            e = dm["medians"][d]
            s["districts"].append((d, e.get("psf"), e.get("rental_psf"), e.get("avg_yield")))
        s["dm_updated"] = dm.get("last_updated", "?")

    # top listings — one row per project (best unit), joined to eval memory
    evals = load_index().get("condos", {})
    s["n_evals"] = len(evals)
    best_by_proj: dict = {}
    for l in db.values():
        # Same status filter as ui.py: skip only explicit "stale" — records
        # written before the staleness sweep existed carry no status at all.
        if l.get("score_1000") is None or l.get("status") == "stale":
            continue
        key = (l.get("project_name") or l.get("title") or "").strip().lower()
        if key and (key not in best_by_proj
                    or l["score_1000"] > best_by_proj[key]["score_1000"]):
            best_by_proj[key] = l
    top = sorted(best_by_proj.values(), key=lambda l: -l["score_1000"])[:20]
    s["top"] = []
    for l in top:
        name = l.get("project_name") or l.get("title") or "?"
        ev = evals.get(slugify(name))
        s["top"].append((l, ev))
    return s


def regime_rows():
    path = os.path.join(DATA, "ppi_nonlanded_locality.csv")
    if not os.path.exists(path):
        return []
    reg_map = {"Core Central Region": "CCR", "Rest of Central Region": "RCR",
               "Outside Central Region": "OCR"}
    idx = defaultdict(dict)
    with open(path) as fh:
        for r in csv.DictReader(fh):
            try:
                y, q = r["quarter"].split("-Q")
                t = int(y) + (int(q) - 0.5) / 4.0
                idx[t][reg_map[r["market_segment"]]] = float(r["price_index"])
            except (KeyError, ValueError):
                continue
    rows = []
    for t in sorted(idx):
        tf = t + 2.0
        if tf in idx and all(g in idx[t] and g in idx[tf] for g in ("CCR", "RCR", "OCR")):
            rows.append({g: (idx[tf][g] / idx[t][g]) ** 0.5 - 1 for g in ("CCR", "RCR", "OCR")})
    buckets = [("DOWN — mkt fwd < 0", lambda m: m < 0),
               ("FLAT — 0 .. 3%/yr", lambda m: 0 <= m < 0.03),
               ("BULL — > 3%/yr", lambda m: m >= 0.03)]
    out = []
    for name, cond in buckets:
        sel = [r for r in rows if cond(sum(r.values()) / 3)]
        if sel:
            out.append({
                "name": name, "n": len(sel),
                "CCR": statistics.fmean(r["CCR"] for r in sel) * 100,
                "RCR": statistics.fmean(r["RCR"] for r in sel) * 100,
                "OCR": statistics.fmean(r["OCR"] for r in sel) * 100,
            })
    return out


# ---------------------------------------------------------------------------
# Headless Claude analysis jobs (background `claude -p`, result → eval memory)
# ---------------------------------------------------------------------------
_NAME_OK = re.compile(r"^[\w @&'().,/\-+#]{2,80}$")
RUNS_DIR = os.path.join(BASE, "output", "analyze_runs")

# slug -> {name, status: running|done|failed, started, log, returncode}
# In-memory only: jobs die with the dashboard process, but their RESULT is the
# evaluation written to eval memory (evaluations/<slug>.json), which persists
# and renders at /eval/<slug>.
JOBS: dict = {}
_JOBS_LOCK = threading.Lock()


def start_claude_analysis(condo_name: str) -> tuple[bool, dict | str]:
    """Run `claude -p "analyze <condo>"` headless in the background.

    The prompt routes to the /analyze-development flow per CLAUDE.md, told to
    run non-interactively (assume investment intent — every dashboard metric
    already assumes it) and to finish with --from-review so the verdict lands
    in eval memory. Transcript goes to output/analyze_runs/<slug>_<ts>.log.
    """
    name = condo_name.strip()
    if not _NAME_OK.match(name):
        return False, "invalid condo name"
    slug = slugify(name)
    with _JOBS_LOCK:
        job = JOBS.get(slug)
        if job and job["status"] == "running":
            return False, "already running"
        os.makedirs(RUNS_DIR, exist_ok=True)
        started = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = os.path.join(RUNS_DIR, f"{slug}_{started}.log")
        prompt = (
            f"analyze {name} — investment purpose (5-7yr hold), per the "
            "/analyze-development flow. This is a non-interactive headless run: "
            "do not ask the user anything; where the flow would ask, assume "
            "investment intent and proceed. Finish the full flow including "
            "--from-review so the evaluation is saved to eval memory."
        )
        # Scoped permissions, not bypassPermissions (audit #13): the analyze
        # flow needs the shell (python invest.py …), repo file IO, code search,
        # web research, and the Skill/TodoWrite tools that drive the
        # /analyze-development runbook — nothing that warrants a blanket
        # permission bypass on a CSRF-reachable endpoint. The server also only
        # binds 127.0.0.1 and POST /analyze requires the per-process token.
        allowed = ("Bash,Read,Write,Edit,Glob,Grep,"
                   "WebSearch,WebFetch,Skill,TodoWrite")
        cmd = ["claude", "-p", prompt, "--allowedTools", allowed]
        try:
            logf = open(log_path, "w")
        except OSError as e:
            return False, str(e)
        try:
            proc = subprocess.Popen(cmd, cwd=BASE, stdin=subprocess.DEVNULL,
                                    stdout=logf, stderr=subprocess.STDOUT)
        except OSError as e:
            logf.close()  # Popen raised — don't leak the log fd
            return False, str(e)
        JOBS[slug] = {"name": name, "status": "running", "started": started,
                      "log": os.path.relpath(log_path, BASE), "returncode": None}
    threading.Thread(target=_reap_job, args=(slug, proc, logf), daemon=True).start()
    return True, {"slug": slug, "log": JOBS[slug]["log"]}


def _reap_job(slug: str, proc: subprocess.Popen, logf) -> None:
    rc = proc.wait()
    logf.close()
    with _JOBS_LOCK:
        job = JOBS.get(slug)
        if job:
            job["status"] = "done" if rc == 0 else "failed"
            job["returncode"] = rc


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _e(x):
    return html.escape(str(x))


def _signal_bar(beta, scale=0.27):
    """Signed horizontal bar, centered axis."""
    w = min(100, abs(beta) / scale * 100)
    cls = "pos" if beta > 0.004 else ("neg" if beta < -0.004 else "zero")
    left = 50 if beta >= 0 else 50 - w / 2
    return (f'<div class="sigbar"><i class="axis"></i>'
            f'<i class="fill {cls}" style="left:{left:.1f}%;width:{w/2:.1f}%"></i></div>')


def render(s: dict) -> str:
    # KPI strip
    kpis = [
        ("FORWARD ρ", f"+{BACKTEST['composite_rho']:.3f}", f"n={BACKTEST['composite_n']} · beats in-sample fit (+{BACKTEST['optimal_rho']:.3f})"),
        ("TESTS", BACKTEST["tests"], "pytest, all green"),
        ("LISTINGS SCORED", f"{s['n_scored']:,}", f"of {s['n_listings']:,} in DB · {s['scored_at']}"),
        ("SCORE /1000", f"{s['score_mean']} ± {s['score_sd']}", "centered · tiers 450 / 650"),
        ("URA PANEL", f"{s['n_txns']:,}", f"txns · {s['n_districts_panel']}/28 districts · {s['n_cache_projects']:,} projects"),
        ("REAL RENTS", f"{s['n_rent_contracts_current']:,}", f"contracts · {s['n_rental_projects']:,} projects cached"),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><div class="kpi-label">{_e(a)}</div>'
        f'<div class="kpi-value">{_e(b)}</div><div class="kpi-sub">{_e(c)}</div></div>'
        for a, b, c in kpis)

    # signal table
    sig_html = "".join(
        f'<tr><td class="sig-name">{_e(n)}</td>'
        f'<td class="num {"pos" if b>0.004 else ("neg" if b<-0.004 else "dim")}">{b:+.2f}</td>'
        f'<td class="barcell">{_signal_bar(b)}</td>'
        f'<td class="note">{_e(note)}</td></tr>'
        for n, b, note in BACKTEST["signals"])

    # region forward
    maxr = max(v for _, v in BACKTEST["region_fwd"])
    region_html = "".join(
        f'<div class="hrow"><span class="hlabel">{_e(g)}</span>'
        f'<div class="htrack"><div class="hfill" style="width:{v/maxr*100:.0f}%"></div></div>'
        f'<span class="hval">+{v:.1f}%/yr</span></div>'
        for g, v in BACKTEST["region_fwd"])

    # regime table
    regime_html = ""
    for r in s["regime"]:
        cells = "".join(
            f'<td class="num {"pos" if r[g]>0 else "neg"}">{r[g]:+.2f}</td>'
            for g in ("CCR", "RCR", "OCR"))
        gap = r["OCR"] - r["CCR"]
        regime_html += (f'<tr><td>{_e(r["name"])}</td><td class="num dim">{r["n"]}</td>'
                        f'{cells}<td class="num pos">+{gap:.2f}</td></tr>')

    # histogram
    hist = s["hist"]
    hmax = max(hist) or 1
    bars = "".join(
        f'<div class="vbar{" t1" if i*40>=650 else (" t2" if i*40>=450 else "")}" '
        f'style="height:{v/hmax*100:.0f}%" title="{i*40}–{i*40+39}: {v}"></div>'
        for i, v in enumerate(hist))
    tc = s["tier_counts"]

    # coverage
    def cov(label, hit, total, extra=""):
        pct = hit / total * 100 if total else 0
        return (f'<div class="hrow"><span class="hlabel wide">{_e(label)}</span>'
                f'<div class="htrack"><div class="hfill" style="width:{pct:.0f}%"></div></div>'
                f'<span class="hval">{pct:.0f}% <i class="dim">({hit:,}/{total:,}{extra})</i></span></div>')

    fb = s["floor_basis"]
    cov_html = (
        cov("Real project rents (listings)", s["rent_cov"], s["n_listings"]) +
        cov("Units + coords (listings)", s["unit_cov"], s["n_listings"]) +
        cov("Per-project floor curve", fb.get("project_fe", 0),
            sum(fb.values()), " projects") +
        cov("URA panel districts", s["n_districts_panel"], 28)
    )

    # weights table — read live off config.py, never hand-copied
    weights_html = "".join(
        f'<tr><td class="sig-name">{_e(n)}</td><td class="num">{_e(w)}</td>'
        f'<td class="note">{_e(note)}</td></tr>'
        for n, w, note in config_weights())

    # district benchmark strip
    dist_html = "".join(
        f'<tr><td>{_e(d)}</td><td class="num">{f"${p:,.0f}" if p is not None else "—"}</td>'
        f'<td class="num">{f"${r:.2f}" if r is not None else "—"}</td>'
        f'<td class="num">{f"{y:.2f}%" if y is not None else "—"}</td></tr>'
        for d, p, r, y in s["districts"])

    # top listings (one per project) with eval status + analyze buttons
    _RATING_CLS = {"strong buy": "pos", "buy": "pos", "neutral": "dim", "avoid": "neg"}
    top_html = ""
    for i, (l, ev) in enumerate(s["top"], 1):
        url = l.get("url") or "#"
        name = l.get("project_name") or l.get("title") or "?"
        slug = slugify(name)
        if ev:
            rating = ev.get("latest_rating") or "?"
            cls = _RATING_CLS.get(rating.lower(), "dim")
            eval_cell = (f'<a class="evaltag {cls}" href="/eval/{_e(slug)}" '
                         f'title="evaluated {_e(ev.get("latest_date") or "?")}">'
                         f'{_e(rating)}</a>')
            btn = (f'<button class="act rerun" data-name="{_e(name)}" data-slug="{_e(slug)}" '
                   f'title="re-run analysis headless in the background">RERUN&nbsp;&#x21bb;</button>')
        else:
            eval_cell = '<span class="dim">—</span>'
            btn = (f'<button class="act" data-name="{_e(name)}" data-slug="{_e(slug)}" '
                   f'title="run a headless claude analysis in the background">ANALYZE&nbsp;&#x25b6;</button>')
        top_html += (
            f'<tr><td class="num dim">{i:02d}</td>'
            f'<td><a href="{_e(url)}" target="_blank">{_e(name)}</a></td>'
            f'<td class="num">{_e(l.get("district") or "")}</td>'
            f'<td class="num">{_e(l.get("beds") or "?")}BR</td>'
            f'<td class="num">${(l.get("price") or 0):,}</td>'
            f'<td class="num">${(l.get("psf") or 0):,.0f}</td>'
            f'<td class="num score">{l["score_1000"]}</td>'
            f'<td>{eval_cell}</td><td>{btn}</td></tr>')

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MMR · Property Finder — System Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,300;9..144,600;9..144,900&family=IBM+Plex+Mono:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
:root {{
  --bg:#0a0d0b; --panel:#0f1411; --panel2:#121a15; --line:#1d2a22;
  --tx:#cfe3d4; --dim:#5d7466; --amber:#ffb454; --green:#52e09c;
  --red:#ff6b6b; --ink:#86f3c0;
}}
* {{ box-sizing:border-box; margin:0; padding:0; }}
html {{ scrollbar-color:#243528 var(--bg); }}
body {{
  background:var(--bg); color:var(--tx);
  font:300 13px/1.55 "IBM Plex Mono", ui-monospace, monospace;
  background-image:
    radial-gradient(1200px 500px at 70% -10%, rgba(82,224,156,.05), transparent 60%),
    radial-gradient(900px 400px at 0% 0%, rgba(255,180,84,.04), transparent 55%);
}}
body::after {{ /* scanline grain */
  content:""; position:fixed; inset:0; pointer-events:none; opacity:.05;
  background:repeating-linear-gradient(0deg, transparent 0 2px, #000 2px 3px);
}}
.wrap {{ max-width:1280px; margin:0 auto; padding:34px 28px 80px; }}
header {{ display:flex; align-items:baseline; gap:18px; border-bottom:1px solid var(--line);
  padding-bottom:18px; margin-bottom:22px; flex-wrap:wrap; }}
h1 {{ font:900 40px/1 "Fraunces", serif; letter-spacing:-.5px; color:#f2fff7; }}
h1 em {{ font-style:normal; color:var(--green); }}
.stamp {{ color:var(--dim); font-size:11px; letter-spacing:.12em; text-transform:uppercase; }}
.stamp b {{ color:var(--amber); font-weight:500; }}
.kpis {{ display:grid; grid-template-columns:repeat(6,1fr); gap:10px; margin-bottom:26px; }}
.kpi {{ background:var(--panel); border:1px solid var(--line); border-radius:4px;
  padding:14px 14px 12px; position:relative; overflow:hidden;
  animation:rise .5s cubic-bezier(.2,.8,.3,1) both; }}
.kpi:nth-child(2){{animation-delay:.05s}} .kpi:nth-child(3){{animation-delay:.1s}}
.kpi:nth-child(4){{animation-delay:.15s}} .kpi:nth-child(5){{animation-delay:.2s}}
.kpi:nth-child(6){{animation-delay:.25s}}
.kpi::before {{ content:""; position:absolute; left:0; top:0; bottom:0; width:2px;
  background:linear-gradient(var(--green), transparent); }}
.kpi-label {{ font-size:10px; letter-spacing:.18em; color:var(--dim); }}
.kpi-value {{ font-size:24px; font-weight:600; color:#eafff2; margin:4px 0 2px;
  font-variant-numeric:tabular-nums; }}
.kpi-sub {{ font-size:10.5px; color:var(--dim); }}
@keyframes rise {{ from {{ opacity:0; transform:translateY(8px); }} }}
.grid {{ display:grid; grid-template-columns:7fr 5fr; gap:14px; }}
section {{ background:var(--panel); border:1px solid var(--line); border-radius:4px;
  padding:18px 18px 16px; margin-bottom:14px; }}
section h2 {{ font:600 11px/1 "IBM Plex Mono",monospace; letter-spacing:.22em;
  text-transform:uppercase; color:var(--amber); margin-bottom:4px; }}
section .sub {{ color:var(--dim); font-size:11px; margin-bottom:14px; }}
table {{ width:100%; border-collapse:collapse; font-size:12px; }}
th {{ text-align:left; color:var(--dim); font-weight:400; font-size:10px;
  letter-spacing:.15em; text-transform:uppercase; padding:0 8px 8px 0; }}
td {{ padding:5px 8px 5px 0; border-top:1px solid #131c16; vertical-align:middle; }}
.num {{ font-variant-numeric:tabular-nums; white-space:nowrap; }}
.pos {{ color:var(--green); }} .neg {{ color:var(--red); }}
.dim {{ color:var(--dim); font-style:normal; }} .zero {{ color:var(--dim); }}
.sig-name {{ color:#e6f5ea; }}
.note {{ color:var(--dim); font-size:11px; }}
.barcell {{ width:150px; }}
.sigbar {{ position:relative; height:10px; background:var(--panel2); border-radius:2px; }}
.sigbar .axis {{ position:absolute; left:50%; top:-2px; bottom:-2px; width:1px; background:#2c4234; }}
.sigbar .fill {{ position:absolute; top:2px; bottom:2px; border-radius:1px; }}
.fill.pos {{ background:var(--green); box-shadow:0 0 8px rgba(82,224,156,.5); }}
.fill.neg {{ background:var(--red); box-shadow:0 0 8px rgba(255,107,107,.45); }}
.fill.zero {{ background:#3a4d40; width:2px !important; left:calc(50% - 1px) !important; }}
.hrow {{ display:flex; align-items:center; gap:10px; margin:7px 0; }}
.hlabel {{ width:42px; color:#e6f5ea; }} .hlabel.wide {{ width:240px; }}
.htrack {{ flex:1; height:12px; background:var(--panel2); border-radius:2px; overflow:hidden; }}
.hfill {{ height:100%; background:linear-gradient(90deg, #1f6e49, var(--green));
  box-shadow:0 0 10px rgba(82,224,156,.35); animation:grow .8s cubic-bezier(.2,.8,.3,1) both; }}
@keyframes grow {{ from {{ width:0 !important; }} }}
.hval {{ min-width:120px; text-align:right; color:var(--tx); font-size:11.5px; }}
.hist {{ display:flex; align-items:flex-end; gap:2px; height:110px; margin:10px 0 6px; }}
.vbar {{ flex:1; background:#2a4434; border-radius:1px 1px 0 0; min-height:1px;
  transition:background .15s; }}
.vbar.t2 {{ background:#3f6a4d; }} .vbar.t1 {{ background:var(--green);
  box-shadow:0 0 6px rgba(82,224,156,.4); }}
.vbar:hover {{ background:var(--amber); }}
.axisrow {{ display:flex; justify-content:space-between; color:var(--dim); font-size:10px; }}
.legend {{ display:flex; gap:18px; margin-top:10px; font-size:11px; color:var(--dim); }}
.swatch {{ display:inline-block; width:9px; height:9px; border-radius:1px; margin-right:6px; }}
a {{ color:var(--ink); text-decoration:none; }}
a:hover {{ color:var(--green); text-decoration:underline; }}
.score {{ color:var(--green); font-weight:600; }}
.foot {{ border-top:1px solid var(--line); margin-top:26px; padding-top:14px;
  color:var(--dim); font-size:11px; max-width:900px; }}
.foot b {{ color:var(--amber); font-weight:500; }}
.scroll {{ max-height:300px; overflow-y:auto; }}
.act {{ font:500 10px/1 "IBM Plex Mono",monospace; letter-spacing:.1em; cursor:pointer;
  color:var(--amber); background:transparent; border:1px solid #4d3a17; border-radius:3px;
  padding:5px 9px; transition:all .15s; white-space:nowrap; }}
.act:hover {{ background:var(--amber); color:#161003; box-shadow:0 0 12px rgba(255,180,84,.4); }}
.act.rerun {{ color:var(--dim); border-color:#22332a; }}
.act.rerun:hover {{ background:#22332a; color:var(--tx); box-shadow:none; }}
.act.done {{ color:var(--green); border-color:#1f4a34; pointer-events:none; }}
.evaltag {{ font-size:10.5px; letter-spacing:.08em; text-transform:uppercase;
  border-bottom:1px dotted currentColor; }}
.evaltag:hover {{ text-decoration:none; filter:brightness(1.3); }}
.qbar {{ display:flex; gap:8px; align-items:center; margin-bottom:14px; }}
.qbar input {{ flex:0 0 320px; background:var(--panel2); border:1px solid var(--line);
  border-radius:3px; color:var(--tx); font:300 12px "IBM Plex Mono",monospace;
  padding:7px 10px; outline:none; }}
.qbar input:focus {{ border-color:#3a5a46; box-shadow:0 0 0 2px rgba(82,224,156,.12); }}
#qmsg {{ font-size:11px; }}
@media (max-width:1000px) {{ .grid {{ grid-template-columns:1fr; }} .kpis {{ grid-template-columns:repeat(3,1fr); }} }}
</style></head>
<body><div class="wrap">

<header>
  <h1>MMR<em>/</em>DESK</h1>
  <div class="stamp">PROPERTY FINDER · SCORING ENGINE <b>v{_e(config.CONFIG_VERSION)}</b> · CONFIG {_e(config.score_version())} · BACKTEST {_e(BACKTEST["run_date"])} · 5–7YR INVESTMENT HOLD · SG CONDO</div>
</header>

<div class="kpis">{kpi_html}</div>

<div class="grid">
<div>
  <section>
    <h2>Forward signals — what actually predicts returns</h2>
    <div class="sub">standardized marginal β vs realized 2yr forward resale-PSF return · value/region/liquidity controlled · full 28-district panel</div>
    <table>
      <tr><th>signal</th><th>std β</th><th></th><th>read</th></tr>
      {sig_html}
    </table>
  </section>

  <section>
    <h2>MMR component weights (live from config.py · {_e(config.score_version())})</h2>
    <div class="sub">every weight is backtest-anchored or explicitly labeled as insurance · missing data is neutral, never penalized ·
      raw base {config.MMR_BASE}, /1000 norm center {config.MMR_NORM_CENTER} / scale {config.MMR_NORM_SCALE}</div>
    <table>
      <tr><th>component</th><th>weight</th><th>why</th></tr>
      {weights_html}
    </table>
  </section>

  <section>
    <h2>Top-ranked projects · click to analyze</h2>
    <div class="sub">best active unit per project, by score_1000 · {s["n_evals"]:,} condos in eval memory ·
      ANALYZE runs <b style="color:var(--ink)">claude -p "analyze &lt;condo&gt;"</b> headless in the background —
      the verdict lands in eval memory (transcript: output/analyze_runs/)</div>
    <div class="qbar">
      <input id="qname" type="text" placeholder="any condo name… e.g. The Continuum" spellcheck="false">
      <button class="act" id="qgo">ANALYZE&nbsp;&#x25b6;</button>
      <span id="qmsg" class="dim"></span>
    </div>
    <table>
      <tr><th>#</th><th>project</th><th>dist</th><th>type</th><th>price</th><th>psf</th><th>score</th><th>verdict</th><th></th></tr>
      {top_html}
    </table>
  </section>
</div>

<div>
  <section>
    <h2>Realized forward by region (2024→26)</h2>
    <div class="sub">per-project panel · annualized resale-PSF</div>
    {region_html}
  </section>

  <section>
    <h2>Region tilt across regimes (2004–2026)</h2>
    <div class="sub">81 rolling 2yr windows, URA non-landed index · fwd %/yr · OCR&gt;CCR in <b style="color:var(--green)">every</b> regime</div>
    <table>
      <tr><th>regime</th><th>n</th><th>CCR</th><th>RCR</th><th>OCR</th><th>gap</th></tr>
      {regime_html}
    </table>
  </section>

  <section>
    <h2>Score distribution ({s["n_scored"]:,} listings)</h2>
    <div class="hist">{bars}</div>
    <div class="axisrow"><span>0</span><span>250</span><span>500</span><span>750</span><span>1000</span></div>
    <div class="legend">
      <span><i class="swatch" style="background:#2a4434"></i>&lt;450 below · {tc["low"]:,}</span>
      <span><i class="swatch" style="background:#3f6a4d"></i>450–650 · {tc["mid"]:,}</span>
      <span><i class="swatch" style="background:var(--green)"></i>650+ recommended · {tc["rec"]:,}</span>
    </div>
  </section>

  <section>
    <h2>Data coverage</h2>
    {cov_html}
  </section>

  <section>
    <h2>District benchmarks (measured, {_e(s.get("dm_updated","?"))})</h2>
    <div class="sub">12mo URA resale PSF · 12mo real rental contracts</div>
    <div class="scroll">
    <table>
      <tr><th>district</th><th>med psf</th><th>rent psf/mo</th><th>gross yield</th></tr>
      {dist_html}
    </table>
    </div>
  </section>
</div>
</div>

<div class="foot">
  <b>Honesty rails:</b> the full model explains &lt;13% of forward variance — small score gaps are noise;
  verdicts require research, not just the score. ROI figures are <b>all-cash</b> (no leverage), rent grows 2%/yr,
  appreciation decays toward mean. Regional tilt is structural (every regime since 2004) but compressed below the
  measured spread. Re-run <b>python backtest_ext.py --split-sample</b> after any data/weight change and
  <b>python calibrate_forward.py</b> quarterly from 2027-03. Fresh-listings + arena UI
  (auto-polls PropertyGuru): <b>python ui.py</b> → :8642.
</div>

</div>
<script>
const CSRF = "{CSRF_TOKEN}";  // per-process token, required on mutating POSTs
const WATCHING = {{}};  // slug -> button
async function spawn(name, btn) {{
  if (!name) return;
  const orig = btn.textContent;
  btn.textContent = "STARTING…";
  try {{
    const r = await fetch("/analyze", {{ method:"POST",
      headers: {{"Content-Type":"application/x-www-form-urlencoded",
                 "X-Csrf-Token": CSRF}},
      body: "name=" + encodeURIComponent(name) }});
    if (r.ok) {{
      const job = await r.json();
      btn.textContent = "RUNNING…";
      btn.classList.add("done");
      WATCHING[job.slug] = btn;
      pollJobs();
    }} else {{ btn.textContent = orig; alert(await r.text()); }}
  }} catch (e) {{ btn.textContent = orig; alert(e); }}
}}
let pollTimer = null;
async function pollJobs() {{
  if (pollTimer) return;
  pollTimer = setInterval(async () => {{
    let jobs;
    try {{ jobs = await (await fetch("/jobs")).json(); }} catch (e) {{ return; }}
    let pending = 0;
    for (const [slug, btn] of Object.entries(WATCHING)) {{
      const j = jobs[slug];
      if (!j || j.status === "running") {{ pending++; continue; }}
      if (j.status === "done") {{
        btn.outerHTML = `<a class="act done" href="/eval/${{slug}}">DONE ✓ VIEW</a>`;
      }} else {{
        btn.textContent = "FAILED ✗";
        btn.title = "see " + j.log;
        btn.classList.remove("done");
      }}
      delete WATCHING[slug];
    }}
    if (!pending) {{ clearInterval(pollTimer); pollTimer = null; }}
  }}, 10000);
}}
document.querySelectorAll("button.act[data-name]").forEach(b =>
  b.addEventListener("click", () => spawn(b.dataset.name, b)));
// resume watching jobs that were already running when this page loaded
fetch("/jobs").then(r => r.json()).then(jobs => {{
  let any = false;
  for (const [slug, j] of Object.entries(jobs)) {{
    if (j.status !== "running") continue;
    const btn = document.querySelector(`button.act[data-slug="${{slug}}"]`);
    if (btn) {{ btn.textContent = "RUNNING…"; btn.classList.add("done");
               WATCHING[slug] = btn; any = true; }}
  }}
  if (any) pollJobs();
}}).catch(() => {{}});
const qgo = document.getElementById("qgo");
if (qgo) {{
  const run = () => {{
    const v = document.getElementById("qname").value.trim();
    if (v) spawn(v, qgo);
  }};
  qgo.addEventListener("click", run);
  document.getElementById("qname").addEventListener("keydown",
    e => {{ if (e.key === "Enter") run(); }});
}}
</script>
</body></html>"""


def render_eval(slug: str) -> "str | None":
    """Stored evaluation page (same theme), latest history entry first."""
    condo = load_condo(slug)
    if not condo:
        return None
    name = condo.get("condo") or slug
    cls = {"strong buy": "pos", "buy": "pos", "neutral": "dim", "avoid": "neg"}
    blocks = ""
    for h in reversed(condo.get("history", [])):
        rating = h.get("rating") or "?"
        rcls = cls.get(rating.lower(), "dim")
        lists = ""
        for label, key in (("red flags", "red_flags"), ("catalysts", "catalysts")):
            items = h.get(key) or []
            if items:
                lis = "".join(f"<li>{_e(x)}</li>" for x in items)
                lists += f'<div class="elist"><h3>{label}</h3><ul>{lis}</ul></div>'
        asof = h.get("as_of") or {}
        asof_str = " · ".join(f"{k} {v:,}" if isinstance(v, (int, float)) else f"{k} {v}"
                              for k, v in asof.items() if v is not None)
        blocks += f"""
  <section>
    <h2><span class="{rcls}" style="font-size:15px">{_e(rating)}</span>
      <span class="dim" style="margin-left:10px">confidence {_e(h.get("confidence") or "?")}
      · {_e(h.get("evaluated_at") or "?")}</span></h2>
    <p class="ptext">{_e(h.get("summary") or "")}</p>
    <p class="ptext dim">{_e(h.get("rating_rationale") or "")}</p>
    {lists}
    {f'<div class="sub" style="margin-top:10px">as of: {_e(asof_str)}</div>' if asof_str else ""}
  </section>"""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>{_e(name)} — evaluation</title>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,900&family=IBM+Plex+Mono:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
:root {{ --bg:#0a0d0b; --panel:#0f1411; --line:#1d2a22; --tx:#cfe3d4; --dim:#5d7466;
  --amber:#ffb454; --green:#52e09c; --red:#ff6b6b; --ink:#86f3c0; }}
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ background:var(--bg); color:var(--tx);
  font:300 13px/1.6 "IBM Plex Mono",monospace; }}
.wrap {{ max-width:840px; margin:0 auto; padding:36px 26px 60px; }}
h1 {{ font:900 34px/1.1 "Fraunces",serif; color:#f2fff7; margin-bottom:4px; }}
.crumb {{ color:var(--dim); font-size:11px; letter-spacing:.15em; text-transform:uppercase;
  margin-bottom:24px; }}
.crumb a {{ color:var(--ink); text-decoration:none; }}
section {{ background:var(--panel); border:1px solid var(--line); border-radius:4px;
  padding:18px; margin-bottom:14px; }}
section h2 {{ font-size:12px; letter-spacing:.15em; text-transform:uppercase;
  color:var(--amber); margin-bottom:10px; }}
.pos {{ color:var(--green); }} .neg {{ color:var(--red); }} .dim {{ color:var(--dim); }}
.ptext {{ margin-bottom:10px; }}
.elist h3 {{ font-size:10px; letter-spacing:.18em; text-transform:uppercase;
  color:var(--dim); margin:10px 0 4px; }}
.elist li {{ margin-left:18px; font-size:12px; }}
.sub {{ color:var(--dim); font-size:11px; }}
</style></head><body><div class="wrap">
<div class="crumb"><a href="/">&larr; MMR/DESK</a> · EVAL MEMORY · {_e(condo.get("district") or "")}</div>
<h1>{_e(name)}</h1>
<div class="crumb">may be stale — re-verify price &amp; conditions before relying on it</div>
{blocks}
</div></body></html>"""


# ---------------------------------------------------------------------------
# Index page cache: re-render at most every TTL so an analysis finished in the
# background shows its verdict on the next refresh (no server restart), while
# rapid reloads stay instant.
_PAGE_TTL_S = 30
_page_cache = {"html": "", "at": 0.0}
_page_lock = threading.Lock()


def index_page() -> str:
    import time
    with _page_lock:
        if time.monotonic() - _page_cache["at"] > _PAGE_TTL_S or not _page_cache["html"]:
            _page_cache["html"] = render(gather_stats())
            _page_cache["at"] = time.monotonic()
        return _page_cache["html"]


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: str, code: int = 200, ctype: str = "text/html; charset=utf-8"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(index_page())
            return
        if self.path == "/jobs":
            with _JOBS_LOCK:
                body = json.dumps(JOBS)
            self._send(body, 200, "application/json")
            return
        if self.path.startswith("/eval/"):
            slug = urllib.parse.unquote(self.path[len("/eval/"):]).strip("/")
            if re.fullmatch(r"[a-z0-9\-]{1,80}", slug):
                page = render_eval(slug)
                if page:
                    self._send(page)
                    return
            self._send("no evaluation found", 404, "text/plain")
            return
        self._send("not found", 404, "text/plain")

    def do_POST(self):
        if self.path != "/analyze":
            self._send("not found", 404, "text/plain")
            return
        if not _csrf_ok(self.headers):
            self._send("missing or invalid CSRF token", 403, "text/plain")
            return
        length = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(min(length, 4096)).decode())
        name = (form.get("name") or [""])[0]
        ok, result = start_claude_analysis(name)
        if ok:
            self._send(json.dumps(result), 200, "application/json")
        else:
            self._send(str(result), 400, "text/plain")

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    print("Gathering live stats from data/ ...")
    index_page()  # warm the cache before opening the browser
    url = f"http://127.0.0.1:{args.port}"
    print(f"MMR dashboard → {url}   (Ctrl-C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    # Threading: a browser's idle speculative connection must not block the
    # accept loop (single-threaded HTTPServer hangs the whole page on it).
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
