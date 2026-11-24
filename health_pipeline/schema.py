"""The unified, source-agnostic health data model.

Every connector (WHOOP, Apple Health, Oura, ...) normalises its native records
into the four record types below. The model is deliberately small and makes a
few decisions explicit instead of leaving them to each consumer:

**Units.** Durations are whole seconds, energy is kilojoules, heart rate is
beats per minute, HRV is milliseconds, temperature is degrees Celsius,
distance is metres and percentages are 0-100 (never 0-1).

**Time.** Every instant is a timezone-aware :class:`~datetime.datetime` that
keeps the wearer's *local* UTC offset, so the local calendar day can always be
recovered without a separate timezone column.

**Which day does a record belong to?** Sleep and the recovery measured during
it belong to the local date the wearer *woke up* (the night of Sunday into
Monday is Monday's sleep, and Monday's recovery). Activity belongs to the local
date it happened on. This differs from :func:`whoop_exporter.summary.build_daily_summary`,
which keys by the UTC start date for backward compatibility with existing
Drive exports.

**Incomparable numbers stay apart.** WHOOP and Oura report HRV as RMSSD;
Apple Watch reports SDNN. They are different statistics, so they get different
fields rather than one ``hrv`` column that silently mixes them. Likewise,
``readiness_score`` keeps each vendor's own 0-100 score and is only comparable
within one source; records are never blended across sources.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime
from enum import StrEnum
from typing import Any, ClassVar, Self

SCHEMA_VERSION = 1

KCAL_TO_KJ = 4.184


class Source(StrEnum):
    """Where a record came from."""

    WHOOP = "whoop"
    APPLE_HEALTH = "apple_health"
    OURA = "oura"


class Sport(StrEnum):
    """A small shared vocabulary of workout types.

    Vendors have hundreds of activity types; dashboards need a handful. Anything
    without a clear mapping becomes :attr:`OTHER`, and the vendor's own label is
    kept in :attr:`Workout.source_sport`.
    """

    RUNNING = "running"
    CYCLING = "cycling"
    WALKING = "walking"
    HIKING = "hiking"
    SWIMMING = "swimming"
    STRENGTH = "strength"
    HIIT = "hiit"
    YOGA = "yoga"
    ROWING = "rowing"
    OTHER = "other"


class SchemaError(ValueError):
    """A record violates an invariant of the unified model."""


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SchemaError(f"{name} must be timezone-aware, got {value!r}")


def _require_range(name: str, value: float | None, low: float, high: float) -> None:
    if value is not None and not low <= value <= high:
        raise SchemaError(f"{name}={value!r} is outside [{low}, {high}]")


def _require_non_negative(obj: Any, *names: str) -> None:
    for name in names:
        value = getattr(obj, name)
        if value is not None and value < 0:
            raise SchemaError(f"{name} must be >= 0, got {value!r}")


def wake_day(end: datetime) -> date:
    """The local calendar date a sleep (and its recovery) is attributed to."""
    return end.date()


@dataclass(frozen=True, slots=True, kw_only=True)
class SleepSession:
    """One continuous sleep period (a night or a nap)."""

    kind: ClassVar[str] = "sleep"

    source: Source
    source_id: str
    start: datetime
    end: datetime
    is_nap: bool
    time_in_bed_s: int
    time_asleep_s: int | None = None
    light_s: int | None = None
    deep_s: int | None = None
    rem_s: int | None = None
    awake_s: int | None = None
    efficiency_pct: float | None = None
    respiratory_rate_bpm: float | None = None

    def __post_init__(self) -> None:
        _require_aware("start", self.start)
        _require_aware("end", self.end)
        if self.end <= self.start:
            raise SchemaError(f"sleep {self.source_id}: end {self.end} is not after {self.start}")
        _require_non_negative(
            self, "time_in_bed_s", "time_asleep_s", "light_s", "deep_s", "rem_s", "awake_s"
        )
        if self.time_asleep_s is not None and self.time_asleep_s > self.time_in_bed_s:
            raise SchemaError(f"sleep {self.source_id}: asleep longer than in bed")
        _require_range("efficiency_pct", self.efficiency_pct, 0, 100)

    @property
    def day(self) -> date:
        return wake_day(self.end)


@dataclass(frozen=True, slots=True, kw_only=True)
class DailyRecovery:
    """Overnight physiology and the vendor's readiness/recovery score for one day."""

    kind: ClassVar[str] = "recovery"

    source: Source
    day: date
    readiness_score: float | None = None
    resting_hr_bpm: float | None = None
    hrv_rmssd_ms: float | None = None
    hrv_sdnn_ms: float | None = None
    spo2_pct: float | None = None
    skin_temp_c: float | None = None
    skin_temp_deviation_c: float | None = None

    def __post_init__(self) -> None:
        _require_range("readiness_score", self.readiness_score, 0, 100)
        _require_range("spo2_pct", self.spo2_pct, 0, 100)
        _require_range("resting_hr_bpm", self.resting_hr_bpm, 20, 250)
        _require_non_negative(self, "hrv_rmssd_ms", "hrv_sdnn_ms")


