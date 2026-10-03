#!/usr/bin/env python3
"""Weekly scan — poll PropertyGuru, algo-grade everything, AI-analyze the few worth it.

Run by hand, once a week. Nothing auto-starts it: the reminder lives in the
personal data store (`projects/property-finder/weekly-scan.md`, surfaced at
session start when it comes due) and the owner drives it from there. This
script closes the loop by stamping that note's `last_run` on success.

The cycle:

  0. **Pre-flight** — URA sales + rentals are synced through the official
     API (ura_api.py / sgprop, ~40 s, no browser) so the poll scores against
     current prints. Without sgprop or a key it falls back to re-fetching
     month-old district CSVs in a browser.
  1. **Poll** — `poller.run_poll()` scrapes the newest listings for the configured
     districts/beds, upserts them, and MMR-scores the new / price-changed ones.
  2. **Algo grade** — every new and price-changed listing already carries
     `score_1000` (plus the valuation / livability / overall axes) from step 1.
     That grade is free, so it is applied to ALL of them.
  3. **Gate** — only listings that clear at least ONE registered lens's bar
     (scoring/lenses.py — mmr's bar is `MIN_SCORE_FOR_AI`) and the sanity /
     cooldown / dedupe rules below earn an AI run. Everything else is logged
     algo-only. This is the whole point: the agent is the expensive step —
     and several independent lenses can each nominate, because the days when
     one number (score_1000) decided everything are what let value traps
     monopolize the research budget (see shortlist.py's header).
     Before the capped gate, the top contenders are detail-enriched (floor,
     facing, PES/loft flags) and re-scored, so the cap picks on full data.
  4. **AI analyze** — realsmart REALSCORE is fetched once per project up
     front; then for each shortlisted listing, `--parallel` at a time
     (default 3), a headless `claude -p` runs
     the repo's `/analyze-listing` flow and lands a Buy/Neutral/Avoid verdict in
     eval memory. Gather is `invest.py --from-db` — the listing is already
     scraped and enriched, so the agent never touches the network for it.
  5. **Digest** — `output/weekly/YYYY-MM-DD.md`: the whole week, honestly (what
     was seen, what was gated out and why, every verdict).
  6. **Store push** — only genuinely good finds (Buy/Strong Buy at >= medium
     confidence) are appended to the personal data store's scan-finds note.
     The bar is deliberately high: an over-fed note gets ignored.

Usage:
    python weekly.py                 # the real thing (headed browser — see below)
    python weekly.py --dry-run       # poll + gate + digest, but spawn no agents
    python weekly.py --force         # re-run even though this week already succeeded
    python weekly.py --no-poll       # skip the scrape; grade what arrived since the last run
    python weekly.py --max-ai 2      # tighter cap for this run
    python weekly.py --parallel 1    # agents one at a time (old behaviour)
    python weekly.py --no-refresh    # skip the URA pre-flight refresh
    python weekly.py --no-enrich     # skip pre-gate enrichment + re-score

**Headed by default.** PropertyGuru sits behind Cloudflare, which blocks
headless background polls (see CLAUDE.md). A Chrome window opens and reuses the
persistent `chrome-profile` clearance cookie; if a challenge does appear, solve
it once and the rest of the cycle proceeds. `--headless` is available for
machines where that already works. Since a human is present anyway — this is a
hand-run script — that challenge is a prompt, not a failure mode.

State lives in `data/weekly_state.json`. A run that polled successfully marks
the ISO week done, so a second run that week is a no-op; a run whose poll FAILED
does not, so the retry is just running it again.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import eval_memory
import listings_db
import poller
from utils.store import store_root, fetch_script

DATA_DIR = os.path.join(BASE, "data")
STATE_FILE = os.path.join(DATA_DIR, "weekly_state.json")
DIGEST_DIR = os.path.join(BASE, "output", "weekly")
RUNS_DIR = os.path.join(BASE, "output", "weekly_runs")

# --------------------------------------------------------------------------- #
# The knobs
#
# Deliberately NOT in config.py: config.py is hashed into `score_version`, so a
# threshold that has nothing to do with scoring would mark the whole scored book
# as a different vintage. These are scan-policy, not scoring calibration.
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# The search mandate — WHAT the owner is actually buying.
#
# Stated 2026-07-28; mirrored in the store's home-buying README (projects/home/buying/). Two buyers,
# two budgets, one shared region constraint. Everything else here is policy
# about HOW to scan; this is the only block that says WHAT to look for, so it
# is the first thing to change when the hunt changes.
#
# OCR (D16-D28) is a hard filter, not a preference. It also happens to be where
# the backtest points: realized forward OCR +3.7%/yr > RCR +3.1 >> CCR +1.9,
# and region is the strongest single forward signal in the model.
# --------------------------------------------------------------------------- #
# OCR is D16-D28, minus four the owner ruled out as "too far and never make
# money" (2026-07-28). Which four is a measured call, not a vibe — from the
# repo's own URA resale prints (data/ura_district_D*.csv, resale-only):
#
#   D24 Lim Chu Kang/Tengah — 860 prints, ALL New Sale, ZERO resales. No resale
#       market exists, so there is no exit liquidity and no evidence anyone has
#       ever resold at a profit. Disqualifying for a resale-profit mandate.
#   D25 Woodlands/Admiralty — 591 resales across just 7 projects, the thinnest
#       book in OCR, and the lowest median psf (~$1,151).
#   D27 Sembawang/Yishun — weakest trailing 3y (+3.0%/yr) of any OCR district.
#   D17 Changi/Loyang — far east, thin (33 projects), low psf (~$1,254), +4.4%.
#
# Honesty about the evidence: trailing CAGR is WEAK grounds on its own — the
# repo's backtest puts its marginal forward power near zero once clustered
# ("story, not signal"). The load-bearing argument here is DEPTH, which does
# survive clustering (buyer-pool std_β +0.16): D24 and D25 are excluded because
# their resale markets are absent or tiny, which is a liquidity fact, not a
# forecast. D27 and D17 are the softer calls, resting more on the trailing
# numbers — revisit them if the search comes up empty.
#
# Kept despite a low trailing print: D16 Bedok (+3.9%) — it is not "far", and
# it is one of the deepest books in OCR (3,309 resales, 70 projects), so exit
# liquidity is excellent. Depth outranks a 3-year window.
OCR_DISTRICTS = [16, 18, 19, 20, 21, 22, 23, 26, 28]
OCR_EXCLUDED = {17: "far east, thin, low psf",
                24: "no resale market at all (Tengah)",
                25: "thinnest book in OCR (7 projects)",
                27: "weakest trailing returns in OCR"}

MANDATES = [
    # label      max_price   beds        who
    ("his",  1_800_000, (3,),    "him — up to ~$1.8M (flexible), 3BR"),
    ("hers", 2_500_000, (3, 4),  "her — ~$2.5M, 3-4BR, whichever returns most"),
]

# A fork's buyer replaces the block above WITHOUT editing it: a git-ignored
# profile.local.json (buyer_profile.py) supplies the mandates and the district
# list. No profile = the owner's block, unchanged. The name OCR_DISTRICTS is
# kept because the gate and the URA refresh read it; with a profile it holds
# whatever districts that buyer chose.
import buyer_profile as _buyer_profile

PROFILE = _buyer_profile.active()
if PROFILE is not None:
    MANDATES = [m.as_tuple() for m in PROFILE.mandates]
    OCR_DISTRICTS = list(PROFILE.districts)
    OCR_EXCLUDED = {}

# Union of the mandates, used for the scrape itself.
SCAN_DISTRICTS = OCR_DISTRICTS
SCAN_BEDS = sorted({b for _, _, beds, _ in MANDATES for b in beds})
SCAN_MAX_PRICE = max(p for _, p, _, _ in MANDATES)
# A little headroom over the top budget: an ask slightly above it is still worth
# seeing (asks negotiate down), whereas anything far above is noise.
SCAN_PRICE_HEADROOM = 1.06


def district_num(district) -> int | None:
    """'D19' / 'd19' / 19 -> 19. None when it can't be read."""
    digits = "".join(c for c in str(district or "") if c.isdigit())
    return int(digits) if digits else None


def in_region(district) -> bool:
    """True if the district is in the mandate's region (OCR).

    Enforced at the GATE, not just in the scrape scope. The scrape scope only
    controls what a fresh poll fetches; the DB also holds thousands of older
    out-of-region listings, and re-gating without this check would happily
    shortlist RCR stock nobody is looking for. Unknown district = not in scope:
    the mandate is a hard filter, so an unreadable district fails closed.
    """
    n = district_num(district)
    return n is not None and n in OCR_DISTRICTS


def mandate_summary() -> str:
    """'3BR<=$1.8M or 3-4BR<=$2.5M' — derived, never hand-written.

    This string goes into every off-mandate rejection line in the digest. It was
    hardcoded once and immediately went stale when his ceiling moved $1.5M ->
    $1.8M, so every rejection told the reader the wrong budget. Deriving it from
    MANDATES means the explanation cannot disagree with the rule again.
    """
    parts = []
    for _, max_price, beds, _ in MANDATES:
        bed_txt = "-".join(str(b) for b in sorted(beds))
        parts.append(f"{bed_txt}BR<=${max_price / 1e6:.1f}M")
    return " or ".join(parts)


def mandate_for(price, beds, district=None) -> str | None:
    """Which buyer's mandate a listing fits ('his' / 'hers'), or None.

    Cheapest-fitting mandate wins, so a $1.4M 3BR is tagged 'his' rather than
    'hers' — it is the tighter budget's find, and hers has better options at
    that price. When `district` is given it must also be in region; callers
    that only have price/beds can omit it and check `in_region` separately.
    """
    if price is None or beds is None:
        return None
    if district is not None and not in_region(district):
        return None
    for label, max_price, ok_beds, _ in MANDATES:
        if beds in ok_beds and price <= max_price:
            return label
    return None


# The AI gate's mmr bar. 650 is the repo's own "recommended tier" (CLAUDE.md)
# and sits at roughly the 85th percentile of the scored book (measured
# 2026-07-31, 9,029 scored active listings) — a listing below it is not worth
# an agent run just for showing up. Since the lens registry landed this is
# ONE lens's bar, not the whole gate: any registered lens can nominate a
# candidate over its own bar (see lens_bars / scoring/lenses.py).
MIN_SCORE_FOR_AI = 650

# Hard cap on agent runs per scan. The gate alone is unbounded: a bulk relist or
# a newly-covered district could put 40 listings over 650 in one week.
#
# 8, not the 5 a daily cadence used: a weekly scan sweeps ~7 days of inventory,
# so the same cap would be ~7x stingier per listing seen. Still far below a
# daily run's 35/week — weekly is the cheaper cadence either way.
#
# Raised to 12 on 2026-07-31, because the cap is the binding constraint on
# COVERAGE, not a safety valve. Measured on the live book: 426 in-mandate
# listings clear the gate, which dedupe to 137 (condo x bed count) cohorts
# across 107 distinct condos — and 26 condos had been researched. At 8/scan the
# 30-day cooldown rotates through 107 condos in ~13 weeks; at 12 it is ~9. The
# real cost is wall clock, not tokens: agent runs are serial at roughly 8
# minutes each, so a 12-run scan takes ~100 minutes (measured 2026-07-29:
# 20:55 -> 22:34 for exactly 12). That is tolerable for a hand-run weekly scan
# and is why this is not higher.
MAX_AI_RUNS_PER_SCAN = 12

