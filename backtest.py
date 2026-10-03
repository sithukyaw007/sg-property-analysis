"""Point-in-time backtest of the scoring algo's predictive features.

We have no historical *score* log (mmr_history started Jun 2026), so the only
ground truth is the URA transaction panel itself (data/ura_district_D*.csv;
106,749 rows across all 28 districts as of Jun-2026 — this harness was written
against the original 13-district ~65k slice, see docs/RELEASES.md v3.5b). This
harness REPLAYS that panel point-in-time:

  1. Pick a split date T.
  2. For each project, compute the features the algo leans on -- using ONLY
     transactions on/before T (no look-ahead): trailing PSF CAGR, momentum,
     transaction volume, PSF vs district median, freehold flag, new-sale share.
  3. Measure the REALIZED forward resale-PSF appreciation after T.
  4. Ask: do the as-of-T features actually rank forward winners? (Spearman +
     quintile forward-return tables.)

The headline question (asked at v3.2, when trailing appreciation/momentum was
~55-60% of MMR variance): does it predict forward returns, or do returns
mean-revert — i.e. does the algo's biggest weight point the wrong way?
ANSWERED, and the answer shipped: trailing CAGR ~0 forward power, momentum
flat/contrarian, cheap-vs-district the strongest channel. Momentum weight is
now 0 and the appreciation slope 7.5 -> 3 (docs/RELEASES.md v3.3/v3.4/v3.5).
Re-run this harness after any weight change or panel growth.

Index method (v1): per-project median resale PSF over 12-month windows.
Resale + Sub Sale only (New Sale is developer pricing). Composition shift
across unit sizes is a known caveat -- see --size-control for a stratified
robustness variant. Medians + resale-only + min-count filters keep v1 honest
enough for a first read.

Honesty rails (Jun-2026 audit):
  * The default splits (2023.75/2024.0/2024.25) are 0.25yr apart with a 2yr
    forward window -> consecutive forward windows overlap ~87.5% and ~74% of
    projects appear in all three splits. Pooled rows are NOT independent;
    effective n ~= unique projects, not rows. Every pooled run now prints
    effective-n diagnostics; use --split-spacing >= --window for a clean
    non-overlapping panel.
  * Spearman uses proper midranks (argsort().argsort() assigned row-order
    ranks within ties — correlated with district file order, ~±0.02 artifact
    on binary/quantized features).
  * Trailing-window coverage is printed per split (data starts 2021.38, so
    the (T-3,T-2] window is truncated at the early default splits).
  * The split-sample fallback (rows whose price-at-T base is shared between
    features and outcome because n_recent < 2*min_txn) is counted and printed.

Usage:
  python backtest.py                      # default multi-split run + report
  python backtest.py --split 2023.99 --window 2.0
  python backtest.py --split-spacing 2.0  # non-overlapping clean mode
  python backtest.py --min-txn 5 --dump out.csv
"""

import argparse
import csv
import glob
import math
import os
from collections import defaultdict

import numpy as np

_MON = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}

# Condo/apartment prints only. Executive Condominium prints (ura_district_D*_EC.csv)
# are a separate product with its own price level and stay OUT of the panel, so
# every backtest/validation read is unchanged by the EC export. EC_DATA_GLOB is
# for consumers that score EC projects on their own history (exit_demand).
DATA_GLOB = os.path.join(os.path.dirname(__file__), "data", "ura_district_D[0-9][0-9].csv")
EC_DATA_GLOB = os.path.join(os.path.dirname(__file__), "data", "ura_district_D[0-9][0-9]_EC.csv")


def _parse_date(s: str):
    """'May-26' -> float year 2026.375 (mid-month). None if unparseable."""
    s = (s or "").strip()
    try:
        mon, yr = s.split("-")
        return (2000 + int(yr)) + (_MON[mon] - 0.5) / 12.0
    except Exception:
        return None


def _num(s: str):
    try:
        return float(str(s).replace(",", "").strip())
    except Exception:
        return None


