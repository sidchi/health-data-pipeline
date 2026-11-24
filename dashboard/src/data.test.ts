import {
  type DailyExport,
  DataError,
  formatHours,
  lastNDays,
  listSources,
  parseDailyExport,
  recoveryZone,
  rollingMean,
  shortDate,
  summarize,
  toSeries,
} from "./data";

describe("parseDailyExport", () => {
  it("accepts the --daily view and keeps known metrics", () => {
    const data = parseDailyExport({
      "2026-01-05": {
        whoop: {
          readiness_score: 75,
          hrv_rmssd_ms: 55.6,
          workouts: [{ sport: "running", duration_s: 2460, energy_kj: 1963.1 }],
        },
      },
    });
    expect(data["2026-01-05"]?.whoop).toEqual({
      readiness_score: 75,
      hrv_rmssd_ms: 55.6,
      workouts: [{ sport: "running", duration_s: 2460, energy_kj: 1963.1 }],
    });
  });

  it("ignores metrics it does not know, so newer pipelines don't break it", () => {
    const data = parseDailyExport({ "2026-01-05": { oura: { future_metric: 1, strain: 3 } } });
    expect(data["2026-01-05"]?.oura).toEqual({ strain: 3 });
  });

  it("normalises a missing workout energy to null", () => {
    const data = parseDailyExport({
      "2026-01-05": { whoop: { workouts: [{ sport: "yoga", duration_s: 600 }] } },
    });
    expect(data["2026-01-05"]?.whoop?.workouts?.[0]?.energy_kj).toBeNull();
  });

  it.each([
    [null, /object keyed by YYYY-MM-DD/],
    [[], /object keyed by YYYY-MM-DD/],
    [{ "Jan 5": {} }, /"Jan 5" is not a YYYY-MM-DD date/],
    [{ "2026-13-45": {} }, /not a YYYY-MM-DD date/],
    [{ "2026-01-05": [] }, /must map source -> metrics/],
    [{ "2026-01-05": { whoop: 3 } }, /2026-01-05.whoop must be an object/],
    [{ "2026-01-05": { whoop: { strain: "high" } } }, /strain must be a finite number/],
    [{ "2026-01-05": { whoop: { workouts: {} } } }, /workouts must be a list/],
    [{ "2026-01-05": { whoop: { workouts: [{ sport: "run", duration_s: -1 }] } } }, /duration_s/],
  ])("rejects %j", (input, message) => {
    expect(() => parseDailyExport(input)).toThrow(DataError);
    expect(() => parseDailyExport(input)).toThrow(message);
  });

  it("points at --daily when given the record view", () => {
    expect(() => parseDailyExport({ schema_version: 1, sources: [], records: [] })).toThrow(
      /--daily/,
    );
  });
});

describe("listSources", () => {
  it("orders by number of days, then name", () => {
    const data: DailyExport = {
      "2026-01-01": { oura: {}, whoop: {}, apple_health: {} },
      "2026-01-02": { oura: {} },
    };
    expect(listSources(data)).toEqual(["oura", "apple_health", "whoop"]);
  });

  it("is empty for an empty export", () => {
    expect(listSources({})).toEqual([]);
  });
});

