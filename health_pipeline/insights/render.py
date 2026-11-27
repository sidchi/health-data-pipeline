"""Markdown rendering of a :class:`~.analysis.WeeklySummary` (for Drive, email or a PR)."""

from __future__ import annotations

from .analysis import METRICS_BY_KEY, WeeklySummary

DISCLAIMER = (
    "_Trend summary of personal wearable data, compared with the wearer's own recent "
    "baseline. Not medical advice._"
)


def _cell(value: float | None, decimals: int, *, signed: bool = False) -> str:
    if value is None:
        return "—"
    return f"{value:+.{decimals}f}" if signed else f"{value:.{decimals}f}"


def to_markdown(summary: WeeklySummary) -> str:
    status = "complete" if summary.complete else f"partial, {summary.days_with_data}/7 days"
    lines = [
        f"# Weekly insights — {summary.week} · {summary.source.value}",
        "",
        f"{summary.start:%a %d %b %Y} to {summary.end:%a %d %b %Y} ({status})",
        "",
    ]
    if summary.narrative:
        lines += [summary.narrative, ""]

    lines += [
        "## Metrics",
        "",
        "| Metric | Mean | Range | vs. last week | Trend | Days |",
        "| --- | ---: | ---: | ---: | --- | ---: |",
    ]
    for s in summary.metrics:
        m = METRICS_BY_KEY[s.metric]
        d = s.to_dict()
        unit = f" {m.unit}" if m.unit else ""
        delta = _cell(d["delta"], m.decimals, signed=True)
        if d["delta"] is not None:
            delta += unit
        lines.append(
            f"| {m.label}{f' ({m.unit})' if m.unit else ''} "
            f"| {_cell(d['mean'], m.decimals)} "
            f"| {_cell(d['min'], m.decimals)} to {_cell(d['max'], m.decimals)} "
            f"| {delta} "
            f"| {d['trend'] or '—'} | {s.days} |"
        )
    lines.append("")

    lines += ["## Flags", ""]
    if summary.illness_days:
        days = ", ".join(f"{d:%a %d %b}" for d in summary.illness_days)
        lines += [f"**Possible illness signal:** {days}", ""]
    if summary.anomalies:
        lines += [
            "| Day | Metric | Value | Baseline | Robust z |",
            "| --- | --- | ---: | ---: | ---: |",
        ]
        for a in summary.anomalies:
            d = a.to_dict()
            unit = f" {d['unit']}" if d["unit"] else ""
            lines.append(
                f"| {a.day:%a %d %b} | {d['label']} | {d['value']}{unit} "
                f"| {d['baseline']}{unit} | {d['z']:+.1f} |"
            )
    else:
        lines.append("No readings outside the usual range.")
    lines.append("")

    w = summary.workouts.to_dict()
    lines += ["## Training", ""]
    if w["count"]:
        sports = ", ".join(f"{sport}: {n}" for sport, n in w["by_sport"].items())
        lines.append(f"{w['count']} workouts, {w['duration_h']} h ({sports}).")
    else:
        lines.append("No workouts recorded.")
    naps = sum(d.nap_count for d in summary.days)
    if naps:
        lines.append(f"{naps} nap{'s' if naps != 1 else ''}.")
    lines += ["", DISCLAIMER, ""]
    return "\n".join(lines)
