# health-data-pipeline

[![CI](https://github.com/sidchi/health-data-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/sidchi/health-data-pipeline/actions/workflows/ci.yml)

Exports [WHOOP](https://developer.whoop.com/) recovery, sleep, strain and workout data
and writes one JSON document per day (raw records + a per-day summary) to Google Drive.
It runs two ways from the same code:

| Entry point | Where it runs | Auth | Output |
|---|---|---|---|
| `whoop.py` | your laptop | browser OAuth on first run, tokens cached locally | local JSON file + optional Drive upload |
| `main.py` (`trigger_upload`) | Google Cloud Function, triggered by Cloud Scheduler | refresh token in Secret Manager, service account for Drive | Drive (Shared Drive) |

See [ROADMAP.md](ROADMAP.md) for where this is heading (multi-source schema, DuckDB, dashboards).

> **Privacy:** this repository contains code and synthetic test fixtures only. Exports,
> tokens and credentials are git-ignored and must never be committed.

## Architecture

```
            ┌────────────── whoop_exporter ───────────────┐
whoop.py ──▶│ cli      ─┐                                  │
            │           ├─▶ auth ──▶ WHOOP OAuth token API │
main.py ───▶│ gcp      ─┘   client ──▶ WHOOP REST API v1/v2 │
            │ (Secret Mgr)   │                              │
            │                ▼                              │
            │             summary (per-day aggregation)     │
            │                │                              │
            │                ▼                              │
            │             drive (idempotent upsert) ──▶ Google Drive
            │ config (env > .config.json > defaults)        │
            └───────────────────────────────────────────────┘
```

* **`config`** – every deployment-specific value (client credentials, Drive folder, secret name,
  look-back window) comes from environment variables or a git-ignored `.config.json`.
* **`auth`** – token exchange/refresh. WHOOP *rotates* refresh tokens, so callers persist the new
  one immediately (Secret Manager version in the cloud, `0600` file locally).
* **`client`** – paginated fetches with timeouts and a page cap; recovery is fetched per cycle and
  missing recoveries (404 for in-progress cycles) are skipped.
* **`drive`** – `WhoopExport-YYYY-MM-DD.json` is created or replaced in place, so retries are safe.

## Configuration

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `WHOOP_CLIENT_ID` | yes | — | WHOOP developer app client id |
| `WHOOP_CLIENT_SECRET` | yes | — | WHOOP developer app client secret |
| `GDRIVE_FOLDER_ID` | Cloud Function: yes; CLI: optional | — | Drive folder (or Shared Drive folder) to write into |
| `WHOOP_SECRET_ID` | no | `whoop-refresh-token` | Secret Manager secret holding the refresh token |
| `DAYS_BACK` | no | `7` | Days of history per export (1–365) |
| `EXPORT_PATH` | no | current directory | CLI output directory |
| `WHOOP_EXPORTER_CONFIG` | no | `./.config.json` | Alternative path for the JSON config file |

Environment variables win over values in the JSON file. Copy
[`.config.example.json`](.config.example.json) to `.config.json` for local use.

## Local setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-local.txt
cp .config.example.json .config.json   # then fill in your values
```

### WHOOP OAuth

1. Create an app at the [WHOOP Developer Dashboard](https://developer-dashboard.whoop.com/).
2. Add the redirect URI `http://localhost:8080/callback`.
3. Request scopes: `read:recovery read:sleep read:workout read:cycles read:body_measurement read:profile offline`
   (`offline` is what grants a refresh token).
4. Put the client id and secret in `.config.json`.

### Google Drive (optional, local)

1. In Google Cloud Console, enable the Drive API and create an **OAuth client ID** of type *Desktop app*.
2. Download it as `credentials.json` into the repo root (git-ignored).
3. Set `GDRIVE_FOLDER_ID` to the id at the end of the folder's URL.

### Run

```bash
python whoop.py                 # DAYS_BACK days, writes JSON, uploads if configured
python whoop.py --days 21 --out ./exports --no-drive
```

The first run opens a browser for WHOOP consent (and Google consent if Drive is configured).
Tokens are cached in `.whoop_token.json` / `.gdrive_token.json` with `0600` permissions.

## Cloud Function deployment

One-time setup (replace the placeholders):

```bash
PROJECT=your-gcp-project
REGION=us-central1
SA=whoop-exporter@$PROJECT.iam.gserviceaccount.com

gcloud services enable cloudfunctions.googleapis.com cloudscheduler.googleapis.com \
  secretmanager.googleapis.com drive.googleapis.com run.googleapis.com --project $PROJECT
gcloud iam service-accounts create whoop-exporter --project $PROJECT

# Seed the refresh token (run whoop.py once locally, then copy refresh_token from .whoop_token.json)
printf '%s' "$REFRESH_TOKEN" | gcloud secrets create whoop-refresh-token --data-file=- --project $PROJECT
# The function both reads and adds versions (token rotation):
gcloud secrets add-iam-policy-binding whoop-refresh-token --project $PROJECT \
  --member serviceAccount:$SA --role roles/secretmanager.secretAccessor
gcloud secrets add-iam-policy-binding whoop-refresh-token --project $PROJECT \
  --member serviceAccount:$SA --role roles/secretmanager.secretVersionAdder
```

Share the destination **Shared Drive** folder with `$SA` as *Content manager* (a service account
has no "My Drive" storage of its own). Then deploy:

```bash
gcloud functions deploy whoop-export --gen2 --runtime python312 --region $REGION \
  --source . --entry-point trigger_upload --trigger-http --no-allow-unauthenticated \
  --service-account $SA \
  --set-env-vars GDRIVE_FOLDER_ID=your-folder-id,DAYS_BACK=7 \
  --set-secrets WHOOP_CLIENT_ID=whoop-client-id:latest,WHOOP_CLIENT_SECRET=whoop-client-secret:latest

gcloud scheduler jobs create http whoop-export-daily --location $REGION \
  --schedule "0 9 * * *" --time-zone "Etc/UTC" \
  --uri "$(gcloud functions describe whoop-export --gen2 --region $REGION --format 'value(serviceConfig.uri)')" \
  --oidc-service-account-email $SA
```

(`--set-secrets` assumes you also stored the client id/secret as secrets; plain
`--set-env-vars` works too but keeps the secret in the function config.) `.gcloudignore`
keeps tests, the CLI and local credentials out of the upload. Terraform for all of this is on
the roadmap.

The handler returns HTTP 500 on any failure so Cloud Scheduler's retry policy kicks in; the
response body never contains exception details (they go to Cloud Logging).

## Sample data

No WHOOP account? `whoop_exporter.sample` simulates one. It is a small, seeded model rather than
independent random numbers, so the data behaves like the real thing: a weekly training plan,
strain feeding a training load, HRV falling after hard days and short nights, resting heart rate
moving against HRV, naps after poor sleep, and occasional multi-day illness episodes (lower HRV,
higher RHR, skin temperature and respiratory rate) whose dates are returned as ground truth.

```bash
python -m whoop_exporter.sample --days 90 --seed 1 --out sample.json   # export document
python -m whoop_exporter.sample --days 30 --api-dir ./fake-api          # API-shaped files
python -m whoop_exporter.sample --sample   # regenerate sample_data/whoop_sample_export.json
```

* `--out` writes the same document the exporter produces (raw records + `summary`), plus a
  `synthetic` block with the seed and illness dates.
* `--api-dir` writes `cycles.json`, `sleep.json`, `recovery_by_cycle.json`, … that the fake WHOOP
  API in `tests/whoop_api.py` can serve (`FakeWhoopApi(data_dir=...)`), so the real client can be
  exercised against any amount of data.
* [`sample_data/whoop_sample_export.json`](sample_data/README.md) is a committed 28-day export
  for demos and the dashboard; a test fails if it drifts from the generator.

The same seed always gives byte-identical output.

## Development

```bash
pip install -r requirements-dev.txt
ruff check . && ruff format --check .
pytest --cov=whoop_exporter --cov=main
```

No network access or credentials are needed. CI runs lint and tests on Python 3.11 and 3.12.

Two layers of test doubles:

* **Per-test fakes** (`tests/fakes.py`) — route tables for HTTP, Secret Manager and Drive, for
  pinning a single branch.
* **A fake WHOOP API** (`tests/whoop_api.py`) — serves the synthetic fixtures in
  `tests/fixtures/whoop/` ([what's in them](tests/fixtures/whoop/README.md)) with the real
  service's semantics: window filtering, `limit`/`nextToken` pagination, bearer-token checks,
  per-cycle recovery 404s, and refresh-token **rotation** (replaying a used refresh token gets
  `400 invalid_grant`). `tests/test_pipeline_fixtures.py` runs a whole export against it and
  compares the daily summary to a golden file; `tests/test_token_lifecycle.py` covers refresh
  across runs and the local browser OAuth callback.
* **Generated data** (`whoop_exporter.sample`) — `tests/test_sample.py` checks the generator's
  output against the fixtures' shape, its invariants (stages sum to time in bed, contiguous
  cycles, zones sum to workout duration), the relationships it is meant to encode, and runs the
  real client against the fake API serving a generated month.

All fixture data is synthetic. Never add a real export, token or Drive folder id to this
repository.

## Output format

```jsonc
{
  "export_date": "2026-01-15T12:00:00+00:00",
  "period": {"start": "...", "end": "...", "days": 7},
  "profile": {}, "body_measurements": {},
  "cycles": [], "recovery": [], "sleep": [], "workouts": [],
  "summary": {
    "2026-01-14": {
      "strain": 12.3, "recovery_score": 66, "hrv_rmssd_milli": 48.7,
      "resting_heart_rate": 55, "sleep_performance_percentage": 91,
      "total_sleep_time_milli": 28800000,   // time in bed (legacy name)
      "nap_count": 1, "nap_time_milli": 2100000,
      "workouts": [{"sport_name": "running", "strain": 9.1, "zone_durations": {}}]
    }
  }
}
```
