import { formatHours, type Summary } from "../data";

interface Tile {
  label: string;
  value: string;
}

/** Headline averages for the selected range. Missing days are excluded, not zeroed. */
export function SummaryTiles({ summary }: { summary: Summary }) {
  const show = (v: number | null, unit = "") => (v === null ? "—" : `${v}${unit}`);
  const tiles: Tile[] = [
    { label: "Avg recovery", value: show(summary.avgRecovery) },
    { label: "Avg HRV", value: show(summary.avgHrv, " ms") },
    { label: "Avg resting HR", value: show(summary.avgRestingHr, " bpm") },
    { label: "Avg sleep", value: formatHours(summary.avgSleepHours) },
    { label: "Avg strain", value: show(summary.avgStrain) },
    { label: "Workouts", value: String(summary.workouts) },
  ];
  return (
    <dl className="tiles" aria-label={`Summary of the last ${summary.days} days`}>
      {tiles.map((t) => (
        <div className="tile" key={t.label}>
          <dt>{t.label}</dt>
          <dd>{t.value}</dd>
        </div>
      ))}
    </dl>
  );
}
