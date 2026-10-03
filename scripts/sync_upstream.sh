#!/usr/bin/env bash
# Merge the upstream repo (ong6/sg-property-analysis) into this fork, then
# re-derive the data this fork owns.
#
# Policy
#   * Code: a normal merge. If code files conflict the script stops with the
#     merge in progress so you can resolve them (git status / git merge --abort).
#   * Upstream-owned data (listings DB, condo URA CSVs, caches, score logs):
#     ALWAYS take upstream's version, conflict or not. The owner refreshes these
#     weekly; the fork never edits them by hand, it only re-derives from them.
#   * Fork-owned data (EC CSVs, ec_rules.json, primary_schools.json): keep ours.
#   * Then: EC-only URA export folded into the caches (needs the URA key) and a
#     full rescore, so EC listings are again benchmarked against EC prints.
#
#   scripts/sync_upstream.sh            # merge upstream/main
#   UPSTREAM_REF=upstream/dev scripts/sync_upstream.sh
#   SKIP_RESCORE=1 scripts/sync_upstream.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PY:-venv/bin/python}"
[ -x "$PY" ] || PY=python3
UPSTREAM_REF="${UPSTREAM_REF:-upstream/main}"

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "Working tree not clean — commit or stash first." >&2
  exit 1
fi
git remote get-url upstream >/dev/null 2>&1 || {
  echo "No 'upstream' remote. Add it: git remote add upstream https://github.com/ong6/sg-property-analysis.git" >&2
  exit 1
}

echo "==> fetching ${UPSTREAM_REF%%/*}"
git fetch -q "${UPSTREAM_REF%%/*}"
if git merge-base --is-ancestor "$UPSTREAM_REF" HEAD; then
  echo "Already up to date with $UPSTREAM_REF."
  exit 0
fi

echo "==> merging $UPSTREAM_REF"
git merge --no-ff --no-commit "$UPSTREAM_REF" >/dev/null 2>&1 || true

# Upstream-owned data: take theirs unconditionally (re-derived below).
UPSTREAM_DATA=$(git ls-tree -r --name-only "$UPSTREAM_REF" -- data \
  | grep -E '^data/(listings_db\.json|listings_sheet\.csv|mmr_history\.csv|lens_history\.csv|ura_cache\.(json|csv)|rental_cache\.json|ura_district_D[0-9]{2}\.csv|ura_rental_D[0-9]{2}(_[0-9_]+)?\.csv)$' || true)
for f in $UPSTREAM_DATA; do
  git checkout "$UPSTREAM_REF" -- "$f"
done

# Fork-owned data: keep ours if anything touched it.
for f in $(git diff --name-only --diff-filter=U | grep -E '^data/(ura_(district|rental)_D[0-9]{2}_EC\.csv|ec_rules\.json|primary_schools\.json)$' || true); do
  git checkout --ours -- "$f" && git add "$f"
done

CONFLICTS=$(git diff --name-only --diff-filter=U || true)
if [ -n "$CONFLICTS" ]; then
  echo "Code conflicts need a human — merge left in progress:" >&2
  echo "$CONFLICTS" | sed 's/^/  /' >&2
  echo "Resolve, 'git add' them, then re-run the data steps:" >&2
  echo "  $PY ura_api.py --ec-only && $PY invest.py --score-db && git commit" >&2
  exit 2
fi

echo "==> re-deriving EC data (URA API)"
if "$PY" ura_api.py --check >/dev/null 2>&1; then
  "$PY" ura_api.py --ec-only >/dev/null
else
  echo "   URA key unavailable — rebuilding caches from the EC CSVs already on disk"
  "$PY" -c "import glob, invest; invest.build_ura_cache_from_csv(sorted(glob.glob('data/ura_district_D[0-9][0-9]_EC.csv')))" >/dev/null 2>&1
  "$PY" build_rental_cache.py >/dev/null
  "$PY" bed_bands.py --build >/dev/null
fi

if [ -z "${SKIP_RESCORE:-}" ]; then
  echo "==> rescoring the listings book"
  "$PY" invest.py --score-db 2>&1 | tail -3
fi

echo "==> running tests"
"$PY" -m pytest -q 2>&1 | tail -2

git add -A data
git commit -q -m "Merge $UPSTREAM_REF; re-derive EC data and rescore

Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
echo "==> done: $(git --no-pager log --oneline -1)"
