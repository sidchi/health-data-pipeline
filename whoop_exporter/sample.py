"""Synthetic, WHOOP-shaped sample data.

The public repository must never contain a real export, so everything that
needs "realistic" data — demos, the dashboard (roadmap item 7), the insights
job (item 8), load tests for storage (item 5) — reads data from this module
instead.

The generator is a small, seeded simulation rather than independent random
numbers per field, so the data has the structure real wearable data has:

* **Training plan.** A weekly schedule of runs, intervals, strength, a long
  ride and rest days. Low recovery downgrades or skips hard sessions, the way a
  sensible athlete would.
* **Load and recovery.** Day strain feeds an exponentially weighted training
  load. Next-morning HRV falls as load rises and as sleep falls short of need;
  resting heart rate moves opposite to HRV; the recovery score is a function of
  both plus sleep performance.
* **Sleep.** Bedtime and wake time drift around a routine with later weekends;
  sleep need grows with recent strain and accumulated debt; stage minutes always
  sum exactly to time in bed. Short nights make an afternoon nap more likely.
* **Illness.** Rare multi-day episodes lower HRV and raise resting heart rate,
  skin temperature and respiratory rate. Their dates are returned as ground
  truth (``SampleDataset.illness_days``) so anomaly detection can be evaluated.
* **Score states.** A configurable share of nights is ``UNSCORABLE`` and the
  final cycle is still open (``end: null``, ``PENDING_SCORE``), as in a live
  export.

Records use the field names and nesting of the WHOOP developer API (v1 cycles,
v2 sleep/recovery/workouts), so the same data can be served by the fake API in
``tests/whoop_api.py`` or fed straight to :func:`build_daily_summary`.

Run ``python -m whoop_exporter.sample --help`` for the command-line interface.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .client import whoop_timestamp
from .summary import build_daily_summary

Record = dict[str, Any]

GENERATOR_NAME = "whoop_exporter.sample"
HOUR_MS = 3_600_000
MINUTE_MS = 60_000
# Typical day strain for the default plan; strain above it costs recovery.
LOAD_REFERENCE = 11.5

# The committed sample consumed by demos and the dashboard. Regenerate with
# ``python -m whoop_exporter.sample --sample``; a test keeps it in sync.
SAMPLE_EXPORT_PATH = (
    Path(__file__).resolve().parent.parent / "sample_data" / "whoop_sample_export.json"
)
SAMPLE_START = date(2026, 1, 5)
SAMPLE_DAYS = 28
SAMPLE_SEED = 13  # chosen so the month includes an illness episode, naps and an UNSCORABLE night

# WHOOP sport ids for the activities the plan uses.
SPORTS: dict[str, int] = {
    "running": 0,
    "cycling": 1,
    "yoga": 44,
    "functional-fitness": 48,
    "walking": 63,
}


@dataclass(frozen=True)
class Session:
    """One planned workout: what, how hard, how long, and when in the day."""

    sport: str
    intensity: str  # "easy" | "moderate" | "hard" | "long"
    minutes: int
    evening: bool = False


# Monday = 0. Wednesday is a full rest day.
DEFAULT_PLAN: dict[int, tuple[Session, ...]] = {
    0: (Session("running", "moderate", 45),),
    1: (Session("functional-fitness", "moderate", 50, evening=True),),
    2: (),
    3: (Session("running", "hard", 50),),
    4: (Session("yoga", "easy", 40, evening=True),),
    5: (Session("cycling", "long", 120),),
    6: (Session("running", "easy", 35), Session("walking", "easy", 45, evening=True)),
}

# Workout strain range, share of max HR, and HR-zone weights (zones 0..5).
_INTENSITY: dict[str, tuple[tuple[float, float], float, tuple[float, ...]]] = {
    "easy": ((5.5, 9.0), 0.62, (0.05, 0.40, 0.45, 0.10, 0.00, 0.00)),
    "moderate": ((9.0, 13.0), 0.75, (0.02, 0.10, 0.35, 0.40, 0.12, 0.01)),
    "hard": ((13.0, 16.5), 0.82, (0.02, 0.08, 0.20, 0.30, 0.28, 0.12)),
    "long": ((13.5, 17.5), 0.73, (0.02, 0.10, 0.40, 0.38, 0.09, 0.01)),
}
_ZONE_KEYS = (
    "zone_zero_milli",
    "zone_one_milli",
    "zone_two_milli",
    "zone_three_milli",
    "zone_four_milli",
    "zone_five_milli",
)
# Metres per second for sports that report distance.
_SPEED = {"running": (2.7, 3.4), "cycling": (6.8, 8.2), "walking": (1.3, 1.6)}


@dataclass(frozen=True)
class Athlete:
    """Baseline physiology and routine of the simulated person."""

    user_id: int = 10000001
    first_name: str = "Sample"
    last_name: str = "Athlete"
    email: str = "sample.athlete@example.com"
    height_meter: float = 1.78
    weight_kilogram: float = 74.5
    max_heart_rate: int = 192
    hrv_baseline_milli: float = 55.0
    resting_hr_baseline: float = 54.0
    sleep_need_hours: float = 7.6
    wake_time_local: float = 6.25  # 06:15
    timezone_offset: str = "+05:30"
    plan: dict[int, tuple[Session, ...]] = field(default_factory=lambda: dict(DEFAULT_PLAN))

    @property
    def tz(self) -> timezone:
        sign = -1 if self.timezone_offset.startswith("-") else 1
        hours, minutes = self.timezone_offset.lstrip("+-").split(":")
        return timezone(sign * timedelta(hours=int(hours), minutes=int(minutes)))


@dataclass(frozen=True)
class GeneratorOptions:
    """Knobs for the rarer events. Defaults aim for "a believable few months"."""

    illness_rate: float = 0.012  # chance per healthy day that an episode starts
    unscorable_rate: float = 0.02  # share of recoveries WHOOP could not score
    base_nap_rate: float = 0.06  # nap chance after a normal night
    skip_rate: float = 0.08  # chance of skipping a planned session for no reason
    open_last_cycle: bool = True  # last cycle still in progress, like a live export


@dataclass
class SampleDataset:
    """Everything one simulated export contains, plus ground-truth labels."""

    seed: int
    athlete: Athlete
    start: date
    days: int
    cycles: list[Record]
    sleep: list[Record]
    recovery: list[Record]
    workouts: list[Record]
    illness_days: list[str]

    @property
    def profile(self) -> Record:
        a = self.athlete
        return {
            "user_id": a.user_id,
            "email": a.email,
            "first_name": a.first_name,
            "last_name": a.last_name,
        }

    @property
    def body_measurements(self) -> Record:
        a = self.athlete
        return {
            "height_meter": a.height_meter,
            "weight_kilogram": a.weight_kilogram,
            "max_heart_rate": a.max_heart_rate,
        }

    def recovery_by_cycle(self) -> dict[str, Record]:
        """Recovery keyed by cycle id, as served by ``GET /v2/cycle/{id}/recovery``."""
        return {str(r["cycle_id"]): r for r in self.recovery}

    def to_export(self) -> dict[str, Any]:
        """The same document :meth:`WhoopClient.fetch_export` produces."""
        cycle_start = {c["id"]: c["start"] for c in self.cycles}
        # The client attaches ``cycle_start`` to each recovery it fetches.
        recovery = [{**r, "cycle_start": cycle_start[r["cycle_id"]]} for r in self.recovery]
        period_start = datetime.combine(self.start, datetime.min.time(), self.athlete.tz)
        period_end = period_start + timedelta(days=self.days)
        return {
            "export_date": period_end.astimezone(UTC).isoformat(),
            "period": {
                "start": period_start.astimezone(UTC).isoformat(),
                "end": period_end.astimezone(UTC).isoformat(),
                "days": self.days,
            },
            "profile": self.profile,
            "body_measurements": self.body_measurements,
            "cycles": self.cycles,
            "recovery": recovery,
            "sleep": self.sleep,
            "workouts": self.workouts,
            "summary": build_daily_summary(self.cycles, recovery, self.sleep, self.workouts),
            "synthetic": {
                "generator": GENERATOR_NAME,
                "seed": self.seed,
                "illness_days": self.illness_days,
            },
        }

    def write_api_fixtures(self, directory: Path) -> list[Path]:
        """Write API-shaped files that ``tests/whoop_api.FakeWhoopApi`` can serve."""
        directory.mkdir(parents=True, exist_ok=True)
        files = {
            "cycles.json": {"records": self.cycles},
            "sleep.json": {"records": self.sleep},
            "workouts.json": {"records": self.workouts},
            "recovery_by_cycle.json": self.recovery_by_cycle(),
            "profile.json": self.profile,
            "body_measurement.json": self.body_measurements,
        }
        written = []
        for name, payload in files.items():
            path = directory / name
            path.write_text(dump_json(payload), encoding="utf-8")
            written.append(path)
        return written


# ----------------------------------------------------------------- simulation


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _ts(dt: datetime) -> str:
    return whoop_timestamp(dt)


class _Simulator:
    """Day-by-day state machine. Not part of the public API; use :func:`generate`."""

    def __init__(self, seed: int, athlete: Athlete, options: GeneratorOptions) -> None:
        self.seed = seed
        self.rng = random.Random(seed)  # noqa: S311 - reproducible simulation, not crypto
        self.a = athlete
        self.o = options
        self.tz = athlete.tz
        self.ln_hrv_base = math.log(athlete.hrv_baseline_milli)
        self.need_ms = round(athlete.sleep_need_hours * HOUR_MS)
        # Mutable state carried from one day to the next.
        self.load = LOAD_REFERENCE  # EWMA of day strain
        self.prev_strain = LOAD_REFERENCE
        self.sleep_debt_ms = 0
        self.hrv_noise = 0.0  # AR(1) day-to-day HRV variation, in log space
        self.ill_days_left = 0
        self.ids = {"cycle": 100_000, "sleep_v1": 400_000, "workout_v1": 500_000}

    # -- helpers -----------------------------------------------------------

    def _uuid(self) -> str:
        return str(uuid.UUID(int=self.rng.getrandbits(128), version=4))

    def _next_id(self, kind: str) -> int:
        self.ids[kind] += 1
        return self.ids[kind]

    def _local(self, day: date, hour: float) -> datetime:
        midnight = datetime.combine(day, datetime.min.time(), self.tz)
        return (midnight + timedelta(hours=hour)).replace(microsecond=0)

    def _wake(self, day: date) -> datetime:
        weekend = day.weekday() >= 5
        hour = self.a.wake_time_local + (0.9 if weekend else 0.0) + self.rng.gauss(0, 0.3)
        # Bounded so two wake-ups never land on the same UTC date.
        return self._local(day, _clip(hour, self.a.wake_time_local - 0.5, 10.0))

    # -- main loop ---------------------------------------------------------

    def run(self, start: date, days: int) -> SampleDataset:
        if days < 1:
            raise ValueError("days must be >= 1")
        wakes = [self._wake(start + timedelta(days=i)) for i in range(days + 1)]
        cycles: list[Record] = []
        sleeps: list[Record] = []
        recoveries: list[Record] = []
        workouts: list[Record] = []
        illness_days: list[str] = []

        for i in range(days):
            day = start + timedelta(days=i)
            ill = self._update_illness()
            if ill:
                illness_days.append(day.isoformat())
            cycle_id = self._next_id("cycle")
            cycle_start, cycle_end = wakes[i], wakes[i + 1]

            night = self._night_sleep(cycle_id, cycle_start, day, ill)
            sleeps.append(night)
            recovery = self._recovery(cycle_id, cycle_start, night, ill)
            recoveries.append(recovery)

            recovery_score = (recovery["score"] or {}).get("recovery_score")
            day_workouts = self._workouts(day, cycle_start, cycle_end, recovery_score, ill)
            workouts.extend(day_workouts)

            nap = self._maybe_nap(cycle_id, day, night)
            if nap:
                sleeps.append(nap)

            is_open = self.o.open_last_cycle and i == days - 1
            cycles.append(self._cycle(cycle_id, cycle_start, cycle_end, day_workouts, is_open))

        return SampleDataset(
            seed=self.seed,
            athlete=self.a,
            start=start,
            days=days,
            cycles=cycles,
            sleep=sleeps,
            recovery=recoveries,
            workouts=workouts,
            illness_days=illness_days,
        )

    def _update_illness(self) -> bool:
        if self.ill_days_left == 0 and self.rng.random() < self.o.illness_rate:
            self.ill_days_left = self.rng.randint(3, 5)
        if self.ill_days_left:
            self.ill_days_left -= 1
            return True
        return False

    # -- sleep ---------------------------------------------------------------

    def _sleep_needed(self) -> Record:
        strain_ms = round(max(0.0, self.prev_strain - LOAD_REFERENCE) * 4 * MINUTE_MS)
        debt_ms = round(min(self.sleep_debt_ms * 0.5, 1.5 * HOUR_MS))
        return {
            "baseline_milli": self.need_ms,
            "need_from_sleep_debt_milli": debt_ms,
            "need_from_recent_strain_milli": strain_ms,
            "need_from_recent_nap_milli": 0,
        }

    def _stages(self, in_bed_ms: int, awake_share: float, nap: bool) -> Record:
        rng = self.rng
        awake_ms = round(in_bed_ms * awake_share)
        asleep_ms = in_bed_ms - awake_ms
        sws_ms = round(asleep_ms * _clip(rng.gauss(0.30 if nap else 0.21, 0.03), 0.1, 0.4))
        rem_ms = 0 if nap else round(asleep_ms * _clip(rng.gauss(0.24, 0.03), 0.12, 0.35))
        return {
            "total_in_bed_time_milli": in_bed_ms,
            "total_awake_time_milli": awake_ms,
            "total_no_data_time_milli": 0,
            "total_light_sleep_time_milli": asleep_ms - sws_ms - rem_ms,
            "total_slow_wave_sleep_time_milli": sws_ms,
            "total_rem_sleep_time_milli": rem_ms,
            "sleep_cycle_count": max(1, round(asleep_ms / (90 * MINUTE_MS))),
            "disturbance_count": rng.randint(0, 3) if nap else rng.randint(5, 14),
        }

    def _night_sleep(self, cycle_id: int, wake: datetime, day: date, ill: bool) -> Record:
        rng = self.rng
        # Friday and Saturday nights end on a weekend morning: longer lie-in.
        weekend_morning = day.weekday() >= 5
        hours = rng.gauss(
            self.a.sleep_need_hours
            + 0.3
            + (0.45 if weekend_morning else 0.0)
            + 0.03 * (self.prev_strain - LOAD_REFERENCE)
            + (0.5 if ill else 0.0),
            0.55,
        )
        in_bed_ms = round(_clip(hours, 4.5, 10.5) * HOUR_MS / MINUTE_MS) * MINUTE_MS
        awake_share = _clip(rng.gauss(0.085 + (0.05 if ill else 0.0), 0.02), 0.03, 0.25)
        stages = self._stages(in_bed_ms, awake_share, nap=False)
        needed = self._sleep_needed()

        asleep_ms = in_bed_ms - stages["total_awake_time_milli"]
        need_total = sum(needed.values())
        performance = min(100, round(100 * asleep_ms / need_total))
        # Debt decays by half each night and grows by any shortfall against need.
        self.sleep_debt_ms = max(0, self.sleep_debt_ms // 2 + need_total - asleep_ms)

        start = wake - timedelta(milliseconds=in_bed_ms)
        usual_bed = self.a.wake_time_local - self.a.sleep_need_hours
        bed_hour = (start.astimezone(self.tz).hour + start.astimezone(self.tz).minute / 60) % 24
        drift = abs(((bed_hour - usual_bed) + 12) % 24 - 12)
        consistency = round(_clip(95 - 22 * drift + rng.gauss(0, 3), 35, 99))

        return {
            "id": self._uuid(),
            "cycle_id": cycle_id,
            "v1_id": self._next_id("sleep_v1"),
            "user_id": self.a.user_id,
            "created_at": _ts(wake),
            "updated_at": _ts(wake),
            "start": _ts(start),
            "end": _ts(wake),
            "timezone_offset": self.a.timezone_offset,
            "nap": False,
            "score_state": "SCORED",
            "score": {
                "stage_summary": stages,
                "sleep_needed": needed,
                "respiratory_rate": round(rng.gauss(15.2, 0.3) + (1.3 if ill else 0.0), 1),
                "sleep_performance_percentage": performance,
                "sleep_consistency_percentage": consistency,
                "sleep_efficiency_percentage": round(100 * asleep_ms / in_bed_ms, 1),
            },
        }

    def _maybe_nap(self, cycle_id: int, day: date, night: Record) -> Record | None:
        performance = night["score"]["sleep_performance_percentage"]
        chance = self.o.base_nap_rate + (0.25 if performance < 70 else 0.0)
        if self.rng.random() >= chance:
            return None
        start = self._local(day, 14.0 + self.rng.uniform(0, 1.5))
        in_bed_ms = self.rng.randint(20, 50) * MINUTE_MS
        end = start + timedelta(milliseconds=in_bed_ms)
        stages = self._stages(in_bed_ms, _clip(self.rng.gauss(0.15, 0.04), 0.05, 0.3), nap=True)
        asleep_ms = in_bed_ms - stages["total_awake_time_milli"]
        return {
            "id": self._uuid(),
            "cycle_id": cycle_id,
            "v1_id": self._next_id("sleep_v1"),
            "user_id": self.a.user_id,
            "created_at": _ts(end),
            "updated_at": _ts(end),
            "start": _ts(start),
            "end": _ts(end),
            "timezone_offset": self.a.timezone_offset,
            "nap": True,
            "score_state": "SCORED",
            "score": {
                "stage_summary": stages,
                "sleep_needed": night["score"]["sleep_needed"],
                "respiratory_rate": round(self.rng.gauss(15.0, 0.3), 1),
                # WHOOP does not grade naps on performance or consistency.
                "sleep_performance_percentage": None,
                "sleep_consistency_percentage": None,
                "sleep_efficiency_percentage": round(100 * asleep_ms / in_bed_ms, 1),
            },
        }

    # -- recovery ------------------------------------------------------------

    def _recovery(self, cycle_id: int, cycle_start: datetime, night: Record, ill: bool) -> Record:
        rng = self.rng
        performance = night["score"]["sleep_performance_percentage"]
        self.hrv_noise = 0.55 * self.hrv_noise + rng.gauss(0, 0.07)
        ln_hrv = (
            self.ln_hrv_base
            + self.hrv_noise
            - 0.015 * (self.prev_strain - LOAD_REFERENCE)
            - 0.015 * (self.load - LOAD_REFERENCE)
            + 0.004 * (performance - 85)
            - (0.28 if ill else 0.0)
        )
        hrv = math.exp(ln_hrv)
        delta = ln_hrv - self.ln_hrv_base
        rhr = round(self.a.resting_hr_baseline - 9 * delta + (5 if ill else 0) + rng.gauss(0, 1))
        score = round(
            _clip(
                66
                + 110 * delta
                - 1.5 * (rhr - self.a.resting_hr_baseline)
                + 0.35 * (performance - 85)
                + rng.gauss(0, 4),
                1,
                99,
            )
        )
        unscorable = rng.random() < self.o.unscorable_rate
        return {
            "cycle_id": cycle_id,
            "sleep_id": night["id"],
            "user_id": self.a.user_id,
            "created_at": _ts(cycle_start),
            "updated_at": _ts(cycle_start),
            "score_state": "UNSCORABLE" if unscorable else "SCORED",
            "score": None
            if unscorable
            else {
                "user_calibrating": False,
                "recovery_score": score,
                "resting_heart_rate": rhr,
                "hrv_rmssd_milli": round(hrv, 1),
                "spo2_percentage": round(
                    _clip(rng.gauss(96.8, 0.5) - (0.9 if ill else 0), 92, 99.9), 1
                ),
                "skin_temp_celsius": round(rng.gauss(33.7, 0.15) + (0.7 if ill else 0.0), 1),
            },
        }

    # -- strain --------------------------------------------------------------

    def _adjust(self, session: Session, recovery: int | None, ill: bool) -> Session | None:
        """Apply the athlete's judgement: rest when sick, back off when recovery is red."""
        if ill or self.rng.random() < self.o.skip_rate:
            return None
        if recovery is not None and recovery < 34 and session.intensity in ("hard", "long"):
            if self.rng.random() < 0.5:
                return None
            return Session(session.sport, "easy", max(25, session.minutes // 2), session.evening)
        return session

    def _workouts(
        self,
        day: date,
        cycle_start: datetime,
        cycle_end: datetime,
        recovery: int | None,
        ill: bool,
    ) -> list[Record]:
        out = []
        for planned in self.a.plan.get(day.weekday(), ()):
            session = self._adjust(planned, recovery, ill)
            if session is None:
                continue
            if session.evening:
                start = self._local(day, 18.25 + self.rng.uniform(0, 1.0))
            else:
                start = cycle_start + timedelta(minutes=self.rng.randint(30, 60))
            minutes = max(15, round(session.minutes * self.rng.uniform(0.85, 1.15)))
            end = start + timedelta(minutes=minutes)
            if end >= cycle_end:  # never spill into the next cycle
                continue
            out.append(self._workout(session, start, end, minutes))
        return out

    def _workout(self, session: Session, start: datetime, end: datetime, minutes: int) -> Record:
        rng = self.rng
        (low, high), hr_share, zone_weights = _INTENSITY[session.intensity]
        strain = round(rng.uniform(low, high), 1)
        avg_hr = round(self.a.max_heart_rate * hr_share + rng.gauss(0, 3))
        max_hr = min(self.a.max_heart_rate, avg_hr + rng.randint(15, 32))
        duration_ms = minutes * MINUTE_MS
        jittered = [w * rng.uniform(0.8, 1.2) for w in zone_weights]
        zones = [round(duration_ms * w / sum(jittered)) for w in jittered]
        zones[2] += duration_ms - sum(zones)  # absorb rounding so zones sum to duration
        distance = None
        if session.sport in _SPEED:
            distance = round(minutes * 60 * rng.uniform(*_SPEED[session.sport]), 1)
        return {
            "id": self._uuid(),
            "v1_id": self._next_id("workout_v1"),
            "user_id": self.a.user_id,
            "created_at": _ts(end),
            "updated_at": _ts(end),
            "start": _ts(start),
            "end": _ts(end),
            "timezone_offset": self.a.timezone_offset,
            "sport_name": session.sport,
            "sport_id": SPORTS[session.sport],
            "score_state": "SCORED",
            "score": {
                "strain": strain,
                "average_heart_rate": avg_hr,
                "max_heart_rate": max_hr,
                "kilojoule": round(minutes * 0.57 * max(0, avg_hr - 60), 1),
                "percent_recorded": 100,
                "distance_meter": distance,
                "altitude_gain_meter": round(rng.uniform(5, 15) * minutes / 10, 1)
                if distance
                else None,
                "altitude_change_meter": round(rng.gauss(0, 2), 1) if distance else None,
                "zone_durations": dict(zip(_ZONE_KEYS, zones, strict=True)),
            },
        }

    def _cycle(
        self,
        cycle_id: int,
        start: datetime,
        end: datetime,
        day_workouts: list[Record],
        is_open: bool,
    ) -> Record:
        rng = self.rng
        background = _clip(rng.gauss(7.0, 1.2), 3.0, 11.0)
        workout_strains = [w["score"]["strain"] for w in day_workouts]
        # Strain is logarithmic (0-21); stacking sessions adds less than their sum.
        strain = round(min(20.9, math.sqrt(background**2 + sum(s**2 for s in workout_strains))), 1)
        self.prev_strain = strain
        self.load = 0.75 * self.load + 0.25 * strain

        if is_open:
            score_state, score, end_ts = "PENDING_SCORE", None, None
        else:
            score_state, end_ts = "SCORED", _ts(end)
            workout_kj = sum(w["score"]["kilojoule"] for w in day_workouts)
            max_hr = max(
                [w["score"]["max_heart_rate"] for w in day_workouts] + [rng.randint(112, 138)]
            )
            score = {
                "strain": strain,
                "kilojoule": round(rng.gauss(7600, 250) + 180 * (background - 7) + workout_kj, 1),
                "average_heart_rate": round(60 + 0.7 * strain + rng.gauss(0, 1.5)),
                "max_heart_rate": max_hr,
            }
        return {
            "id": cycle_id,
            "user_id": self.a.user_id,
            "created_at": _ts(start),
            "updated_at": _ts(end) if end_ts else _ts(start),
            "start": _ts(start),
            "end": end_ts,
            "timezone_offset": self.a.timezone_offset,
            "score_state": score_state,
            "score": score,
        }


# ------------------------------------------------------------------ public API


def generate(
    start: date,
    days: int,
    *,
    seed: int = 0,
    athlete: Athlete | None = None,
    options: GeneratorOptions | None = None,
) -> SampleDataset:
    """Simulate ``days`` days of WHOOP data starting on local date ``start``.

    The same ``seed`` (and arguments) always yields byte-identical output.
    """
    simulator = _Simulator(seed, athlete or Athlete(), options or GeneratorOptions())
    return simulator.run(start, days)


def default_sample() -> SampleDataset:
    """The dataset behind ``sample_data/whoop_sample_export.json``."""
    return generate(SAMPLE_START, SAMPLE_DAYS, seed=SAMPLE_SEED)


def dump_json(payload: Any) -> str:
    """Stable JSON: sorted keys, two-space indent, trailing newline."""
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m whoop_exporter.sample",
        description="Generate synthetic, WHOOP-shaped sleep, recovery, strain and workout data.",
    )
    parser.add_argument(
        "--start",
        type=date.fromisoformat,
        default=date(2026, 1, 1),
        help="first local date, YYYY-MM-DD (default: 2026-01-01)",
    )
    parser.add_argument("--days", type=int, default=90, help="number of days (default: 90)")
    parser.add_argument("--seed", type=int, default=0, help="random seed (default: 0)")
    parser.add_argument(
        "--out",
        type=Path,
        help="write an export document (same shape as the exporter's output) to this file",
    )
    parser.add_argument(
        "--api-dir",
        type=Path,
        help="write API-shaped fixture files (cycles.json, sleep.json, ...) to this directory",
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help=f"regenerate the committed sample ({SAMPLE_DAYS} days, seed {SAMPLE_SEED}) "
        "at sample_data/whoop_sample_export.json; other options are ignored",
    )
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be >= 1")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.sample:
        args.out, args.api_dir = SAMPLE_EXPORT_PATH, None
        dataset = default_sample()
    else:
        dataset = generate(args.start, args.days, seed=args.seed)
    if args.api_dir:
        for path in dataset.write_api_fixtures(args.api_dir):
            print(f"wrote {path}", file=sys.stderr)
    export = dump_json(dataset.to_export())
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(export, encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)
    elif not args.api_dir:
        sys.stdout.write(export)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
