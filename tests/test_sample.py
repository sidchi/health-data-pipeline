"""Tests for the synthetic sample-data generator (``whoop_exporter.sample``).

Four layers:

* **Contract** — generated records have every field the hand-written WHOOP
  fixtures have, so anything that works on one works on the other.
* **Invariants** — things that must hold for every record (stage minutes sum to
  time in bed, cycles are contiguous, ids resolve, values stay in range).
* **Realism** — over a year of data, the relationships the simulation encodes
  are present in the output (HRV drives recovery, hard days cost recovery,
  illness shows up in HRV and resting heart rate).
* **Integration** — the real client, run against the fake WHOOP API serving
  generated data, reproduces the generator's own export.
"""

from __future__ import annotations

import itertools
import json
from datetime import date, datetime, timedelta
from statistics import correlation, mean
from typing import Any

import pytest

from whoop_exporter import sample
from whoop_exporter.client import WhoopClient
from whoop_exporter.sample import Athlete, GeneratorOptions, Session, generate

from .whoop_api import FakeWhoopApi, load_fixture

START = date(2026, 1, 5)  # a Monday
SAMPLE_FILE = sample.SAMPLE_EXPORT_PATH


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


@pytest.fixture(scope="module")
def month() -> sample.SampleDataset:
    return generate(START, 28, seed=7)


@pytest.fixture(scope="module")
def year() -> sample.SampleDataset:
    return generate(date(2026, 1, 1), 365, seed=11)


def scored(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in records if r.get("score")]


# ---------------------------------------------------------------- determinism


def test_same_seed_same_output():
    a = generate(START, 30, seed=3).to_export()
    b = generate(START, 30, seed=3).to_export()
    assert sample.dump_json(a) == sample.dump_json(b)


def test_different_seed_different_output():
    a = generate(START, 30, seed=3).to_export()["summary"]
    b = generate(START, 30, seed=4).to_export()["summary"]
    assert a != b


def test_rejects_empty_range():
    with pytest.raises(ValueError, match="days"):
        generate(START, 0)


# ------------------------------------------------------------------- contract


def _assert_has_keys(actual: Any, expected: Any, where: str) -> None:
    """Every key in ``expected`` exists in ``actual``, recursively through dicts."""
    if not isinstance(expected, dict) or not isinstance(actual, dict):
        return
    missing = set(expected) - set(actual)
    assert not missing, f"{where}: missing {sorted(missing)}"
    for key, value in expected.items():
        _assert_has_keys(actual[key], value, f"{where}.{key}")


@pytest.mark.parametrize(
    ("fixture", "attr", "pick"),
    [
        ("cycles.json", "cycles", lambda rs: scored(rs)[0]),
        ("sleep.json", "sleep", lambda rs: next(r for r in rs if not r["nap"])),
        ("workouts.json", "workouts", lambda rs: scored(rs)[0]),
    ],
)
def test_records_match_whoop_fixture_shape(month, fixture, attr, pick):
    reference = pick(load_fixture(fixture)["records"])
    for record in getattr(month, attr):
        _assert_has_keys(record, {k: v for k, v in reference.items() if record["score"]}, attr)
        if record["score"]:
            _assert_has_keys(record["score"], reference["score"], f"{attr}.score")


def test_recovery_matches_whoop_fixture_shape(month):
    reference = load_fixture("recovery_by_cycle.json")["1002"]
    for record in scored(month.recovery):
        _assert_has_keys(record, reference, "recovery")


def test_profile_and_body_match_fixture_shape(month):
    _assert_has_keys(month.profile, load_fixture("profile.json"), "profile")
    _assert_has_keys(month.body_measurements, load_fixture("body_measurement.json"), "body")
    assert month.profile["email"].endswith("@example.com")


def test_export_document_has_exporter_shape(month):
    export = month.to_export()
    for key in ("export_date", "period", "profile", "body_measurements", "summary"):
        assert key in export
    assert export["period"]["days"] == 28
    assert export["synthetic"]["generator"] == "whoop_exporter.sample"
    assert export["synthetic"]["seed"] == 7
    assert all("cycle_start" in r for r in export["recovery"])


