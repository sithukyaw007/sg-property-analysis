# Property Finder — Claude Code Playbook

This repo is driven by **Claude Code** (you). Python scripts gather *facts* —
scrape PropertyGuru, enrich with URA transaction data, compute costs, distances
and a technical MMR score. **You do the research, evaluation, and the final
Buy / Neutral / Avoid call.** The score is one input, never the answer.

## Before any flow: purpose check

All computed metrics (yield, ROI, liquidity, MMR) assume an **investment**
purpose (5–7yr hold). If the user's request suggests **own-stay** — or is
ambiguous between the two — **ask the user which it is before rating.**

Own-stay **adds** constraints, it does not remove them: run the full investment
screen, then narrow to what the household can live in. It is an intersection,
never a substitution. Every home is eventually sold, and a family selling on a
school timetable or a job move has *less* control over timing than an investor —
so exit liquidity matters more to them, not less. The one flag that genuinely
inverts is `oversized_unit` (space you live in is worth paying for); a thin
resale market or a bad exit record disqualifies a home exactly as it does an
investment. The 0–100 livability score is the filter on survivors, never the
primary lens. Full comparison: `docs/evaluation-rubric.md`.

## Buyer profile (forks) — read it before asking

`python buyer_profile.py` shows the active profile: a git-ignored
`profile.local.json` (template: `profile.example.json`) that states purpose,
buyer type and property count (ABSD), household citizenship (EC eligibility),
mandates (budget, beds, condo and/or EC), districts, and own-stay anchors
(workplace postal code, school radii). **When a profile is active it answers the
purpose check — don't ask again — and its mandate replaces the his/hers block
below.** With no profile, everything here behaves exactly as for the repo owner.

Profile-aware pieces (none of them touch MMR or `config.py`):
- `ROICalculator` defaults to the profile's buyer (a PR's first property pays 5%
  ABSD); `weekly.py` / `shortlist.py` take their mandates and districts from it.
- **Executive Condominiums** are benchmarked against EC prints only
  (`ura_district_D*_EC.csv` / `ura_rental_D*_EC.csv`, exported by `ura_api.py`).
  EC identity comes from URA's records, not the portal label (PropertyGuru calls
  many privatised ECs "Condominium"). `factual_data.ec_status` gives the MOP /
  privatisation stage and whether this household may buy (`scoring/ec_rules.py`,
  rules dated in `data/ec_rules.json`). Unprivatised ECs' trailing appreciation
  is capped at the regional baseline — the launch subsidy and MOP/privatisation
  re-rating are not repeatable returns.
- `python my_shortlist.py` — read-only, own-stay-first candidate list: exit screen
  first (every rejection reasoned), then personal fit + livability + valuation.
- `python -m scoring.personal_fit "<condo>" <district>` — commute (OneMap,
  `utils/onemap.py`) and primary schools within the profile's radii.
- `python import_listings.py <file>` — terms-safe CSV/JSON listing intake; the
  fork does not bulk-scrape PropertyGuru. Fresh listings arrive by syncing the
  upstream repo (`scripts/sync_upstream.sh`).

## Pick the flow (each has a skill with the full runbook)

| User says… | Flow / skill | Output |
|---|---|---|
| "analyze \<condo name\>" or project URL | `/analyze-development` | Development verdict + best stacks/facings/floors; rating per unit type |
| "analyze this: \<listing URL\>" | `/analyze-listing` | Buy/Neutral/Avoid on that exact unit, with confidence |
| "find condos in \<area\>" or results URL | `/market-scan` | Rated shortlist, strongest first ("no buys" is a valid outcome) |
| "research stacks/layout/facing for \<condo\>" (or a condo has no profile) | `/research-development` | Cited `profiles/<slug>.json` of physical facts (stacks/facings/layouts) |

