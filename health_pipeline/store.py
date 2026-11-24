"""DuckDB storage for the unified model, with incremental, idempotent loads.

The Drive JSON exports stay the system of record for the Cloud Function; this
module adds a local analytical store next to them, so history can be queried
with SQL (and, later, read by the dashboard and the insights job).

Loading is designed to be re-run freely:

* **Row-level idempotency.** Every table has a natural key (``source`` +
  ``source_id`` for sleeps and workouts, ``source`` + ``day`` for daily rows).
  Rows are upserted, and a row is rewritten only when the SHA-256 of its
  canonical JSON changed. WHOOP exports are rolling ``DAYS_BACK`` windows, so
  consecutive exports overlap almost entirely; reloading them is a no-op except
  for the records WHOOP re-scored in the meantime.
* **File-level incrementality.** :meth:`HealthStore.load_file` remembers the
  SHA-256 of every file it loaded and skips identical bytes on the next run.
* **Atomicity.** Each load (rows + its ``load_runs`` audit row) is a single
  transaction: a crash mid-load leaves the store exactly as it was, and the
  file is not marked as loaded.

**Time.** DuckDB's ``TIMESTAMPTZ`` normalises to UTC and drops the wearer's
offset, which the unified model needs to recover local days. Instants are
therefore stored as a UTC ``TIMESTAMP`` plus an ``*_offset_s`` column, and the
attributed local ``day`` is materialised for cheap filtering.

**Schema changes** go through numbered, append-only migrations
(:data:`MIGRATIONS`); ``schema_migrations`` records which ones ran. The DDL is
written out explicitly rather than generated from the dataclasses, so a change
to :mod:`health_pipeline.schema` can never silently alter an existing database;
``tests/test_store.py`` fails instead until a migration is added.

DuckDB allows one writing process per database file. That fits a personal
pipeline run from cron or by hand; concurrent writers should serialise.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import duckdb

from .connectors import get_connector
from .schema import (
    DailyActivity,
    DailyRecovery,
    HealthDataset,
    SleepSession,
    Source,
    Sport,
    UnifiedRecord,
    Workout,
    record_to_dict,
)

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ migrations

_SLEEP_DDL = """
CREATE TABLE sleep_sessions (
    source               VARCHAR   NOT NULL,
    source_id            VARCHAR   NOT NULL,
    day                  DATE      NOT NULL,
    start_utc            TIMESTAMP NOT NULL,
    start_offset_s       INTEGER   NOT NULL,
    end_utc              TIMESTAMP NOT NULL,
    end_offset_s         INTEGER   NOT NULL,
    is_nap               BOOLEAN   NOT NULL,
    time_in_bed_s        INTEGER   NOT NULL,
    time_asleep_s        INTEGER,
    light_s              INTEGER,
    deep_s               INTEGER,
    rem_s                INTEGER,
    awake_s              INTEGER,
    efficiency_pct       DOUBLE,
    respiratory_rate_bpm DOUBLE,
    record_hash          VARCHAR   NOT NULL,
    first_run_id         BIGINT    NOT NULL,
    last_changed_run_id  BIGINT    NOT NULL,
    PRIMARY KEY (source, source_id)
)"""

_RECOVERY_DDL = """
CREATE TABLE daily_recovery (
    source                VARCHAR NOT NULL,
    day                   DATE    NOT NULL,
    readiness_score       DOUBLE,
    resting_hr_bpm        DOUBLE,
    hrv_rmssd_ms          DOUBLE,
    hrv_sdnn_ms           DOUBLE,
    spo2_pct              DOUBLE,
    skin_temp_c           DOUBLE,
    skin_temp_deviation_c DOUBLE,
    record_hash           VARCHAR NOT NULL,
    first_run_id          BIGINT  NOT NULL,
    last_changed_run_id   BIGINT  NOT NULL,
    PRIMARY KEY (source, day)
)"""

_ACTIVITY_DDL = """
CREATE TABLE daily_activity (
    source              VARCHAR NOT NULL,
    day                 DATE    NOT NULL,
    strain              DOUBLE,
    activity_score      DOUBLE,
    steps               BIGINT,
    active_energy_kj    DOUBLE,
    total_energy_kj     DOUBLE,
    average_hr_bpm      DOUBLE,
    max_hr_bpm          DOUBLE,
    record_hash         VARCHAR NOT NULL,
    first_run_id        BIGINT  NOT NULL,
    last_changed_run_id BIGINT  NOT NULL,
    PRIMARY KEY (source, day)
)"""

_WORKOUT_DDL = """
CREATE TABLE workouts (
    source              VARCHAR   NOT NULL,
    source_id           VARCHAR   NOT NULL,
    day                 DATE      NOT NULL,
    start_utc           TIMESTAMP NOT NULL,
    start_offset_s      INTEGER   NOT NULL,
    end_utc             TIMESTAMP NOT NULL,
    end_offset_s        INTEGER   NOT NULL,
    sport               VARCHAR   NOT NULL,
    source_sport        VARCHAR,
    energy_kj           DOUBLE,
    average_hr_bpm      DOUBLE,
    max_hr_bpm          DOUBLE,
    distance_m          DOUBLE,
    strain              DOUBLE,
    record_hash         VARCHAR   NOT NULL,
    first_run_id        BIGINT    NOT NULL,
    last_changed_run_id BIGINT    NOT NULL,
    PRIMARY KEY (source, source_id)
)"""

_LOAD_RUNS_DDL = """
CREATE SEQUENCE load_run_id_seq START 1;
CREATE TABLE load_runs (
    run_id         BIGINT    PRIMARY KEY,
    loaded_at      TIMESTAMP NOT NULL,
    label          VARCHAR   NOT NULL,
    connector      VARCHAR,
    content_sha256 VARCHAR,
    records        INTEGER   NOT NULL,
    inserted       INTEGER   NOT NULL,
    updated        INTEGER   NOT NULL,
    unchanged      INTEGER   NOT NULL
)"""

# One row per (source, day): the shape of ``HealthDataset.daily()``, flattened.
# The night's sleep is the longest non-nap session that day, as in Python.
_DAILY_METRICS_VIEW = """
CREATE VIEW daily_metrics AS
WITH keys AS (
    SELECT source, day FROM daily_recovery
    UNION SELECT source, day FROM daily_activity
    UNION SELECT source, day FROM sleep_sessions
    UNION SELECT source, day FROM workouts
),
night AS (
    SELECT * FROM sleep_sessions
    WHERE NOT is_nap
    QUALIFY row_number() OVER (
        PARTITION BY source, day ORDER BY time_in_bed_s DESC, start_utc
    ) = 1
),
naps AS (
    SELECT source, day, count(*) AS nap_count,
           sum(coalesce(time_asleep_s, 0)) AS nap_time_asleep_s
    FROM sleep_sessions WHERE is_nap GROUP BY source, day
),
wk AS (
    SELECT source, day, count(*) AS workout_count,
           sum(epoch(end_utc - start_utc))::BIGINT AS workout_duration_s,
           sum(energy_kj) AS workout_energy_kj
    FROM workouts GROUP BY source, day
)
SELECT
    k.source, k.day,
    r.readiness_score, r.resting_hr_bpm, r.hrv_rmssd_ms, r.hrv_sdnn_ms,
    r.spo2_pct, r.skin_temp_c, r.skin_temp_deviation_c,
    a.strain, a.activity_score, a.steps, a.active_energy_kj, a.total_energy_kj,
    n.time_in_bed_s  AS sleep_time_in_bed_s,
    n.time_asleep_s  AS sleep_time_asleep_s,
    n.light_s        AS sleep_light_s,
    n.deep_s         AS sleep_deep_s,
    n.rem_s          AS sleep_rem_s,
    n.awake_s        AS sleep_awake_s,
    n.efficiency_pct AS sleep_efficiency_pct,
    n.respiratory_rate_bpm AS sleep_respiratory_rate_bpm,
    coalesce(p.nap_count, 0)          AS nap_count,
    coalesce(p.nap_time_asleep_s, 0)  AS nap_time_asleep_s,
    coalesce(w.workout_count, 0)      AS workout_count,
    coalesce(w.workout_duration_s, 0) AS workout_duration_s,
    w.workout_energy_kj
