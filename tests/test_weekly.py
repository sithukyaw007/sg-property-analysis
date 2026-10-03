"""Tests for the weekly scan — the AI gate, verdict readback, digest, store push."""

import json
import os
import time
from datetime import datetime, timedelta

import pytest

import weekly


def _row(rid, score=700, beds=3, name="Test Condo", **kw):
    # Defaults sit INSIDE the mandate (3BR @ $1.4M = "his") so gate tests
    # exercise the rule under test rather than tripping the mandate filter.
    r = {"id": rid, "project_name": name, "district": "D19", "beds": beds,
         "price": 1_400_000, "psf": 2000.0, "score_1000": score,
         "url": f"https://pg/{rid}"}
    r.update(kw)
    return r


def _rec(rid, **kw):
    r = {"id": rid, "price": 1_400_000, "sqft": 950.0, "psf": 2000.0,
         "status": "active", "unit_group": "test condo|3|333", "ingest_flags": []}
    r.update(kw)
    return r


class TestGate:
    def test_above_gate_shortlisted_below_gate_rejected(self):
        rows = [_row("a", score=700), _row("b", score=649, name="Other Condo")]
        db = {"a": _rec("a"), "b": _rec("b")}
        short, rej = weekly.select_candidates(rows, db, min_score=650)
        assert [c["id"] for c in short] == ["a"]
        assert [c["id"] for c in rej] == ["b"]
        # The reason names the lens and its bar, not just "below gate" — with
        # several lenses able to nominate, "which one said no, and by how much"
        # is the whole content of the rejection.
        assert "below every lens's bar" in rej[0]["gate_reason"]
        assert "mmr 649 < 650" in rej[0]["gate_reason"]

    def test_shortlist_is_score_ordered_and_capped(self):
        rows = [_row(str(i), score=650 + i, name=f"Condo {i}") for i in range(6)]
        db = {str(i): _rec(str(i)) for i in range(6)}
        short, rej = weekly.select_candidates(rows, db, min_score=650, max_ai=3)
        assert [c["id"] for c in short] == ["5", "4", "3"]     # best first
        assert len(rej) == 3
        assert all("over the per-scan AI cap" in c["gate_reason"] for c in rej)

    def test_same_condo_same_beds_deduped_within_the_scan(self):
        rows = [_row("a", score=700), _row("b", score=690)]   # same name + beds
        db = {"a": _rec("a"), "b": _rec("b")}
        short, rej = weekly.select_candidates(rows, db)
        assert [c["id"] for c in short] == ["a"]
        assert "already shortlisted this scan" in rej[0]["gate_reason"]

    def test_different_bed_count_is_a_separate_call(self):
        rows = [_row("a", score=700, beds=3), _row("b", score=690, beds=4)]
        db = {"a": _rec("a"), "b": _rec("b")}
        short, _ = weekly.select_candidates(rows, db)
        assert {c["id"] for c in short} == {"a", "b"}

    def test_cooldown_blocks_same_unit_type_only(self):
        rows = [_row("a", score=700, beds=3), _row("b", score=700, beds=4)]
        db = {"a": _rec("a"), "b": _rec("b")}
        cooldown = {("test-condo", 3): "2026-07-01"}
        short, rej = weekly.select_candidates(rows, db, cooldown_index=cooldown)
        assert [c["id"] for c in short] == ["b"]
        assert "cooldown" in rej[0]["gate_reason"]

    def test_wildcard_cooldown_blocks_every_bed_count(self):
        rows = [_row("a", beds=3), _row("b", beds=4)]
        db = {"a": _rec("a"), "b": _rec("b")}
        short, rej = weekly.select_candidates(
            rows, db, cooldown_index={("test-condo", "*"): "2026-07-01"})
        assert short == []
        assert len(rej) == 2

    def test_stale_and_incomplete_records_never_reach_the_agent(self):
        rows = [_row("a", name="A"), _row("b", name="B"), _row("c", name="C")]
        db = {"a": _rec("a", status="stale"),
              "b": _rec("b", sqft=None),
              "c": _rec("c")}
        short, rej = weekly.select_candidates(rows, db)
        assert [c["id"] for c in short] == ["c"]
        reasons = {c["id"]: c["gate_reason"] for c in rej}
        assert "stale" in reasons["a"]
        assert "incomplete" in reasons["b"]

    def test_unscored_row_is_rejected_not_crashed(self):
        rows = [_row("a", score=None)]
        short, rej = weekly.select_candidates(rows, {"a": _rec("a")})
        assert short == [] and rej[0]["gate_reason"] == "not scored"

    def test_row_missing_from_db_is_rejected(self):
        # A listing scored in the poll but absent from the DB snapshot has no
        # price/sqft/psf to gate on — it must fall out, not raise.
        short, rej = weekly.select_candidates([_row("ghost")], {})
        assert short == [] and "incomplete" in rej[0]["gate_reason"]

    def test_ingest_flags_ride_along_for_the_prompt(self):
        db = {"a": _rec("a", ingest_flags=["bedroom_sqft_mismatch"])}
        short, _ = weekly.select_candidates([_row("a")], db)
        assert short[0]["ingest_flags"] == ["bedroom_sqft_mismatch"]
        assert "bedroom_sqft_mismatch" in weekly._agent_prompt(short[0])
        assert "--from-db a" in weekly._agent_prompt(short[0])


class TestDeepScopeRotation:
    """One scope per cycle is crawled to its real end.

    A routine poll reads only the newest POLL_MAX_PAGES pages per scope, so
    deeper stock is never re-seen — 5,378 of 9,040 `active` records were last
    sighted in June — and sweep_staleness cannot age it out either, because it
    only judges listings inside the window a poll demonstrably covered. This
    rotation is the only mechanism that ever closes that loop.
    """

    def test_every_scope_is_covered_before_any_repeats(self):
        n = len(weekly.SCAN_DISTRICTS) * len(weekly.SCAN_BEDS)
        seen = [weekly.deep_scope({"history": [0] * i}) for i in range(n)]
        assert len({(d[0], b[0]) for d, b in seen}) == n

    def test_it_wraps_rather_than_running_off_the_end(self):
        n = len(weekly.SCAN_DISTRICTS) * len(weekly.SCAN_BEDS)
        assert weekly.deep_scope({"history": [0] * n}) == weekly.deep_scope({})

    def test_a_fresh_state_picks_the_first_scope(self):
        d, b = weekly.deep_scope({})
        assert d == [weekly.SCAN_DISTRICTS[0]] and b == [weekly.SCAN_BEDS[0]]

    def test_a_scope_is_one_district_and_one_bed_count(self):
        # It must be a single scope: allow_extend on all 18 would multiply the
        # request budget, which is the failure the hard page cap just fixed.
        d, b = weekly.deep_scope({"history": [0] * 7})
        assert len(d) == 1 and len(b) == 1


