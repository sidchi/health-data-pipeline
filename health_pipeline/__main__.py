"""``python -m health_pipeline`` — normalise exports into one unified JSON file.

Example (all inputs are the committed synthetic samples/fixtures)::

    python -m health_pipeline \\
        --whoop sample_data/whoop_sample_export.json \\
        --oura tests/fixtures/oura/oura_export.json \\
        --apple-health tests/fixtures/apple_health/export.xml \\
        --output unified.json --daily
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from .connectors import ConnectorError, get_connector
from .schema import HealthDataset, Source

_FLAGS = {Source.WHOOP: "--whoop", Source.APPLE_HEALTH: "--apple-health", Source.OURA: "--oura"}


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m health_pipeline",
        description="Normalise WHOOP, Apple Health and Oura exports into the unified schema.",
    )
    for source, flag in _FLAGS.items():
        parser.add_argument(
            flag, dest=source.value, type=Path, action="append", default=[],
            metavar="PATH", help=f"{source.value} export (repeatable)",
        )  # fmt: skip
    parser.add_argument("-o", "--output", type=Path, help="write JSON here instead of stdout")
    parser.add_argument(
        "--daily", action="store_true", help="emit the per-day, per-source view instead of records"
    )
    args = parser.parse_args(argv)
    if not any(getattr(args, s.value) for s in _FLAGS):
        parser.error("give at least one of " + ", ".join(_FLAGS.values()))
    return args


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _parse_args(argv)
    parts: list[HealthDataset] = []
    try:
        for source in _FLAGS:
            for path in getattr(args, source.value):
                dataset = get_connector(source).load(path)
                logging.info("%s: %d records from %s", source.value, len(dataset), path)
                parts.append(dataset)
    except ConnectorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    merged = HealthDataset.merge(*parts)
    payload = merged.daily() if args.daily else merged.to_dict()
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
