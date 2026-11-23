"""A fixture-backed fake of the WHOOP developer API.

Unlike the per-test route tables in :mod:`tests.fakes`, this fake behaves like
the real service for the parts the exporter depends on, so tests exercise the
client's actual request/response contract rather than hand-picked replies:

* **Collections** (``/v1/cycle``, ``/v2/activity/sleep``, ``/v2/activity/workout``)
  are filtered by the ``start``/``end`` query window, sorted newest-first, and
  paged by ``limit`` with an opaque ``nextToken`` — the same semantics as WHOOP.
* **Recovery** is served per cycle and answers 404 for cycles that have none
  yet (the current, still-open cycle).
* **Auth**: every data request must carry ``Bearer <current access token>``;
  stale tokens get 401.
* **Token endpoint** implements refresh-token *rotation*: each refresh mints a
  new access/refresh pair and revokes the refresh token that was used, so
  replaying it returns ``400 invalid_grant``.

All fixture data in ``tests/fixtures/whoop`` is synthetic. See the README there.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .fakes import FakeResponse

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "whoop"
API_PREFIX = "/developer"
TOKEN_PATH = "/oauth/oauth2/token"
MAX_LIMIT = 25

COLLECTIONS = {
    "/v1/cycle": "cycles.json",
    "/v2/activity/sleep": "sleep.json",
    "/v2/activity/workout": "workouts.json",
}
SINGLETONS = {
    "/v1/user/profile/basic": "profile.json",
    "/v1/user/measurement/body": "body_measurement.json",
}
RECOVERY_PREFIX = "/v2/cycle/"
RECOVERY_SUFFIX = "/recovery"


def load_fixture(name: str) -> Any:
    """Load a JSON fixture by file name (a fresh copy each call)."""
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass
class Request:
    method: str
    path: str
    params: dict[str, Any]
    headers: dict[str, str]


@dataclass
class FakeWhoopApi:
    """Drop-in replacement for :class:`requests.Session` (``get``/``post`` only)."""

    access_token: str = "access-0"
    refresh_token: str = "refresh-0"
    expires_in: int = 3600
    requests: list[Request] = field(default_factory=list)
    # Status codes to return instead of data, keyed by (path, page index).
    failures: dict[tuple[str, int], int] = field(default_factory=dict)
    _revoked: set[str] = field(default_factory=set)
    _generation: int = 0

    # ------------------------------------------------------------ inspection

    def pages_served(self, path: str) -> list[Request]:
        return [r for r in self.requests if r.method == "GET" and r.path == path]

    # ------------------------------------------------------------- transport

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        path = self._path(url)
        request = Request(
            "GET", path, dict(kwargs.get("params") or {}), dict(kwargs.get("headers") or {})
        )
        self.requests.append(request)

        if request.headers.get("Authorization") != f"Bearer {self.access_token}":
            return FakeResponse({"error": "unauthorized"}, 401)
        if path in COLLECTIONS:
            return self._collection(path, request.params)
        if path in SINGLETONS:
            return FakeResponse(load_fixture(SINGLETONS[path]))
        if path.startswith(RECOVERY_PREFIX) and path.endswith(RECOVERY_SUFFIX):
            cycle_id = path[len(RECOVERY_PREFIX) : -len(RECOVERY_SUFFIX)]
            recovery = load_fixture("recovery_by_cycle.json").get(cycle_id)
            return FakeResponse(recovery) if recovery else FakeResponse(status_code=404)
        return FakeResponse(status_code=404)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        path = urlparse(url).path
        data = dict(kwargs.get("data") or {})
        self.requests.append(Request("POST", path, data, {}))
        if path != TOKEN_PATH or data.get("grant_type") != "refresh_token":
            return FakeResponse({"error": "unsupported_grant_type"}, 400)

        presented = data.get("refresh_token")
        if presented in self._revoked or presented != self.refresh_token:
            return FakeResponse({"error": "invalid_grant"}, 400)

        # Rotate: the presented refresh token is now dead.
        self._revoked.add(presented)
        self._generation += 1
        self.access_token = f"access-{self._generation}"
        self.refresh_token = f"refresh-{self._generation}"
        body = load_fixture("token_response.json")
        body.update(
            access_token=self.access_token,
            refresh_token=self.refresh_token,
            expires_in=self.expires_in,
        )
        return FakeResponse(body)

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _path(url: str) -> str:
        path = urlparse(url).path
        return path[len(API_PREFIX) :] if path.startswith(API_PREFIX) else path

    def _collection(self, path: str, params: dict[str, Any]) -> FakeResponse:
        limit = int(params.get("limit", 10))
        if not 1 <= limit <= MAX_LIMIT:
            return FakeResponse({"error": f"limit must be 1..{MAX_LIMIT}"}, 400)

        offset = 0
        if token := params.get("nextToken"):
            if not str(token).startswith("page@"):
                return FakeResponse({"error": "bad nextToken"}, 400)
            offset = int(str(token).removeprefix("page@"))

        page_index = offset // limit
        if status := self.failures.get((path, page_index)):
            return FakeResponse({"error": "injected"}, status)

        start = _parse_ts(params["start"]) if "start" in params else None
        end = _parse_ts(params["end"]) if "end" in params else None
        records = [
            r
            for r in load_fixture(COLLECTIONS[path])["records"]
            if (start is None or _parse_ts(r["start"]) >= start)
            and (end is None or _parse_ts(r["start"]) < end)
        ]
        records.sort(key=lambda r: r["start"], reverse=True)

        page = records[offset : offset + limit]
        body: dict[str, Any] = {"records": page}
        if offset + limit < len(records):
            body["next_token"] = f"page@{offset + limit}"
        return FakeResponse(body)