class TestSightingFreshness:
    """--full-book gates the whole book, which promotes never-re-verified stock.

    On the 2026-07-29 sweep 3 of 12 agent runs went to listings last seen
    2026-06-07/08 with times_seen=1, absent from the 07-28 poll of their own
    district and bed count. They stay `active` because the staleness sweep only
    judges listings inside the recency window a poll demonstrably covered.
    """

    @staticmethod
    def _ago(days):
        return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    def test_an_unverified_sighting_does_not_earn_an_agent_run(self):
        rows = [_row("a", score=800)]
        db = {"a": _rec("a", last_seen=self._ago(53), times_seen=1)}
        short, rej = weekly.select_candidates(rows, db)
        assert short == []
        assert "not re-seen since" in rej[0]["gate_reason"]
        assert "unverified" in rej[0]["gate_reason"]

    def test_a_recent_sighting_passes(self):
        rows = [_row("a", score=800)]
        db = {"a": _rec("a", last_seen=self._ago(2), times_seen=3)}
        short, _ = weekly.select_candidates(rows, db)
        assert [c["id"] for c in short] == ["a"]

    def test_the_boundary_is_inclusive(self):
        # Exactly at the limit is still fresh; one day past it is not.
        for days, expect in ((weekly.MAX_SIGHTING_AGE_DAYS, ["a"]),
                             (weekly.MAX_SIGHTING_AGE_DAYS + 1, [])):
            rows = [_row("a", score=800)]
            db = {"a": _rec("a", last_seen=self._ago(days))}
            short, _ = weekly.select_candidates(rows, db)
            assert [c["id"] for c in short] == expect

    def test_a_missing_or_broken_date_is_never_read_as_stale(self):
        # Dropping a record because its date failed to parse would silently
        # shrink the book — the guard has to abstain, not reject.
        for bad in (None, "", "not-a-date", 20260607, {"d": 1}):
            rows = [_row("a", score=800)]
            db = {"a": _rec("a", last_seen=bad)}
            short, _ = weekly.select_candidates(rows, db)
            assert [c["id"] for c in short] == ["a"], f"rejected on last_seen={bad!r}"

    def test_age_is_reported_on_the_candidate(self):
        rows = [_row("a", score=800)]
        db = {"a": _rec("a", last_seen=self._ago(4))}
        short, _ = weekly.select_candidates(rows, db)
        assert short[0]["sighting_age_days"] == 4

    def test_stale_status_still_wins_the_reason(self):
        # An explicitly-swept listing should say so, not blame its sighting age.
        rows = [_row("a", score=800)]
        db = {"a": _rec("a", status="stale", last_seen=self._ago(99))}
        _, rej = weekly.select_candidates(rows, db)
        assert "went stale this cycle" in rej[0]["gate_reason"]


class TestMandate:
    """Two buyers, two budgets, one OCR region constraint (stated 2026-07-28).
    Off-mandate listings must never reach an agent however well they score —
    nobody can buy them."""

    def test_his_budget_and_beds(self):
        assert weekly.mandate_for(1_450_000, 3) == "his"
        assert weekly.mandate_for(1_800_000, 3) == "his"      # exactly at budget

    def test_hers_takes_what_his_cannot(self):
        assert weekly.mandate_for(2_400_000, 3) == "hers"     # over his budget
        assert weekly.mandate_for(1_450_000, 4) == "hers"     # 4BR: not his beds
        assert weekly.mandate_for(2_500_000, 4) == "hers"
        assert weekly.mandate_for(1_850_000, 3) == "hers"     # just over his

    def test_cheapest_fitting_mandate_wins(self):
        # A $1.4M 3BR is his find, not hers — she has better options there.
        assert weekly.mandate_for(1_400_000, 3) == "his"

    def test_off_mandate_returns_none(self):
        assert weekly.mandate_for(2_600_000, 3) is None       # over both budgets
        assert weekly.mandate_for(1_200_000, 2) is None       # 2BR: neither wants
        assert weekly.mandate_for(1_200_000, 5) is None
        assert weekly.mandate_for(None, 3) is None
        assert weekly.mandate_for(1_200_000, None) is None

    def test_gate_cuts_off_mandate_however_good_the_score(self):
        rows = [_row("a", score=900, beds=2, name="Great 2BR"),        # 2BR
                _row("b", score=880, beds=3, name="Pricey", price=3_000_000),
                _row("c", score=700, beds=3, name="Parc Riviera")]
        db = {"a": _rec("a"), "b": _rec("b", price=3_000_000), "c": _rec("c")}
        short, rej = weekly.select_candidates(rows, db)
        assert [c["id"] for c in short] == ["c"]
        assert all("outside the mandate" in c["gate_reason"] for c in rej)

    def test_shortlisted_candidates_carry_their_mandate(self):
        rows = [_row("a", score=700, beds=3, price=1_400_000, name="A"),
                _row("b", score=690, beds=4, price=2_400_000, name="B")]
        db = {"a": _rec("a", price=1_400_000), "b": _rec("b", price=2_400_000)}
        short, _ = weekly.select_candidates(rows, db)
        assert {c["id"]: c["mandate"] for c in short} == {"a": "his", "b": "hers"}

    def test_hers_cannot_crowd_out_his(self):
        # Her budget is ~$1M larger, so on a raw score ranking her candidates
        # would take every slot and his tighter budget would never be researched
        # — his single candidate scores below all six of hers.
        rows = [_row(f"h{i}", score=800 - i, beds=4, price=2_400_000,
                     name=f"Hers {i}") for i in range(6)]
        rows += [_row("m1", score=660, beds=3, price=1_400_000, name="His One")]
        db = {r["id"]: _rec(r["id"], price=r["price"]) for r in rows}
        short, _ = weekly.select_candidates(rows, db, max_ai=4)
        assert "m1" in [c["id"] for c in short], "his mandate was crowded out"
        assert len(short) == 4, "spare slots must not be wasted"

    def test_a_lone_mandate_still_uses_the_whole_budget(self):
        # Fair-share must not become a hard cap: when only one buyer has stock
        # this week, halving the budget would just waste agent runs.
        rows = [_row(str(i), score=700 + i, beds=3, price=1_400_000,
                     name=f"Condo {i}") for i in range(6)]
        db = {str(i): _rec(str(i)) for i in range(6)}
        short, rej = weekly.select_candidates(rows, db, max_ai=4)
        assert len(short) == 4
        assert {c["mandate"] for c in short} == {"his"}
        assert all("over the per-scan AI cap" in c["gate_reason"] for c in rej)

    def test_region_is_enforced_at_the_gate_not_just_the_scrape(self):
        # The DB holds thousands of older out-of-region listings; re-gating
        # must not shortlist RCR stock just because the scrape scope changed.
        assert weekly.in_region("D19") and weekly.in_region(28) and weekly.in_region("d16")
        assert not weekly.in_region("D15")     # RCR
        assert not weekly.in_region("D03")     # RCR
        assert not weekly.in_region(None)      # unreadable fails closed
        assert not weekly.in_region("")

    def test_out_of_region_is_rejected_with_its_own_reason(self):
        rows = [_row("a", score=900, district="D15"),   # RCR, otherwise perfect
                _row("b", score=700, district="D19")]   # OCR
        db = {"a": _rec("a"), "b": _rec("b")}
        short, rej = weekly.select_candidates(rows, db)
        assert [c["id"] for c in short] == ["b"]
        assert "out of region" in rej[0]["gate_reason"]

    def test_mandate_for_respects_district_when_given(self):
        assert weekly.mandate_for(1_400_000, 3, "D19") == "his"
        assert weekly.mandate_for(1_400_000, 3, "D15") is None
        assert weekly.mandate_for(1_400_000, 3) == "his"   # district optional

    def test_scan_scope_is_pruned_ocr_and_3_4br(self):
        assert weekly.SCAN_BEDS == [3, 4]                     # no 2BR
        assert weekly.SCAN_MAX_PRICE == 2_500_000
        # OCR minus the four ruled out as too far / no resale market.
        assert set(weekly.SCAN_DISTRICTS) == set(range(16, 29)) - {17, 24, 25, 27}
        assert set(weekly.OCR_EXCLUDED) == {17, 24, 25, 27}

    def test_excluded_ocr_districts_are_out_of_region(self):
        for d in (17, 24, 25, 27):
            assert not weekly.in_region(f"D{d}"), d
        for d in (16, 18, 19, 20, 21, 22, 23, 26, 28):
            assert weekly.in_region(f"D{d}"), d


