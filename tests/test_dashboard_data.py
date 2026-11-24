"""The dashboard's bundled data must be exactly what the pipeline produces.

``dashboard/public/data/daily.json`` is the unified ``--daily`` view of the
committed synthetic WHOOP sample. Regenerate it after changing the sample, a
connector or ``HealthDataset.daily()``::

    python -m health_pipeline --whoop sample_data/whoop_sample_export.json \\
        --daily --output dashboard/public/data/daily.json
"""

from __future__ import annotations

import json
from pathlib import Path

from health_pipeline.__main__ import main

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DATA = REPO_ROOT / "dashboard" / "public" / "data" / "daily.json"
SAMPLE = REPO_ROOT / "sample_data" / "whoop_sample_export.json"


def test_dashboard_data_is_up_to_date(tmp_path: Path) -> None:
    out = tmp_path / "daily.json"
    assert main(["--whoop", str(SAMPLE), "--daily", "--output", str(out)]) == 0
    assert DASHBOARD_DATA.read_text(encoding="utf-8") == out.read_text(encoding="utf-8")


def test_dashboard_data_is_synthetic_only() -> None:
    """Every day in the bundle comes from the generated sample's date range."""
    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))
    assert sample["synthetic"]["generator"] == "whoop_exporter.sample"
    days = json.loads(DASHBOARD_DATA.read_text(encoding="utf-8"))
    assert set().union(*(d.keys() for d in days.values())) == {"whoop"}
    assert min(days) == "2026-01-05"
    assert len(days) == 28
