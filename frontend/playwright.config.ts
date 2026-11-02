import { defineConfig } from "@playwright/test";

/**
 * Drives the real app in a real browser. Needs the whole stack running, with an LLM key:
 *   docker compose up -d --wait
 *   npm run e2e
 * It uses the installed Google Chrome, so nothing extra has to be downloaded.
 */
export default defineConfig({
  testDir: "./e2e",
  outputDir: "./e2e-artifacts/results",
  timeout: 180_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  reporter: "list",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000",
    channel: "chrome",
    viewport: { width: 1440, height: 1000 },
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
});
