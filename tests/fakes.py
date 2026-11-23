"""Hand-rolled test doubles for HTTP and Google API clients (synthetic data only)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import requests


class FakeResponse:
    def __init__(self, payload: Any = None, status_code: int = 200) -> None:
        self._payload = payload if payload is not None else {}
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


Route = Callable[[str, dict[str, Any] | None], FakeResponse]


class FakeSession:
    """Routes requests by URL suffix and records every call."""

    def __init__(self, routes: dict[str, Route | FakeResponse] | None = None) -> None:
        self.routes = routes or {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def _dispatch(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        for suffix, handler in self.routes.items():
            if url.endswith(suffix):
                if isinstance(handler, FakeResponse):
                    return handler
                return handler(url, kwargs.get("params") or kwargs.get("data"))
        return FakeResponse(status_code=404)

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._dispatch("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._dispatch("POST", url, **kwargs)


class _Exec:
    def __init__(self, result: Any) -> None:
        self._result = result

    def execute(self) -> Any:
        return self._result


class FakeDriveFiles:
    def __init__(self, existing: dict[str, str] | None = None) -> None:
        self.existing = existing or {}  # filename -> id
        self.list_queries: list[str] = []
        self.created: list[dict[str, Any]] = []
        self.updated: list[str] = []

    def list(self, q: str, **kwargs: Any) -> _Exec:
        self.list_queries.append(q)
        assert kwargs.get("supportsAllDrives") is True
        files = [{"id": fid} for name, fid in self.existing.items() if f"name='{name}'" in q]
        return _Exec({"files": files})

    def create(self, body: dict[str, Any], **kwargs: Any) -> _Exec:
        assert kwargs.get("supportsAllDrives") is True
        self.created.append(body)
        return _Exec({"id": "new-file-id"})

    def update(self, fileId: str, **kwargs: Any) -> _Exec:  # noqa: N803 (Drive API name)
        assert kwargs.get("supportsAllDrives") is True
        self.updated.append(fileId)
        return _Exec({"id": fileId})


class FakeDriveService:
    def __init__(self, existing: dict[str, str] | None = None) -> None:
        self._files = FakeDriveFiles(existing)

    def files(self) -> FakeDriveFiles:
        return self._files
