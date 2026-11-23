# Oura fixture — synthetic data only

`oura_export.json` is **hand-written**, shaped like Oura API v2 `usercollection` responses keyed
by endpoint (`sleep`, `daily_readiness`, `daily_spo2`, `daily_activity`, `workout`), three days
in January 2026 at `-08:00`. Some endpoints use the paged `{"data": [...]}` shape and one a bare
list, because the connector accepts both. No real account or person is behind it.

Cases it pins (see `tests/test_connectors.py::TestOura`): a `late_nap` whose HRV must not
replace the night's, a `deleted` period, a night with no HRV, a day with a `null` SpO2, a
malformed period missing `bedtime_end`, kcal → kJ conversion and an unmapped workout activity.
