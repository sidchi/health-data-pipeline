"""Weekly insights: baselines, anomaly flags, weekly summaries, narratives and the job CLI.

The end-to-end checks run on the committed synthetic WHOOP sample, whose
generator records ground-truth illness days; nothing here is real data.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import requests

from health_pipeline.connectors import get_connector
from health_pipeline.insights import (
    METRICS,
    AnthropicClient,
    Direction,
    InsightOptions,
    LLMError,
    LLMNarrator,
    TemplateNarrator,
    Trend,
    WeeklySummary,
    detect_anomalies,
    iso_week,
    robust_baseline,
    to_markdown,
    ungrounded_numbers,
    week_bounds,
    weekly_summaries,
)
from health_pipeline.insights.__main__ import latest_complete
from health_pipeline.insights.__main__ import main as insights_main
from health_pipeline.insights.analysis import (
    METRICS_BY_KEY,
    analyse_days,
    rolling_mean,
    trailing,
)
from health_pipeline.insights.narrative import ANTHROPIC_URL, SYSTEM_PROMPT, build_prompt
from health_pipeline.schema import (
    DailyActivity,
    DailyRecovery,
    HealthDataset,
    SleepSession,
    Source,
    Sport,
    Workout,
)
from whoop_exporter.sample import SAMPLE_EXPORT_PATH

FIXTURES = Path(__file__).parent / "fixtures"
OURA = FIXTURES / "oura" / "oura_export.json"
MONDAY = date(2026, 3, 2)


@pytest.fixture(scope="module")
def sample() -> HealthDataset:
    return get_connector(Source.WHOOP).load(SAMPLE_EXPORT_PATH)


@pytest.fixture(scope="module")
def weeks(sample: HealthDataset) -> dict[str, WeeklySummary]:
    return {s.week: s for s in weekly_summaries(sample)}


def _series(values: list[float], start: date = MONDAY) -> dict[date, float]:
    return {start + timedelta(days=i): v for i, v in enumerate(values)}


# ------------------------------------------------------------------ baselines


def test_robust_baseline_uses_median_and_scaled_mad() -> None:
    base = robust_baseline([50, 52, 54, 56, 58], min_scale=0.1)
    assert base.median == 54
    assert base.scale == pytest.approx(1.4826 * 2)
    assert base.n == 5
    assert base.z(54 + 2 * base.scale) == pytest.approx(2.0)


def test_robust_baseline_floors_the_scale_for_flat_history() -> None:
    assert robust_baseline([55] * 10, min_scale=1.0).scale == 1.0


def test_robust_baseline_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="at least one"):
        robust_baseline([], min_scale=1.0)
    with pytest.raises(ValueError, match="min_scale"):
        robust_baseline([1.0], min_scale=0)


def test_trailing_excludes_the_day_and_rolling_mean_includes_it() -> None:
    series = _series([1, 2, 3, 4, 5])
    day = MONDAY + timedelta(days=4)
    assert trailing(series, day, 3) == [2, 3, 4]
    assert rolling_mean(series, day, 3) == pytest.approx(4.0)
    assert rolling_mean(series, MONDAY - timedelta(days=1), 3) is None


def test_trailing_window_is_calendar_days_not_observations() -> None:
    series = {MONDAY: 1.0, MONDAY + timedelta(days=10): 2.0}
    assert trailing(series, MONDAY + timedelta(days=11), 5) == [2.0]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"window": 0},
        {"min_history": 1},
        {"window": 5, "min_history": 6},
        {"threshold": 0},
        {"illness_min_markers": 0},
        {"rolling_days": 0},
    ],
)
def test_options_are_validated(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        InsightOptions(**kwargs)


# ------------------------------------------------------------------ anomalies

RHR = METRICS_BY_KEY["resting_hr_bpm"]
HRV = METRICS_BY_KEY["hrv_rmssd_ms"]


def test_spike_in_adverse_direction_is_flagged() -> None:
    series = _series([54, 55, 56, 55, 54, 55, 56, 55, 63])
    [anomaly] = detect_anomalies(series, RHR, Source.WHOOP)
    assert anomaly.day == MONDAY + timedelta(days=8)
    assert anomaly.direction is Direction.HIGH
    assert anomaly.baseline == 55
    assert anomaly.z > 3
    assert anomaly.to_dict()["label"] == "Resting heart rate"


def test_deviation_in_the_good_direction_is_not_flagged() -> None:
    assert detect_anomalies(_series([54, 55, 56, 55, 54, 55, 56, 55, 45]), RHR, Source.WHOOP) == []
    assert detect_anomalies(_series([50, 52, 51, 49, 50, 51, 50, 52, 90]), HRV, Source.WHOOP) == []


def test_low_hrv_is_flagged() -> None:
    [anomaly] = detect_anomalies(_series([50, 52, 51, 49, 50, 51, 50, 52, 30]), HRV, Source.OURA)
    assert anomaly.direction is Direction.LOW
    assert anomaly.z < -3


def test_no_flags_until_min_history_is_reached() -> None:
    series = _series([55, 55, 55, 70])
    assert detect_anomalies(series, RHR, Source.WHOOP) == []
    assert detect_anomalies(series, RHR, Source.WHOOP, InsightOptions(min_history=3))


def test_metrics_without_adverse_direction_are_never_flagged() -> None:
    strain = METRICS_BY_KEY["strain"]
    assert strain.adverse is None
    assert detect_anomalies(_series([5] * 10 + [21]), strain, Source.WHOOP) == []


def test_baseline_is_robust_to_a_contaminated_history() -> None:
    # Two wild readings in the history would inflate a mean/SD baseline enough
    # to hide the next spike; the median/MAD baseline still sees it.
    series = _series([55, 56, 54, 90, 55, 56, 95, 55, 54, 64])
    flagged = [a.day for a in detect_anomalies(series, RHR, Source.WHOOP)]
    assert MONDAY + timedelta(days=9) in flagged


def test_every_metric_key_is_unique_and_scaled() -> None:
    assert len({m.key for m in METRICS}) == len(METRICS)
    assert all(m.min_scale > 0 for m in METRICS)


# ------------------------------------------------------------------ ISO weeks


@pytest.mark.parametrize(
    ("day", "week"),
    [
        (date(2026, 1, 5), "2026-W02"),
        (date(2026, 1, 1), "2026-W01"),
        (date(2027, 1, 1), "2026-W53"),
        (date(2024, 12, 30), "2025-W01"),
    ],
)
def test_iso_week_handles_year_boundaries(day: date, week: str) -> None:
    assert iso_week(day) == week
    start, end = week_bounds(week)
    assert start <= day <= end
    assert start.isoweekday() == 1 and (end - start).days == 6


def test_week_bounds_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="ISO week"):
        week_bounds("January")


# ------------------------------------------------------------------ sample, end to end


def test_illness_signal_matches_ground_truth_exactly(sample: HealthDataset) -> None:
    truth = json.loads(SAMPLE_EXPORT_PATH.read_text())["synthetic"]["illness_days"]
    detected = [d.isoformat() for s in weekly_summaries(sample) for d in s.illness_days]
    assert detected == truth  # full recall and no false alarms on the sample month


def test_single_metric_anomaly_is_not_an_illness_signal(weeks: dict[str, WeeklySummary]) -> None:
    w03 = weeks["2026-W03"]
    assert [a.metric for a in w03.anomalies] == ["readiness_score"]
    assert w03.illness_days == ()


def test_weekly_summary_statistics(weeks: dict[str, WeeklySummary]) -> None:
    assert list(weeks) == ["2026-W02", "2026-W03", "2026-W04", "2026-W05"]
    w04 = weeks["2026-W04"]
    assert (w04.start, w04.end) == (date(2026, 1, 19), date(2026, 1, 25))
    assert w04.complete and w04.days_with_data == 7
    rhr = w04.metric("resting_hr_bpm")
    assert rhr is not None
    assert (rhr.days, rhr.low, rhr.high) == (7, 51, 63)
    assert rhr.mean == pytest.approx(400 / 7)
    assert rhr.delta == pytest.approx(400 / 7 - 390 / 7)
    assert rhr.trend is Trend.WORSE
    assert w04.workouts.count == 4
    assert w04.workouts.by_sport == {"cycling": 1, "hiit": 1, "running": 1, "walking": 1}


def test_first_week_has_no_comparison(weeks: dict[str, WeeklySummary]) -> None:
    first = weeks["2026-W02"].metric("readiness_score")
    assert first is not None
    assert first.previous_mean is None and first.trend is None


def test_recovery_week_trends_better(weeks: dict[str, WeeklySummary]) -> None:
    w05 = weeks["2026-W05"]
    for key in ("readiness_score", "hrv_rmssd_ms", "resting_hr_bpm"):
        summary = w05.metric(key)
        assert summary is not None and summary.trend is Trend.BETTER, key


def test_rolling_means_cover_the_trailing_week(sample: HealthDataset) -> None:
    days = analyse_days(sample.daily())[Source.WHOOP]
    by_day = {d.day: d for d in days}
    target = date(2026, 1, 25)
    expected = [by_day[target - timedelta(days=i)].values["resting_hr_bpm"] for i in range(7)]
    assert by_day[target].rolling["resting_hr_bpm"] == pytest.approx(sum(expected) / 7)


def test_to_dict_is_json_safe_and_displays_hours(weeks: dict[str, WeeklySummary]) -> None:
    data = json.loads(json.dumps(weeks["2026-W04"].to_dict()))
    asleep = next(m for m in data["metrics"] if m["metric"] == "sleep_time_asleep_s")
    assert asleep["unit"] == "h" and 6 < asleep["mean"] < 9
    assert data["illness_signal_days"] == ["2026-01-21", "2026-01-22", "2026-01-23"]
    assert len(data["days"]) == 7
    assert "days" not in weeks["2026-W04"].to_dict(include_days=False)


def test_sources_are_summarised_separately(sample: HealthDataset) -> None:
    oura = get_connector(Source.OURA).load(OURA)
    summaries = weekly_summaries(HealthDataset.merge(sample, oura), weeks=["2026-W02"])
    assert [s.source for s in summaries] == [Source.OURA, Source.WHOOP]
    oura_week, whoop_week = summaries
    assert not oura_week.complete
    whoop_alone = weekly_summaries(sample, weeks=["2026-W02"])[0]
    assert whoop_week.metrics == whoop_alone.metrics  # Oura never leaks into WHOOP's numbers


def test_weeks_filter_validates_and_selects(sample: HealthDataset) -> None:
    assert [s.week for s in weekly_summaries(sample, weeks=["2026-W04"])] == ["2026-W04"]
    assert weekly_summaries(sample, weeks=["2025-W10"]) == []
    with pytest.raises(ValueError):
        weekly_summaries(sample, weeks=["2026-04"])


def test_latest_complete_skips_partial_weeks() -> None:
    tz = timezone(timedelta(hours=5, minutes=30))
    records: list[Any] = [
        DailyRecovery(source=Source.OURA, day=MONDAY + timedelta(days=i), readiness_score=70)
        for i in range(9)  # one full week plus two days of the next
    ]
    records.append(
        SleepSession(
            source=Source.OURA,
            source_id="s1",
            start=datetime(2026, 3, 2, 23, tzinfo=tz),
            end=datetime(2026, 3, 3, 7, tzinfo=tz),
            is_nap=False,
            time_in_bed_s=8 * 3600,
        )
    )
    records.append(DailyActivity(source=Source.OURA, day=MONDAY, steps=9000))
    records.append(
        Workout(
            source=Source.OURA,
            source_id="w1",
            start=datetime(2026, 3, 3, 7, 30, tzinfo=tz),
            end=datetime(2026, 3, 3, 8, 15, tzinfo=tz),
            sport=Sport.RUNNING,
        )
    )
    summaries = weekly_summaries(HealthDataset.of(records))
    assert [(s.week, s.complete) for s in summaries] == [("2026-W10", True), ("2026-W11", False)]
    [latest] = latest_complete(summaries)
    assert latest.week == "2026-W10"
    assert latest.workouts.count == 1 and latest.workouts.duration_s == 45 * 60


# ------------------------------------------------------------------ narratives


def test_template_narrative_states_the_key_facts(
    weeks: dict[str, WeeklySummary],
) -> None:
    text = TemplateNarrator().narrate(weeks["2026-W04"])
    assert text.startswith("2026-W04 (whoop): 7 of 7 days recorded.")
    assert "Possible illness signal on Wed Jan 21, Thu Jan 22 and Fri Jan 23" in text
    assert "Resting heart rate averaged 57.1 bpm, up 1.4 bpm on the week before (worse)" in text
    assert "4 workouts" in text
    assert text == TemplateNarrator().narrate(weeks["2026-W04"])  # deterministic


def test_template_narrative_for_a_quiet_week(weeks: dict[str, WeeklySummary]) -> None:
    text = TemplateNarrator().narrate(weeks["2026-W05"])
    assert "No readings were outside the usual range." in text
    assert "illness" not in text


def test_template_narrative_is_grounded(weeks: dict[str, WeeklySummary]) -> None:
    for summary in weeks.values():
        text = TemplateNarrator().narrate(summary)
        assert ungrounded_numbers(text, build_prompt(summary)) == [], summary.week


@pytest.mark.parametrize(
    ("text", "bad"),
    [
        ("Resting HR averaged 57.1 bpm.", []),
        ("Resting HR averaged about 57 bpm.", []),  # rounding a given number is fine
        ("Resting HR was down 1.4 bpm.", []),  # sign is carried by the prose
        ("HRV dropped 30% this week.", ["30"]),
        ("Resting HR averaged 57.9 bpm.", ["57.9"]),
    ],
)
def test_ungrounded_numbers(text: str, bad: list[str]) -> None:
    facts = '{"mean": 57.142, "delta": -1.4, "count": 4}'
    assert ungrounded_numbers(text, facts) == bad


class FakeLLM:
    def __init__(self, reply: str | Exception) -> None:
        self.reply = reply
        self.calls: list[dict[str, str]] = []

    def complete(self, *, system: str, prompt: str) -> str:
        self.calls.append({"system": system, "prompt": prompt})
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_llm_narrative_is_used_when_grounded(weeks: dict[str, WeeklySummary]) -> None:
    reply = "Possible illness signal mid-week. Resting heart rate averaged 57.1 bpm. 4 workouts."
    llm = FakeLLM(f"  {reply}\n")
    assert LLMNarrator(llm).narrate(weeks["2026-W04"]) == reply
    [call] = llm.calls
    assert call["system"] == SYSTEM_PROMPT
    assert "2026-W04" in call["prompt"]


def test_llm_prompt_holds_only_aggregates(weeks: dict[str, WeeklySummary]) -> None:
    prompt = build_prompt(weeks["2026-W04"])
    facts = json.loads(prompt.split("JSON:\n", 1)[1].rsplit("\n\nWrite", 1)[0])
    assert "days" not in facts and "narrative" not in facts
    for leak in ("user_id", "@example.com", "source_id", "100001"):
        assert leak not in prompt


@pytest.mark.parametrize(
    "reply",
    [
        "HRV collapsed by 42% because of overtraining.",  # invented number
        "   ",  # empty
        LLMError("rate limited"),
        requests.Timeout("slow"),
    ],
)
def test_llm_narrative_falls_back_to_template(
    weeks: dict[str, WeeklySummary], reply: str | Exception
) -> None:
    summary = weeks["2026-W04"]
    assert LLMNarrator(FakeLLM(reply)).narrate(summary) == TemplateNarrator().narrate(summary)


class FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_anthropic_client_request_and_response() -> None:
    seen: dict[str, Any] = {}

    def post(url: str, **kwargs: Any) -> FakeResponse:
        seen.update(url=url, **kwargs)
        return FakeResponse(
            200,
            {"content": [{"type": "text", "text": "Hello "}, {"type": "text", "text": "week."}]},
        )

    client = AnthropicClient("test-key", model="test-model", post=post)
    assert client.complete(system="sys", prompt="facts") == "Hello week."
    assert seen["url"] == ANTHROPIC_URL
    assert seen["headers"]["x-api-key"] == "test-key"
    assert seen["headers"]["anthropic-version"]
    assert seen["json"]["model"] == "test-model"
    assert seen["json"]["system"] == "sys"
    assert seen["json"]["messages"] == [{"role": "user", "content": "facts"}]
    assert seen["timeout"] > 0


@pytest.mark.parametrize(
    "response",
    [FakeResponse(529, {}), FakeResponse(200, {"oops": 1}), FakeResponse(200, ValueError("bad"))],
)
def test_anthropic_client_errors(response: FakeResponse) -> None:
    client = AnthropicClient("k", post=lambda *a, **k: response)
    with pytest.raises(LLMError):
        client.complete(system="s", prompt="p")


def test_anthropic_client_needs_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        AnthropicClient()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    monkeypatch.setenv("INSIGHTS_LLM_MODEL", "env-model")
    assert AnthropicClient().model == "env-model"


# ------------------------------------------------------------------ Markdown


def test_markdown_rendering(weeks: dict[str, WeeklySummary]) -> None:
    summary = weeks["2026-W04"]
    md = to_markdown(summary.with_narrative("A narrative."))
    assert md.startswith("# Weekly insights — 2026-W04 · whoop\n")
    assert "A narrative." in md
    assert "| Resting heart rate (bpm) | 57.1 | 51.0 to 63.0 | +1.4 bpm | worse | 7 |" in md
    assert "**Possible illness signal:** Wed 21 Jan, Thu 22 Jan, Fri 23 Jan" in md
    assert "| Wed 21 Jan | Skin temperature | 34.4 °C |" in md
    assert "Not medical advice" in md
    assert "No readings outside the usual range." in to_markdown(weeks["2026-W05"])


# ------------------------------------------------------------------ CLI


def test_cli_writes_json_and_markdown_idempotently(tmp_path: Path) -> None:
    argv = ["--whoop", str(SAMPLE_EXPORT_PATH), "--output-dir", str(tmp_path)]
    assert insights_main(argv) == 0
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == [
        f"insights-2026-W0{n}-whoop.{ext}" for n in (2, 3, 4, 5) for ext in ("json", "md")
    ]
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert insights_main(argv) == 0
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    data = json.loads((tmp_path / "insights-2026-W04-whoop.json").read_text())
    assert data["narrative"].startswith("2026-W04 (whoop)")


def test_cli_stdout_single_week_without_narrative(capsys: pytest.CaptureFixture[str]) -> None:
    code = insights_main(
        ["--whoop", str(SAMPLE_EXPORT_PATH), "--week", "2026-W04", "--narrative", "none"]
    )
    assert code == 0
    [week] = json.loads(capsys.readouterr().out)["weeks"]
    assert week["week"] == "2026-W04" and week["narrative"] is None


def test_cli_reads_from_duckdb(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pytest.importorskip("duckdb")
    from health_pipeline.store import HealthStore

    db = tmp_path / "health.duckdb"
    with HealthStore(db) as store:
        store.load_file(SAMPLE_EXPORT_PATH, Source.WHOOP)
    assert insights_main(["--duckdb", str(db), "--latest-complete"]) == 0
    [week] = json.loads(capsys.readouterr().out)["weeks"]
    assert week["week"] == "2026-W05" and week["complete"]


def test_cli_llm_without_key_fails_cleanly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert insights_main(["--whoop", str(SAMPLE_EXPORT_PATH), "--narrative", "llm"]) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_cli_reports_no_matching_weeks(capsys: pytest.CaptureFixture[str]) -> None:
    assert insights_main(["--whoop", str(SAMPLE_EXPORT_PATH), "--week", "2030-W01"]) == 1
    assert "no matching weeks" in capsys.readouterr().err


def test_cli_rejects_bad_week(capsys: pytest.CaptureFixture[str]) -> None:
    assert insights_main(["--whoop", str(SAMPLE_EXPORT_PATH), "--week", "nope"]) == 2
    assert "ISO week" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--whoop", "a.json", "--duckdb", "b.duckdb"],
        ["--whoop", "a.json", "--llm-model", "m"],
        ["--whoop", "a.json", "--week", "2026-W01", "--latest-complete"],
    ],
)
def test_cli_argument_errors(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        insights_main(argv)
    assert exc.value.code == 2


def test_committed_sample_report_is_up_to_date(tmp_path: Path) -> None:
    committed = Path(__file__).parents[1] / "sample_data" / "insights"
    argv = ["--whoop", str(SAMPLE_EXPORT_PATH), "--week", "2026-W04", "-o", str(tmp_path)]
    assert insights_main(argv) == 0
    for path in tmp_path.iterdir():
        assert (committed / path.name).read_text("utf-8") == path.read_text("utf-8"), (
            "regenerate with: python -m health_pipeline.insights "
            "--whoop sample_data/whoop_sample_export.json --week 2026-W04 -o sample_data/insights"
        )