# ----------------------------------------------------------------- invariants


def test_cycles_are_contiguous_and_last_one_is_open(month):
    cycles = month.cycles
    assert len(cycles) == 28
    for prev, nxt in itertools.pairwise(cycles):
        assert prev["end"] == nxt["start"]
    last = cycles[-1]
    assert last["end"] is None
    assert last["score_state"] == "PENDING_SCORE"
    assert last["score"] is None
    assert all(c["score_state"] == "SCORED" for c in cycles[:-1])


def test_open_last_cycle_can_be_disabled():
    data = generate(START, 3, seed=1, options=GeneratorOptions(open_last_cycle=False))
    assert all(c["end"] and c["score"] for c in data.cycles)


def test_cycle_start_dates_are_unique(year):
    # build_daily_summary keys by UTC start date; two cycles on one date would collide.
    days = [c["start"][:10] for c in year.cycles]
    assert len(days) == len(set(days))


def test_timestamps_are_whoop_formatted_utc(month):
    for record in [*month.cycles, *month.sleep, *month.workouts]:
        for key in ("start", "created_at", "updated_at"):
            assert record[key].endswith("Z") and len(record[key]) == 24, record[key]
        assert record["timezone_offset"] == "+05:30"


def test_every_night_ends_when_its_cycle_starts(month):
    cycle_start = {c["id"]: c["start"] for c in month.cycles}
    nights = [s for s in month.sleep if not s["nap"]]
    assert len(nights) == len(month.cycles)
    for night in nights:
        assert night["end"] == cycle_start[night["cycle_id"]]
        assert parse(night["start"]) < parse(night["end"])


def test_naps_fall_inside_their_cycle(year):
    cycles = {c["id"]: c for c in year.cycles}
    naps = [s for s in year.sleep if s["nap"]]
    assert naps, "a year of data should include naps"
    for nap in naps:
        cycle = cycles[nap["cycle_id"]]
        assert parse(cycle["start"]) < parse(nap["start"])
        assert cycle["end"] is None or parse(nap["end"]) < parse(cycle["end"])
        assert nap["score"]["sleep_performance_percentage"] is None
        assert nap["score"]["stage_summary"]["total_rem_sleep_time_milli"] == 0


def test_sleep_stages_sum_to_time_in_bed(year):
    for s in year.sleep:
        st = s["score"]["stage_summary"]
        parts = (
            st["total_awake_time_milli"]
            + st["total_light_sleep_time_milli"]
            + st["total_slow_wave_sleep_time_milli"]
            + st["total_rem_sleep_time_milli"]
            + st["total_no_data_time_milli"]
        )
        assert parts == st["total_in_bed_time_milli"]
        assert min(st.values()) >= 0
        assert parse(s["end"]) - parse(s["start"]) == timedelta(
            milliseconds=st["total_in_bed_time_milli"]
        )


def test_recovery_references_cycle_and_sleep(month):
    sleep_ids = {s["id"] for s in month.sleep if not s["nap"]}
    cycle_ids = {c["id"] for c in month.cycles}
    assert {r["cycle_id"] for r in month.recovery} == cycle_ids
    assert all(r["sleep_id"] in sleep_ids for r in month.recovery)


def test_workouts_fall_inside_a_cycle(year):
    bounds = [(parse(c["start"]), c["end"] and parse(c["end"])) for c in year.cycles]
    for w in year.workouts:
        start, end = parse(w["start"]), parse(w["end"])
        assert any(s <= start and (e is None or end < e) for s, e in bounds), w["start"]


def test_workout_zones_sum_to_duration(year):
    for w in year.workouts:
        duration = parse(w["end"]) - parse(w["start"])
        zones = sum(w["score"]["zone_durations"].values())
        assert timedelta(milliseconds=zones) == duration
        assert w["sport_id"] == sample.SPORTS[w["sport_name"]]
        has_distance = w["sport_name"] in ("running", "cycling", "walking")
        assert (w["score"]["distance_meter"] is not None) == has_distance