class TestMarketingTitles:
    """PropertyGuru sometimes has no project name and the scraper falls back to
    the listing headline. Those must never reach an agent — every downstream key
    (URA comps, realsmart slug, eval memory, cooldown) is the project name."""

    REAL = ["JadeScape", "The Continuum", "Parc Riviera", "Alexis", "Artra",
            "Sims Green", "d'Leedon", "8 @ Mount Sophia", "West Bay Condo"]
    JUNK = [
        "Cheapest\U0001F48ED03\U0001F48EBest Value\U0001F48EFreehold\U0001F48EDuplex Penthouse\U0001F48E",
        "Cheapest 3Br! FH! Within 1km to St. Andrews",
        "CHEAP !!! WALK TO MRT!!! SUPER CONVENIENT!!! LOTS OF AMENITIES",
        "1km to Temasek Pri Sch. Bayshore MRT. Unblocked Sea View.",
        "Key Collection This Year! West Side's Cheapest New Launch",
        "Brand New Freehold Conservation Apartment - Vintage Charm, Modern Ease",
        "$5280/mth rent! Freehold Dual Key - Super High Rental Yield Condo",
        "",
    ]

    def test_real_project_names_pass(self):
        for n in self.REAL:
            assert not weekly.looks_like_marketing_title(n), n

    def test_marketing_headlines_are_caught(self):
        for n in self.JUNK:
            assert weekly.looks_like_marketing_title(n), n

    def test_ura_match_rescues_a_loud_but_real_name(self):
        # The government's project list outranks our heuristics: a real
        # development must never be demoted for having a shouty name.
        loud = "Cheap Freehold Suites!"
        assert weekly.looks_like_marketing_title(loud)
        assert not weekly.looks_like_marketing_title(
            loud, known_projects={"cheap freehold suites!"})

    def test_no_ura_match_alone_does_not_condemn(self):
        # Genuine new launches have no URA prints yet — absence of a match is
        # not evidence of a junk name.
        assert not weekly.looks_like_marketing_title(
            "Some New Launch", known_projects={"other project"})

    def test_gate_rejects_them_with_a_clear_reason(self):
        rows = [_row("a", score=800, name="Cheapest 3Br! FH! Within 1km"),
                _row("b", score=700, name="Parc Riviera")]
        db = {"a": _rec("a"), "b": _rec("b")}
        short, rej = weekly.select_candidates(rows, db)
        assert [c["id"] for c in short] == ["b"]     # 800 outranked, still cut
        assert "no usable project name" in rej[0]["gate_reason"]

    def test_known_projects_loader_survives_a_missing_cache(self, monkeypatch):
        monkeypatch.setattr(weekly, "DATA_DIR", "/nonexistent")
        assert weekly._known_projects() == set()


class TestRepricedSince:
    def test_first_entry_is_the_original_ask_not_a_change(self):
        rec = {"price_history": [{"date": "2026-07-28", "price": 1e6}]}
        assert not weekly._repriced_since(rec, "2026-07-20")

    def test_later_entry_in_window_counts(self):
        rec = {"price_history": [{"date": "2026-06-01", "price": 1.1e6},
                                 {"date": "2026-07-25", "price": 1e6}]}
        assert weekly._repriced_since(rec, "2026-07-20")

    def test_change_before_the_window_does_not(self):
        rec = {"price_history": [{"date": "2026-06-01", "price": 1.1e6},
                                 {"date": "2026-06-10", "price": 1e6}]}
        assert not weekly._repriced_since(rec, "2026-07-20")

    def test_missing_or_junk_history_is_safe(self):
        for rec in ({}, {"price_history": None}, {"price_history": "x"},
                    {"price_history": [{"date": None}, "junk"]}):
            assert not weekly._repriced_since(rec, "2026-07-20")


