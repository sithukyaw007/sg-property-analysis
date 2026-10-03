#!/usr/bin/env python3
"""Import listings you collected yourself (CSV or JSON) into the listings DB.

The terms-safe intake path for this fork. PropertyGuru prohibits bulk
scraping, so instead of polling the portal you paste or export the listings
you are actually looking at, from any source, into a file and import it.
Each row is upserted (price history kept, same-unit relists linked), scored
with the same pipeline as --score-db, and then shows up in my_shortlist.py,
the UI and every analyze flow.

Accepted columns (sgprop's Listing fields, or this repo's DB names):

  required  project | project_name, price, sqft
  optional  listing_id | id, source, bedrooms | beds, baths, district (19 / D19),
            property_type (condo | ec | "Executive Condominium"), tenure,
            built_year | top_year, floor_level, url, listed_on | listing_date,
            latitude, longitude

    python import_listings.py my_listings.csv
    python import_listings.py viewings.json --source viewing
    python import_listings.py my_listings.csv --dry-run

One-off alternatives without a file:
  python invest.py --url "<PropertyGuru listing URL>"   # one listing, headed browser
  python invest.py --score '{"price":...,"sqft":...,...}' # score inputs, nothing stored
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

ALIASES = {
    "project": "project_name", "bedrooms": "beds", "listed_on": "listing_date",
    "listing_id": "listing_id", "id": "listing_id", "top_year": "built_year",
}
TYPE_MAP = {
    "ec": "Executive Condominium", "executive condominium": "Executive Condominium",
    "condo": "Condominium", "condominium": "Condominium", "apartment": "Apartment",
}


class ListingImportError(ValueError):
    pass


def _num(v):
    if v in (None, ""):
        return None
    return float(str(v).replace(",", "").replace("$", "").strip())


def read_rows(path: str) -> list[dict]:
    if path.lower().endswith(".json"):
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = data.get("listings") or [data]
        return [dict(r) for r in data]
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [dict(r) for r in csv.DictReader(f)]


def to_listing(raw: dict, source: str) -> dict:
    """Normalize one input row into the listings-DB shape. Raises on bad rows."""
    r = {}
    for k, v in raw.items():
        if v in (None, ""):
            continue
        k = (k or "").strip().lower()
        r[ALIASES.get(k, k)] = v.strip() if isinstance(v, str) else v
    name = r.get("project_name")
    price, sqft = _num(r.get("price")), _num(r.get("sqft"))
    if not name or not price or not sqft:
        raise ListingImportError("needs project, price and sqft")
    src = str(r.get("source") or source)
    lid = str(r.get("listing_id") or "").strip()
    if not lid:
        lid = hashlib.sha1(f"{name}|{price}|{sqft}|{r.get('floor_level', '')}".encode()).hexdigest()[:10]
    out = {
        "id": lid if lid.isdigit() and src.lower() in ("propertyguru", "pg") else f"{src}:{lid}",
        "title": name,
        "project_name": name,
        "price": int(price),
        "sqft": sqft,
        "psf": round(price / sqft, 2),
    }
    beds = _num(r.get("beds"))
    if beds is not None:
        out["beds"] = int(beds)
    if _num(r.get("baths")) is not None:
        out["baths"] = int(_num(r["baths"]))
    if r.get("district"):
        d = "".join(c for c in str(r["district"]) if c.isdigit())
        if not d or not 1 <= int(d) <= 28:
            raise ListingImportError(f"bad district {r['district']!r}")
        out["district"] = f"D{int(d):02d}"
    ptype = str(r.get("property_type") or "condo").strip().lower()
    if ptype not in TYPE_MAP:
        raise ListingImportError(f"property_type must be condo or ec, got {r['property_type']!r}")
    out["property_type"] = TYPE_MAP[ptype]
    for k in ("tenure", "floor_level", "url", "listing_date"):
        if r.get(k):
            out[k] = str(r[k])
    if _num(r.get("built_year")):
        out["built_year"] = int(_num(r["built_year"]))
    for k in ("latitude", "longitude"):
        if _num(r.get(k)) is not None:
            out[k] = _num(r[k])
    return out


def run(path: str, source: str = "manual", dry_run: bool = False) -> dict:
    rows = read_rows(path)
    listings, errors = [], []
    for i, raw in enumerate(rows, 1):
        try:
            listings.append(to_listing(raw, source))
        except (ListingImportError, ValueError) as e:
            errors.append(f"row {i}: {e}")
    report = {"rows": len(rows), "valid": len(listings), "errors": errors}
    if dry_run or not listings:
        report["listings"] = listings
        return report
    import listings_db
    import poller
    stats = listings_db.upsert_listings(listings, source={"flow": "import", "file": os.path.basename(path)})
    n, _ = poller._score_keys([l["id"] for l in listings])
    report.update(upsert={k: stats.get(k) for k in ("added", "updated", "price_changes", "skipped")},
                  scored=n, ids=[l["id"] for l in listings])
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("file")
    ap.add_argument("--source", default="manual", help="provenance tag (default: manual)")
    ap.add_argument("--dry-run", action="store_true", help="validate and print, write nothing")
    a = ap.parse_args()
    rep = run(a.file, a.source, a.dry_run)
    print(json.dumps(rep, indent=1, default=str))
    return 1 if rep["errors"] and not rep["valid"] else 0


if __name__ == "__main__":
    sys.exit(main())
