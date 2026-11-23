"""Source connectors and the registry the CLI uses to find them."""

from __future__ import annotations

from ..schema import Source
from .apple_health import AppleHealthConnector
from .base import Connector, ConnectorError
from .oura import OuraConnector
from .whoop import WhoopConnector

CONNECTORS: dict[Source, type[Connector]] = {
    Source.WHOOP: WhoopConnector,
    Source.APPLE_HEALTH: AppleHealthConnector,
    Source.OURA: OuraConnector,
}


def get_connector(source: Source | str) -> Connector:
    """Instantiate the connector for ``source`` (``"whoop"``, ``"apple_health"``, ``"oura"``)."""
    try:
        return CONNECTORS[Source(source)]()
    except ValueError:
        known = ", ".join(s.value for s in CONNECTORS)
        raise ConnectorError(f"unknown source {source!r}; expected one of: {known}") from None


__all__ = [
    "CONNECTORS",
    "AppleHealthConnector",
    "Connector",
    "ConnectorError",
    "OuraConnector",
    "WhoopConnector",
    "get_connector",
]
