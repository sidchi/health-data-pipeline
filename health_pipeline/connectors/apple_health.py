"""Apple Health ``export.xml`` → unified model.

Apple Health exports are a single XML file (``apple_health_export/export.xml``)
that is routinely several gigabytes, so it is *streamed* with ``iterparse`` and
each top-level element is discarded as soon as it has been read; memory stays
flat regardless of file size. Parsing goes through :mod:`defusedxml` because
the file carries an inline DTD and is user-supplied input.

Apple has no notion of a "night" or a "day summary" — only individual samples,
often recorded twice (iPhone *and* Apple Watch). The connector therefore has to
make the choices other vendors make on-device:

**Sleep sessions.** ``HKCategoryTypeIdentifierSleepAnalysis`` samples from one
source are chained into a session while gaps stay under
:data:`SESSION_GAP`. Core/Deep/REM map to light/deep/REM; the legacy
``Asleep`` and ``AsleepUnspecified`` values count as asleep without a stage.
Time in bed is the session's span. When sources overlap (the phone logged
"in bed", the watch logged stages), the session with the most staged sleep
wins and overlapping ones are dropped — summing them would double-count the
night. Sessions shorter than :data:`NAP_MAX` are naps.

**Daily totals** (steps, active energy) are summed per source and the *largest*
source total wins, instead of summing across devices (which double-counts a
walk carried with a phone and a watch). It slightly under-counts when two
devices genuinely saw different activity; Apple's own Health app resolves this
with per-sample source priority, which needs data the export does not carry.

**Daily averages** (resting HR, HRV, SpO2) are averaged over all samples of the
local day. Apple Watch HRV is **SDNN**, so it lands in ``hrv_sdnn_ms`` and is
never compared with WHOOP/Oura RMSSD.

Respiratory-rate samples are attached to the sleep session they fall inside.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import IO
from xml.etree.ElementTree import Element, ParseError

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import iterparse

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
from .base import Connector, ConnectorError, log

SESSION_GAP = timedelta(minutes=60)
NAP_MAX = timedelta(hours=3)

HK = "HKQuantityTypeIdentifier"
SLEEP_TYPE = "HKCategoryTypeIdentifierSleepAnalysis"
STEPS = f"{HK}StepCount"
ACTIVE_ENERGY = f"{HK}ActiveEnergyBurned"
RESTING_HR = f"{HK}RestingHeartRate"
HRV_SDNN = f"{HK}HeartRateVariabilitySDNN"
SPO2 = f"{HK}OxygenSaturation"
RESPIRATORY_RATE = f"{HK}RespiratoryRate"

_SLEEP_PREFIX = "HKCategoryValueSleepAnalysis"
_STAGE = {"AsleepCore": "light", "AsleepDeep": "deep", "AsleepREM": "rem", "Awake": "awake"}
_ASLEEP = {"AsleepCore", "AsleepDeep", "AsleepREM", "AsleepUnspecified", "Asleep"}

_ENERGY_TO_KJ = {"kJ": 1.0, "kcal": KCAL_TO_KJ, "Cal": KCAL_TO_KJ, "cal": KCAL_TO_KJ / 1000}
_DISTANCE_TO_M = {"m": 1.0, "km": 1000.0, "mi": 1609.344, "ft": 0.3048, "yd": 0.9144}

_SPORT = {
    "Running": Sport.RUNNING,
    "Cycling": Sport.CYCLING,
    "Walking": Sport.WALKING,
    "Hiking": Sport.HIKING,
    "Swimming": Sport.SWIMMING,
    "TraditionalStrengthTraining": Sport.STRENGTH,
    "FunctionalStrengthTraining": Sport.STRENGTH,
    "HighIntensityIntervalTraining": Sport.HIIT,
    "Yoga": Sport.YOGA,
    "Rowing": Sport.ROWING,
}


def parse_apple_date(value: str) -> datetime:
    """Apple's ``2026-01-05 07:12:00 +0530`` format (always carries an offset)."""
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S %z")


def _convert(value: float, unit: str | None, table: dict[str, float]) -> float:
    if unit not in table:
        raise ValueError(f"unsupported unit {unit!r}")
    return value * table[unit]


@dataclass(slots=True)
class _SleepSample:
    source: str
    stage: str
    start: datetime
    end: datetime

    @property
    def seconds(self) -> int:
        return int((self.end - self.start).total_seconds())


@dataclass(slots=True)
class _Session:
    source: str
    samples: list[_SleepSample] = field(default_factory=list)

    @property
    def start(self) -> datetime:
        return self.samples[0].start

    @property
    def end(self) -> datetime:
        return max(s.end for s in self.samples)

    def total(self, stages: set[str] | str) -> int:
        wanted = {stages} if isinstance(stages, str) else stages
        return sum(s.seconds for s in self.samples if s.stage in wanted)

    @property
    def staged(self) -> bool:
        return any(s.stage in _STAGE and s.stage != "Awake" for s in self.samples)

    def overlaps(self, other: _Session) -> bool:
        return self.start < other.end and other.start < self.end