# Pages per (district, beds) to pull. PropertyGuru search is date-desc, so this
# is really "how far back does one scan reach". The poller's default of 2 is
# tuned for 6-hourly polling; a week of listings needs deeper pagination or the
# older half of the week silently never gets seen.
#
# 10, raised from 5 on 2026-07-31 — and the raise is a CORRECTION, not an
# expansion. Until that day `max_pages` was a floor: the scraper read
# total_pages after page 1 and silently extended to min(total_pages, 15). So
# the 2026-07-28 poll asked for 5 and actually fetched 2,462 listings across 18
# scopes — about 6.8 pages per scope, well past its stated budget. Making the
# cap real (scrapers/propertyguru.py) would therefore have QUIETLY CUT reach by
# roughly a third while looking like a safety fix. 10 sits above the measured
# 6.8 so the cap binds rarely, and unlike the old behaviour it is a budget the
# caller actually controls. Cost at the ceiling: 18 x 10 = 180 page loads,
# ~2s apart, so about 15 minutes of scraping.
POLL_MAX_PAGES = 10

# How old a sighting may be and still earn an agent run. A record is `active`
# until the staleness sweep demotes it, and that sweep is deliberately
# conservative: it only judges listings whose first_seen falls strictly inside
# the recency window a poll demonstrably covered, because the poll reads only
# the newest POLL_MAX_PAGES pages and absence of deep-page stock proves nothing.
#
# Sound for the delta scan, which only ever looks at this cycle's arrivals. But
# --full-book gates the ENTIRE book, which promotes never-re-verified stock to
# candidate: on the 2026-07-29 sweep, 3 of 12 agent runs (~25 min) went to
# listings last seen 2026-06-07/08 with times_seen=1, absent from the 07-28 poll
# of their own district and bed count. Worse than the wasted time, a listing that
# no longer exists can reach the buy list looking live.
#
# So this is not a claim that an old sighting is dead — it is a refusal to spend
# a research run on one we cannot confirm. Three weekly cycles: long enough that
# a listing merely pushed off page 5 by newer inventory gets re-seen first.
MAX_SIGHTING_AGE_DAYS = 21

# Don't re-analyze the same (condo, bedroom count) inside this window — a fresh
# listing of an already-judged unit type rarely changes the call, and eval memory
# already holds the reasoning. A different bed count IS a different call.
RE_EVAL_COOLDOWN_DAYS = 30

# Per-agent wall clock. The analyze-listing flow does real web research; beyond
# this something is stuck and the run is killed so the cycle finishes.
AI_TIMEOUT_S = 1800

# Agent runs in flight at once. Serial was the old rule ("keeps logs legible"),
# but each run is ~3-4 min of mostly network wait and every write path it
# touches is already concurrency-safe: run dirs are claimed atomically
# (invest.next_run_dir), eval memory saves under a per-condo lock, the DB and
# history CSVs under file locks. 3 turns the 2026-09-18 scan's 39 min AI phase
# into ~13 without hammering any one site. Same-condo candidates still run one
# after another (see run_agents) so they never race one condo file.
AI_PARALLEL = 3

# URA transaction CSVs older than this are re-fetched for the mandate
# districts before the poll scores anything. Every value signal the gate reads
# (psf premium vs own prints, floor-aware comps, trailing CAGR) is computed from
# this data, and on 2026-09-24 it was 106 days old — latest print May-26 — so
# three months of clearings were invisible to the algo while the agents quoted
# them. That gap is how asks above their own recent prints kept clearing the
# gate. Monthly matches URA's own publication lag.
URA_MAX_AGE_DAYS = 30

# Pre-gate enrichment. The poll detail-enriches only the first 20 new keys,
# in scrape order, so the listings that actually reach the agent usually have
# no floor level: "floor unknown (~10% psf spread by tier)" appears in most
# 2026-09-18 verdicts. So the gate runs twice — once uncapped to find who is
# in contention, then the top PRE_GATE_ENRICH of those get a detail visit and
# a re-score (floor-aware comps, PES/loft flags, low-floor demotion) before
# the real, capped gate picks. 3x the cap leaves room for re-ranking to
# matter; ~2-4s per detail page.
PRE_GATE_ENRICH = 36

# What the headless agent may touch — scoped, mirroring dashboard.py's ANALYZE.
AI_ALLOWED_TOOLS = "Bash,Read,Write,Edit,Glob,Grep,WebSearch,WebFetch,Skill,TodoWrite"

# Store push bar: only these reach the personal data store's scan-finds note.
STORE_PUSH_RATINGS = {"strong buy", "buy"}
STORE_PUSH_CONFIDENCE = {"high", "medium"}
_STORE = store_root()
STORE_FINDS_PATH = os.environ.get(
    "PF_STORE_FINDS",
    os.path.join(_STORE, "projects", "home", "buying", "scan-finds.md"))
# The note that reminds the owner to run this. A successful scan stamps its
# `last_run:` so the store's session-start reminder clears itself.
STORE_SCAN_NOTE_PATH = os.environ.get(
    "PF_STORE_SCAN_NOTE",
    os.path.join(_STORE, "projects", "property-finder", "weekly-scan.md"))

_RATING_ICON = {"strong buy": "🟢", "buy": "🟢", "neutral": "🟡", "avoid": "🔴"}

# realsmart.sg REALSCORE lookup. The PUBLIC /p/<slug> page carries it and needs
# no login or browser (the /map SPA is the one that does) — so the agent reads it
# with the store's plain fetcher. One page per shortlisted project, <=8 a week:
# realsmart's ToS is personal-use, so this stays a research lookup and must never
# become a per-listing pipeline feed.
WEB_EXTRACT_FETCH = fetch_script("fetch.py")


def realsmart_url(project_name: str | None) -> tuple[str, bool]:
    """(url, resolved) for a project's realsmart page.

    Resolved against the cached sitemap slug index (`realsmart.py`), because
    guessing gets it wrong often enough to matter — PropertyGuru writes "West
    Bay Condo" where realsmart has "west-bay-condominium", and the guess 404s.
    Falls back to the naive guess with resolved=False, which the prompt then
    labels as unverified so the agent knows to check the page identity before
    trusting a score.
    """
    try:
        import realsmart
        return realsmart.url_for(project_name or "")
    except Exception:  # noqa: BLE001 — a missing/broken index must not stop a scan
        slug = re.sub(r"[^a-z0-9]+", "-", (project_name or "").lower()).strip("-")
        return f"https://realsmart.sg/p/{slug}", False


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def deep_scope(state: dict) -> tuple[list, list]:
    """The one (district, beds) scope to crawl to its real end this cycle.

    A routine poll reads only the newest POLL_MAX_PAGES pages per scope, so
    anything deeper is never re-seen — which is why 5,378 of 9,040 `active`
    records were last sighted in June and why the gate now refuses to spend
    agent runs on them (MAX_SIGHTING_AGE_DAYS). Those records cannot age out
    either: sweep_staleness only judges listings inside the recency window a
    poll demonstrably covered, and a 5-page poll never covers them.

    So one scope per cycle gets `allow_extend` and is crawled to its end. With
    18 scopes that re-verifies every scope's deep stock about every 18 weeks —
    slow, but it is the only mechanism that closes the loop at all, and it adds
    one scope's pages rather than 18x the budget. Round-robin by cycle count so
    no scope is favoured.
    """
    scopes = [([d], [b]) for d in SCAN_DISTRICTS for b in SCAN_BEDS]
    n = len(state.get("history") or [])
    return scopes[n % len(scopes)]


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def iso_week(d: datetime) -> str:
    """ISO year-week key, e.g. '2026-W31'. Weeks start Monday."""
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


def already_done_this_week(state: dict, week: str) -> bool:
    """True only if this ISO week already got a SUCCESSFUL poll in.

    A failed poll (the usual cause: an unsolved Cloudflare challenge) leaves the
    week open, so re-running is the retry — no flag needed.
    """
    last = state.get("last_run") or {}
    return last.get("week") == week and bool(last.get("poll_ok"))


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #
def _eval_cooldown_index(cooldown_days: int, today: datetime) -> dict:
    """Map (condo slug, beds) -> most recent evaluation date inside the window.

    Beds come from each history entry's `as_of`, so a 2BR verdict never blocks a
    3BR in the same project. Entries without a usable bed count fall back to a
    wildcard key that blocks any bed count for that condo — the conservative
    reading of "we already looked at this place".

    Eval memory is append-only and years old: some legacy entries carry `as_of`
    as a free-text note rather than the {price, beds, …} dict, and a hand-edited
    file can hold anything. Nothing in here may raise — a malformed entry
    degrades to the wildcard, it does not stop the scan.
    """
    index: dict[tuple[str, object], str] = {}
    cutoff = (today - timedelta(days=cooldown_days)).strftime("%Y-%m-%d")
    try:
        slugs = eval_memory.load_index().get("condos", {}).keys()
    except Exception:  # noqa: BLE001 — a broken index must not stop the scan
        return index
    for slug in slugs:
        data = eval_memory.load_condo(slug) or {}
        for entry in data.get("history", []) or []:
            if not isinstance(entry, dict):
                continue
            when = entry.get("evaluated_at") or ""
            if not isinstance(when, str) or when < cutoff:
                continue
            as_of = entry.get("as_of")
            beds = as_of.get("beds") if isinstance(as_of, dict) else None
            key = (slug, beds if beds is not None else "*")
            if when > index.get(key, ""):
                index[key] = when
    return index


def _blocked_by_cooldown(index: dict, slug: str, beds) -> str | None:
    """Return the blocking evaluation date, or None if this unit type is open."""
    return index.get((slug, beds)) or index.get((slug, "*"))


# PropertyGuru's payload does not always carry a project name; when it doesn't,
# the scraper falls back to the listing TITLE — which is the agent's marketing
# headline ("Cheapest💎D03💎Best Value💎Freehold💎Duplex Penthouse💎"). ~15% of
# distinct names in the DB are these. They must never reach an agent: the URA
# comps, realsmart lookup, eval-memory slug and cooldown key are ALL keyed on
# project name, so the run researches nothing and permanently pollutes
# git-tracked memory with a junk slug.
_MARKETING_RX = re.compile(
    r"\b(cheapest|cheap|best\s*value|best\s*buy|must\s*sell|don'?t\s*miss|urgent|"
    r"rare(\s*find)?|below\s*valuation|undervalued?|steal|doorstep|key\s*collection|"
    r"walk(ing)?\s*to|near\s*mrt|min(s)?\s*to|\d\s*km|1km|rental\s*yield|high\s*floor|"
    r"brand\s*new|freehold|renovated|unblocked|bedder|bdrm|psf|amenities|"
    r"price[d]?\s*to\s*sell|for\s*sale|value[\s.-]*buy|super\s|top\s*soon)\b",
    re.I)
