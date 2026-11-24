import "@testing-library/jest-dom/vitest";

// jsdom has no layout engine. Recharts' ResponsiveContainer measures its parent
// with ResizeObserver, so give it a fixed-size stand-in.
class FixedSizeResizeObserver {
  constructor(private readonly callback: ResizeObserverCallback) {}
  observe(target: Element) {
    const entry = { target, contentRect: { width: 800, height: 240 } } as ResizeObserverEntry;
    this.callback([entry], this as unknown as ResizeObserver);
  }
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver = FixedSizeResizeObserver as unknown as typeof ResizeObserver;
