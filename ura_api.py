#!/usr/bin/env python3
"""URA data through the official API (via sgprop), no browser.

Replaces the headed-browser downloads in fetch_ura_districts.py and
fetch_ura_rentals.py. sgprop (github.com/ong6/sgprop) syncs every private
residential sale (5 yrs) and rental contract into its own store in ~40 s, then
writes the SAME per-district CSVs the browser produced, so every parser here
(parse_ura_csv, build_rental_cache, bed_bands) reads them unchanged.

Verified 2026-09-24: API transactions matched the eservice CSVs row for row
(D19 9,724/9,724, D20 2,669/2,669). The eservice rental CSV was silently
capped at 10,000 rows per district (D19 has 18,004 in the same window), so
rental medians and bed bands were built on a truncated sample until this.

The key never lives in this (public) repo: sgprop reads URA_ACCESS_KEY from
the environment or ~/.config/sgprop/credentials.

    python ura_api.py                 # sync + export (condo + EC) + rebuild all caches
    python ura_api.py --no-ec         # condo/apartment files only
    python ura_api.py --ec-only       # EC files only (forks: condo CSVs come from upstream)
    python ura_api.py --check         # is the API path usable here?
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).parent
DATA_DIR = BASE / "data"
ALL_DISTRICTS = list(range(1, 29))
# Three years of rental contracts, the same span the browser fetch asked for.
# Medians still move versus the browser era wherever the eservice download hit
# its 10,000-row cap (D19: 10,000 -> 18,004 rows), because the old sample was
# truncated, not because the window changed.
RENTAL_QUARTERS = 12

# An export that comes back much smaller than the file it replaces is treated
# as a bad sync, not as the market shrinking: the district keeps its old file.
MIN_KEEP_RATIO = 0.9

# URA's rental API labels EC contracts with their own property type; sgprop's
# default rental export is "Non-landed Properties" only.
EC_RENTAL_TYPES = ("Executive Condominium",)


def _rows(path: Path) -> int:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return max(0, sum(1 for _ in f) - 1)
    except OSError:
        return 0


def _export_guarded(write, store, district: int, path: Path, skipped: list,
                    accept_shrink: bool = False) -> int:
    """Export one district to a temp file; swap it in only if it looks whole.

    Never truncates a good CSV in place: a failed or empty export leaves the
    old file (and its mtime, so staleness checks still see it as old).
    """
    tmp = path.with_suffix(".csv.tmp")
    try:
        n = write(store, district, tmp)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    old = _rows(path)
    if n == 0 and old == 0:
        tmp.unlink(missing_ok=True)       # a district with no data, before and after
        return 0
    # A rolling window CAN legitimately shrink a district by >10%; after
    # checking, `python ura_api.py --accept-shrink` takes the smaller export.
    if n == 0 or (old and n < MIN_KEEP_RATIO * old and not accept_shrink):
        tmp.unlink(missing_ok=True)
        skipped.append(f"{path.name}: {n} rows vs {old} before — kept the old file")
        return old
    os.replace(tmp, path)
    return n


def available() -> tuple[bool, str]:
    """(usable, why-not). Usable = sgprop importable AND a URA key configured."""
    try:
        from sgprop import config
    except ImportError:
        return False, "sgprop not installed (pip install -r requirements.txt)"
    try:
        config.credential("URA_ACCESS_KEY")
    except config.MissingCredential as e:
        return False, str(e)
    return True, ""


def refresh(transactions: bool = True, rentals: bool = True, force: bool = False,
            rebuild: bool = True, accept_shrink: bool = False, ec: bool = True) -> dict:
    """Sync from URA, export district CSVs, and rebuild the derived caches.

    Sync is a no-op when sgprop's store is already newer than URA's last
    publish (Tue/Fri for sales, the 15th for rentals), but the CSV export
    always runs: it is ~2 s and keeps data/ in step with the store.

    `ec` (default on) also exports Executive Condominium sales to
    ura_district_D{NN}_EC.csv and EC rentals to ura_rental_D{NN}_EC.csv. ECs
    are a separate URA product that trades at its own PSF level, so they live
    in their own files: the condo files (and every backtest/validation panel
    built from them) stay byte-identical, while EC listings finally get EC
    comps instead of condo medians. Districts with no EC activity get no file.
    """
    from functools import partial

    from sgprop import export
    from sgprop.store import Store
    from sgprop.sync import sync_rentals, sync_transactions
    from sgprop.ura import UraClient

    store, client = Store(), UraClient()
    out: dict = {}
    skipped: list[str] = []
    if transactions:
        out["transactions"] = sync_transactions(store, client, force=force)
        out["tx_rows"] = sum(
            _export_guarded(export.transactions_csv, store, d,
                            DATA_DIR / f"ura_district_D{d:02d}.csv", skipped, accept_shrink)
            for d in ALL_DISTRICTS)
        if ec:
            ec_tx = partial(export.transactions_csv, property_types=export.EC_TYPES)
            out["ec_tx_rows"] = sum(
                _export_guarded(ec_tx, store, d,
                                DATA_DIR / f"ura_district_D{d:02d}_EC.csv", skipped, accept_shrink)
                for d in ALL_DISTRICTS)
        if rebuild:
            from fetch_ura_districts import build_cache_from_district_csvs
            out["ura_cache_projects"] = len(build_cache_from_district_csvs(None))
    if rentals:
        out["rentals"] = sync_rentals(store, client, quarters=RENTAL_QUARTERS, force=force)
        out["rent_rows"] = sum(
            _export_guarded(export.rentals_csv, store, d,
                            DATA_DIR / f"ura_rental_D{d:02d}.csv", skipped, accept_shrink)
            for d in ALL_DISTRICTS)
        if ec:
            ec_rent = partial(export.rentals_csv, property_types=EC_RENTAL_TYPES)
            out["ec_rent_rows"] = sum(
                _export_guarded(ec_rent, store, d,
                                DATA_DIR / f"ura_rental_D{d:02d}_EC.csv", skipped, accept_shrink)
                for d in ALL_DISTRICTS)
        if rebuild:
            for script in (["build_rental_cache.py"], ["bed_bands.py", "--build"]):
                subprocess.run([sys.executable, str(BASE / script[0]), *script[1:]],
                               cwd=BASE, check=True, stdout=subprocess.DEVNULL)
            out["rebuilt"] = ["rental_cache.json", "bed_bands.json"]
    if skipped:
        out["skipped_districts"] = skipped
        print("  ⚠ " + "\n  ⚠ ".join(skipped)
              + "\n  (if the shrink is real, re-run `python ura_api.py --accept-shrink`)",
              file=sys.stderr)
    return out


def refresh_ec_only(force: bool = False, accept_shrink: bool = False) -> dict:
    """Export ONLY the EC files and fold them into the derived caches.

    For forks that take condo/apartment CSVs from upstream (which refreshes them
    weekly): re-exporting those here would rewrite tracked files for no gain and
    conflict on every merge. This syncs sgprop's store (a no-op when current),
    rewrites ura_*_D{NN}_EC.csv, adds EC projects to ura_cache.json without
    touching condo entries, and rebuilds rental_cache.json / bed_bands.json
    (both per project, so condo values are unchanged).
    """
    from functools import partial

    from sgprop import export
    from sgprop.store import Store
    from sgprop.sync import sync_rentals, sync_transactions
    from sgprop.ura import UraClient

    store, client = Store(), UraClient()
    skipped: list[str] = []
    out: dict = {
        "transactions": sync_transactions(store, client, force=force),
        "rentals": sync_rentals(store, client, quarters=RENTAL_QUARTERS, force=force),
    }
    ec_tx = partial(export.transactions_csv, property_types=export.EC_TYPES)
    ec_rent = partial(export.rentals_csv, property_types=EC_RENTAL_TYPES)
    out["ec_tx_rows"] = sum(
        _export_guarded(ec_tx, store, d, DATA_DIR / f"ura_district_D{d:02d}_EC.csv",
                        skipped, accept_shrink) for d in ALL_DISTRICTS)
    out["ec_rent_rows"] = sum(
        _export_guarded(ec_rent, store, d, DATA_DIR / f"ura_rental_D{d:02d}_EC.csv",
                        skipped, accept_shrink) for d in ALL_DISTRICTS)
    import glob as _glob

    from invest import build_ura_cache_from_csv
    ec_files = sorted(_glob.glob(str(DATA_DIR / "ura_district_D[0-9][0-9]_EC.csv")))
    if ec_files:
        cache = build_ura_cache_from_csv(ec_files)
        out["ec_cache_projects"] = sum(1 for v in cache.values()
                                       if isinstance(v, dict) and v.get("is_ec"))
    for script in (["build_rental_cache.py"], ["bed_bands.py", "--build"]):
        subprocess.run([sys.executable, str(BASE / script[0]), *script[1:]],
                       cwd=BASE, check=True, stdout=subprocess.DEVNULL)
    out["rebuilt"] = ["ura_cache.json (EC entries)", "rental_cache.json", "bed_bands.json"]
    if skipped:
        out["skipped_districts"] = skipped
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--force", action="store_true", help="ignore URA's publish schedule")
    ap.add_argument("--no-rentals", action="store_true")
    ap.add_argument("--no-transactions", action="store_true")
    ap.add_argument("--accept-shrink", action="store_true",
                    help="write exports even if a district shrank >10%% (after checking)")
    ap.add_argument("--no-ec", action="store_true",
                    help="skip the Executive Condominium sale/rental exports")
    ap.add_argument("--ec-only", action="store_true",
                    help="export only the EC files and fold them into the caches "
                         "(forks that take condo CSVs from upstream)")
    a = ap.parse_args()
    ok, why = available()
    if a.check or not ok:
        print("URA API: ready" if ok else f"URA API unavailable: {why}")
        return 0 if ok else 1
    if a.ec_only:
        print(json.dumps(refresh_ec_only(force=a.force, accept_shrink=a.accept_shrink), indent=2))
        return 0
    print(json.dumps(refresh(transactions=not a.no_transactions,
                             rentals=not a.no_rentals, force=a.force,
                             accept_shrink=a.accept_shrink, ec=not a.no_ec), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
