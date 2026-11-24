import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  // Relative asset paths so the static build works from any sub-path
  // (GitHub Pages, a bucket prefix, or `file://` via `vite preview`).
  base: "./",
  build: {
    rollupOptions: {
      // Keep the charting library in its own long-cacheable chunk.
      output: { manualChunks: { recharts: ["recharts"] } },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
