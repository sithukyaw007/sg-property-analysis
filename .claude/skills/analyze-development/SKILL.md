---
name: analyze-development
description: Flow A — evaluate a whole condo development by name or project URL. Produces a development-level verdict plus best/worst stacks, facings and floors, with a Buy/Neutral/Avoid rating per unit type. Use when the user names a condo ("analyze The Continuum", "is Grand Dunman good?") or gives a PropertyGuru project page.
---

# Analyze a Development (Flow A)

**Altitude**: the question is whether the DEVELOPMENT is worth buying into, and
which stacks/facings/floors to target or avoid. Rate each unit type too.

**Purpose check first**: metrics assume investment (5–7yr hold). If the request
hints at own-stay or is ambiguous, ask the user before rating
(see `docs/evaluation-rubric.md` → Purpose check). For own-stay weight the
0–100 livability score (`scoring/livability.py` — heuristic, never folded
into MMR) alongside the rubric's own-stay lens.

**Buyer profile (fork)**: run `python buyer_profile.py` first. When a profile is
active it already answers the purpose check and the buyer type — don't ask
again. Apply its mandate (budget, beds, districts, condo/EC), its `buyer_type`
for ABSD (the ROI block already uses it), and its household citizenship for EC
eligibility (`factual_data.ec_status.household_eligible` — a PR-only household
cannot buy an EC still within its MOP). For own-stay also read
`python -m scoring.personal_fit "<condo>" <district>` (commute + primary
schools; never evidence of returns). EC rules: `docs/evaluation-rubric.md` →
Executive Condominiums.

## Steps

1. **Recall** — past evaluations are references, not truth; re-verify prices:
   ```bash
   python invest.py --recall "<condo name>"
   python invest.py --search-db "<condo name>"
   python invest.py --profile "<condo name>"   # existing stack/facing/layout profile
   ```

2. **Gather**:
   ```bash
   python invest.py --condo "<name>" --beds 1,2,3            # by name
   python invest.py --url "<propertyguru project URL>"        # or by project page
   ```
   Creates `output/run_NNN/raw_analysis.json` (one entry per unit type).

   Also pull per-project market context from realsmart.sg via the
   `/realsmart-data` skill (or `python realsmart_cache.py --show "<name>"`) —
   REALSCORE, % of resales sold at a profit, avg holding period. Record it in
   `agent_evaluation` *with the transaction count*: 5.0 across six resales is
   noise. It is backward-looking downside evidence, never an appreciation forecast.

**Forward-signal priors**: see `docs/evaluation-rubric.md` ("What the data
actually predicts") — value + region lead; trailing CAGR/momentum ≈ 0 forward
power; model explains <10% of forward variance, so calibrate confidence.

3. **Research & evaluate** (form your own view BEFORE looking at algo_reference):
   - Web search: `"<name> Singapore review"` (build quality, developer, defects),
     `"<name> price trend transactions"` (resale PSF direction),
     `"<name> floor plan site plan facing"` (stack/facing/floor quality)
   - Read `factual_data` per unit type; rubric: `docs/evaluation-rubric.md`
   - Trust rules: if factual_data shows `ask_above_own_stack_prints`,
     `ask_below_stack_prints`, or `stack_low_floor_share` ≥ 0.7, the unit's
     apparent discount is a floor/PES/loft artifact — treat the stack's own
     recent (24mo) prints as the true comp and verify before crediting any
     deep (>25%) discount or >5.5% gross yield.
   - Optional sanity-check: `python invest.py --score '{...}'` — remember it
     takes your inputs at face value; it is not independent confirmation.

4. **Persist the physical findings to a profile** — this flow already researches
   the site plan/stacks/facings, so don't let it evaporate into freeform notes:
   run **`/research-development "<name>"`** (or build the profile JSON and
   `python invest.py --save-profile <file>`) so every future listing in this
   condo auto-joins to real stack data. Cite sources; set `confidence` honestly;
   never invent a stack. This is the structured home for what used to go only in
   `stack_notes`.

5. **Fill `agent_evaluation`** for each unit type in `raw_analysis.json`. Put
   stack/facing/floor guidance in `stack_notes`; overall development verdict in
   `report_level.agent_executive_summary`. Always set `rating`, `confidence`,
   `rating_rationale`.

6. **Report** (auto-saves evaluation memory; commit `evaluations/` and `profiles/`):
   ```bash
   python invest.py --from-review output/run_NNN/raw_analysis.json --output output/run_NNN/final
   ```
