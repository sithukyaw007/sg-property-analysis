#!/usr/bin/env python3
"""Build data/primary_schools.json — MOE primary schools with coordinates.

Source: MOE "General information of schools" on data.gov.sg (Singapore Open
Data Licence), filtered to schools that take Primary 1 (PRIMARY and
MIXED LEVEL (P1-S4)), geocoded by postal code through OneMap search (no token).

Used by scoring/personal_fit.py to count primary schools within 1 km / 2 km
of a development. That count is a straight-line proxy: P1 registration
priority uses MOE's own home-school distance (OneMap SchoolQuery), so always
confirm a specific address there before relying on it.

    python build_primary_schools.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from utils import onemap  # noqa: E402

DATASET = "d_688b934f82c1059ed0a6993d2a829089"
API = "https://data.gov.sg/api/action/datastore_search"
OUT = os.path.join(BASE, "data", "primary_schools.json")
P1_LEVELS = {"PRIMARY", "MIXED LEVEL (P1-S4)"}


def fetch_schools() -> list[dict]:
    r = requests.get(API, params={"resource_id": DATASET, "limit": 1000}, timeout=30)
    r.raise_for_status()
    rows = r.json()["result"]["records"]
    return [x for x in rows if (x.get("mainlevel_code") or "").strip() in P1_LEVELS]


def main() -> int:
    schools = fetch_schools()
    out, missed = [], []
    for s in schools:
        postal = (s.get("postal_code") or "").strip().zfill(6)
        g = onemap.geocode(postal)
        if not g:
            missed.append(s["school_name"])
            continue
        out.append({
            "name": s["school_name"].strip().title(),
            "postal": postal,
            "address": (s.get("address") or "").strip(),
            "level": s.get("mainlevel_code"),
            "zone": s.get("zone_code"),
            "lat": round(g[0], 6),
            "lng": round(g[1], 6),
        })
    doc = {
        "source": "MOE General information of schools (data.gov.sg dataset "
                  f"{DATASET}), geocoded via OneMap search",
        "built": date.today().isoformat(),
        "levels": sorted(P1_LEVELS),
        "count": len(out),
        "not_geocoded": missed,
        "schools": sorted(out, key=lambda x: x["name"]),
    }
    with open(OUT, "w") as f:
        json.dump(doc, f, indent=1)
    print(f"wrote {len(out)} schools -> {OUT}" + (f" ({len(missed)} not geocoded: {missed})"
                                                    if missed else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
