import json
from datetime import date

from whoop_exporter.drive import export_filename, find_file_id, serialize_export, upsert_json

from .fakes import FakeDriveService


def test_export_filename():
    assert export_filename(date(2026, 1, 5)) == "WhoopExport-2026-01-05.json"


def test_serialize_handles_non_json_types():
    payload = json.loads(serialize_export({"when": date(2026, 1, 5)}))
    assert payload == {"when": "2026-01-05"}


def test_creates_file_when_absent():
    service = FakeDriveService()
    file_id, created = upsert_json(service, "folder-1", "WhoopExport-2026-01-05.json", b"{}")
    assert (file_id, created) == ("new-file-id", True)
    assert service.files().created == [
        {"name": "WhoopExport-2026-01-05.json", "parents": ["folder-1"]}
    ]


def test_updates_existing_file_in_place():
    service = FakeDriveService(existing={"WhoopExport-2026-01-05.json": "abc"})
    file_id, created = upsert_json(service, "folder-1", "WhoopExport-2026-01-05.json", b"{}")
    assert (file_id, created) == ("abc", False)
    assert service.files().updated == ["abc"]
    assert service.files().created == []


def test_query_escapes_quotes():
    service = FakeDriveService()
    find_file_id(service, "fold'er", "it's.json")
    assert service.files().list_queries == [
        "name='it\\'s.json' and 'fold\\'er' in parents and trashed=false"
    ]
