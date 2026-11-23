"""Oura → unified model.

Input is a JSON object holding Oura API v2 ``usercollection`` responses keyed by
endpoint name — ``sleep``, ``daily_readiness``, ``daily_activity``,
``daily_spo2`` and ``workout``. Each value may be the raw response
(``{"data": [...], "next_token": null}``) or just the list; missing endpoints
are fine. This is what you get by saving each endpoint's pages into one file.

Mapping notes:

* Oura sleep periods have a ``type``: ``long_sleep`` is the main night;
  ``sleep``, ``late_nap`` and ``rest`` are shorter periods and become naps;
  ``deleted`` periods are dropped.
* Resting HR and HRV come from the day's main (longest ``long_sleep``) period:
  Oura's ``lowest_heart_rate`` and ``average_hrv`` (RMSSD). The readiness score
  and ``temperature_deviation`` come from ``daily_readiness`` for the same day.
* Oura reports energy in kcal; it is converted to kJ.
* Days are recomputed from the local timestamps (wake date for sleep) instead
  of trusting Oura's ``day`` field, so every connector follows the same rule;
  daily documents (readiness, activity, SpO2) use their ``day`` as-is.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

from ..schema import (
    KCAL_TO_KJ,
    DailyActivity,
    DailyRecovery,
    HealthDataset,
    SleepSession,
    Source,
    Sport,
    Workout,
)
from .base import Connector, ConnectorError, parse_instant, read_json

Record = Mapping[str, Any]
ENDPOINTS = ("sleep", "daily_readiness", "daily_activity", "daily_spo2", "workout")
NAP_TYPES = frozenset({"sleep", "late_nap", "rest"})

_SPORT: dict[str, Sport] = {
    "running": Sport.RUNNING,
    "cycling": Sport.CYCLING,
    "walking": Sport.WALKING,
    "hiking": Sport.HIKING,
    "swimming": Sport.SWIMMING,
    "strength_training": Sport.STRENGTH,
    "weight_lifting": Sport.STRENGTH,
    "hiit": Sport.HIIT,
    "yoga": Sport.YOGA,
    "rowing": Sport.ROWING,
}


def _documents(export: Mapping[str, Any], endpoint: str) -> list[Record]:
    value = export.get(endpoint)
    if value is None:
        return []
    if isinstance(value, Mapping):
        value = value.get("data") or []
    if not isinstance(value, list):
        raise ConnectorError(f"oura {endpoint}: expected a list or {{'data': [...]}}")
    return value


def _kj(kcal: float | None) -> float | None:
    return None if kcal is None else round(kcal * KCAL_TO_KJ, 1)


class OuraConnector(Connector):
    source = Source.OURA

    def load(self, path: Path) -> HealthDataset:
        payload = read_json(path)
        if not isinstance(payload, Mapping):
            raise ConnectorError(f"{path}: expected an object keyed by Oura endpoint")
        return self.parse(payload)

    def parse(self, export: Mapping[str, Any]) -> HealthDataset:
        if not any(e in export for e in ENDPOINTS):
            raise ConnectorError(f"not an Oura export: none of {', '.join(ENDPOINTS)}")
        dataset = HealthDataset()

        # The day's main night (longest non-nap period), kept with its raw document
        # because resting HR and HRV are only on the raw Oura sleep record.
        main_sleep: dict[date, tuple[SleepSession, Record]] = {}
        for raw in _documents(export, "sleep"):
            if raw.get("type") == "deleted":
                continue
            rec = self._safe(f"sleep {raw.get('id')}", lambda raw=raw: self._sleep(raw))
            if isinstance(rec, SleepSession):
                dataset.add(rec)
                current = main_sleep.get(rec.day)
                if not rec.is_nap and (
                    current is None or rec.time_in_bed_s > current[0].time_in_bed_s
                ):
                    main_sleep[rec.day] = (rec, raw)

        readiness = {d["day"]: d for d in _documents(export, "daily_readiness") if "day" in d}
        spo2 = {d["day"]: d for d in _documents(export, "daily_spo2") if "day" in d}
        days = sorted({*readiness, *spo2, *(d.isoformat() for d in main_sleep)})
        for day in days:
            night = main_sleep.get(date.fromisoformat(day))
            rec = self._safe(
                f"recovery {day}",
                lambda day=day, night=night: self._recovery(
                    date.fromisoformat(day),
                    night[1] if night else None,
                    readiness.get(day),
                    spo2.get(day),
                ),
            )
            if rec is not None:
                dataset.add(rec)

        for raw in _documents(export, "daily_activity"):
            rec = self._safe(f"activity {raw.get('day')}", lambda raw=raw: self._activity(raw))
            if rec is not None:
                dataset.add(rec)
        for raw in _documents(export, "workout"):
            rec = self._safe(f"workout {raw.get('id')}", lambda raw=raw: self._workout(raw))
            if rec is not None:
                dataset.add(rec)
        return dataset

    def _sleep(self, raw: Record) -> SleepSession:
        start = parse_instant(raw["bedtime_start"])
        end = parse_instant(raw["bedtime_end"])
        in_bed = raw.get("time_in_bed")
        if in_bed is None:
            in_bed = int((end - start).total_seconds())
        return SleepSession(
            source=self.source,
            source_id=str(raw["id"]),
            start=start,
            end=end,
            is_nap=raw.get("type") in NAP_TYPES,
            time_in_bed_s=in_bed,
            time_asleep_s=raw.get("total_sleep_duration"),
            light_s=raw.get("light_sleep_duration"),
            deep_s=raw.get("deep_sleep_duration"),
            rem_s=raw.get("rem_sleep_duration"),
            awake_s=raw.get("awake_time"),
            efficiency_pct=raw.get("efficiency"),
            respiratory_rate_bpm=raw.get("average_breath"),
        )

    def _recovery(
        self,
        day: date,
        sleep: Record | None,
        readiness: Record | None,
        spo2: Record | None,
    ) -> DailyRecovery | None:
        sleep, readiness, spo2 = sleep or {}, readiness or {}, spo2 or {}
        record = DailyRecovery(
            source=self.source,
            day=day,
            readiness_score=readiness.get("score"),
            resting_hr_bpm=sleep.get("lowest_heart_rate"),
            hrv_rmssd_ms=sleep.get("average_hrv"),
            spo2_pct=(spo2.get("spo2_percentage") or {}).get("average"),
            skin_temp_deviation_c=readiness.get("temperature_deviation"),
        )
        has_data = any(
            getattr(record, f) is not None
            for f in ("readiness_score", "resting_hr_bpm", "hrv_rmssd_ms", "spo2_pct")
        )
        return record if has_data else None

    def _activity(self, raw: Record) -> DailyActivity:
        return DailyActivity(
            source=self.source,
            day=date.fromisoformat(raw["day"]),
            activity_score=raw.get("score"),
            steps=raw.get("steps"),
            active_energy_kj=_kj(raw.get("active_calories")),
            total_energy_kj=_kj(raw.get("total_calories")),
        )

    def _workout(self, raw: Record) -> Workout:
        activity = raw.get("activity")
        return Workout(
            source=self.source,
            source_id=str(raw["id"]),
            start=parse_instant(raw["start_datetime"]),
            end=parse_instant(raw["end_datetime"]),
            sport=_SPORT.get(str(activity).lower(), Sport.OTHER),
            source_sport=activity,
            energy_kj=_kj(raw.get("calories")),
            distance_m=raw.get("distance"),
        )
