"""Rolling baselines, anomaly flags and weekly summaries over the unified model.

Everything here is a pure function of a :class:`~health_pipeline.schema.HealthDataset`
(or the ``{day: {source: metrics}}`` view from :meth:`HealthDataset.daily`), so it
reads the same whether the records came straight from an export or from the
DuckDB store.

**Per source, never blended.** As elsewhere in the pipeline, each source gets
its own baseline and its own weekly summary: WHOOP recovery and Oura readiness
are different scores, and RMSSD and SDNN are different statistics.

**Baselines are robust and strictly trailing.** A day is compared with the
median of the previous ``window`` days that have a value (default 28), scaled
by the median absolute deviation (MAD x 1.4826, which matches the standard
deviation for normal data). The median and MAD barely move when the baseline
itself contains a few sick or unusual days, which a mean and standard
deviation would absorb; and the day being scored is never part of its own
baseline. Each metric has a minimum scale so a run of near-identical values
(an integer resting HR that sat at 55 for a week) cannot turn a 1-bpm change
into a huge z-score.

**Only adverse deviations are flagged.** A metric declares which direction is
bad (low HRV, high resting HR, ...). An unusually *high* HRV is good news and
is not an anomaly worth a notification.

**Illness signal.** One metric out of range is often noise (a late meal, a
warm room). A day is marked as a possible illness signal when at least
``illness_min_markers`` (default 2) *physiological* markers — HRV, resting
HR, skin temperature, respiratory rate, SpO2 — are flagged on the same day.
The synthetic sample carries ground-truth illness days, and
``tests/test_insights.py`` checks the detector against them.

None of this is medical advice; it is a trend summary for one person's own data.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from typing import Any

from ..schema import HealthDataset, Source

#: Scale factor that makes the MAD a consistent estimator of the standard
#: deviation for normally distributed data.
MAD_TO_SD = 1.4826


class Direction(StrEnum):
    """Which deviation from baseline is adverse for a metric."""

    LOW = "low"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class Metric:
    """A daily metric the insights job tracks.

    ``key`` is the field name in :meth:`HealthDataset.daily` rows. ``adverse``
    is ``None`` for metrics that are summarised but never flagged (strain and
    steps are choices, not symptoms). ``min_scale`` floors the robust spread,
    in the metric's own unit. ``display_divisor`` converts the stored unit to
    the display unit (seconds to hours).
    """

    key: str
    label: str
    unit: str
    adverse: Direction | None
    min_scale: float
    physiological: bool = False
    decimals: int = 1
    display_divisor: float = 1.0

    def display(self, value: float) -> float:
        return round(value / self.display_divisor, self.decimals)


METRICS: tuple[Metric, ...] = (
    Metric("readiness_score", "Recovery / readiness", "", Direction.LOW, 5.0, decimals=0),
    Metric("hrv_rmssd_ms", "HRV (RMSSD)", "ms", Direction.LOW, 2.0, physiological=True),
    Metric("hrv_sdnn_ms", "HRV (SDNN)", "ms", Direction.LOW, 2.0, physiological=True),
    Metric("resting_hr_bpm", "Resting heart rate", "bpm", Direction.HIGH, 1.0, physiological=True),
    Metric("skin_temp_c", "Skin temperature", "°C", Direction.HIGH, 0.1, physiological=True),
    Metric(
        "skin_temp_deviation_c",
        "Skin temp deviation",
        "°C",
        Direction.HIGH,
        0.1,
        physiological=True,
        decimals=2,
    ),
    Metric(
        "sleep_respiratory_rate_bpm",
        "Respiratory rate",
        "br/min",
        Direction.HIGH,
        0.2,
        physiological=True,
    ),
    Metric("spo2_pct", "SpO2", "%", Direction.LOW, 0.3, physiological=True),
    Metric(
        "sleep_time_asleep_s",
        "Time asleep",
        "h",
        Direction.LOW,
        1200.0,
        decimals=2,
        display_divisor=3600.0,
    ),
    Metric("sleep_efficiency_pct", "Sleep efficiency", "%", Direction.LOW, 2.0, decimals=0),
    Metric("strain", "Day strain", "", None, 1.0),
    Metric("steps", "Steps", "", None, 500.0, decimals=0),
)
METRICS_BY_KEY: dict[str, Metric] = {m.key: m for m in METRICS}


@dataclass(frozen=True, slots=True)
class InsightOptions:
    """Tuning knobs, with defaults that suit daily wearable data."""

    window: int = 28
    min_history: int = 7
    threshold: float = 3.0
    rolling_days: int = 7
    illness_min_markers: int = 2

    def __post_init__(self) -> None:
        if self.window < 1 or self.rolling_days < 1:
            raise ValueError("window and rolling_days must be >= 1")
        if not 2 <= self.min_history <= self.window:
            raise ValueError("min_history must be between 2 and window")
        if self.threshold <= 0:
            raise ValueError("threshold must be > 0")
        if self.illness_min_markers < 1:
            raise ValueError("illness_min_markers must be >= 1")


DEFAULT_OPTIONS = InsightOptions()


# ------------------------------------------------------------------ baselines


@dataclass(frozen=True, slots=True)
class Baseline:
    """Robust centre and spread of a metric's trailing history."""

    median: float
    scale: float
    n: int

    def z(self, value: float) -> float:
        return (value - self.median) / self.scale


