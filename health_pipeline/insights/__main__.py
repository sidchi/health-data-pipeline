"""``python -m health_pipeline.insights`` — the weekly insights job.

Reads exports (the same flags as ``python -m health_pipeline``) or a DuckDB
store, and writes one JSON and one Markdown file per (ISO week, source)::

    python -m health_pipeline.insights \\
        --whoop sample_data/whoop_sample_export.json --output-dir insights/

    # From the DuckDB store, just the most recent complete week:
    python -m health_pipeline.insights --duckdb health.duckdb --latest-complete \\
        --output-dir insights/

Output names are ``insights-<week>-<source>.{json,md}`` and the content is a
pure function of the input (with the default template narrative), so re-running
the job rewrites identical files: safe to schedule, safe to retry.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from ..connectors import ConnectorError, get_connector
from ..schema import HealthDataset, Source
from .analysis import InsightOptions, WeeklySummary, weekly_summaries
from .narrative import AnthropicClient, LLMError, LLMNarrator, Narrator, TemplateNarrator
from .render import to_markdown

log = logging.getLogger("health_pipeline.insights")

_FLAGS = {Source.WHOOP: "--whoop", Source.APPLE_HEALTH: "--apple-health", Source.OURA: "--oura"}


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m health_pipeline.insights",
        description="Weekly summaries with rolling baselines, anomaly flags and a narrative.",
    )
    for source, flag in _FLAGS.items():
        parser.add_argument(
            flag, dest=source.value, type=Path, action="append", default=[],
            metavar="PATH", help=f"{source.value} export (repeatable)",
        )  # fmt: skip
    parser.add_argument("--duckdb", type=Path, metavar="DB", help="read from this DuckDB store")
    which = parser.add_mutually_exclusive_group()
    which.add_argument(
        "--week", action="append", metavar="YYYY-Www", help="only this ISO week (repeatable)"
    )
    which.add_argument(
        "--latest-complete", action="store_true",
        help="only the most recent week with all 7 days recorded (per source)",
    )  # fmt: skip
    parser.add_argument(
        "-o", "--output-dir", type=Path, help="write files here (default: JSON to stdout)"
    )
    parser.add_argument(
        "--narrative", choices=("template", "llm", "none"), default="template",
        help="template (default, offline), llm (opt-in; sends weekly aggregates to the "
        "Anthropic API using ANTHROPIC_API_KEY) or none",
    )  # fmt: skip
    parser.add_argument("--llm-model", help="model id for --narrative llm")
    parser.add_argument("--window", type=int, default=28, help="baseline window in days")
    parser.add_argument(
        "--threshold", type=float, default=3.0, help="robust z-score that counts as an anomaly"
    )
    args = parser.parse_args(argv)
    has_exports = any(getattr(args, s.value) for s in _FLAGS)
    if has_exports == bool(args.duckdb):
        parser.error("give either --duckdb or at least one of " + ", ".join(_FLAGS.values()))
    if args.llm_model and args.narrative != "llm":
        parser.error("--llm-model only applies with --narrative llm")
    return args


def _load(args: argparse.Namespace) -> HealthDataset:
    if args.duckdb:
        from ..store import HealthStore  # duckdb is optional

        with HealthStore(args.duckdb) as store:
            return store.read()
    return HealthDataset.merge(
        *(get_connector(s).load(path) for s in _FLAGS for path in getattr(args, s.value))
    )


def _narrator(args: argparse.Namespace) -> Narrator | None:
    if args.narrative == "none":
        return None
    if args.narrative == "llm":
        return LLMNarrator(AnthropicClient(model=args.llm_model))
    return TemplateNarrator()


def latest_complete(summaries: Sequence[WeeklySummary]) -> list[WeeklySummary]:
    """The most recent complete week of each source."""
    best: dict[Source, WeeklySummary] = {}
    for s in summaries:
        if s.complete and (s.source not in best or s.week > best[s.source].week):
            best[s.source] = s
    return sorted(best.values(), key=lambda s: (s.week, s.source.value))


def _write(summaries: Sequence[WeeklySummary], output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for s in summaries:
        stem = output_dir / f"insights-{s.week}-{s.source.value}"
        json_path, md_path = stem.with_suffix(".json"), stem.with_suffix(".md")
        json_path.write_text(json.dumps(s.to_dict(), indent=2, ensure_ascii=False) + "\n", "utf-8")
        md_path.write_text(to_markdown(s), "utf-8")
        written += [json_path, md_path]
    return written


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _parse_args(argv)
    try:
        options = InsightOptions(window=args.window, threshold=args.threshold)
        dataset = _load(args)
        summaries = weekly_summaries(dataset, options, weeks=args.week)
        narrator = _narrator(args)
    except (ConnectorError, OSError, ValueError, LLMError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.latest_complete:
        summaries = latest_complete(summaries)
    if not summaries:
        print("error: no matching weeks in the input", file=sys.stderr)
        return 1
    if narrator is not None:
        summaries = [s.with_narrative(narrator.narrate(s)) for s in summaries]

    if args.output_dir:
        for path in _write(summaries, args.output_dir):
            log.info("wrote %s", path)
    else:
        payload = {"weeks": [s.to_dict() for s in summaries]}
        sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
