# WHOOP API fixtures — synthetic data only

Every file here is **hand-authored synthetic data**, shaped to match the WHOOP
developer API (v1 cycles/user, v2 sleep/workout/recovery). No real account,
token, person or export has ever been committed to this repository, and none
ever should be: see the privacy note in the top-level [README](../../../README.md).

The sample athlete is `user_id: 10000001`, five days of January 2026.

| File | Stands in for | Notes |
| --- | --- | --- |
| `cycles.json` | `GET /v1/cycle` | 6 cycles; the last is the open cycle (`end: null`, `PENDING_SCORE`) |
| `sleep.json` | `GET /v2/activity/sleep` | 5 nights + 1 nap (`nap: true`) on 2026-01-12 |
| `recovery_by_cycle.json` | `GET /v2/cycle/{id}/recovery` | keyed by cycle id; cycle 1004 is `UNSCORABLE`, the open cycle 1006 is absent (404) |
| `workouts.json` | `GET /v2/activity/workout` | 4 workouts, one `PENDING_SCORE`, one with no distance |
| `profile.json` | `GET /v1/user/profile/basic` | `example.com` address |
| `body_measurement.json` | `GET /v1/user/measurement/body` | |
| `token_response.json` | `POST /oauth/oauth2/token` | placeholder strings; the fake rewrites the token values per refresh |
| `expected_summary.json` | — | golden output of `build_daily_summary` for the whole window |

The edge cases are deliberate: a `null` score, a missing recovery, a nap on the
same day as a night's sleep, and a record just outside the export window
(cycle 1001) all exist so the exporter's handling of them stays pinned.

`tests/whoop_api.py` serves these files as a fake API with real pagination,
bearer-token checks and refresh-token rotation.

## Regenerating the golden file

After an intentional change to the daily-summary shape:

```bash
UPDATE_GOLDEN=1 pytest tests/test_pipeline_fixtures.py
```

Review the diff before committing — an unexpected change there is the point of
the test.
