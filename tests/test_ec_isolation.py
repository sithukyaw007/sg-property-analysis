"""Executive Condominium isolation — ECs compare to ECs, condos to condos.

ECs are a separate URA product that trades below private condos. Before the
EC export, EC listings were benchmarked against condo district medians and
read as systematically "cheap" (median score_1000 592 vs 497). These tests pin
the separation at every cross-project join, and that adding EC data leaves
condo results untouched.
"""

import glob
import os

from scoring.full_scorer import FullScorer
from scoring.relative_value import compute_relative_value

CURRENT_YEAR = 2026


def _peers(n, base_psf, is_ec, prefix, district="D19"):
    out = {}
    for i in range(n):
        lease = 2015 - i
        e = {
            "project_name": f"{prefix} {i}",
            "district": district,
            "tenure": f"99 yrs lease commencing from {lease}",
            "lease_start_year": lease,
            "median_psf": base_psf,
            "transaction_count": 60,
        }
        if is_ec:
            e["is_ec"] = True
        out[f"{prefix.lower()} {i}"] = e
    return out


def _mixed_cache():
    ura = _peers(8, 2000, False, "Condo")
    ura.update(_peers(8, 1400, True, "Ecproj"))
    return ura


class TestRelativeValuePeers:
    def test_ec_subject_uses_only_ec_peers(self):
        rv = compute_relative_value(1400, 8, "D19", "99 yrs lease commencing from 2015",
                                    _mixed_cache(), CURRENT_YEAR, subject_is_ec=True)
        assert rv is not None
        assert rv["peer_count"] == 8                      # the 8 EC peers, not all 16
        # Priced at the EC level -> roughly fair, NOT a ~30% "discount" vs condos
        assert abs(rv["premium_vs_age_adjusted_median_pct"]) < 10

    def test_condo_subject_ignores_ec_peers(self):
        mixed = compute_relative_value(2000, 8, "D19", "99 yrs lease commencing from 2015",
                                       _mixed_cache(), CURRENT_YEAR)
        condo_only = compute_relative_value(2000, 8, "D19", "99 yrs lease commencing from 2015",
                                            _peers(8, 2000, False, "Condo"), CURRENT_YEAR)
        assert mixed == condo_only

    def test_ec_subject_without_ec_peers_is_neutral_none(self):
        rv = compute_relative_value(1400, 8, "D19", "99 yrs lease commencing from 2015",
                                    _peers(8, 2000, False, "Condo"), CURRENT_YEAR,
                                    subject_is_ec=True)
        assert rv is None


class TestUraLookupByKind:
    def test_lookup_only_returns_same_product(self):
        ura = _mixed_cache()
        ura["shared name"] = {"project_name": "Shared Name", "district": "D19"}
        s = FullScorer(ura_data=ura, cohort_stats={})
        s._current_is_ec = True
        assert s._fuzzy_ura_lookup("ecproj 1")["project_name"] == "Ecproj 1"
        assert s._fuzzy_ura_lookup("condo 1") is None
        assert s._fuzzy_ura_lookup("shared name") is None
        s._current_is_ec = False
        assert s._fuzzy_ura_lookup("condo 1")["project_name"] == "Condo 1"
        assert s._fuzzy_ura_lookup("ecproj 1") is None
        assert s._fuzzy_ura_lookup("shared name")["project_name"] == "Shared Name"

    def test_partition_rebuilt_when_cache_replaced(self):
        s = FullScorer(ura_data=_peers(2, 2000, False, "Condo"), cohort_stats={})
        assert s._fuzzy_ura_lookup("condo 0") is not None
        s.ura_data = _peers(2, 1400, True, "Ecproj")
        s._current_is_ec = True
        assert s._fuzzy_ura_lookup("ecproj 0") is not None


