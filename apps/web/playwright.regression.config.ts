import { defineConfig, devices } from "@playwright/test";

/**
 * The UI half of the regression suite (apps/api/tests/regression is the rest).
 *
 * It signs in to a THROWAWAY tenant created by `python -m tests.regression.framework.ui_tenant create`,
 * whose path is given by OPENCORTYX_UI_TENANT, so the browser never touches an operator's data.
 * Results land beside the backend results, where the report reads them.
 */
const out = process.env.OPENCORTYX_UI_RESULTS ?? "../api/test-results/dev/ui";

export default defineConfig({
  testDir: "./e2e/regression",
  outputDir: `${out}/artifacts`,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 180_000,
  expect: { timeout: 20_000 },
  reporter: [["list"], ["json", { outputFile: `${out}/playwright.json` }],
             ["html", { open: "never", outputFolder: `${out}/html` }]],
  globalSetup: "./e2e/regression/global-setup.ts",
  use: {
    baseURL: process.env.OPENCORTYX_WEB_URL ?? "http://localhost:3000",
    viewport: { width: 1440, height: 900 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [{ name: "regression", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } }],
});
