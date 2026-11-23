from datetime import UTC, datetime, timedelta, timezone

import pytest

from whoop_exporter import client as client_mod
from whoop_exporter.client import WhoopClient, whoop_timestamp

from .fakes import FakeResponse, FakeSession

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def paged(pages):
    """Route handler returning successive pages and checking nextToken."""
    calls = iter(range(len(pages)))

    def handler(_url, params):
        i = next(calls)
        if i > 0:
            assert params["nextToken"] == f"t{i}"
        return FakeResponse(pages[i])

    return handler


def test_whoop_timestamp_is_utc_with_z_suffix():
    ist = timezone(timedelta(hours=5, minutes=30))
    assert whoop_timestamp(datetime(2026, 1, 1, 5, 30, tzinfo=ist)) == "2026-01-01T00:00:00.000Z"
    assert whoop_timestamp(datetime(2026, 1, 1)) == "2026-01-01T00:00:00.000Z"


def test_sends_bearer_token_and_timeout():
    session = FakeSession({"/v1/user/profile/basic": FakeResponse({"first_name": "Test"})})
    WhoopClient("tok", session=session, timeout=5).get("/v1/user/profile/basic")
    _, url, kwargs = session.calls[0]
    assert url == "https://api.prod.whoop.com/developer/v1/user/profile/basic"
    assert kwargs["headers"] == {"Authorization": "Bearer tok"}
    assert kwargs["timeout"] == 5


def test_paginate_follows_next_token():
    pages = [
        {"records": [{"id": 1}], "next_token": "t1"},
        {"records": [{"id": 2}], "next_token": "t2"},
        {"records": [{"id": 3}]},
    ]
    session = FakeSession({"/v1/cycle": paged(pages)})
    records = WhoopClient("tok", session=session).paginate(
        "/v1/cycle", NOW - timedelta(days=1), NOW
    )
    assert [r["id"] for r in records] == [1, 2, 3]
    first_params = session.calls[0][2]["params"]
    assert first_params["start"] == "2026-01-14T12:00:00.000Z"
    assert first_params["limit"] == client_mod.PAGE_LIMIT


def test_paginate_stops_on_empty_page_even_with_token():
    session = FakeSession({"/x": FakeResponse({"records": [], "next_token": "t1"})})
    assert WhoopClient("tok", session=session).paginate("/x", NOW, NOW) == []
    assert len(session.calls) == 1


def test_paginate_guards_against_infinite_loops(monkeypatch):
    monkeypatch.setattr(client_mod, "MAX_PAGES", 3)
    session = FakeSession({"/x": FakeResponse({"records": [{}], "next_token": "same"})})
    with pytest.raises(RuntimeError, match="exceeded 3 pages"):
        WhoopClient("tok", session=session).paginate("/x", NOW, NOW)


def test_recovery_skips_cycles_without_recovery():
    session = FakeSession(
        {
            "/v2/cycle/1/recovery": FakeResponse({"score": {"recovery_score": 70}}),
            "/v2/cycle/2/recovery": FakeResponse(status_code=404),
        }
    )
    cycles = [{"id": 1, "start": "2026-01-14T06:00:00Z"}, {"id": 2}, {"no_id": True}]
    recovery = WhoopClient("tok", session=session).fetch_recovery_for_cycles(cycles)
    assert recovery == [
        {"score": {"recovery_score": 70}, "cycle_id": 1, "cycle_start": "2026-01-14T06:00:00Z"}
    ]


def test_fetch_export_assembles_document_and_tolerates_optional_failures():
    session = FakeSession(
        {
            "/v1/user/profile/basic": FakeResponse(status_code=500),
            "/v1/user/measurement/body": FakeResponse({"height_meter": 1.8}),
            "/v1/cycle": FakeResponse(
                {"records": [{"id": 7, "start": "2026-01-14T06:00:00Z", "score": {"strain": 10}}]}
            ),
            "/v2/cycle/7/recovery": FakeResponse({"score": {"recovery_score": 55}}),
            "/v2/activity/sleep": FakeResponse({"records": []}),
            "/v2/activity/workout": FakeResponse({"records": []}),
        }
    )
    export = WhoopClient("tok", session=session).fetch_export(3, now=lambda: NOW)

    assert export["profile"] == {}
    assert export["body_measurements"] == {"height_meter": 1.8}
    assert export["period"] == {
        "start": (NOW - timedelta(days=3)).isoformat(),
        "end": NOW.isoformat(),
        "days": 3,
    }
    assert export["summary"]["2026-01-14"]["recovery_score"] == 55
    assert export["summary"]["2026-01-14"]["strain"] == 10


def test_required_endpoint_failure_propagates():
    session = FakeSession({"/v1/cycle": FakeResponse(status_code=401)})
    with pytest.raises(Exception, match="401"):
        WhoopClient("tok", session=session).fetch_export(1, now=lambda: NOW)
