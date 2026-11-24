import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "./App";

const twoSources = {
  "2026-01-01": { whoop: { readiness_score: 40 }, oura: { readiness_score: 90 } },
  "2026-01-02": { whoop: { readiness_score: 80 } },
};

const serve = (json: unknown) => () => Promise.resolve(json);

function tile(label: string) {
  const summary = screen.getByRole("definition", {
    name: (_, el) => el.previousElementSibling?.textContent === label,
  });
  return summary.textContent;
}

describe("App", () => {
  it("loads the sample and labels it as synthetic", async () => {
    render(<App fetchJson={serve(twoSources)} />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading");
    expect(await screen.findByText(/Synthetic sample data/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Recovery" })).toBeInTheDocument();
  });

  it("defaults to the source with the most days and can switch", async () => {
    const user = userEvent.setup();
    render(<App fetchJson={serve(twoSources)} />);
    const select = await screen.findByRole("combobox", { name: "Source" });
    expect(select).toHaveValue("whoop");
    expect(tile("Avg recovery")).toBe("60");

    await user.selectOptions(select, "oura");
    expect(tile("Avg recovery")).toBe("90");
  });

  it("filters the date range", async () => {
    const user = userEvent.setup();
    render(<App fetchJson={serve(twoSources)} />);
    await screen.findByRole("combobox", { name: "Source" });
    const range = screen.getByRole("radiogroup", { name: "Date range" });

    await user.click(within(range).getByRole("radio", { name: "7 days" }));
    expect(within(range).getByRole("radio", { name: "7 days" })).toBeChecked();
    expect(tile("Avg recovery")).toBe("60");
    expect(screen.getByText("Jan 1 – Jan 2")).toBeInTheDocument();
  });

  it("shows a readable error for invalid data", async () => {
    render(<App fetchJson={serve({ schema_version: 1, records: [] })} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/--daily/);
  });

  it("shows a fetch failure", async () => {
    render(<App fetchJson={() => Promise.reject(new Error("GET x failed: 404"))} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("GET x failed: 404");
  });

  it("handles an export with no days", async () => {
    render(<App fetchJson={serve({})} />);
    expect(await screen.findByText("This file has no days with data.")).toBeInTheDocument();
  });

  it("opens a local file without uploading it", async () => {
    const user = userEvent.setup();
    render(<App fetchJson={serve(twoSources)} />);
    await screen.findByRole("combobox", { name: "Source" });

    const mine = { "2026-05-01": { oura: { readiness_score: 77 } } };
    const file = new File([JSON.stringify(mine)], "my-daily.json", { type: "application/json" });
    await user.upload(screen.getByLabelText("Open daily.json…"), file);

    expect(await screen.findByText("my-daily.json")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Source" })).toHaveValue("oura");
    expect(tile("Avg recovery")).toBe("77");
  });

  it("reports a broken local file by name", async () => {
    const user = userEvent.setup();
    render(<App fetchJson={serve(twoSources)} />);
    await screen.findByRole("combobox", { name: "Source" });
    const file = new File(["{not json"], "broken.json", { type: "application/json" });
    await user.upload(screen.getByLabelText("Open daily.json…"), file);
    expect(await screen.findByRole("alert")).toHaveTextContent(/^Couldn’t load data: broken.json:/);
  });
});
