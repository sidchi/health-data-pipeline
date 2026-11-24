import { render, screen, within } from "@testing-library/react";
import { toSeries } from "../data";
import { HrvChart, niceScale, RecoveryChart, SleepChart, StrainChart } from "./Charts";

const points = toSeries(
  {
    "2026-01-01": {
      whoop: { readiness_score: 20, hrv_rmssd_ms: 41, strain: 15.2, sleep_time_asleep_s: 25200 },
    },
    "2026-01-02": { whoop: { readiness_score: 50, hrv_rmssd_ms: 52 } },
    "2026-01-03": { whoop: { readiness_score: 90, hrv_rmssd_ms: 63 } },
  },
  "whoop",
);

describe("niceScale", () => {
  it("pads to round ticks around the data", () => {
    expect(niceScale([43.4, null, 66], 10)).toEqual({
      domain: [30, 80],
      ticks: [30, 40, 50, 60, 70, 80],
    });
  });

  it("never goes below zero and copes with no data", () => {
    expect(niceScale([1, 2], 5).domain[0]).toBe(0);
    expect(niceScale([null], 5)).toEqual({ domain: [0, 5], ticks: [0, 5] });
  });
});

describe("RecoveryChart", () => {
  it("labels WHOOP zones in the legend and the data table", () => {
    render(<RecoveryChart points={points} source="whoop" />);
    const figure = screen.getByRole("figure", { name: "Recovery" });
    expect(within(figure).getByText("Green 67–100")).toBeInTheDocument();
    const rows = within(figure).getAllByRole("row");
    expect(rows.map((r) => r.textContent)).toEqual([
      "DayScoreZone",
      "2026-01-0120Red",
      "2026-01-0250Yellow",
      "2026-01-0390Green",
    ]);
  });

  it("does not apply WHOOP's zones to another vendor's score", () => {
    render(<RecoveryChart points={points} source="oura" />);
    const figure = screen.getByRole("figure", { name: "Recovery" });
    expect(within(figure).queryByText("Green 67–100")).not.toBeInTheDocument();
    expect(within(figure).queryByRole("columnheader", { name: "Zone" })).not.toBeInTheDocument();
    expect(within(figure).getByText(/as reported by oura/)).toBeInTheDocument();
  });
});

describe("other charts", () => {
  it("HRV names its statistic and shows the baseline column", () => {
    render(<HrvChart points={points} />);
    const figure = screen.getByRole("figure", { name: "Heart rate variability" });
    expect(within(figure).getByText("HRV (RMSSD)")).toBeInTheDocument();
    expect(within(figure).getByRole("columnheader", { name: "7-day mean" })).toBeInTheDocument();
  });

  it("sleep table shows durations, with a dash for missing nights", () => {
    render(<SleepChart points={points} />);
    const figure = screen.getByRole("figure", { name: "Sleep" });
    expect(within(figure).getByRole("cell", { name: "7h 00m" })).toBeInTheDocument();
    expect(within(figure).getAllByRole("cell", { name: "—" }).length).toBeGreaterThan(0);
  });

  it("renders an SVG chart", () => {
    const { container } = render(<StrainChart points={points} />);
    expect(container.querySelector("svg.recharts-surface")).not.toBeNull();
  });
});
