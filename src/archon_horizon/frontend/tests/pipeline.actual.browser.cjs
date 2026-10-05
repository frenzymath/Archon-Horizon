const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { randomUUID } = require('node:crypto');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

async function main() {
  assert(process.env.PIPELINE_TEST_ACCESS_FILE, 'Use an explicitly prepared, isolated API demo');
  const access = JSON.parse(fs.readFileSync(process.env.PIPELINE_TEST_ACCESS_FILE));
  assert(/^horizon_demo_[a-z0-9]+$/.test(access.database_name), 'Only a disposable Horizon demo database is allowed');
  const origin = new URL(access.url).origin;
  assert.equal(new URL(origin).hostname, '127.0.0.1');
  const output = process.env.PIPELINE_TEST_SCREENSHOTS;
  assert(output && path.isAbsolute(output), 'Use an explicit disk-backed artifact directory');
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  const results = [];
  try {
    for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
      const context = await browser.newContext({ viewport });
      const page = await context.newPage();
      const errors = [], failures = [];
      page.on('pageerror', error => errors.push(error.message));
      page.on('response', response => {
        if (response.url().includes('/api/v3/') && response.status() >= 500) failures.push([new URL(response.url()).pathname, response.status()]);
      });
      await page.goto(`${origin}/pipeline?project=${access.project_id}&run=${access.run_id}`, { waitUntil: 'domcontentloaded' });
      await page.getByLabel('Username').fill(access.username);
      await page.getByLabel('Password').fill(access.password);
      await page.getByRole('button', { name: 'Sign in', exact: true }).click();
      await page.getByText('Connected', { exact: true }).waitFor();
      await page.getByRole('button', { name: /^R\d+\/A\d+$/ }).first().waitFor();
      await page.screenshot({ path: path.join(output, `actual-work-${viewport.width}.png`), fullPage: true });
      for (const view of ['Roadmap & Graph', 'Changes', 'Discussions', 'Resources', 'Settings', 'Work']) {
        await page.getByRole('link', { name: view, exact: true }).click();
        await page.waitForTimeout(200);
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `${view} overflows ${viewport.width}px`);
        await page.screenshot({ path: path.join(output, `actual-${view.split(' ')[0].toLowerCase()}-${viewport.width}.png`), fullPage: true });
      }
      await page.getByRole('button', { name: /^R\d+\/A\d+$/ }).first().click();
      await page.getByLabel('Assignment detail').waitFor();
      for (const tab of ['Context', 'Activity', 'Changes', 'Diagnostics']) {
        await page.getByRole('tab', { name: tab, exact: true }).click();
        await page.waitForTimeout(100);
      }
      if (viewport.width === 1440) {
        const run = await (await context.request.get(`${origin}/api/v3/runs/${access.run_id}`)).json();
        const created = await context.request.post(`${origin}/api/v3/records/assignment`, {
          headers: { Origin: origin, 'Idempotency-Key': randomUUID() },
          data: { run_id: run.id, mission_id: run.mission_id, role: 'worker' },
        });
        assert(created.ok(), await created.text());
        const assignment = await created.json();
        await page.goto(`${origin}/pipeline?project=${access.project_id}&run=${run.id}&assignment=${assignment.id}`);
        await page.getByText('Connected', { exact: true }).waitFor();
        const ref = `R${run.number}/A${assignment.number}`;
        const move = page.waitForResponse(response => response.url().endsWith('/api/v3/commands') && response.request().method() === 'POST');
        await page.getByRole('button', { name: `Move ${ref} earlier`, exact: true }).click();
        const moved = await move;
        assert(moved.ok(), await moved.text());
        const request = moved.request();
        const replay = await context.request.post(request.url(), {
          headers: { Origin: origin, 'Idempotency-Key': request.headers()['idempotency-key'] },
          data: request.postDataJSON(),
        });
        assert(replay.ok(), await replay.text());
        assert.deepEqual(await replay.json(), await moved.json());
        await page.getByText('Start conditions', { exact: true }).click();
        const starts = new Date(Date.now() + 86400000).toISOString();
        await page.getByLabel('Not before', { exact: true }).fill(starts);
        const scheduling = page.waitForResponse(response => response.url().endsWith('/api/v3/commands') && response.request().method() === 'POST');
        await page.getByRole('button', { name: 'Update schedule', exact: true }).click();
        let scheduled = await scheduling;
        if (scheduled.status() === 409) {
          assert.equal(await page.getByLabel('Not before', { exact: true }).inputValue(), starts);
          await page.getByRole('button', { name: 'Reload changed schedule', exact: true }).click();
          await page.getByLabel('Not before', { exact: true }).fill(starts);
          const retry = page.waitForResponse(response => response.url().endsWith('/api/v3/commands') && response.request().method() === 'POST');
          await page.getByRole('button', { name: 'Update schedule', exact: true }).click();
          scheduled = await retry;
        }
        assert(scheduled.ok(), await scheduled.text());
        const after = await (await context.request.get(`${origin}/api/v3/assignments/${assignment.id}`)).json();
        assert.equal(Date.parse(after.not_before), Date.parse(starts), JSON.stringify({sent: scheduled.request().postDataJSON(), after: {not_before: after.not_before, error: after.error}, response: await scheduled.json()}));
        await context.setOffline(true);
        await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
        await page.getByText('Reconnecting', { exact: true }).waitFor({ timeout: 15000 });
        assert(await page.getByRole('button', { name: 'Cancel assignment', exact: true }).isDisabled());
        assert(await page.getByRole('heading', { name: ref, exact: true }).isVisible());
        await context.setOffline(false);
        await page.getByText('Connected', { exact: true }).waitFor({ timeout: 30000 });
        page.once('dialog', dialog => dialog.accept());
        const cancelling = page.waitForResponse(response => response.url().endsWith('/api/v3/commands') && response.request().method() === 'POST');
        await page.getByRole('button', { name: 'Cancel assignment', exact: true }).click();
        const cancelled = await cancelling;
        assert(cancelled.ok(), await cancelled.text());
        const final = await (await context.request.get(`${origin}/api/v3/assignments/${assignment.id}`)).json();
        assert.equal(final.status, 'cancelled');
      }
      await page.screenshot({ path: path.join(output, `actual-detail-${viewport.width}.png`), fullPage: true });
      assert.deepEqual(errors, []);
      assert.deepEqual(failures, []);
      results.push({ viewport, errors, failures });
      await context.close();
    }
  } finally { await browser.close(); }
  console.log(JSON.stringify(results, null, 2));
}
main().catch(error => { console.error(error); process.exitCode = 1; });