def robust_baseline(values: Sequence[float], *, min_scale: float) -> Baseline:
    """Median and floored MAD-based spread of ``values`` (must be non-empty)."""
    if not values:
        raise ValueError("robust_baseline needs at least one value")
    if min_scale <= 0:
        raise ValueError("min_scale must be > 0")
    median = statistics.median(values)
    mad = statistics.median(abs(v - median) for v in values)
    return Baseline(median=median, scale=max(MAD_TO_SD * mad, min_scale), n=len(values))


def trailing(series: Mapping[date, float], day: date, days: int) -> list[float]:
    """Values for the ``days`` calendar days strictly before ``day``, oldest first."""
    start = day - timedelta(days=days)
    return [series[d] for d in sorted(series) if start <= d < day]


def rolling_mean(series: Mapping[date, float], day: date, days: int) -> float | None:
    """Mean over the ``days`` calendar days ending on (and including) ``day``."""
    values = trailing(series, day + timedelta(days=1), days)
    return statistics.fmean(values) if values else None


@dataclass(frozen=True, slots=True)
class Anomaly:
    """One metric on one day, outside its trailing baseline in the adverse direction."""

    day: date
    source: Source
    metric: str
    value: float
    baseline: float
    z: float
    direction: Direction

    def to_dict(self) -> dict[str, Any]:
        m = METRICS_BY_KEY[self.metric]
        return {
            "day": self.day.isoformat(),
            "source": self.source.value,
            "metric": self.metric,
            "label": m.label,
            "unit": m.unit,
            "value": m.display(self.value),
            "baseline": m.display(self.baseline),
            "z": round(self.z, 1),
            "direction": self.direction.value,
        }


def detect_anomalies(
    series: Mapping[date, float],
    metric: Metric,
    source: Source,
    options: InsightOptions = DEFAULT_OPTIONS,
) -> list[Anomaly]:
    """Days where ``metric`` deviates adversely by at least ``options.threshold`` robust SDs."""
    if metric.adverse is None:
        return []
    found: list[Anomaly] = []
    for day in sorted(series):
        history = trailing(series, day, options.window)
        if len(history) < options.min_history:
            continue
        base = robust_baseline(history, min_scale=metric.min_scale)
        z = base.z(series[day])
        if metric.adverse is Direction.LOW:
            adverse = z <= -options.threshold
        else:
            adverse = z >= options.threshold
        if adverse:
            found.append(
                Anomaly(day, source, metric.key, series[day], base.median, z, metric.adverse)
            )
    return found


# ------------------------------------------------------------------ daily view


@dataclass(frozen=True, slots=True)
class DayInsight:
    """One source's metrics for one day, with rolling means and flags."""

    day: date
    source: Source
    values: Mapping[str, float]
    rolling: Mapping[str, float]
    anomalies: tuple[Anomaly, ...]
    workouts: int
    nap_count: int

    @property
    def illness_markers(self) -> tuple[str, ...]:
        return tuple(a.metric for a in self.anomalies if METRICS_BY_KEY[a.metric].physiological)

    def illness_signal(self, options: InsightOptions) -> bool:
        return len(self.illness_markers) >= options.illness_min_markers


DailyView = Mapping[str, Mapping[str, Mapping[str, Any]]]


