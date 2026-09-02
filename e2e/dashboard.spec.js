// End-to-end tests for the dashboard.
//
// The Python suite proves the server returns correct JSON. It cannot prove the
// page renders that JSON, because the UI is 1,300 lines of vanilla JS with no
// module boundary to unit-test. So these run a real browser against a real
// server reading a real (fixture) vault, and assert the round trip:
//
//     markdown on disk -> parser -> JSON -> fetch -> DOM
//
// Deliberately few and deliberately shallow. E2E tests are the slowest and
// flakiest thing in any suite, so they cover the paths that would embarrass the
// project if broken, and nothing else.

const { test, expect } = require('@playwright/test');

test.describe('dashboard', () => {
  test('loads and lands on the Command tab', async ({ page }) => {
    await page.goto('/');
    await expect(page).toHaveTitle('AGENTIC//OS');
    await expect(page.locator('#tab-title')).toHaveText('Command');
    await expect(page.locator('.nav-item')).toHaveCount(8);
  });

  test('renders projects read from the vault on disk', async ({ page }) => {
    // The full round trip: this string exists only in e2e/fixture-vault/.
    await page.goto('/');
    await expect(page.locator('#projects')).toContainText('P - Example Project', {
      timeout: 10000,
    });
  });

  test('the project count badge reflects the vault', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('#n-projects')).toHaveText('2', { timeout: 10000 });
  });

  test('clicking a nav item switches the visible tab', async ({ page }) => {
    await page.goto('/');
    await page.locator('[data-tab="projects"]').click();

    await expect(page.locator('#tab-title')).toHaveText('Operations');
    await expect(page.locator('#tab-projects')).toHaveClass(/\bon\b/);
    await expect(page.locator('#tab-overview')).not.toHaveClass(/\bon\b/);
  });

  test('the selected tab survives a reload', async ({ page }) => {
    // The UI persists the tab in localStorage and replays it on boot.
    // Uses "system" rather than "brain" on purpose: clicking brain kicks off
    // initBrain() and a force-directed graph render, which is a lot of work to
    // drag into a test about localStorage.
    await page.goto('/');
    await page.locator('[data-tab="system"]').click();
    await expect(page.locator('#tab-title')).toHaveText('Core');

    await page.reload();
    await expect(page.locator('#tab-title')).toHaveText('Core');
  });

  test('boots without an uncaught exception', async ({ page }) => {
    // Scoped to uncaught page errors rather than all console output: the
    // optional LLM proxy on :8082 is not running here, and a failed fetch for
    // an optional feature is expected noise, not a defect.
    const crashes = [];
    page.on('pageerror', (err) => crashes.push(err.message));

    await page.goto('/');

    // Wait for the vault fetch to land and render before judging the page.
    // Deliberately NOT waitFor({ state: 'visible' }): #projects sits inside the
    // Operations tab, which is display:none until clicked, so on first load it
    // is present and populated but never visible. Asserting on its text waits
    // for the same thing without requiring it to be on screen.
    await expect(page.locator('#projects')).toContainText('P - Example Project', {
      timeout: 10000,
    });
    await page.waitForTimeout(500);

    expect(crashes).toEqual([]);
  });

  test('the optional LLM proxy being down does not break the page', async ({ page }) => {
    // Uplink needs a local proxy that most people cloning this will not run.
    // The rest of the dashboard must still work without it.
    await page.goto('/');
    await page.locator('[data-tab="chat"]').click();
    await expect(page.locator('#tab-chat')).toHaveClass(/\bon\b/);

    // and the vault-backed tabs still render
    await page.locator('[data-tab="projects"]').click();
    await expect(page.locator('#projects')).toContainText('P - Example Project');
  });
});
