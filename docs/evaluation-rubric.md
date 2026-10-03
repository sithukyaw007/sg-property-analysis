# Condo Evaluation Rubric

Read this before filling any `agent_evaluation`. The technical score is one input —
**you** make the call. Form your view from `factual_data` and your own web research
FIRST; use `algo_reference` and past evaluations to pressure-test it, not as a
starting point (anti-anchoring).

## Purpose check (before anything else)

Everything the scripts compute (yield, ROI, liquidity, MMR) assumes an
**investment** purpose with a 5–7 year hold. If the user's request suggests
**own-stay** — or is ambiguous — **ask the user which it is before rating.**

**Own-stay ADDS constraints; it does not remove them.** This section used to read
the other way — that own-stay relaxed the money tests, so a quiet low-liquidity
boutique "can be a fine home". That framing was wrong for this buyer and is
corrected here. The owner's mandate is explicitly *"whichever makes the most
money"*, and an own-stay purchase is still the household's largest leveraged
position; a home that cannot be exited is a bad home, not a different kind of
good one. So own-stay is an INTERSECTION, never a substitution: run the full
investment screen, then narrow to what the household can actually live in.

Every home is eventually sold. The only question own-stay changes is *when* and
*under what pressure* — and a family that has to sell on a school timetable or a
job move has less control over timing than an investor does, which makes exit
liquidity MORE important to them, not less.

| | Investment | Own-stay |
|---|---|---|
| Screen | Exit demand, appreciation, liquidity, yield | The same screen, **then** livability/layout/schools/commute as a filter on the survivors |
| Yield/ROI | Central | Secondary, never ignored — it is the carry cost of the home |
| Liquidity | Critical (can you exit?) | **Still critical** — and timing is less under your control |
| Unit size | Efficient = better | Generous is a genuine plus, but a size the resale market does not want is still a cost |
| Verdict framing | Buy/Neutral/Avoid as investment | Same verdict, **plus** whether the household can live there |

The only red flag that genuinely inverts is `oversized_unit`: extra space is
worth paying for when you live in it. Everything else survives — a thin resale
market, a bad exit record or a quantum above the segment's exit ceiling are as
disqualifying for a home as for an investment. Say explicitly which rubric you
applied, and if own-stay, say which lifestyle constraints narrowed the list and
what they cost in exit terms.

For own-stay, weigh
the 0–100 **livability score** (`scoring/livability.py`: baths/bed, space/bed, MRT
walk, floor, facing, age; 50 = neutral) alongside this rubric — it's an explicit
heuristic, never folded into MMR, and never evidence of returns.

**Its facing term is effectively always dormant.** PropertyGuru does not serve a
facing/direction on listing pages, so `facing` is null on the whole book and the
component scores neutral. If facing matters to the call, get it from a researched
profile (`/research-development` → `profiles/<slug>.json`) and say so — do not
read a neutral livability score as "facing is fine". `floor_level` is available
but only on enriched listings, and caps around 35–40% coverage.

**Personal anchors (buyer profile).** When `profile.local.json` sets anchors,
`scoring/personal_fit.py` adds a second own-stay axis (0–100, 50 = neutral):
public-transport commute to the workplace (OneMap, weekday rush hour; a
straight-line fallback is labelled as such) and primary schools within the
profile's radii (default 1 km / 2 km, straight-line from the development
centroid). Like livability it filters survivors and is never evidence of
returns. School counts are a screening proxy — P1 priority uses MOE's
home-school distance for the specific block, so confirm on OneMap SchoolQuery
before telling the household a school is "within 1 km".

## Executive Condominiums (EC)

An EC is a separate URA product — subsidised public housing that becomes a
private condo on a fixed timetable — and must be judged as one. Facts
(MND, 8 May 2026; dated in `data/ec_rules.json`, re-verify before relying):

| Land tender closed | MOP | Fully privatised | Notes |
|---|---|---|---|
| before 8 May 2026 (every EC on the resale market today) | 5 yrs | after 10 yrs | — |
| on/after 8 May 2026 (launches ~2027+) | 10 yrs | after 15 yrs | no Deferred Payment Scheme; 90% first-timer quota for 2 yrs |

