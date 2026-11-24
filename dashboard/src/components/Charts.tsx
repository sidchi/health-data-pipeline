import type { ReactElement } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { type DayPoint, formatHours, recoveryZone, shortDate } from "../data";
import { ChartCard, type Column, Legend } from "./ChartCard";

/*
 * Design rules (see README "Dashboard"): one y-axis per chart, never two —
 * measures with different units get their own chart. Colors are CSS custom
 * properties defined in styles.css so light and dark mode swap in one place.
 */

const HEIGHT = 220;
const fmt = (v: number | null, unit = "") => (v === null ? "—" : `${v}${unit}`);

/**
 * Y-axis domain and ticks on multiples of `step`, padded so no point sits on
 * the frame. Computed here rather than by Recharts because function domains
 * turn off its "nice" tick placement.
 */
export function niceScale(
  values: readonly (number | null)[],
  step: number,
): { domain: [number, number]; ticks: number[] } {
  const present = values.filter((v): v is number => v !== null);
  if (present.length === 0) return { domain: [0, step], ticks: [0, step] };
  const low = Math.floor((Math.min(...present) - step / 2) / step) * step;
  const high = Math.ceil((Math.max(...present) + step / 2) / step) * step;
  const ticks: number[] = [];
  for (let t = Math.max(0, low); t <= high; t += step) ticks.push(t);
  return { domain: [Math.max(0, low), high], ticks };
}

const dayColumn: Column<DayPoint> = { header: "Day", value: (p) => p.day };

const axisProps = {
  tick: { fill: "var(--text-muted)", fontSize: 12 },
  tickLine: false,
  axisLine: { stroke: "var(--axis)" },
} as const;

function Frame({ children }: { children: ReactElement }) {
  return (
    <ResponsiveContainer
      width="100%"
      height={HEIGHT}
      initialDimension={{ width: 800, height: HEIGHT }}
    >
      {children}
    </ResponsiveContainer>
  );
}

function Grid() {
  return <CartesianGrid vertical={false} stroke="var(--grid)" />;
}

function DayAxis() {
  return <XAxis dataKey="day" tickFormatter={shortDate} minTickGap={16} {...axisProps} />;
}

const tooltipProps = {
  labelFormatter: (day: unknown) => shortDate(String(day)),
  contentStyle: {
    background: "var(--surface-1)",
    border: "1px solid var(--border)",
    borderRadius: 8,
    color: "var(--text-primary)",
  },
  cursor: { fill: "var(--hover-wash)" },
} as const;

const ZONE_COLOR = {
  green: "var(--status-good)",
  yellow: "var(--status-warning)",
  red: "var(--status-critical)",
} as const;

const ZONE_LABEL = { green: "Green", yellow: "Yellow", red: "Red" } as const;

export function RecoveryChart({ points, source }: { points: DayPoint[]; source: string }) {
  // WHOOP's bands are WHOOP's; other vendors' readiness scores get a neutral color.
  const zoned = source === "whoop";
  return (
    <ChartCard
      title="Recovery"
      description={
        zoned
          ? "Daily recovery score (0–100), colored by WHOOP's published zones."
          : `Daily readiness score (0–100) as reported by ${source}.`
      }
      legend={
        zoned ? (
          <Legend
            items={[
              { label: "Green 67–100", color: ZONE_COLOR.green },
              { label: "Yellow 34–66", color: ZONE_COLOR.yellow },
              { label: "Red 0–33", color: ZONE_COLOR.red },
            ]}
          />
        ) : null
      }
      rows={points}
      columns={[
        dayColumn,
        { header: "Score", value: (p) => fmt(p.recovery) },
        ...(zoned
          ? [
              {
                header: "Zone",
                value: (p: DayPoint) =>
                  p.recovery === null ? "—" : ZONE_LABEL[recoveryZone(p.recovery)],
              },
            ]
          : []),
      ]}
    >
      <Frame>
        <BarChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: -16 }}>
          <Grid />
          <DayAxis />
          <YAxis domain={[0, 100]} ticks={[0, 33, 66, 100]} {...axisProps} />
          <Tooltip
            {...tooltipProps}
            formatter={(value) => {
              const score = Number(value);
              const zone = zoned ? ` (${ZONE_LABEL[recoveryZone(score)]})` : "";
              return [`${score}${zone}`, "Recovery"];
            }}
          />
          <Bar
            dataKey="recovery"
            name="Recovery"
            radius={[4, 4, 0, 0]}
            maxBarSize={24}
            isAnimationActive={false}
          >
            {points.map((p) => (
              <Cell
                key={p.day}
                fill={
                  zoned && p.recovery !== null
                    ? ZONE_COLOR[recoveryZone(p.recovery)]
                    : "var(--series-1)"
                }
              />
            ))}
          </Bar>
        </BarChart>
      </Frame>
    </ChartCard>
  );
}