_EMOJI_RX = re.compile("[\U0001F000-\U0001FAFF☀-➿️⭐]")


def looks_like_marketing_title(name: str, known_projects: set | None = None) -> bool:
    """True when `name` is a listing headline rather than a development name.

    Deliberately two-sided. The heuristics alone would risk demoting a real
    development with a loud name, so an exact match against a known URA project
    RESCUES any name — the government's own project list outranks any guess we
    make here. Conversely a name with no URA match is not condemned on that
    basis alone (genuine new launches have no prints yet); it needs to actually
    read as marketing copy.
    """
    n = (name or "").strip()
    if not n:
        return True
    if known_projects and n.lower() in known_projects:
        return False
    return bool(_EMOJI_RX.search(n)) or "!" in n or len(n) > 45 \
        or bool(_MARKETING_RX.search(n))


def _repriced_since(rec: dict, since: str) -> bool:
    """Did this listing record a price change on/after `since`?

    Only counts as a re-price if there is more than one price_history entry —
    the first is the listing's original ask, not a change.
    """
    hist = rec.get("price_history") or []
    if not isinstance(hist, list) or len(hist) < 2:
        return False
    for entry in hist[1:]:
        if isinstance(entry, dict) and str(entry.get("date") or "") >= since:
            return True
    return False


def _bed_mismatch(project: str, beds, sqft) -> dict | None:
    """A confirmed bedroom relabel for this project, or None.

    Only `mismatch` blocks — the size is below the project's own band for that
    bed count AND lands in a smaller one, which is the 2BR-sold-as-3BR pattern.
    `oversize`, `undersize` and `unknown` all pass: a roomy unit is not a
    mislabel, and most projects have no rental history to judge by. Never
    raises — a missing band file must not stop a scan.
    """
    try:
        import bed_bands
        v = bed_bands.check(project, beds, sqft)
    except Exception:  # noqa: BLE001 — advisory check, never fatal
        return None
    return v if v.get("verdict") == "mismatch" else None


def _sighting_age_days(last_seen) -> int | None:
    """Days since this listing was last actually returned by a poll, or None.

    None for anything unparseable — a missing or malformed date must not be
    read as "stale", because the gate would then silently drop the record.
    """
    if not isinstance(last_seen, str):
        return None
    try:
        return (datetime.now() - datetime.strptime(last_seen[:10], "%Y-%m-%d")).days
    except ValueError:
        return None


def _bed_contested(project: str, beds, sqft) -> dict | None:
    """A bed count that fits better than the advertised one, or None.

    Advisory, not a gate. `mismatch` means the buyer is being offered fewer
    bedrooms than advertised and blocks; `contested` means the size sits in a
    different format's band, which usually explains an apparent psf discount as
    size-mix rather than value. That is the analyst's call to make with the
    project's own prints in front of them, so it is passed to the agent as
    evidence rather than used to drop the listing. Never raises.
    """
    try:
        import bed_bands
        return bed_bands.check(project, beds, sqft).get("contested")
    except Exception:  # noqa: BLE001 — advisory check, never fatal
        return None


def _known_projects() -> set:
    """Lowercased URA project names — the rescue list. Never raises."""
    try:
        with open(os.path.join(DATA_DIR, "ura_cache.json")) as f:
            data = json.load(f)
        return {str(k).lower() for k in (data.get("projects") or data).keys()}
    except (OSError, json.JSONDecodeError, AttributeError):
        return set()


# --------------------------------------------------------------------------- #
# Lenses at the gate. Multiple independent scorers (scoring/lenses.py) can
# each nominate a candidate; a listing is score-eligible when it clears ANY
# lens's bar. Deliberately an OR, never an average or a rank fusion — folding
# lenses back into one consensus number is the operation that let score_1000
# (r ~ -0.7 against realized exit profitability) monopolize the research
# budget in the first place.
#
# A lens in NO_SOLO still co-nominates (it labels and drafts candidates
# another lens also picked) but cannot spend an agent run alone. exit_demand
# went in on 2026-09-29: 8 solo picks over four scans (2026-08-12 .. 09-29),
# 0 better than Neutral, 5 of them Avoid. Remove it once the lens changes.
NO_SOLO = {"exit_demand"}
# --------------------------------------------------------------------------- #
def _lenses():
    from scoring import lenses
    return lenses


def lens_bars(db_listings: dict, min_score: int = MIN_SCORE_FOR_AI) -> dict:
    """Per-lens nomination bars over the scored book. {lens: bar or None}.

    mmr keeps the ABSOLUTE bar (`min_score`, default 650): it is the repo's
    recommended tier, the --min-score CLI knob, and the pre-lens gate must be
    reproduced exactly (regression-pinned in tests/test_lenses.py). Every
    other lens has no tier tradition, so its bar is its own gate_percentile of
    the scored book — the same construction that put 650 in the book's top
    ~15%, computed HERE and not in config.py (a constant there would restamp
    all 9,029 stored scores via score_version; see scoring/lenses.py).

    None means the lens has scored too little of the book to hold a bar, so it
    cannot nominate anyone this scan — a brand-new lens earns its gate by
    scoring the book first, not by guessing.
    """
    lenses = _lenses()
    books = lenses.book_scores(db_listings.values())
    bars: dict = {}
    for name, lens in lenses.LENSES.items():
        if name == "mmr":
            bars[name] = float(min_score)
            continue
        book = books.get(name) or []
        if len(book) < lenses.MIN_BOOK_FOR_RANK:
            bars[name] = None
        else:
            bars[name] = book[min(len(book) - 1,
                                  int(lens.gate_percentile * len(book)))]
    return bars


def _cand_lens_scores(row: dict, rec: dict) -> dict:
    """Per-lens scores for one candidate. Absent key = unscored by that lens.

    mmr reads the poll row's score_1000 (fresh this cycle — the DB flat field
    is the same number, but the row survives even when the record merge is
    racing); other lenses read the DB record's stored lens_scores.
    """
    out: dict = {}
    if row.get("score_1000") is not None:
        out["mmr"] = row["score_1000"]
    registered = _lenses().LENSES
    for name, entry in (rec.get("lens_scores") or {}).items():
        if name == "mmr" or name not in registered:
            continue
        if isinstance(entry, dict) and entry.get("score") is not None:
            out[name] = entry["score"]
    return out


def _below_bars_reason(lens_view: dict, bars: dict) -> str:
    """Why NO lens nominated this listing — every lens states its shortfall.

    The gate is never a black box: "below every lens's bar (mmr 620 < 650;
    exitrec 0.41 < 0.62)" tells the digest reader exactly which lens came
    closest and by how much, which is what makes a bar worth arguing with.
    """
    parts = []
    for name, bar in bars.items():
        s = lens_view.get(name)
        if s is None:
            parts.append(f"{name} unscored")
        elif bar is None:
            parts.append(f"{name} has no bar yet")
        else:
            parts.append(f"{name} {s:g} < {bar:g}")
    return f"below every lens's bar ({'; '.join(parts)})"


def lens_endorsements(rec: dict, min_score: int = MIN_SCORE_FOR_AI) -> list:
    """Which lenses currently vouch for this DB record — the display read.

    Used by shortlist rows and the shortlist UI to say WHICH lens surfaced a
    candidate. mmr endorses at the absolute recommended tier; other lenses
    endorse when the record's STORED rank_norm clears their gate_percentile.
    The scan gate recomputes bars over the live book; this uses the rank
    stamped at scoring time, which is accurate enough for a chip and keeps
    the viewers pipeline-free (they must never load the whole book).
    """
    out = []
    for name, lens in _lenses().LENSES.items():
        if name == "mmr":
            if (rec.get("score_1000") or 0) >= min_score:
                out.append(name)
            continue
        entry = (rec.get("lens_scores") or {}).get(name)
        rn = entry.get("rank_norm") if isinstance(entry, dict) else None
        if rn is not None and rn >= lens.gate_percentile:
            out.append(name)
    return out


