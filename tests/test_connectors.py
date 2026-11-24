"""Fixture tests for the WHOOP, Apple Health and Oura connectors.

Every input here is synthetic: ``tests/fixtures/apple_health``,
``tests/fixtures/oura`` and the generated WHOOP sample in ``sample_data``.
"""

from __future__ import annotations

import io
import json
import logging
from datetime import date, timedelta
from pathlib import Path

import pytest

from health_pipeline import CONNECTORS, ConnectorError, HealthDataset, Source, Sport, get_connector
from health_pipeline.connectors import AppleHealthConnector, OuraConnector, WhoopConnector
from whoop_exporter.sample import SAMPLE_EXPORT_PATH

FIXTURES = Path(__file__).parent / "fixtures"
APPLE_EXPORT = FIXTURES / "apple_health" / "export.xml"
OURA_EXPORT = FIXTURES / "oura" / "oura_export.json"
IST = timedelta(hours=5, minutes=30)
PST = timedelta(hours=-8)


def by_day(records) -> dict[date, list]:
    out: dict[date, list] = {}
    for r in records:
        out.setdefault(r.day, []).append(r)
    return out


class TestRegistry:
    def test_every_source_has_a_connector(self) -> None:
        assert set(CONNECTORS) == set(Source)
        for source in Source:
            assert get_connector(source.value).source is source

    def test_unknown_source(self) -> None:
        with pytest.raises(ConnectorError, match="unknown source 'fitbit'"):
            get_connector("fitbit")


class TestWhoop:
    @pytest.fixture(scope="class")
    def export(self) -> dict:
        return json.loads(SAMPLE_EXPORT_PATH.read_text())

    @pytest.fixture(scope="class")
    def dataset(self, export: dict) -> HealthDataset:
        return WhoopConnector().load(SAMPLE_EXPORT_PATH)

    def test_one_session_per_sleep_and_workout(self, export: dict, dataset) -> None:
        assert len(dataset.sleep) == len(export["sleep"])
        assert len(dataset.workouts) == len(export["workouts"])
        assert len(dataset.activity) == len(export["cycles"])
        assert {s.is_nap for s in dataset.sleep} == {True, False}

    def test_unscored_recoveries_are_dropped(self, export: dict, dataset) -> None:
        scored = [r for r in export["recovery"] if r["score_state"] == "SCORED"]
        assert 0 < len(scored) < len(export["recovery"])  # sample has an UNSCORABLE night
        assert len(dataset.recovery) == len(scored)

    def test_local_offset_is_preserved(self, dataset) -> None:
        assert {s.start.utcoffset() for s in dataset.sleep} == {IST}

    def test_recovery_lands_on_the_wake_date_of_its_sleep(self, export: dict, dataset) -> None:
        wake = {s.source_id: s.day for s in dataset.sleep}
        hrv_by_day = {r.day: r.hrv_rmssd_ms for r in dataset.recovery}
        for raw in export["recovery"]:
            if raw["score_state"] == "SCORED":
                assert hrv_by_day[wake[raw["sleep_id"]]] == raw["score"]["hrv_rmssd_milli"]

    def test_stage_mapping(self, export: dict, dataset) -> None:
        raw = export["sleep"][0]
        stages = raw["score"]["stage_summary"]
        session = next(s for s in dataset.sleep if s.source_id == raw["id"])
        assert session.deep_s == round(stages["total_slow_wave_sleep_time_milli"] / 1000)
        assert session.time_asleep_s == session.light_s + session.deep_s + session.rem_s
        assert session.time_in_bed_s == stages["total_in_bed_time_milli"] // 1000

    def test_open_cycle_has_no_metrics(self, export: dict, dataset) -> None:
        assert export["cycles"][-1]["end"] is None
        last = max(dataset.activity, key=lambda a: a.day)
        assert last.strain is None and last.total_energy_kj is None

    def test_sports_are_normalised(self, dataset) -> None:
        assert {w.sport for w in dataset.workouts} <= set(Sport)
        assert Sport.RUNNING in {w.sport for w in dataset.workouts}
        assert all(w.source_sport for w in dataset.workouts)

    def test_malformed_record_is_skipped_and_logged(self, caplog) -> None:
        export = {
            "sleep": [
                {"id": "ok", "start": "2026-01-01T22:00:00Z", "end": "2026-01-02T06:00:00Z"},
                {"id": "bad", "start": "2026-01-02T22:00:00Z"},
            ]
        }
        with caplog.at_level(logging.WARNING):
            dataset = WhoopConnector().parse(export)
        assert [s.source_id for s in dataset.sleep] == ["ok"]
        assert dataset.sleep[0].time_in_bed_s == 8 * 3600  # falls back to the span
        assert "sleep bad" in caplog.text

    def test_rejects_non_whoop_payload(self) -> None:
        with pytest.raises(ConnectorError, match="not a WHOOP export"):
            WhoopConnector().parse({"hello": "world"})