export function HrvChart({ points }: { points: DayPoint[] }) {
  const kind = points.find((p) => p.hrvKind !== null)?.hrvKind;
  const statistic = kind === "sdnn" ? "SDNN" : "RMSSD";
  return (
    <ChartCard
      title="Heart rate variability"
      description={`Overnight HRV (${statistic}, ms) against its trailing 7-day mean. A sustained drop below baseline is an early strain or illness signal.`}
      legend={
        <Legend
          items={[
            { label: `HRV (${statistic})`, color: "var(--series-1)", shape: "line" },
            { label: "7-day baseline", color: "var(--text-muted)", shape: "dashed" },
          ]}
        />
      }
      rows={points}
      columns={[
        dayColumn,
        { header: "HRV (ms)", value: (p) => fmt(p.hrv) },
        { header: "7-day mean", value: (p) => fmt(p.hrvBaseline) },
      ]}
    >
      <Frame>
        <LineChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: -16 }}>
          <Grid />
          <DayAxis />
          <YAxis
            {...niceScale(
              points.flatMap((p) => [p.hrv, p.hrvBaseline]),
              10,
            )}
            {...axisProps}
          />
          <Tooltip {...tooltipProps} cursor={{ stroke: "var(--axis)" }} />
          <Line
            dataKey="hrvBaseline"
            name="7-day baseline"
            stroke="var(--text-muted)"
            strokeDasharray="4 4"
            strokeWidth={2}
            dot={false}
            isAnimationActive={false}
          />
          <Line
            dataKey="hrv"
            name="HRV (ms)"
            stroke="var(--series-1)"
            strokeWidth={2}
            dot={{ r: 3, fill: "var(--series-1)", stroke: "var(--surface-1)", strokeWidth: 2 }}
            activeDot={{ r: 5 }}
            isAnimationActive={false}
          />
        </LineChart>
      </Frame>
    </ChartCard>
  );
}

export function RestingHrChart({ points }: { points: DayPoint[] }) {
  return (
    <ChartCard
      title="Resting heart rate"
      description="Overnight resting heart rate (bpm). Lower is generally better; a rise of a few bpm often accompanies poor recovery."
      rows={points}
      columns={[dayColumn, { header: "RHR (bpm)", value: (p) => fmt(p.restingHr) }]}
    >
      <Frame>
        <LineChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: -16 }}>
          <Grid />
          <DayAxis />
          <YAxis
            {...niceScale(
              points.map((p) => p.restingHr),
              5,
            )}
            {...axisProps}
          />
          <Tooltip {...tooltipProps} cursor={{ stroke: "var(--axis)" }} />
          <Line
            dataKey="restingHr"
            name="Resting HR (bpm)"
            stroke="var(--series-1)"
            strokeWidth={2}
            dot={{ r: 3, fill: "var(--series-1)", stroke: "var(--surface-1)", strokeWidth: 2 }}
            activeDot={{ r: 5 }}
            isAnimationActive={false}
          />
        </LineChart>
      </Frame>
    </ChartCard>
  );
}

const STAGES = [
  { key: "deepHours", label: "Deep", color: "var(--series-1)" },
  { key: "remHours", label: "REM", color: "var(--series-2)" },
  { key: "lightHours", label: "Light", color: "var(--series-3)" },
  { key: "awakeHours", label: "Awake", color: "var(--series-4)" },
] as const;

export function SleepChart({ points }: { points: DayPoint[] }) {
  return (
    <ChartCard
      title="Sleep"
      wide
      description="Main sleep by stage (hours), attributed to the morning you woke up. Naps are counted separately."
      legend={<Legend items={STAGES.map(({ label, color }) => ({ label, color }))} />}
      rows={points}
      columns={[
        dayColumn,
        { header: "Asleep", value: (p) => formatHours(p.sleepHours) },
        ...STAGES.map(({ key, label }) => ({
          header: label,
          value: (p: DayPoint) => formatHours(p[key]),
        })),
        { header: "Efficiency", value: (p) => fmt(p.sleepEfficiency, "%") },
        { header: "Naps", value: (p) => String(p.napCount) },
      ]}
    >
      <Frame>
        <BarChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: -16 }}>
          <Grid />
          <DayAxis />
          <YAxis allowDecimals={false} unit="h" {...axisProps} />
          <Tooltip
            {...tooltipProps}
            formatter={(value, name) => [formatHours(Number(value)), name]}
          />
          {STAGES.map(({ key, label, color }, i) => (
            <Bar
              key={key}
              dataKey={key}
              name={label}
              stackId="sleep"
              isAnimationActive={false}
              fill={color}
              stroke="var(--surface-1)"
              strokeWidth={1}
              maxBarSize={24}
              radius={i === STAGES.length - 1 ? [4, 4, 0, 0] : 0}
            />
          ))}
        </BarChart>
      </Frame>
    </ChartCard>
  );
}

export function StrainChart({ points }: { points: DayPoint[] }) {
  return (
    <ChartCard
      title="Strain"
      description="Day strain on WHOOP's 0–21 scale. Hover a bar for that day's workouts."
      rows={points}
      columns={[
        dayColumn,
        { header: "Strain", value: (p) => fmt(p.strain) },
        { header: "Workouts", value: (p) => String(p.workoutCount) },
        { header: "Workout min", value: (p) => String(p.workoutMinutes) },
      ]}
    >
      <Frame>
        <BarChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: -16 }}>
          <Grid />
          <DayAxis />
          <YAxis domain={[0, 21]} ticks={[0, 7, 14, 21]} {...axisProps} />
          <Tooltip
            {...tooltipProps}
            formatter={(value, _name, item) => {
              const p = item.payload as DayPoint;
              const workouts =
                p.workoutCount > 0
                  ? ` · ${p.workoutCount} workout${p.workoutCount > 1 ? "s" : ""}, ${p.workoutMinutes} min`
                  : "";
              return [`${String(value)}${workouts}`, "Strain"];
            }}
          />
          <Bar
            dataKey="strain"
            name="Strain"
            isAnimationActive={false}
            fill="var(--series-1)"
            radius={[4, 4, 0, 0]}
            maxBarSize={24}
          />
        </BarChart>
      </Frame>
    </ChartCard>
  );
}
