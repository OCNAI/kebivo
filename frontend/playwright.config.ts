import { defineConfig, devices } from '@playwright/test';

// Run by scripts/e2e-ui.sh (make e2e-ui), which starts the e2e stack and sets E2E_*.
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [['list']],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://127.0.0.1:18000',
    trace: 'retain-on-failure',
    ...devices['Desktop Chrome'],
    viewport: { width: 1280, height: 800 },
    // CI installs Playwright's Chromium; set E2E_CHROMIUM_PATH to use another build.
    launchOptions: { executablePath: process.env.E2E_CHROMIUM_PATH || undefined },
  },
  projects: [{ name: 'chromium' }],
});
