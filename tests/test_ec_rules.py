"""scoring/ec_rules.py — EC lifecycle stage, regime and buyer eligibility."""

from datetime import date

import pytest

from scoring import ec_rules

TODAY = date(2026, 10, 3)


def _s(top=None, tenure=None, who=None):
    return ec_rules.ec_status(top_year=top, tenure=tenure, household_citizenship=who, today=TODAY)


def test_lease_start_year_parses_ura_tenure():
    assert ec_rules.lease_start_year("99 yrs lease commencing from 2010") == 2010
    assert ec_rules.lease_start_year("99-year Leasehold") is None
    assert ec_rules.lease_start_year(None) is None


def test_post_mop_ec_is_open_to_pr_household():
    s = _s(top=2018, tenure="99 yrs lease commencing from 2014", who="PR")
    assert s["regime"] == "pre_may_2026" and s["regime_certain"]
    assert s["stage"] == "post_mop"
    assert s["mop_ends_year"] == 2023 and s["privatised_year"] == 2028
    assert s["years_to_privatisation"] == 2
    assert s["household_eligible"] is True
    assert s["eligible_citizenship"] == ["SC", "PR"]


def test_within_mop_ec_is_citizen_only():
    s = _s(top=2023, tenure="99 yrs lease commencing from 2019", who="PR")
    assert s["stage"] == "new_or_within_mop"
    assert s["household_eligible"] is False
    assert _s(top=2023, tenure="99 yrs lease commencing from 2019", who="SC")["household_eligible"]


def test_privatised_ec_is_open_to_everyone():
    s = _s(top=2014, tenure="99 yrs lease commencing from 2010", who="Foreigner")
    assert s["stage"] == "privatised" and s["household_eligible"] is True
    assert s["years_to_privatisation"] == 0


def test_boundary_year_is_flagged():
    s = _s(top=2021, tenure="99 yrs lease commencing from 2017")
    assert s["mop_ends_year"] == 2026 and s["boundary_year"] is True
    assert s["stage"] == "post_mop"


def test_new_regime_for_tenders_after_may_2026():
    s = _s(top=2031, tenure="99 yrs lease commencing from 2027", who="SC")
    assert s["regime"] == "may_2026" and s["mop_years"] == 10
    assert s["privatised_after_years"] == 15
    assert s["mop_ends_year"] == 2041 and s["privatised_year"] == 2046


def test_award_in_cutoff_year_is_stricter_regime_and_uncertain():
    s = _s(top=2030, tenure="99 yrs lease commencing from 2026")
    assert s["regime"] == "may_2026" and s["regime_certain"] is False


def test_top_estimated_from_award_year_when_missing():
    s = _s(tenure="99 yrs lease commencing from 2014", who="PR")
    assert s["top_year"] == 2018 and s["top_year_estimated"] is True
    assert s["stage"] == "post_mop"


def test_unknown_when_no_dates():
    s = _s(who="PR")
    assert s["stage"] == "unknown" and s["household_eligible"] is None


def test_household_none_leaves_eligibility_open():
    assert _s(top=2018, tenure="99 yrs lease commencing from 2014")["household_eligible"] is None


def test_describe_lines():
    assert "post-MOP" in ec_rules.describe(_s(top=2018, tenure="99 yrs lease commencing from 2014"))
    assert "verify" in ec_rules.describe(_s())


def test_rules_file_has_provenance():
    r = ec_rules.load_rules()
    assert r["as_of"] and r["sources"]
    assert {x["mop_years"] for x in r["regimes"]} == {5, 10}


class TestScorerEcIdentityAndLifecycle:
    """EC identity from URA prints, and the lifecycle appreciation cap."""

    @pytest.fixture
    def seeded(self, monkeypatch):
        from scoring import full_scorer as fs
        t = date(2026, 6, 1)
        from datetime import datetime
        dt = datetime(t.year, t.month, 1)
        monkeypatch.setattr(fs, "_raw_prints_cache", {"D19": {
            "piermont grand": [(1000, 1500, "06 to 10", dt, "Resale", True)],
            "the quartz": [(1000, 1600, "06 to 10", dt, "Resale", False)],
            "mixed place": [(1000, 1500, "06 to 10", dt, "Resale", True),
                            (1000, 1600, "06 to 10", dt, "Resale", False)],
        }})
        return fs

    def test_condo_label_upgraded_when_ura_says_ec(self, seeded):
        fs = seeded
        assert fs.listing_is_ec({"project_name": "Piermont Grand", "district": "D19",
                                 "property_type": "Condominium"})
        assert not fs.listing_is_ec({"project_name": "The Quartz", "district": "D19",
                                     "property_type": "Condominium"})
        assert not fs.listing_is_ec({"project_name": "Mixed Place", "district": "D19",
                                     "property_type": "Condominium"})
        # never downgrades an EC label
        assert fs.listing_is_ec({"project_name": "The Quartz", "district": "D19",
                                 "property_type": "Executive Condominium"})

    def test_lifecycle_caps_unprivatised_ec_at_baseline(self, seeded):
        s = seeded.FullScorer(ura_data={}, cohort_stats={})
        listing = {"district": "D19", "built_year": 2019}
        status = ec_rules.ec_status(top_year=2019, tenure="99 yrs lease commencing from 2015")
        rate, adj, why = s._apply_ec_lifecycle_adjustment(0.08, 0.0, "", listing,
                                                           "ura_5yr_cagr", status)
        base = s._get_regional_baseline(listing)
        assert rate == pytest.approx(base) and adj == pytest.approx(base - 0.08)
        assert "EC lifecycle" in why

    def test_lifecycle_leaves_long_privatised_ec_alone(self, seeded):
        s = seeded.FullScorer(ura_data={}, cohort_stats={})
        status = ec_rules.ec_status(top_year=2005, tenure="99 yrs lease commencing from 2001")
        rate, adj, why = s._apply_ec_lifecycle_adjustment(
            0.08, 0.0, "", {"district": "D19"}, "ura_5yr_cagr", status)
        assert (rate, adj, why) == (0.08, 0.0, "")

    def test_lifecycle_ignores_baseline_sources(self, seeded):
        s = seeded.FullScorer(ura_data={}, cohort_stats={})
        status = ec_rules.ec_status(top_year=2019, tenure="99 yrs lease commencing from 2015")
        assert s._apply_ec_lifecycle_adjustment(
            0.05, 0.0, "", {"district": "D19"}, "regional_baseline", status) == (0.05, 0.0, "")
