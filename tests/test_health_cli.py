"""``python -m health_pipeline`` end to end on the synthetic inputs."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from health_pipeline.__main__ import main
from health_pipeline.schema import HealthDataset, Source
from whoop_exporter.sample import SAMPLE_EXPORT_PATH

FIXTURES = Path(__file__).parent / "fixtures"
ARGS = [
    "--whoop",
    str(SAMPLE_EXPORT_PATH),
    "--apple-health",
    str(FIXTURES / "apple_health" / "export.xml"),
    "--oura",
    str(FIXTURES / "oura" / "oura_export.json"),
]


def test_writes_unified_records(tmp_path: Path) -> None:
    out = tmp_path / "unified.json"
    assert main([*ARGS, "--output", str(out)]) == 0
    payload = json.loads(out.read_text())
    assert payload["sources"] == ["apple_health", "oura", "whoop"]
    assert len(HealthDataset.from_dict(payload)) == len(payload["records"])


def test_daily_view_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([*ARGS, "--daily"]) == 0
    daily = json.loads(capsys.readouterr().out)
    assert set(daily["2026-01-06"]) == {"apple_health", "oura", "whoop"}


def test_requires_an_input(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2
    assert "at least one of" in capsys.readouterr().err


def test_reports_unreadable_input(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--oura", str(tmp_path / "missing.json")]) == 2
    assert "cannot read JSON" in capsys.readouterr().err


def test_loads_into_duckdb_idempotently(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    from health_pipeline.store import HealthStore

    db = tmp_path / "health.duckdb"
    assert main([*ARGS, "--duckdb", str(db)]) == 0
    assert capsys.readouterr().out == ""  # no --output: DuckDB only
    with HealthStore(db) as store:
        first = store.counts()
        assert store.read().sources == {Source.WHOOP, Source.APPLE_HEALTH, Source.OURA}

    caplog.clear()
    caplog.set_level(logging.INFO)
    assert main([*ARGS, "--duckdb", str(db)]) == 0
    assert caplog.text.count("skipped (already loaded") == 3
    assert main([*ARGS, "--duckdb", str(db), "--force"]) == 0
    with HealthStore(db) as store:
        assert store.counts() == first
        assert len(store.runs()) == 6


def test_duckdb_and_json_together(tmp_path: Path) -> None:
    out = tmp_path / "daily.json"
    db = tmp_path / "h.duckdb"
    assert main([*ARGS, "--duckdb", str(db), "--daily", "--output", str(out)]) == 0
    assert "2026-01-06" in json.loads(out.read_text())
    assert db.is_file()


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--force"], "--force only applies with --duckdb"),
        (["--duckdb", "x.duckdb", "--daily"], "--daily with --duckdb needs --output"),
    ],
)
def test_duckdb_flag_validation(
    extra: list[str], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        main([*ARGS, *extra])
    assert message in capsys.readouterr().err
