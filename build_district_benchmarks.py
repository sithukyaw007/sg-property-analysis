"""Rebuild district-level price/rent benchmarks from MEASURED data.

Replaces the hand-set, 7-district `data/district_medians.json` (and the static
`median_psf` / `median_rental_psf` fields in `data/district_profiles.json`)
with values computed from:
  * sale PSF   — data/ura_district_D*.csv resale+subsale, trailing 12 months
  * rental PSF — data/ura_rental_D*.csv contracts (band-midpoint sqft),
                 trailing 12 months, overall and per bedroom count
  * avg_yield  — 12 x median rental PSF / median sale PSF

These benchmarks feed the rental-estimator fallback waterfall (priorities 2-3,
the 27% of listings without a real project-rent match), the quick scorer, and
the AI-facing district context — previously they covered only 7 districts with
folk values that real contracts showed to be up to ~30% off.

Usage:
  python build_district_benchmarks.py
"""

import csv
import glob
import json
import re
from datetime import date
from pathlib import Path
from statistics import median

DATA_DIR = Path(__file__).parent / "data"

_MON = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}

_CCR = {1, 2, 6, 7, 9, 10, 11}
_RCR = {3, 4, 5, 8, 12, 13, 14, 15}


def _region(d):
    return "CCR" if d in _CCR else ("RCR" if d in _RCR else "OCR")


def _parse_month(s):
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


def _open(p):
    fh = open(p, encoding="utf-8")
    try:
        fh.read(1 << 20)
        fh.seek(0)
        return fh
    except UnicodeDecodeError:
        fh.close()
        return open(p, encoding="windows-1252")


def sale_psf_by_district(window_years=1.0):
    rows = []
    # Condo/apartment files only — EC prints (_EC.csv) trade at their own
    # level and must not drag the condo district benchmark down.
    for p in sorted(glob.glob(str(DATA_DIR / "ura_district_D[0-9][0-9].csv"))):
        with _open(p) as fh:
            for r in csv.DictReader(fh):
                t = _parse_month(r.get("Sale Date", ""))
                psf = _num(r.get("Unit Price ($ PSF)"))
                if t is None or not psf:
                    continue
                if r.get("Type of Sale", "").strip() not in ("Resale", "Sub Sale"):
                    continue
                # URA sale CSVs zero-pad the district ("01") — strip to bare
                # digits so D1-D9 match the "1".."28" lookups in main() (the
                # rental CSVs are already lstripped below).
                rows.append((r.get("Postal District", "").strip().lstrip("0"), t, psf))
    t_max = max(t for _, t, _ in rows)
    out = {}
    for d in {d for d, _, _ in rows}:
        v = [psf for dd, t, psf in rows if dd == d and t > t_max - window_years]
        if len(v) >= 20:
            out[d] = round(median(v))
    return out


def rent_psf_by_district(window_years=1.0):
    rows = []
    for p in sorted(glob.glob(str(DATA_DIR / "ura_rental_D[0-9][0-9].csv"))):
        with _open(p) as fh:
            for r in csv.DictReader(fh):
                t = _parse_month(r.get("Lease Commencement Date"))
                rent = _num(r.get("Monthly Rent ($)"))
                band = (r.get("Floor Area (SQFT)") or "").replace(",", "")
                m = re.findall(r"\d+", band)
                sqft = (float(m[0]) + float(m[1])) / 2 if len(m) >= 2 else None
                if t is None or not rent or not sqft:
                    continue
                beds = (r.get("No of Bedroom") or "").strip()
                rows.append(((r.get("Postal District") or "").strip().lstrip("0"),
                             t, rent / sqft, beds if beds.isdigit() else None))
    t_max = max(t for _, t, _, _ in rows)
    overall, by_bed = {}, {}
    for d in {d for d, _, _, _ in rows}:
        recent = [(psf, b) for dd, t, psf, b in rows
                  if dd == d and t > t_max - window_years]
        if len(recent) >= 20:
            overall[d] = round(median(p for p, _ in recent), 2)
            beds_out = {}
            for bed in ("1", "2", "3", "4", "5"):
                v = [p for p, b in recent if b == bed]
                if len(v) >= 10:
                    beds_out[bed] = round(median(v), 2)
            if beds_out:
                by_bed[d] = beds_out
    return overall, by_bed


def main():
    sale = sale_psf_by_district()
    rent, rent_beds = rent_psf_by_district()
    print(f"sale medians: {len(sale)} districts; rent medians: {len(rent)} districts")

    medians, bedroom_rental, info = {}, {}, {}
    for d in range(1, 29):
        key = f"D{d:02d}"
        s = sale.get(str(d))
        rp = rent.get(str(d))
        entry = {}
        if s:
            entry["psf"] = s
        if rp:
            entry["rental_psf"] = rp
        if s and rp:
            entry["avg_yield"] = round(12 * rp / s * 100, 2)
        if entry:
            medians[key] = entry
        rb = rent_beds.get(str(d))
        if rb:
            bedroom_rental[key] = rb
        info[key] = {"region": _region(d)}

    out = {
        "last_updated": date.today().isoformat(),
        "source": ("measured: URA resale PSF (12mo) + URA rental contracts "
                   "(12mo, band-midpoint sqft) — build_district_benchmarks.py"),
        "medians": medians,
        "bedroom_rental_psf": bedroom_rental,
        "district_info": info,
    }
    (DATA_DIR / "district_medians.json").write_text(json.dumps(out, indent=2))
    print(f"wrote district_medians.json: {len(medians)} districts with medians, "
          f"{len(bedroom_rental)} with per-bed rents")

    # refresh district_profiles price/rent fields in place (labels untouched)
    prof_path = DATA_DIR / "district_profiles.json"
    profiles = json.loads(prof_path.read_text())
    changed = 0
    for dnum, prof in profiles.get("districts", {}).items():
        s = sale.get(dnum)
        rp = rent.get(dnum)
        if s:
            prof["median_psf"] = s
            changed += 1
        if rp:
            prof["median_rental_psf"] = rp
    profiles["last_updated"] = date.today().isoformat()
    prof_path.write_text(json.dumps(profiles, indent=2))
    print(f"refreshed district_profiles.json psf/rent fields for {changed} districts")


if __name__ == "__main__":
    main()
