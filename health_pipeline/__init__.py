"""Multi-source health data pipeline.

:mod:`health_pipeline.schema` defines one unified model (sleep sessions, daily
recovery, daily activity, workouts) and :mod:`health_pipeline.connectors`
normalise WHOOP, Apple Health and Oura exports into it. Fetching WHOOP data
stays in :mod:`whoop_exporter`; this package only reads exports.
:mod:`health_pipeline.store` (optional, needs ``duckdb``) persists the unified
records with incremental, idempotent loads. :mod:`health_pipeline.insights`
builds weekly summaries with robust baselines, anomaly flags and a narrative.

Run ``python -m health_pipeline --help`` for the command-line interface.
"""

from .connectors import CONNECTORS, ConnectorError, get_connector
from .schema import (
    SCHEMA_VERSION,
    DailyActivity,
    DailyRecovery,
    HealthDataset,
    SchemaError,
    SleepSession,
    Source,
    Sport,
    Workout,
)

__all__ = [
    "CONNECTORS",
    "SCHEMA_VERSION",
    "ConnectorError",
    "DailyActivity",
    "DailyRecovery",
    "HealthDataset",
    "SchemaError",
    "SleepSession",
    "Source",
    "Sport",
    "Workout",
    "get_connector",
]
