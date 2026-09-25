// Offline tests for the ops #3 / #13 changes. No network, no database.
//   node test/onboarding.test.js
// Loads the real server.js with pg, Clerk and Stripe stubbed, and the model API
// replaced by a scripted fake, then drives it over HTTP on a random port.

const assert = require('assert');
const Module = require('module');
const http = require('http');

// ---------------------------------------------------------------- fake DB
const db = {
  users: [
    { id: 'u1', clerk_id: 'clerk_alice', email: 'alice@example.com', plan_tier: 'pro', subscription_status: 'active',
      billing_source: 'whop', api_key: 'ptb_' + 'a'.repeat(64), tv_token: 't'.repeat(48), tv_passphrase: 'ptv_' + 'b'.repeat(24),
      last_bot_heartbeat: new Date(), last_alert_poll_at: null, last_tv_test_at: null, created_at: new Date() },
    { id: 'u2', clerk_id: 'clerk_bob', email: 'bob@example.com', plan_tier: 'pro', subscription_status: 'active',
      billing_source: 'whop', api_key: 'ptb_' + 'c'.repeat(64), tv_token: 'z'.repeat(48), tv_passphrase: 'ptv_' + 'd'.repeat(24),
      last_bot_heartbeat: null, created_at: new Date() },
  ],
  pending_alerts: [],
  onboarding_messages: [],
  onboarding_usage: [],
  support_tickets: [],
  agent_status: {},
  agent_events: [],
  settings_versions: [],
  queries: [],
};
const U = (id) => db.users.find((u) => u.id === id);

