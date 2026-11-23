"""DuckDB store: migrations, round trips, idempotent upserts and incremental loads.

All inputs are synthetic: the generated WHOOP sample and the Oura / Apple Health
fixtures.
"""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta
from pathlib import Path

import pytest

from health_pipeline.connectors import get_connector
from health_pipeline.schema import DailyRecovery, HealthDataset, Source
from health_pipeline.store import (
    SCHEMA_VERSION,
    TABLES,
    HealthStore,
    LoadResult,
    incremental_days,
    record_hash,
)
from whoop_exporter.sample import SAMPLE_EXPORT_PATH

FIXTURES = Path(__file__).parent / "fixtures"
INPUTS = {
    Source.WHOOP: SAMPLE_EXPORT_PATH,
    Source.OURA: FIXTURES / "oura" / "oura_export.json",
    Source.APPLE_HEALTH: FIXTURES / "apple_health" / "export.xml",
}


@pytest.fixture(scope="module")
def datasets() -> dict[Source, HealthDataset]:
    return {source: get_connector(source).load(path) for source, path in INPUTS.items()}


@pytest.fixture(scope="module")
def whoop(datasets: dict[Source, HealthDataset]) -> HealthDataset:
    return datasets[Source.WHOOP]


@pytest.fixture
def store() -> HealthStore:
    with HealthStore() as s:
        yield s


def as_dict(dataset: HealthDataset) -> dict:
    return dataset.sorted().to_dict()


class TestMigrations:
    def test_fresh_store_is_at_latest_version(self, store: HealthStore) -> None:
        assert store.schema_version() == SCHEMA_VERSION
        assert store.migrate() == 0

    def test_reopening_a_file_does_not_rerun_migrations(self, tmp_path: Path, whoop) -> None:
        db = tmp_path / "h.duckdb"
        with HealthStore(db) as first:
            first.load(whoop, label="seed")
        with HealthStore(db) as second:
            assert second.migrate() == 0
            assert as_dict(second.read()) == as_dict(whoop)

    def test_refuses_a_database_from_newer_code(self, tmp_path: Path) -> None:
        db = tmp_path / "h.duckdb"
        with HealthStore(db) as s:
            s.connection.execute(
                "INSERT INTO schema_migrations VALUES (?, 'future', now())", [SCHEMA_VERSION + 1]
            )
        with pytest.raises(RuntimeError, match="newer than this code"):
            HealthStore(db)

    @pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
    def test_ddl_matches_the_dataclasses(self, store: HealthStore, table) -> None:
        """Changing a schema dataclass must come with a migration, never silently."""
        described = store.connection.execute(f"DESCRIBE {table.name}").fetchall()
        assert tuple(row[0] for row in described) == table.columns


class TestRoundTrip:
    def test_every_source_round_trips_exactly(self, store: HealthStore, datasets) -> None:
        for source, dataset in datasets.items():
            store.load(dataset, label=source.value)
        for source, dataset in datasets.items():
            assert as_dict(store.read(sources=[source])) == as_dict(dataset)

    def test_local_offset_survives_storage(self, store: HealthStore, whoop) -> None:
        store.load(whoop, label="whoop")
        original = {s.source_id: s.start.utcoffset() for s in whoop.sleep}
        stored = {s.source_id: s.start.utcoffset() for s in store.read().sleep}
        assert stored == original
        assert None not in stored.values()

    def test_read_filters_by_source_and_inclusive_day_range(
        self, store: HealthStore, datasets
    ) -> None:
        for source, dataset in datasets.items():
            store.load(dataset, label=source.value)
        start, end = date(2026, 1, 10), date(2026, 1, 12)
        got = store.read(sources=["whoop"], start=start, end=end)
        assert got.sources == {Source.WHOOP}
        assert {r.day for r in got} == {start, date(2026, 1, 11), end}
        expected = [r for r in datasets[Source.WHOOP] if start <= r.day <= end]
        assert len(got) == len(expected)


