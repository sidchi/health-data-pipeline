"""Google Cloud Function entry point (HTTP, invoked by Cloud Scheduler).

Configuration comes from environment variables set at deploy time; the WHOOP
refresh token lives in Secret Manager. See README.md for deployment.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import functions_framework

from whoop_exporter.client import WhoopClient
from whoop_exporter.config import ConfigError, load_settings
from whoop_exporter.drive import export_filename, serialize_export, upsert_json
from whoop_exporter.gcp import (
    SecretManagerRefreshTokenStore,
    default_project_id,
    drive_service_from_default_credentials,
    get_access_token,
)
from whoop_exporter.summary import format_summary_line

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("whoop_exporter.function")


def run_export() -> str:
    """Fetch, upload and return the Drive file id. Raises on any failure."""
    settings = load_settings(require_drive_folder=True)
    project_id = default_project_id()
    logger.info("Project %s: fetching %d days of WHOOP data", project_id, settings.days_back)

    store = SecretManagerRefreshTokenStore(project_id, settings.whoop_secret_id)
    client = WhoopClient(get_access_token(settings, store))
    data = client.fetch_export(settings.days_back)
    for day in sorted(data["summary"], reverse=True):
        logger.info(format_summary_line(day, data["summary"][day]))

    folder_id = settings.gdrive_folder_id
    if not folder_id:  # unreachable: require_drive_folder=True; narrows the type
        raise ConfigError("GDRIVE_FOLDER_ID is required")
    file_id, _ = upsert_json(
        drive_service_from_default_credentials(),
        folder_id,
        export_filename(datetime.now(UTC).date()),
        serialize_export(data),
    )
    return file_id


@functions_framework.http
def trigger_upload(request):  # noqa: ARG001 (signature required by functions-framework)
    """HTTP handler. Returns 200 on success, 500 (so Scheduler retries) on failure."""
    try:
        file_id = run_export()
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        return f"Configuration error: {exc}", 500
    except Exception:
        logger.exception("Export failed")
        return "Export failed; see logs", 500
    return f"OK: WhoopExport uploaded (Drive file id={file_id})", 200