@dataclass
class _Accumulator:
    """Everything collected in one streaming pass, aggregated afterwards."""

    sleep: list[_SleepSample] = field(default_factory=list)
    # (day, type) -> source -> running total
    totals: dict[tuple[date, str], dict[str, float]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(float))
    )
    # (day, type) -> sample values
    samples: dict[tuple[date, str], list[float]] = field(default_factory=lambda: defaultdict(list))
    respiratory: list[tuple[datetime, float]] = field(default_factory=list)
    workouts: list[Workout] = field(default_factory=list)


class AppleHealthConnector(Connector):
    source = Source.APPLE_HEALTH

    def load(self, path: Path) -> HealthDataset:
        try:
            with Path(path).open("rb") as fh:
                return self.parse(fh)
        except OSError as exc:
            raise ConnectorError(f"cannot read {path}: {exc}") from exc

    def parse(self, stream: IO[bytes]) -> HealthDataset:
        acc = _Accumulator()
        root: Element | None = None
        depth = 0
        try:
            for event, elem in iterparse(stream, events=("start", "end"), forbid_dtd=False):
                if event == "start":
                    if root is None:
                        root = elem
                        if elem.tag != "HealthData":
                            raise ConnectorError(f"not an Apple Health export: <{elem.tag}>")
                    depth += 1
                    continue
                depth -= 1
                if depth == 1:  # a direct child of <HealthData> is complete
                    if elem.tag == "Record":
                        self._safe_record(acc, elem)
                    elif elem.tag == "Workout":
                        workout = self._safe("workout", lambda elem=elem: self._workout(elem))
                        if isinstance(workout, Workout):
                            acc.workouts.append(workout)
                    if root is not None:
                        root.clear()  # drop everything read so far; keeps memory flat
        except (ParseError, DefusedXmlException) as exc:
            raise ConnectorError(f"invalid Apple Health XML: {exc}") from exc
        if root is None:
            raise ConnectorError("empty Apple Health export")
        return self._aggregate(acc)

    # -- streaming pass ---------------------------------------------------------------

    def _safe_record(self, acc: _Accumulator, elem: Element) -> None:
        try:
            self._record(acc, elem)
        except (KeyError, ValueError) as exc:
            log.warning("apple_health: skipping malformed %s: %s", elem.get("type"), exc)

    def _record(self, acc: _Accumulator, elem: Element) -> None:
        kind = elem.get("type")
        source = elem.get("sourceName") or "unknown"
        start = parse_apple_date(elem.attrib["startDate"])
        if kind == SLEEP_TYPE:
            value = elem.attrib["value"].removeprefix(_SLEEP_PREFIX)
            end = parse_apple_date(elem.attrib["endDate"])
            if end > start:
                acc.sleep.append(_SleepSample(source, value, start, end))
            return
        if kind not in (STEPS, ACTIVE_ENERGY, RESTING_HR, HRV_SDNN, SPO2, RESPIRATORY_RATE):
            return
        value = float(elem.attrib["value"])
        key = (start.date(), kind)
        if kind == STEPS:
            acc.totals[key][source] += value
        elif kind == ACTIVE_ENERGY:
            acc.totals[key][source] += _convert(value, elem.get("unit"), _ENERGY_TO_KJ)
        elif kind == SPO2:
            acc.samples[key].append(value * 100 if value <= 1 else value)
        elif kind == RESPIRATORY_RATE:
            acc.respiratory.append((start, value))
        else:
            acc.samples[key].append(value)

    def _workout(self, elem: Element) -> Workout:
        activity = elem.attrib["workoutActivityType"].removeprefix("HKWorkoutActivityType")
        energy = distance = avg_hr = max_hr = None
        if (raw := elem.get("totalEnergyBurned")) is not None:
            energy = _convert(float(raw), elem.get("totalEnergyBurnedUnit"), _ENERGY_TO_KJ)
        if (raw := elem.get("totalDistance")) is not None:
            distance = _convert(float(raw), elem.get("totalDistanceUnit"), _DISTANCE_TO_M)
        # Exports since iOS 16 put totals in <WorkoutStatistics> children instead.
        for stat in elem.iter("WorkoutStatistics"):
            kind, unit = stat.get("type", ""), stat.get("unit")
            if kind == ACTIVE_ENERGY and energy is None and stat.get("sum"):
                energy = _convert(float(stat.attrib["sum"]), unit, _ENERGY_TO_KJ)
            elif kind.startswith(f"{HK}Distance") and distance is None and stat.get("sum"):
                distance = _convert(float(stat.attrib["sum"]), unit, _DISTANCE_TO_M)
            elif kind == f"{HK}HeartRate":
                avg_hr = float(stat.attrib["average"]) if stat.get("average") else None
                max_hr = float(stat.attrib["maximum"]) if stat.get("maximum") else None
        start = parse_apple_date(elem.attrib["startDate"])
        return Workout(
            source=self.source,
            source_id=f"{elem.get('sourceName', 'unknown')}:{start.isoformat()}",
            start=start,
            end=parse_apple_date(elem.attrib["endDate"]),
            sport=_SPORT.get(activity, Sport.OTHER),
            source_sport=activity,
            energy_kj=None if energy is None else round(energy, 1),
            distance_m=None if distance is None else round(distance, 1),
            average_hr_bpm=avg_hr,
            max_hr_bpm=max_hr,
        )

    # -- aggregation ------------------------------------------------------------------

    def _aggregate(self, acc: _Accumulator) -> HealthDataset:
        dataset = HealthDataset()
        for session in self._dedupe(self._sessions(acc.sleep)):
            rec = self._safe("sleep session", lambda s=session: self._sleep(s, acc.respiratory))
            if rec is not None:
                dataset.add(rec)

        days = sorted({day for day, _ in (*acc.totals, *acc.samples)})
        for day in days:

            def mean(kind: str, day: date = day) -> float | None:
                values = acc.samples.get((day, kind))
                return round(statistics.fmean(values), 1) if values else None

            def best_total(kind: str, day: date = day) -> float | None:
                per_source = acc.totals.get((day, kind))
                return max(per_source.values()) if per_source else None

            recovery = DailyRecovery(
                source=self.source,
                day=day,
                resting_hr_bpm=mean(RESTING_HR),
                hrv_sdnn_ms=mean(HRV_SDNN),
                spo2_pct=mean(SPO2),
            )
            if any(v is not None for v in (recovery.resting_hr_bpm, recovery.hrv_sdnn_ms,
                                           recovery.spo2_pct)):  # fmt: skip
                dataset.add(recovery)
            steps, energy = best_total(STEPS), best_total(ACTIVE_ENERGY)
            if steps is not None or energy is not None:
                dataset.add(
                    DailyActivity(
                        source=self.source,
                        day=day,
                        steps=None if steps is None else round(steps),
                        active_energy_kj=None if energy is None else round(energy, 1),
                    )
                )
        for workout in acc.workouts:
            dataset.add(workout)
        return dataset

    @staticmethod
    def _sessions(samples: list[_SleepSample]) -> list[_Session]:
        """Chain each source's samples into sessions separated by long gaps."""
        by_source: dict[str, list[_SleepSample]] = defaultdict(list)
        for s in samples:
            by_source[s.source].append(s)
        sessions: list[_Session] = []
        for source, items in by_source.items():
            current: _Session | None = None
            for sample in sorted(items, key=lambda s: s.start):
                if current is None or sample.start - current.end > SESSION_GAP:
                    current = _Session(source)
                    sessions.append(current)
                current.samples.append(sample)
        return sessions

    @staticmethod
    def _dedupe(sessions: list[_Session]) -> list[_Session]:
        """Keep the most detailed session wherever sources overlap."""

        def quality(s: _Session) -> tuple[bool, int, int, str]:
            span = int((s.end - s.start).total_seconds())
            return (s.staged, s.total(_ASLEEP), span, s.source)

        kept: list[_Session] = []
        for candidate in sorted(sessions, key=quality, reverse=True):
            if not any(candidate.overlaps(k) for k in kept):
                kept.append(candidate)
        return sorted(kept, key=lambda s: s.start)

    def _sleep(self, session: _Session, respiratory: list[tuple[datetime, float]]) -> SleepSession:
        in_bed = int((session.end - session.start).total_seconds())
        asleep = min(session.total(_ASLEEP), in_bed) if session.total(_ASLEEP) else None
        staged = session.staged
        rates = [v for t, v in respiratory if session.start <= t <= session.end]
        return SleepSession(
            source=self.source,
            source_id=f"{session.source}:{session.start.isoformat()}",
            start=session.start,
            end=session.end,
            is_nap=session.end - session.start < NAP_MAX,
            time_in_bed_s=in_bed,
            time_asleep_s=asleep,
            light_s=session.total("AsleepCore") if staged else None,
            deep_s=session.total("AsleepDeep") if staged else None,
            rem_s=session.total("AsleepREM") if staged else None,
            awake_s=session.total("Awake") if staged else None,
            efficiency_pct=round(100 * asleep / in_bed, 1) if asleep and in_bed else None,
            respiratory_rate_bpm=round(statistics.fmean(rates), 1) if rates else None,
        )
