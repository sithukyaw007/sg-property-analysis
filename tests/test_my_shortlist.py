"""my_shortlist — scope, gates (with reasons), EC eligibility, exit tiers, ranking."""

from datetime import date

import pytest

import buyer_profile as bp
import my_shortlist as ms

TODAY = date(2026, 10, 3)


def _profile(citizenship="PR", kinds=("condo", "ec")):
    return bp.parse({
        "purpose": "own_stay", "buyer_type": "PR", "household_citizenship": citizenship,
        "mandates": [{"label": "mine", "max_price": 1_600_000, "beds": [3],
                      "property_types": list(kinds)}],
        "districts": [18, 19],
    })


def _rec(i, project="Good Condo", district="D19", price=1_500_000, beds=3, sqft=1100,
         ptype="Condominium", last_seen="2026-09-29", **kw):
    r = {"id": str(i), "project_name": project, "district": district, "price": price,
         "beds": beds, "sqft": sqft, "psf": price / sqft, "property_type": ptype,
         "status": "active", "last_seen": last_seen, "url": f"https://x/{i}",
         "valuation_score": 60, "livability_score": 55, "score_1000": 600,
         "built_year": 2015, "unit_group": f"u{i}"}
    r.update(kw)
    return r


class FakePF:
    @staticmethod
    def personal_fit(name, district, listing=None, profile=None, cache=None):
        return {"score": 70 if name == "Good Condo" else 50, "components": {}, "schools": {}}


class FakeCtx:
    pf = FakePF()
    pf_cache: dict = {}
    URA = {
        "good condo": {"resale_transaction_count": 120,
                       "tenure": "99 yrs lease commencing from 2011"},
        "fresh ec": {"resale_transaction_count": None,
                     "tenure": "99 yrs lease commencing from 2019"},
        "old ec": {"resale_transaction_count": 80,
                   "tenure": "99 yrs lease commencing from 2010"},
        "tiny place": {"resale_transaction_count": 4, "tenure": "Freehold"},
        "loser condo": {"resale_transaction_count": 90,
                        "tenure": "99 yrs lease commencing from 2008"},
    }

    def is_ec(self, rec):
        return (rec.get("property_type") == "Executive Condominium"
                or rec.get("project_name") == "Old EC")          # relabelled on the portal

    def ura_entry(self, name, is_ec):
        return self.URA.get(name.lower())

    def realsmart(self, name):
        return None

    def headline(self, name):
        return "💎" in name


@pytest.fixture
def stubbed(monkeypatch):
    monkeypatch.setattr(ms.bed_bands, "check",
                        lambda p, b, s: {"verdict": "mismatch", "looks_like": 2}
                        if s < 800 else {"verdict": "ok"})

    def fake_exit(name, district, ura, rs):
        table = {"Good Condo": (0.95, 40, 38), "Old EC": (0.92, 30, 29),
                 "Loser Condo": (0.60, 30, 18), "Fresh EC": (None, 0, 0),
                 "Tiny Place": (None, 0, 0)}
        pr, pairs, profit = table.get(name, (None, 0, 0))
        return {"resales_5y": (ura or {}).get("resale_transaction_count"), "pair_record": pr,
                "pairs": pairs, "raw_share": (profit / pairs) if pairs else None,
                "exit_lens": None, "realsmart_pct_profitable": None,
                "realsmart_resales": None, "realscore": None}
    monkeypatch.setattr(ms, "exit_stats", fake_exit)


def _run(records, profile=None, **kw):
    return ms.build(profile or _profile(), records, FakeCtx(), today=TODAY, **kw)


def _reasons(res):
    return {(x["project"], x["reason"].split(" (")[0].split(":")[0]) for x in res["rejected"]}


def test_scope_filters_are_counted_not_listed(stubbed):
    res = _run([
        _rec(1, district="D15"),                       # out of profile districts
        _rec(2, price=1_700_000),                      # over budget
        _rec(3, beds=2),                               # wrong beds
        _rec(4, ptype="Cluster House"),                # format
        _rec(5, status="stale"),
    ])
    assert res["projects"] == [] and res["rejected"] == []
    assert sum(res["out_of_scope"].values()) == 5