class TestIdempotentLoads:
    def test_reloading_the_same_data_changes_nothing(self, store: HealthStore, whoop) -> None:
        first = store.load(whoop, label="a")
        counts = store.counts()
        second = store.load(whoop, label="b")
        assert (first.inserted, first.updated, first.unchanged) == (len(whoop), 0, 0)
        assert (second.inserted, second.updated, second.unchanged) == (0, 0, len(whoop))
        assert store.counts() == counts
        assert sum(counts.values()) == len(whoop)

    def test_a_rescored_record_is_updated_in_place(self, store: HealthStore, whoop) -> None:
        first = store.load(whoop, label="a")
        old = whoop.recovery[0]
        rescored = dataclasses.replace(old, readiness_score=old.readiness_score - 1)
        result = store.load([rescored], label="rescore")
        assert (result.inserted, result.updated, result.unchanged) == (0, 1, 0)

        (row,) = store.connection.execute(
            "SELECT readiness_score, record_hash, first_run_id, last_changed_run_id "
            "FROM daily_recovery WHERE source = ? AND day = ?",
            [old.source.value, old.day],
        ).fetchall()
        assert row == (rescored.readiness_score, record_hash(rescored), first.run_id, result.run_id)

    def test_overlapping_windows_converge_to_the_union(self, store: HealthStore, whoop) -> None:
        """Rolling DAYS_BACK exports overlap; loading them in turn equals one full load."""
        days = sorted({r.day for r in whoop})
        early = HealthDataset.of(r for r in whoop if r.day <= days[20])
        late = HealthDataset.of(r for r in whoop if r.day >= days[10])
        overlap = sum(1 for r in whoop if days[10] <= r.day <= days[20])

        store.load(early, label="early")
        second = store.load(late, label="late")
        assert second.unchanged == overlap
        assert second.inserted == len(late) - overlap
        assert as_dict(store.read()) == as_dict(whoop)

    def test_duplicate_keys_in_one_load_last_one_wins(self, store: HealthStore) -> None:
        a = DailyRecovery(source=Source.OURA, day=date(2026, 1, 5), readiness_score=70)
        b = dataclasses.replace(a, readiness_score=80)
        result = store.load([a, b], label="dupes")
        assert (result.records, result.inserted) == (1, 1)
        assert store.read().recovery == [b]

    def test_a_failed_load_leaves_no_trace(self, store: HealthStore, whoop, monkeypatch) -> None:
        calls = 0
        real = HealthStore._upsert

        def fail_on_second_table(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("disk full")
            return real(*args, **kwargs)

        monkeypatch.setattr(HealthStore, "_upsert", staticmethod(fail_on_second_table))
        with pytest.raises(RuntimeError, match="disk full"):
            store.load_file(SAMPLE_EXPORT_PATH, "whoop")
        assert sum(store.counts().values()) == 0
        assert store.runs() == []

        monkeypatch.undo()
        assert not store.load_file(SAMPLE_EXPORT_PATH, "whoop").skipped


class TestFileLoads:
    def test_identical_file_is_skipped_unless_forced(self, store: HealthStore) -> None:
        first = store.load_file(SAMPLE_EXPORT_PATH, Source.WHOOP)
        again = store.load_file(SAMPLE_EXPORT_PATH, "whoop")
        forced = store.load_file(SAMPLE_EXPORT_PATH, "whoop", force=True)

        assert again == LoadResult(str(SAMPLE_EXPORT_PATH), first.run_id, records=0, skipped=True)
        assert "skipped" in str(again)
        assert not forced.skipped
        assert forced.unchanged == first.inserted
        assert [r["label"] for r in store.runs()] == [str(SAMPLE_EXPORT_PATH)] * 2

    def test_a_changed_file_is_loaded(self, store: HealthStore, tmp_path: Path) -> None:
        copy = tmp_path / "export.json"
        copy.write_bytes(SAMPLE_EXPORT_PATH.read_bytes())
        store.load_file(copy, "whoop")
        copy.write_bytes(SAMPLE_EXPORT_PATH.read_bytes() + b"\n")
        result = store.load_file(copy, "whoop")
        assert not result.skipped
        assert result.inserted == 0
        assert "0 new" in str(result)

    def test_run_audit_records_counts_and_digest(self, store: HealthStore) -> None:
        result = store.load_file(INPUTS[Source.OURA], "oura")
        (run,) = store.runs()
        assert run["run_id"] == result.run_id
        assert run["connector"] == "oura"
        assert len(run["content_sha256"]) == 64
        assert (run["records"], run["inserted"]) == (result.records, result.inserted)


class TestQueries:
    def test_watermark(self, store: HealthStore, whoop) -> None:
        assert store.watermark("whoop") is None
        store.load(whoop, label="whoop")
        assert store.watermark(Source.WHOOP) == max(r.day for r in whoop)
        assert store.watermark("oura") is None

    def test_daily_metrics_view_agrees_with_python_daily(
        self, store: HealthStore, datasets
    ) -> None:
        merged = HealthDataset.merge(*datasets.values())
        store.load(merged, label="all")
        python = merged.daily()
        rows = store.daily_metrics()
        assert {(r["day"].isoformat(), r["source"]) for r in rows} == {
            (day, source) for day, by_source in python.items() for source in by_source
        }
        compared = ("readiness_score", "strain", "steps", "sleep_time_asleep_s", "nap_count",
                    "sleep_efficiency_pct", "hrv_rmssd_ms", "hrv_sdnn_ms")  # fmt: skip
        for row in rows:
            expected = python[row["day"].isoformat()][row["source"]]
            for column in compared:
                if column in expected:
                    assert row[column] == expected[column], (row["day"], row["source"], column)
            workouts = expected.get("workouts", [])
            assert row["workout_count"] == len(workouts)
            assert row["workout_duration_s"] == sum(w["duration_s"] for w in workouts)

    def test_daily_metrics_filter(self, store: HealthStore, datasets) -> None:
        for dataset in datasets.values():
            store.load(dataset, label="x")
        assert {r["source"] for r in store.daily_metrics("oura")} == {"oura"}


class TestIncrementalDays:
    today = date(2026, 2, 10)

    def test_empty_store_uses_fallback(self) -> None:
        assert incremental_days(None, self.today, fallback=30) == 30

    def test_fetches_gap_plus_overlap(self) -> None:
        assert incremental_days(date(2026, 2, 1), self.today, fallback=30) == 9 + 3
        assert incremental_days(date(2026, 2, 1), self.today, fallback=30, overlap=0) == 9

    def test_stale_store_backfills_beyond_fallback(self) -> None:
        stale = self.today - timedelta(days=100)
        assert incremental_days(stale, self.today, fallback=30) == 103

    def test_at_least_one_day(self) -> None:
        assert incremental_days(self.today, self.today, fallback=30, overlap=0) == 1

    def test_rejects_negative_overlap(self) -> None:
        with pytest.raises(ValueError, match="overlap"):
            incremental_days(None, self.today, fallback=30, overlap=-1)
