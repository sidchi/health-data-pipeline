import json
import stat
from typing import ClassVar

import pytest

from whoop_exporter import auth, cli
from whoop_exporter.config import Settings

SETTINGS = Settings(whoop_client_id="cid", whoop_client_secret="cs")


def test_uses_cached_token_while_fresh(tmp_path, monkeypatch):
    token_file = tmp_path / "tok.json"
    token_file.write_text(json.dumps({"access_token": "cached", "expires_in": 3600}))
    monkeypatch.setattr(auth, "is_access_token_fresh", lambda _t: True)
    monkeypatch.setattr(cli, "_browser_authorize", pytest.fail)
    assert cli.get_access_token(SETTINGS, token_file) == "cached"


def test_refreshes_and_persists_rotated_token_privately(tmp_path, monkeypatch):
    token_file = tmp_path / "tok.json"
    token_file.write_text(json.dumps({"access_token": "old", "refresh_token": "r1"}))
    monkeypatch.setattr(
        auth,
        "refresh_access_token",
        lambda rt, cid, cs: {"access_token": "new", "refresh_token": f"{rt}-rotated"},
    )
    assert cli.get_access_token(SETTINGS, token_file) == "new"
    assert json.loads(token_file.read_text())["refresh_token"] == "r1-rotated"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


def test_falls_back_to_browser_when_refresh_is_rejected(tmp_path, monkeypatch):
    token_file = tmp_path / "tok.json"
    token_file.write_text(json.dumps({"refresh_token": "revoked"}))

    def reject(*_args):
        raise auth.TokenError("HTTP 400")

    monkeypatch.setattr(auth, "refresh_access_token", reject)
    monkeypatch.setattr(cli, "_browser_authorize", lambda _s: "code-1")
    monkeypatch.setattr(auth, "exchange_code", lambda code, *_a: {"access_token": f"from-{code}"})
    assert cli.get_access_token(SETTINGS, token_file) == "from-code-1"


def test_main_reports_config_errors(monkeypatch, tmp_path, capsys):
    for key in ("WHOOP_CLIENT_ID", "WHOOP_CLIENT_SECRET"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("WHOOP_EXPORTER_CONFIG", str(tmp_path / "none.json"))
    assert cli.main([]) == 2
    assert "WHOOP_CLIENT_ID" in capsys.readouterr().err


def test_main_writes_export_and_skips_drive(monkeypatch, tmp_path):
    monkeypatch.setenv("WHOOP_EXPORTER_CONFIG", str(tmp_path / "none.json"))
    monkeypatch.setenv("WHOOP_CLIENT_ID", "cid")
    monkeypatch.setenv("WHOOP_CLIENT_SECRET", "cs")
    monkeypatch.setattr(cli, "get_access_token", lambda _s: "tok")

    class StubClient:
        def __init__(self, token):
            assert token == "tok"

        def fetch_export(self, days):
            return {"period": {"days": days}, "summary": {"2026-01-01": {"strain": 5.0}}}

    monkeypatch.setattr(cli, "WhoopClient", StubClient)
    monkeypatch.setattr(cli, "_drive_service", pytest.fail)

    assert cli.main(["--days", "3", "--out", str(tmp_path), "--no-drive"]) == 0
    (export,) = tmp_path.glob("WhoopExport-*.json")
    assert json.loads(export.read_text())["period"] == {"days": 3}


class SampleClient:
    """Returns the committed synthetic sample export and records the window asked for."""

    requested_days: ClassVar[list[int]] = []

    def __init__(self, token):
        pass

    def fetch_export(self, days):
        from whoop_exporter.sample import SAMPLE_EXPORT_PATH

        SampleClient.requested_days.append(days)
        return json.loads(SAMPLE_EXPORT_PATH.read_text())


@pytest.fixture
def offline_cli(monkeypatch, tmp_path):
    monkeypatch.setenv("WHOOP_EXPORTER_CONFIG", str(tmp_path / "none.json"))
    monkeypatch.setenv("WHOOP_CLIENT_ID", "cid")
    monkeypatch.setenv("WHOOP_CLIENT_SECRET", "cs")
    monkeypatch.setenv("DAYS_BACK", "30")
    monkeypatch.setattr(cli, "get_access_token", lambda _s: "tok")
    monkeypatch.setattr(cli, "WhoopClient", SampleClient)
    monkeypatch.setattr(cli, "_drive_service", pytest.fail)
    SampleClient.requested_days = []
    return tmp_path


def test_main_loads_export_into_duckdb(offline_cli, capsys):
    from health_pipeline.store import HealthStore

    db = offline_cli / "h.duckdb"
    args = ["--out", str(offline_cli), "--no-drive", "--duckdb", str(db)]
    assert cli.main(args) == 0
    assert "new" in capsys.readouterr().out
    assert cli.main(args) == 0  # same export bytes again
    assert "skipped" in capsys.readouterr().out
    with HealthStore(db) as store:
        assert store.watermark("whoop") is not None
        assert len(store.runs()) == 1


def test_incremental_fetches_from_the_watermark(offline_cli):
    from datetime import UTC, datetime

    from health_pipeline.store import HealthStore, incremental_days

    db = offline_cli / "h.duckdb"
    args = ["--out", str(offline_cli), "--no-drive", "--duckdb", str(db), "--incremental"]
    assert cli.main(args) == 0  # empty store: falls back to DAYS_BACK
    with HealthStore(db) as store:
        watermark = store.watermark("whoop")
    assert cli.main(args) == 0
    today = datetime.now(UTC).date()
    assert SampleClient.requested_days == [30, incremental_days(watermark, today, fallback=30)]


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--incremental"], "--incremental requires --duckdb"),
        (["--duckdb", "h.duckdb", "--incremental", "--days", "3"], "mutually exclusive"),
    ],
)
def test_incremental_flag_validation(argv, message, capsys):
    with pytest.raises(SystemExit):
        cli.main(argv)
    assert message in capsys.readouterr().err
