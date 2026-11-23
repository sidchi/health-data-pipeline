"""WHOOP export → unified model.

Input is the JSON written by ``whoop.py`` / the Cloud Function (and by
:mod:`whoop_exporter.sample`): raw ``cycles``, ``recovery``, ``sleep`` and
``workouts`` lists in WHOOP API shape. WHOOP stores instants in UTC with a
separate ``timezone_offset``; records are converted to that offset so local
dates are correct.

Mapping notes:

* Records that are not ``SCORED`` keep their timing but carry no metrics.
  ``UNSCORABLE`` / ``PENDING_SCORE`` recoveries are dropped entirely, since a
  recovery row without numbers says nothing.
* Time asleep is light + slow-wave + REM. WHOOP's "slow wave sleep" maps to
  ``deep_s``.
* A recovery is attributed to the wake date of the sleep it was computed from
  (``sleep_id``); a cycle (day strain) to the local date of its midpoint,
  because a WHOOP cycle runs from falling asleep to falling asleep again and so
  usually *starts* on the previous evening.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..schema import (
    DailyActivity,
    DailyRecovery,
    HealthDataset,
    SleepSession,
    Source,
    Sport,
    Workout,
)
from .base import Connector, ConnectorError, parse_instant, parse_offset, read_json, seconds

Record = Mapping[str, Any]

# WHOOP sport names (as returned in ``sport_name``) and v1 sport ids.
_SPORT_BY_NAME: dict[str, Sport] = {
    "running": Sport.RUNNING,
    "cycling": Sport.CYCLING,
    "walking": Sport.WALKING,
    "hiking/rucking": Sport.HIKING,
    "hiking": Sport.HIKING,
    "swimming": Sport.SWIMMING,
    "weightlifting": Sport.STRENGTH,
    "powerlifting": Sport.STRENGTH,
    "functional-fitness": Sport.HIIT,
    "hiit": Sport.HIIT,
    "spin": Sport.CYCLING,
    "yoga": Sport.YOGA,
    "rowing": Sport.ROWING,
}
# Fallback for payloads without ``sport_name``; only ids this repo already uses
# (see ``whoop_exporter.sample.SPORTS``) — unknown ids map to Sport.OTHER.
_SPORT_BY_ID: dict[int, Sport] = {
    0: Sport.RUNNING,
    1: Sport.CYCLING,
    44: Sport.YOGA,
    48: Sport.HIIT,
    63: Sport.WALKING,
}


def _scored(record: Record) -> dict[str, Any]:
    if record.get("score_state", "SCORED") != "SCORED":
        return {}
    return dict(record.get("score") or {})


def _tz(record: Record) -> timezone:
    return parse_offset(record.get("timezone_offset"))


def _local(record: Record, key: str) -> datetime:
    return parse_instant(record[key], _tz(record))


def whoop_sport(sport_name: str | None, sport_id: int | None) -> Sport:
    if sport_name and (sport := _SPORT_BY_NAME.get(sport_name.lower())):
        return sport
    if sport_id is not None and (sport := _SPORT_BY_ID.get(sport_id)):
        return sport
    return Sport.OTHER


class WhoopConnector(Connector):
    source = Source.WHOOP

    def load(self, path: Path) -> HealthDataset:
        payload = read_json(path)
        if not isinstance(payload, Mapping):
            raise ConnectorError(f"{path}: expected a WHOOP export object")
        return self.parse(payload)

    def parse(self, export: Mapping[str, Any]) -> HealthDataset:
        if not any(k in export for k in ("cycles", "sleep", "recovery", "workouts")):
            raise ConnectorError("not a WHOOP export: no cycles/sleep/recovery/workouts")
        dataset = HealthDataset()
        sleeps: list[Record] = list(export.get("sleep") or [])
        wake_by_sleep_id: dict[str, date] = {}

        for raw in sleeps:
            rec = self._safe(f"sleep {raw.get('id')}", lambda raw=raw: self._sleep(raw))
            if isinstance(rec, SleepSession):
                dataset.add(rec)
                wake_by_sleep_id[str(raw.get("id"))] = rec.day

        cycle_day: dict[str, date] = {}
        for raw in export.get("cycles") or []:
            rec = self._safe(f"cycle {raw.get('id')}", lambda raw=raw: self._cycle(raw))
            if isinstance(rec, DailyActivity):
                dataset.add(rec)
                cycle_day[str(raw.get("id"))] = rec.day

        for raw in export.get("recovery") or []:
            rec = self._safe(
                f"recovery for cycle {raw.get('cycle_id')}",
                lambda raw=raw: self._recovery(raw, wake_by_sleep_id, cycle_day),
            )
            if rec is not None:
                dataset.add(rec)

        for raw in export.get("workouts") or []:
            rec = self._safe(f"workout {raw.get('id')}", lambda raw=raw: self._workout(raw))
            if rec is not None:
                dataset.add(rec)
        return dataset

    def _sleep(self, raw: Record) -> SleepSession:
        start, end = _local(raw, "start"), _local(raw, "end")
        score = _scored(raw)
        stages = score.get("stage_summary") or {}
        in_bed = seconds(stages.get("total_in_bed_time_milli"))
        if in_bed is None:
            in_bed = int((end - start).total_seconds())
        light = seconds(stages.get("total_light_sleep_time_milli"))
        deep = seconds(stages.get("total_slow_wave_sleep_time_milli"))
        rem = seconds(stages.get("total_rem_sleep_time_milli"))
        asleep = None if None in (light, deep, rem) else light + deep + rem  # type: ignore[operator]
        return SleepSession(
            source=self.source,
            source_id=str(raw["id"]),
            start=start,
            end=end,
            is_nap=bool(raw.get("nap")),
            time_in_bed_s=in_bed,
            time_asleep_s=asleep,
            light_s=light,
            deep_s=deep,
            rem_s=rem,
            awake_s=seconds(stages.get("total_awake_time_milli")),
            efficiency_pct=score.get("sleep_efficiency_percentage"),
            respiratory_rate_bpm=score.get("respiratory_rate"),
        )

    def _cycle(self, raw: Record) -> DailyActivity:
        start = _local(raw, "start")
        end = _local(raw, "end") if raw.get("end") else start + timedelta(hours=24)
        midpoint = start + (end - start) / 2
        score = _scored(raw)
        return DailyActivity(
            source=self.source,
            day=midpoint.date(),
            strain=score.get("strain"),
            total_energy_kj=score.get("kilojoule"),
            average_hr_bpm=score.get("average_heart_rate"),
            max_hr_bpm=score.get("max_heart_rate"),
        )

    def _recovery(
        self, raw: Record, wake_by_sleep_id: Mapping[str, date], cycle_day: Mapping[str, date]
    ) -> DailyRecovery | None:
        score = _scored(raw)
        if not score:
            return None
        day = wake_by_sleep_id.get(str(raw.get("sleep_id"))) or cycle_day.get(
            str(raw.get("cycle_id"))
        )
        if day is None:
            anchor = raw.get("cycle_start") or raw["created_at"]
            day = parse_instant(anchor, _tz(raw)).date()
        return DailyRecovery(
            source=self.source,
            day=day,
            readiness_score=score.get("recovery_score"),
            resting_hr_bpm=score.get("resting_heart_rate"),
            hrv_rmssd_ms=score.get("hrv_rmssd_milli"),
            spo2_pct=score.get("spo2_percentage"),
            skin_temp_c=score.get("skin_temp_celsius"),
        )

    def _workout(self, raw: Record) -> Workout:
        score = _scored(raw)
        name = raw.get("sport_name")
        return Workout(
            source=self.source,
            source_id=str(raw["id"]),
            start=_local(raw, "start"),
            end=_local(raw, "end"),
            sport=whoop_sport(name, raw.get("sport_id")),
            source_sport=name if name is not None else f"sport_id:{raw.get('sport_id')}",
            energy_kj=score.get("kilojoule"),
            average_hr_bpm=score.get("average_heart_rate"),
            max_hr_bpm=score.get("max_heart_rate"),
            distance_m=score.get("distance_meter"),
            strain=score.get("strain"),
        )
