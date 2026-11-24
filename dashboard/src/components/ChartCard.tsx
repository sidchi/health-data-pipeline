import type { CSSProperties, ReactNode } from "react";

export interface Column<T> {
  header: string;
  value: (row: T) => string;
}

interface ChartCardProps<T> {
  title: string;
  description: string;
  children: ReactNode;
  /** Rows for the accessible table view; every chart ships one. */
  rows: T[];
  columns: Column<T>[];
  legend?: ReactNode;
  /** Span the full grid row. */
  wide?: boolean;
}

/**
 * Frame for one chart: a titled figure with an optional legend and a
 * collapsible data table, so no value is available only as a colored mark.
 */
export function ChartCard<T>({
  title,
  description,
  children,
  rows,
  columns,
  legend,
  wide = false,
}: ChartCardProps<T>) {
  return (
    <figure className={wide ? "card wide" : "card"} aria-label={title}>
      <figcaption>
        <h2>{title}</h2>
        <p className="muted">{description}</p>
      </figcaption>
      {legend}
      <div className="plot">{children}</div>
      <details>
        <summary>Data table</summary>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                {columns.map((c) => (
                  <th key={c.header} scope="col">
                    {c.header}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
                <tr key={i}>
                  {columns.map((c) => (
                    <td key={c.header}>{c.value(row)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </figure>
  );
}

export interface LegendItem {
  label: string;
  color: string;
  /** "line" | "dashed" | "swatch" — shape, so identity never rests on color alone. */
  shape?: "line" | "dashed" | "swatch";
}

export function Legend({ items }: { items: LegendItem[] }) {
  return (
    <ul className="legend">
      {items.map((item) => (
        <li key={item.label}>
          <span
            className={`key key-${item.shape ?? "swatch"}`}
            style={{ "--key-color": item.color } as CSSProperties}
            aria-hidden="true"
          />
          {item.label}
        </li>
      ))}
    </ul>
  );
}