@dataclass(frozen=True, slots=True, kw_only=True)
class DailyActivity:
    """Whole-day load and movement."""

    kind: ClassVar[str] = "activity"

    source: Source
    day: date
    strain: float | None = None
    activity_score: float | None = None
    steps: int | None = None
    active_energy_kj: float | None = None
    total_energy_kj: float | None = None
    average_hr_bpm: float | None = None
    max_hr_bpm: float | None = None

    def __post_init__(self) -> None:
        _require_range("strain", self.strain, 0, 21)
        _require_range("activity_score", self.activity_score, 0, 100)
        _require_non_negative(self, "steps", "active_energy_kj", "total_energy_kj")


@dataclass(frozen=True, slots=True, kw_only=True)
class Workout:
    """One recorded exercise session."""

    kind: ClassVar[str] = "workout"

    source: Source
    source_id: str
    start: datetime
    end: datetime
    sport: Sport
    source_sport: str | None = None
    energy_kj: float | None = None
    average_hr_bpm: float | None = None
    max_hr_bpm: float | None = None
    distance_m: float | None = None
    strain: float | None = None

    def __post_init__(self) -> None:
        _require_aware("start", self.start)
        _require_aware("end", self.end)
        if self.end < self.start:
            raise SchemaError(f"workout {self.source_id}: end {self.end} is before {self.start}")
        _require_non_negative(self, "energy_kj", "distance_m")
        _require_range("strain", self.strain, 0, 21)

    @property
    def day(self) -> date:
        return self.start.date()

    @property
    def duration_s(self) -> int:
        return int((self.end - self.start).total_seconds())


UnifiedRecord = SleepSession | DailyRecovery | DailyActivity | Workout
RECORD_TYPES: tuple[type[UnifiedRecord], ...] = (
    SleepSession,
    DailyRecovery,
    DailyActivity,
    Workout,
)
_BY_KIND: dict[str, type[UnifiedRecord]] = {t.kind: t for t in RECORD_TYPES}


