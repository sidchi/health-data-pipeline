# Sample data — synthetic only

`whoop_sample_export.json` is **generated**, not recorded. It comes from
`whoop_exporter.sample` (28 days from Monday 2026-01-05, seed 13) and describes a
fictional athlete (`sample.athlete@example.com`). No real person's health data is,
or should ever be, in this repository.

It has exactly the shape of a real export from `whoop.py` / the Cloud Function —
`profile`, `body_measurements`, raw `cycles` / `recovery` / `sleep` / `workouts`
and the per-day `summary` — plus one extra block:

```json
"synthetic": {"generator": "whoop_exporter.sample", "seed": 13, "illness_days": ["2026-01-21", "..."]}
```

`illness_days` is ground truth for evaluating anomaly detection (roadmap item 8).
The seed was picked so this month includes an illness episode, naps, an
`UNSCORABLE` recovery and an open (`PENDING_SCORE`) final cycle.

Regenerate after changing the simulation, and review the diff:

```bash
python -m whoop_exporter.sample --sample
```

`tests/test_sample.py::test_committed_sample_is_up_to_date` fails if this file and
the generator disagree.