def _open_csv(path):
    """URA exports are windows-1252 when a project name carries an accent
    (e.g. ENCHANTÉ) — try utf-8 first, fall back instead of crashing."""
    try:
        fh = open(path, encoding="utf-8")
        fh.read(1 << 20)
        fh.seek(0)
        return fh
    except UnicodeDecodeError:
        return open(path, encoding="windows-1252")


def load_txns(paths=None):
    """Load resale-relevant URA transactions as dicts."""
    paths = paths or sorted(glob.glob(DATA_GLOB))
    out = []
    for p in paths:
        with _open_csv(p) as fh:
            for row in csv.DictReader(fh):
                t = _parse_date(row.get("Sale Date", ""))
                psf = _num(row.get("Unit Price ($ PSF)"))
                if t is None or not psf:
                    continue
                out.append({
                    "project": row["Project Name"].strip().upper(),
                    "district": row.get("Postal District", "").strip(),
                    "t": t,
                    "psf": psf,
                    "sqft": _num(row.get("Area (SQFT)")),
                    "sale_type": row.get("Type of Sale", "").strip(),
                    "tenure": row.get("Tenure", "").strip(),
                    "segment": row.get("Market Segment", "").strip(),
                })
    return out


def _median(xs):
    return float(np.median(xs)) if xs else None


def _win_vals(txns, lo, hi, sqft_band=None):
    """PSF list of resale txns with sale date in (lo, hi], time-ordered."""
    vals = [(x["t"], x["psf"]) for x in txns
            if lo < x["t"] <= hi and x["sale_type"] in ("Resale", "Sub Sale")
            and not (sqft_band and x["sqft"] and not (sqft_band[0] <= x["sqft"] < sqft_band[1]))]
    vals.sort()
    return [p for _, p in vals]


def _win_median(txns, lo, hi, sqft_band=None):
    """Median PSF of resale txns with sale date in (lo, hi]."""
    vals = _win_vals(txns, lo, hi, sqft_band)
    return _median(vals), len(vals)