Who may buy: **new or within MOP** — households with at least one Singapore
Citizen, within the income ceiling (≤ $16k; $18k for sites tendered from 24 Aug
2026), MSR 30% on developer sales. **After MOP, before privatisation** — SC and
PR households, no income ceiling. **Privatised** — anyone. A PR-only household
can only buy post-MOP resale ECs. `factual_data.ec_status` gives the stage,
`mop_ends_year`, `privatised_year`, `years_to_privatisation`, a `boundary_year`
flag (TOP is a proxy for key collection — verify the exact date) and
`household_eligible` for the active profile.

How to evaluate one:
- **Comps are EC prints only.** The scorer now benchmarks ECs against EC prints,
  EC district peers and EC rents. An EC that looks "cheap" next to private
  condos is not cheap; that was the old artifact (EC valuation median 72 → 52
  once fixed). EC identity comes from URA's records, so a privatised EC that the
  portal calls "Condominium" is still compared with ECs.
- **Trailing appreciation is mostly the lifecycle.** EC resale CAGRs (median
  ~7%/yr 2021–26 vs ~4.7% for OCR condos) carry the subsidised launch price
  unwinding and the MOP/privatisation re-ratings. The scorer caps an
  unprivatised EC's rate at the regional baseline (`raw_rate_before_bias_
  adjustment_pct` keeps the raw number). A buyer entering after MOP captures at
  most the remaining privatisation step — say how many years are left and
  whether the price already reflects it (compare with privatised neighbours).
- **Exit pool shifts at each milestone.** Post-MOP the buyer pool is SC/PR
  (often HDB upgraders — deep in OCR); after privatisation it widens to
  foreigners, who rarely buy OCR ECs. Weigh the upgrader pool, not the
  privatisation headline.
- **The forward model was never validated on ECs.** The backtest and
  validation panels are condo-only (EC files are excluded by design). EC MMR
  rests on structure (size, liquidity, region, age) measured on condos — keep
  confidence at medium or below unless physical/price evidence is decisive.

## What the data actually predicts (backtest-calibrated priors, v3.5 — full 28-district panel)

Read this BEFORE the per-factor sections below — it overrides older intuitions
baked into them. From point-in-time URA backtests (`backtest.py` / `backtest_ext.py`,
**106k txns, all 28 districts**, 2021–26, mean-reversion corrected). Forward =
realized 2yr resale-PSF appreciation:

- **Cheap vs district peers is the strongest robust forward signal** (ρ≈−0.25;
  the one signal that survives controlling for region). Lead your appreciation
  view with `factual_data.relative_value.premium_vs_age_adjusted_median_pct`
  (negative = cheap for its age) and `psf_premium_vs_ura_median_pct`.
- **Region matters and the old priors were INVERTED.** Realized forward returns
  on the full panel: **OCR ≈ +3.7%/yr > RCR ≈ +3.1% ≫ CCR ≈ +1.9%**. Do **not**
  treat CCR/prestige as a forward edge — but the tilt is a long-run average,
  **not a law**: at era level (v3.10 honest re-read) OCR led in 3 of 5 eras
  and 43/81 rolling windows since 2004, while **CCR won 20/81** (2004–08
  outright, and most recently 2023Q3→2025Q3). Expect rotation; don't over-bet
  either side.
- **MRT proximity is a real but modest forward signal** (as-of-T std_β ≈
  **−0.11**, CI excludes 0; the previously-quoted −0.16 was inflated ~⅓ by
  counting stations that hadn't opened yet). Weigh `mrt_distance_m`, but as a
  tiebreaker, not a thesis.
- **District buyer-pool depth carries real content beyond region** (std_β
  +0.16, CI +0.07..+0.24 — survives clustering). Bigger developments tilt
  mildly positive (dev_size marginal ≈ +0.05, undetermined).
- **Absolute price "cheapness" is mostly the region effect in disguise** — once
  region is controlled it vanishes (ridge weight ≈ 0). A low PSF is not an
  independent buy signal; it's mostly "this is OCR." Don't double-count it.
- **Trailing CAGR has little forward power (marginal ≈ +0.03) and momentum is
  unstable across samples.** Use `annual_rate_pct` / `momentum` for the
  *story/catalyst*, never as the forward number.
- **Freehold is NOT a forward edge** — exactly neutral forward on the full panel
  (marginal ≈ 0; the earlier "freehold underperforms" was a 13-district artifact).
  It carries a ~5% PSF *level* premium — already in the price you pay. Weigh
  tenure only as own-stay/optionality and short-remaining-lease *downside*.
- **Real-rent yield predicts CARRY, not price — confirmed and stronger (v3.10).**
  Joined actual URA rental contracts: gross yield's multivariate forward-price
  std_β is **−0.16** (CI excludes 0) — high yield predicts *lower* forward PSF
  growth after controls. A 1pp yield edge is still ~1pp/yr of total return —
  count it as income, never as an appreciation argument. Rents come from real
  URA contracts where available (`rent_source: ura_project_bed` /
  `ura_project`; check `rent_evidence` for contract depth and whether the
  sqft was capped).
- **The future-infrastructure score has NO measured forward power** (marginal ≈ 0
  district-level). "Transformation upside" is narrative until proven — don't let
  it carry a rating.
- **Liquidity is an exit-risk gate, not a forward driver** (marginal ≈ 0/negative
  once value is in). Penalize genuinely thin names; don't push a fairly-priced,
  liquid-enough unit up.
- **The ceiling is low: the full model explains ~6–13% of forward variance**
  (composite ρ +0.29, CI +0.21..+0.36, **563 effective projects, in-sample,
  one bull regime** — the honest framing after the v3.10 audit; the true
  out-of-sample read starts ~mid-2027 via calibrate_forward.py). Small
  `score_1000` gaps are noise. Reserve **high** confidence for cases where
  physical/catalyst evidence (not the score) is decisive; treat near-ties as
  Neutral.

## Investment rubric (5–7 year hold)

### 1. Value first, then appreciation (see the priors section above)

The forward signal is **value (cheap vs district peers) + region**, NOT trailing
appreciation. Lead with value; use the appreciation rate only for the story.

From `factual_data` — value (primary):
- `relative_value.premium_vs_age_adjusted_median_pct` — negative = cheap for its
  age vs district peers (the strongest robust forward signal). Also
  `relative_value.premium_vs_new_launch_implied_pct`.
- `psf_premium_vs_ura_median_pct` (signed) and `psf_cohort_txns` — how cheap/dear
  vs same-size sales, and how many sales back that read (thin cohort → low trust).

From `factual_data.appreciation` — story/context (secondary, ~0 forward power):
- `annual_rate_pct` + `data_source` — `ura_*` reliable; `regional_baseline` means no
  project data (research it). **Trailing CAGR does not predict forward returns** —
  read it for the narrative, not as the forward number.
- `momentum` — flat-to-contrarian; informational only.
- `raw_rate_before_bias_adjustment_pct` — if present, the system already discounted
  new-launch developer-pricing inflation. **Do not override the adjusted rate with a
  portal headline CAGR.** Only override with resale-to-resale evidence (and a stated
  catalyst with a date).

Research: catalysts (MRT, govt zones, en-bloc), supply, developer reputation.

Regional forward priors: see "What the data actually predicts" above (OCR > RCR ≫
CCR, regime-tested; config baselines CCR 2.8 / RCR 3.7 / OCR 4.2%/yr). Region is a
strong but coarse prior — judge the specific project, and don't treat CCR/prestige
as a forward edge (nor over-bet OCR; CCR can mean-revert).

### 2. Liquidity & exit risk

From `factual_data.liquidity`: `ura_transaction_count` (50+ strong, <10 thin),
`buyer_pool_depth`, `total_units` (400+ ideal). Your judgment: price band
($1.8M–2.5M deepest pool; >$3M thins), HDB-upgrader appeal, broad vs niche unit.

### 3. Future potential

`factual_data.future_infrastructure` — future MRT within 500m is the strongest
uplift. Govt zones by impact: JLD (D22), GSW (D4/5), Paya Lebar Central (D14),
PDD (D19), Woodlands RC (D25). CRL ~2030, JRL ~2028.

### 4. Rental yield

`rental.gross_yield_pct` — 4%+ excellent at $2M+; 3–3.5% typical. Check tenant
demand drivers (CBD, hospitals, universities, MRT) and rental oversupply from
nearby new launches. 2BR has the widest tenant pool.

**An implausible yield is a verify-first signal, not a bonus** (the mirror of
the deep-discount rule). Yield = rent ÷ price, so a mis-scraped/stale/bait
price or a non-comparable format (strata villa, dual-key, combined unit)
manufactures a spectacular yield from a perfectly real rent — e.g. a 5,349sqft
strata unit at an artifact $636psf "earned" ~8% gross on the project's genuine
URA 4BR rent. SG condo gross yields above ~5.5% are almost always a price or
rent artifact: before crediting one, (a) check the ask against the project's
own 12-month prints, (b) check `rent_source` — `ura_project_bed`/`ura_project`
are real URA contracts, `district_*`/`fallback` are synthetic constants, and
(c) check the unit's format/sqft is the thing the rent series actually priced.
v3.6.1 tanh-caps the MMR yield credit, but the dossier still displays the raw
`gross_yield_pct` — the number itself stays wrong until you verify it.

### 5. Costs & taxes

`roi_projections` / `roi_sensitivity` already include BSD, ABSD, SSD, property
tax. SSD is why holds are modeled at 5–7yr. ABSD follows the active buyer
profile (`buyer_type`, `property_count`; SC first property = 0%, PR first
property = 5%) — without a profile the model assumes an SC's first property.

### Relative value: new launch vs resale, age-adjusted

`factual_data.relative_value` (when district peer coverage allows) answers
"is this condo cheap or dear *for its age*?" using real URA transacted PSF:

- `new_launch_median_psf` — what new launches (≤3yr) actually transact at in
  this district: the current new-launch market price
- `implied_fair_psf_from_new_launch` — new-launch median minus the measured age
  decay. The decay is **piecewise** (`AGE_PSF_DECAY_SEGMENTS`, district-FE
  hedonic, all regions agree): ~3%/yr (≈$50/psf/yr) ages 0–10, a 10–15yr plateau
  (~0.5%/yr), ~2.6%/yr at 15–20, then slow drift; freehold ×0.6 (unmeasured
  heuristic). A flat $/yr rule under-adjusts new-vs-old comparisons ~2× inside
  0–10yr — don't substitute one in your own reasoning.
- `premium_vs_new_launch_implied_pct` — subject asking PSF vs that implied
  fair value; negative = priced below what new-launch pricing implies
- `premium_vs_age_adjusted_median_pct` — vs the age-normalized median of ALL
  district peers (headline; negative = cheap for its age)

Use both: a resale can look "cheap" vs a new launch simply because it's old —
the age adjustment is what makes the comparison fair. When the block is
missing (thin peer data), research new-launch and resale PSF for the area
yourself before judging value.

### 6. Red flags — verify, don't just inherit

Algorithmic flags in `factual_data.red_flags_detected` (west facing, small dev,
low lease, old property, PSF mismatch flags) are *detections*, not verdicts —
check each. Also research what the algorithm can't see: construction defects,
noise sources, stigma, developer track record, competing launches.
`psf_premium_vs_ura_median_pct` is signed: negative = listed below recent
transactions (worth investigating why — could be motivated seller or a problem).

**Discount trust (v3.6–v3.9) — a deep discount is a verify-first signal, not
value.** The ranking *selects* for asks that look cheap by artifact, so before
crediting any discount deeper than ~25% vs comps, verify sqft/format/price
against the project's own URA prints. Specific flags in `factual_data`:

- `ask_above_own_stack_prints` / `stack_premium_pct` > +5 / `stack_low_floor_share`
  ≥ 0.7 — the "discount" is a ground-floor/PES artifact: patio sqft prices cheap
  vs the coarse size band while the unit actually asks *above* its own stack's
  prints. Treat the stack's own recent prints as the true comp; expect thin exit
  demand.
- `ask_below_stack_prints` — ask sits below the 10th percentile of a deep (n≥8)
  recent same-size print set: double-volume loft (void counts in strata sqft) or
  stale/bait pricing. Real sellers don't price 15–20% under every recent print.
- `bedroom_sqft_mismatch` or a deep discount on a thin `psf_cohort_txns` — likely
  mis-scrape or non-comparable format (strata villa, dual-key, combined unit).
- An ask merely cheap-but-inside the print distribution (p10–p50) is the
  genuine-deal zone — that's what the ranking exists to find.
- **Always compare against recent (24mo) prints, not all-time** — stale prints
  fake premiums (and discounts) in a moved market.

### Developer (reference, v3.3)

`factual_data.developer` (present for backfilled / previously-evaluated projects)
gives the attributed `developer` / `developer_group`, a `confidence`, and the
group's other projects in our data (`also_built_in_our_data`). Treat it as a
**research prompt, not a verdict**: confirm build quality, defect history (CONQUAS
/ news), and delivery track record yourself. `portfolio_median_appreciation_pct`
is context only — past CAGR is a weak forward signal (the v3.3 backtest), so a
strong-appreciating portfolio is not a guarantee. If the block is absent, research
the developer from scratch (it's still a real factor). Low `confidence` = verify
the attribution before relying on it.

## Technical score (MMR)

The scorer outputs an Elo-style **MMR** (base 1500, uncapped, continuous) and its
0–1000 normalization (`score_1000`; 500 = market-typical, 650+ recommended tier).
Missing data scores neutral, never penalized. Sanity-check your evaluation with:

```bash
python invest.py --score '{"price":2300000,"sqft":958,"psf":2401,"beds":2,"district":"D15","tenure":"Freehold","total_units":420,"project_name":"The Continuum","appreciation_rate_pct":5.5,"monthly_rent":6200}'
```

⚠ If you pass `appreciation_rate_pct`/`monthly_rent`, the score takes your numbers
at face value — it validates internal consistency, **not** your research. Don't
cite it as independent confirmation of inputs you supplied.

## Rating scale

| Rating | Verdict | Meaning |
|--------|---------|---------|
| Strong Buy | 🟢 BUY | Excellent fundamentals, no major flags, clear catalysts |
| Buy | 🟢 BUY | Solid fundamentals, minor concerns |
| Neutral | 🟡 NEUTRAL | Mixed signals; strengths and concerns balance |
| Avoid | 🔴 AVOID | Poor fundamentals, major flags, or overpriced |

Always set `rating` AND `confidence`, and explain the rating in
`rating_rationale` (it renders directly under the verdict). **"No buys in this
batch" is a valid scan outcome** — don't manufacture a winner.

**Calibration audit:** `python invest.py --eval-stats` shows the rating and
confidence distribution against drift bands (overall + post-2026-06-10
current-priors cohort). The bands are alarms, not quotas — but if Strong Buy
is at ~0% across hundreds of evals (it is, 2026-06: 0.3%), the label has gone
dead: a genuinely exceptional setup (deep verified discount + dated catalyst +
deep liquidity) deserves it, and grading everything Neutral is its own form of
miscalibration. Use the full scale; reserve extremes for evidence, not vibes.

## agent_evaluation fields

```json
{
  "summary": "2-3 sentence investment thesis",
  "rating": "Strong Buy / Buy / Neutral / Avoid",
  "rating_rationale": "Specific reasons, numbers and facts",
  "red_flags": ["specific risk"],
  "catalysts": ["specific growth driver"],
  "rental_assessment": "...", "appreciation_assessment": "...",
  "stack_notes": "Best/worst stacks, floors, facing (development mode)",
  "confidence": "high/medium/low",
  "appreciation_rate_override_pct": 4.5,
  "appreciation_rate_source": "resale-to-resale basis only"
}
```

Report-level: `agent_executive_summary` (verdict + picks), `agent_market_commentary`,
`agent_methodology_notes` (data gaps, what you couldn't verify).

## District reference (verify before relying — written Jun 2026)

- **RCR**: D03 Queenstown (GSW, TEL), D05 Clementi (One-North), D14 Paya Lebar
  (airbase relocation), D15 East Coast (deep buyer pool)
- **OCR**: D22 Jurong (JLD "2nd CBD", JRL), D19 Punggol (PDD, CRL), D18 Tampines
  (CRL+DTL), D16 Bedok/Upper East Coast (TEL, Bayshore precinct)
- **CCR**: D09/10/11 prime — high entry cost, typically lower yield

These labels are coarse priors. The quick-scorer's district points and the regional
baselines both encode them too — don't triple-count the same prior in your own
reasoning; judge the specific project.