class TestCooldownIndex:
    def test_index_reads_recent_evals_and_ignores_old_ones(self, monkeypatch):
        now = datetime(2026, 7, 27)
        recent = (now - timedelta(days=5)).strftime("%Y-%m-%d")
        old = (now - timedelta(days=100)).strftime("%Y-%m-%d")
        monkeypatch.setattr(weekly.eval_memory, "load_index",
                            lambda: {"condos": {"fresh-condo": {}, "old-condo": {}}})
        monkeypatch.setattr(weekly.eval_memory, "load_condo", lambda slug: {
            "fresh-condo": {"history": [{"evaluated_at": recent, "as_of": {"beds": 2}}]},
            "old-condo": {"history": [{"evaluated_at": old, "as_of": {"beds": 2}}]},
        }[slug])
        idx = weekly._eval_cooldown_index(30, now)
        assert idx == {("fresh-condo", 2): recent}

    def test_entry_without_beds_becomes_a_wildcard(self, monkeypatch):
        now = datetime(2026, 7, 27)
        when = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        monkeypatch.setattr(weekly.eval_memory, "load_index",
                            lambda: {"condos": {"c": {}}})
        monkeypatch.setattr(weekly.eval_memory, "load_condo",
                            lambda slug: {"history": [{"evaluated_at": when, "as_of": {}}]})
        assert weekly._eval_cooldown_index(30, now) == {("c", "*"): when}

    def test_legacy_freetext_as_of_degrades_to_wildcard(self, monkeypatch):
        # Real eval memory holds entries whose `as_of` is a re-judgment note
        # rather than the {price, beds, …} dict — must not raise.
        now = datetime(2026, 7, 27)
        when = (now - timedelta(days=2)).strftime("%Y-%m-%d")
        monkeypatch.setattr(weekly.eval_memory, "load_index",
                            lambda: {"condos": {"c": {}}})
        monkeypatch.setattr(weekly.eval_memory, "load_condo", lambda slug: {
            "history": [{"evaluated_at": when, "as_of": "2026-06 re-judgment under v3.10b"}]})
        assert weekly._eval_cooldown_index(30, now) == {("c", "*"): when}

    def test_junk_history_entries_are_skipped(self, monkeypatch):
        now = datetime(2026, 7, 27)
        monkeypatch.setattr(weekly.eval_memory, "load_index",
                            lambda: {"condos": {"c": {}}})
        monkeypatch.setattr(weekly.eval_memory, "load_condo", lambda slug: {
            "history": ["a string", None, {"evaluated_at": None}]})
        assert weekly._eval_cooldown_index(30, now) == {}

    def test_broken_index_does_not_stop_the_scan(self, monkeypatch):
        def boom():
            raise OSError("index gone")
        monkeypatch.setattr(weekly.eval_memory, "load_index", boom)
        assert weekly._eval_cooldown_index(30, datetime(2026, 7, 27)) == {}


class TestAgentTimeout:
    """A stuck agent blocks every candidate behind it. On the first real OCR
    scan one ran 1h45m against a 30-minute timeout that never fired, so this
    is regression-tested rather than trusted."""

    def test_timeout_kills_a_hung_agent(self, monkeypatch, tmp_path):
        monkeypatch.setattr(weekly, "RUNS_DIR", str(tmp_path))
        monkeypatch.setattr(weekly, "_agent_prompt", lambda c: "x")
        # `sleep 60` stands in for a hung agent; a 1s deadline must end it.
        real_popen = weekly.subprocess.Popen          # capture BEFORE patching
        monkeypatch.setattr(weekly.subprocess, "Popen",
                            lambda cmd, **kw: real_popen(["sleep", "60"], **kw))
        started = time.time()
        r = weekly.run_agent({"id": "a", "slug": "a"}, timeout_s=1)
        elapsed = time.time() - started
        assert r["ok"] is False and "timed out" in r["error"]
        assert elapsed < 30, f"kill took {elapsed:.0f}s — group kill not working"

    def test_normal_exit_reports_returncode(self, monkeypatch, tmp_path):
        monkeypatch.setattr(weekly, "RUNS_DIR", str(tmp_path))
        monkeypatch.setattr(weekly, "_agent_prompt", lambda c: "x")
        real_popen = weekly.subprocess.Popen
        monkeypatch.setattr(weekly.subprocess, "Popen",
                            lambda cmd, **kw: real_popen(["true"], **kw))
        r = weekly.run_agent({"id": "a", "slug": "a"}, timeout_s=30)
        assert r["ok"] is True and r["returncode"] == 0

    def test_unlaunchable_command_is_reported_not_raised(self, monkeypatch, tmp_path):
        monkeypatch.setattr(weekly, "RUNS_DIR", str(tmp_path))
        monkeypatch.setattr(weekly, "_agent_prompt", lambda c: "x")
        monkeypatch.setattr(weekly.subprocess, "Popen",
                            lambda cmd, **kw: (_ for _ in ()).throw(
                                OSError("no such binary")))
        r = weekly.run_agent({"id": "a", "slug": "a"}, timeout_s=5)
        assert r["ok"] is False and "OSError" in r["error"]


class TestVerdictReadback:
    def _hist(self, url, rating="Buy", when="2026-07-27"):
        return {"evaluated_at": when, "rating": rating, "confidence": "high",
                "summary": "s", "source": {"url": url}}

    def test_matches_on_listing_url_not_just_the_condo(self, monkeypatch):
        monkeypatch.setattr(weekly.eval_memory, "load_condo", lambda slug: {"history": [
            self._hist("https://pg/other", rating="Avoid"),
            self._hist("https://pg/a", rating="Buy"),
        ]})
        v = weekly.read_verdict({"id": "a", "slug": "c", "url": "https://pg/a"},
                               on_or_after="2026-07-27")
        assert v["rating"] == "Buy"

    def test_falls_back_to_newest_entry_of_the_day(self, monkeypatch):
        monkeypatch.setattr(weekly.eval_memory, "load_condo", lambda slug: {
            "history": [self._hist(None, rating="Neutral")]})
        v = weekly.read_verdict({"id": "a", "slug": "c", "url": "https://pg/a"},
                               on_or_after="2026-07-27")
        assert v["rating"] == "Neutral"

    def test_ignores_evaluations_predating_this_run(self, monkeypatch):
        monkeypatch.setattr(weekly.eval_memory, "load_condo", lambda slug: {
            "history": [self._hist("https://pg/a", when="2026-07-20")]})
        assert weekly.read_verdict({"id": "a", "slug": "c", "url": "https://pg/a"},
                                  on_or_after="2026-07-27") is None

    def test_no_memory_file_returns_none(self, monkeypatch):
        monkeypatch.setattr(weekly.eval_memory, "load_condo", lambda slug: None)
        assert weekly.read_verdict({"id": "a", "slug": "c"}, "2026-07-27") is None


REAL_NOTE = """---
title: Daily finds
updated: 2026-07-01
---

# Daily finds

Some intro prose.

| Date | Project | Type | Price · District | Algo | AI verdict | Summary |
|---|---|---|---|---|---|---|

## What to do with a row

1. Sanity-check it yourself.
"""


class TestRowInsertion:
    def test_rows_land_in_the_table_not_after_the_trailing_prose(self):
        out = weekly._insert_rows(REAL_NOTE, ["| ROW |"])
        lines = out.split("\n")
        assert lines[lines.index("|---|---|---|---|---|---|---|") + 1] == "| ROW |"
        # the prose that follows the table must survive, still below it
        assert out.index("| ROW |") < out.index("## What to do with a row")
        assert "1. Sanity-check it yourself." in out

    def test_newest_row_goes_on_top(self):
        once = weekly._insert_rows(REAL_NOTE, ["| OLD |"])
        twice = weekly._insert_rows(once, ["| NEW |"])
        assert twice.index("| NEW |") < twice.index("| OLD |")

    def test_note_without_a_table_falls_back_to_appending(self):
        out = weekly._insert_rows("# Title\n\nprose\n", ["| ROW |"])
        assert out.endswith("| ROW |\n")

    def test_separator_alone_is_not_mistaken_for_a_table(self):
        # A horizontal rule or stray pipe line with no header row above it.
        out = weekly._insert_rows("# T\n\n|---|---|\n\nprose\n", ["| ROW |"])
        assert out.endswith("| ROW |\n")