class TestAppleHealth:
    @pytest.fixture(scope="class")
    def dataset(self) -> HealthDataset:
        return AppleHealthConnector().load(APPLE_EXPORT)

    def test_overlapping_sources_keep_the_staged_watch_night(self, dataset) -> None:
        nights = [s for s in dataset.sleep if s.day == date(2026, 1, 6)]
        assert len(nights) == 1  # iPhone "in bed" record dropped, not double-counted
        n = nights[0]
        assert n.source_id.startswith("Sample Watch:")
        assert (n.time_in_bed_s, n.time_asleep_s) == (8 * 3600, 27000)
        assert (n.light_s, n.deep_s, n.rem_s, n.awake_s) == (16200, 3600, 7200, 1800)
        assert n.efficiency_pct == 93.8
        assert n.respiratory_rate_bpm == 14.5
        assert n.start.utcoffset() == IST

    def test_short_gap_stays_one_session_and_nap_is_separate(self, dataset) -> None:
        jan7 = sorted(
            (s for s in dataset.sleep if s.day == date(2026, 1, 7)), key=lambda s: s.start
        )
        night, nap = jan7
        assert not night.is_nap and night.time_in_bed_s == 7 * 3600
        assert night.time_asleep_s == 23400 and night.awake_s == 0
        assert nap.is_nap and nap.time_asleep_s == 2400
        assert nap.light_s is None  # unstaged sleep has no stage breakdown

    def test_daily_totals_take_the_best_source_not_the_sum(self, dataset) -> None:
        activity = {a.day: a for a in dataset.activity}
        assert activity[date(2026, 1, 6)].steps == 9500  # watch 9500 vs iPhone 8000
        assert activity[date(2026, 1, 6)].active_energy_kj == pytest.approx(500 * 4.184, 0.01)
        assert activity[date(2026, 1, 7)].active_energy_kj == 1000.0  # already kJ

    def test_recovery_averages_and_uses_sdnn(self, dataset) -> None:
        recovery = {r.day: r for r in dataset.recovery}
        jan6 = recovery[date(2026, 1, 6)]
        assert (jan6.resting_hr_bpm, jan6.hrv_sdnn_ms, jan6.spo2_pct) == (52.0, 45.0, 96.0)
        assert jan6.hrv_rmssd_ms is None
        assert recovery[date(2026, 1, 7)].resting_hr_bpm == 54.0

    def test_workouts_both_export_styles(self, dataset) -> None:
        workouts = {w.sport: w for w in dataset.workouts}
        run = workouts[Sport.RUNNING]
        assert (run.distance_m, run.energy_kj) == (8100.0, 2175.7)
        strength = workouts[Sport.STRENGTH]
        assert strength.energy_kj == 1046.0
        assert (strength.average_hr_bpm, strength.max_hr_bpm) == (118.0, 151.0)
        assert workouts[Sport.OTHER].source_sport == "Curling"

    def test_malformed_record_is_skipped(self, caplog) -> None:
        with caplog.at_level(logging.WARNING):
            AppleHealthConnector().load(APPLE_EXPORT)
        assert "skipping malformed HKQuantityTypeIdentifierRestingHeartRate" in caplog.text

    def test_streams_large_exports(self) -> None:
        rows = "".join(
            f'<Record type="HKQuantityTypeIdentifierStepCount" sourceName="Sample Watch" '
            f'unit="count" startDate="2026-03-{1 + i % 28:02d} 10:00:00 +0000" '
            f'endDate="2026-03-{1 + i % 28:02d} 10:01:00 +0000" value="1"/>'
            for i in range(28_000)
        )
        xml = f'<?xml version="1.0"?><HealthData locale="en_US">{rows}</HealthData>'
        dataset = AppleHealthConnector().parse(io.BytesIO(xml.encode()))
        assert len(dataset.activity) == 28
        assert {a.steps for a in dataset.activity} == {1000}

    @pytest.mark.parametrize(
        ("xml", "message"),
        [
            (b"<Workouts/>", "not an Apple Health export"),
            (b"<HealthData><Record", "invalid Apple Health XML"),
            (
                b'<?xml version="1.0"?><!DOCTYPE HealthData [<!ENTITY a "aaaa">'
                b'<!ENTITY b "&a;&a;&a;&a;">]><HealthData>&b;</HealthData>',
                "invalid Apple Health XML",
            ),
        ],
        ids=["wrong-root", "truncated", "entity-expansion"],
    )
    def test_rejects_bad_input(self, xml: bytes, message: str) -> None:
        with pytest.raises(ConnectorError, match=message):
            AppleHealthConnector().parse(io.BytesIO(xml))

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConnectorError, match="cannot read"):
            AppleHealthConnector().load(tmp_path / "nope.xml")


