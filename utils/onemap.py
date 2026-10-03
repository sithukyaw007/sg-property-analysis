"""OneMap (Singapore Land Authority) client — geocoding and public-transport routing.

Used for the own-stay "personal fit" lens: commute time from a development to
the buyer's workplace, and geocoding school / workplace postal codes.

Terms: OneMap APIs are free under the Singapore Open Data Licence and API
Terms of Use. Routing needs a token (valid 3 days, 300 calls/min); search does
not. Every routing answer is cached on disk per (project, destination, time),
so a normal run calls the API once per new development, not per listing.

Credentials never live in this repo. Lookup order (same pattern as sgprop):
  1. env ONEMAP_EMAIL / ONEMAP_PASSWORD (auto-refreshing token), or ONEMAP_TOKEN
  2. ~/.config/onemap/credentials — KEY=value lines, chmod 600
     ONEMAP_EMAIL=you@example.com
     ONEMAP_PASSWORD=...
     # or, without auto-refresh: ONEMAP_TOKEN=eyJ...

    python -m utils.onemap --check               # token usable?
    python -m utils.onemap --geocode 018989      # postal -> lat/lng
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

BASE_URL = "https://www.onemap.gov.sg"
SEARCH_URL = BASE_URL + "/api/common/elastic/search"
TOKEN_URL = BASE_URL + "/api/auth/post/getToken"
ROUTE_URL = BASE_URL + "/api/public/routingsvc/route"

MIN_INTERVAL_S = 60.0 / 120          # well under the 300 calls/min limit (search 429s sooner)
TOKEN_REFRESH_MARGIN_S = 3600        # refresh when < 1h of validity is left
TIMEOUT_S = 20

_lock = threading.Lock()
_last_call = 0.0


class OneMapError(RuntimeError):
    pass


def config_dir() -> Path:
    return Path(os.environ.get("ONEMAP_CONFIG", "~/.config/onemap")).expanduser()


def cache_dir() -> Path:
    d = Path(os.environ.get("ONEMAP_HOME", "~/.cache/onemap")).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _credentials() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in (config_dir() / "credentials").read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    for k in ("ONEMAP_EMAIL", "ONEMAP_PASSWORD", "ONEMAP_TOKEN"):
        if os.environ.get(k):
            out[k] = os.environ[k]
    return out


def _throttle() -> None:
    global _last_call
    with _lock:
        wait = MIN_INTERVAL_S - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def _get(url: str, params: dict, headers: dict | None = None, attempts: int = 5) -> dict:
    for i in range(attempts):
        _throttle()
        r = requests.get(url, params=params, headers=headers or {}, timeout=TIMEOUT_S)
        if r.status_code == 401:
            raise OneMapError("OneMap: unauthorized (token missing or expired)")
        if r.status_code == 429 and i < attempts - 1:
            time.sleep(min(60.0, 5.0 * 2 ** i))   # back off; the limit is per minute
            continue
        r.raise_for_status()
        return r.json()
    raise OneMapError("OneMap: rate-limited after retries")


# --------------------------------------------------------------------------- #
# Token
# --------------------------------------------------------------------------- #
def _token_file() -> Path:
    return cache_dir() / "token.json"


def _jwt_expiry(token: str) -> float | None:
    """Expiry (epoch s) from a JWT payload, without verifying it."""
    import base64
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload)).get("exp"))
    except Exception:
        return None


def get_token(refresh: bool = False) -> str | None:
    """A usable routing token, or None when no credentials are configured."""
    creds = _credentials()
    tf = _token_file()
    if not refresh and tf.exists():
        try:
            cached = json.loads(tf.read_text())
            if cached.get("expiry", 0) - time.time() > TOKEN_REFRESH_MARGIN_S:
                return cached["token"]
        except (OSError, ValueError, KeyError):
            pass
    if creds.get("ONEMAP_EMAIL") and creds.get("ONEMAP_PASSWORD"):
        _throttle()
        r = requests.post(TOKEN_URL, json={"email": creds["ONEMAP_EMAIL"],
                                           "password": creds["ONEMAP_PASSWORD"]},
                          timeout=TIMEOUT_S)
        if r.status_code >= 400:
            raise OneMapError(f"OneMap token request failed: HTTP {r.status_code}")
        data = r.json()
        token = data.get("access_token")
        if not token:
            raise OneMapError("OneMap token response had no access_token")
        expiry = float(data.get("expiry_timestamp") or _jwt_expiry(token) or time.time() + 3 * 86400)
        tf.write_text(json.dumps({"token": token, "expiry": expiry}))
        os.chmod(tf, 0o600)
        return token
    token = creds.get("ONEMAP_TOKEN")
    if token:
        exp = _jwt_expiry(token)
        if exp is not None and exp < time.time():
            raise OneMapError("ONEMAP_TOKEN has expired — renew it on onemap.gov.sg, "
                              "or set ONEMAP_EMAIL/ONEMAP_PASSWORD for auto-refresh")
        return token
    return None


def available() -> tuple[bool, str]:
    try:
        tok = get_token()
    except (OneMapError, requests.RequestException) as e:
        return False, str(e)
    if not tok:
        return False, (f"no OneMap credentials — add ONEMAP_EMAIL/ONEMAP_PASSWORD to "
                       f"{config_dir() / 'credentials'} (chmod 600)")
    return True, ""


# --------------------------------------------------------------------------- #
# Geocoding (no token)
# --------------------------------------------------------------------------- #
def geocode(query: str) -> tuple[float, float, str] | None:
    """Postal code / address -> (lat, lng, matched address), or None."""
    data = _get(SEARCH_URL, {"searchVal": query, "returnGeom": "Y",
                             "getAddrDetails": "Y", "pageNum": 1})
    for r in data.get("results") or []:
        try:
            return float(r["LATITUDE"]), float(r["LONGITUDE"]), r.get("ADDRESS") or ""
        except (KeyError, TypeError, ValueError):
            continue
    return None


# --------------------------------------------------------------------------- #
# Public-transport routing (token)
# --------------------------------------------------------------------------- #
def next_weekday(depart_hhmm: str, today: datetime | None = None) -> datetime:
    """The next Tuesday at HH:MM — a representative weekday rush hour."""
    today = today or datetime.now()
    h, m = (int(x) for x in depart_hhmm.split(":"))
    d = today + timedelta(days=(1 - today.weekday()) % 7 or 7)
    return d.replace(hour=h, minute=m, second=0, microsecond=0)


def route_pt_minutes(start: tuple[float, float], end: tuple[float, float],
                     depart: datetime, token: str | None = None) -> float | None:
    """Fastest public-transport door-to-door time in minutes, or None if no route."""
    token = token or get_token()
    if not token:
        raise OneMapError("no OneMap token")
    data = _get(ROUTE_URL, {
        "start": f"{start[0]:.6f},{start[1]:.6f}",
        "end": f"{end[0]:.6f},{end[1]:.6f}",
        "routeType": "pt",
        "date": depart.strftime("%m-%d-%Y"),
        "time": depart.strftime("%H:%M:%S"),
        "mode": "TRANSIT",
        "maxWalkDistance": 1000,
        "numItineraries": 3,
    }, headers={"Authorization": token})
    its = ((data.get("plan") or {}).get("itineraries")) or []
    secs = [it.get("duration") for it in its if isinstance(it.get("duration"), (int, float))]
    return round(min(secs) / 60.0, 1) if secs else None


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--geocode")
    a = ap.parse_args()
    if a.geocode:
        print(geocode(a.geocode))
        return 0
    ok, why = available()
    print("OneMap routing: ready" if ok else f"OneMap routing unavailable: {why}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
