"""Smoke tests for the Cloud Function handler's error contract."""

import pytest

import main


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    for key in ("WHOOP_CLIENT_ID", "WHOOP_CLIENT_SECRET", "GDRIVE_FOLDER_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("WHOOP_EXPORTER_CONFIG", str(tmp_path / "none.json"))


def test_missing_config_returns_500_with_reason():
    body, status = main.trigger_upload(None)
    assert status == 500
    assert "GDRIVE_FOLDER_ID" in body


def test_unexpected_error_returns_generic_500(monkeypatch):
    def boom():
        raise RuntimeError("token=should-not-leak")

    monkeypatch.setattr(main, "run_export", boom)
    body, status = main.trigger_upload(None)
    assert status == 500
    assert "should-not-leak" not in body


def test_success_returns_file_id(monkeypatch):
    monkeypatch.setattr(main, "run_export", lambda: "file-42")
    body, status = main.trigger_upload(None)
    assert status == 200
    assert "file-42" in body


def test_run_export_wires_components(monkeypatch):
    monkeypatch.setenv("WHOOP_CLIENT_ID", "cid")
    monkeypatch.setenv("WHOOP_CLIENT_SECRET", "cs")
    monkeypatch.setenv("GDRIVE_FOLDER_ID", "folder-9")
    monkeypatch.setattr(main, "default_project_id", lambda: "proj")
    monkeypatch.setattr(main, "SecretManagerRefreshTokenStore", lambda p, s: (p, s))
    monkeypatch.setattr(main, "get_access_token", lambda settings, store: "tok")
    monkeypatch.setattr(main, "drive_service_from_default_credentials", lambda: "svc")

    class StubClient:
        def __init__(self, token):
            assert token == "tok"

        def fetch_export(self, days):
            return {"summary": {"2026-01-01": {}}, "days": days}

    uploads = []
    monkeypatch.setattr(main, "WhoopClient", StubClient)
    monkeypatch.setattr(main, "upsert_json", lambda *args: uploads.append(args) or ("file-1", True))

    assert main.run_export() == "file-1"
    service, folder, filename, _content = uploads[0]
    assert (service, folder) == ("svc", "folder-9")
    assert filename.startswith("WhoopExport-")