def _encode(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def record_to_dict(record: UnifiedRecord) -> dict[str, Any]:
    """JSON-safe dict, tagged with ``kind`` so a mixed list can be decoded."""
    return {"kind": record.kind, **{k: _encode(v) for k, v in asdict(record).items()}}


def record_from_dict(data: Mapping[str, Any]) -> UnifiedRecord:
    """Inverse of :func:`record_to_dict`. Raises :class:`SchemaError` on bad input."""
    kind = data.get("kind")
    cls = _BY_KIND.get(kind)  # type: ignore[arg-type]
    if cls is None:
        raise SchemaError(f"unknown record kind {kind!r}")
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        if value is not None:
            if f.name == "source":
                value = Source(value)
            elif f.name == "sport":
                value = Sport(value)
            elif f.name in ("start", "end"):
                value = datetime.fromisoformat(value)
            elif f.name == "day":
                value = date.fromisoformat(value)
        kwargs[f.name] = value
    try:
        return cls(**kwargs)
    except TypeError as exc:  # missing required field
        raise SchemaError(f"{kind}: {exc}") from exc


@dataclass(slots=True)
class HealthDataset:
    """A bag of unified records, possibly from several sources."""

    sleep: list[SleepSession] = field(default_factory=list)
    recovery: list[DailyRecovery] = field(default_factory=list)
    activity: list[DailyActivity] = field(default_factory=list)
    workouts: list[Workout] = field(default_factory=list)

    def __iter__(self) -> Iterator[UnifiedRecord]:
        yield from self.sleep
        yield from self.recovery
        yield from self.activity
        yield from self.workouts

    def __len__(self) -> int:
        return len(self.sleep) + len(self.recovery) + len(self.activity) + len(self.workouts)

    def add(self, record: UnifiedRecord) -> None:
        match record:
            case SleepSession():
                self.sleep.append(record)
            case DailyRecovery():
                self.recovery.append(record)
            case DailyActivity():
                self.activity.append(record)
            case Workout():
                self.workouts.append(record)

    @classmethod
    def of(cls, records: Iterable[UnifiedRecord]) -> Self:
        dataset = cls()
        for record in records:
            dataset.add(record)
        return dataset

    @classmethod
    def merge(cls, *datasets: HealthDataset) -> Self:
        return cls.of(r for d in datasets for r in d)

    @property
    def sources(self) -> set[Source]:
        return {r.source for r in self}

    def sorted(self) -> HealthDataset:
        """A copy with every list in a stable (source, time) order."""

        def by_time(r: SleepSession | Workout) -> tuple[str, datetime, str]:
            return (r.source.value, r.start, r.source_id)

        def by_day(r: DailyRecovery | DailyActivity) -> tuple[str, date]:
            return (r.source.value, r.day)

        return HealthDataset(
            sleep=sorted(self.sleep, key=by_time),
            recovery=sorted(self.recovery, key=by_day),
            activity=sorted(self.activity, key=by_day),
            workouts=sorted(self.workouts, key=by_time),
        )

    def daily(self) -> dict[str, dict[str, dict[str, Any]]]:
        """``{day: {source: metrics}}`` — one flat row per day per source.

        Sources are kept side by side rather than averaged: their scores are not
        on the same scale. Within a source, the longest non-nap sleep provides the
        night's metrics and naps are counted separately (the same rule as the
        WHOOP daily summary).
        """
        out: dict[str, dict[str, dict[str, Any]]] = {}

        def row(day: date, source: Source) -> dict[str, Any]:
            return out.setdefault(day.isoformat(), {}).setdefault(source.value, {})

        skip = {"source", "day"}
        for rec in self.recovery:
            row(rec.day, rec.source).update(
                {k: v for k, v in asdict(rec).items() if k not in skip and v is not None}
            )
        for act in self.activity:
            row(act.day, act.source).update(
                {k: v for k, v in asdict(act).items() if k not in skip and v is not None}
            )

        grouped: dict[tuple[date, Source], list[SleepSession]] = {}
        for s in self.sleep:
            grouped.setdefault((s.day, s.source), []).append(s)
        for (day, source), sessions in grouped.items():
            target = row(day, source)
            naps = [s for s in sessions if s.is_nap]
            nights = [s for s in sessions if not s.is_nap]
            target["nap_count"] = len(naps)
            target["nap_time_asleep_s"] = sum(n.time_asleep_s or 0 for n in naps)
            if nights:
                night = max(nights, key=lambda s: s.time_in_bed_s)
                for name in ("time_in_bed_s", "time_asleep_s", "light_s", "deep_s", "rem_s",
                             "awake_s", "efficiency_pct", "respiratory_rate_bpm"):  # fmt: skip
                    if (value := getattr(night, name)) is not None:
                        target[f"sleep_{name}"] = value

        for w in self.workouts:
            row(w.day, w.source).setdefault("workouts", []).append(
                {"sport": w.sport.value, "duration_s": w.duration_s, "energy_kj": w.energy_kj}
            )

        return {day: out[day] for day in sorted(out)}

    def to_dict(self) -> dict[str, Any]:
        ordered = self.sorted()
        return {
            "schema_version": SCHEMA_VERSION,
            "sources": sorted(s.value for s in self.sources),
            "records": [record_to_dict(r) for r in ordered],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise SchemaError(f"unsupported schema_version {version!r}")
        return cls.of(record_from_dict(r) for r in data.get("records", []))
