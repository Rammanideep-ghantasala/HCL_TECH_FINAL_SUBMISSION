import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

/** In development the API runs separately; in production it is the same origin. */
const DEV_API_TARGET = "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": DEV_API_TARGET,
      "/health": DEV_API_TARGET,
      "/ready": DEV_API_TARGET,
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    restoreMocks: true,
    // Whole-app jsdom tests run several files in parallel; the first test in a
    // file can take over the 5 s default on a loaded machine. No test relies on
    // real time (the search timeout test uses fake timers).
    testTimeout: 15_000,
  },
});