def test_values_stay_in_physiological_ranges(year):
    for c in scored(year.cycles):
        assert 0 < c["score"]["strain"] <= 21
        assert c["score"]["max_heart_rate"] <= year.athlete.max_heart_rate
    for r in scored(year.recovery):
        s = r["score"]
        assert 1 <= s["recovery_score"] <= 99
        assert 15 <= s["hrv_rmssd_milli"] <= 150
        assert 40 <= s["resting_heart_rate"] <= 80
        assert 90 <= s["spo2_percentage"] <= 100
        assert 32 <= s["skin_temp_celsius"] <= 36
    for s in (s for s in year.sleep if not s["nap"]):
        score = s["score"]
        assert 0 <= score["sleep_performance_percentage"] <= 100
        assert 35 <= score["sleep_consistency_percentage"] <= 99
        assert 50 <= score["sleep_efficiency_percentage"] <= 100
        assert 4.5 <= score["stage_summary"]["total_in_bed_time_milli"] / 3_600_000 <= 10.5


def test_unscorable_rate_controls_null_recoveries():
    always = generate(START, 10, seed=1, options=GeneratorOptions(unscorable_rate=1.0))
    assert all(r["score_state"] == "UNSCORABLE" and r["score"] is None for r in always.recovery)
    never = generate(START, 60, seed=1, options=GeneratorOptions(unscorable_rate=0.0))
    assert all(r["score_state"] == "SCORED" for r in never.recovery)


def test_custom_plan_and_athlete():
    athlete = Athlete(
        timezone_offset="-05:00",
        plan={d: (Session("running", "easy", 30),) for d in range(7)},
    )
    data = generate(START, 14, seed=2, athlete=athlete, options=GeneratorOptions(skip_rate=0))
    assert {w["sport_name"] for w in data.workouts} == {"running"}
    assert all(r["timezone_offset"] == "-05:00" for r in data.cycles)


# -------------------------------------------------------------------- realism


def _recovery_rows(data: sample.SampleDataset) -> list[tuple[dict, dict, dict | None]]:
    """(recovery score, its cycle, the previous cycle) for every scored recovery."""
    cycles = data.cycles
    index = {c["id"]: i for i, c in enumerate(cycles)}
    rows = []
    for r in scored(data.recovery):
        i = index[r["cycle_id"]]
        rows.append((r["score"], cycles[i], cycles[i - 1] if i else None))
    return rows


def test_recovery_distribution_is_plausible(year):
    scores = [s["recovery_score"] for s, _, _ in _recovery_rows(year)]
    green = sum(x >= 67 for x in scores) / len(scores)
    red = sum(x < 34 for x in scores) / len(scores)
    assert 45 <= mean(scores) <= 70
    assert 0.15 <= green <= 0.5
    assert 0.02 <= red <= 0.25


def test_hrv_and_resting_hr_drive_recovery(year):
    rows = [s for s, _, _ in _recovery_rows(year)]
    recovery = [s["recovery_score"] for s in rows]
    assert correlation(recovery, [s["hrv_rmssd_milli"] for s in rows]) > 0.7
    assert correlation(recovery, [s["resting_heart_rate"] for s in rows]) < -0.4


def test_hard_days_cost_next_morning_recovery(year):
    pairs = [
        (prev["score"]["strain"], s["recovery_score"])
        for s, _, prev in _recovery_rows(year)
        if prev and prev["score"]
    ]
    assert correlation(*zip(*pairs, strict=True)) < -0.15


def test_low_recovery_means_easier_training(year):
    rows = _recovery_rows(year)
    red = [c["score"]["strain"] for s, c, _ in rows if c["score"] and s["recovery_score"] < 34]
    green = [c["score"]["strain"] for s, c, _ in rows if c["score"] and s["recovery_score"] >= 67]
    assert red and green
    assert mean(red) < mean(green)


