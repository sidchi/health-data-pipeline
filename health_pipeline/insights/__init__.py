"""Weekly insights: rolling baselines, anomaly flags and an optional LLM narrative.

* :mod:`.analysis` — robust trailing baselines, adverse-direction anomaly flags,
  a multi-marker illness signal and per-source ISO-week summaries.
* :mod:`.narrative` — a deterministic template narrator and an opt-in,
  provider-agnostic LLM narrator with a numeric grounding check.
* :mod:`.render` — Markdown output.

Run ``python -m health_pipeline.insights --help`` for the job's CLI.
"""

from .analysis import (
    METRICS,
    Anomaly,
    Baseline,
    DayInsight,
    Direction,
    InsightOptions,
    Metric,
    MetricSummary,
    Trend,
    WeeklySummary,
    analyse_days,
    detect_anomalies,
    iso_week,
    robust_baseline,
    week_bounds,
    weekly_summaries,
)
from .narrative import (
    AnthropicClient,
    LLMClient,
    LLMError,
    LLMNarrator,
    Narrator,
    TemplateNarrator,
    ungrounded_numbers,
)
from .render import to_markdown

__all__ = [
    "METRICS",
    "Anomaly",
    "AnthropicClient",
    "Baseline",
    "DayInsight",
    "Direction",
    "InsightOptions",
    "LLMClient",
    "LLMError",
    "LLMNarrator",
    "Metric",
    "MetricSummary",
    "Narrator",
    "TemplateNarrator",
    "Trend",
    "WeeklySummary",
    "analyse_days",
    "detect_anomalies",
    "iso_week",
    "robust_baseline",
    "to_markdown",
    "ungrounded_numbers",
    "week_bounds",
    "weekly_summaries",
]