class FakePool {
  constructor() {}
  on() {}
  query(sql, params = [], cb) {
    if (typeof params === 'function') { cb = params; params = []; }
    const p = this._q(sql, params);
    if (cb) { p.then((r) => cb(null, r), (e) => cb(e)); return; }
    return p;
  }
  async _q(sql, p) {
    const q = sql.replace(/\s+/g, ' ').trim();
    db.queries.push({ q, p });
    const rows = (r) => ({ rows: r, rowCount: r.length });
    if (/^SELECT NOW\(\)/.test(q)) return rows([{ now: new Date() }]);
    if (/FROM users WHERE clerk_id = \$1/.test(q)) {
      const u = db.users.find((x) => x.clerk_id === p[0]);
      return rows(u ? [{ ...u }] : []);
    }
    if (/FROM users WHERE api_key = \$1/.test(q)) {
      const u = db.users.find((x) => x.api_key === p[0]);
      return rows(u ? [{ ...u, user_created_at: u.created_at }] : []);
    }
    if (/FROM users WHERE tv_token = \$1/.test(q)) {
      const u = db.users.find((x) => x.tv_token === p[0]);
      return rows(u ? [{ ...u }] : []);
    }
    if (/^UPDATE users SET last_tv_test_at/.test(q)) { U(p[0]).last_tv_test_at = new Date(); U(p[0]).last_tv_test_payload = p[1]; return rows([]); }
    if (/^UPDATE users SET last_alert_poll_at/.test(q)) { U(p[0]).last_alert_poll_at = new Date(); return rows([]); }
    if (/^INSERT INTO pending_alerts/.test(q)) { db.pending_alerts.push({ user_id: p[0], payload: p[1], source: p[2], created_at: new Date(), delivered_at: null, outcome: null }); return rows([]); }
    if (/^UPDATE pending_alerts SET delivered_at = now\(\), outcome = 'delivered'/.test(q)) {
      const got = db.pending_alerts.filter((a) => a.user_id === p[0] && !a.delivered_at && Date.now() - a.created_at < 60000);
      got.forEach((a) => { a.delivered_at = new Date(); a.outcome = 'delivered'; });
      return rows(got.map((a) => ({ id: 'x', payload: a.payload, source: a.source, created_at: a.created_at })));
    }
    if (/^UPDATE pending_alerts SET delivered_at = now\(\), outcome = 'expired'/.test(q)) {
      db.pending_alerts.filter((a) => a.user_id === p[0] && !a.delivered_at && Date.now() - a.created_at >= 60000)
        .forEach((a) => { a.delivered_at = new Date(); a.outcome = 'expired'; });
      return rows([]);
    }
    if (/SELECT plan_tier, subscription_status, COALESCE\(billing_source/.test(q)) {
      const u = U(p[0]); return rows([{ plan_tier: u.plan_tier, subscription_status: u.subscription_status, billing_source: u.billing_source, has_api_key: !!u.api_key, created_at: u.created_at }]);
    }
    if (/SELECT last_bot_heartbeat, last_alert_poll_at FROM users WHERE id/.test(q)) return rows([{ last_bot_heartbeat: U(p[0]).last_bot_heartbeat, last_alert_poll_at: U(p[0]).last_alert_poll_at }]);
    if (/FROM bot_heartbeats/.test(q)) return rows([{ version: '4.0.0', uptime_seconds: 60, alerts_processed: 0, positions_active: 0, created_at: new Date() }]);
    if (/SELECT tv_token, last_tv_test_at FROM users WHERE id/.test(q)) return rows([{ tv_token: U(p[0]).tv_token, last_tv_test_at: U(p[0]).last_tv_test_at }]);
    if (/SELECT max\(created_at\) AS t FROM pending_alerts/.test(q)) return rows([{ t: null }]);
    if (/FROM pending_alerts WHERE user_id = \$1 AND created_at > now\(\)/.test(q))
      return rows(db.pending_alerts.filter((a) => a.user_id === p[0]).map((a) => ({ ...a })));
    if (/FROM trades WHERE user_id/.test(q)) return rows([]);
    if (/^INSERT INTO support_tickets/.test(q)) { db.support_tickets.push({ user_id: p[0], email: p[1], reason: p[2], summary: p[3] }); return rows([{ id: 'ticket-1' }]); }
    if (/^DELETE FROM onboarding_messages/.test(q)) return rows([]);
    if (/FROM onboarding_messages WHERE user_id = \$1/.test(q))
      return rows(db.onboarding_messages.filter((m) => m.user_id === p[0]).map((m) => ({ role: m.role, content: m.content, created_at: m.t })));
    if (/^INSERT INTO onboarding_messages/.test(q)) {
      db.onboarding_messages.push({ user_id: p[0], role: 'user', content: p[1], t: Date.now() }, { user_id: p[0], role: 'assistant', content: p[2], t: Date.now() + 1 });
      return rows([]);
    }
    if (/SELECT turns FROM onboarding_usage/.test(q)) { const r = db.onboarding_usage.find((x) => x.user_id === p[0] && x.day === p[1]); return rows(r ? [r] : []); }
    if (/^INSERT INTO onboarding_usage/.test(q)) {
      let r = db.onboarding_usage.find((x) => x.user_id === p[0] && x.day === p[1]);
      if (!r) db.onboarding_usage.push(r = { user_id: p[0], day: p[1], turns: 0 });
      r.turns += 1; return rows([{ turns: r.turns }]);
    }
    if (/^UPDATE onboarding_usage SET input_tokens/.test(q)) return rows([]);
    if (/SELECT count\(\*\)::int AS n FROM support_tickets/.test(q)) return rows([{ n: db.support_tickets.filter((t) => t.user_id === p[0]).length }]);
    if (/^DELETE FROM onboarding_messages WHERE created_at/.test(q)) return rows([]);
    if (/^INSERT INTO agent_status/.test(q)) { db.agent_status[p[0]] = { payload: JSON.parse(p[1]), updated_at: new Date() }; return rows([]); }
    if (/FROM agent_status WHERE user_id = \$1/.test(q)) { const a = db.agent_status[p[0]]; return rows(a ? [{ ...a }] : []); }
    if (/^INSERT INTO agent_events/.test(q)) {
      for (let i = 0; i < p.length; i += 4) db.agent_events.push({ user_id: p[i], ts: new Date(p[i + 1]), kind: p[i + 2], payload: JSON.parse(p[i + 3]) });
      return rows([]);
    }
    if (/FROM agent_events WHERE user_id = \$1/.test(q)) return rows(db.agent_events.filter((e) => e.user_id === p[0]).slice().reverse().map((e) => ({ ...e })));
    if (/^DELETE FROM agent_events/.test(q)) return rows([]);
    if (/FROM settings_versions WHERE user_id = \$1 ORDER BY version DESC LIMIT 1/.test(q)) {
      const v = db.settings_versions.filter((x) => x.user_id === p[0]).sort((a, b) => b.version - a.version)[0];
      return rows(v ? [{ ...v }] : []);
    }
    if (/FROM settings_versions WHERE user_id = \$1/.test(q))
      return rows(db.settings_versions.filter((x) => x.user_id === p[0]).sort((a, b) => b.version - a.version).map((x) => ({ ...x })));
    if (/^INSERT INTO settings_versions/.test(q)) {
      const version = p[1];
      if (db.settings_versions.some((x) => x.user_id === p[0] && x.version === version)) return rows([]);
      const rec = /'app'/.test(q)
        ? { user_id: p[0], version, settings: JSON.parse(p[2]), source: 'app', risk_reasons: [], created_at: new Date() }
        : { user_id: p[0], version, settings: JSON.parse(p[2]), source: 'web', risk_reasons: JSON.parse(p[3]), created_at: new Date() };
      db.settings_versions.push(rec); return rows([{ version }]);
    }
    if (/FROM bot_configs|FROM accounts/.test(q)) return rows([]);
    throw new Error('FakePool: unhandled query: ' + q.slice(0, 120));
  }
}

// ---------------------------------------------------------------- stubs
const clerkStub = {
  ClerkExpressRequireAuth: () => (req, res, next) => {
    const id = req.headers['x-test-clerk'];
    if (!id) return next(new Error('unauthenticated'));
    req.auth = { userId: id, sessionClaims: {} };
    next();
  },
  clerkClient: { users: { getUser: async () => ({}) } },
};
const stripeStub = () => ({ accounts: { retrieve: async () => ({}) }, prices: { list: async () => ({ data: [] }) }, products: { list: async () => ({ data: [] }) } });
const origLoad = Module._load;
Module._load = function (req, parent, isMain) {
  if (req === 'pg') return { Pool: FakePool };
  if (req === '@clerk/clerk-sdk-node') return clerkStub;
  if (req === 'stripe') return stripeStub;
  return origLoad.apply(this, arguments);
};

// ---------------------------------------------------------------- fake model
let modelScript = [];          // queue of functions (body) => response
let modelCalls = [];
global.fetch = async (url, opts) => {
  assert.strictEqual(url, 'https://api.anthropic.com/v1/messages');
  const body = JSON.parse(opts.body);
  modelCalls.push(body);
  const step = modelScript.shift();
  if (!step) throw new Error('model called more times than scripted');
  const out = step(body);
  return { ok: true, status: 200, json: async () => out };
};
const say = (text) => () => ({ stop_reason: 'end_turn', content: [{ type: 'text', text }], usage: { input_tokens: 10, output_tokens: 5 } });
const use = (name, input = {}) => () => ({ stop_reason: 'tool_use', content: [{ type: 'tool_use', id: 'tu_' + name, name, input }], usage: { input_tokens: 10, output_tokens: 5 } });

// ---------------------------------------------------------------- boot
process.env.PORT = '0';
process.env.ANTHROPIC_API_KEY = 'test-key-not-real';
const realListen = require('express').application.listen;
let server;
require('express').application.listen = function (port, cb) { server = realListen.call(this, 0, cb); return server; };
require('../server.js');

function req(method, path, { headers = {}, body } = {}) {
  return new Promise((resolve, reject) => {
    const data = body === undefined ? null : (typeof body === 'string' ? body : JSON.stringify(body));
    const r = http.request({ host: '127.0.0.1', port: server.address().port, method, path,
      headers: { 'content-type': 'application/json', ...(data ? { 'content-length': Buffer.byteLength(data) } : {}), ...headers } }, (res) => {
      let s = ''; res.on('data', (c) => (s += c)); res.on('end', () => { let j = null; try { j = JSON.parse(s); } catch (e) {} resolve({ status: res.statusCode, body: j, text: s }); });
    });
    r.on('error', reject); if (data) r.write(data); r.end();
  });
}

const { _test: T } = require('../onboarding_agent');
const results = [];
async function test(name, fn) {
  try { await fn(); results.push(['PASS', name]); }
  catch (e) { results.push(['FAIL', name, e.message]); }
}

(async () => {
  await new Promise((r) => setTimeout(r, 200));
  const alice = { 'x-test-clerk': 'clerk_alice' };

  // ---------------- #13 server fixes
  await test('stripe-diag is gone', async () => {
    const r = await req('GET', '/api/stripe-diag');
    assert.notStrictEqual(r.status, 200); assert.ok(!/sk_live/.test(r.text));
  });
  await test('GET /api/bot/config without a key now reaches requireApiKey (401, not 400)', async () => {
    const r = await req('GET', '/api/bot/config'); assert.strictEqual(r.status, 401); assert.strictEqual(r.body.code, 'NO_API_KEY');
  });
  await test('GET /api/bot/config with a valid key works', async () => {
    const r = await req('GET', '/api/bot/config', { headers: { 'x-api-key': U('u1').api_key } }); assert.strictEqual(r.status, 200); assert.ok(r.body.success);
  });
  await test('API key in the query string is refused', async () => {
    const r = await req('GET', '/api/bot/config?apiKey=' + U('u1').api_key); assert.strictEqual(r.status, 401);
  });
  await test('TV webhook: wrong passphrase rejected', async () => {
    const r = await req('POST', '/tv/' + U('u1').tv_token, { body: { ticker: 'MES1!', action: 'buy', passphrase: 'nope' } }); assert.strictEqual(r.status, 401);
  });
  await test('TV webhook: TEST alert is recorded and NOT queued', async () => {
    const before = db.pending_alerts.length;
    const r = await req('POST', '/tv/' + U('u1').tv_token, { body: { ticker: 'MES1!', action: 'buy', passphrase: U('u1').tv_passphrase, test: true } });
    assert.strictEqual(r.status, 200); assert.strictEqual(r.body.test, true); assert.strictEqual(r.body.queued, false);
    assert.strictEqual(db.pending_alerts.length, before); assert.ok(U('u1').last_tv_test_at);
    assert.ok(!String(U('u1').last_tv_test_payload).includes(U('u1').tv_passphrase));
  });
  await test('TV webhook: test as the string "true" is also not queued', async () => {
    const before = db.pending_alerts.length;
    await req('POST', '/tv/' + U('u1').tv_token, { body: { ticker: 'MES1!', action: 'buy', passphrase: U('u1').tv_passphrase, test: 'true' } });
    assert.strictEqual(db.pending_alerts.length, before);
  });
  await test('TV webhook: a real alert is still queued', async () => {
    const before = db.pending_alerts.length;
    const r = await req('POST', '/tv/' + U('u1').tv_token, { body: { ticker: 'MES1!', action: 'buy', passphrase: U('u1').tv_passphrase } });
    assert.strictEqual(r.body.queued, true); assert.strictEqual(db.pending_alerts.length, before + 1);
  });
  await test('Poll: records last poll, marks delivered vs expired', async () => {
    db.pending_alerts.push({ user_id: 'u1', payload: '{}', source: 'tradingview', created_at: new Date(Date.now() - 120000), delivered_at: null, outcome: null });
    const r = await req('GET', '/api/bot/alerts', { headers: { 'x-api-key': U('u1').api_key } });
    assert.strictEqual(r.status, 200); assert.strictEqual(r.body.alerts.length, 1);
    assert.ok(U('u1').last_alert_poll_at);
    const outcomes = db.pending_alerts.filter((a) => a.user_id === 'u1').map((a) => a.outcome).sort();
    assert.deepStrictEqual(outcomes, ['delivered', 'expired']);
  });

  await test('PUT /api/bot/config is gone (no remote size/stop changes)', async () => {
    const r = await req('PUT', '/api/bot/config', { headers: { 'x-api-key': U('u1').api_key }, body: { contract_count: 50 } });
    assert.ok(r.status === 404, 'status ' + r.status);
  });
  await test('GET /api/bot/config never selects api_key_encrypted', async () => {
    db.queries = [];
    await req('GET', '/api/bot/config', { headers: { 'x-api-key': U('u1').api_key } });
    assert.ok(!db.queries.some((x) => /SELECT \* FROM accounts/.test(x.q)));
  });
  await test('TV webhook: "test": "yes" / 1 are tests too, "test": false is real', async () => {
    const before = db.pending_alerts.length;
    for (const t of ['yes', 1, 'TRUE']) await req('POST', '/tv/' + U('u1').tv_token, { body: { ticker: 'MES1!', action: 'buy', passphrase: U('u1').tv_passphrase, test: t } });
    assert.strictEqual(db.pending_alerts.length, before);
    await req('POST', '/tv/' + U('u1').tv_token, { body: { ticker: 'MES1!', action: 'buy', passphrase: U('u1').tv_passphrase, test: false } });
    assert.strictEqual(db.pending_alerts.length, before + 1);
    db.pending_alerts.pop();
  });
  await test('Poll still delivers when the poll-timestamp write fails', async () => {
    const orig = FakePool.prototype._q;
    FakePool.prototype._q = async function (sql, p) { if (/last_alert_poll_at = now\(\)/.test(sql)) throw new Error('column missing'); return orig.call(this, sql, p); };
    db.pending_alerts.push({ user_id: 'u1', payload: '{}', source: 'tradingview', created_at: new Date(), delivered_at: null, outcome: null });
    const r = await req('GET', '/api/bot/alerts', { headers: { 'x-api-key': U('u1').api_key } });
    FakePool.prototype._q = orig;
    assert.strictEqual(r.status, 200); assert.strictEqual(r.body.alerts.length, 1);
  });
  await test('secret detection: reviewer probe set', async () => {
    for (const s of ['rnyTaYD/ufDFvgTilTduDcvXIr8buHPUjq163jyxEGY', 'rnyTaYD_ufDFvgTilTduDcvXIr8buHPUjq163jyxEGY', 'ptb_ ' + 'ab'.repeat(32), 'ab'.repeat(32),
      'sec: 3f2504e0-4f89-11d3-9a0c-0305e82c3301', '"passphrase": "MyCustomPass99"', '{"api_key":"abcdef123456"}', 'my password is Hunter2Hunter2',
      'pass: Tr4d3r!2026', 'password: correcthorsebattery', 'https://x.com/tv/' + 'ab'.repeat(24), 'whsec_abcdefghijklmnop1234', 'sk-ant-api03-abcdefghijklmnopqrst'])
      assert.ok(T.containsSecret(s), 'missed: ' + s.slice(0, 14));
    for (const s of ['2026-09-24 10:32:01 INFO /Users/johnsmith/Library/Application Support/PropTradeBot/proptradebot.log opened',
      'CON.F.US.MES.Z26 order 1234567 filled', 'password reset link please', 'the token expired?'])
      assert.ok(!T.containsSecret(s), 'false positive: ' + s.slice(0, 30));
  });

  // ---------------- onboarding: unit
  await test('secret detection: catches real key shapes', async () => {
    for (const s of ['ptb_' + '1f'.repeat(20), 'my passphrase is ptv_0123456789abcdef', 'rnyTaYD/ufDFvgTilTduDcvXIr8buHPUjq163jyxEGY=',
      'sk_live_51Pqabcdefgh', '8510277665:AAFlAxVkT-Z2r_LK_yyLQ7LGJLvOly1aKts', 'password: Hunter22abc'])
      assert.ok(T.containsSecret(s), 'missed: ' + s.slice(0, 12));
  });
  await test('secret detection: no false positives on normal questions', async () => {
    for (const s of ["my api key: isn't working", 'Where do I paste my API key?', 'ES Long 7407 stop 7399', 'what is the passphrase for?',
      'I got error 401 invalid passphrase', 'MES 1-1-1 preset with 11 point stop'])
      assert.ok(!T.containsSecret(s), 'false positive: ' + s);
  });
  await test('redact() scrubs keys inside larger text', async () => {
    const out = T.redact('key=ptb_' + 'ab'.repeat(20) + ' ok');
    assert.ok(!/ptb_ab/.test(out) && /\[redacted\]/.test(out));
  });
  await test('check_settings: ladder that does not add up', async () => {
    const r = T.checkSettings({ legs: [{ instrument: 'MES', contracts: 7, t1_contracts: 5, t2_contracts: 1, runner_contracts: 2, initial_stop: 11, stop_after_t1: 4, t1_target: 4.5, t2_target: 11, runner_auto_close: 20 }] });
    assert.ok(!r.ok && r.warnings.some((w) => /don't add up/.test(w)));
  });
  await test('check_settings: clean MES ladder passes; accounts listed for confirmation', async () => {
    const r = T.checkSettings({ legs: [{ instrument: 'MES', contracts: 7, t1_contracts: 5, t2_contracts: 1, runner_contracts: 1, initial_stop: 11, stop_after_t1: 4, t1_target: 4.5, t2_target: 11, runner_auto_close: 20 }], broker_ladder: true, eod_auto_flatten: true, enabled_accounts: ['PRAC-1'], firm: 'Topstep' });
    assert.ok(r.ok, JSON.stringify(r.warnings));
  });
  await test('check_settings: missing stop, loosening stop, full-size, EOD off, blocked firm', async () => {
    const r = T.checkSettings({ legs: [{ instrument: 'ES', contracts: 1, t1_contracts: 1, t2_contracts: 0, runner_contracts: 0, initial_stop: 0 }, { instrument: 'MNQ', initial_stop: 10, stop_after_t1: 15 }], eod_auto_flatten: false, firm: 'Apex' });
    const j = r.warnings.join(' | ');
    for (const re of [/no initial stop/, /further away than the initial stop/, /full-size/, /EOD auto-flatten is off/, /Apex/]) assert.ok(re.test(j), 'missing ' + re);
  });
  await test('check_alert_format: good, and common mistakes', async () => {
    const ok = T.checkAlertFormat(JSON.stringify({ ticker: 'CME_MINI:MES1!', action: 'buy', price: 5000, passphrase: 'x' }));
    assert.ok(ok.ok && ok.parsed.instrument === 'MES' && ok.parsed.direction === 'buy');
    assert.ok(!T.checkAlertFormat('MES buy now').ok);
    const bad = T.checkAlertFormat(JSON.stringify({ ticker: 'AAPL', price: 1 }));
    assert.ok(bad.problems.length >= 3);
  });
  await test('no tool accepts a user id / email / key', async () => {
    for (const t of T.TOOL_DEFS) {
      const keys = JSON.stringify(t.input_schema);
      assert.ok(!/user_?id|email|api_?key|clerk/i.test(keys), t.name);
    }
  });
  await test('no tool can write to bot, orders, config or accounts', async () => {
    const names = T.TOOL_DEFS.map((t) => t.name);
    assert.deepStrictEqual(names.filter((n) => /order|trade_(place|close)|flatten|config_set|set_|update|enable|toggle/i.test(n)), []);
  });

  // ---------------- onboarding: routes
  await test('chat requires sign-in', async () => {
    const r = await req('POST', '/api/onboarding/chat', { body: { message: 'hi' } }); assert.strictEqual(r.status, 401);
  });
  await test('a pasted key never reaches the model or the database', async () => {
    modelCalls = []; modelScript = [];
    const key = 'rnyTaYD/ufDFvgTilTduDcvXIr8buHPUjq163jyxEGY=';
    const r = await req('POST', '/api/onboarding/chat', { headers: alice, body: { message: 'here is my topstep key ' + key } });
    assert.strictEqual(r.status, 200); assert.strictEqual(r.body.blocked, 'secret');
    assert.strictEqual(modelCalls.length, 0);
    assert.ok(!db.onboarding_messages.some((m) => m.content.includes(key)));
  });
  await test('tool loop: model asks for bot status, gets it, answers', async () => {
    modelCalls = []; modelScript = [use('get_bot_status'), say('Your app is online.')];
    const r = await req('POST', '/api/onboarding/chat', { headers: alice, body: { message: 'is my bot connected?' } });
    assert.strictEqual(r.status, 200, r.text); assert.strictEqual(r.body.reply, 'Your app is online.');
    assert.strictEqual(modelCalls.length, 2);
    const toolResult = modelCalls[1].messages.at(-1).content[0];
    assert.strictEqual(toolResult.type, 'tool_result'); assert.ok(/"online":true/.test(toolResult.content));
  });
  await test('tool results are redacted even if alert content holds a key', async () => {
    db.pending_alerts.push({ user_id: 'u1', payload: JSON.stringify({ text: 'MES long ptb_' + 'e'.repeat(64) }), source: 'tradingview', created_at: new Date(), delivered_at: null, outcome: null });
    modelCalls = []; modelScript = [use('get_recent_alerts', { hours: 2 }), say('done')];
    await req('POST', '/api/onboarding/chat', { headers: alice, body: { message: 'why no trade?' } });
    const c = modelCalls[1].messages.at(-1).content[0].content;
    assert.ok(!/ptb_eeee/.test(c) && /\[redacted\]/.test(c));
  });
  await test('tools are scoped to the session user even if the model passes another id', async () => {
    db.queries = [];
    modelCalls = []; modelScript = [use('get_account_status', { user_id: 'u2', email: 'bob@example.com' }), say('ok')];
    await req('POST', '/api/onboarding/chat', { headers: alice, body: { message: 'status' } });
    const q = db.queries.find((x) => /COALESCE\(billing_source/.test(x.q));
    assert.deepStrictEqual(q.p, ['u1']);
  });
  await test('unknown tool name is refused without crashing', async () => {
    modelCalls = []; modelScript = [use('place_order', { side: 'buy' }), say('I can’t do that.')];
    const r = await req('POST', '/api/onboarding/chat', { headers: alice, body: { message: 'buy 1 MES for me' } });
    assert.strictEqual(r.status, 200); assert.ok(/failed/.test(modelCalls[1].messages.at(-1).content[0].content));
  });
  await test('escalation limited to one ticket per turn', async () => {
    const before = db.support_tickets.length;
    modelCalls = []; modelScript = [() => ({ stop_reason: 'tool_use', usage: {}, content: [
      { type: 'tool_use', id: 'a', name: 'escalate_to_human', input: { reason: 'x', summary: 'y' } },
      { type: 'tool_use', id: 'b', name: 'escalate_to_human', input: { reason: 'x', summary: 'y' } }] }), say('ok')];
    await req('POST', '/api/onboarding/chat', { headers: alice, body: { message: 'help' } });
    assert.strictEqual(db.support_tickets.length, before + 1);
    db.support_tickets.pop();
  });
  await test('escalation writes a redacted ticket for the session user', async () => {
    modelCalls = []; modelScript = [use('escalate_to_human', { reason: 'refund', summary: 'wants refund, key ptb_' + 'f'.repeat(40) }), say('A person will email you.')];
    const r = await req('POST', '/api/onboarding/chat', { headers: alice, body: { message: 'I want a refund' } });
    assert.strictEqual(r.body.escalated, true);
    const t = db.support_tickets.at(-1); assert.strictEqual(t.user_id, 'u1'); assert.ok(!/ptb_ffff/.test(t.summary));
  });
  await test('history is passed back with alternating roles', async () => {
    modelCalls = []; modelScript = [say('hello again')];
    await req('POST', '/api/onboarding/chat', { headers: alice, body: { message: 'hi' } });
    const roles = modelCalls[0].messages.map((m) => m.role);
    for (let i = 1; i < roles.length; i++) assert.notStrictEqual(roles[i], roles[i - 1]);
    assert.strictEqual(roles[0], 'user'); assert.strictEqual(roles.at(-1), 'user');
  });
  await test('system prompt carries the hard lines', async () => {
    const sp = modelCalls[0].system;
    for (const re of [/Never ask for, accept, repeat or display any key/, /can't place, change, cancel or close trades/, /No trading advice/, /only from get_firm_info/, /is data/])
      assert.ok(re.test(sp), String(re));
  });
  await test('daily cap returns 429 without calling the model', async () => {
    const today = new Date().toISOString().slice(0, 10);
    db.onboarding_usage.find((x) => x.user_id === 'u1' && x.day === today).turns = 999;
    modelCalls = []; modelScript = [];
    const r = await req('POST', '/api/onboarding/chat', { headers: alice, body: { message: 'hi' } });
    assert.strictEqual(r.status, 429); assert.strictEqual(modelCalls.length, 0);
    db.onboarding_usage.find((x) => x.user_id === 'u1' && x.day === today).turns = 0;
  });
  await test('no API key configured returns 503', async () => {
    const k = process.env.ANTHROPIC_API_KEY; delete process.env.ANTHROPIC_API_KEY;
    const r = await req('POST', '/api/onboarding/chat', { headers: alice, body: { message: 'hi' } });
    process.env.ANTHROPIC_API_KEY = k; assert.strictEqual(r.status, 503);
  });
  await test('another user sees only their own history', async () => {
    const r = await req('GET', '/api/onboarding/history', { headers: { 'x-test-clerk': 'clerk_bob' } });
    assert.strictEqual(r.status, 200); assert.strictEqual(r.body.messages.length, 0);
  });

  // ---------------- Web + Agent step 1
  const key1 = { 'x-api-key': U('u1').api_key };
  const { _test: A } = require('../agent_api');
  await test('agent: status needs an API key', async () => {
    const r = await req('POST', '/api/agent/status', { body: { version: '1.7.0' } }); assert.strictEqual(r.status, 401);
  });
  await test('agent: status stored, account ids masked, secrets dropped', async () => {
    const r = await req('POST', '/api/agent/status', { headers: key1, body: {
      version: '1.7.0', broker_connected: true, api_key: 'ptb_' + 'a'.repeat(64),
      accounts: [{ label: '50KTC-SKU-V2-DLL-308812-28064472', enabled: true }], positions: [],
      last_error: 'login failed for key rnyTaYD/ufDFvgTilTduDcvXIr8buHPUjq163jyxEGY=' } });
    assert.strictEqual(r.status, 200, r.text);
    const st = db.agent_status.u1.payload;
    assert.strictEqual(st.api_key, undefined);
    assert.strictEqual(st.accounts[0].label, '50KTC-…4472');
    assert.ok(!/rnyTaYD/.test(st.last_error));
  });
  await test('agent: oversized status refused', async () => {
    const r = await req('POST', '/api/agent/status', { headers: key1, body: { junk: 'x'.repeat(40000) } });
    assert.ok(r.status === 413 || r.status === 400, 'status ' + r.status);
  });
  await test('agent: events stored with kind whitelist and batch limit', async () => {
    const r = await req('POST', '/api/agent/events', { headers: key1, body: { events: [
      { kind: 'ALERT', ts: new Date().toISOString(), source: 'tradingview', type: 'entry', direction: 'long', acted: false, reason: 'duplicate alert' },
      { kind: 'rm -rf', message: 'weird' },
      { kind: 'ENTRY', message: 'LONG 7 MES @ 5000 on 50KTC-V2-308812-78426344, P&L +$64.96' }] } });
    assert.strictEqual(r.body.stored, 3);
    assert.deepStrictEqual(db.agent_events.map((e) => e.kind), ['ALERT', 'OTHER', 'ENTRY']);
    assert.ok(/50KTC-…6344/.test(db.agent_events[2].payload.message));
    const big = await req('POST', '/api/agent/events', { headers: key1, body: { events: Array(101).fill({ kind: 'OTHER' }) } });
    assert.strictEqual(big.status, 413);
  });
  await test('agent: dashboard read is per signed-in user', async () => {
    const a = await req('GET', '/api/user/agent', { headers: alice });
    assert.strictEqual(a.status, 200); assert.ok(a.body.status.online); assert.strictEqual(a.body.events.length, 3);
    const b = await req('GET', '/api/user/agent', { headers: { 'x-test-clerk': 'clerk_bob' } });
    assert.strictEqual(b.body.status, null); assert.strictEqual(b.body.events.length, 0);
  });
  await test('agent: nothing on the Agent API can send instructions', async () => {
    for (const path of ['/api/agent/command', '/api/agent/config', '/api/agent/flatten']) {
      const r = await req('POST', path, { headers: key1, body: {} }); assert.strictEqual(r.status, 404, path);
    }
  });
  await test('assistant: get_bot_status includes live status; get_bot_events strips dollars', async () => {
    modelCalls = []; modelScript = [use('get_bot_status'), use('get_bot_events', { hours: 2 }), say('ok')];
    await req('POST', '/api/onboarding/chat', { headers: alice, body: { message: 'did my app get the alert?' } });
    const st = modelCalls[1].messages.at(-1).content[0].content;
    assert.ok(/"live_status":\{/.test(st) && /"broker_connected":true/.test(st), st.slice(0, 300));
    const ev = modelCalls[2].messages.at(-1).content[0].content;
    assert.ok(/duplicate alert/.test(ev)); assert.ok(!/64\.96/.test(ev), 'dollar amount leaked');
  });
  await test('agent: status rate-limited to one per 5 s per user', async () => {
    const r = await req('POST', '/api/agent/status', { headers: key1, body: { version: '1.7.0' } });
    assert.strictEqual(r.status, 429);
  });
  await test('agent: pathological strings cost < 50 ms (no regex blow-up)', async () => {
    const t = Date.now(); A.clean('a1'.repeat(15000)); A.clean('-'.repeat(30000)); A.clean('1'.repeat(30000));
    assert.ok(Date.now() - t < 50, (Date.now() - t) + 'ms');
  });
  await test('agent: Rithmic/Apex ids masked to the real last 4', async () => {
    assert.strictEqual(A.maskIds('PA-APEX-123456-01'), 'PA-…3456');
  });
  await test('agent: reported kind kept, validated kind wins', async () => {
    const r = await req('POST', '/api/agent/events', { headers: key1, body: { events: [{ kind: 'MES_T1_FILLED', message: 'T1 5c' }, { kind: 'SOMETHING_NEW' }] } });
    assert.strictEqual(r.body.stored, 2);
    const a = await req('GET', '/api/user/agent', { headers: alice });
    assert.strictEqual(a.body.events[0].kind, 'OTHER'); assert.strictEqual(a.body.events[0].reported_kind, 'SOMETHING_NEW');
    assert.strictEqual(a.body.events[1].kind, 'MES_T1_FILLED');
  });
  await test('assistant: money scrubbed in every format the app writes', async () => {
    const { _test: O } = require('../onboarding_agent');
    for (const m of ['daily loss limit hit ($-450.00)', 'Today P&L: $-450.00', 'P&L 64.96', '+64.96 USD', 'profit of 120.5', 'net +$194.88'])
      assert.ok(!/\d{2,}\.\d\d|450|194|120/.test(O.scrubMoney(m)), m + ' -> ' + O.scrubMoney(m));
    assert.strictEqual(O.scrubMoney('LONG 7 MES @ 7771.25 stop 7760.25'), 'LONG 7 MES @ 7771.25 stop 7760.25');
  });
  await test('agent: maskIds leaves prices and times alone', async () => {
    assert.strictEqual(A.maskIds('LONG @ 7771.25 at 13:01:16'), 'LONG @ 7771.25 at 13:01:16');
    assert.strictEqual(A.maskIds('acct 12345678'), 'acct …5678');
  });

  // ---------------- Web + Agent step 2: settings
  const M = require('../settings_model');
  const SV = require('./settings_vectors.json');
  await test('settings: shared rule vectors (validate + risk) all hold', async () => {
    for (const v of SV.vectors) {
      const e = M.validate(v.new);
      assert.strictEqual(e.length === 0, v.valid, v.name + ' ' + e.join('; '));
      if (v.valid && v.risk) {
        const r = M.riskIncrease(v.old, v.new);
        if (v.risk.length === 0) assert.deepStrictEqual(r, [], v.name);
        else v.risk.forEach((x) => assert.ok(r.some((y) => y.includes(x)), v.name + ': ' + r.join('; ')));
      }
    }
  });
  const key2 = { 'x-api-key': U('u1').api_key };
  const clone = (o) => JSON.parse(JSON.stringify(o));
  await test('settings: page says "no settings yet" before the app imports', async () => {
    const r = await req('GET', '/api/user/settings', { headers: alice });
    assert.strictEqual(r.body.current, null);
    const w = await req('POST', '/api/user/settings', { headers: alice, body: { settings: SV.base, base_version: 0 } });
    assert.strictEqual(w.status, 409);
  });
  await test('settings: app import creates version 1 once; invalid import refused', async () => {
    const bad = clone(SV.base); bad.legs.sp.runner_contracts = 3;
    assert.strictEqual((await req('POST', '/api/agent/settings/import', { headers: key2, body: { settings: bad } })).status, 400);
    const a = await req('POST', '/api/agent/settings/import', { headers: key2, body: { settings: SV.base } });
    assert.ok(a.body.imported && a.body.version === 1);
    const b = await req('POST', '/api/agent/settings/import', { headers: key2, body: { settings: SV.base } });
    assert.ok(!b.body.imported && b.body.version === 1);
  });
  await test('settings: a risk-reducing save needs no approval', async () => {
    const s2 = clone(SV.base); s2.legs.sp.stop = 8;
    const r = await req('POST', '/api/user/settings', { headers: alice, body: { settings: s2, base_version: 1 } });
    assert.strictEqual(r.status, 200, r.text); assert.strictEqual(r.body.version, 2); assert.strictEqual(r.body.needs_approval, false);
    const g = await req('GET', '/api/agent/settings', { headers: key2 });
    assert.strictEqual(g.body.version, 2); assert.strictEqual(g.body.settings.legs.sp.stop, 8);
  });
  await test('settings: a risk-adding save is flagged for approval, with reasons', async () => {
    const s3 = clone(SV.base); s3.legs.sp.stop = 15; s3.accounts[1].enabled = true;
    const r = await req('POST', '/api/user/settings', { headers: alice, body: { settings: s3, base_version: 2 } });
    assert.strictEqual(r.body.needs_approval, true);
    assert.ok(r.body.reasons.some((x) => /wider stop/.test(x)) && r.body.reasons.some((x) => /turned on/.test(x)), r.body.reasons.join('; '));
  });
  await test('settings: stale page (old base_version) is refused', async () => {
    const r = await req('POST', '/api/user/settings', { headers: alice, body: { settings: SV.base, base_version: 1 } });
    assert.strictEqual(r.status, 409);
  });
  await test('settings: the website cannot add accounts or rename them', async () => {
    const s4 = clone(SV.base); s4.accounts.push({ ref: 'a_1111111111111111', label: 'x', enabled: true });
    assert.strictEqual((await req('POST', '/api/user/settings', { headers: alice, body: { settings: s4, base_version: 3 } })).status, 400);
    const s5 = clone(SV.base); s5.accounts[0].label = 'EVIL'; s5.accounts[0].leader = true;
    const r = await req('POST', '/api/user/settings', { headers: alice, body: { settings: s5, base_version: 3 } });
    assert.strictEqual(r.status, 200, r.text);
    const g = await req('GET', '/api/agent/settings', { headers: key2 });
    assert.strictEqual(g.body.settings.accounts[0].label, '50KTC-…6344');
  });
  await test('settings: invalid settings from the page are refused with reasons', async () => {
    const s6 = clone(SV.base); s6.legs.sp.stop = 0;
    const r = await req('POST', '/api/user/settings', { headers: alice, body: { settings: s6, base_version: 4 } });
    assert.strictEqual(r.status, 400); assert.ok(r.body.errors.some((x) => /initial stop/.test(x)));
  });
  await test('settings: another user sees nothing and cannot save into this account', async () => {
    const bob = { 'x-test-clerk': 'clerk_bob' };
    assert.strictEqual((await req('GET', '/api/user/settings', { headers: bob })).body.current, null);
    assert.strictEqual((await req('POST', '/api/user/settings', { headers: bob, body: { settings: SV.base, base_version: 4 } })).status, 409);
  });
  await test('settings: app import with a local edit adds a version; identical import does not', async () => {
    const g = await req('GET', '/api/agent/settings', { headers: key2 });
    const same = await req('POST', '/api/agent/settings/import', { headers: key2, body: { settings: g.body.settings, local_change: true } });
    assert.strictEqual(same.body.imported, false);
    const edited = clone(g.body.settings); edited.legs.sp.stop = 9;
    const no = await req('POST', '/api/agent/settings/import', { headers: key2, body: { settings: edited } });
    assert.strictEqual(no.body.imported, false, 'without local_change nothing is created');
    const yes = await req('POST', '/api/agent/settings/import', { headers: key2, body: { settings: edited, local_change: true } });
    assert.strictEqual(yes.body.imported, true); assert.strictEqual(yes.body.version, g.body.version + 1);
  });
  await test('settings: page shows what the app reports as running', async () => {
    db.agent_status.u1 = { payload: { settings: { running: 2, pending: 4, state: 'needs_approval' } }, updated_at: new Date() };
    const r = await req('GET', '/api/user/settings', { headers: alice });
    assert.strictEqual(r.body.current.version, 5); assert.strictEqual(r.body.running.state, 'needs_approval'); assert.ok(r.body.history.length >= 5);
  });
  await test('settings: model is served to the settings page', async () => {
    const r = await req('GET', '/js/settings_model.js'); assert.strictEqual(r.status, 200); assert.ok(/window.PTBSettings/.test(r.text));
  });

  for (const [s, n, e] of results) console.log(`${s}  ${n}${e ? '\n      ' + e : ''}`);
  const failed = results.filter((r) => r[0] === 'FAIL').length;
  console.log(`\n${results.length - failed}/${results.length} passed`);
  server.close(); process.exit(failed ? 1 : 0);
})();