class TestStorePush:
    def _note(self, tmp_path, body=REAL_NOTE):
        p = tmp_path / "daily-finds.md"
        p.write_text(body)
        return str(p)

    def _cand(self, rid="a"):
        return {"id": rid, "project_name": "Test Condo", "beds": 2, "sqft": 750,
                "price": 1_500_000, "psf": 2000.0, "district": "D15",
                "score_1000": 700, "url": f"https://pg/{rid}"}

    def test_only_buy_grade_at_decent_confidence_is_pushed(self, tmp_path):
        path = self._note(tmp_path)
        shortlist = [self._cand("a"), self._cand("b"), self._cand("c"), self._cand("d")]
        verdicts = {
            "a": {"rating": "Buy", "confidence": "high", "summary": "yes"},
            "b": {"rating": "Neutral", "confidence": "high", "summary": "meh"},
            "c": {"rating": "Buy", "confidence": "low", "summary": "unsure"},
            "d": {"rating": "Avoid", "confidence": "high", "summary": "no"},
        }
        added = weekly.push_to_store("2026-07-27", shortlist, verdicts, path=path)
        assert [x["url"] for x in added] == ["https://pg/a"]
        text = open(path).read()
        assert "https://pg/a" in text and "https://pg/b" not in text

    def test_strong_buy_counts_and_frontmatter_date_is_bumped(self, tmp_path):
        path = self._note(tmp_path)
        added = weekly.push_to_store(
            "2026-07-27", [self._cand("a")],
            {"a": {"rating": "Strong Buy", "confidence": "medium", "summary": "s"}},
            path=path)
        assert len(added) == 1
        assert "updated: 2026-07-27" in open(path).read()

    def test_rerunning_a_day_does_not_duplicate_rows(self, tmp_path):
        path = self._note(tmp_path)
        v = {"a": {"rating": "Buy", "confidence": "high", "summary": "s"}}
        weekly.push_to_store("2026-07-27", [self._cand("a")], v, path=path)
        again = weekly.push_to_store("2026-07-27", [self._cand("a")], v, path=path)
        assert again == []
        assert open(path).read().count("https://pg/a") == 1

    def test_missing_note_is_skipped_never_created(self, tmp_path):
        path = str(tmp_path / "nope" / "daily-finds.md")
        added = weekly.push_to_store(
            "2026-07-27", [self._cand("a")],
            {"a": {"rating": "Buy", "confidence": "high", "summary": "s"}}, path=path)
        assert added == [] and not os.path.exists(path)

    def test_pipes_and_newlines_cannot_break_the_table(self, tmp_path):
        path = self._note(tmp_path)
        weekly.push_to_store(
            "2026-07-27", [self._cand("a")],
            {"a": {"rating": "Buy", "confidence": "high",
                   "summary": "a | b\nc"}}, path=path)
        row = [l for l in open(path).read().splitlines() if "https://pg/a" in l]
        assert len(row) == 1 and "\n" not in row[0]
        assert row[0].count("|") == 9   # 8 columns -> 9 delimiters, no injected cell

    def test_realscore_lands_in_its_column(self, tmp_path):
        path = self._note(tmp_path)
        weekly.push_to_store(
            "2026-07-27", [self._cand("a")],
            {"a": {"rating": "Buy", "confidence": "high", "summary": "s",
                   "realscore": 4.6, "realsmart_pct_profitable": 100}}, path=path)
        row = [l for l in open(path).read().splitlines() if "https://pg/a" in l][0]
        assert "| 4.6 · 100% prof |" in row

    def test_missing_realscore_is_a_dash_not_a_blank_cell(self, tmp_path):
        path = self._note(tmp_path)
        weekly.push_to_store(
            "2026-07-27", [self._cand("a")],
            {"a": {"rating": "Buy", "confidence": "high", "summary": "s"}}, path=path)
        row = [l for l in open(path).read().splitlines() if "https://pg/a" in l][0]
        assert "| — |" in row and row.count("|") == 9


class TestRealsmart:
    def test_url_resolves_against_the_sitemap_index(self, monkeypatch):
        import realsmart
        idx = {"slugs": ["jadescape", "west-bay-condominium", "the-continuum"]}
        monkeypatch.setattr(realsmart, "load_slugs", lambda: idx)
        assert weekly.realsmart_url("JadeScape") == \
            ("https://realsmart.sg/p/jadescape", True)
        # the case guessing gets wrong: PG abbreviates, realsmart spells it out
        assert weekly.realsmart_url("West Bay Condo") == \
            ("https://realsmart.sg/p/west-bay-condominium", True)

    def test_unresolved_name_falls_back_and_says_so(self, monkeypatch):
        import realsmart
        monkeypatch.setattr(realsmart, "load_slugs", lambda: {"slugs": ["other"]})
        url, ok = weekly.realsmart_url("Totally New Launch")
        assert url == "https://realsmart.sg/p/totally-new-launch" and ok is False

    def test_prompt_requires_the_lookup_with_a_real_url(self):
        cand = {"id": "a", "project_name": "JadeScape", "score_1000": 700,
                "url": "https://pg/a", "ingest_flags": []}
        p = weekly._agent_prompt(cand)
        assert "realsmart.sg/p/jadescape" in p
        assert "REALSCORE" in p and "never guess a number" in p

    def test_prompt_flags_an_unresolved_url_as_a_guess(self, monkeypatch):
        import realsmart
        monkeypatch.setattr(realsmart, "load_slugs", lambda: {"slugs": []})
        cand = {"id": "a", "project_name": "Unknown Place", "score_1000": 700,
                "url": "https://pg/a", "ingest_flags": []}
        assert "GUESS" in weekly._agent_prompt(cand)

    def test_fields_read_from_either_shape(self):
        top = {"realscore": 4.6, "realsmart_pct_profitable": 100,
               "realsmart_annual_return_pct": 5.4}
        nested = {"agent_evaluation": top}
        assert weekly._realsmart(top) == weekly._realsmart(nested) == top

    def test_line_renders_what_is_present(self):
        assert weekly._realsmart_line({"realscore": 4.6}) == "realsmart: **REALSCORE 4.6**/5"
        assert "100% of resales profitable" in weekly._realsmart_line(
            {"realsmart_pct_profitable": 100})
        assert weekly._realsmart_line({}) == ""

    def test_annual_return_alone_is_not_enough_to_claim_a_score(self):
        # A return figure without a score or profitability split isn't the
        # signal the owner asked for — don't imply we looked it up.
        assert weekly._realsmart_line({"realsmart_annual_return_pct": 5.4}) == ""


