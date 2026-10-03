"""Personal fit — how well a development suits THIS household's daily life.

Own-stay anchors the investment model cannot see: the commute to the buyer's
workplace and primary schools nearby. Configured in the buyer profile
(profile.local.json → anchors). A separate 0-100 axis, like livability:
never folded into MMR, never evidence of returns. 50 = neutral; missing data
is neutral, never penalised.

  * commute — fastest public-transport time to the workplace (OneMap routing,
    weekday departure from the profile, cached per project). Without OneMap
    credentials it falls back to straight-line distance and says so.
  * schools — primary schools (data/primary_schools.json) within the profile's
    radii (default 1 km / 2 km), straight-line from the development's centroid.
    P1 priority uses MOE's own home-school distance, so this is a screening
    proxy: confirm any specific address on OneMap's SchoolQuery.

Results are cached per (project, district, anchors) in data/personal/
(git-ignored): personal data never enters the tracked listings DB.

    python -m scoring.personal_fit "Treasure at Tampines" D18
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from statistics import median
from typing import Any, Optional

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(_BASE, "data")
PERSONAL_DIR = os.path.join(DATA_DIR, "personal")
SCHOOLS_FILE = os.path.join(DATA_DIR, "primary_schools.json")
CACHE_FILE = os.path.join(PERSONAL_DIR, "personal_fit_cache.json")

_lock = threading.Lock()
_schools: Optional[list] = None
_store_index: Optional[dict] = None


def _norm(name: str | None) -> str:
    s = (name or "").lower().strip()
    s = re.sub(r"[’'`]", "", s)
    s = re.sub(r"\s*@\s*", " at ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _dnum(district) -> str:
    digits = "".join(c for c in str(district or "") if c.isdigit())
    return str(int(digits)) if digits else ""


# --------------------------------------------------------------------------- #
# Coordinates
# --------------------------------------------------------------------------- #
def _sgprop_index() -> dict:
    """(normalized project, district) -> (lat, lng) median from sgprop's store."""
    global _store_index
    if _store_index is not None:
        return _store_index
    idx: dict = {}
    try:
        import sqlite3

        from sgprop import config as sgcfg
        path = os.path.join(str(sgcfg.data_dir()), "sgprop.db")
        if os.path.exists(path):
            con = sqlite3.connect(path)
            acc: dict = {}
            for proj, dist, lat, lon in con.execute(
                    "SELECT project, district, lat, lon FROM transactions "
                    "WHERE lat IS NOT NULL AND lon IS NOT NULL"):
                acc.setdefault((_norm(proj), str(dist)), []).append((lat, lon))
            con.close()
            idx = {k: (median(p[0] for p in v), median(p[1] for p in v)) for k, v in acc.items()}
    except Exception:
        idx = {}
    _store_index = idx
    return idx


def project_coords(name: str | None, district=None,
                   listing: dict | None = None) -> tuple[float, float, str] | None:
    """(lat, lng, source) for a development, best source first."""
    if listing and listing.get("latitude") and listing.get("longitude"):
        try:
            return float(listing["latitude"]), float(listing["longitude"]), "listing"
        except (TypeError, ValueError):
            pass
    try:
        from scoring.full_scorer import _project_units_lookup
        pu = _project_units_lookup(name)
        if pu and pu.get("lat") and pu.get("lng"):
            return float(pu["lat"]), float(pu["lng"]), "ura_project_units"
    except Exception:
        pass
    hit = _sgprop_index().get((_norm(name), _dnum(district)))
    if hit:
        return hit[0], hit[1], "ura_transactions"
    return None


# --------------------------------------------------------------------------- #
# Schools
# --------------------------------------------------------------------------- #
def load_schools(path: str = SCHOOLS_FILE) -> list:
    global _schools
    if _schools is None or path != SCHOOLS_FILE:
        try:
            with open(path) as f:
                schools = json.load(f).get("schools") or []
        except (OSError, json.JSONDecodeError):
            schools = []
        if path != SCHOOLS_FILE:
            return schools
        _schools = schools
    return _schools


def schools_near(lat: float, lng: float, radii_m=(1000, 2000), schools=None) -> dict:
    from utils.onemap import haversine_m
    schools = load_schools() if schools is None else schools
    dists = sorted(((haversine_m((lat, lng), (s["lat"], s["lng"])), s["name"]) for s in schools),
                   key=lambda x: x[0])
    out: dict[str, Any] = {"school_data": bool(schools)}
    for r in radii_m:
        out[f"within_{r}m"] = [n for d, n in dists if d <= r]
    if dists:
        out["nearest"] = {"name": dists[0][1], "distance_m": round(dists[0][0])}
    return out


