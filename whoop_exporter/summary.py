"""Per-day aggregation of raw WHOOP records.

The output is a ``{"YYYY-MM-DD": {...metrics...}}`` mapping that downstream
dashboards consume. Days are keyed by the *start* date of each record as
reported by WHOOP (UTC ISO-8601), so a sleep that starts at 23:30 counts toward
that calendar day.
"""

from __future__ import annotations

from typing import Any

Record = dict[str, Any]
DailySummary = dict[str, dict[str, Any]]


def _score(record: Record) -> dict[str, Any]:
    # WHOOP returns "score": null while a record is still PENDING_SCORE or
    # UNSCORABLE, so ``record.get("score", {})`` is not enough.
    return record.get("score") or {}


def _day(timestamp: str | None) -> str:
    return (timestamp or "")[:10]


def _in_bed_milli(sleep: Record) -> int | None:
    score = _score(sleep)
    stages = score.get("stage_summary") or {}
    # WHOOP nests this under stage_summary; the top-level lookup is a
    # defensive fallback for older payloads.
    return stages.get("total_in_bed_time_milli") or score.get("total_in_bed_time_milli")


def _apply_sleep_metrics(entry: dict[str, Any], sleep: Record) -> None:
    score = _score(sleep)
    stages = score.get("stage_summary") or {}
    entry["sleep_performance_percentage"] = score.get("sleep_performance_percentage")
    entry["sleep_efficiency_percentage"] = score.get("sleep_efficiency_percentage")
    entry["sleep_consistency_percentage"] = score.get("sleep_consistency_percentage")
    # NOTE: the key name is kept for backward compatibility with existing
    # exports, but the value is time *in bed*, not time asleep. The unified
    # schema (health_pipeline.schema.SleepSession) exposes both explicitly.
    entry["total_sleep_time_milli"] = _in_bed_milli(sleep)
    entry["rem_sleep_time_milli"] = stages.get("total_rem_sleep_time_milli")
    entry["sws_sleep_time_milli"] = stages.get("total_slow_wave_sleep_time_milli")
    entry["light_sleep_time_milli"] = stages.get("total_light_sleep_time_milli")
    entry["awake_time_milli"] = stages.get("total_awake_time_milli")
    entry["respiratory_rate"] = score.get("respiratory_rate")


def build_daily_summary(
    cycles: list[Record],
    recovery: list[Record],
    sleep: list[Record],
    workouts: list[Record],
) -> DailySummary:
    """Collapse cycles, recovery, sleep and workouts into one dict per day.

    Records without a usable date are skipped rather than grouped under ``""``.

    Sleep: a day can hold several sleep records (naps, split nights). The
    nightly metrics come from the longest non-nap sleep, independent of the
    order WHOOP returns them in; naps are reported as ``nap_count`` and
    ``nap_time_milli`` (time in bed) so they are visible without polluting
    the night's numbers.
    """
    daily: DailySummary = {}

    def bucket(day: str) -> dict[str, Any] | None:
        if not day:
            return None
        return daily.setdefault(day, {})

    for cycle in cycles:
        if (entry := bucket(_day(cycle.get("start")))) is None:
            continue
        score = _score(cycle)
        entry["strain"] = score.get("strain")
        entry["kilojoule"] = score.get("kilojoule")
        entry["average_heart_rate"] = score.get("average_heart_rate")
        entry["max_heart_rate"] = score.get("max_heart_rate")

    for rec in recovery:
        # ``cycle_start`` is attached by the client when recovery is fetched per cycle.
        day = _day(rec.get("cycle_start")) or _day(rec.get("created_at"))
        if (entry := bucket(day)) is None:
            continue
        score = _score(rec)
        entry["recovery_score"] = score.get("recovery_score")
        entry["resting_heart_rate"] = score.get("resting_heart_rate")
        entry["hrv_rmssd_milli"] = score.get("hrv_rmssd_milli")
        entry["spo2_percentage"] = score.get("spo2_percentage")
        entry["skin_temp_celsius"] = score.get("skin_temp_celsius")

    sleeps_by_day: dict[str, list[Record]] = {}
    for s in sleep:
        if day := _day(s.get("start")):
            sleeps_by_day.setdefault(day, []).append(s)
    for day, records in sleeps_by_day.items():
        entry = daily.setdefault(day, {})
        naps = [s for s in records if s.get("nap")]
        nights = [s for s in records if not s.get("nap")]
        entry["nap_count"] = len(naps)
        entry["nap_time_milli"] = sum(_in_bed_milli(n) or 0 for n in naps)
        if nights:
            _apply_sleep_metrics(entry, max(nights, key=lambda s: _in_bed_milli(s) or 0))

    for w in workouts:
        if (entry := bucket(_day(w.get("start")))) is None:
            continue
        score = _score(w)
        entry.setdefault("workouts", []).append(
            {
                "sport_id": w.get("sport_id"),
                "sport_name": w.get("sport_name"),
                "strain": score.get("strain"),
                "average_heart_rate": score.get("average_heart_rate"),
                "max_heart_rate": score.get("max_heart_rate"),
                "kilojoule": score.get("kilojoule"),
                "distance_meter": score.get("distance_meter"),
                "zone_durations": score.get("zone_durations") or {},
            }
        )

    return daily


def format_summary_line(day: str, metrics: dict[str, Any]) -> str:
    """One human-readable log line per day."""

    def fmt(value: Any, spec: str = "") -> str:
        if value is None:
            return "—"
        return format(value, spec) if spec else str(value)

    return (
        f"{day}  Recovery: {fmt(metrics.get('recovery_score'))}%  "
        f"Strain: {fmt(metrics.get('strain'), '.1f')}  "
        f"RHR: {fmt(metrics.get('resting_heart_rate'))}  "
        f"HRV: {fmt(metrics.get('hrv_rmssd_milli'), '.0f')}  "
        f"Sleep: {fmt(metrics.get('sleep_performance_percentage'))}%"
    )
