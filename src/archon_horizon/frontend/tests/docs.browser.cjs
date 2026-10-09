const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const root = path.resolve(__dirname, '../../../../_site');
const prefix = '/Archon-Horizon/';
const server = http.createServer((request, response) => {
  const url = new URL(request.url, 'http://localhost');
  if (!url.pathname.startsWith(prefix)) {response.writeHead(404); response.end('Not found'); return;}
  const relative = decodeURIComponent(url.pathname.slice(prefix.length));
  const file = path.resolve(root, relative + (url.pathname.endsWith('/') ? 'index.html' : ''));
  if (!file.startsWith(root + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
    response.writeHead(404); response.end('Not found'); return;
  }
  response.writeHead(200, {'Content-Type': file.endsWith('.js') ? 'application/javascript' : file.endsWith('.css') ? 'text/css' : file.endsWith('.html') ? 'text/html' : 'application/octet-stream'});
  fs.createReadStream(file).pipe(response);
});

(async () => {
  let browser;
  try {
    assert.ok(fs.existsSync(path.join(root, 'index.html')), 'Build the documentation before its browser check.');
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const base = `http://127.0.0.1:${server.address().port}`;
    browser = await chromium.launch({headless: true, args: ['--no-sandbox']});
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    page.setDefaultTimeout(15000);
    const errors = [], consoleErrors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => {if (message.type() === 'error') consoleErrors.push(message.text());});
    await page.route('https://cdnjs.cloudflare.com/**', route => route.fulfill({status: 200,
      contentType: route.request().url().endsWith('.js') ? 'application/javascript' : 'text/css',
      body: route.request().url().endsWith('.js') ? 'window.hljs = {highlightAll() {}};' : ''}));
    const response = await page.goto(`${base}${prefix}`);
    assert.equal(response.status(), 200);
    await page.getByRole('heading', {name: 'Archon Horizon Documentation', exact: true}).waitFor();
    await page.locator('#theme-menu').click();
    await page.locator('[data-bs-theme-value="dark"]').click();
    assert.equal(await page.locator('html').getAttribute('data-bs-theme'), 'dark');
    assert.equal(await page.locator('#hljs-light').evaluate(element => element.disabled), true);
    assert.equal(await page.locator('#hljs-dark').evaluate(element => element.disabled), false);
    await page.reload();
    assert.equal(await page.locator('html').getAttribute('data-bs-theme'), 'dark');
    await page.locator('#theme-menu').click();
    await page.locator('[data-bs-theme-value="light"]').click();
    assert.equal(await page.locator('html').getAttribute('data-bs-theme'), 'light');
    await page.getByRole('link', {name: 'Releases', exact: true}).first().click();
    await page.getByRole('heading', {name: /Current Alpha/}).waitFor();
    assert.match(await page.locator('body').innerText(), /0\.2\.0-alpha\.2/);
    await page.getByRole('link', {name: 'Dashboard demo', exact: true}).first().click();
    await page.getByRole('link', {name: 'Open the published read-only dashboard', exact: true}).waitFor();
    assert.deepEqual(errors, []);
    assert.deepEqual(consoleErrors, []);
    console.log('Documentation browser navigation and persisted color mode passed under /Archon-Horizon/.');
  } finally {
    await browser?.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