def build_panel(txns, split, window, min_txn=5, size_control=False, split_sample=False):
    """Per-project as-of-`split` features + realized forward return.

    Windows are 12-month, center-to-center spans give clean annualized rates:
      old    (T-3, T-2]      center T-2.5
      mid    (T-2, T-1]      center T-1.5
      recent (T-1, T]        center T-0.5   <- best estimate of "price at T"
      fwd    (T+W-1, T+W]    center T+W-0.5

      trailing_cagr = (recent/old)^(1/2) - 1
      momentum      = (recent/mid - 1) - trailing_cagr        # short minus long horizon
      forward_cagr  = (fwd/recent)^(1/W) - 1                   # the OUTCOME
    """
    by_proj = defaultdict(list)
    for x in txns:
        by_proj[(x["project"], x["district"])].append(x)

    # district recent-window median (for the value/psf-vs-district feature)
    district_psf = defaultdict(list)
    for x in txns:
        if split - 1 < x["t"] <= split and x["sale_type"] in ("Resale", "Sub Sale"):
            district_psf[x["district"]].append(x["psf"])
    district_med = {d: _median(v) for d, v in district_psf.items() if v}

    rows = []
    for (proj, dist), ts in by_proj.items():
        band = None
        if size_control:
            # dominant resale sqft band before T -> compare like-for-like sizes
            sq = [x["sqft"] for x in ts if x["sqft"] and x["t"] <= split
                  and x["sale_type"] in ("Resale", "Sub Sale")]
            if sq:
                m = float(np.median(sq))
                band = (m * 0.75, m * 1.25)

        old, n_old = _win_median(ts, split - 3, split - 2, band)
        mid, n_mid = _win_median(ts, split - 2, split - 1, band)
        fwd, n_fwd = _win_median(ts, split + window - 1, split + window, band)

        # "Price at T" from the recent window. In split-sample mode, estimate it
        # from two disjoint halves: recent_feat feeds the FEATURES, recent_out is
        # the OUTCOME base. This removes the shared-estimation-noise artifact that
        # spuriously pushes trailing_cagr / psf_vs_dist correlations negative.
        rec_vals = _win_vals(ts, split - 1, split, band)
        n_rec = len(rec_vals)
        if n_rec < min_txn or not fwd or n_fwd < min_txn:
            continue
        ss_fallback = 0
        if split_sample and n_rec >= 2 * min_txn:
            a, b = rec_vals[0::2], rec_vals[1::2]   # interleaved halves (balanced in time)
            recent_feat, recent_out = _median(a), _median(b)
        else:
            # NOTE: when split_sample was requested this is a silent fallback to
            # the shared price base (features and outcome share estimation
            # noise) — counted per split and printed (Jun-2026 audit).
            ss_fallback = 1 if split_sample else 0
            recent_feat = recent_out = _median(rec_vals)
        recent = recent_feat

        forward_cagr = (fwd / recent_out) ** (1.0 / window) - 1.0

        trailing_cagr = None
        if old and n_old >= min_txn:
            trailing_cagr = (recent / old) ** (1.0 / 2.0) - 1.0
        momentum = None
        if mid and n_mid >= min_txn and trailing_cagr is not None:
            momentum = (recent / mid - 1.0) - trailing_cagr

        # trailing 2yr resale volume (liquidity)
        vol = sum(1 for x in ts if split - 2 < x["t"] <= split
                  and x["sale_type"] in ("Resale", "Sub Sale"))
        # new-sale share in trailing 2yr (new-launch-bias / youth proxy)
        recent_all = [x for x in ts if split - 2 < x["t"] <= split]
        ns = sum(1 for x in recent_all if x["sale_type"] == "New Sale")
        ns_share = ns / len(recent_all) if recent_all else None

        dmed = district_med.get(dist)
        psf_vs_dist = (recent / dmed - 1.0) if dmed else None
        # freehold from the MODAL tenure across the project's txns — the old
        # ts[0] read keyed the flag to whichever transaction happened to load
        # first (file/row order), mislabeling mixed/dirty-tenure projects.
        n_fh = sum(1 for x in ts
                   if "freehold" in (x["tenure"] or "").lower()
                   or "999" in (x["tenure"] or ""))
        freehold = 1 if (n_fh > 0 and n_fh * 2 >= len(ts)) else 0

        rows.append({
            "project": proj, "district": dist,
            "split": split,
            "trailing_cagr": trailing_cagr,
            "momentum": momentum,
            "psf_level": recent,
            "psf_vs_dist": psf_vs_dist,
            "txn_vol": vol,
            "new_sale_share": ns_share,
            "freehold": freehold,
            "forward_cagr": forward_cagr,
            "n_recent": n_rec, "n_fwd": n_fwd,
            "ss_fallback": ss_fallback,
        })
    return rows


# ---- analysis helpers -------------------------------------------------------

def _midranks(a):
    """Average ranks for ties (proper Spearman midranks).

    The old argsort().argsort() assigned ROW-ORDER ranks within tie groups —
    correlated with input order (the district file order), a ~±0.02 rho
    artifact on binary/quantized features (freehold, txn_vol, district joins).
    """
    a = np.asarray(a, float)
    n = len(a)
    if n == 0:
        return a.astype(float)
    order = np.argsort(a, kind="mergesort")
    s = a[order]
    new_grp = np.empty(n, bool)
    new_grp[0] = True
    new_grp[1:] = s[1:] != s[:-1]
    grp = np.cumsum(new_grp) - 1
    counts = np.bincount(grp)
    ends = np.cumsum(counts)            # 1-past-the-end index of each tie group
    starts = ends - counts
    mid = 0.5 * (starts + ends - 1)     # average of the 0-based ranks in group
    ranks = np.empty(n, float)
    ranks[order] = mid[grp]
    return ranks


