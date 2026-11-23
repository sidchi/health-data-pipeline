"""Shared building blocks for the WHOOP exporter.

Both entry points — the local CLI (``whoop.py``) and the Google Cloud Function
(``main.py``) — are thin wrappers around this package:

* :mod:`whoop_exporter.config`  — settings from environment / ``.config.json``
* :mod:`whoop_exporter.auth`    — WHOOP OAuth 2.0 token exchange and refresh
* :mod:`whoop_exporter.client`  — WHOOP REST client (pagination, export assembly)
* :mod:`whoop_exporter.summary` — per-day aggregation of cycles, recovery, sleep, workouts
* :mod:`whoop_exporter.drive`   — idempotent JSON upload to a Google Drive folder
"""

__version__ = "0.2.0"
