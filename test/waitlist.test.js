// Offline tests for the waitlist route (ops #4). No network, no database.
//   node test/waitlist.test.js
const assert = require('assert');
const express = require('express');
const http = require('http');
const { mountWaitlist } = require('../waitlist');

const rows = [];
let failInsert = false;
const pool = {
  async query(sql, params) {
    if (failInsert) throw new Error('relation "waitlist" does not exist');
    assert.ok(/INSERT INTO waitlist/.test(sql));
    const [email, handle, source, heard] = params;
    if (!rows.find((r) => r.email === email)) rows.push(heard === undefined ? { email, handle, source } : (heard === null ? { email, handle, source } : { email, handle, source, heard }));
    return { rowCount: 1 };
  },
};
let clock = 1_000_000;
const app = express();
app.use(express.json());
mountWaitlist(app, pool, { now: () => clock });
const server = app.listen(0);
const port = server.address().port;

function post(body, ip = '1.1.1.1') {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(body);
    const r = http.request({ port, path: '/api/waitlist', method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data), 'X-Forwarded-For': ip } },
    (res) => { let b = ''; res.on('data', (c) => (b += c)); res.on('end', () => resolve({ status: res.statusCode, json: JSON.parse(b) })); });
    r.on('error', reject); r.end(data);
  });
}

let failed = 0, passed = 0;
async function test(name, fn) {
  try { await fn(); passed++; console.log('  ok  ', name); }
  catch (e) { failed++; console.log('  FAIL', name, '\n       ', e.message); }
}

(async () => {
  await test('valid email is stored, lower-cased, with handle', async () => {
    const r = await post({ email: '  Alice@Example.COM ', handle: '@alice' });
    assert.strictEqual(r.status, 200); assert.strictEqual(r.json.success, true);
    assert.deepStrictEqual(rows[0], { email: 'alice@example.com', handle: '@alice', source: 'site' });
  });
  await test('heard: listed value stored, unknown value dropped, source sanitised', async () => {
    let r = await post({ email: 'h1@example.com', heard: 'reddit', source: 'X' }, '7.7.7.1');
    assert.strictEqual(r.status, 200);
    assert.deepStrictEqual(rows.find((x) => x.email === 'h1@example.com'), { email: 'h1@example.com', handle: null, source: 'x', heard: 'reddit' });
    r = await post({ email: 'h2@example.com', heard: '<script>', source: 'aff_<b>bob</b>' }, '7.7.7.2');
    assert.strictEqual(r.status, 200);
    assert.deepStrictEqual(rows.find((x) => x.email === 'h2@example.com'), { email: 'h2@example.com', handle: null, source: 'aff_bbobb' });
  });
  await test('duplicate email gets the same reply and is stored once', async () => {
    const r = await post({ email: 'alice@example.com' }, '2.2.2.2');
    assert.strictEqual(r.status, 200); assert.strictEqual(r.json.success, true);
    assert.strictEqual(rows.filter((x) => x.email === 'alice@example.com').length, 1);
  });
  await test('invalid email is rejected with 400', async () => {
    for (const email of ['', 'nope', 'a@b', 'a b@c.com', 'x'.repeat(250) + '@e.com']) {
      const r = await post({ email }, '3.3.3.3');
      assert.strictEqual(r.status, 400, email);
    }
  });
  await test('honeypot field: pretends success, stores nothing', async () => {
    const n = rows.length;
    const r = await post({ email: 'bot@spam.com', website: 'http://spam' }, '4.4.4.4');
    assert.strictEqual(r.json.success, true); assert.strictEqual(rows.length, n);
  });
  await test('rate limit: 6th signup from one IP within an hour is refused', async () => {
    for (let i = 0; i < 5; i++) assert.strictEqual((await post({ email: `u${i}@x.com` }, '5.5.5.5')).status, 200);
    assert.strictEqual((await post({ email: 'u6@x.com' }, '5.5.5.5')).status, 429);
  });
  await test('rate limit resets after an hour', async () => {
    clock += 60 * 60 * 1000 + 1;
    assert.strictEqual((await post({ email: 'u7@x.com' }, '5.5.5.5')).status, 200);
  });
  await test('missing table (migration not run): friendly 503, not a crash', async () => {
    failInsert = true;
    const r = await post({ email: 'late@x.com' }, '6.6.6.6');
    failInsert = false;
    assert.strictEqual(r.status, 503); assert.ok(/support@proptradebot.com/.test(r.json.error));
  });
  await test('handle is capped at 100 chars', async () => {
    await post({ email: 'long@x.com', handle: 'h'.repeat(300) }, '7.7.7.7');
    assert.strictEqual(rows.find((x) => x.email === 'long@x.com').handle.length, 100);
  });
  console.log(`\n${passed}/${passed + failed} passed`);
  server.close(); process.exit(failed ? 1 : 0);
})();