def _spearman(x, y):
    """Spearman rho via Pearson on midranks. Returns (rho, n)."""
    pairs = [(a, b) for a, b in zip(x, y) if a is not None and b is not None
             and not (isinstance(a, float) and math.isnan(a))]
    n = len(pairs)
    if n < 8:
        return None, n
    xs = np.array([p[0] for p in pairs], float)
    ys = np.array([p[1] for p in pairs], float)
    rx = _midranks(xs)
    ry = _midranks(ys)
    if rx.std() == 0 or ry.std() == 0:
        return None, n
    rho = float(np.corrcoef(rx, ry)[0, 1])
    return rho, n


def _cluster_boot_rho(x, y, clusters, reps=400, seed=42):
    """95% bootstrap CI for Spearman rho, resampling whole CLUSTERS (projects)
    with replacement — a block bootstrap over the project dimension: a
    project's rows from overlapping splits move together, so the CI does not
    pretend the ~87.5%-overlapping pooled rows are independent.

    Returns (lo, hi, n_clusters); (None, None, k) when too thin.
    """
    by_c = defaultdict(list)
    for a, b, c in zip(x, y, clusters):
        if a is None or b is None:
            continue
        if isinstance(a, float) and math.isnan(a):
            continue
        by_c[c].append((a, b))
    keys = sorted(by_c)
    k = len(keys)
    if k < 8:
        return None, None, k
    rng = np.random.default_rng(seed)
    rhos = []
    for _ in range(reps):
        idx = rng.integers(0, k, k)
        xs, ys = [], []
        for i in idx:
            for a, b in by_c[keys[i]]:
                xs.append(a)
                ys.append(b)
        r, _n = _spearman(xs, ys)
        if r is not None:
            rhos.append(r)
    if len(rhos) < reps * 0.5:
        return None, None, k
    lo, hi = np.percentile(rhos, [2.5, 97.5])
    return float(lo), float(hi), k


def _fmt_ci(lo, hi):
    return f"CI[{lo:+.3f},{hi:+.3f}]" if lo is not None else "CI[ n/a ]"


def effective_n_report(pooled_rows, splits, window):
    """Honesty diagnostics for a pooled multi-split panel (Jun-2026 audit):
    unique projects, windows per project, forward-window overlap. Printed on
    every pooled run so 'n=1,460 rows' can never silently impersonate 1,460
    independent observations again."""
    projs = defaultdict(int)
    for r in pooled_rows:
        projs[(r["project"], r["district"])] += 1
    n_rows = len(pooled_rows)
    n_proj = len(projs)
    print("== Effective-n diagnostics (pooled panel) ==")
    if not n_proj:
        print("   no rows\n")
        return 0
    print(f"   pooled rows {n_rows:,} | unique projects {n_proj:,} | "
          f"mean windows/project {n_rows / n_proj:.2f}")
    if len(splits) > 1:
        in_all = sum(1 for v in projs.values() if v == len(splits))
        spacing = min(b - a for a, b in zip(splits, splits[1:]))
        overlap = max(0.0, 1.0 - spacing / window)
        print(f"   {in_all:,}/{n_proj:,} projects ({in_all / n_proj:.0%}) appear in all "
              f"{len(splits)} splits")
        print(f"   split spacing {spacing:.2f}yr vs forward window {window:.2f}yr "
              f"-> consecutive forward windows overlap {overlap:.0%}")
        if overlap > 0:
            print("   *** WARNING: OVERLAPPING forward windows — pooled rows are NOT")
            print(f"   *** independent. Effective n ~= {n_proj:,} projects, not "
                  f"{n_rows:,} rows; pooled rho/std_beta p-values that assume row")
            print("   *** independence are overstated. Use --split-spacing >= window "
                  "for a clean panel.")
        else:
            print("   non-overlapping clean mode: forward windows are disjoint across "
                  "splits.")
    print()
    return n_proj


def _window_coverage(lo, hi, data_start, data_end):
    """Fraction of the (lo, hi] window covered by the loaded data range."""
    return max(0.0, min(hi, data_end) - max(lo, data_start)) / (hi - lo)