# --------------------------------------------------------------------------- #
# Commute
# --------------------------------------------------------------------------- #
def _workplace(anchors, cache: dict) -> tuple[float, float] | None:
    key = f"workplace|{anchors.workplace_postal}"
    if key in cache:
        v = cache[key]
        return (v[0], v[1]) if v else None
    from utils import onemap
    try:
        g = onemap.geocode(anchors.workplace_postal)
    except Exception:
        return None  # transient: don't cache a failure
    cache[key] = [g[0], g[1]] if g else None
    return (g[0], g[1]) if g else None


def commute(origin: tuple[float, float], anchors, cache: dict, cache_key: str) -> dict:
    """{'minutes' | 'km', 'basis'} to the workplace, cached per project."""
    if not anchors or not anchors.has_workplace:
        return {"basis": None}
    if cache_key in cache and cache[cache_key].get("basis") == "onemap_pt":
        return cache[cache_key]
    work = _workplace(anchors, cache)
    if not work:
        return {"basis": None, "note": "workplace postal code could not be geocoded"}
    from utils import onemap
    km = round(onemap.haversine_m(origin, work) / 1000, 1)
    try:
        ok, _ = onemap.available()
        if ok:
            depart = onemap.next_weekday(anchors.commute_depart)
            mins = onemap.route_pt_minutes(origin, work, depart)
            if mins is not None:
                res = {"basis": "onemap_pt", "minutes": mins, "km": km,
                       "depart": depart.strftime("%a %H:%M")}
                cache[cache_key] = res
                return res
    except Exception as e:
        return {"basis": "straight_line", "km": km, "note": f"OneMap routing failed: {e}"}
    return {"basis": "straight_line", "km": km,
            "note": "approximate — add OneMap credentials for public-transport time"}


# --------------------------------------------------------------------------- #
# Score
# --------------------------------------------------------------------------- #
def _commute_points(c: dict) -> int | None:
    if c.get("basis") == "onemap_pt":
        m = c["minutes"]
        return 20 if m <= 30 else 10 if m <= 45 else 0 if m <= 60 else -10 if m <= 75 else -20
    if c.get("basis") == "straight_line":
        k = c["km"]   # weaker evidence -> smaller swings
        return 12 if k <= 5 else 5 if k <= 10 else 0 if k <= 15 else -8 if k <= 20 else -15
    return None


def _school_points(s: dict, radii) -> int | None:
    if not s.get("school_data"):
        return None
    near, far = (s.get(f"within_{radii[0]}m") or []), (s.get(f"within_{radii[-1]}m") or [])
    if len(near) >= 2:
        return 15
    if near:
        return 12
    if far:
        return 4
    return -8


def personal_fit(name: str | None, district=None, listing: dict | None = None,
                 profile=None, cache: dict | None = None) -> dict:
    """Personal-fit block for one development. Pure given its cache."""
    if profile is None:
        import buyer_profile
        profile = buyer_profile.active()
    anchors = profile.anchors if profile else None
    radii = tuple(anchors.school_radii_m) if anchors else (1000, 2000)
    cache = {} if cache is None else cache

    coords = project_coords(name, district, listing)
    out: dict[str, Any] = {"project": name, "district": district,
                           "coords_source": coords[2] if coords else None}
    comps: dict[str, int] = {}
    if coords:
        s = schools_near(coords[0], coords[1], radii)
        out["schools"] = {
            f"within_{r}m": len(s.get(f"within_{r}m") or []) for r in radii}
        out["schools"]["names_within_" + str(radii[0]) + "m"] = s.get(f"within_{radii[0]}m") or []
        if s.get("nearest"):
            out["schools"]["nearest"] = s["nearest"]
        pts = _school_points(s, radii)
        if pts is not None:
            comps["schools"] = pts
        if anchors and anchors.has_workplace:
            key = f"{_norm(name)}|{_dnum(district)}|{anchors.workplace_postal}|{anchors.commute_depart}"
            c = commute((coords[0], coords[1]), anchors, cache, key)
            out["commute"] = c
            pts = _commute_points(c)
            if pts is not None:
                comps["commute"] = pts
    out["components"] = comps
    out["score"] = max(0, min(100, 50 + sum(comps.values())))
    out["caveats"] = ["school counts are straight-line from the development centroid; "
                      "confirm P1 distance on OneMap SchoolQuery"]
    if out.get("commute", {}).get("basis") == "straight_line":
        out["caveats"].append("commute is straight-line distance, not travel time")
    return out


def load_cache() -> dict:
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(cache: dict) -> None:
    os.makedirs(PERSONAL_DIR, exist_ok=True)
    with _lock:
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f, indent=1, sort_keys=True)
        os.replace(tmp, CACHE_FILE)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.strip().splitlines()[-1])
        return 1
    cache = load_cache()
    res = personal_fit(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None, cache=cache)
    save_cache(cache)
    print(json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, _BASE)
    sys.exit(main())
