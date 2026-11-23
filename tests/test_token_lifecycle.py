"""Token refresh across runs, against a token endpoint that rotates refresh tokens.

WHOOP revokes a refresh token the moment it is used. These tests pin down the
consequences for both entry points: the rotated token must be persisted before
the run continues, and a replayed (already-used) token must fail loudly.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from whoop_exporter import auth, cli
from whoop_exporter.client import WhoopClient
from whoop_exporter.config import Settings
from whoop_exporter.gcp import get_access_token as gcp_get_access_token

from .whoop_api import FakeWhoopApi

SETTINGS = Settings(whoop_client_id="cid", whoop_client_secret="cs", gdrive_folder_id="f")
NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


class VersionedStore:
    """In-memory stand-in for the Secret Manager store, keeping every version."""

    def __init__(self, initial: str, *, fail_on_save: bool = False) -> None:
        self.versions = [initial]
        self.fail_on_save = fail_on_save

    def load(self) -> str:
        return self.versions[-1]

    def save(self, refresh_token: str) -> None:
        if self.fail_on_save:
            raise RuntimeError("Secret Manager unavailable")
        self.versions.append(refresh_token)


@pytest.fixture
def api() -> FakeWhoopApi:
    return FakeWhoopApi()


# ------------------------------------------------------------ Cloud Function


def test_consecutive_runs_follow_the_rotation_chain(api):
    store = VersionedStore(api.refresh_token)
    first = gcp_get_access_token(SETTINGS, store, session=api)
    second = gcp_get_access_token(SETTINGS, store, session=api)

    assert (first, second) == ("access-1", "access-2")
    assert store.versions == ["refresh-0", "refresh-1", "refresh-2"]
    sent = [r.params["refresh_token"] for r in api.requests if r.method == "POST"]
    assert sent == ["refresh-0", "refresh-1"]


def test_replaying_a_used_refresh_token_is_rejected(api):
    stale = VersionedStore(api.refresh_token)
    gcp_get_access_token(SETTINGS, VersionedStore(api.refresh_token), session=api)

    with pytest.raises(auth.TokenError, match="400"):
        gcp_get_access_token(SETTINGS, stale, session=api)


def test_failed_persist_fails_the_run_before_any_data_is_fetched(api):
    # The old token is already revoked, so continuing would strand the
    # deployment. Failing loudly makes Cloud Scheduler retry and alerts fire.
    store = VersionedStore(api.refresh_token, fail_on_save=True)
    with pytest.raises(RuntimeError, match="Secret Manager"):
        gcp_get_access_token(SETTINGS, store, session=api)
    assert all(r.method == "POST" for r in api.requests)


def test_refreshed_access_token_authorizes_data_requests(api):
    token = gcp_get_access_token(SETTINGS, VersionedStore(api.refresh_token), session=api)
    export = WhoopClient(token, session=api).fetch_export(2, now=lambda: NOW)
    assert export["cycles"]


def test_previous_access_token_stops_working_after_refresh(api):
    old_access = api.access_token
    gcp_get_access_token(SETTINGS, VersionedStore(api.refresh_token), session=api)
    with pytest.raises(requests.HTTPError, match="401"):
        WhoopClient(old_access, session=api).get("/v1/user/profile/basic")


# ---------------------------------------------------------------------- CLI


@pytest.fixture
def cli_api(api, monkeypatch):
    """Route the CLI's default ``requests.Session()`` to the fake API."""
    monkeypatch.setattr(auth.requests, "Session", lambda: api)
    monkeypatch.setattr(cli, "_browser_authorize", pytest.fail)
    return api


def test_cli_refreshes_expired_token_then_reuses_it_while_fresh(cli_api, tmp_path):
    token_file = tmp_path / "tok.json"
    expired = {"access_token": "old", "refresh_token": "refresh-0", "obtained_at": 0}
    token_file.write_text(json.dumps(expired))

    assert cli.get_access_token(SETTINGS, token_file) == "access-1"
    saved = json.loads(token_file.read_text())
    assert saved["refresh_token"] == "refresh-1"
    assert saved["obtained_at"] == pytest.approx(time.time(), abs=60)

    # Second run within the hour: no network call to the token endpoint.
    posts_before = sum(r.method == "POST" for r in cli_api.requests)
    assert cli.get_access_token(SETTINGS, token_file) == "access-1"
    assert sum(r.method == "POST" for r in cli_api.requests) == posts_before


def test_missing_expires_in_defaults_to_one_hour():
    token = {"access_token": "a", "obtained_at": 1000.0}
    assert auth.is_access_token_fresh(token, now=1000.0 + 3600 - auth.EXPIRY_SKEW_SECONDS - 1)
    assert not auth.is_access_token_fresh(token, now=1000.0 + 3600 - auth.EXPIRY_SKEW_SECONDS)


# ------------------------------------------------- CLI browser OAuth callback


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


def _authorize_with_redirect(monkeypatch, make_query):
    """Run ``_browser_authorize`` with a fake browser that hits the callback.

    ``make_query(state)`` returns the query string WHOOP would redirect with.
    Returns ``(result_or_exception, (status, body), auth_url)``.
    """
    port = _free_port()
    monkeypatch.setattr(cli, "REDIRECT_PORT", port)
    monkeypatch.setattr(cli, "REDIRECT_URI", f"http://localhost:{port}/callback")
    seen: dict = {}

    def fake_browser(url: str) -> bool:
        seen["url"] = url
        state = parse_qs(urlparse(url).query)["state"][0]
        callback = f"http://localhost:{port}/callback?{make_query(state)}"

        def hit() -> None:
            for _ in range(50):  # wait for the server to start listening
                try:
                    with urllib.request.urlopen(callback, timeout=5) as resp:
                        seen["response"] = (resp.status, resp.read())
                    return
                except urllib.error.HTTPError as err:
                    seen["response"] = (err.code, err.read())
                    return
                except urllib.error.URLError:
                    time.sleep(0.05)

        seen["thread"] = threading.Thread(target=hit, daemon=True)
        seen["thread"].start()
        return True

    monkeypatch.setattr(cli.webbrowser, "open", fake_browser)
    try:
        outcome = cli._browser_authorize(SETTINGS)
    except auth.TokenError as exc:
        outcome = exc
    seen["thread"].join(timeout=10)
    return outcome, seen["response"], seen["url"]


def test_browser_flow_returns_code_and_sends_expected_auth_request(monkeypatch, capsys):
    code, response, url = _authorize_with_redirect(monkeypatch, lambda s: f"code=c-123&state={s}")

    assert code == "c-123"
    assert response[0] == 200
    query = parse_qs(urlparse(url).query)
    assert query["client_id"] == ["cid"]
    assert query["response_type"] == ["code"]
    assert query["scope"] == [auth.SCOPES]
    assert len(query["state"][0]) >= 32
    assert "Opening WHOOP login" in capsys.readouterr().out


def test_browser_flow_rejects_state_mismatch(monkeypatch):
    outcome, response, _ = _authorize_with_redirect(
        monkeypatch, lambda _s: "code=c-123&state=forged"
    )
    assert isinstance(outcome, auth.TokenError)
    assert "state mismatch" in str(outcome)
    assert response[0] == 400


def test_browser_flow_surfaces_provider_error(monkeypatch):
    outcome, _, _ = _authorize_with_redirect(
        monkeypatch, lambda s: f"error=access_denied&state={s}"
    )
    assert isinstance(outcome, auth.TokenError)
    assert "access_denied" in str(outcome)
