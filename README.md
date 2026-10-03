# Property Finder

A Singapore condo investment analysis toolkit that combines algorithmic scoring with AI-powered qualitative research. It scrapes PropertyGuru listings, enriches them with URA government transaction data, and scores them with **MMR** — an Elo-style rating displayed as `score_1000` (0–1000) — while an AI coding agent (like [Claude Code](https://docs.anthropic.com/en/docs/claude-code)) does the research and makes the final Buy / Neutral / Avoid call.

Built for **5-7 year hold periods** — all cost models, ROI projections, and appreciation decay curves are calibrated for medium-term residential investment in Singapore.

## How It Works

The tool runs a **hybrid algo + AI agent pipeline**:

```
Stage 1 (Algo)              Stage 2 (AI Agent)              Stage 3 (Generator)
invest.py --raw         →   Agent reads raw JSON,       →   invest.py --from-review
scores + empty fields       does web research,               renders verdicts,
                            fills agent_evaluation           generates final report
```

**Stage 1** scrapes PropertyGuru, enriches with URA transaction data, and computes the MMR score plus the factual inputs (yield, relative value, liquidity, costs). **Stage 2** (optional) is where an AI agent or human researches each property — stack quality, condo reviews, construction issues, anything the algorithm can't quantify — and fills the `agent_evaluation` block. **Stage 3** renders the final markdown report and auto-saves the evaluation to memory.

## Prerequisites

- **Python 3.10+**
- **Google Chrome** (used by Patchright for headless scraping)
- **Node.js 18+** (only needed if using the Playwright MCP server for URA data fetching)

## Installation

```bash
git clone https://github.com/ong6/sg-property-analysis.git
cd sg-property-analysis
python -m venv venv && source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
python -m patchright install chromium            # Patchright needs a browser
```

## Quick Start

### Three ways to start an analysis

```bash
# A — A whole development, by name (groups listings by unit type)
python invest.py --condo "The Continuum" --beds 1,2,3
# B — A specific PropertyGuru listing, project page, or results page (by URL)
python invest.py --url "https://www.propertyguru.com.sg/listing/24xxxxxx-..."
# C — A market scan across districts
python invest.py --auto --beds 2,3 --top 20
python invest.py --districts 3,5,14,15 --beds 2,3 --top 20
```

Each run writes `output/run_NNN/` (raw analysis, report, scored JSON) and updates the
searchable listings sheet. See [CLAUDE.md](CLAUDE.md) for the full Claude Code playbook.

### Local web UIs

```bash
python ui.py            # http://127.0.0.1:8642 — fresh listings + condo arena
python dashboard.py     # http://127.0.0.1:8643 — system dashboard + headless ANALYZE
python shortlist_ui.py  # http://127.0.0.1:8644 — the researched buy-list
```

All three are **viewers over data already on disk — none of them scrape by default.**
PropertyGuru sits behind Cloudflare, which blocks headless background polls, so in-UI
scraping is opt-in: `python ui.py --poll --poll-no-headless` turns on the background
auto-poll and the SCAN NOW button (`--poll-interval-mins`, `--poll-districts`,
`--poll-beds` to tune). The normal way to refresh data is the standalone headed poller,
`python poller.py` (`--loop` to keep cycling) — solve Cloudflare once in the Chrome window
and the clearance cookie in `chrome-profile/` is reused.

### Standalone (no AI agent)

```bash
python invest.py --discover-districts    # district rankings, no scraping
python invest.py --score '{"price":2300000,"sqft":958,"psf":2401,"beds":2,"district":"D15","tenure":"Freehold","project_name":"The Continuum","appreciation_rate_pct":5.5,"monthly_rent":6200}'
python invest.py --fight                 # condo arena: pairwise tournament over the DB
python invest.py --search-db "continuum" # search the master listings sheet
python invest.py --update-db --districts 3,5,14,15 --beds 2,3   # refresh sheet only
```

### With AI Agent Review

```bash
python invest.py --recall "The Continuum"     # 0 (optional): recall past evaluations
python invest.py --auto --beds 2,3 --top 10   # 1: algo scoring → output/run_NNN/raw_analysis.json
# 2: AI agent (or human) reads raw_analysis.json, researches each property,
#    and fills the agent_evaluation fields (rating + confidence + rationale)
python invest.py --from-review output/run_NNN/raw_analysis.json --output output/run_NNN/final  # 3
```

The final report leads with a clear **🟢 BUY / 🟡 NEUTRAL / 🔴 AVOID** verdict and
confidence per listing. Evaluations are saved to the git-tracked `evaluations/` folder —
commit it so they can be recalled later (a *reference*; prices change, always re-verify).

### With Claude Code

If you use [Claude Code](https://docs.anthropic.com/en/docs/claude-code), just ask in plain
language — **"analyze The Continuum"**, **"analyze this: \<PropertyGuru link\>"**, or
**"find me 2-bedders in D15"**. It picks the right flow, recalls past evaluations, does the
research, and generates the report. [CLAUDE.md](CLAUDE.md) is the playbook it follows.

## CLI Reference

Most-used flags for `python invest.py`, grouped by purpose (`--help` for the full list):

| Flag | Description |
|------|-------------|
| **Flows** | |
| `--condo NAME` | Analyze a development by name (fuzzy match; groups by unit type) |
| `--url URL` | Analyze a PropertyGuru URL (listing, project page, or results page) |
| `--auto` | Auto-discover top districts, scrape, and score |
| `--districts, -d` | Comma-separated district numbers (e.g. `3,5,14`) |
| `--discover-districts` | Show district rankings without scraping |
| `--auto-districts N` / `--include-ccr` | Auto-mode tuning: district count (default 5) / include CCR |
| **Scraping filters** | |
| `--beds, -b` | Bedroom counts (default: 2,3 for scraping; ALL for `--fight`) |
| `--min-price` / `--max-price` | Price filters in SGD (optional, no default) |
| `--max-pages` / `--url-max-pages` | Pages to scrape per district / per results URL (default: 5) |
| `--enrich-top N` | Visit detail pages for the top N listings (default: 0 = off) |
| **Scoring** | |
| `--score JSON` | Score a condo from key inputs; prints the technical breakdown |
| `--score-db` | Batch-score every usable listing in the DB and write scores back |
| `--fight` | Condo arena: pairwise tournament over the DB (Elo ranking + champion) |
| `--raw PATH` / `--from-review PATH` | Write raw analysis JSON for agent review / generate final report from it |
| **Listings DB** | |
| `--from-db IDS` | Build a run dir + raw analysis from listings already in the DB — **no scraping** |
| `--search-db QUERY` / `--list-db` | Search the master sheet by condo name / show sheet stats |
| `--update-db` | Scrape and refresh the sheet only (skip scoring) |
| `--export-sheet` / `--no-db` | Re-export the CSV / don't upsert scraped listings |
| **Memory** | |
| `--recall NAME` | Recall past evaluations for a condo (fuzzy) |
| `--list-evals` / `--eval-stats` | List evaluations / rating-confidence distribution audit |
| `--no-save-eval` | Don't auto-save evaluations during `--from-review` |
| `--profile NAME` | Show a condo's researched stack/layout profile (fuzzy) |
| `--list-profiles` / `--save-profile FILE` | List profiles / save a profile JSON into `profiles/` |
| **URA data** | |
| `--build-ura-cache CSV...` / `--fetch-ura-districts` | Build the URA cache from CSVs / fetch by district |
| **Output / misc** | |
| `--top, -n` / `--output, -o` / `--csv` / `--json` | Result count and export paths |
| `--verbose, -v` / `--no-headless` / `--list-districts` | Score breakdowns / show browser / list districts |

## Weekly Scan

`weekly.py` is the once-a-week loop: **poll → algo-grade every new and
price-changed listing → gate → AI-analyze only what clears the gate → digest →
push the good finds to a notes store**. The algo grade is free and applied to
everything; the AI agent is expensive, so it only runs on listings scoring
`>= 650` (capped at 8 per scan, with a 30-day re-evaluation cooldown per condo +
bed count). Everything filtered out is still logged, with the reason.

```bash
python weekly.py              # the real thing (headed browser)
python weekly.py --dry-run    # poll + gate + digest, no agents
python weekly.py --no-poll    # skip the scrape, grade what already arrived
python weekly.py --full-book  # gate every active listing, not just the week's delta
python weekly.py --force      # re-run a week that already completed
```

The scan is driven by a **search mandate** — the `MANDATES` block at the top of
`weekly.py`, which is the one place that says *what* is being bought (budget per
buyer, bedroom counts, and a hard region filter over a chosen district list).
Region, budget and bed count are enforced at the gate, not just in the scrape
scope, and the AI budget is split fairly across buyers before leftovers go to the
best-scoring listings — otherwise the larger budget takes every slot. Listings
whose bedroom count looks relabelled are rejected by `bed_bands.py` (below).

By default a scan looks at the **delta** (new + re-priced). `--full-book`
re-gates every active listing in the DB instead — standing inventory is invisible
to a delta scan forever, which is how a 103-listing project never surfaced.

Nothing auto-starts it — no cron, no LaunchAgent; run it by hand when the reminder
comes up. Digests land in `output/weekly/<date>.md`; verdicts go to `evaluations/`
like any other flow. Thresholds are at the top of `weekly.py`. Full details:
`CLAUDE.md`.

## Shortlist and Supporting Data

```bash
python shortlist.py --mandate his   # ranked buy-list for one buyer (--json for rows)
python bed_bands.py --build         # per-project bedroom→size bands from URA rentals
python realsmart.py --refresh       # cache realsmart.sg's sitemap slug index
python realsmart_cache.py --from-db --districts 16,18,19   # warm per-project stats
```

- **`shortlist.py` / `shortlist_ui.py`** join evaluation memory, per-project
  realsmart.sg stats and the live listings DB into one ranked table. It ranks on
  **evidence of exit** — the share of a project's resales that actually sold at a
  profit, only trusted past 150 resales — with the algo score *last*, because in
  the batch measured so far the two ran opposite (r ≈ −0.7). A high algo score
  next to a poor profitability record is flagged as a value trap.
- **`bed_bands.py`** learns each project's real bedroom→size bands from URA
  *rental* contracts (the only source that files a bedroom count against a unit)
  and rejects the 2BR-sold-as-3BR relabel that a global sqft sanity table waves
  through. Only a confirmed downward mismatch blocks; oversize units and thin
  evidence pass.
- **`realsmart.py` / `realsmart_cache.py`** resolve a project's realsmart.sg page
  from their sitemap (guessed slugs 404 or, worse, hit a different project) and
  cache per-project stats with a 45-day TTL, since they belong to the development
  rather than the listing. Low-volume research lookups only — their terms are
  personal-use.

## Running your own fork

The repo is set up for its owner's household (an SC buyer, two investment
mandates, a weekly scrape-and-agent loop). A fork for a different buyer needs
no code edits:

```bash
python buyer_profile.py --init        # creates git-ignored profile.local.json
$EDITOR profile.local.json            # purpose, buyer type, budget/beds, condo/EC, districts, anchors
python buyer_profile.py --check
python ura_api.py                     # URA sales + rentals incl. Executive Condominiums (needs a free URA key)
python my_shortlist.py                # own-stay-first candidate list -> output/my_shortlist/
```

- **Profile** — `profile.local.json` drives mandates, districts, ABSD (buyer
  type and property count) and EC eligibility (household citizenship). Without
  it, nothing changes.
- **Executive Condominiums** — EC sales/rentals are exported to
  `data/ura_*_D*_EC.csv`; ECs are compared only with ECs, their lifecycle stage
  and buyer eligibility come from `scoring/ec_rules.py`, and the condo
  backtest/validation panels exclude them by design.
- **Own-stay anchors** — `scoring/personal_fit.py` adds commute time (OneMap;
  put `ONEMAP_EMAIL` / `ONEMAP_PASSWORD` in `~/.config/onemap/credentials`,
  chmod 600) and primary schools nearby (`data/primary_schools.json`, rebuilt by
  `build_primary_schools.py`).
- **Listings without scraping** — `python import_listings.py my_listings.csv`
  takes listings you collected yourself; `python invest.py --url <listing>` still
  analyzes one PropertyGuru page. Fresh portal listings arrive by merging the
  upstream repo: `scripts/sync_upstream.sh`.
- **Owner-only flows** — `weekly.py`'s store push, the realsmart fetcher and the
  background `claude -p` agents depend on the owner's personal data store and
  Claude Code. Run the analyze skills interactively instead.

## Configuration

Investment parameters and scoring calibration live in `config.py`:

```python
TARGET_HOLD_YEARS = [5, 6, 7]
DEFAULT_BUYER_TYPE = "SC"       # SC, PR, or Foreigner
DEFAULT_PROPERTY_COUNT = 0      # 0 = first property (0% ABSD for SC)
```

The same file holds all MMR calibration: normalization constants (`MMR_BASE`,
`MMR_NORM_CENTER`, `MMR_NORM_SCALE`), tier thresholds (`SCORE1000_TIER1_MIN` /
`SCORE1000_TIER2_MIN`), backtest-calibrated component slopes, and regional baselines.

## Scoring System (MMR)

The score is **MMR** — Elo-style, base 1500, uncapped and continuous, built from
backtest-calibrated components (relative value, region, yield, liquidity, age,
red flags) — displayed as **`score_1000`** on a 0–1000 scale: **500 =
market-typical, 650+ = recommended tier, <450 = below threshold.** Missing data
is neutral, never penalized. The score is one *input* to the verdict — the AI
agent's researched rating is the answer. A separate 0–100 **livability score**
(`scoring/livability.py`) gives an own-stay lens; it is a heuristic and never
folds into MMR.

For the evaluation rubric, component details, and the release history of scoring
changes, see [CLAUDE.md](CLAUDE.md), [docs/evaluation-rubric.md](docs/evaluation-rubric.md),
and [docs/RELEASES.md](docs/RELEASES.md).

## URA Data Integration

[URA](https://www.ura.gov.sg/) provides 5 years of official government transaction data —
the most reliable source for appreciation calculations. Download CSVs from
[URA Property Market Information](https://eservice.ura.gov.sg/property-market-information/pmiResidentialTransactionSearch) into `data/`, then build the cache:

```bash
python invest.py --build-ura-cache data/ura_*.csv
```

The cache (`data/ura_cache.json`, with a CSV export) stores per-project appreciation
rates, transaction counts, momentum, and new-sale proportions. Matched properties
get real data instead of regional estimates.

## Agent Evaluation

When using the agent pipeline (`--raw` → review → `--from-review`), each listing in
the raw JSON carries a nested **`agent_evaluation`** dict to fill in: `rating`
(Buy/Neutral/Avoid), `confidence`, `rating_rationale`, `summary`, `red_flags`,
`catalysts`, rental/appreciation assessments, `stack_notes`, and an optional
appreciation-rate override with source. The rating scale and field guide are in
[docs/evaluation-rubric.md](docs/evaluation-rubric.md).

## Project Structure

```
property-finder/
├── invest.py              # Main CLI: flows, --score, --recall, --search-db, --from-review
├── weekly.py              # Weekly scan: mandate → poll → grade → gate → AI → digest
├── shortlist.py           # Ranked buy-list; ranks on exit record, not the algo score
├── shortlist_ui.py        # Shortlist viewer (:8644) with the value-trap read
├── realsmart.py           # realsmart.sg slug resolution from their sitemap
├── realsmart_cache.py     # Per-project realsmart stats, cached with a 45-day TTL
├── bed_bands.py           # Per-project bedroom→size bands from URA rental contracts
├── backtest.py / backtest_ext.py   # Point-in-time URA backtests of scoring features
├── calibrate_forward.py   # Joins past scores to later URA PSF (forward calibration)
├── config.py              # MMR calibration, weights, thresholds, buyer profile
├── eval_memory.py         # Git-tracked evaluation memory (judgements)
├── profile_memory.py      # Git-tracked condo profiles (physical facts) + listing→stack join
├── listings_db.py         # Master listings sheet (continuously-updated, searchable)
├── poller.py              # PropertyGuru poll cycle: scrape newest → upsert → score new
├── ui.py                  # Fresh-listings + arena UI (:8642); scraping is opt-in (--poll)
├── dashboard.py           # System dashboard (:8643) with headless ANALYZE buttons
├── scoring/
│   ├── full_scorer.py     # Enrichment + component scoring
│   ├── mmr.py             # MMR (uncapped, continuous) + /1000 normalization
│   ├── arena.py           # Condo arena (--fight) tournament engine
│   ├── livability.py      # 0-100 own-stay livability score (heuristic)
│   ├── relative_value.py  # Age/size-normalized peer comparison
│   ├── raw_output.py      # raw_analysis.json (mode-aware AI instructions)
│   ├── models.py          # ScoredListing + verdict mapping
│   └── roi.py / costs.py / rental_estimator.py / future_scorer.py / district_scorer.py / quick_scorer.py
├── scrapers/              # PropertyGuru + URA CSV + headless browser
├── utils/                 # Report generator, geo, logging
├── docs/                  # evaluation-rubric.md, RELEASES.md
├── .claude/skills/        # analyze-development / analyze-listing / market-scan /
│                          #   research-development / arena-cycle / realsmart-data
├── evaluations/           # Past evaluations — judgements (commit; may be stale)
├── profiles/              # Condo profiles — stacks/facings/layouts (commit)
├── data/                  # Reference data, listings DB/sheet, URA + rental caches
├── tests/                 # Test suite
└── output/                # Generated per-run reports (gitignored)
```

## Troubleshooting

**Cloudflare blocks** — PropertyGuru uses Cloudflare protection. Re-run headed and solve
the challenge once; the clearance cookie is stored in `chrome-profile/` and reused:

```bash
python invest.py --districts 3 --no-headless --max-pages 1   # solve the challenge manually
```

Only if the profile itself is corrupt, `rm -rf chrome-profile/` and solve again — that
deletes the clearance cookie every scraper path depends on, so it is a last resort, not
a first step.

**`facing` is always empty, `floor_level` is sparse** — not a bug. PropertyGuru does not
serve a facing/direction on listing detail pages (null on every sampled listing), so
`facing` stays at 0% coverage; use a researched profile in `profiles/` for orientation.
`floor_level` is optional for the posting agent and tops out around 35–40%.

**No URA data for a property** — the scorer falls back to the **regional
appreciation baseline** (`REGIONAL_APPRECIATION_BASELINES` in `config.py`:
CCR 2.8% / RCR 3.7% / OCR 4.2%, default 3.5%/yr). For real per-project data,
download the relevant district CSVs from URA and build the cache.

**Missing dependencies** — `pip install -r requirements.txt && python -m patchright install chromium`

## Contributing

Contributions are welcome. Some areas that could use help:

- **More data sources** — integrate 99.co, EdgeProp, or SRX listings
- **Better rental estimation** — real-time rental data instead of district medians
- **International markets** — adapt the scoring framework for other countries
- **Test coverage** — expand the test suite for scoring edge cases

Please open an issue to discuss larger changes before submitting a PR.

## Disclaimer

This tool is for **educational and research purposes only**. It is not financial advice. Property investment carries risk, and past performance does not guarantee future results. Always do your own due diligence and consult a qualified financial advisor before making investment decisions. The authors are not responsible for any losses incurred from using this tool.

## License

[MIT](LICENSE)