def metric_series(daily: DailyView) -> dict[Source, dict[str, dict[date, float]]]:
    """``{source: {metric: {day: value}}}`` for every tracked metric present."""
    out: dict[Source, dict[str, dict[date, float]]] = {}
    for day_s, by_source in daily.items():
        day = date.fromisoformat(day_s)
        for source_s, row in by_source.items():
            per_metric = out.setdefault(Source(source_s), {})
            for m in METRICS:
                value = row.get(m.key)
                if isinstance(value, int | float) and not isinstance(value, bool):
                    per_metric.setdefault(m.key, {})[day] = float(value)
    return out


def analyse_days(
    daily: DailyView,
    options: InsightOptions = DEFAULT_OPTIONS,
) -> dict[Source, list[DayInsight]]:
    """Per-source, day-ordered insights for every day in ``daily``."""
    series = metric_series(daily)
    anomalies: dict[tuple[Source, date], list[Anomaly]] = {}
    for source, per_metric in series.items():
        for key, values in per_metric.items():
            for a in detect_anomalies(values, METRICS_BY_KEY[key], source, options):
                anomalies.setdefault((source, a.day), []).append(a)

    out: dict[Source, list[DayInsight]] = {}
    for day_s, by_source in daily.items():
        day = date.fromisoformat(day_s)
        for source_s, row in by_source.items():
            source = Source(source_s)
            per_metric = series.get(source, {})
            values = {k: s[day] for k, s in per_metric.items() if day in s}
            rolling = {
                k: mean
                for k, s in per_metric.items()
                if (mean := rolling_mean(s, day, options.rolling_days)) is not None
            }
            out.setdefault(source, []).append(
                DayInsight(
                    day=day,
                    source=source,
                    values=values,
                    rolling=rolling,
                    anomalies=tuple(anomalies.get((source, day), ())),
                    workouts=len(row.get("workouts", ())),
                    nap_count=int(row.get("nap_count", 0)),
                )
            )
    for days in out.values():
        days.sort(key=lambda d: d.day)
    return out


# ------------------------------------------------------------------ weekly view


def iso_week(day: date) -> str:
    """``YYYY-Www`` — the ISO week (Monday to Sunday) containing ``day``."""
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def week_bounds(week: str) -> tuple[date, date]:
    """Monday and Sunday of an ISO week string such as ``2026-W04``."""
    try:
        monday = date.fromisoformat(f"{week}-1")
    except ValueError as exc:
        raise ValueError(f"not an ISO week like 2026-W04: {week!r}") from exc
    return monday, monday + timedelta(days=6)


class Trend(StrEnum):
    """Week-over-week change, judged by the metric's adverse direction."""

    BETTER = "better"
    WORSE = "worse"
    STEADY = "steady"
    UP = "up"  # for metrics with no adverse direction
    DOWN = "down"


@dataclass(frozen=True, slots=True)
class MetricSummary:
    metric: str
    days: int
    mean: float
    low: float
    high: float
    previous_mean: float | None

    @property
    def delta(self) -> float | None:
        return None if self.previous_mean is None else self.mean - self.previous_mean

    @property
    def trend(self) -> Trend | None:
        delta = self.delta
        if delta is None:
            return None
        m = METRICS_BY_KEY[self.metric]
        if abs(delta) < m.min_scale:
            return Trend.STEADY
        if m.adverse is None:
            return Trend.UP if delta > 0 else Trend.DOWN
        worse = delta < 0 if m.adverse is Direction.LOW else delta > 0
        return Trend.WORSE if worse else Trend.BETTER

    def to_dict(self) -> dict[str, Any]:
        m = METRICS_BY_KEY[self.metric]
        delta = self.delta
        return {
            "metric": self.metric,
            "label": m.label,
            "unit": m.unit,
            "days": self.days,
            "mean": m.display(self.mean),
            "min": m.display(self.low),
            "max": m.display(self.high),
            "previous_mean": None if self.previous_mean is None else m.display(self.previous_mean),
            "delta": None if delta is None else m.display(delta),
            "trend": None if self.trend is None else self.trend.value,
        }


@dataclass(frozen=True, slots=True)
class WorkoutTotals:
    count: int = 0
    duration_s: int = 0
    by_sport: Mapping[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "duration_h": round(self.duration_s / 3600, 1),
            "by_sport": dict(sorted(self.by_sport.items())),
        }