def test_illness_is_labelled_and_visible_in_vitals():
    data = generate(date(2026, 1, 1), 365, seed=5, options=GeneratorOptions(illness_rate=0.03))
    ill = set(data.illness_days)
    assert ill
    local_day = {
        c["id"]: (data.start + timedelta(days=i)).isoformat() for i, c in enumerate(data.cycles)
    }
    sick_cycles = {cid for cid, day in local_day.items() if day in ill}

    sick, well = [], []
    for r in scored(data.recovery):
        (sick if r["cycle_id"] in sick_cycles else well).append(r["score"])

    def avg(rows, key):
        return mean(row[key] for row in rows)

    assert avg(sick, "hrv_rmssd_milli") < 0.9 * avg(well, "hrv_rmssd_milli")
    assert avg(sick, "resting_heart_rate") > avg(well, "resting_heart_rate") + 3
    assert avg(sick, "skin_temp_celsius") > avg(well, "skin_temp_celsius")

    # Sick days are rest days: no workout starts inside a sick cycle.
    for c in data.cycles:
        if c["id"] in sick_cycles and c["end"]:
            start, end = parse(c["start"]), parse(c["end"])
            assert not any(start <= parse(w["start"]) < end for w in data.workouts)


# ---------------------------------------------------------------- integration


def test_client_against_generated_api_reproduces_export(tmp_path):
    data = generate(START, 21, seed=9)
    data.write_api_fixtures(tmp_path)
    api = FakeWhoopApi(data_dir=tmp_path)

    expected = data.to_export()
    # Export at the end of the period, looking back far enough to include the
    # evening before START (the first night's sleep).
    now = datetime.fromisoformat(expected["period"]["end"])
    days_back = expected["period"]["days"] + 1
    fetched = WhoopClient(api.access_token, session=api).fetch_export(days_back, now=lambda: now)

    def by_start(records):
        return sorted(records, key=lambda r: r["start"])

    assert by_start(fetched["cycles"]) == by_start(expected["cycles"])
    assert by_start(fetched["sleep"]) == by_start(expected["sleep"])
    assert by_start(fetched["workouts"]) == by_start(expected["workouts"])
    key = lambda r: r["cycle_id"]  # noqa: E731
    assert sorted(fetched["recovery"], key=key) == sorted(expected["recovery"], key=key)
    assert fetched["profile"] == expected["profile"]

    def normalized(summary):
        for metrics in summary.values():
            metrics.get("workouts", []).sort(key=json.dumps)
        return summary

    assert normalized(fetched["summary"]) == normalized(expected["summary"])


# ------------------------------------------------------------------------ CLI


def test_cli_writes_export_and_api_files(tmp_path, capsys):
    out = tmp_path / "export.json"
    api_dir = tmp_path / "api"
    rc = sample.main(
        [
            "--start",
            "2026-03-02",
            "--days",
            "5",
            "--seed",
            "1",
            "--out",
            str(out),
            "--api-dir",
            str(api_dir),
        ]
    )
    assert rc == 0
    export = json.loads(out.read_text(encoding="utf-8"))
    assert export["period"]["days"] == 5
    assert sorted(p.name for p in api_dir.iterdir()) == [
        "body_measurement.json",
        "cycles.json",
        "profile.json",
        "recovery_by_cycle.json",
        "sleep.json",
        "workouts.json",
    ]
    assert capsys.readouterr().out == ""


def test_cli_prints_to_stdout_by_default(capsys):
    assert sample.main(["--days", "2", "--seed", "1"]) == 0
    export = json.loads(capsys.readouterr().out)
    assert export["synthetic"]["seed"] == 1


def test_cli_rejects_bad_days(capsys):
    with pytest.raises(SystemExit):
        sample.main(["--days", "0"])
    assert "--days" in capsys.readouterr().err


def test_committed_sample_is_up_to_date():
    """``sample_data/`` must be exactly what the generator produces.

    Regenerate after changing the simulation:
    ``python -m whoop_exporter.sample --sample``
    """
    committed = SAMPLE_FILE.read_text(encoding="utf-8")
    assert committed == sample.dump_json(sample.default_sample().to_export())


def test_committed_sample_shows_the_interesting_cases():
    export = json.loads(SAMPLE_FILE.read_text(encoding="utf-8"))
    assert export["synthetic"]["illness_days"], "demo data should include an illness episode"
    assert any(s["nap"] for s in export["sleep"])
    assert any(r["score_state"] == "UNSCORABLE" for r in export["recovery"])
    assert export["cycles"][-1]["score_state"] == "PENDING_SCORE"
    assert export["profile"]["email"].endswith("@example.com")