class TestDigest:
    def _digest(self, **kw):
        base = dict(day="2026-07-27",
                    poll_state={"ok": True, "districts": [15], "beds": [2],
                                "scraped": 40, "added": 3, "price_changes": 1,
                                "enriched": 3, "duration_s": 60,
                                "new_listings": [{}, {}, {}], "changed_listings": [{}]},
                    shortlist=[], rejected=[], verdicts={}, min_score=650,
                    max_ai=5, dry_run=False, pushed=[])
        base.update(kw)
        return weekly.build_digest(**base)

    def test_reports_gate_and_counts(self):
        out = self._digest()
        assert "# Weekly scan — 2026-07-27 (2026-W31)" in out
        assert "score_1000 >= 650" in out
        assert "3 new + 1 price-changed" in out

    def test_failed_poll_is_loud(self):
        out = self._digest(poll_state={"ok": False, "error": "CloudflareBlockedError: x"})
        assert "Poll FAILED" in out and "CloudflareBlockedError" in out

    def test_rejected_listings_are_listed_with_reasons(self):
        rej = [{"id": "b", "project_name": "B", "beds": 2, "price": 1e6,
                "psf": 1500, "district": "D15", "score_1000": 500,
                "gate_reason": "below gate (500 < 650)"}]
        out = self._digest(rejected=rej)
        assert "Algo-only — 1 not sent to the agent" in out
        assert "below gate (500 < 650)" in out

    def test_verdict_block_renders_rating_and_rationale(self):
        cand = {"id": "a", "project_name": "A", "beds": 2, "sqft": 750,
                "price": 1_500_000, "psf": 2000, "district": "D15",
                "score_1000": 700, "url": "https://pg/a", "slug": "a"}
        v = {"rating": "Buy", "confidence": "high", "summary": "good one",
             "rating_rationale": "because", "red_flags": ["lease"], "catalysts": []}
        out = self._digest(shortlist=[cand], verdicts={"a": v})
        assert "🟢 Buy — A" in out and "good one" in out
        assert "**Why:** because" in out and "- lease" in out

    def test_shortlisted_run_that_saved_nothing_is_shown_not_hidden(self):
        cand = {"id": "a", "project_name": "A", "beds": 2, "price": 1e6,
                "psf": 1500, "district": "D15", "score_1000": 700, "slug": "a"}
        out = self._digest(shortlist=[cand], verdicts={"b": {"rating": "Buy"}})
        assert "No verdict saved" in out


class TestWeekGuard:
    def test_iso_week_key(self):
        # Mon 2026-07-27 .. Sun 2026-08-02 are one ISO week; the next day rolls.
        assert weekly.iso_week(datetime(2026, 7, 27)) == \
            weekly.iso_week(datetime(2026, 8, 2))
        assert weekly.iso_week(datetime(2026, 8, 3)) != \
            weekly.iso_week(datetime(2026, 8, 2))

    def test_successful_run_blocks_the_rest_of_the_week(self):
        state = {"last_run": {"week": "2026-W31", "poll_ok": True}}
        assert weekly.already_done_this_week(state, "2026-W31")

    def test_failed_poll_leaves_the_week_open_for_a_retry(self):
        state = {"last_run": {"week": "2026-W31", "poll_ok": False}}
        assert not weekly.already_done_this_week(state, "2026-W31")

    def test_new_week_always_runs(self):
        state = {"last_run": {"week": "2026-W30", "poll_ok": True}}
        assert not weekly.already_done_this_week(state, "2026-W31")

    def test_no_state_yet(self):
        assert not weekly.already_done_this_week({}, "2026-W31")


class TestScanNoteStamp:
    NOTE = ("---\ntitle: Weekly scan\nroutine: weekly\n"
            "last_run: 2026-07-01\nupdated: 2026-07-01\n---\n\n# Weekly scan\n")

    def test_stamps_last_run_and_updated(self, tmp_path):
        p = tmp_path / "weekly-scan.md"
        p.write_text(self.NOTE)
        assert weekly.stamp_scan_note("2026-07-27", path=str(p))
        text = p.read_text()
        assert "last_run: 2026-07-27" in text and "updated: 2026-07-27" in text
        assert "routine: weekly" in text          # untouched
        assert text.endswith("# Weekly scan\n")   # body untouched

    def test_missing_note_is_not_created(self, tmp_path):
        p = tmp_path / "nope" / "weekly-scan.md"
        assert weekly.stamp_scan_note("2026-07-27", path=str(p)) is False
        assert not p.exists()

    def test_absent_key_is_not_invented(self, tmp_path):
        p = tmp_path / "n.md"
        p.write_text("---\ntitle: t\n---\n\nbody\n")
        weekly.stamp_scan_note("2026-07-27", path=str(p))
        assert "last_run" not in p.read_text()

    def test_note_without_frontmatter_is_left_alone(self, tmp_path):
        p = tmp_path / "n.md"
        p.write_text("# plain\n")
        weekly.stamp_scan_note("2026-07-27", path=str(p))
        assert p.read_text() == "# plain\n"


class TestParallelAgents:
    def _fake(self, monkeypatch, verdict_for):
        import threading
        active, peak, lock = [0], [0], threading.Lock()
        order = []

        def fake_run(cand, timeout_s=None):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
                order.append(cand["id"])
            time.sleep(0.05)
            with lock:
                active[0] -= 1
            return {"id": cand["id"], "log": "x.log", "ok": True}

        monkeypatch.setattr(weekly, "run_agent", fake_run)
        monkeypatch.setattr(weekly, "read_verdict",
                            lambda cand, on_or_after: verdict_for(cand))
        return peak, order

    def test_runs_in_parallel_and_keeps_every_verdict(self, monkeypatch):
        peak, _ = self._fake(monkeypatch, lambda c: {"rating": "Neutral"})
        short = [{"id": str(i), "slug": f"condo-{i}"} for i in range(6)]
        verdicts, runs = weekly.run_agents(short, "2026-09-24", parallel=3)
        assert set(verdicts) == {str(i) for i in range(6)}
        assert peak[0] == 3
        assert [r["id"] for r in runs] == [str(i) for i in range(6)]

    def test_same_condo_candidates_never_overlap(self, monkeypatch):
        peak, order = self._fake(monkeypatch, lambda c: {"rating": "Avoid"})
        short = [{"id": "a3", "slug": "livia"}, {"id": "a4", "slug": "livia"}]
        weekly.run_agents(short, "2026-09-24", parallel=3)
        assert peak[0] == 1 and order == ["a3", "a4"]

    def test_missing_verdict_is_retried_once(self, monkeypatch):
        calls = {"n": 0}

        def verdict(c):
            calls["n"] += 1
            return None if calls["n"] == 1 else {"rating": "Neutral"}

        self._fake(monkeypatch, verdict)
        verdicts, runs = weekly.run_agents([{"id": "x", "slug": "x"}], "d", parallel=2)
        assert "x" in verdicts and len(runs) == 2 and runs[1]["retry"]


