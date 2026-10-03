---
name: market-scan
description: Flow C — scan many candidates across districts and produce a rated shortlist (Buy/Neutral/Avoid each, strongest first). Use when the user asks to find condos in an area, pastes a results-page URL, or wants a comparison across districts/budgets.
---

# Market Scan (Flow C)

**Altitude**: a comparable shortlist. Every shortlisted listing gets a
Buy/Neutral/Avoid rating + confidence. **"No buys in this batch" is a valid
outcome** — don't manufacture a winner.

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
With a profile, start from `python my_shortlist.py` — the exit-screened,
profile-filtered candidate list with every rejection reasoned — and research
down it rather than re-deriving the scope.

## Steps

1. **Gather** (pick one):
   ```bash
   python invest.py --discover-districts                  # see district rankings
   python invest.py --auto --beds 2,3 --top 20            # auto-pick districts
   python invest.py --districts 3,5,14,15 --beds 2,3      # specific districts
   python invest.py --url "<PG search-results URL>"       # from a results page
   ```
   Creates `output/run_NNN/raw_analysis.json`.

2. **Recall per candidate** as you go:
   ```bash
   python invest.py --recall "<condo name>"
   ```
   For genuine contenders, add realsmart.sg per-project context via the
   `/realsmart-data` skill (or `python realsmart_cache.py --show "<condo>"`) —
   REALSCORE and % of resales sold at a profit, always reported with the
   transaction count. Their ToS is personal-use: look up contenders, not the
   whole scan.

**Forward-signal priors**: see `docs/evaluation-rubric.md` ("What the data
actually predicts") — value + region lead; trailing CAGR/momentum ≈ 0 forward
power; model explains <10% of forward variance, so calibrate confidence.

3. **Research & evaluate**:
   - Cover ALL shortlisted listings, not just the algo's top ranks — the
     ordering is an input, not the shortlist
   - Note: the quick filter drops listings with very incomplete scraped data;
     mention in `agent_methodology_notes` if coverage looked thin
   - Web search per project: reviews, issues, transactions; area trends and
     upcoming supply once per district
   - **Stack profiles**: `factual_data.stack_profile` carries the matched
     stack/facing/known-issues. Only `status: not_researched` for a **genuine
     contender** (one you'd rate Buy/Neutral) is worth a `/research-development`
     pass — don't research every candidate in a broad scan.
   - Trust rules: if factual_data shows `ask_above_own_stack_prints`,
     `ask_below_stack_prints`, or `stack_low_floor_share` ≥ 0.7, the unit's
     apparent discount is a floor/PES/loft artifact — treat the stack's own
     recent (24mo) prints as the true comp and verify before crediting any
     deep (>25%) discount or >5.5% gross yield.
   - Rubric: `docs/evaluation-rubric.md`

4. **Fill `agent_evaluation`** per listing (`rating`, `confidence`,
   `rating_rationale` mandatory). Executive summary ordered by rating strength.

5. **Report** (auto-saves evaluation memory; commit `evaluations/`):
   ```bash
   python invest.py --from-review output/run_NNN/raw_analysis.json --output output/run_NNN/final
   ```

## Keeping the sheet fresh (optional)

```bash
python invest.py --update-db --districts 3,5,14,15 --beds 2,3   # refresh only, no report
python invest.py --list-db                                       # stats + price drops
```