Every flow: **① Recall → ② Gather → ③ Research & evaluate → ④ Fill
`agent_evaluation` → ⑤ `--from-review` report (auto-saves to memory).**
In ② Gather, pull per-project market context from realsmart.sg via the
`/realsmart-data` skill (owner-preferred source: **REALSCORE**, transaction/
profitability stats, holding-period distribution, rental yield) alongside URA
data. The public `/p/<slug>` page carries all of it on a plain fetch — no login;
resolve the slug with `realsmart.py` (never guess it) or just read the cached
answer with `python realsmart_cache.py --show "<condo>"`.
Record REALSCORE, `realsmart_pct_profitable` and `realsmart_annual_return_pct`
in `agent_evaluation`; read a high score as *downside* evidence (nobody has lost
money here), never as an appreciation forecast, and always report the
transaction count behind it — a 5.0 on six resales is noise.
Requests that fit none of these (e.g. "compare these two condos", "should I
sell?") still use the same tools — pick the closest altitude, state the rubric
used, and ask if intent is unclear.

## Evaluation principles (current rules — history & evidence in docs/RELEASES.md)

- **Your view first.** Form it from `factual_data` + web research; consult
  `algo_reference`, MMR and past evaluations to pressure-test, not to start from.
- **Research broadly.** The algo's ranking is an input, not a shortlist — in
  scans, cover lower-ranked listings too.
- **Honest verdicts.** "No buys", Avoid, and low confidence are first-class
  outcomes. Never manufacture a winner. Always fill `rating`, `confidence` and
  `rating_rationale` (the rationale renders under the verdict).
- **Value & region lead; trailing appreciation is de-weighted.** Backtested on
  the full 28-district URA panel (106k txns; **in-sample, single 2021–26 bull
  regime, 563 effective projects** — see docs/AUDIT_JUN2026.md): the robust
  forward signals are **cheap-vs-district-peers** and **region** (realized fwd
  OCR +3.7 > RCR +3.1 ≫ CCR +1.9%/yr; config baselines 2.8/3.7/4.2). The
  OCR>CCR tilt is long-run but **not a law** — CCR won 20/81 rolling windows
  since 2004, incl. the 2004–08 era outright. Trailing CAGR/momentum: marginal
  undetermined once clustered (univariate +0.13) — story, not signal. The
  future-infra score has no measured power; **freehold is forward-neutral**
  (a ~5% PSF *level* premium, not a return edge); absolute cheapness is mostly
  the region effect — don't double-count. Buyer-pool depth survives clustering
  (std_β +0.16); MRT proximity is real but weaker than first measured
  (as-of-T std_β −0.11 — a third of the old −0.16 was unopened-station
  look-ahead). High gross yield predicts *lower* forward price growth
  (std_β −0.16) — count yield as carry, never as appreciation. **The full
  model explains <10% of forward variance** (composite ρ +0.29, CI +0.21..
  +0.36) — small `score_1000` gaps are noise; calibrate confidence accordingly.
- **Value is age-relative — and the age curve is KINKED.** Don't compare a
  resale's PSF to a new launch's directly — use `factual_data.relative_value`,
  which normalizes peers along the measured piecewise decay (`AGE_PSF_DECAY_
  SEGMENTS`): ~3%/yr (≈$50/psf/yr) ages 0–10, a 10–15yr plateau, ~2.6%/yr at
  15–20, then slow drift. A flat $/yr rule under-adjusts new-vs-old ~2× inside
  0–10yr.
- **Age for *returns* plateaus 7–30yr.** Forward returns: the 0–5yr cohort is the
  *worst* (still amortizing launch premium); 5–30yr all fine; only 30+ slows.
  Don't discount a 15–25yr-old condo's prospects just for age, and don't stack
  an age penalty on top of the lease component (leasehold decay is its job).
- **Value is size-relative.** A small unit trades at a structurally higher PSF
  than a large one in the same project — MMR benchmarks against the same-size
  band plus **tight same-stack comps (±7% sqft, 24mo)** and damps signals on a
  thin cohort (`psf_cohort_txns`). When a small high-PSF unit ranks well, check
  its cohort depth before trusting it.
- **A deep discount is a verify-first signal, not value.** The ranking *selects*
  for artifact cheapness (mis-scraped sqft/beds, strata villas vs apartment
  medians, PES patios, double-volume lofts, stale/bait asks). MMR already
  kinks credit at −25%, caps psf_value/age_value/yield, and damps suspects —
  but a still-extreme discount that survives is a flag to confirm sqft, format
  and price against the project's own **recent (24mo)** URA prints before
  crediting. Trust flags: `ask_above_own_stack_prints` / `stack_low_floor_share`
  ≥ 0.7 (floor/PES artifact — the stack's own prints are the true comp),
  `ask_below_stack_prints` (below p10 of a deep print set — loft void or bait),
  `bedroom_sqft_mismatch`. Gross yield >~5.5% is the same artifact mirrored
  through rent ÷ price — verify ask, `rent_source`, and format first.
- **Override with care.** The system de-biases new-launch appreciation; don't
  override the adjusted rate with a portal headline CAGR — resale-to-resale
  evidence only.
- **`--score` circularity.** If you feed it your own rate/rent, the result
  validates internal consistency, not your inputs.

Full rubric, rating scale, and field guide: **`docs/evaluation-rubric.md`**
(read it in step ③). Release-by-release rationale: **`docs/RELEASES.md`**.

## Technical score (MMR)

Elo-style, base 1500, uncapped, continuous; displayed as `score_1000`
(0–1000; 500 = market-typical, 650+ recommended tier, <450 below threshold).
Missing data is neutral, never penalized. Sanity-check tool:

```bash
python invest.py --score '{"price":2300000,"sqft":958,"psf":2401,"beds":2,"district":"D15","tenure":"Freehold","project_name":"...","appreciation_rate_pct":5.5,"monthly_rent":6200}'
```

## Validation harness (`validation/`) — does an algorithm rank better than nothing?

```bash
python -m validation.run --both        # DEV + VAL + the degradation ratio
```

Separate from `backtest.py` on purpose. That harness measures FEATURES on a
panel the v3.3–v3.10 weights were then tuned against — useful, but in-sample by
construction. This one answers only "does algorithm A rank condos better than
algorithm B, and better than no algorithm at all", and refuses to answer it in
ways that flatter the algorithm.

- **Target** `excess_fwd`: forward PSF CAGR per project × size-band, **minus its
  district's**. Differenced because the whole usable panel sits in one bull
  market, so a raw forward return mostly measures "was it 2022". Size-banded
  because PSF falls with unit size (elasticity ≈ −0.19) — the artifact that
  explained away all 12 top scorers in the 2026-07-29 sweep.
- **The bar is `psf_vs_dist`, not zero.** Baselines (random, district-median,
  single-feature cheapness) are reported on every run. An assembled model that
  cannot beat one feature has added complexity and nothing else.
- **Splits are measured, not chosen.** A split needs a 3-year feature lookback
  plus the outcome window, inside URA data spanning 2021.38–2026.46. At
  window=2.0 the legal range is 0.08 years — one split, no validation split at
  all. Only window=1.0 admits two with disjoint outcome windows, and that is a
  real compromise (far short of a 5–7yr hold). The fix is another year of
  prints, not cleverness.
- **In-regime is not validated.** Both splits sit in 2021–2026. Passing VAL
  means "not overfit to noise", never "works". Only `calibrate_forward.py`'s
  prospective clock tests that; first honest read ~2027-08.
- Results append to `data/validation_results.csv` so they can be graphed across
  months. `panel_id` embeds a content hash — two rows are only comparable if it
  matches.

**Never add a constant to `config.py` for a non-scoring knob.** It is
sha-hashed into `score_version`, so any non-comment line there restamps the
vintage of all 9,029 scored listings and splits the registered calibration
cohort. `calibrate_forward.VERSION_ALIASES` exists because this already
happened once.

## Condo arena (`--fight`) — AI referee required

`python invest.py --fight` runs a pairwise tournament over the listings DB —
contenders are (condo × unit type), purely algorithmic (no AI in the fights).
**Every run requires you to referee before results are trusted**: read
`output/arena_referee_packet.json`, work through its `auto_flags`, verify the
top contenders' stats are real (web-check if extreme), then append a
**Referee verdict** to `output/arena_latest.md`: confirmed ranks, demoted
contenders + reasons, and run confidence. The arena ranks stats; you certify
the stats deserve to rank. Each run also writes a timestamped archive
(`output/arena_<ts>.md`).

## Weekly scan (`weekly.py`) — poll, algo-grade, AI-triage

The once-a-week loop. **Nothing auto-starts it** — there is no daily job, no
LaunchAgent, no cron. The reminder lives in the personal data store
(`projects/property-finder/weekly-scan.md`, surfaced at session start when due)
and the owner drives it from there; a successful run stamps that note's
`last_run` so the reminder clears itself. It chains what already existed:

**refresh stale URA prints → poll → algo-grade EVERY new/changed listing →
enrich + re-score the contenders → gate → AI-analyze only what clears the gate
(3 in parallel) → digest → push the good ones to the store.**

Pre-flight (2026-09-24): URA sales and rentals sync through the **official URA
API** via [sgprop](https://github.com/ong6/sgprop) (`ura_api.py`, ~40 s, no
browser), which rewrites the same `data/ura_district_D*.csv` /
`ura_rental_D*.csv` files and rebuilds `ura_cache.json`, `rental_cache.json`
and `bed_bands.json`. The key lives in `~/.config/sgprop/credentials`, never
here. Without sgprop or a key it falls back to re-fetching district CSVs older
than `URA_MAX_AGE_DAYS` (30) in a browser. Before this the prints had drifted
to 106 days old, and the eservice rental download was capped at 10,000 rows
per district. Then the top `PRE_GATE_ENRICH` (36) gate-eligible
listings with no detail visit are enriched and re-scored, so the cap picks on
floor-aware comps rather than card-level data.

### The mandate — WHAT is being bought

The `MANDATES` block at the top of `weekly.py` is the only part that says what
to look for; everything else is policy about *how* to scan. Two buyers, two
budgets, one shared region:

| Mandate | Budget | Beds |
|---|---|---|
| `his` | ≤ $1.8M (flexible) | 3BR |
| `hers` | ≈ $2.5M | 3–4BR |

Region is a **hard filter, not a preference**: OCR only, `OCR_DISTRICTS =
[16, 18, 19, 20, 21, 22, 23, 26, 28]` — D16–D28 minus D17/D24/D25/D27, each
excluded for a reason recorded in `OCR_EXCLUDED` and argued from the repo's own
URA resale prints (D24 has *zero* resales, D25 the thinnest book; D17/D27 are
the softer trailing-return calls). The scrape ceiling is the top budget ×
`SCAN_PRICE_HEADROOM` (1.06) — an ask slightly over budget still negotiates.

Region and mandate are enforced **at the gate**, not just in the scrape scope:
the DB holds thousands of older out-of-region rows, and an unreadable district
fails closed.

### The gate

`score_1000` is free, an agent run is not. Checks run cheapest-first and the
first failure wins: stale → incomplete (price/sqft/psf) → unscored → below
`MIN_SCORE_FOR_AI` → out of region → outside the mandate → no usable project
name (a marketing headline, not a development) → **bedroom relabel**
(`bed_bands`, below) → re-eval cooldown → same (condo, beds) already
shortlisted this scan. Every rejected listing carries a `gate_reason` that the
digest prints — the gate is never a black box.

Knobs live at the top of `weekly.py` (deliberately **not** `config.py` — that
file is hashed into `score_version`, and a scan-policy threshold must not
restamp the scored book's vintage):

| Knob | Default | Why |
|---|---|---|
| `MIN_SCORE_FOR_AI` | 650 | the recommended tier; ~p88 of the book |
| `MAX_AI_RUNS_PER_SCAN` | 12 | the gate alone is unbounded — a bulk relist could put 40 listings over 650 |
| `AI_PARALLEL` | 3 | agents in flight; same-condo candidates share a lane so they never race one eval file |
| `URA_MAX_AGE_DAYS` | 30 | re-fetch URA prints older than this before scoring |
| `PRE_GATE_ENRICH` | 36 | contenders detail-enriched + re-scored before the capped gate |
| `POLL_MAX_PAGES` | 10 | search is date-desc, so pages = reach; the poller's 2 only covers a 6-hourly cadence |
| `RE_EVAL_COOLDOWN_DAYS` | 30 | a fresh listing of an already-judged (condo, beds) rarely changes the call |
| `AI_TIMEOUT_S` | 1800 | per-agent wall clock; past it something is stuck |
| store push | Buy/Strong Buy **and** ≥ medium confidence | the store note is a feed the owner reads, not a log |

**Budget allocation is per-mandate, not pure score rank.** Hers is a ~$1M
larger budget and therefore systematically nicer stock, so a straight ranking
would spend every slot on her and never research his. `_allocate_budget()` gives
each mandate present a fair share first (`ceil(max_ai / mandates_present)`, so
4 + 4 of 8), then hands unclaimed slots to the best remaining listings
regardless of buyer. Anything left over is deferred with a cap reason.

Each shortlisted project also gets a **realsmart.sg REALSCORE** lookup, done
once per project by `realsmart_cache.warm()` right after the gate and handed to
the agent in its prompt (it only fetches itself on a miss). The store's
`fetch.py` is a `uv run --script` file — calling it with this repo's Python
fails on its imports, which silently broke every lookup until 2026-09-24. This
sits *after* the gate on purpose: realsmart's ToS is personal-use, so ~12
project lookups a week is a research source — never a per-listing feed.

```bash
python weekly.py                # the real thing (HEADED browser — Cloudflare)
python weekly.py --dry-run      # poll + gate + digest, spawn no agents
python weekly.py --no-poll      # grade what already arrived since the last run
python weekly.py --full-book    # gate the WHOLE active book, not just the delta
python weekly.py --force        # re-run a week that already succeeded
```

**`--full-book` answers a different question.** The default delta view ("what
arrived or re-priced this week?") is right for monitoring, but the owner is
buying ONE property out of everything currently for sale, and standing inventory
is invisible to a delta scan *forever* — Treasure at Tampines had 103 rows in the
DB and could never surface, because none of them were new in a later cycle.
`--full-book` re-gates every non-stale listing in the DB instead.

Other flags: `--headless` (Cloudflare usually blocks it), `--min-score`,
`--max-ai`, `--max-pages`, `--districts` / `--beds` / `--max-price` (override the
mandate for one run), `--parallel N`, `--no-refresh`, `--no-enrich`,
`--no-store-push`.

Outputs: digest `output/weekly/<date>.md`, agent transcripts
`output/weekly_runs/`, state `data/weekly_state.json`. A run whose **poll
failed** does not close the week — just run it again. Verdicts land in
`evaluations/` like any other flow — that, not the digest, is the durable record.

The agent step gathers with **`invest.py --from-db <id>`**, which builds a run
dir + `raw_analysis.json` straight from the DB record with **no scraping**
(cohort stats from the full DB, so the MMR matches what the gate saw). Use it
by hand whenever PropertyGuru is unreachable but the listing is already in the DB.

## Bedroom bands (`bed_bands.py`) — catching relabelled unit types

A listing's bedroom count comes from the marketing agent, not a registry, and
agents relabel: a Waterview 926 sqft unit was listed as "3BR" when the developer's
own mix and URA's filings call that size a 2-bedder (real 3BRs there start at
1,109 sqft / $1.78M+). `listings_db`'s global 850–1800 sqft sanity table waves
that through, so the check has to be **per project**.

URA *rental* contracts are the authority available to us — unlike sale prints
they carry "No of Bedroom" beside a floor-area band, filed by the landlord
against the actual unit. `bed_bands.py --build` learns each project's
bedroom→size bands from `data/ura_rental_D*.csv`.

```bash
python bed_bands.py --build                 # rebuild data/bed_bands.json (gitignored)
python bed_bands.py --check "Waterview" 3 926
```

Verdicts: `ok` / `mismatch` / `oversize` / `undersize` / `unknown`. **Only
`mismatch` blocks** — the size is below the project's own band for that bed count
*and* lands in a smaller one. `oversize` passes (penthouses and dual-keys are
genuinely bigger), and a band needs `MIN_CONTRACTS = 8` filings before it may
contradict a listing, with `TOLERANCE = 0.12` slack for URA's 100 sqft buckets.
`unknown` is honest, not a failure — most projects have no rental history.

## realsmart.sg — slug resolution and the project cache

Two modules, one source:

- **`realsmart.py`** resolves a project name to its real `/p/<slug>` URL against
  realsmart's own **sitemap** (12.8k slugs cached in `data/realsmart_slugs.json`,
  gitignored/regenerable). Guessing is wrong often enough to matter — PropertyGuru
  writes "West Bay Condo" where realsmart has "west-bay-condominium" and the guess
  404s. `resolve()` returning **None is a first-class answer**: a guessed URL that
  happens to 200 on a *different* project would attach the wrong REALSCORE.
  ```bash
  python realsmart.py --refresh              # one sitemap request, ~monthly
  python realsmart.py "West Bay Condo"       # resolve one name
  ```
- **`realsmart_cache.py`** holds per-**project** facts (REALSCORE, % profitable,
  profitable/unprofitable counts, avg holding, annual return, tenure/completion)
  in `data/realsmart_projects.json` with a **45-day TTL** — these are properties
  of the development, so twenty listings in one condo share one answer and a
  normal run fetches nothing. Writes are merged under a file lock (two parallel
  warms previously overwrote each other and silently lost ten fetches).
  ```bash
  python realsmart_cache.py --from-db --districts 16,18,19 --min-score 600
  python realsmart_cache.py --show "Regentville"
  ```
  Fetching is two rungs: the plain public `/p/<slug>` page first, and — **only
  when that page carries no REALSCORE** — a rendered `/map` fallback. realsmart
  serves two `/p` templates and the lite one has no score at any depth, so this
  is a genuine escalation, not a retry. The `/map` rung needs the logged-in
  browser profile; an expired session just records no score.

Parsing is deliberately paranoid: the page mixes *value-then-label* stat tiles
with *label-subtitle-value* headers whose subtitles contain digits, and a
highlights badge reading "100% / Profitable" sits above the real count tile.
A naive read returns confident wrong numbers rather than failing.

## Shortlist (`shortlist.py`, `shortlist_ui.py`) — the ranked buy-list

`shortlist.py` joins eval memory + the realsmart cache + the listings DB into one
ranked table (newest verdict per condo, filtered to the mandate).

```bash
python shortlist.py                  # everything evaluated since --since
python shortlist.py --mandate his    # one buyer  (--json for the raw rows)
python shortlist_ui.py               # http://127.0.0.1:8644 — same data, one page
```

**It ranks on evidence of exit, not on the algo score** — verdict first, then
% of resales sold at a profit (only trusted at `MIN_RESALES_FOR_TRUST = 150`
resales), then REALSCORE, and `score_1000` *last*. That ordering is deliberate:
across this batch `score_1000` and % profitable correlated around **−0.7**. MMR
rewards "cheap versus district peers", and a project is often cheap precisely
because the market has learned it underperforms — so the score selects value
traps. Until that is measured across the full book, transaction history is the
more trustworthy sort key. **Treat this as a warning about MMR, not a proven
law: it is one batch, not the full panel.**

`shortlist_ui.py` computes that pairing into one READ chip per row — TRAP /
HOLDS / WEAK / UNPROVEN — rather than leaving the arithmetic to the reader, and
opens with the two or three projects actually worth visiting. It is a **viewer**:
nothing there scrapes, scores, or spends an agent run.

## UI, dashboard, poller

**Listings UI**: `python ui.py` → http://127.0.0.1:8642 — a **viewer over the
scored DB** (full book, sorted by score_1000; overview stats strip, NEW /
price-drop badges, min-score / recency / district / beds filters, same-unit
dedupe ×N badge, Score/Val/Liv/Ovr axis chips); arena table is the second tab
(`#arena`). **Live scraping is OFF by default** — PropertyGuru is behind
Cloudflare, which blocks headless background polls, so the UI never scrapes on
its own. Refresh data with the standalone headed poller: `python poller.py`
(one cycle) or `--loop` (solve Cloudflare once in the Chrome window; the
clearance cookie is reused), state in `data/poll_state.json`. To opt the
in-UI scraping back in (auto-poll + SCAN NOW): `python ui.py --poll
--poll-no-headless` (`--poll-districts/--poll-beds/--poll-interval-mins` to
tune). JSON APIs: `/api/fresh`, `/api/rankings`, `/api/poll-status`,
`POST /poll`.

The fresh view shows the **three-score system** (v3.11) — `score_1000` (MMR —
forward-return money axis, backtest-anchored), plus three 0–100 axes:
**Valuation** (`scoring/valuation.py` — "priced right today?" vs age-adjusted
peers; backtested, 50 = typical ask, >50 cheaper, shares MMR's artifact
defences), **Livability** (`scoring/livability.py`: baths/bed, space/bed, MRT
walk, floor, facing, age, facilities; 50 = neutral, missing data neutral, hover
the chip), and **Overall** (`scoring/overall.py` — purpose-weighted; the fresh
view shows the investment-weighted one). Livability is an explicit
**heuristic** — URA carries none of those fields, so it can never be
backtested and must NEVER be folded into MMR; in the investment Overall it
enters only as a capped demand-floor nudge, never as appreciation. Valuation
and Overall populate on the next poll / `--score-db` run (like `score_1000`).

**System dashboard**: `python dashboard.py` → http://127.0.0.1:8643 — engine
state on one page (forward signals, regime analysis, score distribution,
coverage, district benchmarks, top projects) with per-condo ANALYZE buttons
that run `claude -p "analyze <condo>"` headless (transcript →
`output/analyze_runs/`; verdict auto-saves to eval memory).

**Shortlist UI**: `python shortlist_ui.py` → http://127.0.0.1:8644 — the
researched buy-list with the value-trap read (see the shortlist section above).

Supporting data backbone (append-only — grep/analyze freely):
`data/listings_sheet.csv` (latest state + MMR), `data/mmr_history.csv`
(every scoring run), `data/arena_results.csv` (every tournament),
`data/ura_cache.json` (per-project URA metrics), `data/ura_district_D*.csv`
(raw URA prints — the comp source).

### Detail-page enrichment — what PropertyGuru actually serves

`--enrich-top N` / the poller's enrich step visit listing detail pages for
`floor_level`, `facing`, `furnishing`, `total_units`, `developer`, lat/lng and
facilities. This was silently broken — the fields sat at **0% coverage across
~9,040 listings** because the JSON path moved (`props.pageProps.listingData`
no longer exists; the data is under `props.pageProps.pageData.data`) and the
fallback default was `None`, so extraction returned `{}` and reported success.
Fixed 2026-07-29; extraction is now four layers (structured `__NEXT_DATA__` →
that JSON's rendered metatable → `ld+json` `@graph` → body text), each filling
only what earlier layers left blank.

Know the honest ceilings before you read a coverage number as a bug:

- **`facing` is unobtainable.** Null on 26/26 sampled listings — PropertyGuru
  does not serve it. Expect it to stay 0%. Use `profiles/` (via
  `/research-development`) for facing, never the listing feed.
- **`floor_level` caps around 35–40%** — it is optional for the posting agent.
- Populating `floor_level` does **not** re-arm the v3.12 low-floor demotion:
  `MMR_LOW_FLOOR_PENALTY` fires on `stack_low_floor_share`, computed from URA
  prints, which was never affected. What it does re-arm is the floor-basis comp
  adjustment, floor-tier benchmark scaling and stack matching.

## The memory systems

**`evaluations/` (git-tracked)** — your past judgements, one JSON per condo,
append-only. May be stale or wrong: re-verify price and conditions before
relying on one. Pre-2026-06-10 evals predate the current priors — re-judge
rather than inherit. Commit this folder after evaluations.
```bash
python invest.py --recall "<condo>"     # prior ratings + staleness warning
python invest.py --list-evals
python invest.py --eval-stats           # rating/confidence calibration audit
```

**`profiles/` (git-tracked)** — a development's *physical facts* (stacks,
facings, views, layouts, site plan) — researched once, reused. Stable (not
price-dependent). Listings auto-join to a stack/layout at eval time
(`factual_data.stack_profile`). Never invent a stack; absent =
not-yet-researched. Populate on-demand via `/research-development`.
```bash
python invest.py --profile "<condo>"    # show stacks/facings/layouts (fuzzy)
python invest.py --list-profiles
```

**`data/listings_db.json` + `listings_sheet.csv`** — every listing ever
scraped, deduplicated, with price history (price drops surface).
```bash
python invest.py --search-db "<name>"   # find listings
python invest.py --list-db              # stats, price drops, by district
python invest.py --update-db --districts 3,5,14,15 --beds 2,3   # refresh only
```

## URA appreciation cache

```bash
python invest.py --build-ura-cache data/ura_*.csv
python invest.py --fetch-ura-districts 3,5,14,15
```

## Project structure

```
invest.py              # CLI: flows, --score, --recall, --search-db, --from-db, --from-review
buyer_profile.py       # Fork: git-ignored profile.local.json -> mandates, ABSD, EC eligibility
my_shortlist.py        # Fork: read-only own-stay-first candidate list (exit screen first)
import_listings.py     # Fork: terms-safe CSV/JSON listing intake (upsert + score)
build_primary_schools.py # Fork: data/primary_schools.json (MOE + OneMap geocode)
scripts/sync_upstream.sh # Fork: merge upstream, then re-derive EC data + rescore
weekly.py              # Weekly scan: MANDATES, poll -> algo grade -> gate -> digest -> store push
shortlist.py           # Ranked buy-list (eval memory + realsmart + DB), exit-record first
shortlist_ui.py        # Shortlist viewer (:8644) with the TRAP/HOLDS read chip
realsmart.py           # realsmart.sg slug resolution from their sitemap
validation/            # Out-of-sample harness: does an algorithm rank better than nothing?
  panel.py             #   excess_fwd target, district-differenced, size-banded; legal splits
  algos.py             #   algorithms under test + the baselines they must beat
  metrics.py           #   rho w/ cluster-bootstrap CI, decile lift, hit@K w/ permutation null
  run.py               #   runner -> data/validation_results.csv (append-only ledger)
realsmart_cache.py     # Per-project realsmart facts, 45-day TTL, /p then /map fallback
bed_bands.py           # Per-project bedroom->size bands from URA rental contracts
backtest.py            # Point-in-time URA backtest (validates MMR weights)
backtest_ext.py        # Extended panel: lever measurement, age curve, regimes
calibrate_forward.py   # Shipped-score vs realized-return calibration (~mid-2027+)
config.py              # MMR calibration, weights, thresholds
eval_memory.py         # Git-tracked evaluation memory (judgements)
profile_memory.py      # Git-tracked condo profiles + listing→stack join
listings_db.py         # Master listings sheet
poller.py              # PropertyGuru poll cycle (data/poll_state.json)
ui.py                  # Fresh-listings + arena UI (:8642), auto-polls
dashboard.py           # System dashboard (:8643) + headless ANALYZE
scoring/
  full_scorer.py       # Enrichment + component scoring + tight stack comps
  mmr.py               # MMR (uncapped, continuous) + /1000 norm + trust rules
  livability.py        # 0–100 own-stay heuristic (NEVER folded into MMR)
  personal_fit.py      # Fork: 0–100 commute + primary schools (profile anchors; never MMR)
  ec_rules.py          # Fork: EC MOP/privatisation stage + buyer eligibility (data/ec_rules.json)
  raw_output.py        # raw_analysis.json (mode-aware AI instructions)
  models.py            # ScoredListing + verdict_from_rating
  roi.py / costs.py / rental_estimator.py / future_scorer.py / district_scorer.py / quick_scorer.py
scrapers/              # PropertyGuru + URA CSV + headless browser
docs/evaluation-rubric.md   # Full evaluation rubric (read in step ③)
docs/RELEASES.md            # Condensed release history + open items
docs/AUDIT_JUN2026.md       # Jun-2026 full audit — prioritized fix queue
.claude/skills/        # analyze-development / analyze-listing / market-scan /
                       # research-development / arena-cycle / realsmart-data
evaluations/           # Past evaluations — judgements (commit; may be stale)
profiles/              # Condo profiles — physical facts (commit)
output/                # Per-run reports (gitignored)
```
