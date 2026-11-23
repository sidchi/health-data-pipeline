"""Idempotent JSON upload to a Google Drive folder.

One file per day (``WhoopExport-YYYY-MM-DD.json``): re-running on the same day
replaces the file's content instead of creating duplicates, so the scheduler
can safely retry.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)

EXPORT_MIME_TYPE = "application/json"


def export_filename(day: date) -> str:
    return f"WhoopExport-{day.isoformat()}.json"


def serialize_export(data: dict[str, Any]) -> bytes:
    return json.dumps(data, indent=2, default=str).encode("utf-8")


def _quote(value: str) -> str:
    """Escape a value for a Drive ``q`` string literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def find_file_id(service: Any, folder_id: str, filename: str) -> str | None:
    """Return the id of a non-trashed file named ``filename`` in ``folder_id``."""
    query = f"name='{_quote(filename)}' and '{_quote(folder_id)}' in parents and trashed=false"
    files = (
        service.files()
        .list(
            q=query,
            fields="files(id)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
        .get("files", [])
    )
    return files[0]["id"] if files else None


def upsert_json(service: Any, folder_id: str, filename: str, content: bytes) -> tuple[str, bool]:
    """Create or replace ``filename`` in ``folder_id``.

    ``supportsAllDrives`` is always set so the same code works for "My Drive"
    folders and Shared Drives (the Cloud Function's service account has no
    "My Drive" of its own and must write into a Shared Drive).

    Returns:
        ``(file_id, created)`` where ``created`` is False if an existing file
        was updated in place.
    """
    from googleapiclient.http import MediaInMemoryUpload  # heavy import; keep lazy

    media = MediaInMemoryUpload(content, mimetype=EXPORT_MIME_TYPE, resumable=False)
    existing_id = find_file_id(service, folder_id, filename)
    if existing_id:
        result = (
            service.files()
            .update(fileId=existing_id, media_body=media, supportsAllDrives=True)
            .execute()
        )
        logger.info("Drive: updated %s (id=%s)", filename, result["id"])
        return result["id"], False

    result = (
        service.files()
        .create(
            body={"name": filename, "parents": [folder_id]},
            media_body=media,
            fields="id",
            supportsAllDrives=True,
        )
        .execute()
    )
    logger.info("Drive: created %s (id=%s)", filename, result["id"])
    return result["id"], True