def select_candidates(
    rows: list[dict],
    db_listings: dict,
    cooldown_index: dict | None = None,
    min_score: int = MIN_SCORE_FOR_AI,
    max_ai: int = MAX_AI_RUNS_PER_SCAN,
    known_projects: set | None = None,
) -> tuple[list[dict], list[dict]]:
    """Split the scan's new/changed listings into (shortlist, rejected).

    Pure over its inputs so the policy is testable without a poll. `rows` are
    poll-state rows (id / project_name / score_1000 / …); each is joined to its
    DB record for the fields the gate needs. Every rejected row carries a
    `gate_reason` — the digest prints them, so the gate is never a black box.

    The score check is an ANY-LENS gate: a listing is score-eligible when it
    clears at least one registered lens's bar (lens_bars), and the lenses that
    nominated it are recorded as `surfaced_by`. With only "mmr" registered
    this is exactly the old `score_1000 >= min_score` rule (golden-pinned in
    tests/test_lenses.py against the pre-lens gate's output on a real-book
    sample).

    Order of checks matters: cheap data-quality rules first, then the score,
    then the two rules that need the rest of the batch (in-batch dedupe, cap).
    """
    cooldown_index = {} if cooldown_index is None else cooldown_index
    shortlist: list[dict] = []
    rejected: list[dict] = []
    bars = lens_bars(db_listings, min_score)

    ranked = sorted(rows, key=lambda r: r.get("score_1000") or 0, reverse=True)
    seen_cohort: set[tuple[str, object]] = set()
    eligible: list[dict] = []

    for row in ranked:
        rec = db_listings.get(row["id"]) or {}
        cand = {**row,
                "unit_group": rec.get("unit_group"),
                "ingest_flags": rec.get("ingest_flags") or [],
                "tenure": rec.get("tenure"),
                "sqft": rec.get("sqft"),
                "status": rec.get("status")}
        lens_view = _cand_lens_scores(row, rec)
        surfaced = [n for n, bar in bars.items()
                    if bar is not None and lens_view.get(n) is not None
                    and lens_view[n] >= bar]
        cand["lens_view"] = lens_view
        cand["surfaced_by"] = surfaced
        name = row.get("project_name") or rec.get("title") or ""
        slug = eval_memory.slugify(name) if name else ""
        cohort = (slug, row.get("beds"))
        district = row.get("district") or rec.get("district")
        mandate = mandate_for(row.get("price"), row.get("beds"), district)
        cand["mandate"] = mandate

        age = _sighting_age_days(rec.get("last_seen"))
        cand["sighting_age_days"] = age

        if rec.get("status") == "stale":
            cand["gate_reason"] = "listing went stale this cycle"
        elif age is not None and age > MAX_SIGHTING_AGE_DAYS:
            # Still `active` only because the staleness sweep cannot judge
            # deep-page stock — not because anyone confirmed it is live.
            cand["gate_reason"] = (
                f"not re-seen since {str(rec.get('last_seen'))[:10]} ({age}d, seen "
                f"{rec.get('times_seen') or 0}x) — unverified, so not worth an agent run")
        elif not (rec.get("price") and rec.get("sqft") and rec.get("psf")):
            cand["gate_reason"] = "incomplete record (price/sqft/psf)"
        elif not lens_view:
            cand["gate_reason"] = "not scored"
        elif not surfaced:
            cand["gate_reason"] = _below_bars_reason(lens_view, bars)
        elif not set(surfaced) - NO_SOLO:
            cand["gate_reason"] = (
                f"nominated by {', '.join(surfaced)} alone — solo picks paused "
                f"for it (NO_SOLO in weekly.py)")
        elif not in_region(district):
            cand["gate_reason"] = (
                f"out of region ({district or '?'} is not OCR — mandate is D16-D28)")
        elif mandate is None:
            # Off-mandate: nobody can buy it, so its score is irrelevant.
            cand["gate_reason"] = (
                f"outside the mandate ({row.get('beds')}BR @ "
                f"{_money(row.get('price'))} — need {mandate_summary()})")
        elif looks_like_marketing_title(name, known_projects):
            # Not a judgement on the unit — we simply don't know which
            # development it is, so there is nothing to research.
            cand["gate_reason"] = "no usable project name (listing headline, not a development)"
        elif (bed := _bed_mismatch(name, row.get("beds"), cand.get("sqft"))):
            # The bedroom count comes from the marketing agent, not a registry,
            # and 2BR+study routinely gets listed as 3BR. The mandate screens on
            # bed count, so an unchallenged relabel puts the wrong product in
            # front of the buyer entirely.
            cand["gate_reason"] = (
                f"bed count looks wrong — {bed['reason']}")
        elif (blocked := _blocked_by_cooldown(cooldown_index, slug, row.get("beds"))):
            cand["gate_reason"] = f"same unit type evaluated {blocked} (cooldown)"
        elif cohort in seen_cohort:
            cand["gate_reason"] = "same condo + bed count already shortlisted this scan"
        else:
            cand["slug"] = slug
            seen_cohort.add(cohort)
            eligible.append(cand)     # budget allocated below, not here
            continue

        rejected.append(cand)

    # --- Allocate the AI budget across the buyers -------------------------- #
    # Two passes, because neither extreme is right on its own:
    #   * pure score ranking lets hers (a ~$1M larger budget, so systematically
    #     nicer stock) take every slot and his mandate never gets researched;
    #   * a hard per-buyer cap wastes slots in the common case where only one
    #     buyer has candidates this week.
    # So: give each mandate its fair share first, then hand any slots nobody
    # claimed to the best remaining listings regardless of buyer.
    shortlist, deferred = _allocate_budget(eligible, max_ai)
    rejected.extend(deferred)
    return shortlist, rejected


def _lens_rankings(eligible: list[dict]) -> dict:
    """{lens: {id(cand): rank}} — each lens's own ordering over the eligibles.

    Ranks are per-lens because lens scales are incommensurable (score_1000
    runs 0-1000, an exit-record lens might run 0-1): "this lens's 3rd pick"
    is comparable across lenses where the raw scores never are. Sorting is
    stable over the incoming (score_1000-desc) order, so ties — and the
    mmr-only case — reproduce the pre-lens ordering exactly.
    """
    rankings: dict = {}
    for name in _lenses().LENSES:
        cands = [c for c in eligible
                 if (c.get("lens_view") or {}).get(name) is not None]
        cands.sort(key=lambda c: -c["lens_view"][name])
        rankings[name] = {id(c): i for i, c in enumerate(cands)}
    return rankings


def _snake_draft(cands: list[dict], rankings: dict) -> list[dict]:
    """Order one mandate's candidates by a snake draft across lenses.

    Each lens drafts its own best remaining candidate BY ITS OWN RANK, in
    registry order, direction alternating each round (L1..Ln, then Ln..L1) so
    the last lens of round one is not also last in round two. A candidate
    surfaced by several lenses is taken once — the first lens to reach it
    spends the pick, and it leaves every other queue.

    This is deliberately NOT rank aggregation (no Borda, no RRF): averaging
    lenses back into one consensus dilutes the lone outlier a second lens
    exists to catch, which is the exact failure the registry replaces. With
    one lens registered the snake collapses to that lens's rank order —
    identical to the old score-ordered allocator.
    """
    lens_names = [n for n in _lenses().LENSES
                  if any(n in (c.get("surfaced_by") or []) for c in cands)]
    queues = {n: sorted((c for c in cands if n in (c.get("surfaced_by") or [])),
                        key=lambda c: rankings[n][id(c)])
              for n in lens_names}
    order: list[dict] = []
    taken: set[int] = set()
    forward = True
    while True:
        picked = False
        for n in (lens_names if forward else reversed(lens_names)):
            q = queues[n]
            while q and id(q[0]) in taken:
                q.pop(0)
            if q:
                c = q.pop(0)
                taken.add(id(c))
                order.append(c)
                picked = True
        if not picked:
            return order
        forward = not forward


