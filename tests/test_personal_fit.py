"""scoring/personal_fit.py and utils/onemap.py — offline, OneMap mocked."""

from datetime import datetime

import pytest

import buyer_profile as bp
from scoring import personal_fit as pf
from utils import onemap

HOME = (1.3500, 103.9400)
SCHOOLS = [
    {"name": "Near Pri", "lat": 1.3530, "lng": 103.9400},    # ~330 m
    {"name": "Mid Pri", "lat": 1.3640, "lng": 103.9400},     # ~1.56 km
    {"name": "Far Pri", "lat": 1.4500, "lng": 103.9400},     # ~11 km
]


def _profile(postal="018989"):
    return bp.parse({
        "purpose": "own_stay", "buyer_type": "PR", "household_citizenship": "PR",
        "mandates": [{"label": "m", "max_price": 1_600_000, "beds": [3]}],
        "districts": [18],
        "anchors": {"workplace_postal": postal, "school_radii_m": [1000, 2000]},
    })


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(pf, "_schools", SCHOOLS)
    monkeypatch.setattr(pf, "project_coords", lambda n, d=None, l=None: (*HOME, "test"))
    monkeypatch.setattr(onemap, "geocode", lambda q: (1.2790, 103.8540, "WORK"))
    return monkeypatch


def test_haversine_sanity():
    assert onemap.haversine_m((1.35, 103.94), (1.36, 103.94)) == pytest.approx(1112, rel=0.01)


def test_schools_near_counts_by_radius(offline):
    s = pf.schools_near(*HOME, radii_m=(1000, 2000))
    assert s["within_1000m"] == ["Near Pri"]
    assert s["within_2000m"] == ["Near Pri", "Mid Pri"]
    assert s["nearest"]["name"] == "Near Pri"


def test_pt_commute_scored_and_cached(offline):
    calls = []
    offline.setattr(onemap, "available", lambda: (True, ""))
    offline.setattr(onemap, "route_pt_minutes",
                    lambda a, b, d, token=None: calls.append(1) or 38.0)
    cache = {}
    r = pf.personal_fit("X", "D18", profile=_profile(), cache=cache)
    assert r["commute"]["basis"] == "onemap_pt" and r["commute"]["minutes"] == 38.0
    assert r["components"] == {"schools": 12, "commute": 10}
    assert r["score"] == 72
    pf.personal_fit("X", "D18", profile=_profile(), cache=cache)
    assert len(calls) == 1                                  # second call served from cache


def test_straight_line_fallback_without_onemap(offline):
    offline.setattr(onemap, "available", lambda: (False, "no creds"))
    r = pf.personal_fit("X", "D18", profile=_profile(), cache={})
    assert r["commute"]["basis"] == "straight_line"
    assert r["commute"]["km"] == pytest.approx(12.4, abs=0.2)
    assert r["components"]["commute"] == 0                  # 10-15 km band
    assert any("straight-line distance" in c for c in r["caveats"])


def test_no_workplace_means_no_commute_component(offline):
    r = pf.personal_fit("X", "D18", profile=_profile(postal=None), cache={})
    assert "commute" not in r and "commute" not in r["components"]


def test_missing_coords_is_neutral(monkeypatch):
    monkeypatch.setattr(pf, "project_coords", lambda *a, **k: None)
    r = pf.personal_fit("Nowhere", "D18", profile=_profile(), cache={})
    assert r["score"] == 50 and r["components"] == {}


def test_no_school_in_2km_is_penalised(offline):
    offline.setattr(pf, "_schools", [SCHOOLS[2]])
    r = pf.personal_fit("X", "D18", profile=_profile(postal=None), cache={})
    assert r["components"] == {"schools": -8} and r["score"] == 42


def test_next_weekday_is_a_tuesday_morning():
    d = onemap.next_weekday("08:30", today=datetime(2026, 10, 3, 14, 0))   # a Saturday
    assert d.weekday() == 1 and (d.hour, d.minute) == (8, 30) and d.day == 6


def test_token_from_static_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ONEMAP_CONFIG", str(tmp_path))
    monkeypatch.setenv("ONEMAP_HOME", str(tmp_path))
    monkeypatch.delenv("ONEMAP_EMAIL", raising=False)
    monkeypatch.delenv("ONEMAP_PASSWORD", raising=False)
    monkeypatch.setenv("ONEMAP_TOKEN", "abc")             # not a JWT: expiry unknown
    assert onemap.get_token() == "abc"
    monkeypatch.delenv("ONEMAP_TOKEN")
    assert onemap.get_token() is None
    assert onemap.available()[0] is False


def test_token_refresh_via_email_password(monkeypatch, tmp_path):
    monkeypatch.setenv("ONEMAP_CONFIG", str(tmp_path))
    monkeypatch.setenv("ONEMAP_HOME", str(tmp_path))
    (tmp_path / "credentials").write_text("ONEMAP_EMAIL=a@b.c\nONEMAP_PASSWORD=pw\n")
    for k in ("ONEMAP_EMAIL", "ONEMAP_PASSWORD", "ONEMAP_TOKEN"):
        monkeypatch.delenv(k, raising=False)

    class R:
        status_code = 200

        @staticmethod
        def json():
            return {"access_token": "tok1", "expiry_timestamp": 9_999_999_999}

    posts = []
    monkeypatch.setattr(onemap.requests, "post", lambda *a, **k: posts.append(k) or R())
    assert onemap.get_token() == "tok1"
    assert onemap.get_token() == "tok1" and len(posts) == 1   # cached on disk
    assert posts[0]["json"] == {"email": "a@b.c", "password": "pw"}
