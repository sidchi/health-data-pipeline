import { type ChangeEvent, useEffect, useMemo, useState } from "react";
import {
  HrvChart,
  RecoveryChart,
  RestingHrChart,
  SleepChart,
  StrainChart,
} from "./components/Charts";
import { SummaryTiles } from "./components/SummaryTiles";
import {
  type DailyExport,
  DataError,
  lastNDays,
  listSources,
  parseDailyExport,
  shortDate,
  summarize,
  toSeries,
} from "./data";

export const DEFAULT_DATA_URL = import.meta.env.VITE_DAILY_URL ?? "data/daily.json";

const RANGES = [
  { label: "7 days", days: 7 },
  { label: "14 days", days: 14 },
  { label: "28 days", days: 28 },
  { label: "All", days: null },
] as const;

type State =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; data: DailyExport; origin: string };

function describe(error: unknown): string {
  if (error instanceof DataError || error instanceof SyntaxError) return error.message;
  return error instanceof Error ? error.message : String(error);
}

interface AppProps {
  /** Injected in tests; defaults to the browser's fetch. */
  fetchJson?: (url: string) => Promise<unknown>;
  dataUrl?: string;
}

/** FileReader rather than Blob.text(): the latter is missing from jsdom and older Safari. */
function readText(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error ?? new Error("could not read file"));
    reader.readAsText(file);
  });
}

async function defaultFetchJson(url: string): Promise<unknown> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`GET ${url} failed: ${response.status}`);
  return response.json();
}

export function App({ fetchJson = defaultFetchJson, dataUrl = DEFAULT_DATA_URL }: AppProps) {
  const [state, setState] = useState<State>({ status: "loading" });
  const [source, setSource] = useState<string | null>(null);
  const [rangeDays, setRangeDays] = useState<number | null>(28);

  useEffect(() => {
    let cancelled = false;
    fetchJson(dataUrl)
      .then((json) => {
        if (!cancelled)
          setState({ status: "ready", data: parseDailyExport(json), origin: "sample" });
      })
      .catch((error: unknown) => {
        if (!cancelled) setState({ status: "error", message: describe(error) });
      });
    return () => {
      cancelled = true;
    };
  }, [fetchJson, dataUrl]);

  // A file the viewer picks is parsed in the browser and never uploaded anywhere.
  async function onPickFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      const data = parseDailyExport(JSON.parse(await readText(file)));
      setSource(null);
      setState({ status: "ready", data, origin: file.name });
    } catch (error) {
      setState({ status: "error", message: `${file.name}: ${describe(error)}` });
    }
  }

  const sources = useMemo(() => (state.status === "ready" ? listSources(state.data) : []), [state]);
  const activeSource = source !== null && sources.includes(source) ? source : (sources[0] ?? null);
  const series = useMemo(
    () => (state.status === "ready" && activeSource ? toSeries(state.data, activeSource) : []),
    [state, activeSource],
  );
  const points = lastNDays(series, rangeDays);
  const first = points[0];
  const last = points.at(-1);

  return (
    <main className="page">
      <header className="header">
        <div>
          <h1>Health trends</h1>
          <p className="muted">
            Recovery, sleep and strain from the unified <code>--daily</code> view.{" "}
            {state.status === "ready" && state.origin === "sample" && (
              <strong>Synthetic sample data — not a real person.</strong>
            )}
            {state.status === "ready" && state.origin !== "sample" && (
              <>
                Showing <strong>{state.origin}</strong> (read locally, not uploaded).
              </>
            )}
          </p>
        </div>
        <label className="file-button">
          Open daily.json…
          <input type="file" accept="application/json,.json" onChange={onPickFile} />
        </label>
      </header>

      {state.status === "loading" && <p role="status">Loading…</p>}
      {state.status === "error" && (
        <p role="alert" className="error">
          Couldn’t load data: {state.message}
        </p>
      )}

      {state.status === "ready" && activeSource === null && (
        <p role="status">This file has no days with data.</p>
      )}

      {state.status === "ready" && activeSource !== null && (
        <>
          <div className="controls" role="group" aria-label="Filters">
            <label>
              Source{" "}
              <select value={activeSource} onChange={(e) => setSource(e.target.value)}>
                {sources.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </label>
            <div className="segmented" role="radiogroup" aria-label="Date range">
              {RANGES.map((r) => (
                <button
                  key={r.label}
                  type="button"
                  role="radio"
                  aria-checked={rangeDays === r.days}
                  onClick={() => setRangeDays(r.days)}
                >
                  {r.label}
                </button>
              ))}
            </div>
            {first && last && (
              <span className="muted range-label">
                {shortDate(first.day)} – {shortDate(last.day)}
              </span>
            )}
          </div>

          <SummaryTiles summary={summarize(points)} />

          <div className="grid">
            <RecoveryChart points={points} source={activeSource} />
            <StrainChart points={points} />
            <HrvChart points={points} />
            <RestingHrChart points={points} />
            <SleepChart points={points} />
          </div>
        </>
      )}
    </main>
  );
}