@dataclass(frozen=True, slots=True)
class WeeklySummary:
    """One source's ISO week: metric statistics, flags and workouts."""

    source: Source
    week: str
    start: date
    end: date
    days: tuple[DayInsight, ...]
    metrics: tuple[MetricSummary, ...]
    workouts: WorkoutTotals
    illness_days: tuple[date, ...]
    narrative: str | None = None

    @property
    def days_with_data(self) -> int:
        return len(self.days)

    @property
    def complete(self) -> bool:
        return self.days_with_data == 7

    @property
    def anomalies(self) -> tuple[Anomaly, ...]:
        return tuple(a for d in self.days for a in d.anomalies)

    def metric(self, key: str) -> MetricSummary | None:
        return next((m for m in self.metrics if m.metric == key), None)

    def with_narrative(self, text: str | None) -> WeeklySummary:
        return WeeklySummary(
            self.source, self.week, self.start, self.end, self.days, self.metrics,
            self.workouts, self.illness_days, text,
        )  # fmt: skip

    def to_dict(self, *, include_days: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "source": self.source.value,
            "week": self.week,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "days_with_data": self.days_with_data,
            "complete": self.complete,
            "metrics": [m.to_dict() for m in self.metrics],
            "workouts": self.workouts.to_dict(),
            "naps": sum(d.nap_count for d in self.days),
            "anomalies": [a.to_dict() for a in self.anomalies],
            "illness_signal_days": [d.isoformat() for d in self.illness_days],
            "narrative": self.narrative,
        }
        if include_days:
            out["days"] = [
                {
                    "day": d.day.isoformat(),
                    "rolling": {
                        k: METRICS_BY_KEY[k].display(v) for k, v in sorted(d.rolling.items())
                    },
                    "flags": [a.metric for a in d.anomalies],
                }
                for d in self.days
            ]
        return out


def _summarise(
    key: str, week_days: Sequence[DayInsight], previous: Sequence[DayInsight]
) -> MetricSummary | None:
    values = [d.values[key] for d in week_days if key in d.values]
    if not values:
        return None
    before = [d.values[key] for d in previous if key in d.values]
    return MetricSummary(
        metric=key,
        days=len(values),
        mean=statistics.fmean(values),
        low=min(values),
        high=max(values),
        previous_mean=statistics.fmean(before) if before else None,
    )


def _workout_totals(
    dataset: HealthDataset, source: Source, start: date, end: date
) -> WorkoutTotals:
    sessions = [w for w in dataset.workouts if w.source is source and start <= w.day <= end]
    by_sport: dict[str, int] = {}
    for w in sessions:
        by_sport[w.sport.value] = by_sport.get(w.sport.value, 0) + 1
    return WorkoutTotals(len(sessions), sum(w.duration_s for w in sessions), by_sport)


def weekly_summaries(
    dataset: HealthDataset,
    options: InsightOptions = DEFAULT_OPTIONS,
    *,
    weeks: Iterable[str] | None = None,
) -> list[WeeklySummary]:
    """One :class:`WeeklySummary` per (source, ISO week), ordered by week then source.

    Baselines and rolling means use *all* of ``dataset``, so a single requested
    week still gets a full trailing history. ``weeks`` limits which weeks are
    returned; weeks without data for a source are skipped.
    """
    by_source = analyse_days(dataset.daily(), options)
    wanted = None if weeks is None else set(weeks)
    for week in wanted or ():
        week_bounds(week)  # validate early, with a clear error
    out: list[WeeklySummary] = []
    for source, days in by_source.items():
        grouped: dict[str, list[DayInsight]] = {}
        for d in days:
            grouped.setdefault(iso_week(d.day), []).append(d)
        for week, week_days in grouped.items():
            if wanted is not None and week not in wanted:
                continue
            start, end = week_bounds(week)
            previous = grouped.get(iso_week(start - timedelta(days=1)), [])
            metrics = tuple(
                s for m in METRICS if (s := _summarise(m.key, week_days, previous)) is not None
            )
            out.append(
                WeeklySummary(
                    source=source,
                    week=week,
                    start=start,
                    end=end,
                    days=tuple(week_days),
                    metrics=metrics,
                    workouts=_workout_totals(dataset, source, start, end),
                    illness_days=tuple(d.day for d in week_days if d.illness_signal(options)),
                )
            )
    out.sort(key=lambda s: (s.week, s.source.value))
    return out
