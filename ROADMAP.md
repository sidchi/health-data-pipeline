# Roadmap — health-data-pipeline

WHOOP data exporter (local script + Google Cloud Function) that pulls recovery, sleep and strain data and writes daily summaries to Google Drive; grows into a multi-source pipeline with dashboards. The public repo uses SAMPLE data only. Never commit personal health data, tokens, or exports. Each item is one PR. The daily pipeline takes the first unchecked item.

- [x] 1. Hardening: move the hardcoded GDRIVE_FOLDER_ID (main.py, whoop.py) and other deployment-specific values into config/env, deduplicate shared logic between main.py and whoop.py into a package, add README (setup, WHOOP OAuth, Cloud Function deploy), ruff, pytest, GitHub Actions CI
- [x] 2. Tests: unit tests for token refresh, pagination and daily aggregation using recorded/synthetic WHOOP API fixtures (responses mocked)
- [x] 3. Sample-data generator producing realistic synthetic WHOOP-shaped sleep, recovery and strain data
- [x] 4. Unified schema + additional sources: normalize WHOOP, Apple Health export XML and Oura into one model, pluggable connectors with fixture tests
- [x] 5. Storage: DuckDB with incremental, idempotent loads alongside the Drive output
- [ ] 6. Infra as code: Terraform for the Cloud Function, Cloud Scheduler and Secret Manager; deploy workflow
- [ ] 7. Dashboard: React + Recharts views for recovery, sleep and strain trends, reading sample data
- [ ] 8. Insights: weekly summary job (rolling averages, anomaly flags), optional LLM narrative
- [ ] 9. Docker Compose for one-command local run with sample data
- [ ] 10. Polish: screenshots, architecture diagram, demo deploy on sample data, v1.0 release
