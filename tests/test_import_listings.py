"""import_listings — CSV/JSON intake, normalization, upsert + score."""

import json

import pytest

import import_listings as il
import listings_db


def test_to_listing_normalizes_sgprop_fields():
    out = il.to_listing({"project": "Treasure at Tampines", "price": "1,550,000", "sqft": "1033",
                         "bedrooms": "3", "district": "18", "property_type": "condo",
                         "listing_id": "abc", "floor_level": "High"}, "manual")
    assert out["id"] == "manual:abc" and out["district"] == "D18" and out["beds"] == 3
    assert out["psf"] == pytest.approx(1500.48, abs=0.01)
    assert out["property_type"] == "Condominium" and out["floor_level"] == "High"


def test_ec_type_and_pg_ids():
    out = il.to_listing({"project_name": "Bellewaters", "price": 1500000, "sqft": 1000,
                         "property_type": "EC", "id": "500228654", "source": "propertyguru"},
                        "manual")
    assert out["property_type"] == "Executive Condominium" and out["id"] == "500228654"


def test_stable_hash_id_without_listing_id():
    a = il.to_listing({"project": "X", "price": 1, "sqft": 2}, "m")
    b = il.to_listing({"project": "X", "price": 1, "sqft": 2}, "m")
    assert a["id"] == b["id"] and a["id"].startswith("m:")


@pytest.mark.parametrize("row,msg", [
    ({"project": "X", "price": 1}, "needs project"),
    ({"project": "X", "price": 1, "sqft": 2, "district": "D99"}, "bad district"),
    ({"project": "X", "price": 1, "sqft": 2, "property_type": "landed"}, "property_type"),
])
def test_bad_rows_rejected(row, msg):
    with pytest.raises(il.ListingImportError, match=msg):
        il.to_listing(row, "m")


def test_run_dry_run_reports_errors(tmp_path):
    f = tmp_path / "l.csv"
    f.write_text("project,price,sqft,bedrooms,district\nGood,1500000,1100,3,19\nBad,,1100,3,19\n")
    rep = il.run(str(f), dry_run=True)
    assert rep["valid"] == 1 and len(rep["errors"]) == 1 and "row 2" in rep["errors"][0]


def test_run_upserts_and_scores(tmp_path, monkeypatch):
    monkeypatch.setattr(listings_db, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(listings_db, "DB_FILE", str(tmp_path / "listings_db.json"))
    monkeypatch.setattr(listings_db, "SHEET_FILE", str(tmp_path / "listings_sheet.csv"))
    monkeypatch.setattr(listings_db, "_DB_LOCK_FILE", str(tmp_path / "listings_db.json.lock"))
    import poller
    scored = []
    monkeypatch.setattr(poller, "_score_keys", lambda keys, **k: (scored.extend(keys) or len(keys), []))
    f = tmp_path / "l.json"
    f.write_text(json.dumps([{"project": "Good", "price": 1500000, "sqft": 1100, "beds": 3,
                              "district": "D19", "listing_id": "1"}]))
    rep = il.run(str(f))
    assert rep["upsert"]["added"] == 1 and rep["scored"] == 1 and scored == ["manual:1"]
    rec = listings_db.load_db()["listings"]["manual:1"]
    assert rec["project_name"] == "Good" and rec["status"] == "active"
    assert rec["source"] == {"flow": "import", "file": "l.json"}
