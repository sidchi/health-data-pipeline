# Apple Health fixture — synthetic data only

`export.xml` is **hand-written**, shaped like the `apple_health_export/export.xml` an iPhone
produces (inline DTD, `Record` and `Workout` elements, `+0530` offsets). The devices are
"Sample iPhone" and "Sample Watch"; no real person's data is in it.

Cases it pins (see `tests/test_connectors.py::TestAppleHealth`):

* Night to 2026-01-06: the iPhone logged one `InBed` sample and the Watch logged stages over the
  same night. Only the Watch session survives.
* Night to 2026-01-07: a 30-minute gap between samples is still one session; a 14:00 unstaged
  sample is a separate nap.
* Steps on 2026-01-06: iPhone 8,000 and Watch 9,500 for the same walk → 9,500, not 17,500.
* Energy in `kcal` and in `kJ`; SpO2 as a 0–1 fraction; HRV as SDNN.
* Workouts in the old attribute style and the iOS 16+ `WorkoutStatistics` style, plus an
  unmapped activity type.
* One record with an unparseable date, which must be skipped.