describe("toSeries", () => {
  const data: DailyExport = {
    "2026-01-30": { whoop: { readiness_score: 70, hrv_rmssd_ms: 50, sleep_time_asleep_s: 27000 } },
    "2026-02-02": {
      whoop: {
        readiness_score: 40,
        strain: 12,
        workouts: [{ sport: "run", duration_s: 1830, energy_kj: null }],
      },
    },
    "2026-02-03": { oura: { readiness_score: 88 } },
  };

  it("fills calendar gaps with nulls instead of skipping them (across a month end)", () => {
    const series = toSeries(data, "whoop");
    expect(series.map((p) => p.day)).toEqual([
      "2026-01-30",
      "2026-01-31",
      "2026-02-01",
      "2026-02-02",
    ]);
    expect(series[1]).toMatchObject({ recovery: null, hrv: null, workoutCount: 0, napCount: 0 });
  });

  it("converts units for charting", () => {
    const [first, , , last] = toSeries(data, "whoop");
    expect(first?.sleepHours).toBe(7.5);
    expect(last).toMatchObject({ strain: 12, workoutCount: 1, workoutMinutes: 31 });
  });

  it("only covers the requested source's own date range", () => {
    expect(toSeries(data, "oura").map((p) => p.day)).toEqual(["2026-02-03"]);
    expect(toSeries(data, "missing")).toEqual([]);
  });

  it("labels which HRV statistic it holds", () => {
    const apple = toSeries({ "2026-01-01": { apple_health: { hrv_sdnn_ms: 40 } } }, "apple_health");
    expect(apple[0]).toMatchObject({ hrv: 40, hrvKind: "sdnn" });
    expect(toSeries(data, "whoop")[0]?.hrvKind).toBe("rmssd");
  });

  it("computes the HRV baseline before range filtering", () => {
    const days: DailyExport = {};
    for (let d = 1; d <= 10; d++) {
      days[`2026-03-${String(d).padStart(2, "0")}`] = { whoop: { hrv_rmssd_ms: d * 10 } };
    }
    const visible = lastNDays(toSeries(days, "whoop"), 2);
    // Day 9's baseline averages days 3-9, which the 2-day view does not show.
    expect(visible[0]?.hrvBaseline).toBe(60);
  });
});

describe("lastNDays", () => {
  const series = toSeries({ "2026-01-01": { whoop: {} }, "2026-01-05": { whoop: {} } }, "whoop");

  it("keeps the trailing n calendar days", () => {
    expect(lastNDays(series, 2).map((p) => p.day)).toEqual(["2026-01-04", "2026-01-05"]);
  });

  it("returns everything for null or a range longer than the data", () => {
    expect(lastNDays(series, null)).toHaveLength(5);
    expect(lastNDays(series, 30)).toHaveLength(5);
  });
});

describe("rollingMean", () => {
  it("is a trailing mean that skips nulls", () => {
    expect(rollingMean([10, null, 20, 30], 3, 1)).toEqual([10, 10, 15, 25]);
  });

  it("needs minPeriods values before reporting a mean (default: half the window)", () => {
    expect(rollingMean([10, 20, 30, 40], 4)).toEqual([null, 15, 20, 25]);
    expect(rollingMean([10, null, null, null], 4)).toEqual([null, null, null, null]);
  });

  it("rejects a non-positive window", () => {
    expect(() => rollingMean([1], 0)).toThrow(RangeError);
  });
});

describe("recoveryZone", () => {
  it.each([
    [0, "red"],
    [33, "red"],
    [34, "yellow"],
    [66, "yellow"],
    [67, "green"],
    [100, "green"],
  ] as const)("%d is %s", (score, zone) => {
    expect(recoveryZone(score)).toBe(zone);
  });
});

describe("summarize", () => {
  it("averages only days that have each metric", () => {
    const series = toSeries(
      {
        "2026-01-01": { whoop: { readiness_score: 80, strain: 10.04 } },
        "2026-01-03": {
          whoop: {
            readiness_score: 41,
            workouts: [{ sport: "run", duration_s: 60, energy_kj: 1 }],
          },
        },
      },
      "whoop",
    );
    expect(summarize(series)).toEqual({
      days: 3,
      avgRecovery: 61,
      avgHrv: null,
      avgRestingHr: null,
      avgSleepHours: null,
      avgStrain: 10,
      workouts: 1,
    });
  });
});

describe("formatting", () => {
  it("formats hours as h/m", () => {
    expect(formatHours(7.25)).toBe("7h 15m");
    expect(formatHours(0.99)).toBe("0h 59m");
    expect(formatHours(null)).toBe("—");
  });

  it("formats days without drifting across timezones", () => {
    expect(shortDate("2026-01-05")).toBe("Jan 5");
    expect(shortDate("2026-12-31")).toBe("Dec 31");
  });
});
