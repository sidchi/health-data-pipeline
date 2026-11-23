"""Runtime configuration.

Every deployment-specific value lives here instead of in source. Values are
resolved in this order (first match wins):

1. Process environment variables (how the Cloud Function is configured).
2. A local JSON file, ``.config.json`` by default (convenient for the CLI).
3. Built-in defaults, where a sensible default exists.

The JSON file uses the same keys as the environment variables, e.g.::

    {"WHOOP_CLIENT_ID": "...", "WHOOP_CLIENT_SECRET": "...", "GDRIVE_FOLDER_ID": "..."}
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_FILE = Path(__file__).resolve().parent.parent / ".config.json"
DEFAULT_WHOOP_SECRET_ID = "whoop-refresh-token"  # noqa: S105 (secret *name*)
DEFAULT_DAYS_BACK = 7

# Environment variable that may point at an alternative config file.
CONFIG_FILE_ENV = "WHOOP_EXPORTER_CONFIG"


class ConfigError(ValueError):
    """Raised when required configuration is missing or malformed."""


@dataclass(frozen=True)
class Settings:
    """Resolved configuration for one exporter run."""

    whoop_client_id: str
    whoop_client_secret: str = field(repr=False)
    gdrive_folder_id: str | None = None
    whoop_secret_id: str = DEFAULT_WHOOP_SECRET_ID
    days_back: int = DEFAULT_DAYS_BACK
    export_path: Path | None = None


def _read_config_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a JSON object")
    return {str(k): str(v) for k, v in data.items() if v is not None}


def _parse_days_back(raw: str | None) -> int:
    if raw is None or raw == "":
        return DEFAULT_DAYS_BACK
    try:
        days = int(raw)
    except ValueError as exc:
        raise ConfigError(f"DAYS_BACK must be an integer, got {raw!r}") from exc
    if not 1 <= days <= 365:
        raise ConfigError(f"DAYS_BACK must be between 1 and 365, got {days}")
    return days


def load_settings(
    env: Mapping[str, str] | None = None,
    config_file: Path | None = None,
    *,
    require_drive_folder: bool = False,
) -> Settings:
    """Build :class:`Settings` from the environment and an optional JSON file.

    Args:
        env: Environment mapping; defaults to :data:`os.environ`.
        config_file: JSON file to read; defaults to ``$WHOOP_EXPORTER_CONFIG``
            or ``.config.json`` at the repository root. A missing file is fine.
        require_drive_folder: Fail if ``GDRIVE_FOLDER_ID`` is not set. The
            Cloud Function requires it; the CLI can run without Drive upload.

    Raises:
        ConfigError: If a required value is missing or a value is malformed.
    """
    env = os.environ if env is None else env
    if config_file is None:
        config_file = (
            Path(env[CONFIG_FILE_ENV]) if env.get(CONFIG_FILE_ENV) else DEFAULT_CONFIG_FILE
        )
    file_values = _read_config_file(config_file)

    def get(key: str) -> str | None:
        value = env.get(key) or file_values.get(key)
        return value.strip() if value else None

    required = ["WHOOP_CLIENT_ID", "WHOOP_CLIENT_SECRET"]
    if require_drive_folder:
        required.append("GDRIVE_FOLDER_ID")
    missing = [key for key in required if not get(key)]
    if missing:
        raise ConfigError(
            f"Missing required configuration: {', '.join(missing)}. "
            f"Set them as environment variables or in {config_file.name} "
            "(see .config.example.json)."
        )

    export_path = get("EXPORT_PATH")
    return Settings(
        whoop_client_id=get("WHOOP_CLIENT_ID") or "",
        whoop_client_secret=get("WHOOP_CLIENT_SECRET") or "",
        gdrive_folder_id=get("GDRIVE_FOLDER_ID"),
        whoop_secret_id=get("WHOOP_SECRET_ID") or DEFAULT_WHOOP_SECRET_ID,
        days_back=_parse_days_back(get("DAYS_BACK")),
        export_path=Path(export_path).expanduser() if export_path else None,
    )
