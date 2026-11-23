from whoop_exporter.summary import build_daily_summary, format_summary_line

# Synthetic, WHOOP-shaped records. Not real data.
CYCLES = [
    {
        "id": 1,
        "start": "2026-01-10T06:00:00.000Z",
        "score": {
            "strain": 12.3,
            "kilojoule": 9000,
            "average_heart_rate": 70,
            "max_heart_rate": 170,
        },
    },
    {"id": 2, "start": "2026-01-11T06:00:00.000Z", "score": None},  # PENDING_SCORE
]
RECOVERY = [
    {
        "cycle_id": 1,
        "cycle_start": "2026-01-10T06:00:00.000Z",
        "score": {
            "recovery_score": 66,
            "resting_heart_rate": 55,
            "hrv_rmssd_milli": 48.7,
            "spo2_percentage": 96.5,
            "skin_temp_celsius": 33.4,
        },
    },
    {"cycle_id": 9, "created_at": "2026-01-09T07:00:00.000Z", "score": {"recovery_score": 40}},
]
SLEEP = [
    {
        "start": "2026-01-10T22:45:00.000Z",
        "score": {
            "sleep_performance_percentage": 91,
            "sleep_efficiency_percentage": 88.5,
            "sleep_consistency_percentage": 75,
            "respiratory_rate": 15.2,
            "stage_summary": {
                "total_in_bed_time_milli": 28_800_000,
                "total_rem_sleep_time_milli": 6_000_000,
                "total_slow_wave_sleep_time_milli": 5_000_000,
                "total_light_sleep_time_milli": 13_000_000,
                "total_awake_time_milli": 2_000_000,
            },
        },
    }
]
WORKOUTS = [
    {
        "start": "2026-01-10T17:00:00.000Z",
        "sport_id": 0,
        "sport_name": "running",
        "score": {"strain": 9.1, "distance_meter": 5000, "zone_durations": {"zone_two_milli": 60}},
    },
    {"start": "2026-01-10T19:00:00.000Z", "sport_id": 1, "score": None},
]


def test_aggregates_every_source_by_day():
    daily = build_daily_summary(CYCLES, RECOVERY, SLEEP, WORKOUTS)

    assert sorted(daily) == ["2026-01-09", "2026-01-10", "2026-01-11"]
    day = daily["2026-01-10"]
    assert day["strain"] == 12.3
    assert day["recovery_score"] == 66
    assert day["hrv_rmssd_milli"] == 48.7
    assert day["sleep_performance_percentage"] == 91
    assert day["total_sleep_time_milli"] == 28_800_000
    assert day["rem_sleep_time_milli"] == 6_000_000
    assert [w["sport_name"] for w in day["workouts"]] == ["running", None]
    assert day["workouts"][0]["zone_durations"] == {"zone_two_milli": 60}


def test_recovery_falls_back_to_created_at():
    daily = build_daily_summary([], RECOVERY, [], [])
    assert daily["2026-01-09"]["recovery_score"] == 40


def test_null_scores_do_not_crash():
    daily = build_daily_summary(CYCLES, [], [], WORKOUTS)
    assert daily["2026-01-11"]["strain"] is None
    assert daily["2026-01-10"]["workouts"][1]["zone_durations"] == {}


def test_records_without_dates_are_skipped():
    daily = build_daily_summary([{"score": {"strain": 1}}], [{"score": {}}], [{}], [{}])
    assert daily == {}


def test_empty_input_gives_empty_summary():
    assert build_daily_summary([], [], [], []) == {}


def test_format_summary_line_handles_missing_metrics():
    line = format_summary_line("2026-01-11", {"strain": 7.25, "hrv_rmssd_milli": 51.6})
    assert line == "2026-01-11  Recovery: —%  Strain: 7.2  RHR: —  HRV: 52  Sleep: —%"