def _quintiles(rows, feat, outcome="forward_cagr", q=5):
    vals = [(r[feat], r[outcome]) for r in rows
            if r.get(feat) is not None and r.get(outcome) is not None]
    if len(vals) < q * 3:
        return None
    vals.sort(key=lambda v: v[0])
    n = len(vals)
    out = []
    for i in range(q):
        chunk = vals[i * n // q:(i + 1) * n // q]
        fv = [c[0] for c in chunk]
        ov = [c[1] for c in chunk]
        out.append({
            "q": i + 1, "n": len(chunk),
            "feat_lo": min(fv), "feat_hi": max(fv),
            "fwd_mean": float(np.mean(ov)), "fwd_median": float(np.median(ov)),
        })
    return out


def _fmt_pct(x):
    return f"{x*100:+.1f}%" if x is not None else "  n/a"


def derive_splits(txns, spacing, window):
    """Generate splits walking back from the latest feasible split (full
    forward window) in steps of `spacing`, down to the earliest split whose
    recent (T-1, T] window is fully covered by data."""
    data_start = min(x["t"] for x in txns)
    data_end = max(x["t"] for x in txns)
    t_hi = data_end - window
    t_lo = data_start + 1.0
    splits = []
    t = t_hi
    while t >= t_lo - 1e-9:
        splits.append(round(t, 4))
        t -= spacing
    splits.sort()
    return splits


def run_report(splits, window, min_txn, size_control, split_sample=False, dump=None,
               split_spacing=None):
    txns = load_txns()
    print(f"Loaded {len(txns):,} URA txns "
          f"({sum(1 for t in txns if t['sale_type'] in ('Resale','Sub Sale')):,} resale).")
    data_start = min(x["t"] for x in txns)
    data_end = max(x["t"] for x in txns)
    if splits is None and split_spacing:
        splits = derive_splits(txns, split_spacing, window)
        mode = ("non-overlapping clean mode" if split_spacing >= window
                else f"OVERLAPPING (spacing {split_spacing} < window {window})")
        print(f"--split-spacing {split_spacing}: splits {splits}  [{mode}]")
    elif splits is None:
        splits = [2023.75, 2024.0, 2024.25]
    print(f"Window={window}yr forward, min_txn/window={min_txn}, "
          f"size_control={size_control}, split_sample={split_sample}\n")

    features = ["trailing_cagr", "momentum", "psf_vs_dist", "txn_vol",
                "new_sale_share", "freehold"]

    all_corr = defaultdict(list)
    last_rows = None
    pooled = []
    for split in splits:
        rows = build_panel(txns, split, window, min_txn, size_control, split_sample)
        last_rows = rows
        if len(rows) < 20:
            print(f"== T={split:.2f}: only {len(rows)} projects in-sample, skipping ==\n")
            continue
        pooled.extend(rows)
        fwd = [r["forward_cagr"] for r in rows]
        print(f"== Split T={split:.2f}  (forward {split:.2f}->{split+window:.2f}) "
              f"| {len(rows)} projects ==")
        # trailing-window coverage: data starts {data_start}; the early default
        # splits have a truncated (T-3, T-2] window -> trailing_cagr measured
        # over less history than its label claims (Jun-2026 audit).
        covs = {"old (T-3,T-2]": _window_coverage(split - 3, split - 2, data_start, data_end),
                "mid (T-2,T-1]": _window_coverage(split - 2, split - 1, data_start, data_end),
                "recent (T-1,T]": _window_coverage(split - 1, split, data_start, data_end)}
        cov_s = "  ".join(f"{k} {v:.0%}" for k, v in covs.items())
        print(f"   trailing-window data coverage: {cov_s}")
        low = [k for k, v in covs.items() if v < 0.75]
        if low:
            print(f"   *** FLAG: {', '.join(low)} <75% covered (data starts "
                  f"{data_start:.2f}) — trailing features at this split are "
                  f"computed on a truncated window")
        if split_sample:
            fb = sum(r.get("ss_fallback", 0) for r in rows)
            print(f"   split-sample fallback (n_recent < {2*min_txn} -> shared "
                  f"feature/outcome price base): {fb}/{len(rows)} rows "
                  f"({fb/len(rows):.0%})")
        print(f"   forward annual resale-PSF return: "
              f"mean {_fmt_pct(float(np.mean(fwd)))}  "
              f"median {_fmt_pct(float(np.median(fwd)))}  "
              f"sd {np.std(fwd)*100:.1f}pp")
        print(f"   {'feature':<16}{'spearman_rho':>14}{'n':>7}")
        for f in features:
            rho, n = _spearman([r[f] for r in rows], fwd)
            if rho is not None:
                all_corr[f].append(rho)
            print(f"   {f:<16}{(f'{rho:+.3f}' if rho is not None else 'n/a'):>14}{n:>7}")
        # headline quintile table: the algo's biggest bet
        for f in ("trailing_cagr", "momentum"):
            qt = _quintiles(rows, f)
            if qt:
                print(f"\n   {f} quintiles -> forward return:")
                for b in qt:
                    print(f"     Q{b['q']} n={b['n']:>3}  "
                          f"{f} [{_fmt_pct(b['feat_lo'])},{_fmt_pct(b['feat_hi'])}]"
                          f"   fwd mean {_fmt_pct(b['fwd_mean'])} "
                          f"median {_fmt_pct(b['fwd_median'])}")
        print()

    if len(splits) > 1 and pooled:
        effective_n_report(pooled, splits, window)

    if len(splits) > 1 and all_corr:
        print("== Stability across splits (mean Spearman rho; pooled rho with "
              "project-cluster bootstrap 95% CI) ==")
        fwd_p = [r["forward_cagr"] for r in pooled]
        cl_p = [(r["project"], r["district"]) for r in pooled]
        for f in features:
            cs = all_corr.get(f, [])
            if not cs:
                continue
            rho_p, _n_p = _spearman([r.get(f) for r in pooled], fwd_p)
            lo, hi, k = _cluster_boot_rho([r.get(f) for r in pooled], fwd_p, cl_p)
            pooled_s = (f"pooled {rho_p:+.3f} {_fmt_ci(lo, hi)} "
                        f"({k} projects)" if rho_p is not None else "pooled n/a")
            print(f"   {f:<16}{np.mean(cs):+.3f}   "
                  f"(per-split: {', '.join(f'{c:+.2f}' for c in cs)})  | {pooled_s}")
        print()

    if dump and last_rows:
        keys = list(last_rows[0].keys())
        with open(dump, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(last_rows)
        print(f"Dumped {len(last_rows)} rows -> {dump}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", type=float, action="append",
                    help="split year(s), e.g. 2023.99. Repeatable. Default: walk-forward set.")
    ap.add_argument("--window", type=float, default=2.0, help="forward window (years)")
    ap.add_argument("--min-txn", type=int, default=5, help="min resale txns per window")
    ap.add_argument("--size-control", action="store_true",
                    help="stratify PSF to project's dominant sqft band (composition-bias check)")
    ap.add_argument("--split-sample", action="store_true",
                    help="estimate price-at-T from disjoint txn halves (removes mean-reversion artifact)")
    ap.add_argument("--split-spacing", type=float, default=None,
                    help="generate splits this many years apart (walking back from the "
                         "latest feasible split). Spacing >= --window gives a clean "
                         "NON-overlapping panel; the default 0.25yr-spaced splits "
                         "overlap ~87.5%% and print a warning.")
    ap.add_argument("--dump", help="write last-split per-project rows to CSV")
    args = ap.parse_args()

    if args.split and args.split_spacing:
        print("--split given: ignoring --split-spacing")
        args.split_spacing = None
    run_report(args.split, args.window, args.min_txn, args.size_control,
               args.split_sample, args.dump, args.split_spacing)


if __name__ == "__main__":
    main()
