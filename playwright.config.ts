import { defineConfig, devices } from "@playwright/test";

// E2e stack: mock CCC/NetBox/ISE on :9100, the app (serving the built SPA)
// on :8061. `make e2e` builds the frontend and copies it to app/static first.
export default defineConfig({
  testDir: "tests/e2e",
  timeout: 120_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1, // the wizard flows share one app DB + one mock state
  reporter: [["list"]],
  use: {
    baseURL: "http://127.0.0.1:8061",
    trace: "retain-on-failure",
    // Use a system/pre-provisioned Chromium instead of downloading one
    // (PW_CHROMIUM_PATH overrides; the default fits the Claude Code runner).
    launchOptions: process.env.PW_CHROMIUM_PATH
      ? { executablePath: process.env.PW_CHROMIUM_PATH }
      : undefined,
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
      testIgnore: /mobile\.spec\.ts/,
    },
    {
      name: "mobile",
      use: { ...devices["Pixel 5"] }, // chromium-based mobile profile
      testMatch: /mobile\.spec\.ts/,
    },
  ],
  webServer: [
    {
      command: "bash tests/e2e/serve-mocks.sh",
      url: "http://127.0.0.1:9100/__mock__/health",
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
    },
    {
      command: "bash tests/e2e/serve-app.sh",
      url: "http://127.0.0.1:8061/api/health",
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
    },
  ],
});
