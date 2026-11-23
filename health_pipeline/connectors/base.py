"""The connector contract and a small registry.

A connector turns one vendor's export into a :class:`~health_pipeline.schema.HealthDataset`.
Connectors are pure: they read a file (or an already-parsed payload), never
talk to the network and never write anything, which keeps them trivially
testable against fixtures. Fetching data stays the job of the per-vendor
clients (e.g. :mod:`whoop_exporter.client`).

Malformed individual records are skipped and logged rather than failing the
whole load: a single corrupt night should not block a year of data. Structural
problems (wrong file type, unreadable JSON/XML) raise :class:`ConnectorError`.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar

from ..schema import HealthDataset, SchemaError, Source, UnifiedRecord

log = logging.getLogger(__name__)


class ConnectorError(ValueError):
    """The input cannot be read by this connector at all."""


class Connector(ABC):
    """Base class for source connectors."""

    source: ClassVar[Source]

    @abstractmethod
    def load(self, path: Path) -> HealthDataset:
        """Read and normalise an export file."""

    def _safe(self, what: str, build: Callable[[], UnifiedRecord | None]) -> UnifiedRecord | None:
        """Build one record, logging and skipping it if it is malformed."""
        try:
            return build()
        except (SchemaError, KeyError, TypeError, ValueError) as exc:
            log.warning("%s: skipping malformed %s: %s", self.source.value, what, exc)
            return None


def read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConnectorError(f"cannot read JSON from {path}: {exc}") from exc


def parse_offset(offset: str | None) -> timezone:
    """``"+05:30"`` / ``"-0700"`` / ``"Z"`` → :class:`datetime.timezone`; ``None`` → UTC."""
    if not offset or offset in ("Z", "z"):
        return UTC
    sign = -1 if offset[0] == "-" else 1
    digits = offset.lstrip("+-").replace(":", "")
    if len(digits) != 4 or not digits.isdigit():
        raise ValueError(f"bad UTC offset {offset!r}")
    delta = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
    return timezone(sign * delta)


def parse_instant(value: str, tz: timezone | None = None) -> datetime:
    """Parse an ISO-8601 timestamp; naive values are taken as UTC.

    If ``tz`` is given the result is converted to that offset, so local dates
    come out right for sources that store UTC plus a separate offset (WHOOP).
    """
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(tz) if tz is not None else dt


def seconds(milli: float | None) -> int | None:
    return None if milli is None else round(milli / 1000)
