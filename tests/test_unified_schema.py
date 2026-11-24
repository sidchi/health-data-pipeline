"""Invariants, serialisation and the daily view of the unified schema."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from health_pipeline.schema import (
    SCHEMA_VERSION,
    DailyActivity,
    DailyRecovery,
    HealthDataset,
    SchemaError,
    SleepSession,
    Source,
    Sport,
    Workout,
    record_from_dict,
    record_to_dict,
)

IST = timezone(timedelta(hours=5, minutes=30))


def night(**overrides) -> SleepSession:
    fields = {
        "source": Source.WHOOP,
        "source_id": "s1",
        "start": datetime(2026, 1, 5, 22, 0, tzinfo=IST),
        "end": datetime(2026, 1, 6, 6, 0, tzinfo=IST),
        "is_nap": False,
        "time_in_bed_s": 8 * 3600,
        "time_asleep_s": 7 * 3600,
        "light_s": 4 * 3600,
        "deep_s": 3600,
        "rem_s": 2 * 3600,
        "awake_s": 3600,
        "efficiency_pct": 87.5,
        "respiratory_rate_bpm": 14.5,
    }
    return SleepSession(**{**fields, **overrides})


def workout(**overrides) -> Workout:
    fields = {
        "source": Source.OURA,
        "source_id": "w1",
        "start": datetime(2026, 1, 6, 7, 0, tzinfo=IST),
        "end": datetime(2026, 1, 6, 7, 45, tzinfo=IST),
        "sport": Sport.RUNNING,
        "source_sport": "running",
        "energy_kj": 2000.0,
        "distance_m": 8000.0,
    }
    return Workout(**{**fields, **overrides})


ALL_RECORDS = [
    night(),
    DailyRecovery(source=Source.WHOOP, day=date(2026, 1, 6), readiness_score=70, hrv_rmssd_ms=55),
    DailyRecovery(source=Source.APPLE_HEALTH, day=date(2026, 1, 6), hrv_sdnn_ms=45),
    DailyActivity(source=Source.OURA, day=date(2026, 1, 6), steps=9000, active_energy_kj=2000.0),
    workout(),
]


class TestInvariants:
    def test_sleep_belongs_to_the_local_wake_date(self) -> None:
        # 00:30 UTC on the 6th is still the 6th locally, but a 23:00 UTC wake in
        # IST is the *next* local day; the local offset decides.
        assert night().day == date(2026, 1, 6)
        late = night(
            start=datetime(2026, 1, 6, 16, 0, tzinfo=UTC),
            end=datetime(2026, 1, 6, 23, 0, tzinfo=UTC).astimezone(IST),
        )
        assert late.day == date(2026, 1, 7)

    def test_naive_datetimes_are_rejected(self) -> None:
        with pytest.raises(SchemaError, match="timezone-aware"):
            night(start=datetime(2026, 1, 5, 22, 0))

    def test_sleep_must_end_after_it_starts(self) -> None:
        with pytest.raises(SchemaError, match="not after"):
            night(end=datetime(2026, 1, 5, 22, 0, tzinfo=IST))

    def test_cannot_sleep_longer_than_in_bed(self) -> None:
        with pytest.raises(SchemaError, match="asleep longer"):
            night(time_asleep_s=9 * 3600)

    def test_negative_durations_are_rejected(self) -> None:
        with pytest.raises(SchemaError, match="rem_s"):
            night(rem_s=-1)

    @pytest.mark.parametrize(
        ("kwargs", "field"),
        [
            ({"readiness_score": 101}, "readiness_score"),
            ({"spo2_pct": 0.97 * 1000}, "spo2_pct"),
            ({"resting_hr_bpm": 5}, "resting_hr_bpm"),
            ({"hrv_rmssd_ms": -3}, "hrv_rmssd_ms"),
        ],
    )
    def test_recovery_ranges(self, kwargs: dict, field: str) -> None:
        with pytest.raises(SchemaError, match=field):
            DailyRecovery(source=Source.OURA, day=date(2026, 1, 1), **kwargs)

    def test_strain_is_on_the_whoop_scale(self) -> None:
        with pytest.raises(SchemaError, match="strain"):
            DailyActivity(source=Source.WHOOP, day=date(2026, 1, 1), strain=22)

    def test_workout_duration_and_day(self) -> None:
        w = workout()
        assert w.duration_s == 45 * 60
        assert w.day == date(2026, 1, 6)
        with pytest.raises(SchemaError, match="before"):
            workout(end=w.start - timedelta(seconds=1))

    def test_records_are_immutable(self) -> None:
        with pytest.raises(AttributeError):
            night().is_nap = True  # type: ignore[misc]


class TestSerialisation:
    @pytest.mark.parametrize("record", ALL_RECORDS, ids=lambda r: r.kind)
    def test_round_trip(self, record) -> None:
        encoded = record_to_dict(record)
        assert encoded["kind"] == record.kind
        assert record_from_dict(encoded) == record

    def test_offsets_survive_round_trip(self) -> None:
        decoded = record_from_dict(record_to_dict(night()))
        assert decoded.start.utcoffset() == timedelta(hours=5, minutes=30)

    def test_dataset_round_trip_is_stable(self) -> None:
        dataset = HealthDataset.of(reversed(ALL_RECORDS))
        payload = dataset.to_dict()
        assert payload["schema_version"] == SCHEMA_VERSION
        assert payload["sources"] == ["apple_health", "oura", "whoop"]
        again = HealthDataset.from_dict(payload)
        assert again.to_dict() == payload
        assert len(again) == len(ALL_RECORDS)

    def test_unknown_kind(self) -> None:
        with pytest.raises(SchemaError, match="unknown record kind"):
            record_from_dict({"kind": "steps"})

    def test_missing_required_field(self) -> None:
        with pytest.raises(SchemaError, match="recovery"):
            record_from_dict({"kind": "recovery", "source": "oura"})

    def test_unsupported_schema_version(self) -> None:
        with pytest.raises(SchemaError, match="schema_version"):
            HealthDataset.from_dict({"schema_version": 99, "records": []})


class TestDataset:
    def test_add_routes_by_type_and_merge_concatenates(self) -> None:
        a = HealthDataset.of(ALL_RECORDS[:2])
        b = HealthDataset.of(ALL_RECORDS[2:])
        merged = HealthDataset.merge(a, b)
        assert (len(merged.sleep), len(merged.recovery), len(merged.activity)) == (1, 2, 1)
        assert len(merged.workouts) == 1
        assert merged.sources == {Source.WHOOP, Source.APPLE_HEALTH, Source.OURA}

    def test_daily_keeps_sources_side_by_side(self) -> None:
        daily = HealthDataset.of(ALL_RECORDS).daily()
        day = daily["2026-01-06"]
        assert set(day) == {"whoop", "apple_health", "oura"}
        # RMSSD and SDNN never share a column.
        assert day["whoop"]["hrv_rmssd_ms"] == 55
        assert "hrv_rmssd_ms" not in day["apple_health"]
        assert day["apple_health"]["hrv_sdnn_ms"] == 45
        assert day["oura"]["workouts"] == [
            {"sport": "running", "duration_s": 2700, "energy_kj": 2000.0}
        ]

    def test_daily_uses_the_longest_night_and_counts_naps(self) -> None:
        short = night(source_id="short", time_in_bed_s=3 * 3600, time_asleep_s=3 * 3600)
        nap = night(
            source_id="nap",
            start=datetime(2026, 1, 6, 14, 0, tzinfo=IST),
            end=datetime(2026, 1, 6, 14, 30, tzinfo=IST),
            is_nap=True,
            time_in_bed_s=1800,
            time_asleep_s=1500,
        )
        row = HealthDataset.of([short, nap, night()]).daily()["2026-01-06"]["whoop"]
        assert row["sleep_time_in_bed_s"] == 8 * 3600
        assert row["nap_count"] == 1
        assert row["nap_time_asleep_s"] == 1500

    def test_daily_is_sorted_by_day(self) -> None:
        later = DailyActivity(source=Source.OURA, day=date(2026, 2, 1), steps=1)
        earlier = DailyActivity(source=Source.OURA, day=date(2025, 12, 1), steps=1)
        assert list(HealthDataset.of([later, earlier]).daily()) == ["2025-12-01", "2026-02-01"]