class TestPanelsExcludeEc:
    def test_backtest_glob_is_condo_only(self, tmp_path):
        import backtest as bt
        assert glob.fnmatch.fnmatch("ura_district_D19.csv", os.path.basename(bt.DATA_GLOB))
        assert not glob.fnmatch.fnmatch("ura_district_D19_EC.csv", os.path.basename(bt.DATA_GLOB))
        assert glob.fnmatch.fnmatch("ura_district_D19_EC.csv", os.path.basename(bt.EC_DATA_GLOB))

    def test_district_rates_skip_ec(self, tmp_path, monkeypatch):
        import json

        from scoring import district_scorer as ds
        cache = {"projects": {}}
        for i in range(3):
            cache["projects"][f"c{i}"] = {"district": "D19", "annualized_appreciation": 4.0}
            cache["projects"][f"e{i}"] = {"district": "D19", "annualized_appreciation": 9.0,
                                          "is_ec": True}
        (tmp_path / "ura_cache.json").write_text(json.dumps(cache))
        monkeypatch.setattr(ds, "_DATA_DIR", str(tmp_path))
        rates = ds.DistrictScorer._derive_ura_district_rates()
        assert rates["19"]["rate"] == 0.04 and rates["19"]["projects"] == 3


class TestCacheBuilderTagsEc:
    def test_ec_file_entries_tagged(self, tmp_path, monkeypatch):
        import invest
        header = ("Project Name,Transacted Price ($),Area (SQFT),Unit Price ($ PSF),Sale Date,"
                  "Street Name,Type of Sale,Type of Area,Area (SQM),Unit Price ($ PSM),"
                  "Nett Price($),Property Type,Number of Units,Tenure,Postal District,"
                  "Market Segment,Floor Level\n")
        row = ('{name},"1,500,000","1,000",1500,{mon},X ROAD,Resale,Strata,93,"16,000",-,'
               '{ptype},1,99 yrs lease commencing from 2015,19,Outside Central Region,06 to 10\n')
        (tmp_path / "ura_district_D19.csv").write_text(
            header + row.format(name="CONDO A", mon="Jan-26", ptype="Condominium"))
        (tmp_path / "ura_district_D19_EC.csv").write_text(
            header + row.format(name="EC B", mon="Jan-26", ptype="Executive Condominium"))
        monkeypatch.setattr(invest, "load_ura_cache", lambda: {})
        monkeypatch.setattr(invest, "save_ura_cache", lambda c: None)
        monkeypatch.setattr(invest, "export_ura_cache_csv", lambda c: None)
        cache = invest.build_ura_cache_from_csv([str(tmp_path / "ura_district_D19*.csv")])
        assert cache["ec b"].get("is_ec") is True
        assert "is_ec" not in cache["condo a"]


class TestUraApiExportsEc:
    def test_refresh_writes_ec_files(self, tmp_path, monkeypatch):
        import sys
        import types

        import ura_api

        calls = []

        def tx_csv(store, d, path, property_types=("Apartment", "Condominium")):
            calls.append(("tx", d, tuple(property_types), path.name))
            if d == 19:
                path.write_text("h\nrow\n")
                return 1
            return 0

        def rent_csv(store, d, path, property_types=("Non-landed Properties",)):
            calls.append(("rent", d, tuple(property_types), path.name))
            if d == 19:
                path.write_text("h\nrow\n")
                return 1
            return 0

        export = types.SimpleNamespace(transactions_csv=tx_csv, rentals_csv=rent_csv,
                                       EC_TYPES=("Executive Condominium",))
        fake = {
            "sgprop": types.SimpleNamespace(export=export),
            "sgprop.export": export,
            "sgprop.store": types.SimpleNamespace(Store=lambda: object()),
            "sgprop.sync": types.SimpleNamespace(sync_transactions=lambda *a, **k: "ok",
                                                 sync_rentals=lambda *a, **k: "ok"),
            "sgprop.ura": types.SimpleNamespace(UraClient=lambda: object()),
        }
        for k, v in fake.items():
            monkeypatch.setitem(sys.modules, k, v)
        monkeypatch.setattr(ura_api, "DATA_DIR", tmp_path)
        out = ura_api.refresh(rebuild=False)
        assert out["ec_tx_rows"] == 1 and out["ec_rent_rows"] == 1
        assert (tmp_path / "ura_district_D19_EC.csv").exists()
        assert (tmp_path / "ura_rental_D19_EC.csv").exists()
        assert ("tx", 19, ("Executive Condominium",), "ura_district_D19_EC.csv.tmp") in calls
        out2 = ura_api.refresh(rebuild=False, ec=False)
        assert "ec_tx_rows" not in out2 and "ec_rent_rows" not in out2
