/**
 * Parsing and shaping of the unified daily view produced by
 * `python -m health_pipeline --daily` (see `HealthDataset.daily()`).
 *
 * The file is `{ "YYYY-MM-DD": { "<source>": { metric: value, ... } } }`.
 * Units follow the Python schema: seconds, kilojoules, bpm, ms, 0-100 percent.
 * Sources are kept apart on purpose: a WHOOP recovery score and an Oura
 * readiness score are not on the same scale, so the dashboard shows one source
 * at a time and never blends them.
 */

export interface WorkoutSummary {
  sport: string;
  duration_s: number;
  energy_kj: number | null;
}

/** Every metric is optional: a day can have sleep but no scored cycle, etc. */
export interface SourceDay {
  readiness_score?: number;
  resting_hr_bpm?: number;
  hrv_rmssd_ms?: number;
  hrv_sdnn_ms?: number;
  spo2_pct?: number;
  skin_temp_c?: number;
  strain?: number;
  activity_score?: number;
  steps?: number;
  total_energy_kj?: number;
  active_energy_kj?: number;
  average_hr_bpm?: number;
  max_hr_bpm?: number;
  nap_count?: number;
  nap_time_asleep_s?: number;
  sleep_time_in_bed_s?: number;
  sleep_time_asleep_s?: number;
  sleep_light_s?: number;
  sleep_deep_s?: number;
  sleep_rem_s?: number;
  sleep_awake_s?: number;
  sleep_efficiency_pct?: number;
  sleep_respiratory_rate_bpm?: number;
  workouts?: WorkoutSummary[];
}

export type DailyExport = Record<string, Record<string, SourceDay>>;

/** One calendar day for one source, flattened for charting. `null` = no data. */
export interface DayPoint {
  day: string;
  recovery: number | null;
  hrv: number | null;
  /** Which HRV statistic `hrv` holds; RMSSD and SDNN are not comparable. */
  hrvKind: "rmssd" | "sdnn" | null;
  /**
   * Trailing 7-day mean of `hrv`, computed over the whole series before any
   * range filter, so the first day of a "last 7 days" view still has a
   * full-week baseline behind it.
   */
  hrvBaseline: number | null;
  restingHr: number | null;
  strain: number | null;
  sleepHours: number | null;
  deepHours: number | null;
  remHours: number | null;
  lightHours: number | null;
  awakeHours: number | null;
  sleepEfficiency: number | null;
  napCount: number;
  workoutCount: number;
  workoutMinutes: number;
}

export class DataError extends Error {
  override name = "DataError";
}

