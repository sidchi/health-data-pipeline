"""Thin WHOOP developer API client.

Only the endpoints the exporter needs are wrapped. The HTTP session is
injectable so tests can substitute a fake without patching ``requests``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import requests

from .summary import build_daily_summary

logger = logging.getLogger(__name__)

API_BASE = "https://api.prod.whoop.com/developer"
PAGE_LIMIT = 25  # WHOOP's maximum page size for collection endpoints.
DEFAULT_TIMEOUT_SECONDS = 30
# Hard stop so a misbehaving next_token can never loop forever.
MAX_PAGES = 500

PROFILE_ENDPOINT = "/v1/user/profile/basic"
BODY_ENDPOINT = "/v1/user/measurement/body"
CYCLE_ENDPOINT = "/v1/cycle"
SLEEP_ENDPOINT = "/v2/activity/sleep"
WORKOUT_ENDPOINT = "/v2/activity/workout"


def recovery_endpoint(cycle_id: Any) -> str:
    return f"/v2/cycle/{cycle_id}/recovery"


def whoop_timestamp(dt: datetime) -> str:
    """Format a datetime as the UTC ISO-8601 string WHOOP expects (``...Z``)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class WhoopClient:
    """Authenticated client for the WHOOP developer API."""

    def __init__(
        self,
        access_token: str,
        *,
        session: requests.Session | None = None,
        base_url: str = API_BASE,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._session = session or requests.Session()
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self._session.get(
            f"{self._base_url}{endpoint}",
            headers=self._headers,
            params=params,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def paginate(self, endpoint: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Fetch every record of a collection endpoint between ``start`` and ``end``."""
        params: dict[str, Any] = {
            "start": whoop_timestamp(start),
            "end": whoop_timestamp(end),
            "limit": PAGE_LIMIT,
        }
        records: list[dict[str, Any]] = []
        for _ in range(MAX_PAGES):
            page = self.get(endpoint, params)
            batch = page.get("records") or []
            records.extend(batch)
            next_token = page.get("next_token")
            if not next_token or not batch:
                return records
            params["nextToken"] = next_token
        raise RuntimeError(f"{endpoint}: exceeded {MAX_PAGES} pages; aborting pagination")

    def _get_optional(self, endpoint: str) -> dict[str, Any]:
        try:
            return self.get(endpoint)
        except requests.RequestException as exc:
            logger.warning("Optional endpoint %s failed: %s", endpoint, exc)
            return {}

    def fetch_recovery_for_cycles(self, cycles: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Fetch recovery per cycle, tagging each with its cycle id and start time.

        Recent cycles often have no recovery yet (WHOOP answers 404), so
        per-cycle HTTP errors are logged and skipped rather than failing the run.
        """
        recovery = []
        for cycle in cycles:
            cycle_id = cycle.get("id")
            if cycle_id is None:
                continue
            try:
                rec = self.get(recovery_endpoint(cycle_id))
            except requests.HTTPError as exc:
                logger.info("No recovery for cycle %s: %s", cycle_id, exc)
                continue
            rec["cycle_id"] = cycle_id
            rec["cycle_start"] = cycle.get("start", "")
            recovery.append(rec)
        return recovery

    def fetch_export(
        self,
        days_back: int,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> dict[str, Any]:
        """Fetch ``days_back`` days of data and assemble the export document."""
        end = now()
        start = end - timedelta(days=days_back)

        profile = self._get_optional(PROFILE_ENDPOINT)
        body = self._get_optional(BODY_ENDPOINT)
        cycles = self.paginate(CYCLE_ENDPOINT, start, end)
        recovery = self.fetch_recovery_for_cycles(cycles)
        sleep = self.paginate(SLEEP_ENDPOINT, start, end)
        workouts = self.paginate(WORKOUT_ENDPOINT, start, end)
        logger.info(
            "Fetched %d cycles, %d recovery, %d sleep, %d workouts",
            len(cycles),
            len(recovery),
            len(sleep),
            len(workouts),
        )

        return {
            "export_date": end.isoformat(),
            "period": {"start": start.isoformat(), "end": end.isoformat(), "days": days_back},
            "profile": profile,
            "body_measurements": body,
            "cycles": cycles,
            "recovery": recovery,
            "sleep": sleep,
            "workouts": workouts,
            "summary": build_daily_summary(cycles, recovery, sleep, workouts),
        }
