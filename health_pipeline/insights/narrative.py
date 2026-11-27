"""Plain-language narratives for weekly summaries.

Two narrators share one interface (:class:`Narrator`):

* :class:`TemplateNarrator` — deterministic sentences built from the summary.
  It is the default, needs no network, and is what tests and the committed
  examples use.
* :class:`LLMNarrator` — asks a language model to write the paragraph, through
  the provider-agnostic :class:`LLMClient` protocol. It is **opt-in**: sending
  even aggregated health metrics to a third-party API is a privacy decision the
  user makes explicitly (``--narrative llm``), never a default.

The LLM path is guarded so it degrades to the template rather than failing the
job or publishing something wrong:

* the prompt holds only the weekly aggregates (no raw records, no profile, no
  identifiers);
* any error, timeout or empty reply falls back to the template;
* **grounding check** — every number in the reply must match a number in the
  facts it was given (allowing for rounding). A model that invents a figure
  ("HRV dropped 30%") is rejected and the template is used instead.

:class:`AnthropicClient` is one concrete :class:`LLMClient` over the public
Messages HTTP API, using ``requests`` (already a dependency). Any other
provider is a ~20-line class with a ``complete`` method.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable, Iterable
from datetime import date
from typing import Any, Protocol

import requests

from .analysis import METRICS_BY_KEY, MetricSummary, Trend, WeeklySummary

log = logging.getLogger(__name__)

_HEADLINE = (
    "readiness_score",
    "hrv_rmssd_ms",
    "hrv_sdnn_ms",
    "resting_hr_bpm",
    "sleep_time_asleep_s",
    "strain",
)


class Narrator(Protocol):
    def narrate(self, summary: WeeklySummary) -> str: ...


class LLMError(RuntimeError):
    """The model could not produce a usable narrative."""


class LLMClient(Protocol):
    """The smallest interface a model provider needs to implement."""

    def complete(self, *, system: str, prompt: str) -> str: ...


# ------------------------------------------------------------------ template


def _fmt(value: float, decimals: int) -> str:
    return f"{value:.{decimals}f}"


def _day_list(days: Iterable[date]) -> str:
    labels = [f"{d:%a} {d:%b} {d.day}" for d in days]
    if len(labels) <= 2:
        return " and ".join(labels)
    return ", ".join(labels[:-1]) + f" and {labels[-1]}"


def _metric_sentence(s: MetricSummary) -> str:
    m = METRICS_BY_KEY[s.metric]
    unit = f" {m.unit}" if m.unit else ""
    text = f"{m.label} averaged {_fmt(m.display(s.mean), m.decimals)}{unit}"
    delta = s.delta
    trend = s.trend
    if delta is None or trend is None:
        return text
    if trend is Trend.STEADY:
        return text + ", about the same as the week before"
    size = _fmt(abs(m.display(delta)), m.decimals)
    word = "up" if delta > 0 else "down"
    judgement = {Trend.BETTER: " (better)", Trend.WORSE: " (worse)"}.get(trend, "")
    return text + f", {word} {size}{unit} on the week before{judgement}"


class TemplateNarrator:
    """Deterministic narrative; no network, no randomness."""

    def narrate(self, summary: WeeklySummary) -> str:
        parts = [
            f"{summary.week} ({summary.source.value}): {summary.days_with_data} of 7 days recorded."
        ]
        for key in _HEADLINE:
            if (s := summary.metric(key)) is not None:
                parts.append(_metric_sentence(s) + ".")
        if summary.illness_days:
            labels = sorted(
                {
                    METRICS_BY_KEY[a.metric].label
                    for a in summary.anomalies
                    if a.day in summary.illness_days and METRICS_BY_KEY[a.metric].physiological
                }
            )
            parts.append(
                f"Possible illness signal on {_day_list(summary.illness_days)}: "
                f"{', '.join(labels)} moved well outside the usual range together."
            )
        others = [a for a in summary.anomalies if a.day not in summary.illness_days]
        if others:
            flagged = sorted({METRICS_BY_KEY[a.metric].label for a in others})
            parts.append(f"Other out-of-range readings: {', '.join(flagged)}.")
        if not summary.anomalies:
            parts.append("No readings were outside the usual range.")
        w = summary.workouts
        if w.count:
            noun = "workout" if w.count == 1 else "workouts"
            parts.append(f"{w.count} {noun}, {w.to_dict()['duration_h']} h in total.")
        else:
            parts.append("No workouts recorded.")
        return " ".join(parts)


# ------------------------------------------------------------------ LLM


SYSTEM_PROMPT = (
    "You write a short weekly summary of one person's wearable data for that person. "
    "Use only the facts provided. Quote every number exactly as it appears in the facts "
    "and do not compute new numbers (no percentages, no differences that are not given). "
    "Write 3 to 5 plain sentences, no lists, no headings. Mention any possible illness "
    "signal first. Do not diagnose, do not give medical advice, and do not speculate about "
    "causes that are not in the facts."
)

_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def prompt_facts(summary: WeeklySummary) -> dict[str, Any]:
    """The aggregates the model is allowed to see (and must stay within)."""
    data = summary.to_dict(include_days=False)
    data.pop("narrative", None)
    return data


def build_prompt(summary: WeeklySummary) -> str:
    facts = json.dumps(prompt_facts(summary), indent=1, sort_keys=True, ensure_ascii=False)
    return f"Facts for the week, as JSON:\n{facts}\n\nWrite the summary."


def ungrounded_numbers(text: str, facts: str) -> list[str]:
    """Numbers in ``text`` that do not match any number in ``facts``.

    A number in the reply matches a fact if it equals that fact rounded to the
    reply's own number of decimal places (``49`` matches ``49.3``). Signs are
    ignored: the facts carry ``"delta": -8.1`` and prose says "down 8.1".
    """
    allowed = [float(n) for n in _NUMBER.findall(facts)]
    bad: list[str] = []
    for token in _NUMBER.findall(text):
        decimals = len(token.split(".")[1]) if "." in token else 0
        value = float(token)
        if not any(round(a, decimals) == value for a in allowed):
            bad.append(token)
    return bad


class LLMNarrator:
    """Narrative from a language model, falling back to a template on any problem."""

    def __init__(self, client: LLMClient, fallback: Narrator | None = None) -> None:
        self._client = client
        self._fallback = fallback or TemplateNarrator()

    def narrate(self, summary: WeeklySummary) -> str:
        prompt = build_prompt(summary)
        try:
            text = self._client.complete(system=SYSTEM_PROMPT, prompt=prompt).strip()
        except (LLMError, requests.RequestException) as exc:
            log.warning("%s %s: LLM narrative failed (%s); using template", summary.week,
                        summary.source.value, exc)  # fmt: skip
            return self._fallback.narrate(summary)
        if not text:
            log.warning("%s: empty LLM narrative; using template", summary.week)
            return self._fallback.narrate(summary)
        if bad := ungrounded_numbers(text, prompt):
            log.warning("%s: LLM narrative has numbers not in the facts %s; using template",
                        summary.week, bad)  # fmt: skip
            return self._fallback.narrate(summary)
        return text


ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-4-5"


class AnthropicClient:
    """:class:`LLMClient` over the Anthropic Messages API.

    The key is read from ``ANTHROPIC_API_KEY`` (never from a file in the repo);
    the model from ``INSIGHTS_LLM_MODEL`` unless given. ``post`` is injectable
    so tests never touch the network.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str | None = None,
        timeout_s: float = 30.0,
        max_tokens: int = 400,
        post: Callable[..., requests.Response] = requests.post,
    ) -> None:
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        self._key = key
        self.model = model or os.environ.get("INSIGHTS_LLM_MODEL") or DEFAULT_MODEL
        self._timeout_s = timeout_s
        self._max_tokens = max_tokens
        self._post = post

    def complete(self, *, system: str, prompt: str) -> str:
        response = self._post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": self._key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": self._max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=self._timeout_s,
        )
        if response.status_code != 200:
            raise LLMError(f"HTTP {response.status_code} from the Messages API")
        try:
            blocks = response.json()["content"]
            return "".join(b["text"] for b in blocks if b.get("type") == "text")
        except (ValueError, KeyError, TypeError) as exc:
            raise LLMError(f"unexpected Messages API response: {exc}") from exc