const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;
const NUMERIC_KEYS = new Set<keyof SourceDay>([
  "readiness_score",
  "resting_hr_bpm",
  "hrv_rmssd_ms",
  "hrv_sdnn_ms",
  "spo2_pct",
  "skin_temp_c",
  "strain",
  "activity_score",
  "steps",
  "total_energy_kj",
  "active_energy_kj",
  "average_hr_bpm",
  "max_hr_bpm",
  "nap_count",
  "nap_time_asleep_s",
  "sleep_time_in_bed_s",
  "sleep_time_asleep_s",
  "sleep_light_s",
  "sleep_deep_s",
  "sleep_rem_s",
  "sleep_awake_s",
  "sleep_efficiency_pct",
  "sleep_respiratory_rate_bpm",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseWorkout(value: unknown, where: string): WorkoutSummary {
  if (!isRecord(value)) throw new DataError(`${where}: workout must be an object`);
  const { sport, duration_s, energy_kj } = value;
  if (typeof sport !== "string") throw new DataError(`${where}: workout.sport must be a string`);
  if (typeof duration_s !== "number" || !Number.isFinite(duration_s) || duration_s < 0) {
    throw new DataError(`${where}: workout.duration_s must be a non-negative number`);
  }
  if (energy_kj !== null && energy_kj !== undefined && typeof energy_kj !== "number") {
    throw new DataError(`${where}: workout.energy_kj must be a number or null`);
  }
  return { sport, duration_s, energy_kj: energy_kj ?? null };
}

function parseSourceDay(value: unknown, where: string): SourceDay {
  if (!isRecord(value)) throw new DataError(`${where} must be an object`);
  const out: SourceDay = {};
  for (const [key, raw] of Object.entries(value)) {
    if (key === "workouts") {
      if (!Array.isArray(raw)) throw new DataError(`${where}.workouts must be a list`);
      out.workouts = raw.map((w, i) => parseWorkout(w, `${where}.workouts[${i}]`));
    } else if (NUMERIC_KEYS.has(key as keyof SourceDay)) {
      if (typeof raw !== "number" || !Number.isFinite(raw)) {
        throw new DataError(`${where}.${key} must be a finite number, got ${JSON.stringify(raw)}`);
      }
      (out as Record<string, number>)[key] = raw;
    }
    // Unknown keys are ignored so newer pipeline versions don't break old dashboards.
  }
  return out;
}

/**
 * Validate untrusted JSON (fetched or picked from disk) into a {@link DailyExport}.
 * Throws {@link DataError} with a path to the first offending value.
 */
export function parseDailyExport(json: unknown): DailyExport {
  if (!isRecord(json)) {
    throw new DataError("expected the --daily view: an object keyed by YYYY-MM-DD");
  }
  if ("schema_version" in json && "records" in json) {
    throw new DataError("this is the record view; re-run `python -m health_pipeline` with --daily");
  }
  const out: DailyExport = {};
  for (const [day, sources] of Object.entries(json)) {
    if (!ISO_DAY.test(day) || Number.isNaN(Date.parse(`${day}T00:00:00Z`))) {
      throw new DataError(`"${day}" is not a YYYY-MM-DD date`);
    }
    if (!isRecord(sources)) throw new DataError(`${day} must map source -> metrics`);
    out[day] = {};
    for (const [source, metrics] of Object.entries(sources)) {
      out[day][source] = parseSourceDay(metrics, `${day}.${source}`);
    }
  }
  return out;
}

/** Sources present in the export, most days first (ties alphabetical). */
export function listSources(data: DailyExport): string[] {
  const days = new Map<string, number>();
  for (const sources of Object.values(data)) {
    for (const source of Object.keys(sources)) days.set(source, (days.get(source) ?? 0) + 1);
  }
  return [...days.entries()]
    .sort(([a, na], [b, nb]) => nb - na || a.localeCompare(b))
    .map(([source]) => source);
}

const MS_PER_DAY = 86_400_000;
export const HRV_BASELINE_DAYS = 7;

function addDays(day: string, n: number): string {
  return new Date(Date.parse(`${day}T00:00:00Z`) + n * MS_PER_DAY).toISOString().slice(0, 10);
}

const hours = (seconds: number | undefined): number | null =>
  seconds === undefined ? null : Math.round((seconds / 3600) * 100) / 100;

function toPoint(day: string, m: SourceDay | undefined): DayPoint {
  const workouts = m?.workouts ?? [];
  const hrv = m?.hrv_rmssd_ms ?? m?.hrv_sdnn_ms ?? null;
  return {
    day,
    recovery: m?.readiness_score ?? null,
    hrv,
    hrvBaseline: null,
    hrvKind: m?.hrv_rmssd_ms !== undefined ? "rmssd" : m?.hrv_sdnn_ms !== undefined ? "sdnn" : null,
    restingHr: m?.resting_hr_bpm ?? null,
    strain: m?.strain ?? null,
    sleepHours: hours(m?.sleep_time_asleep_s),
    deepHours: hours(m?.sleep_deep_s),
    remHours: hours(m?.sleep_rem_s),
    lightHours: hours(m?.sleep_light_s),
    awakeHours: hours(m?.sleep_awake_s),
    sleepEfficiency: m?.sleep_efficiency_pct ?? null,
    napCount: m?.nap_count ?? 0,
    workoutCount: workouts.length,
    workoutMinutes: Math.round(workouts.reduce((sum, w) => sum + w.duration_s, 0) / 60),
  };
}

/**
 * One point per calendar day from the source's first to last day.
 *
 * Missing days are filled with nulls rather than skipped, so a week without
 * the strap on reads as a gap in the chart instead of being silently
 * squeezed out of the time axis.
 */
export function toSeries(data: DailyExport, source: string): DayPoint[] {
  const days = Object.keys(data)
    .filter((day) => data[day]?.[source] !== undefined)
    .sort();
  const first = days[0];
  const last = days.at(-1);
  if (first === undefined || last === undefined) return [];
  const points: DayPoint[] = [];
  for (let day = first; day <= last; day = addDays(day, 1)) {
    points.push(toPoint(day, data[day]?.[source]));
  }
  const baseline = rollingMean(
    points.map((p) => p.hrv),
    HRV_BASELINE_DAYS,
  );
  return points.map((p, i) => ({ ...p, hrvBaseline: baseline[i] ?? null }));
}

/** The trailing `n` calendar days of a series (all of it when `n` is null). */
export function lastNDays(points: DayPoint[], n: number | null): DayPoint[] {
  return n === null ? points : points.slice(Math.max(0, points.length - n));
}

/**
 * Trailing mean over `window` points, skipping nulls. A point whose window has
 * fewer than `minPeriods` values is null, so the baseline does not start from
 * a single noisy night.
 */
export function rollingMean(
  values: readonly (number | null)[],
  window: number,
  minPeriods = Math.ceil(window / 2),
): (number | null)[] {
  if (!Number.isInteger(window) || window < 1) throw new RangeError("window must be >= 1");
  return values.map((_, i) => {
    const present = values
      .slice(Math.max(0, i - window + 1), i + 1)
      .filter((v): v is number => v !== null);
    if (present.length < minPeriods) return null;
    return Math.round((present.reduce((a, b) => a + b, 0) / present.length) * 10) / 10;
  });
}

export type RecoveryZone = "green" | "yellow" | "red";

/**
 * WHOOP's published recovery bands: 67-100 green, 34-66 yellow, 0-33 red.
 * Only meaningful for WHOOP; other vendors' readiness scores use their own
 * bands, so callers pass `null` for them.
 */
export function recoveryZone(score: number): RecoveryZone {
  if (score >= 67) return "green";
  if (score >= 34) return "yellow";
  return "red";
}

export interface Summary {
  days: number;
  avgRecovery: number | null;
  avgHrv: number | null;
  avgRestingHr: number | null;
  avgSleepHours: number | null;
  avgStrain: number | null;
  workouts: number;
}

function mean(values: (number | null)[], digits: number): number | null {
  const present = values.filter((v): v is number => v !== null);
  if (present.length === 0) return null;
  const scale = 10 ** digits;
  return Math.round((present.reduce((a, b) => a + b, 0) / present.length) * scale) / scale;
}

/** Averages over the days that have each metric (missing days don't count as zero). */
export function summarize(points: DayPoint[]): Summary {
  return {
    days: points.length,
    avgRecovery: mean(
      points.map((p) => p.recovery),
      0,
    ),
    avgHrv: mean(
      points.map((p) => p.hrv),
      1,
    ),
    avgRestingHr: mean(
      points.map((p) => p.restingHr),
      0,
    ),
    avgSleepHours: mean(
      points.map((p) => p.sleepHours),
      1,
    ),
    avgStrain: mean(
      points.map((p) => p.strain),
      1,
    ),
    workouts: points.reduce((sum, p) => sum + p.workoutCount, 0),
  };
}

/** "Jan 5" — compact axis label for an ISO day, independent of the viewer's timezone. */
export function shortDate(day: string): string {
  return new Date(`${day}T00:00:00Z`).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}

/** 7.25 -> "7h 15m". */
export function formatHours(value: number | null): string {
  if (value === null) return "—";
  const totalMinutes = Math.round(value * 60);
  return `${Math.floor(totalMinutes / 60)}h ${String(totalMinutes % 60).padStart(2, "0")}m`;
}