def test_gates_record_reasons(stubbed):
    res = _run([
        _rec(1, last_seen="2026-06-01"),
        _rec(2, sqft=700),                              # bedroom relabel
        _rec(3, project="💎Cheapest💎"),
        _rec(4, project="Tiny Place"),
        _rec(5, project="Loser Condo"),
        _rec(6, project="Fresh EC", ptype="Executive Condominium", built_year=2023),
    ])
    reasons = _reasons(res)
    assert ("Good Condo", "not seen since 2026-06-01") in reasons
    assert ("Good Condo", "bedroom relabel") in reasons
    assert ("💎Cheapest💎", "no usable project name") in reasons
    assert ("Tiny Place", "thin resale market") in reasons
    assert ("Loser Condo", "poor exit record") in reasons
    assert any(p == "Fresh EC" and r.startswith("EC new or within mop") for p, r in reasons)
    assert res["projects"] == []


def test_ec_within_mop_allowed_for_citizen_household(stubbed):
    res = _run([_rec(1, project="Fresh EC", ptype="Executive Condominium", built_year=2023)],
               profile=_profile(citizenship="SC"))
    assert [p["project"] for p in res["projects"]] == ["Fresh EC"]
    assert res["projects"][0]["tier"] == "C"


def test_relabelled_ec_detected_and_staged(stubbed):
    res = _run([_rec(1, project="Old EC", built_year=2014)])
    p = res["projects"][0]
    assert p["kind"] == "ec" and p["ec"]["stage"] == "privatised"


def test_condo_only_profile_drops_ecs(stubbed):
    res = _run([_rec(1, project="Old EC", built_year=2014)], profile=_profile(kinds=("condo",)))
    assert res["projects"] == [] and res["out_of_scope"]


def test_tiers_and_ranking(stubbed):
    res = _run([_rec(1), _rec(2, price=1_400_000), _rec(3, project="Old EC", built_year=2014)])
    names = [(p["project"], p["tier"]) for p in res["projects"]]
    assert names == [("Good Condo", "A"), ("Old EC", "A")]     # fit 70 beats 50
    good = res["projects"][0]
    assert good["listings"] == 2 and good["ask_min"] == 1_400_000
    assert good["upfront_taxes"]["absd"] == round(good["rep"]["price"] * 0.05)


def test_duplicate_units_collapse_to_cheapest(stubbed):
    res = _run([_rec(1, unit_group="g", price=1_550_000), _rec(2, unit_group="g", price=1_500_000)])
    p = res["projects"][0]
    assert p["listings"] == 1 and p["ask_min"] == 1_500_000


@pytest.mark.parametrize("x,tier", [
    ({"resales_5y": 120, "pair_record": 0.95, "raw_share": 0.97, "pairs": 40}, "A"),
    ({"resales_5y": 40, "pair_record": 0.95, "raw_share": 0.97, "pairs": 40}, "B"),
    ({"resales_5y": 20, "pair_record": None, "raw_share": None, "pairs": 0}, "C"),
    ({"resales_5y": None, "pair_record": None, "raw_share": None, "pairs": 0}, "C"),
    ({"resales_5y": 5, "pair_record": None, "raw_share": None, "pairs": 0}, None),
    ({"resales_5y": 90, "pair_record": 0.7, "raw_share": 0.75, "pairs": 25}, None),
    ({"resales_5y": 90, "pair_record": 0.6, "raw_share": 0.6, "pairs": 8}, "B"),   # too few pairs
])
def test_exit_tier_rules(x, tier):
    x.setdefault("realsmart_pct_profitable", None)
    x.setdefault("realsmart_resales", None)
    assert ms.exit_tier(x)[0] == tier


def test_realsmart_poor_record_rejects_with_depth():
    x = {"resales_5y": 300, "pair_record": 0.9, "raw_share": 0.9, "pairs": 30,
         "realsmart_pct_profitable": 85.0, "realsmart_resales": 374}
    tier, why = ms.exit_tier(x)
    assert tier is None and "realsmart 85%" in why


def test_remaining_lease():
    assert ms.remaining_lease("99 yrs lease commencing from 2011", TODAY) == 84
    assert ms.remaining_lease("Freehold", TODAY) is None
    assert ms.remaining_lease(None, TODAY) is None


def test_render_contains_rejections_and_caveats(stubbed):
    res = _run([_rec(1), _rec(2, project="Loser Condo")])
    md = ms.render_md(res, top=10)
    assert "Good Condo" in md and "poor exit record" in md and "## Caveats" in md