class TestRealsmartPrewarm:
    def test_attaches_cached_facts_and_prompt_skips_the_fetch(self, monkeypatch):
        import realsmart_cache
        monkeypatch.setattr(realsmart_cache, "warm", lambda names, **k: {
            "Livia": {"ok": True, "resolved": True, "realscore": 4.0, "pct_profitable": 92.2,
                      "annual_return_pct": 4.8, "resale_txns": 567,
                      "url": "https://realsmart.sg/p/livia", "fetched_at": time.time()},
            "Nowhere": {"ok": False}})
        short = [{"id": "1", "project_name": "Livia", "url": "u"},
                 {"id": "2", "project_name": "Nowhere", "url": "u"}]
        assert weekly.prewarm_realsmart(short) == {"ok": 1, "missing": 1}
        p = weekly._agent_prompt(short[0])
        assert "ALREADY FETCHED" in p and "REALSCORE 4.0" in p and "567" in p
        assert "uv run" not in p
        # A miss falls back to the agent fetching, with the right interpreter.
        monkeypatch.setattr(weekly, "realsmart_url", lambda n: ("https://r/p/x", True))
        assert "uv run --quiet --script" in weekly._agent_prompt(short[1])


class TestUraRefresh:
    @pytest.fixture(autouse=True)
    def no_api(self, monkeypatch):
        # These cover the browser fallback; never touch the real API.
        import ura_api
        monkeypatch.setattr(ura_api, "available", lambda: (False, "test"))

    def test_api_path_is_preferred_when_available(self, monkeypatch):
        import ura_api
        monkeypatch.setattr(ura_api, "available", lambda: (True, ""))
        monkeypatch.setattr(ura_api, "refresh", lambda **k: {"tx_rows": 1})
        monkeypatch.setattr(weekly.subprocess, "run",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError))
        out = weekly.refresh_ura([19])
        assert out["ok"] and out["via"] == "api"

    def test_fresh_csvs_skip_the_browser(self, monkeypatch, tmp_path):
        monkeypatch.setattr(weekly, "DATA_DIR", str(tmp_path))
        (tmp_path / "ura_district_D19.csv").write_text("x")
        monkeypatch.setattr(weekly.subprocess, "run",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError))
        out = weekly.refresh_ura([19], max_age_days=30)
        assert out["ok"] and out["stale"] == []

    def test_stale_csv_triggers_fetch_and_reports_failure(self, monkeypatch, tmp_path):
        monkeypatch.setattr(weekly, "DATA_DIR", str(tmp_path))
        f = tmp_path / "ura_district_D19.csv"
        f.write_text("x")
        old = time.time() - 90 * 86400
        os.utime(f, (old, old))
        called = {}

        class P:
            returncode = 1

        def fake_run(cmd, **k):
            called["cmd"] = cmd
            return P()

        monkeypatch.setattr(weekly.subprocess, "run", fake_run)
        out = weekly.refresh_ura([19], max_age_days=30)
        assert "19" in called["cmd"] and not out["ok"] and out["still_stale"] == [19]


class TestReviewFixes:
    def test_deep_rows_merge_without_duplicates(self):
        ps = {"new_listings": [{"id": "a"}], "changed_listings": [{"id": "b"}]}
        deep = {"new_listings": [{"id": "a"}, {"id": "c"}],
                "changed_listings": [{"id": "b"}, {"id": "d"}, {"id": "c"}]}
        weekly.merge_deep_rows(ps, deep)
        assert [r["id"] for r in ps["new_listings"]] == ["a", "c"]
        assert [r["id"] for r in ps["changed_listings"]] == ["b", "d"]

    def test_api_failure_is_reported_not_masked(self, monkeypatch):
        import ura_api
        monkeypatch.setattr(ura_api, "available", lambda: (True, ""))

        def boom(**k):
            raise RuntimeError("bed_bands failed")

        monkeypatch.setattr(ura_api, "refresh", boom)
        monkeypatch.setattr(weekly.subprocess, "run",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError))
        out = weekly.refresh_ura([19])
        assert out["ok"] is False and "bed_bands failed" in out["error"]

    def test_guarded_export_keeps_the_old_file_on_a_short_export(self, tmp_path):
        import ura_api
        path = tmp_path / "ura_district_D19.csv"
        path.write_text("h\n" + "r\n" * 100)

        def short(store, d, p):
            p.write_text("h\n" + "r\n" * 10)
            return 10

        skipped = []
        assert ura_api._export_guarded(short, None, 19, path, skipped) == 100
        assert path.read_text().count("\n") == 101 and skipped
        assert not (tmp_path / "ura_district_D19.csv.tmp").exists()

        def full(store, d, p):
            p.write_text("h\n" + "r\n" * 120)
            return 120

        assert ura_api._export_guarded(full, None, 19, path, []) == 120

    def test_unresolved_realsmart_record_is_left_to_the_agent(self, monkeypatch):
        import realsmart_cache
        monkeypatch.setattr(realsmart_cache, "warm", lambda names, **k: {
            "Guess": {"ok": True, "resolved": False, "realscore": 5.0}})
        short = [{"id": "1", "project_name": "Guess", "url": "u"}]
        assert weekly.prewarm_realsmart(short) == {"ok": 0, "missing": 1}
        assert "realsmart" not in short[0]

    def test_one_failing_lane_keeps_the_others_verdicts(self, monkeypatch):
        def fake(cand, day):
            if cand["id"] == "bad":
                raise RuntimeError("boom")
            return [{"id": cand["id"], "log": "x", "ok": True}], {"rating": "Neutral"}

        monkeypatch.setattr(weekly, "_analyze_one", fake)
        short = [{"id": "bad", "slug": "a"}, {"id": "good", "slug": "b"}]
        verdicts, runs = weekly.run_agents(short, "d", parallel=2)
        assert set(verdicts) == {"good"}
        assert any(r["id"] == "bad" and not r["ok"] for r in runs)

    def test_pre_gate_enrich_rescores_contenders_without_history(self, monkeypatch):
        calls = {}
        monkeypatch.setattr(weekly.poller, "_enrich_new_keys",
                            lambda keys, headless, cap: calls.setdefault("enrich", keys) and
                            {"attempted": len(keys), "enriched": len(keys)})

        def score(keys, record_history=True):
            calls["history"] = record_history
            return len(keys), [{"id": k, "score_1000": 900} for k in keys]

        monkeypatch.setattr(weekly.poller, "_score_keys", score)
        rows = [_row("a", score=700), _row("b", score=600, name="Other")]
        db = {"a": _rec("a"), "b": _rec("b")}
        out, rep = weekly.pre_gate_enrich(rows, db, {}, 650, set(), headless=True)
        assert calls["enrich"] == ["a"] and calls["history"] is False
        assert next(r for r in out if r["id"] == "a")["score_1000"] == 900
        assert next(r for r in out if r["id"] == "b")["score_1000"] == 600
        assert rep["rescored"] == 1

    def test_pre_gate_enrich_skips_already_visited_and_survives_errors(self, monkeypatch):
        monkeypatch.setattr(weekly.poller, "_enrich_new_keys",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("pg down")))
        rows = [_row("a", score=700)]
        db = {"a": _rec("a")}
        out, rep = weekly.pre_gate_enrich(rows, db, {}, 650, set(), headless=True)
        assert out == rows and "pg down" in rep["error"]
        db = {"a": _rec("a", latitude=1.3)}
        out, rep = weekly.pre_gate_enrich(rows, db, {}, 650, set(), headless=True)
        assert rep["attempted"] == 0


