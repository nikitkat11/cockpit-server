// Playwright boots the real cockpit server against the fixture vault, waits for
// it to answer, runs the specs, then tears it down. No manual setup step — a
// test you have to remember to prepare is a test that stops being run.

const { defineConfig, devices } = require('@playwright/test');

module.exports = defineConfig({
  testDir: '.',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['github'], ['html', { open: 'never' }]] : 'list',

  use: {
    baseURL: 'http://127.0.0.1:8090',
    trace: 'on-first-retry',
  },

  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],

  webServer: {
    // COCKPIT_NO_BROWSER stops the server opening a Chrome window on start,
    // which would otherwise happen on every local test run.
    command: 'COCKPIT_NO_BROWSER=1 python3 ../cockpit.py --vault ./fixture-vault',
    url: 'http://127.0.0.1:8090',
    reuseExistingServer: !process.env.CI,
    timeout: 30 * 1000,
    stdout: 'pipe',
    stderr: 'pipe',
  },
});
