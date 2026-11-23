"""End-to-end export against the fixture-backed fake WHOOP API.

These tests run the real :class:`WhoopClient` (pagination, recovery fan-out,
export assembly and daily aggregation) against :class:`FakeWhoopApi`, and pin
the resulting daily summary to a golden file.

Regenerate the golden file after an intentional change to the summary shape::

    UPDATE_GOLDEN=1 pytest tests/test_pipeline_fixtures.py
"""

from __future__ import annotations

import json
import math
import os
from datetime import UTC, datetime

import pytest
import requests

from whoop_exporter import client as client_mod
from whoop_exporter.client import (
    CYCLE_ENDPOINT,
    SLEEP_ENDPOINT,
    WORKOUT_ENDPOINT,
    WhoopClient,
)

from .whoop_api import FIXTURE_DIR, FakeWhoopApi, load_fixture

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
DAYS_BACK = 5  # window: 2026-01-10T12:00Z .. 2026-01-15T12:00Z
GOLDEN = FIXTURE_DIR / "expected_summary.json"


@pytest.fixture
def api() -> FakeWhoopApi:
    return FakeWhoopApi()


@pytest.fixture
def small_pages(monkeypatch):
    """Shrink the page size so a handful of fixture records spans several pages."""
    monkeypatch.setattr(client_mod, "PAGE_LIMIT", 2)
    return 2


def run_export(api: FakeWhoopApi) -> dict:
    return WhoopClient(api.access_token, session=api).fetch_export(DAYS_BACK, now=lambda: NOW)


# ------------------------------------------------------------------- golden


def test_daily_summary_matches_golden_file(api, small_pages):
    summary = run_export(api)["summary"]

    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        pytest.skip("golden file regenerated")
    assert summary == json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_page_size_does_not_change_the_result(api, monkeypatch):
    baseline = run_export(api)
    for limit in (1, 3, client_mod.PAGE_LIMIT):
        monkeypatch.setattr(client_mod, "PAGE_LIMIT", limit)
        assert run_export(FakeWhoopApi()) == baseline


# --------------------------------------------------------------- pagination


@pytest.mark.parametrize(
    ("endpoint", "in_window"),
    [(CYCLE_ENDPOINT, 5), (SLEEP_ENDPOINT, 6), (WORKOUT_ENDPOINT, 4)],
)
def test_every_page_is_requested_exactly_once(api, small_pages, endpoint, in_window):
    export = run_export(api)
    key = {CYCLE_ENDPOINT: "cycles", SLEEP_ENDPOINT: "sleep", WORKOUT_ENDPOINT: "workouts"}
    assert len(export[key[endpoint]]) == in_window

    pages = api.pages_served(endpoint)
    assert len(pages) == math.ceil(in_window / small_pages)
    # The window and page size are constant; only the cursor advances.
    assert {(p.params["start"], p.params["end"], p.params["limit"]) for p in pages} == {
        ("2026-01-10T12:00:00.000Z", "2026-01-15T12:00:00.000Z", small_pages)
    }
    assert [p.params.get("nextToken") for p in pages] == [None] + [
        f"page@{i * small_pages}" for i in range(1, len(pages))
    ]


def test_records_outside_the_window_are_not_exported(api):
    export = run_export(api)
    cycle_ids = {c["id"] for c in export["cycles"]}
    assert 1001 not in cycle_ids  # starts 2026-01-10T06:10Z, before the window
    assert cycle_ids == {1002, 1003, 1004, 1005, 1006}


def test_records_keep_api_order_and_are_not_duplicated_across_pages(api, small_pages):
    sleep = run_export(api)["sleep"]
    starts = [s["start"] for s in sleep]
    assert starts == sorted(starts, reverse=True)
    assert len({s["id"] for s in sleep}) == len(sleep)


def test_failure_on_a_later_page_aborts_instead_of_exporting_partial_data(api, small_pages):
    api.failures[(SLEEP_ENDPOINT, 2)] = 503
    with pytest.raises(requests.HTTPError, match="503"):
        run_export(api)
    assert len(api.pages_served(SLEEP_ENDPOINT)) == 3


def test_expired_access_token_surfaces_as_401(api):
    stale = WhoopClient("access-from-yesterday", session=api)
    with pytest.raises(requests.HTTPError, match="401"):
        stale.fetch_export(DAYS_BACK, now=lambda: NOW)


# ------------------------------------------------------------- aggregation


def test_recovery_is_fetched_per_cycle_and_open_cycle_is_skipped(api):
    export = run_export(api)
    assert [r["cycle_id"] for r in export["recovery"]] == [1005, 1004, 1003, 1002]
    assert api.pages_served("/v2/cycle/1006/recovery")  # asked, got 404, skipped


def test_summary_values_trace_back_to_fixture_records(api):
    summary = run_export(api)["summary"]

    cycles = {c["id"]: c for c in load_fixture("cycles.json")["records"]}
    recovery = load_fixture("recovery_by_cycle.json")
    day = summary["2026-01-11"]
    assert day["strain"] == cycles[1002]["score"]["strain"]
    assert day["recovery_score"] == recovery["1002"]["score"]["recovery_score"]
    assert day["hrv_rmssd_milli"] == recovery["1002"]["score"]["hrv_rmssd_milli"]
    assert [w["sport_name"] for w in day["workouts"]] == ["functional-fitness", "running"]


def test_unscorable_and_pending_records_yield_nulls_not_errors(api):
    summary = run_export(api)["summary"]
    assert summary["2026-01-13"]["recovery_score"] is None  # recovery UNSCORABLE
    assert summary["2026-01-15"]["strain"] is None  # current cycle PENDING_SCORE
    (walk,) = summary["2026-01-15"]["workouts"]
    assert walk["strain"] is None
    assert walk["zone_durations"] == {}


def test_nap_does_not_overwrite_the_nights_sleep(api):
    day = run_export(api)["summary"]["2026-01-12"]
    # 2026-01-12 has a 22:30 main sleep and a 13:30 nap; the API lists the
    # newer record first, so a naive "last one wins" would report the nap.
    assert day["sleep_performance_percentage"] == 95
    assert day["total_sleep_time_milli"] == 28_200_000
    assert day["nap_count"] == 1
    assert day["nap_time_milli"] == 2_100_000