class TestOura:
    @pytest.fixture(scope="class")
    def dataset(self) -> HealthDataset:
        return OuraConnector().load(OURA_EXPORT)

    def test_sleep_types(self, dataset) -> None:
        ids = {s.source_id: s for s in dataset.sleep}
        assert "oura-sleep-deleted" not in ids
        assert "oura-sleep-malformed" not in ids
        assert ids["oura-sleep-2-nap"].is_nap
        assert not ids["oura-sleep-1"].is_nap
        assert ids["oura-sleep-1"].day == date(2026, 1, 5)
        assert ids["oura-sleep-1"].start.utcoffset() == PST

    def test_recovery_combines_main_sleep_readiness_and_spo2(self, dataset) -> None:
        recovery = {r.day: r for r in dataset.recovery}
        jan5 = recovery[date(2026, 1, 5)]
        assert (jan5.readiness_score, jan5.resting_hr_bpm, jan5.hrv_rmssd_ms) == (82, 48, 62)
        assert (jan5.spo2_pct, jan5.skin_temp_deviation_c) == (97.1, -0.1)
        # The nap's HRV (40) must not replace the night's (55).
        assert recovery[date(2026, 1, 6)].hrv_rmssd_ms == 55
        assert recovery[date(2026, 1, 6)].spo2_pct is None
        assert recovery[date(2026, 1, 7)].hrv_rmssd_ms is None
        assert recovery[date(2026, 1, 7)].readiness_score == 68

    def test_activity_and_workouts_convert_kcal(self, dataset) -> None:
        activity = {a.day: a for a in dataset.activity}
        assert activity[date(2026, 1, 5)].active_energy_kj == 2175.7
        assert activity[date(2026, 1, 5)].steps == 9500
        sports = {w.source_sport: w for w in dataset.workouts}
        assert sports["running"].sport is Sport.RUNNING
        assert sports["running"].energy_kj == 1715.4
        assert sports["tennis"].sport is Sport.OTHER

    def test_accepts_bare_lists_and_missing_endpoints(self) -> None:
        dataset = OuraConnector().parse({"daily_activity": [{"day": "2026-01-01", "steps": 10}]})
        assert len(dataset) == 1 and dataset.activity[0].steps == 10

    def test_rejects_non_oura_payload(self, tmp_path: Path) -> None:
        with pytest.raises(ConnectorError, match="not an Oura export"):
            OuraConnector().parse({"cycles": []})
        bad = tmp_path / "x.json"
        bad.write_text("[1, 2]")
        with pytest.raises(ConnectorError, match="expected an object"):
            OuraConnector().load(bad)
        bad.write_text("{not json")
        with pytest.raises(ConnectorError, match="cannot read JSON"):
            OuraConnector().load(bad)

    def test_bad_endpoint_shape(self) -> None:
        with pytest.raises(ConnectorError, match="oura sleep"):
            OuraConnector().parse({"sleep": "nope"})


def test_all_sources_merge_into_one_daily_view() -> None:
    merged = HealthDataset.merge(
        WhoopConnector().load(SAMPLE_EXPORT_PATH),
        AppleHealthConnector().load(APPLE_EXPORT),
        OuraConnector().load(OURA_EXPORT),
    )
    day = merged.daily()["2026-01-06"]
    assert set(day) == {"whoop", "apple_health", "oura"}
    assert HealthDataset.from_dict(merged.to_dict()).to_dict() == merged.to_dict()