def _allocate_budget(eligible: list[dict], max_ai: int) -> tuple[list[dict], list[dict]]:
    """Split `max_ai` slots: mandate fair-share OUTER, lens snake-draft INNER.

    Mandates stay the outer split — a second lens must not become a new way
    for hers' larger budget to crowd his out. WITHIN each mandate the fair
    share is drafted across lenses (_snake_draft), so every lens that surfaced
    candidates gets its say before any lens gets a second pick. A candidate
    surfaced by several lenses consumes one slot, once.

    `eligible` arrives in gate order (score_1000-desc). When the fair shares
    overshoot `max_ai` (odd cap, both mandates full) the weakest drafted picks
    are trimmed by best-lens-rank; leftovers then fill unclaimed slots in the
    same order. With only "mmr" registered every one of these orderings
    collapses to score order and the result is identical to the pre-lens
    allocator (pinned against a verbatim copy of it in tests/test_lenses.py).
    Returns (shortlist, deferred); deferred candidates carry a gate_reason
    naming the cap they hit.
    """
    present = [m for m, *_ in MANDATES if any(c.get("mandate") == m for c in eligible)]
    if not present:
        return [], []
    fair_share = max(1, -(-max_ai // len(present)))    # ceil, so 8/2 -> 4 each

    rankings = _lens_rankings(eligible)

    def best_rank(c: dict) -> int:
        # A candidate's strongest claim: the best rank ANY surfacing lens
        # gives it. Cross-lens comparable where raw scores are not.
        return min(rankings[n][id(c)] for n in (c.get("surfaced_by") or ["mmr"])
                   if id(c) in rankings.get(n, {}))

    order_index = {id(c): i for i, c in enumerate(eligible)}  # stable tiebreak

    # Pass 1: each mandate drafts its fair share; if the shares overshoot the
    # cap (odd max_ai, several full mandates), trim the weakest claims.
    drafted = [c for m in present
               for c in _snake_draft(
                   [c for c in eligible if c.get("mandate") == m],
                   rankings)[:fair_share]]
    drafted.sort(key=lambda c: (best_rank(c), order_index[id(c)]))
    shortlist = drafted[:max_ai]
    chosen = {id(c) for c in shortlist}

    # Pass 2: slots nobody claimed go to the best remaining listings
    # regardless of buyer or lens.
    deferred = []
    remaining = sorted((c for c in eligible if id(c) not in chosen),
                       key=lambda c: (best_rank(c), order_index[id(c)]))
    for cand in remaining:
        if len(shortlist) < max_ai:
            shortlist.append(cand)
        else:
            cand["gate_reason"] = f"over the per-scan AI cap ({max_ai})"
            deferred.append(cand)

    shortlist.sort(key=lambda c: c.get("score_1000") or 0, reverse=True)
    return shortlist, deferred


# --------------------------------------------------------------------------- #
# The AI step
# --------------------------------------------------------------------------- #
def _agent_prompt(cand: dict) -> str:
    flags = cand.get("ingest_flags") or []
    flag_note = (
        f"\nThe ingest flags on this record are {flags} — treat any apparent "
        "discount as unverified until you have checked it against the project's "
        "own recent (24mo) prints, per the trust rules in CLAUDE.md."
        if flags else ""
    )
    bed = _bed_contested(cand.get("project_name"), cand.get("beds"), cand.get("sqft"))
    bed_note = (
        f"\nBEDROOM COUNT IS CONTESTED — {bed['reason']}\n"
        f"Settle this FIRST, from the project's own URA prints and unit mix: an "
        f"advertised bed count is the marketing agent's word, and the size band a "
        f"unit really belongs to is what its psf should be compared against. If "
        f"the format is different from the label (PES/roof-terrace area, a "
        f"penthouse, a dual-key, or simply a smaller bed count), say so plainly "
        f"and re-read the discount against the RIGHT cohort before rating it.\n"
        if bed else ""
    )
    rs = cand.get("realsmart") or {}
    if rs.get("ok"):
        # Pre-fetched by prewarm_realsmart — the agent does not fetch at all.
        rs_block = (
            f"realsmart.sg REALSCORE for this project is ALREADY FETCHED (cached "
            f"{rs.get('fetched_on') or 'recently'} from {rs.get('url')}): "
            f"REALSCORE {rs.get('realscore')}, {rs.get('pct_profitable')}% "
            f"profitable, avg annualized {rs.get('annual_return_pct')}%, on "
            f"{rs.get('transactions') or '?'} matched resale pairs. Copy these into "
            "`realscore`, `realsmart_pct_profitable` and "
            "`realsmart_annual_return_pct` in agent_evaluation — do not re-fetch.\n")
    else:
        rs_url, rs_resolved = realsmart_url(cand.get("project_name"))
        rs_note = "" if rs_resolved else (
            "(NOTE: this URL is a GUESS — the project is not in realsmart's sitemap "
            "index, so verify the page is actually this development, or skip it.)\n")
        rs_block = (
            f"REQUIRED in step ② Gather — realsmart.sg REALSCORE for this project "
            f"(the pre-scan lookup found nothing, so try once yourself):\n"
            f"    uv run --quiet --script {WEB_EXTRACT_FETCH} \"{rs_url}\"\n"
            f"{rs_note}"
            "Public page, plain fetch, no login. Read off REALSCORE (0-5 "
            "profitability rank), the '% Profitable' badge, avg annualized profit, "
            "and the transaction COUNT behind them, then fill `realscore`, "
            "`realsmart_pct_profitable` and `realsmart_annual_return_pct` in "
            "agent_evaluation. Confirm the page's title really is this development "
            "before reading numbers off it. If it 404s or is a different project, "
            "leave the fields null and say so — never guess a number, and never "
            "attach another project's score to this listing.\n")
    return (
        f"Analyze this PropertyGuru listing per the /analyze-listing flow: "
        f"{cand.get('url')}\n\n"
        f"It is already scraped, detail-enriched and scored in the listings DB "
        f"(id {cand['id']}, {cand.get('project_name')}, "
        f"score_1000 {cand.get('score_1000')}). For step ② Gather, run\n"
        f"    python invest.py --from-db {cand['id']}\n"
        f"instead of `--url` — it builds the run dir and raw_analysis.json from "
        f"the DB record with no scraping (PropertyGuru is behind Cloudflare and "
        f"this is an unattended run).{flag_note}\n{bed_note}\n"
        f"{rs_block}"
        "Weigh REALSCORE as downside "
        "evidence (has this project ever lost owners money?), NOT as an "
        "appreciation forecast: it is backward-looking like trailing CAGR, and "
        "a perfect score on a handful of transactions means little — always "
        "report the transaction count with it. Uncompleted projects "
        "legitimately show N.A.\n\n"
        "This is a NON-INTERACTIVE headless run in the weekly scan: do not ask "
        "anything; where the flow would ask about purpose, assume investment "
        "(5-7yr hold) and proceed. Do the real research step — your view first, "
        "algo_reference second. An honest Neutral or Avoid is the expected "
        "outcome most weeks; never manufacture a Buy. Finish the full flow "
        "including --from-review so the verdict is saved to eval memory."
    )


def run_agent(cand: dict, timeout_s: int = AI_TIMEOUT_S) -> dict:
    """Run one headless `claude -p` analyze pass. Never raises.

    The deadline is enforced on the WALL CLOCK, not via subprocess.run(timeout=),
    and the child gets its own process group.

    Both details are load-bearing, learned the hard way: on the first real OCR
    scan an agent ran 1h45m against a 30-minute timeout that never fired,
    blocking the queue behind it. subprocess.run's timeout is measured with
    time.monotonic(), which on macOS does NOT advance while the system sleeps —
    so a laptop that naps mid-run pauses the timeout while real time keeps
    passing. Wall clock is what "this has been stuck for an hour" actually
    means. The process group matters because `claude` spawns children; killing
    only the direct child can leave orphans holding the work.
    """
    os.makedirs(RUNS_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")   # a same-second retry keeps its own log
    log_path = os.path.join(RUNS_DIR, f"{cand.get('slug') or cand['id']}_{stamp}.log")
    cmd = ["claude", "-p", _agent_prompt(cand), "--allowedTools", AI_ALLOWED_TOOLS]
    result = {"id": cand["id"], "log": os.path.relpath(log_path, BASE)}
    logf = None
    try:
        logf = open(log_path, "w")
        proc = subprocess.Popen(cmd, cwd=BASE, stdin=subprocess.DEVNULL,
                                stdout=logf, stderr=subprocess.STDOUT,
                                start_new_session=True)
    except (OSError, ValueError) as e:
        if logf:
            logf.close()
        result.update(ok=False, error=f"{type(e).__name__}: {e}")
        return result

    deadline = time.time() + timeout_s
    try:
        while True:
            rc = proc.poll()
            if rc is not None:
                result["returncode"] = rc
                result["ok"] = rc == 0
                break
            if time.time() >= deadline:
                _kill_group(proc)
                result.update(ok=False,
                              error=f"timed out after {timeout_s}s (wall clock)")
                break
            time.sleep(2)
    finally:
        logf.close()
    return result


def _analyze_one(cand: dict, day: str) -> tuple[list[dict], dict | None]:
    """One candidate end to end: run, read the verdict, retry once if none.

    The retry exists because transient API errors ("Connection closed
    mid-response") land AFTER the research is done but before the evaluation
    is written — losing the whole slot to a flaky connection. Slots are the
    scarce resource, so one retry is cheaper than dropping a candidate.
    Strictly one: a listing that genuinely defeats the flow must not loop.
    """
    runs = [run_agent(cand)]
    v = read_verdict(cand, on_or_after=day)
    if v is None:
        run = run_agent(cand)
        run["retry"] = True
        runs.append(run)
        v = read_verdict(cand, on_or_after=day)
    return runs, v


def run_agents(shortlist: list[dict], day: str,
               parallel: int = AI_PARALLEL) -> tuple[dict, list]:
    """Analyze the shortlist, `parallel` agents at a time. Returns (verdicts, runs).

    Candidates sharing a condo (a 3BR and a 4BR of the same project) go into
    one lane and run back to back: they append to the same eval-memory file,
    and read_verdict's newest-entry fallback must not pick up a sibling's
    verdict written moments earlier.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    lanes: dict[str, list[dict]] = {}
    for c in shortlist:
        lanes.setdefault(c.get("slug") or c["id"], []).append(c)
    total = len(shortlist)
    done = [0]
    verdicts: dict[str, dict] = {}
    all_runs: list = []

    def lane(cands: list[dict]) -> list[tuple[dict, list, dict | None]]:
        out = []
        for cand in cands:
            try:
                runs, v = _analyze_one(cand, day)
            except Exception as e:  # noqa: BLE001 — one bad run must not sink the lane
                runs = [{"id": cand["id"], "ok": False, "log": "-",
                         "error": f"{type(e).__name__}: {e}"}]
                v = None
            done[0] += 1
            tag = (f"{v.get('rating')} ({v.get('confidence')})" if v
                   else f"no evaluation saved — see {runs[-1]['log']}")
            print(f"  [{done[0]}/{total}] {cand.get('project_name')} "
                  f"{cand.get('beds')}BR ({cand.get('score_1000')}) → {tag}",
                  file=sys.stderr)
            out.append((cand, runs, v))
        return out

    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        futures = [pool.submit(lane, cands) for cands in lanes.values()]
        for fut in as_completed(futures):
            for cand, runs, v in fut.result():
                all_runs.extend(runs)
                if v:
                    verdicts[cand["id"]] = v
    # Keep the state file's run list in shortlist order, not finish order.
    order = {c["id"]: i for i, c in enumerate(shortlist)}
    all_runs.sort(key=lambda r: order.get(r["id"], 0))
    return verdicts, all_runs


# --------------------------------------------------------------------------- #
# Pre-flight and pre-gate: make the data the gate reads current
# --------------------------------------------------------------------------- #
def _csv_age_days(path: str) -> float | None:
    try:
        return (time.time() - os.path.getmtime(path)) / 86400
    except OSError:
        return None


def refresh_ura(districts: list[int], max_age_days: int = URA_MAX_AGE_DAYS) -> dict:
    """Re-fetch stale URA transaction CSVs for `districts`, then rebuild the cache.

    Delegates to fetch_ura_districts.py, which skips fresh districts, opens a
    headed browser only when something is stale, and MERGES the rebuilt
    projects into ura_cache.json (other districts keep their entries). Runs
    before the poll so every listing scored this cycle is benchmarked against
    current prints. Never raises: a failed refresh leaves last month's data in
    place, which is exactly where the scan was before — the digest says so.
    """
    # Official API first: every district, sales and rentals, ~40 s, no
    # browser; a no-op sync when URA hasn't published since the last one.
    try:
        import ura_api
        ok, why = ura_api.available()
    except Exception as e:  # noqa: BLE001
        ok, why = False, str(e)
    if ok:
        print("Refreshing URA data via the API (no browser)…", file=sys.stderr)
        try:
            res = ura_api.refresh(transactions=True, rentals=True)
        except Exception as e:  # noqa: BLE001 — reported, never fatal
            # NOT a fall-through to the browser: the API may have exported
            # fresh CSVs before failing, and their new mtimes would make the
            # browser path report "current" while caches never got rebuilt.
            print(f"  ⚠ URA API refresh failed ({e}) — scoring on the data as it stands",
                  file=sys.stderr)
            ages = [a for a in (_csv_age_days(os.path.join(
                DATA_DIR, f"ura_district_D{d:02d}.csv")) for d in districts) if a is not None]
            return {"ok": False, "via": "api", "error": f"{type(e).__name__}: {e}",
                    "oldest_days": round(max(ages)) if ages else None}
        # "ura_district_D19.csv: ..." -> 19 (transaction files only; a skipped
        # rental file is in the detail but doesn't make prints stale).
        stale = sorted({int(m.group(1)) for s in res.get("skipped_districts") or []
                        if (m := re.match(r"ura_district_D(\d+)\.csv", s))
                        and int(m.group(1)) in districts})
        return {"ok": not res.get("skipped_districts"), "via": "api",
                "refreshed": [d for d in districts if d not in stale],
                "oldest_days": 0, "detail": res,
                **({"still_stale": stale} if stale else {}),
                **({"error": "; ".join(res["skipped_districts"])}
                   if res.get("skipped_districts") and not stale else {})}
    else:
        print(f"  URA API unavailable ({why}) — using the browser", file=sys.stderr)

    ages = {d: _csv_age_days(os.path.join(DATA_DIR, f"ura_district_D{d:02d}.csv"))
            for d in districts}
    stale = [d for d, a in ages.items() if a is None or a > max_age_days]
    oldest = max((a for a in ages.values() if a is not None), default=None)
    out = {"stale": stale, "oldest_days": round(oldest) if oldest else None}
    if not stale:
        out["ok"] = True
        return out
    print(f"Refreshing URA prints for {len(stale)} district(s) "
          f"(oldest {out['oldest_days']}d; a browser window will open)…",
          file=sys.stderr)
    cmd = [sys.executable, os.path.join(BASE, "fetch_ura_districts.py"),
           "--districts", ",".join(str(d) for d in stale),
           "--max-age", str(max_age_days), "--browser"]
    try:
        proc = subprocess.run(cmd, cwd=BASE, timeout=1200)
        out["ok"] = proc.returncode == 0
    except (subprocess.TimeoutExpired, OSError) as e:
        out.update(ok=False, error=f"{type(e).__name__}: {e}")
    still = [d for d in stale
             if (_csv_age_days(os.path.join(DATA_DIR, f"ura_district_D{d:02d}.csv"))
                 or 1e9) > max_age_days]
    out["refreshed"] = [d for d in stale if d not in still]
    out["still_stale"] = still
    if still:
        out["ok"] = False
    return out


def pre_gate_enrich(rows: list[dict], db_listings: dict, cooldown: dict,
                    min_score: int, known: set, headless: bool,
                    limit: int = PRE_GATE_ENRICH) -> tuple[list[dict], dict]:
    """Detail-enrich and re-score the listings in contention, before the real gate.

    Runs the gate uncapped to find who is eligible, takes the top `limit` that
    have no floor level yet, visits their detail pages, re-scores them, and
    swaps the fresh scores into `rows`. Returns (rows, report). Best-effort:
    any failure returns the rows untouched.
    """
    report = {"attempted": 0, "enriched": 0, "rescored": 0}
    try:
        eligible, _ = select_candidates(rows, db_listings, cooldown_index=cooldown,
                                        min_score=min_score, max_ai=10_000,
                                        known_projects=known)
        # Rank by each candidate's best rank under ANY nominating lens, so an
        # exit_demand-only contender is enriched too, not just high mmr.
        ranks = _lens_rankings(eligible)
        eligible.sort(key=lambda c: min(
            (ranks[n][id(c)] for n in (c.get("surfaced_by") or ["mmr"])
             if id(c) in ranks.get(n, {})), default=10**9))
        # No latitude = the detail page was never visited (floor_level alone
        # is a bad test: posting agents leave it blank ~60% of the time).
        targets = [c["id"] for c in eligible
                   if not (db_listings.get(c["id"]) or {}).get("latitude")][:limit]
        if not targets:
            return rows, report
        print(f"Pre-gate: detail-enriching {len(targets)} contender(s) of "
              f"{len(eligible)} eligible, then re-scoring…", file=sys.stderr)
        enr = poller._enrich_new_keys(targets, headless=headless, cap=limit)
        report.update(attempted=enr["attempted"], enriched=enr["enriched"])
        _, scored = poller._score_keys(targets, record_history=False)
        report["rescored"] = len(scored)
        fresh = {r["id"]: r for r in scored}
        rows = [{**r, **fresh[r["id"]]} if r["id"] in fresh else r for r in rows]
    except Exception as e:  # noqa: BLE001 — a coverage bonus, never fatal
        report["error"] = f"{type(e).__name__}: {e}"
        print(f"  pre-gate enrichment failed ({e}) — gating on poll scores",
              file=sys.stderr)
    return rows, report


def prewarm_realsmart(shortlist: list[dict]) -> dict:
    """Fetch REALSCORE once per shortlisted project and attach it to each cand.

    Used to be step ② of every agent run — a fetch the agent had to make,
    parse and cross-check itself, and which failed on every 2026-09-18 run
    (wrong interpreter for the fetch script). realsmart_cache dedupes by
    project, caches for 45 days, and spaces requests politely. Never raises.
    """
    try:
        import realsmart_cache
        got = realsmart_cache.warm([c.get("project_name") for c in shortlist
                                    if c.get("project_name")])
    except Exception as e:  # noqa: BLE001 — the agent falls back to fetching
        print(f"  realsmart prewarm failed ({e}) — agents will fetch",
              file=sys.stderr)
        return {"ok": 0, "missing": len(shortlist)}
    ok = 0
    for c in shortlist:
        rec = got.get(c.get("project_name")) or {}
        # A score read off a GUESSED slug may belong to another development;
        # those go to the agent, which checks the page title before trusting it.
        if rec.get("ok") and rec.get("resolved"):
            ok += 1
            c["realsmart"] = {
                "ok": True, "url": rec.get("url"),
                "realscore": rec.get("realscore"),
                "pct_profitable": rec.get("pct_profitable"),
                "annual_return_pct": rec.get("annual_return_pct"),
                "transactions": rec.get("resale_txns"),
                "fetched_on": datetime.fromtimestamp(
                    rec.get("fetched_at") or time.time()).strftime("%Y-%m-%d"),
            }
    return {"ok": ok, "missing": len(shortlist) - ok}


def merge_deep_rows(poll_state: dict, deep: dict) -> None:
    """Fold the deep crawl's new/changed rows into the main poll's, deduped.

    They used to be dropped (only the main poll's rows were gated), and the
    week after they are no longer "new", so they never reached the gate.
    """
    seen = {r["id"] for r in (poll_state.get("new_listings") or [])
            + (poll_state.get("changed_listings") or [])}
    for key in ("new_listings", "changed_listings"):
        extra = [r for r in (deep.get(key) or []) if r["id"] not in seen]
        poll_state[key] = list(poll_state.get(key) or []) + extra
        seen.update(r["id"] for r in extra)


def keep_awake() -> subprocess.Popen | None:
    """Hold off idle sleep for as long as this scan runs (macOS).

    Not a nicety — it is the fix for the failure that wrecked the first real OCR
    scan. On battery the Mac took repeated 'Maintenance Sleep' naps mid-run;
    every sleeping agent lost its connection ("API Error: Connection closed
    mid-response") and 5 of 8 candidates came back with no verdict at all.

    `caffeinate -w <pid>` exits by itself when this process does, so the
    assertion can never outlive the scan and leave the machine awake. Silent
    no-op off macOS, and a failure to caffeinate is not worth aborting a scan
    over — it only makes sleep possible again, which is where we started.
    """
    if sys.platform != "darwin":
        return None
    try:
        return subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except (OSError, ValueError) as e:  # noqa: BLE001 — best effort by design
        print(f"  ⚠ could not hold off sleep ({type(e).__name__}: {e}); "
              "long agent runs may be interrupted", file=sys.stderr)
        return None


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGTERM the child's process group, then SIGKILL what survives."""
    for sig, grace in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


def read_verdict(cand: dict, on_or_after: str) -> dict | None:
    """Pull the verdict the agent just wrote back out of eval memory.

    Matches on the listing URL first (the precise join — `save_evaluations_from
    _review` stamps `source.url`), then falls back to the newest entry for that
    condo dated on/after the run. Returns None when the agent saved nothing,
    which is how a failed or hedged run shows up in the digest.
    """
    slug = cand.get("slug") or eval_memory.slugify(cand.get("project_name") or "")
    data = eval_memory.load_condo(slug) or {}
    fresh = [h for h in (data.get("history") or [])
             if isinstance(h, dict) and str(h.get("evaluated_at") or "") >= on_or_after]
    if not fresh:
        return None
    url = cand.get("url")
    for entry in reversed(fresh):
        if url and (entry.get("source") or {}).get("url") == url:
            return entry
    return fresh[-1]


# --------------------------------------------------------------------------- #
# Digest
# --------------------------------------------------------------------------- #
def _money(v) -> str:
    return f"${v:,.0f}" if isinstance(v, (int, float)) else "—"


def _realsmart(v: dict) -> dict:
    """realsmart fields out of a verdict, wherever the agent put them.

    `save_evaluations_from_review` copies agent_evaluation wholesale AND lifts
    some keys to the history entry's top level, so accept either shape.
    """
    ae = v.get("agent_evaluation") or {}
    return {k: (v.get(k) if v.get(k) is not None else ae.get(k))
            for k in ("realscore", "realsmart_pct_profitable",
                      "realsmart_annual_return_pct")}


def _realsmart_line(v: dict) -> str:
    rs = _realsmart(v)
    if rs["realscore"] is None and rs["realsmart_pct_profitable"] is None:
        return ""
    bits = []
    if rs["realscore"] is not None:
        bits.append(f"**REALSCORE {rs['realscore']}**/5")
    if rs["realsmart_pct_profitable"] is not None:
        bits.append(f"{rs['realsmart_pct_profitable']}% of resales profitable")
    if rs["realsmart_annual_return_pct"] is not None:
        bits.append(f"{rs['realsmart_annual_return_pct']}%/yr avg (past 1y)")
    return "realsmart: " + " · ".join(bits)


_MANDATE_TAG = {"his": "**HIS**", "hers": "**HERS**"}


def _row(c: dict) -> str:
    # `Lens` = which lens(es) nominated the candidate. Rejected rows keep a
    # dash — nothing vouched for them, which is usually the story.
    return (f"| {c.get('score_1000') or '—'} | "
            f"{','.join(c.get('surfaced_by') or []) or '—'} | "
            f"{_MANDATE_TAG.get(c.get('mandate'), '—')} | "
            f"{c.get('project_name') or '—'} | "
            f"{c.get('beds') or '—'}BR | {_money(c.get('price'))} | "
            f"{_money(c.get('psf'))} psf | {c.get('district') or '—'} |")


def build_digest(day: str, poll_state: dict, shortlist: list[dict],
                 rejected: list[dict], verdicts: dict, min_score: int,
                 max_ai: int, dry_run: bool, pushed: list[dict]) -> str:
    n_new = len(poll_state.get("new_listings") or [])
    n_changed = len(poll_state.get("changed_listings") or [])
    L = [f"# Weekly scan — {day} ({iso_week(datetime.strptime(day, '%Y-%m-%d'))})", ""]

    if poll_state.get("ok"):
        L.append(
            f"**Poll** · D{','.join(str(d) for d in poll_state.get('districts', []))} · "
            f"{','.join(str(b) for b in poll_state.get('beds', []))}BR · "
            f"{poll_state.get('scraped', 0)} scraped → "
            f"**+{poll_state.get('added', 0)} new**, "
            f"{poll_state.get('price_changes', 0)} price changes, "
            f"{poll_state.get('enriched', 0)} detail-enriched "
            f"({poll_state.get('duration_s', 0)}s)")
    elif poll_state.get("skipped"):
        L.append("**Poll** · skipped (`--no-poll`) — graded what was already in the DB")
    else:
        L.append(f"> ⚠️ **Poll FAILED** — `{poll_state.get('error')}`. "
                 "Everything below is from the DB as it stands; the week was NOT "
                 "marked done, so just run it again.")

    pf = poll_state.get("preflight") or {}
    ura, enr = pf.get("ura") or {}, pf.get("enrich") or {}
    bits = []
    if ura:
        bits.append(_ura_line(ura))
    if enr:
        bits.append(f"pre-gate: {enr.get('enriched', 0)}/{enr.get('attempted', 0)} "
                    f"contenders detail-enriched, {enr.get('rescored', 0)} re-scored"
                    + (f" (⚠️ {enr['error']})" if enr.get("error") else ""))
    if bits:
        L += ["", "**Pre-flight** · " + " · ".join(bits)]

    # The re-eval cooldown is deliberately LENS-AGNOSTIC: a fresh nomination
    # by a different lens is not a different question about the same unit
    # type, so it must not buy a second agent run inside the window.
    lens_names = list(_lenses().LENSES)
    gate_txt = (f"`score_1000 >= {min_score}`" if lens_names == ["mmr"] else
                f"any lens bar ({len(lens_names)} lenses; "
                f"mmr: `score_1000 >= {min_score}`)")
    L += ["",
          f"**Gate** · {gate_txt} · max {max_ai} AI runs/scan · "
          f"{RE_EVAL_COOLDOWN_DAYS}d re-eval cooldown",
          f"**Result** · {n_new} new + {n_changed} price-changed → "
          f"**{len(shortlist)} cleared the gate** → "
          f"{'0 analyzed (dry run)' if dry_run else f'{len(verdicts)} analyzed'}",
          ""]

    if verdicts:
        L += ["## AI verdicts", ""]
        for cand in shortlist:
            v = verdicts.get(cand["id"])
            if not v:
                L += [f"### ⚪️ No verdict saved — {cand.get('project_name')} "
                      f"({cand['id']})",
                      "The agent run did not write an evaluation — see its log.", ""]
                continue
            rating = (v.get("rating") or "?").strip()
            icon = _RATING_ICON.get(rating.lower(), "⚪️")
            via = ",".join(cand.get("surfaced_by") or [])
            L.append(f"### {icon} {rating} — {cand.get('project_name')} · "
                     f"{cand.get('beds')}BR {cand.get('sqft') or '?'} sqft · "
                     f"{_money(cand.get('price'))} ({_money(cand.get('psf'))} psf) · "
                     f"{cand.get('district')} · score {cand.get('score_1000')}"
                     + (f" (via {via})" if via else "") + " · "
                     f"{v.get('confidence') or '?'} confidence")
            if (rs := _realsmart_line(v)):
                L += ["", rs]
            if v.get("summary"):
                L += ["", f"> {v['summary']}"]
            if v.get("rating_rationale"):
                L += ["", f"**Why:** {v['rating_rationale']}"]
            for label, key in (("Red flags", "red_flags"), ("Catalysts", "catalysts")):
                items = v.get(key) or []
                if items:
                    L += ["", f"**{label}:**"] + [f"- {i}" for i in items]
            L += ["", f"[Listing]({cand.get('url')}) · "
                      f"eval memory: `evaluations/{cand.get('slug')}.json`", ""]

    if dry_run and shortlist:
        L += ["## Cleared the gate (dry run — no agent spawned)", "",
              "| Score | Lens | For | Project | Beds | Price | PSF | District |",
              "|---|---|---|---|---|---|---|---|"] + [_row(c) for c in shortlist] + [""]

    if rejected:
        L += [f"## Algo-only — {len(rejected)} not sent to the agent", "",
              "| Score | Lens | For | Project | Beds | Price | PSF | District | Why |",
              "|---|---|---|---|---|---|---|---|---|"]
        L += [_row(c) + f" {c.get('gate_reason', '—')} |" for c in rejected]
        L.append("")

    if pushed:
        L += ["## Pushed to the store", ""]
        L += [f"- **{p['project_name']}** — {p['rating']} "
              f"({p['confidence']} confidence) → `{os.path.basename(STORE_FINDS_PATH)}`"
              for p in pushed]
        L.append("")

    if not shortlist and not rejected:
        L += ["Nothing new or price-changed in scope this week.", ""]

    return "\n".join(L)


def _ura_line(ura: dict) -> str:
    """The digest's URA pre-flight line. Failure is checked FIRST: a partial
    refresh must read as a warning, never as 'refreshed'."""
    via = " via API" if ura.get("via") == "api" else ""
    age = (f"{ura['oldest_days']}d old" if ura.get("oldest_days") is not None
           else "of unknown age")
    if not ura.get("ok"):
        why = ura.get("error") or (
            f"still stale: D{','.join(str(d) for d in ura['still_stale'])}"
            if ura.get("still_stale") else "unknown")
        done = (f"; refreshed D{','.join(str(d) for d in ura['refreshed'])}"
                if ura.get("refreshed") else "")
        return f"⚠️ URA refresh incomplete{via} ({why}){done} — some prints {age}"
    if ura.get("refreshed"):
        return f"URA prints refreshed{via} D{','.join(str(d) for d in ura['refreshed'])}"
    return f"URA prints current (oldest {age})"


def write_digest(day: str, text: str) -> str:
    os.makedirs(DIGEST_DIR, exist_ok=True)
    path = os.path.join(DIGEST_DIR, f"{day}.md")
    with open(path, "w") as f:
        f.write(text)
    return path


# --------------------------------------------------------------------------- #
# Store push — only the genuinely good ones
# --------------------------------------------------------------------------- #
_TABLE_SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$")


def _insert_rows(existing: str, rows: list[str]) -> str:
    """Insert `rows` directly under the note's first markdown table header.

    Newest-first, and — the reason this isn't a plain append — the store note
    keeps explanatory prose BELOW its table. Appending at EOF would drop table
    rows after that prose and break both. Falls back to appending only if the
    note has no table at all.
    """
    lines = existing.split("\n")
    for i, line in enumerate(lines):
        if _TABLE_SEP.match(line) and i > 0 and lines[i - 1].lstrip().startswith("|"):
            return "\n".join(lines[:i + 1] + rows + lines[i + 1:])
    return existing.rstrip("\n") + "\n" + "\n".join(rows) + "\n"


def push_to_store(day: str, shortlist: list[dict], verdicts: dict,
                  path: str = STORE_FINDS_PATH) -> list[dict]:
    """Append Buy-grade finds to the store's scan-finds note. Returns what was added.

    The bar is intentionally high (Buy/Strong Buy at >= medium confidence) — the
    store note is a signal feed the owner actually reads, not a log. The full
    week, verdicts included, is always in the digest regardless.

    Never creates the file: if the note is missing the store is not where we
    think it is, and silently seeding a stray markdown file elsewhere is worse
    than skipping. Rows already present (same listing URL) are not re-added, so
    re-running a scan is safe.
    """
    good = []
    for cand in shortlist:
        v = verdicts.get(cand["id"])
        if not v:
            continue
        if (v.get("rating") or "").strip().lower() not in STORE_PUSH_RATINGS:
            continue
        if (v.get("confidence") or "").strip().lower() not in STORE_PUSH_CONFIDENCE:
            continue
        good.append((cand, v))
    if not good:
        return []

    if not os.path.exists(path):
        print(f"  ⚠ store note not found at {path} — skipping push "
              f"({len(good)} find(s) are in the digest only)", file=sys.stderr)
        return []

    try:
        with open(path) as f:
            existing = f.read()
    except OSError as e:
        print(f"  ⚠ could not read {path}: {e}", file=sys.stderr)
        return []

    added, lines = [], []
    for cand, v in good:
        url = cand.get("url") or ""
        if url and url in existing:
            continue
        summary = (v.get("summary") or v.get("rating_rationale") or "").strip()
        summary = summary.replace("|", "·").replace("\n", " ")
        if len(summary) > 320:
            summary = summary[:317].rstrip() + "…"
        rs = _realsmart(v)
        real = "—" if rs["realscore"] is None else f"{rs['realscore']}"
        if rs["realsmart_pct_profitable"] is not None:
            real += f" · {rs['realsmart_pct_profitable']}% prof"
        lines.append(
            f"| {day} | [{cand.get('project_name')}]({url}) | "
            f"{cand.get('beds')}BR {cand.get('sqft') or '?'} sqft | "
            f"{_money(cand.get('price'))} ({_money(cand.get('psf'))} psf) · "
            f"{cand.get('district')} | {cand.get('score_1000')} | {real} | "
            f"**{v.get('rating')}** ({v.get('confidence')}) | {summary} |")
        added.append({"project_name": cand.get("project_name"),
                      "rating": v.get("rating"), "confidence": v.get("confidence"),
                      "url": url})
    if not lines:
        return []

    # Keep the note's `updated:` frontmatter honest — the store's convention.
    body = _set_frontmatter(_insert_rows(existing, lines), "updated", day)
    _atomic_write(path, body)
    return added


def _atomic_write(path: str, body: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(body)
    os.replace(tmp, path)


def _set_frontmatter(text: str, key: str, value: str) -> str:
    """Rewrite one existing frontmatter key. Adds nothing, touches nothing else.

    Only rewrites a key that is already there — the store's notes own their own
    shape, and a scan script inventing frontmatter fields in them would be
    overreach.
    """
    if not text.startswith("---\n"):
        return text
    head, sep, rest = text.partition("\n---\n")
    if not sep:
        return text
    head = "\n".join(
        f"{key}: {value}" if ln.startswith(f"{key}:") else ln
        for ln in head.split("\n"))
    return head + sep + rest


def stamp_scan_note(day: str, path: str = STORE_SCAN_NOTE_PATH) -> bool:
    """Stamp `last_run:` on the store's reminder note so the reminder clears.

    This is what makes the store the driver: the note says when the scan is due,
    the scan says when it last ran, and the session-start hook does the
    subtraction. Best-effort — a missing store just means the reminder stays up.
    """
    if not os.path.exists(path):
        print(f"  ⚠ scan note not found at {path} — reminder not stamped",
              file=sys.stderr)
        return False
    try:
        with open(path) as f:
            text = f.read()
        updated = _set_frontmatter(_set_frontmatter(text, "last_run", day),
                                   "updated", day)
        if updated != text:
            _atomic_write(path, updated)
        return True
    except OSError as e:
        print(f"  ⚠ could not stamp {path}: {e}", file=sys.stderr)
        return False


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def check_environment(need_browser: bool, need_agents: bool) -> list[str]:
    """Fail fast on a machine that can't finish the scan. Returns fatal problems.

    A scan polls for ~10 minutes before it first needs the agent CLI, and the
    2026-09-18 run died on a missing patchright browser only after the URA step.
    A fresh clone also has no realsmart slug index (it is gitignored), which
    silently turned every REALSCORE lookup into a guessed URL; that one is fixed
    here rather than reported.
    """
    fatal = []
    if need_agents and not shutil.which("claude"):
        fatal.append("`claude` CLI not on PATH — AI analysis can't run (use --dry-run to grade only)")
    if need_browser:
        try:
            from patchright.sync_api import sync_playwright
            with sync_playwright() as pw:
                exe = pw.chromium.executable_path
            if not os.path.exists(exe):
                fatal.append("patchright Chromium missing — run: python -m patchright install chromium")
        except Exception as e:  # noqa: BLE001
            fatal.append(f"patchright unusable ({type(e).__name__}: {e}) — pip install -r requirements.txt")
    if not os.path.exists(STORE_FINDS_PATH):
        print(f"  ⚠ store not found ({STORE_FINDS_PATH}) — finds won't be pushed; set PF_STORE",
              file=sys.stderr)
    try:
        import realsmart
        age = _csv_age_days(realsmart.SLUG_FILE)
        if age is None or age > 30:
            n = realsmart.refresh_slugs()
            print(f"  realsmart slug index {'rebuilt' if age is None else 'refreshed'}: {n} condos",
                  file=sys.stderr)
    except Exception as e:  # noqa: BLE001 — REALSCORE is enrichment, never fatal
        print(f"  ⚠ realsmart slug refresh failed: {e}", file=sys.stderr)
    return fatal


def main() -> int:
    ap = argparse.ArgumentParser(description="Weekly PropertyGuru scan + AI triage")
    ap.add_argument("--force", action="store_true",
                    help="run even if this week already completed a successful poll")
    ap.add_argument("--dry-run", action="store_true",
                    help="poll, grade and write the digest, but spawn no agents")
    ap.add_argument("--no-poll", action="store_true",
                    help="skip the scrape — grade what already arrived since the last run")
    ap.add_argument("--full-book", action="store_true",
                    help="gate over EVERY active in-scope listing, not just this "
                         "cycle's new/re-priced ones — 'what is the best thing on "
                         "the market', rather than 'what is new this week'")
    ap.add_argument("--headless", action="store_true",
                    help="poll headless (Cloudflare usually blocks this — see module docs)")
    ap.add_argument("--min-score", type=int, default=MIN_SCORE_FOR_AI)
    ap.add_argument("--max-ai", type=int, default=MAX_AI_RUNS_PER_SCAN)
    ap.add_argument("--max-pages", type=int, default=POLL_MAX_PAGES,
                    help=f"pages per district — how far back one scan reaches "
                         f"(default {POLL_MAX_PAGES})")
    ap.add_argument("--districts", type=str, default=None,
                    help=f"override the mandate's districts (default OCR {SCAN_DISTRICTS})")
    ap.add_argument("--beds", type=str, default=None,
                    help=f"override the mandate's bed counts (default {SCAN_BEDS})")
    ap.add_argument("--max-price", type=int,
                    default=int(SCAN_MAX_PRICE * SCAN_PRICE_HEADROOM),
                    help="scrape ceiling; defaults to the top mandate budget + headroom")
    ap.add_argument("--no-deep", action="store_true",
                    help="skip this cycle's rotating deep-verification scope "
                         "(one scope crawled to its end so deep stock is re-seen)")
    ap.add_argument("--parallel", type=int, default=AI_PARALLEL,
                    help=f"agent runs in flight at once (default {AI_PARALLEL}; 1 = serial)")
    ap.add_argument("--no-refresh", action="store_true",
                    help="skip the pre-flight URA transaction refresh")
    ap.add_argument("--no-enrich", action="store_true",
                    help="skip pre-gate detail enrichment + re-score of the contenders")
    ap.add_argument("--no-store-push", action="store_true",
                    help="don't append good finds to the personal data store note")
    args = ap.parse_args()

    keep_awake()

    now = datetime.now()
    day = now.strftime("%Y-%m-%d")
    week = iso_week(now)
    state = load_state()

    if already_done_this_week(state, week) and not args.force:
        last = state["last_run"]
        print(f"Weekly scan already ran this week ({week}, "
              f"{last.get('finished_at')}) — digest: {last.get('digest')}. "
              f"Use --force to re-run.")
        return 0

    # The scrapers log page-by-page progress and Cloudflare waits; without a
    # handler the poll was ~10 silent minutes, indistinguishable from a hang.
    from utils import setup_logging
    setup_logging()

    problems = check_environment(need_browser=not args.no_poll and not args.full_book,
                                 need_agents=not args.dry_run)
    if problems:
        for p_ in problems:
            print(f"  ✗ {p_}", file=sys.stderr)
        return 2

    # 0. Pre-flight — current URA prints before anything gets scored. Skipped
    # with --no-poll: nothing is re-scored then, so fresh comps would change
    # nothing but the wait.
    refresh = {}
    if not args.no_refresh and not args.no_poll and not args.full_book:
        refresh = refresh_ura(OCR_DISTRICTS)
        if not refresh.get("ok"):
            print(f"  ⚠ URA refresh incomplete ({refresh.get('error') or refresh.get('still_stale')})"
                  " — scoring on the older prints", file=sys.stderr)

    # 1. Poll
    if args.no_poll:
        # Reuse the LAST poll's own results rather than re-deriving from the DB
        # — that keeps price-changed listings (which a first_seen scan can't
        # recover, their first_seen is old) and costs PropertyGuru nothing.
        poll_state = poller.load_poll_state() or {}
        if poll_state.get("ok") and poll_state.get("last_run", "")[:10] == day:
            poll_state = {**poll_state, "skipped": True, "reused": True}
            print(f"--no-poll: reusing today's poll "
                  f"({len(poll_state.get('new_listings') or [])} new, "
                  f"{len(poll_state.get('changed_listings') or [])} changed)",
                  file=sys.stderr)
        else:
            poll_state = {"ok": True, "skipped": True,
                          "new_listings": [], "changed_listings": []}
    else:
        print("Polling PropertyGuru…", file=sys.stderr)
        poll_state = poller.run_poll(
            districts=[int(d) for d in args.districts.split(",")] if args.districts
            else SCAN_DISTRICTS,
            beds=[int(b) for b in args.beds.split(",")] if args.beds else SCAN_BEDS,
            max_pages=args.max_pages,
            max_price=args.max_price,
            headless=args.headless,
        )

        # One scope per cycle crawled to its real end — the only mechanism that
        # ever re-verifies deep stock (see deep_scope). Best-effort: it is a
        # coverage bonus, so a failure here must not fail the scan.
        if not args.no_deep and not args.districts and not args.beds:
            dd, db_ = deep_scope(state)
            print(f"Deep-verifying scope D{dd[0]:02d} {db_[0]}BR "
                  f"(rotating, ~every {len(SCAN_DISTRICTS) * len(SCAN_BEDS)} cycles)…",
                  file=sys.stderr)
            try:
                deep = poller.run_poll(districts=dd, beds=db_,
                                       max_pages=args.max_pages, allow_extend=True,
                                       max_price=args.max_price, headless=args.headless)
                poll_state["deep_scope"] = {
                    "districts": dd, "beds": db_,
                    "scraped": deep.get("scraped"), "added": deep.get("added"),
                    "ok": bool(deep.get("ok")),
                }
                merge_deep_rows(poll_state, deep)
            except Exception as e:  # noqa: BLE001 — bonus coverage, never fatal
                poll_state["deep_scope"] = {"districts": dd, "beds": db_,
                                            "ok": False, "error": str(e)}
                print(f"  deep verify failed ({e}) — continuing", file=sys.stderr)
            # run_poll saved the DEEP scope as the last poll; put the main
            # poll back so `--no-poll` reuse later today sees the whole cycle.
            if poll_state.get("ok"):
                poller._save_poll_state(poll_state)

    rows = list(poll_state.get("new_listings") or []) + \
        list(poll_state.get("changed_listings") or [])

    # --no-poll still has a job to do: grade everything that arrived since the
    # last run (not just today — the whole point of a weekly cadence). That
    # means NEW listings *and* ones that re-priced in the window: a drop INTO
    # the gate is exactly the signal worth catching, and keying only on
    # first_seen would silently discard every one of them.
    # --full-book asks a different question. The delta view ("what arrived this
    # week?") is right for monitoring, but the owner is buying ONE property out
    # of everything currently for sale — and standing inventory is invisible to
    # a delta scan forever. Treasure at Tampines had 103 listings in the DB and
    # could never surface, because none of them were new in any later cycle.
    if args.full_book:
        db = listings_db.load_db()["listings"]
        rows = [{"id": k, "project_name": r.get("project_name") or r.get("title"),
                 "district": r.get("district"), "beds": r.get("beds"),
                 "price": r.get("price"), "psf": r.get("psf"),
                 "score_1000": r.get("score_1000"), "url": r.get("url")}
                for k, r in db.items() if r.get("status") != "stale"]
        print(f"--full-book: gating over {len(rows)} active listing(s) — the whole "
              f"market in scope, not just this cycle's changes", file=sys.stderr)

    elif args.no_poll and not poll_state.get("reused"):
        since = (state.get("last_run") or {}).get("date") \
            or (now - timedelta(days=7)).strftime("%Y-%m-%d")
        db = listings_db.load_db()["listings"]
        rows = [{"id": k, "project_name": r.get("project_name") or r.get("title"),
                 "district": r.get("district"), "beds": r.get("beds"),
                 "price": r.get("price"), "psf": r.get("psf"),
                 "score_1000": r.get("score_1000"), "url": r.get("url")}
                for k, r in db.items()
                if (r.get("first_seen") or "") >= since or _repriced_since(r, since)]
        print(f"--no-poll: grading {len(rows)} listing(s) new or re-priced since {since}",
              file=sys.stderr)

    # 2/3. Gate — enrich + re-score the contenders first, then the real gate.
    db_listings = listings_db.load_db()["listings"]
    cooldown = _eval_cooldown_index(RE_EVAL_COOLDOWN_DAYS, now)
    known = _known_projects()
    enrich = {}
    # Not under --no-poll: that flag promises no PropertyGuru traffic.
    if (not args.no_enrich and not args.full_book and not args.no_poll
            and poll_state.get("ok")):
        rows, enrich = pre_gate_enrich(rows, db_listings, cooldown, args.min_score,
                                       known, headless=args.headless)
        if enrich.get("rescored"):
            db_listings = listings_db.load_db()["listings"]
    poll_state["preflight"] = {"ura": refresh, "enrich": enrich}
    shortlist, rejected = select_candidates(
        rows, db_listings, cooldown_index=cooldown,
        min_score=args.min_score, max_ai=args.max_ai,
        known_projects=known)
    print(f"\nGate: {len(rows)} new/changed → {len(shortlist)} for AI analysis "
          f"(>= {args.min_score}, cap {args.max_ai})", file=sys.stderr)

    # 4. AI analyze — realsmart looked up once per project first, then the
    # agents run `--parallel` at a time.
    verdicts: dict[str, dict] = {}
    runs = []
    if not args.dry_run and shortlist:
        rs = prewarm_realsmart(shortlist)
        print(f"realsmart: {rs['ok']}/{len(shortlist)} pre-fetched", file=sys.stderr)
        print(f"Analyzing {len(shortlist)} listing(s), {args.parallel} at a time…",
              file=sys.stderr)
        t_ai = time.time()
        verdicts, runs = run_agents(shortlist, day, parallel=args.parallel)
        print(f"AI phase: {len(verdicts)}/{len(shortlist)} verdicts in "
              f"{(time.time() - t_ai) / 60:.0f} min", file=sys.stderr)

    # 6. Store push (before the digest, so the digest can report what was pushed)
    pushed = [] if (args.dry_run or args.no_store_push) \
        else push_to_store(day, shortlist, verdicts)

    # 5. Digest
    digest = build_digest(day, poll_state, shortlist, rejected, verdicts,
                          args.min_score, args.max_ai, args.dry_run, pushed)
    digest_path = write_digest(day, digest)
    print(f"\nDigest: {digest_path}", file=sys.stderr)

    # Clear the store's reminder — only on a real, successful run.
    closed = bool(poll_state.get("ok")) and not args.dry_run
    if closed and not args.no_store_push:
        stamp_scan_note(day)

    state.setdefault("history", []).append({
        "date": day, "week": week, "poll_ok": bool(poll_state.get("ok")),
        # `closed` is the one that matters for the guard: a dry run can have a
        # perfectly good poll and still deliberately leave the week open.
        "closed": closed, "dry_run": args.dry_run,
        "candidates": len(rows), "shortlisted": len(shortlist),
        "analyzed": len(verdicts), "pushed": len(pushed),
    })
    state["history"] = state["history"][-52:]
    state["last_run"] = {
        "date": day, "week": week,
        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        # A dry run deliberately does not close the week.
        "poll_ok": closed,
        "digest": os.path.relpath(digest_path, BASE),
        "shortlisted": len(shortlist), "analyzed": len(verdicts),
        "pushed": len(pushed), "runs": runs,
    }
    save_state(state)
    return 0 if poll_state.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
