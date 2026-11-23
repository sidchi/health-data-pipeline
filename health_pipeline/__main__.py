"""``python -m health_pipeline`` — normalise exports into unified JSON and/or DuckDB.

Example (all inputs are the committed synthetic samples/fixtures)::

    python -m health_pipeline \\
        --whoop sample_data/whoop_sample_export.json \\
        --oura tests/fixtures/oura/oura_export.json \\
        --apple-health tests/fixtures/apple_health/export.xml \\
        --output unified.json --daily

    # Load the same files into a DuckDB store; re-running is a no-op.
    python -m health_pipeline --whoop sample_data/whoop_sample_export.json \\
        --duckdb health.duckdb
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
    parser.add_argument(
        "--duckdb",
        type=Path,
        metavar="DB",
        help="also load every input into this DuckDB file (incremental and idempotent); "
        "JSON is then written only if --output is given",
    )
    parser.add_argument(
        "--force", action="store_true", help="with --duckdb, reload files even if already loaded"
    )
    args = parser.parse_args(argv)
    if not any(getattr(args, s.value) for s in _FLAGS):
        parser.error("give at least one of " + ", ".join(_FLAGS.values()))
    if args.force and not args.duckdb:
        parser.error("--force only applies with --duckdb")
    if args.duckdb and args.daily and not args.output:
        parser.error("--daily with --duckdb needs --output")
    return args


def _inputs(args: argparse.Namespace) -> list[tuple[Source, Path]]:
    return [(source, path) for source in _FLAGS for path in getattr(args, source.value)]


def _load_into_duckdb(args: argparse.Namespace) -> None:
    from .store import HealthStore  # duckdb is optional for the JSON-only path

    with HealthStore(args.duckdb) as store:
        for source, path in _inputs(args):
            logging.info("%s", store.load_file(path, source, force=args.force))
        counts = ", ".join(f"{name}={n}" for name, n in store.counts().items())
        logging.info("%s: %s", args.duckdb, counts)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _parse_args(argv)
    try:
        if args.duckdb:
            _load_into_duckdb(args)
            if not args.output:
                return 0
        parts: list[HealthDataset] = []
        for source, path in _inputs(args):
            dataset = get_connector(source).load(path)
            logging.info("%s: %d records from %s", source.value, len(dataset), path)
            parts.append(dataset)
    except (ConnectorError, OSError) as exc:
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
