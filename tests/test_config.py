import json
from pathlib import Path

import pytest

from whoop_exporter.config import (
    DEFAULT_DAYS_BACK,
    DEFAULT_WHOOP_SECRET_ID,
    ConfigError,
    load_settings,
)

BASE_ENV = {"WHOOP_CLIENT_ID": "env-id", "WHOOP_CLIENT_SECRET": "env-secret"}


@pytest.fixture
def no_file(tmp_path: Path) -> Path:
    return tmp_path / "missing.json"


def write_config(tmp_path: Path, values: dict) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(values))
    return path


def test_env_only_uses_defaults(no_file):
    settings = load_settings(BASE_ENV, no_file)
    assert settings.whoop_client_id == "env-id"
    assert settings.gdrive_folder_id is None
    assert settings.whoop_secret_id == DEFAULT_WHOOP_SECRET_ID
    assert settings.days_back == DEFAULT_DAYS_BACK
    assert settings.export_path is None


def test_env_takes_precedence_over_file(tmp_path):
    path = write_config(
        tmp_path,
        {"WHOOP_CLIENT_ID": "file-id", "WHOOP_CLIENT_SECRET": "file-secret", "DAYS_BACK": "14"},
    )
    settings = load_settings({"WHOOP_CLIENT_ID": "env-id"}, path)
    assert settings.whoop_client_id == "env-id"
    assert settings.whoop_client_secret == "file-secret"
    assert settings.days_back == 14


def test_config_file_can_be_selected_via_env(tmp_path):
    path = write_config(tmp_path, {"WHOOP_CLIENT_ID": "a", "WHOOP_CLIENT_SECRET": "b"})
    settings = load_settings({"WHOOP_EXPORTER_CONFIG": str(path)})
    assert settings.whoop_client_id == "a"


def test_missing_required_values_are_all_reported(no_file):
    with pytest.raises(ConfigError, match="WHOOP_CLIENT_ID, WHOOP_CLIENT_SECRET"):
        load_settings({}, no_file)


def test_drive_folder_required_only_when_requested(no_file):
    assert load_settings(BASE_ENV, no_file).gdrive_folder_id is None
    with pytest.raises(ConfigError, match="GDRIVE_FOLDER_ID"):
        load_settings(BASE_ENV, no_file, require_drive_folder=True)
    settings = load_settings(
        {**BASE_ENV, "GDRIVE_FOLDER_ID": " folder-123 "}, no_file, require_drive_folder=True
    )
    assert settings.gdrive_folder_id == "folder-123"


@pytest.mark.parametrize("raw", ["abc", "0", "366", "-1"])
def test_invalid_days_back_is_rejected(no_file, raw):
    with pytest.raises(ConfigError, match="DAYS_BACK"):
        load_settings({**BASE_ENV, "DAYS_BACK": raw}, no_file)


def test_export_path_expands_user(no_file):
    settings = load_settings({**BASE_ENV, "EXPORT_PATH": "~/exports"}, no_file)
    assert settings.export_path == Path("~/exports").expanduser()


def test_invalid_json_file_raises(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_settings(BASE_ENV, path)


def test_non_object_json_file_raises(tmp_path):
    path = tmp_path / "list.json"
    path.write_text("[]")
    with pytest.raises(ConfigError, match="JSON object"):
        load_settings(BASE_ENV, path)


def test_secret_is_not_in_repr(no_file):
    assert "env-secret" not in repr(load_settings(BASE_ENV, no_file))


def test_example_config_has_only_placeholders():
    example = Path(__file__).resolve().parent.parent / ".config.example.json"
    values = json.loads(example.read_text())
    assert values["WHOOP_CLIENT_SECRET"].startswith("YOUR_")
    assert values["GDRIVE_FOLDER_ID"].startswith("YOUR_")
