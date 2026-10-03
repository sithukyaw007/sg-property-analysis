"""Build data/rental_cache.json from URA rental-contract CSVs.

Aggregates data/ura_rental_D*.csv (the current slice fetched by
fetch_ura_rentals.py — per-contract project, district, bedrooms, monthly rent,
floor-area band, lease commencement month) into per-project medians:

  { "<project lower>|<district>": {
        project_name, district,
        rent_psf,            # median monthly $/sqft (band-midpoint sqft)
        contracts,           # contracts behind it
        window_months,       # recency window actually used (12 -> widened 24)
        by_beds: { "2": {"monthly_rent": ..., "rent_psf": ..., "contracts": n} },
        updated } }

Why: production rents were synthetic (district·bed constants), so the MMR
yield component was mechanically const/PSF — re-skinned cheapness, downweighted
to 0.5/0.15 confidence in v3.4. Real per-project rents replace the constant with
actual rental evidence. What the real rents then showed (backtest_ext PART 5g,
first read v3.5, re-measured with cluster CIs in v3.10): high gross yield does
not merely carry ~0 forward PRICE signal, it predicts *lower* forward price
growth (multivariate std_beta −0.16). So yield is carry/income only — exactly
what the component should price for a 5-7yr hold, and never appreciation.

Usage:
  python build_rental_cache.py            # uses data/ura_rental_D*.csv (no suffix)
"""

import csv
import glob
import json
import re
from collections import defaultdict
from datetime import date
from pathlib import Path
from statistics import median

DATA_DIR = Path(__file__).parent / "data"
OUT = DATA_DIR / "rental_cache.json"

_MON = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def _parse_month(s):
    """'Apr-26' -> float year 2026.29 (mid-month)."""
    try:
        mon, yr = (s or "").strip().split("-")
        return (2000 + int(yr)) + (_MON[mon] - 0.5) / 12.0
    except Exception:
        return None


def _num(s):
    try:
        return float(str(s).replace(",", "").strip())
    except Exception:
        return None


def load_contracts():
    rows = []
    # current-slice files only (no date-range suffix), plus the EC slice
    # (ura_rental_D{NN}_EC.csv). The cache is keyed per project, so EC
    # contracts only ever feed EC projects' rents.
    paths = sorted(glob.glob(str(DATA_DIR / "ura_rental_D[0-9][0-9].csv"))
                   + glob.glob(str(DATA_DIR / "ura_rental_D[0-9][0-9]_EC.csv")))
    for p in paths:
        try:
            fh = open(p, encoding="utf-8")
            fh.read(1 << 20)
            fh.seek(0)
        except UnicodeDecodeError:
            fh = open(p, encoding="windows-1252")
        with fh:
            for r in csv.DictReader(fh):
                t = _parse_month(r.get("Lease Commencement Date"))
                rent = _num(r.get("Monthly Rent ($)"))
                band = (r.get("Floor Area (SQFT)") or "").replace(",", "")
                m = re.findall(r"\d+", band)
                sqft = (float(m[0]) + float(m[1])) / 2 if len(m) >= 2 else None
                if t is None or not rent or not sqft:
                    continue
                beds_raw = (r.get("No of Bedroom") or "").strip()
                rows.append({
                    "project": (r.get("Project Name") or "").strip(),
                    "district": (r.get("Postal District") or "").strip().lstrip("0"),
                    "t": t,
                    "rent": rent,
                    "rent_psf": rent / sqft,
                    "beds": beds_raw if beds_raw.isdigit() else None,
                })
    return rows


def main():
    rows = load_contracts()
    if not rows:
        print("no data/ura_rental_D*.csv files — run fetch_ura_rentals.py first")
        return
    t_max = max(r["t"] for r in rows)
    print(f"{len(rows):,} rental contracts; latest lease month {t_max:.2f}")

    by_key = defaultdict(list)
    for r in rows:
        by_key[(r["project"].lower(), r["district"])].append(r)

    out = {}
    for (proj, dist), rs in by_key.items():
        window = 12
        recent = [r for r in rs if r["t"] > t_max - 1.0]
        if len(recent) < 5:
            window = 24
            recent = [r for r in rs if r["t"] > t_max - 2.0]
        if len(recent) < 3:
            continue
        by_beds = {}
        bed_groups = defaultdict(list)
        for r in recent:
            if r["beds"]:
                bed_groups[r["beds"]].append(r)
        for b, brs in bed_groups.items():
            if len(brs) >= 3:
                by_beds[b] = {
                    "monthly_rent": round(median(x["rent"] for x in brs)),
                    "rent_psf": round(median(x["rent_psf"] for x in brs), 2),
                    "contracts": len(brs),
                }
        out[f"{proj}|{dist}"] = {
            "project_name": rs[0]["project"],
            "district": f"D{int(dist):02d}" if dist.isdigit() else dist,
            "rent_psf": round(median(r["rent_psf"] for r in recent), 2),
            "contracts": len(recent),
            "window_months": window,
            "by_beds": by_beds,
        }

    OUT.write_text(json.dumps({
        "source": "URA PMI rental contracts (fetch_ura_rentals.py)",
        "built": date.today().isoformat(),
        "latest_lease_month": round(t_max, 2),
        "projects": out,
    }, indent=1))
    print(f"wrote {len(out):,} project rental medians -> {OUT}")


if __name__ == "__main__":
    main()
