const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const root = path.resolve(__dirname, '../build/dashboard-demo');
const prefix = '/Archon-Horizon/demo/';
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (!url.pathname.startsWith(prefix)) {res.writeHead(404); res.end('Not found'); return;}
  const relative = decodeURIComponent(url.pathname.slice(prefix.length)) || 'index.html';
  const file = path.resolve(root, relative);
  if (!file.startsWith(root + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
    res.writeHead(404); res.end('Not found'); return;
  }
  res.writeHead(200, {'Content-Type': file.endsWith('.js') ? 'application/javascript' : file.endsWith('.css') ? 'text/css' : file.endsWith('.html') ? 'text/html' : 'application/octet-stream'});
  fs.createReadStream(file).pipe(res);
});

(async () => {
  let browser, page;
  try {
    assert.ok(fs.existsSync(path.join(root, 'index.html')), 'Build the demo before its browser check.');
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const base = `http://127.0.0.1:${server.address().port}`;
    browser = await chromium.launch({headless: true, args: ['--no-sandbox']});
    page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    page.setDefaultTimeout(15000);
    const errors = [], forbidden = [], consoleErrors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => {if (message.type() === 'error') consoleErrors.push(message.text());});
    page.on('request', request => {
      const url = new URL(request.url());
      if (url.origin !== base || !url.pathname.startsWith(prefix)) forbidden.push(request.url());
    });
    await page.goto(`${base}${prefix}`);
    await page.getByLabel('Demo notice').waitFor();
    await page.getByText('demo-admin · admin', {exact: true}).waitFor();
    await page.getByRole('button', {name: 'Execution hosts', exact: true}).click();
    await page.getByRole('heading', {name: 'Proof worker', exact: true}).waitFor();
    await page.getByRole('heading', {name: 'Review worker', exact: true}).waitFor();
    await page.getByText('One synthetic proof session; three free slots.', {exact: true}).waitFor();
    await page.getByRole('button', {name: 'Agents', exact: true}).click();
    await page.getByRole('button', {name: /codex-example/}).waitFor();
    await page.getByRole('button', {name: /claude-example/}).waitFor();
    await page.getByRole('tab', {name: 'Reviewers', exact: true}).click();
    await page.getByRole('button', {name: /mathematical-fidelity/}).waitFor();
    await page.getByRole('tab', {name: 'Skills', exact: true}).click();
    await page.getByRole('heading', {name: 'horizon-pipeline', exact: true}).waitFor();
    await page.getByRole('button', {name: 'lean-check', exact: true}).click();
    await page.getByRole('heading', {name: 'lean-check', exact: true}).waitFor();
    await page.getByRole('button', {name: 'Planning mission', exact: true}).click();
    await page.getByRole('heading', {name: 'Planning mission', exact: true}).waitFor();
    await page.getByRole('tab', {name: 'Source', exact: true}).click();
    assert.ok((await page.locator('.instruction-source').innerText()).length > 20);
    await page.getByRole('tab', {name: 'Subagent library', exact: true}).click();
    await page.getByRole('heading', {name: 'lean-worker', exact: true}).waitFor();
    await page.getByRole('button', {name: 'build-checker', exact: true}).click();
    await page.getByRole('heading', {name: 'build-checker', exact: true}).waitFor();
    await page.getByRole('button', {name: 'Forge', exact: true}).click();
    await page.frameLocator('iframe[title="Forge"]').getByRole('heading', {name: 'Finite sums repository', exact: true}).waitFor();
    await page.frameLocator('iframe[title="Forge"]').getByRole('link', {name: 'Pull requests · 1', exact: true}).click();
    await page.reload();
    await page.frameLocator('iframe[title="Forge"]').getByRole('heading', {name: 'Finite sums repository', exact: true}).waitFor();
    await page.getByRole('button', {name: 'Zulip', exact: true}).click();
    await page.frameLocator('iframe[title="Zulip"]').getByRole('heading', {name: 'Finite sums discussions', exact: true}).waitFor();
    await page.frameLocator('iframe[title="Zulip"]').getByRole('link', {name: '# operations', exact: true}).click();
    await page.getByRole('button', {name: 'Accounts', exact: true}).click();
    await page.getByText('Administrator', {exact: true}).waitFor();
    await page.getByRole('button', {name: 'Projects', exact: true}).click();
    await page.getByRole('button', {name: /Finite sums A synthetic project/}).click();
    await page.getByRole('heading', {name: 'Finite sums', exact: true}).waitFor();
    assert.equal(new URL(page.url()).pathname, prefix);
    await page.getByRole('button', {name: 'Objectives', exact: true}).click();
    await page.getByRole('button', {name: /Formalize finite sums Revision/}).click();
    await page.getByRole('heading', {name: 'Formalize finite sums', exact: true}).waitFor();
    await page.locator('.platform-roadmap-item').first().waitFor();
    assert.equal(await page.locator('.platform-roadmap-item').count(), 4);
    await page.getByRole('button', {name: 'Nodes', exact: true}).click();
    await page.getByRole('button', {name: /Sum of the first n natural numbers/}).click();
    await page.getByRole('button', {name: 'DAG', exact: true}).click();
    await page.locator('svg g.node').first().waitFor();
    await page.reload();
    await page.locator('svg g.node').first().waitFor();
    assert.equal(new URL(page.url()).pathname, prefix);
    await page.getByRole('button', {name: 'Back to nodes', exact: true}).click();
    await page.getByRole('button', {name: 'References', exact: true}).click();
    await page.getByText('A synthetic introduction to finite sums', {exact: true}).first().waitFor();
    assert.equal(await page.getByRole('button', {name: /Add reference/}).count(), 0);
    await page.getByRole('button', {name: 'Back to projects', exact: true}).click();
    await page.getByRole('button', {name: 'Activity', exact: true}).click();
    await page.getByRole('button', {name: /Formalize finite sums/}).first().click();
    await page.getByRole('heading', {name: 'Formalize finite sums', exact: true}).waitFor();
    await page.getByRole('button', {name: 'Load older sessions', exact: true}).click();
    await page.getByRole('button', {name: /Prove the empty-sum case/}).waitFor();
    await page.getByRole('button', {name: 'Load older sessions', exact: true}).waitFor({state: 'hidden'});
    await page.getByRole('button', {name: /Complete the finite-sum theorem/}).first().click();
    await page.getByRole('tab', {name: 'Reports', exact: true}).click();
    await page.getByLabel('Goal ledger').waitFor();
    assert.equal(await page.getByRole('button', {name: 'Cancel run', exact: true}).count(), 0);
    assert.equal(await page.getByRole('button', {name: 'Sign out', exact: true}).isVisible(), false);
    const mutation = await page.evaluate(async () => (await fetch('/api/v3/commands', {method: 'POST', body: '{}'})).status);
    assert.equal(mutation, 403);
    assert.deepEqual(forbidden, [], `Demo contacted a network endpoint: ${forbidden.join(', ')}`);
    await page.goto(`${base}${prefix}?project=demo-project&node=sum-step`);
    await page.getByRole('heading', {name: 'Induction step', exact: true}).waitFor();
    assert.deepEqual(errors, []);
    assert.deepEqual(consoleErrors, []);
    console.log('Dashboard demo administrator, hosts, agent catalog, local Forge/Zulip and project navigation passed under /Archon-Horizon/demo/.');
  } catch (error) {
    if (page) console.error((await page.locator('body').innerText()).slice(0, 6000));
    throw error;
  } finally {
    await browser?.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
