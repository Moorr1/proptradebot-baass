// Offline tests for page-view counting (pageviews.js). No network, no database.
//   node test/pageviews.test.js
const assert = require('assert');
const express = require('express');
const http = require('http');
const path = require('path');
const { mountPageviews, pageFor } = require('../pageviews');

const rows = [];
let failInsert = false;
const pool = {
  async query(sql, params) {
    if (/CREATE TABLE/.test(sql)) return {};
    if (failInsert) throw new Error('relation "page_views" does not exist');
    assert.ok(/INSERT INTO page_views/.test(sql));
    const [page, referrer_host, utm_source, is_bot, is_doc] = params;
    rows.push({ page, referrer_host, utm_source, is_bot, is_doc });
    return { rowCount: 1 };
  },
};
const app = express();
mountPageviews(app, pool, { publicDir: path.join(__dirname, '..', 'public') });
app.use(express.static(path.join(__dirname, '..', 'public'), { extensions: ['html'] }));
app.use((req, res) => res.status(404).send('nope'));
const server = app.listen(0);
const port = server.address().port;

const CHROME = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36';
const BROWSER = { 'User-Agent': CHROME, 'Sec-Fetch-Dest': 'document' };

function get(p, headers = BROWSER, method = 'GET') {
  return new Promise((resolve, reject) => {
    const r = http.request({ port, path: p, method, headers },
      (res) => { res.resume(); res.on('end', () => setTimeout(() => resolve(res.statusCode), 5)); });
    r.on('error', reject); r.end();
  });
}
const last = () => rows[rows.length - 1];

let failed = 0, passed = 0;
async function test(name, fn) {
  try { await fn(); passed++; console.log('  ok  ', name); }
  catch (e) { failed++; console.log('  FAIL', name, '\n       ', e.message); }
}

(async () => {
  await test('pageFor maps /, /x, /x.html, trailing slash; unknown -> null', async () => {
    const pages = new Set(['index', 'support']);
    assert.strictEqual(pageFor('/', pages), 'index');
    assert.strictEqual(pageFor('/support', pages), 'support');
    assert.strictEqual(pageFor('/support.html', pages), 'support');
    assert.strictEqual(pageFor('/support/', pages), 'support');
    assert.strictEqual(pageFor('/wp-login.php', pages), null);
    assert.strictEqual(pageFor('/index.html.bak_20261015_seo', pages), null);
  });
  await test('home page from Google: page index, referrer host, human', async () => {
    const s = await get('/', { ...BROWSER, Referer: 'https://www.google.com/' });
    assert.strictEqual(s, 200);
    assert.deepStrictEqual(last(), { page: 'index', referrer_host: 'www.google.com', utm_source: null, is_bot: false, is_doc: true });
  });
  await test('utm_source is lower-cased and sanitised; direct arrival = null referrer', async () => {
    await get('/support?utm_source=X<script>', BROWSER);
    assert.deepStrictEqual(last(), { page: 'support', referrer_host: null, utm_source: 'xscript', is_bot: false, is_doc: true });
  });
  await test('click between our own pages stores (self)', async () => {
    await get('/nt8-guide.html', { ...BROWSER, Referer: 'https://proptradebot.com/' });
    assert.strictEqual(last().referrer_host, '(self)');
    await get('/support', { ...BROWSER, Referer: 'https://WWW.proptradebot.com/x' });
    assert.strictEqual(last().referrer_host, '(self)');
  });
  await test('bot UA flagged; missing Sec-Fetch-Dest flagged', async () => {
    await get('/', { 'User-Agent': 'Mozilla/5.0 (compatible; Googlebot/2.1)' });
    assert.strictEqual(last().is_bot, true); assert.strictEqual(last().is_doc, false);
    await get('/', { 'User-Agent': 'curl/8.4.0' });
    assert.strictEqual(last().is_bot, true);
    await get('/', { 'User-Agent': CHROME });
    assert.strictEqual(last().is_bot, false); assert.strictEqual(last().is_doc, false);
  });
  await test('assets, API paths, 404 scans and POST are not counted', async () => {
    const n = rows.length;
    await get('/robots.txt'); await get('/img/nothing.png'); await get('/api/health');
    await get('/wp-login.php'); await get('/', BROWSER, 'POST');
    assert.strictEqual(rows.length, n);
  });
  await test('DB failure never breaks the page', async () => {
    failInsert = true;
    const s = await get('/support');
    failInsert = false;
    assert.strictEqual(s, 200);
  });
  server.close();
  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();