FROM keys k
LEFT JOIN daily_recovery r USING (source, day)
LEFT JOIN daily_activity a USING (source, day)
LEFT JOIN night n USING (source, day)
LEFT JOIN naps p USING (source, day)
LEFT JOIN wk w USING (source, day)
"""

#: Append-only. Never edit a migration that has shipped; add a new one.
MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (
        1,
        "unified record tables, load audit and daily_metrics view",
        ";\n".join(
            [
                _SLEEP_DDL,
                _RECOVERY_DDL,
                _ACTIVITY_DDL,
                _WORKOUT_DDL,
                _LOAD_RUNS_DDL,
                _DAILY_METRICS_VIEW,
            ]
        ),
    ),
)
SCHEMA_VERSION = MIGRATIONS[-1][0]

# ------------------------------------------------------------- record mapping

_BOOKKEEPING = ("record_hash", "first_run_id", "last_changed_run_id")


@dataclass(frozen=True, slots=True)
class _Table:
    name: str
    record_type: type[UnifiedRecord]
    key: tuple[str, ...]
    derived_day: bool  # ``day`` is a property (sleep, workout), not a field

    @property
    def data_columns(self) -> tuple[str, ...]:
        cols: list[str] = []
        for f in fields(self.record_type):
            if f.name in ("start", "end"):
                cols += [f"{f.name}_utc", f"{f.name}_offset_s"]
            else:
                cols.append(f.name)
        if self.derived_day:
            cols.insert(2, "day")
        return tuple(cols)

    @property
    def columns(self) -> tuple[str, ...]:
        return (*self.data_columns, *_BOOKKEEPING)


TABLES: tuple[_Table, ...] = (
    _Table("sleep_sessions", SleepSession, ("source", "source_id"), derived_day=True),
    _Table("daily_recovery", DailyRecovery, ("source", "day"), derived_day=False),
    _Table("daily_activity", DailyActivity, ("source", "day"), derived_day=False),
    _Table("workouts", Workout, ("source", "source_id"), derived_day=True),
)
_TABLE_BY_TYPE = {t.record_type: t for t in TABLES}


def record_hash(record: UnifiedRecord) -> str:
    """SHA-256 of the record's canonical JSON; equal iff the records are equal."""
    canonical = json.dumps(record_to_dict(record), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _split_instant(value: datetime) -> tuple[datetime, int]:
    offset = value.utcoffset()
    assert offset is not None  # guaranteed by the schema
    return value.astimezone(UTC).replace(tzinfo=None), int(offset.total_seconds())


def _join_instant(utc: datetime, offset_s: int) -> datetime:
    return utc.replace(tzinfo=UTC).astimezone(timezone(timedelta(seconds=offset_s)))


def _to_row(table: _Table, record: UnifiedRecord, run_id: int) -> tuple[Any, ...]:
    values: list[Any] = []
    for f in fields(record):
        value = getattr(record, f.name)
        if f.name in ("start", "end"):
            values.extend(_split_instant(value))
        elif isinstance(value, Source | Sport):
            values.append(value.value)
        else:
            values.append(value)
    if table.derived_day:
        values.insert(2, record.day)
    return (*values, record_hash(record), run_id, run_id)


def _from_row(table: _Table, row: Mapping[str, Any]) -> UnifiedRecord:
    kwargs: dict[str, Any] = {}
    for f in fields(table.record_type):
        if f.name in ("start", "end"):
            kwargs[f.name] = _join_instant(row[f"{f.name}_utc"], row[f"{f.name}_offset_s"])
        elif f.name == "source":
            kwargs[f.name] = Source(row[f.name])
        elif f.name == "sport":
            kwargs[f.name] = Sport(row[f.name])
        else:
            kwargs[f.name] = row[f.name]
    return table.record_type(**kwargs)


# ---------------------------------------------------------------------- store


@dataclass(frozen=True, slots=True)
class LoadResult:
    """What one load did. ``skipped`` means the identical file was loaded before."""

    label: str
    run_id: int | None
    records: int
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: bool = False

    def __str__(self) -> str:
        if self.skipped:
            return f"{self.label}: skipped (already loaded in run {self.run_id})"
        return (
            f"{self.label}: {self.records} records → {self.inserted} new, "
            f"{self.updated} updated, {self.unchanged} unchanged (run {self.run_id})"
        )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class HealthStore:
    """A DuckDB database holding unified health records.

    Use as a context manager::

        with HealthStore("health.duckdb") as store:
            print(store.load_file("export.json", "whoop"))
            week = store.read(start=date(2026, 1, 5), end=date(2026, 1, 11))
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._con = duckdb.connect(self.path)
        self.migrate()

    # -- lifecycle

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[duckdb.DuckDBPyConnection]:
        self._con.begin()
        try:
            yield self._con
        except BaseException:
            self._con.rollback()
            raise
        self._con.commit()

    @property
    def connection(self) -> duckdb.DuckDBPyConnection:
        """The underlying connection, for ad-hoc SQL. Do not write through it."""
        return self._con

    # -- schema

    def schema_version(self) -> int:
        row = self._con.execute(
            "SELECT coalesce(max(version), 0) FROM schema_migrations"
        ).fetchone()
        return int(row[0]) if row else 0

    def migrate(self) -> int:
        """Apply pending migrations; returns how many ran."""
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version INTEGER PRIMARY KEY, description VARCHAR NOT NULL,"
            " applied_at TIMESTAMP NOT NULL)"
        )
        current = self.schema_version()
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"{self.path} is at schema v{current}, newer than this code (v{SCHEMA_VERSION})"
            )
        applied = 0
        for version, description, sql in MIGRATIONS:
            if version <= current:
                continue
            with self._transaction() as con:
                con.execute(sql)
                con.execute(
                    "INSERT INTO schema_migrations VALUES (?, ?, ?)",
                    [version, description, _now()],
                )
            log.info("applied migration %d: %s", version, description)
            applied += 1
        return applied

    # -- writes

    def load(
        self,
        dataset: Iterable[UnifiedRecord],
        *,
        label: str,
        connector: str | None = None,
        content_sha256: str | None = None,
    ) -> LoadResult:
        """Upsert ``dataset`` in one transaction and record a ``load_runs`` row.

        If the same natural key appears more than once in ``dataset``, the last
        occurrence wins (the same rule a sequence of loads would apply).
        """
        by_table: dict[_Table, dict[tuple[Any, ...], UnifiedRecord]] = {t: {} for t in TABLES}
        for record in dataset:
            table = _TABLE_BY_TYPE[type(record)]
            key = tuple(getattr(record, k) for k in table.key)
            by_table[table][key] = record
        total = sum(len(v) for v in by_table.values())

        inserted = updated = 0
        with self._transaction() as con:
            run_id = _scalar(con.execute("SELECT nextval('load_run_id_seq')"))
            for table, records in by_table.items():
                if records:
                    ins, upd = self._upsert(con, table, records.values(), run_id)
                    inserted += ins
                    updated += upd
            unchanged = total - inserted - updated
            con.execute(
                "INSERT INTO load_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [run_id, _now(), label, connector, content_sha256, total, inserted, updated,
                 unchanged],
            )  # fmt: skip
        return LoadResult(label, run_id, total, inserted, updated, unchanged)

    def load_file(
        self, path: str | Path, source: Source | str, *, force: bool = False
    ) -> LoadResult:
        """Parse ``path`` with the ``source`` connector and load it.

        A file whose bytes were already loaded with the same connector is
        skipped without parsing, unless ``force`` is set.
        """
        path = Path(path)
        connector = Source(source).value
        digest = file_sha256(path)
        if not force and (previous := self._run_for(digest, connector)) is not None:
            return LoadResult(str(path), previous, records=0, skipped=True)
        dataset = get_connector(connector).load(path)
        return self.load(dataset, label=str(path), connector=connector, content_sha256=digest)

    @staticmethod
    def _upsert(
        con: duckdb.DuckDBPyConnection,
        table: _Table,
        records: Iterable[UnifiedRecord],
        run_id: int,
    ) -> tuple[int, int]:
        cols = table.columns
        stage = f"_stage_{table.name}"
        con.execute(f"CREATE OR REPLACE TEMP TABLE {stage} AS SELECT * FROM {table.name} LIMIT 0")
        placeholders = ", ".join("?" * len(cols))
        con.executemany(
            f"INSERT INTO {stage} ({', '.join(cols)}) VALUES ({placeholders})",
            [_to_row(table, r, run_id) for r in records],
        )
        on_key = " AND ".join(f"t.{k} = s.{k}" for k in table.key)
        to_update = [c for c in cols if c not in table.key and c != "first_run_id"]
        updated = _scalar(
            con.execute(
                f"UPDATE {table.name} AS t SET "
                + ", ".join(f"{c} = s.{c}" for c in to_update)
                + f" FROM {stage} AS s WHERE {on_key} AND t.record_hash <> s.record_hash"
            )
        )
        inserted = _scalar(
            con.execute(
                f"INSERT INTO {table.name} ({', '.join(cols)}) "
                f"SELECT {', '.join(f's.{c}' for c in cols)} FROM {stage} AS s "
                f"WHERE NOT EXISTS (SELECT 1 FROM {table.name} AS t WHERE {on_key})"
            )
        )
        con.execute(f"DROP TABLE {stage}")
        return inserted, updated

    # -- reads

    def _run_for(self, digest: str, connector: str) -> int | None:
        row = self._con.execute(
            "SELECT max(run_id) FROM load_runs WHERE content_sha256 = ? AND connector = ?",
            [digest, connector],
        ).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def read(
        self,
        *,
        sources: Sequence[Source | str] | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> HealthDataset:
        """Records whose attributed ``day`` is within ``[start, end]`` (inclusive)."""
        where: list[str] = []
        params: list[Any] = []
        if sources is not None:
            where.append(f"source IN ({', '.join('?' * len(sources))})")
            params += [Source(s).value for s in sources]
        if start is not None:
            where.append("day >= ?")
            params.append(start)
        if end is not None:
            where.append("day <= ?")
            params.append(end)
        clause = f" WHERE {' AND '.join(where)}" if where else ""

        dataset = HealthDataset()
        for table in TABLES:
            cols = table.data_columns
            order = ", ".join(table.key)
            cursor = self._con.execute(
                f"SELECT {', '.join(cols)} FROM {table.name}{clause} ORDER BY {order}", params
            )
            for values in cursor.fetchall():
                dataset.add(_from_row(table, dict(zip(cols, values, strict=True))))
        return dataset

    def watermark(self, source: Source | str) -> date | None:
        """The latest day with any stored record for ``source``, or ``None``."""
        value = Source(source).value
        union = " UNION ALL ".join(
            f"SELECT max(day) AS d FROM {t.name} WHERE source = ?" for t in TABLES
        )
        row = self._con.execute(f"SELECT max(d) FROM ({union})", [value] * len(TABLES)).fetchone()
        return None if row is None else row[0]

    def counts(self) -> dict[str, int]:
        """Row count per table."""
        return {
            t.name: _scalar(self._con.execute(f"SELECT count(*) FROM {t.name}")) for t in TABLES
        }

    def daily_metrics(self, source: Source | str | None = None) -> list[dict[str, Any]]:
        """Rows of the ``daily_metrics`` view, ordered by day then source."""
        sql = "SELECT * FROM daily_metrics"
        params: list[Any] = []
        if source is not None:
            sql += " WHERE source = ?"
            params.append(Source(source).value)
        cursor = self._con.execute(sql + " ORDER BY day, source", params)
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    def runs(self) -> list[dict[str, Any]]:
        """The ``load_runs`` audit log, oldest first."""
        cursor = self._con.execute("SELECT * FROM load_runs ORDER BY run_id")
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


#: Days re-fetched before the watermark on an incremental run. WHOOP can
#: re-score a recent sleep or recovery after the fact (late sync, a nap merged
#: into the night), and upserts make the overlap free to reload.
DEFAULT_OVERLAP_DAYS = 3


def incremental_days(
    watermark: date | None,
    today: date,
    *,
    fallback: int,
    overlap: int = DEFAULT_OVERLAP_DAYS,
) -> int:
    """How many days of history to fetch so nothing after ``watermark`` is missed.

    With no watermark (empty store) this is ``fallback``. There is deliberately
    no upper bound: a store that has not been fed for months backfills the gap
    in one run instead of silently leaving a hole.
    """
    if overlap < 0:
        raise ValueError("overlap must be >= 0")
    if watermark is None:
        return fallback
    return max(1, (today - watermark).days + overlap)


def _scalar(cursor: duckdb.DuckDBPyConnection) -> int:
    row = cursor.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
