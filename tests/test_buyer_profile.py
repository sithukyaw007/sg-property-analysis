"""buyer_profile — parsing, validation, activation and the owner-default fallback."""

import json

import pytest

import buyer_profile as bp


def _base(**over):
    d = {
        "name": "t",
        "purpose": "own_stay",
        "buyer_type": "PR",
        "property_count": 0,
        "household_citizenship": "PR",
        "mandates": [{"label": "mine", "max_price": 1_600_000, "beds": [3],
                      "property_types": ["condo", "ec"]}],
        "districts": [16, 19, 28],
        "anchors": {"workplace_postal": "018989", "school_radii_m": [1000, 2000]},
    }
    d.update(over)
    return d


@pytest.fixture
def use_profile(tmp_path, monkeypatch):
    """Activate a profile file for one test, then restore the disabled default."""
    def _activate(data):
        p = tmp_path / "profile.json"
        p.write_text(json.dumps(data))
        monkeypatch.setenv(bp.PROFILE_ENV, str(p))
        bp.reset_cache()
        return bp.active()
    yield _activate
    bp.reset_cache()


def test_parse_valid_profile():
    p = bp.parse(_base())
    assert p.buyer_type == "PR" and p.household_citizenship == "PR"
    assert p.mandates[0].beds == (3,) and p.mandates[0].property_types == ("condo", "ec")
    assert p.districts == (16, 19, 28)
    assert p.anchors.workplace_postal == "018989" and p.anchors.has_workplace
    assert p.wants_ec


@pytest.mark.parametrize("over,msg", [
    ({"purpose": "flip"}, "purpose"),
    ({"buyer_type": "Alien"}, "buyer_type"),
    ({"property_count": -1}, "property_count"),
    ({"household_citizenship": "XX"}, "household_citizenship"),
    ({"mandates": []}, "mandates"),
    ({"mandates": [{"label": "a", "max_price": 1, "beds": [3], "property_types": ["landed"]}]},
     "property_types"),
    ({"mandates": [{"label": "a", "max_price": "lots", "beds": [3]}]}, "max_price"),
    ({"mandates": [{"label": "a", "max_price": 1, "beds": [3]},
                   {"label": "a", "max_price": 2, "beds": [3]}]}, "unique"),
    ({"districts": [0]}, "districts"),
    ({"districts": []}, "districts"),
    ({"anchors": {"workplace_postal": "12345"}}, "postal"),
])
def test_parse_rejects_bad_input(over, msg):
    with pytest.raises(bp.ProfileError, match=msg):
        bp.parse(_base(**over))


def test_mandate_for_respects_price_beds_region_and_type():
    p = bp.parse(_base())
    assert p.mandate_for(1_500_000, 3, "D19") == "mine"
    assert p.mandate_for(1_600_000, 3, "D19") == "mine"          # at budget
    assert p.mandate_for(1_600_001, 3, "D19") is None            # over budget
    assert p.mandate_for(1_500_000, 2, "D19") is None            # wrong beds
    assert p.mandate_for(1_500_000, 3, "D15") is None            # out of region
    assert p.mandate_for(1_500_000, 3, "D19", "Executive Condominium") == "mine"
    assert p.mandate_for(1_500_000, 3, "D19", "Cluster House") is None


def test_condo_only_mandate_rejects_ec():
    p = bp.parse(_base(mandates=[{"label": "c", "max_price": 2_000_000, "beds": [3],
                                  "property_types": ["condo"]}]))
    assert p.mandate_for(1_500_000, 3, "D19", "Condominium") == "c"
    assert p.mandate_for(1_500_000, 3, "D19", "Executive Condominium") is None
    assert not p.wants_ec


def test_property_kind_mapping():
    assert bp.property_kind("Executive Condominium") == "ec"
    assert bp.property_kind("Condominium") == "condo"
    assert bp.property_kind("Apartment") == "condo"
    assert bp.property_kind("Cluster House") is None
    assert bp.property_kind(None) is None


def test_disabled_profile_gives_owner_defaults(monkeypatch):
    monkeypatch.setenv(bp.PROFILE_ENV, "none")
    bp.reset_cache()
    try:
        assert bp.active() is None
        assert bp.buyer_defaults() == ("SC", 0)
    finally:
        bp.reset_cache()


def test_active_profile_drives_buyer_defaults(use_profile):
    p = use_profile(_base())
    assert p is not None and bp.buyer_defaults() == ("PR", 0)


def test_invalid_json_fails_loudly(tmp_path, monkeypatch):
    f = tmp_path / "bad.json"
    f.write_text("{not json")
    monkeypatch.setenv(bp.PROFILE_ENV, str(f))
    bp.reset_cache()
    try:
        with pytest.raises(bp.ProfileError, match="invalid JSON"):
            bp.active()
    finally:
        bp.reset_cache()


def test_example_profile_is_valid():
    p = bp.load(bp.EXAMPLE_FILE)
    assert p.mandates and p.districts


def test_weekly_reads_profile_mandates(use_profile, monkeypatch):
    import importlib

    import weekly
    use_profile(_base())
    try:
        w = importlib.reload(weekly)
        assert w.MANDATES == [("mine", 1_600_000, (3,), "")]
        assert w.OCR_DISTRICTS == [16, 19, 28]
        assert w.mandate_for(1_500_000, 3, "D19") == "mine"
        assert w.mandate_for(1_500_000, 3, "D18") is None        # not in this profile
        assert w.mandate_summary() == "3BR<=$1.6M"
    finally:
        monkeypatch.setenv(bp.PROFILE_ENV, "none")
        bp.reset_cache()
        w = importlib.reload(weekly)
    assert w.MANDATES[0][0] == "his"                            # owner defaults restored


def test_roi_calculator_uses_profile_buyer(use_profile):
    from scoring.roi import ROICalculator
    use_profile(_base())
    calc = ROICalculator()
    assert (calc.buyer_type, calc.property_count) == ("PR", 0)
    explicit = ROICalculator(buyer_type="SC", property_count=1)
    assert (explicit.buyer_type, explicit.property_count) == ("SC", 1)


def test_roi_calculator_defaults_to_sc_without_profile():
    from scoring.roi import ROICalculator
    calc = ROICalculator()
    assert (calc.buyer_type, calc.property_count) == ("SC", 0)


def test_pr_first_property_pays_5pct_absd_in_roi(use_profile):
    from scoring.roi import ROICalculator
    listing = {"price": 1_600_000, "sqft": 1100, "beds": 3, "district": "D19"}
    sc = ROICalculator(buyer_type="SC", property_count=0).calculate(listing, hold_years=5,
                                                                         monthly_rent=4500)
    use_profile(_base())
    pr = ROICalculator().calculate(listing, hold_years=5, monthly_rent=4500)
    assert pr.entry_costs - sc.entry_costs == 80_000          # 5% ABSD on $1.6M
    assert pr.total_return == pytest.approx(sc.total_return - 80_000, abs=1)