class TestDigestPreflight:
    def test_partial_api_refresh_reads_as_a_warning(self):
        line = weekly._ura_line({"ok": False, "via": "api", "refreshed": [16, 18],
                                 "still_stale": [19], "oldest_days": 0})
        assert line.startswith("⚠️ URA refresh incomplete via API") and "D19" in line
        assert "refreshed D16,18" in line

    def test_api_exception_names_the_error_and_age(self):
        line = weekly._ura_line({"ok": False, "via": "api", "error": "RuntimeError: x",
                                 "oldest_days": None})
        assert "RuntimeError: x" in line and "unknown age" in line and "None" not in line

    def test_clean_refresh(self):
        assert weekly._ura_line({"ok": True, "via": "api", "refreshed": [19]}) == \
            "URA prints refreshed via API D19"

    def test_api_skip_maps_back_to_district_numbers(self, monkeypatch):
        import ura_api
        monkeypatch.setattr(ura_api, "available", lambda: (True, ""))
        monkeypatch.setattr(ura_api, "refresh", lambda **k: {
            "skipped_districts": ["ura_district_D19.csv: 0 rows vs 9724 before — kept the old file"]})
        out = weekly.refresh_ura([16, 19])
        assert out["ok"] is False and out["still_stale"] == [19] and out["refreshed"] == [16]


class TestLensAwareEnrichment:
    def test_exit_demand_only_contender_outranks_lower_ranked_mmr(self, monkeypatch):
        picked = {}
        monkeypatch.setattr(weekly.poller, "_enrich_new_keys",
                            lambda keys, headless, cap: picked.setdefault("k", keys) and
                            {"attempted": len(keys), "enriched": 0})
        monkeypatch.setattr(weekly.poller, "_score_keys", lambda keys, record_history=True: (0, []))
        monkeypatch.setattr(weekly, "select_candidates", lambda *a, **k: ([
            {"id": "m1", "score_1000": 900, "surfaced_by": ["mmr"], "lens_view": {"mmr": 900}},
            {"id": "m2", "score_1000": 800, "surfaced_by": ["mmr"], "lens_view": {"mmr": 800}},
            {"id": "e1", "score_1000": 500, "surfaced_by": ["exit_demand"],
             "lens_view": {"mmr": 500, "exit_demand": 90}},
        ], []))
        weekly.pre_gate_enrich([], {}, {}, 650, set(), headless=True, limit=2)
        # m1 is mmr's #1 and e1 is exit_demand's #1: both beat mmr's #2.
        assert set(picked["k"]) == {"m1", "e1"}


class TestRound3Fixes:
    def test_accept_shrink_takes_a_smaller_export(self, tmp_path):
        import ura_api
        path = tmp_path / "ura_district_D19.csv"
        path.write_text("h\n" + "r\n" * 100)

        def short(store, d, p):
            p.write_text("h\n" + "r\n" * 50)
            return 50

        assert ura_api._export_guarded(short, None, 19, path, [], accept_shrink=True) == 50

    def test_failed_export_leaves_no_temp_file(self, tmp_path):
        import ura_api
        path = tmp_path / "ura_district_D19.csv"

        def boom(store, d, p):
            p.write_text("partial")
            raise RuntimeError("x")

        with pytest.raises(RuntimeError):
            ura_api._export_guarded(boom, None, 19, path, [])
        assert not (tmp_path / "ura_district_D19.csv.tmp").exists()

    def test_ec_request_takes_the_api_path_with_ec_export(self, monkeypatch):
        # Since the EC export landed (ura_api.refresh(ec=True) writes
        # ura_district_D*_EC.csv), an EC request no longer needs the browser.
        import sys as _sys
        import fetch_ura_districts as f
        import ura_api
        seen = {}
        monkeypatch.setattr(ura_api, "available", lambda: (True, ""))
        monkeypatch.setattr(ura_api, "refresh", lambda **k: seen.update(k) or {"ec_tx_rows": 1})
        monkeypatch.setattr(f, "build_cache_from_district_csvs",
                            lambda d: (_ for _ in ()).throw(AssertionError("browser path used")))
        monkeypatch.setattr(_sys, "argv", ["x", "--districts", "19", "--property-types", "ec"])
        f.main()
        assert seen.get("ec") is True and seen.get("rentals") is False

    def test_condo_request_skips_ec_export(self, monkeypatch):
        import sys as _sys
        import fetch_ura_districts as f
        import ura_api
        seen = {}
        monkeypatch.setattr(ura_api, "available", lambda: (True, ""))
        monkeypatch.setattr(ura_api, "refresh", lambda **k: seen.update(k) or {})
        monkeypatch.setattr(_sys, "argv", ["x", "--districts", "19"])
        f.main()
        assert seen.get("ec") is False


class TestCheckEnvironment:
    def _quiet(self, monkeypatch, tmp_path):
        import realsmart
        slug = tmp_path / "slugs.json"
        slug.write_text("{}")
        monkeypatch.setattr(realsmart, "SLUG_FILE", str(slug))
        monkeypatch.setattr(weekly, "STORE_FINDS_PATH", str(slug))

    def test_missing_claude_is_fatal_only_when_agents_run(self, monkeypatch, tmp_path):
        self._quiet(monkeypatch, tmp_path)
        monkeypatch.setattr(weekly.shutil, "which", lambda _: None)
        assert any("claude" in p for p in weekly.check_environment(False, True))
        assert weekly.check_environment(False, False) == []

    def test_missing_slug_index_is_rebuilt(self, monkeypatch, tmp_path):
        import realsmart
        self._quiet(monkeypatch, tmp_path)
        monkeypatch.setattr(realsmart, "SLUG_FILE", str(tmp_path / "none.json"))
        calls = []
        monkeypatch.setattr(realsmart, "refresh_slugs", lambda: calls.append(1) or 5)
        assert weekly.check_environment(False, False) == []
        assert calls == [1]
