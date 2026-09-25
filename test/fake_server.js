// Boots the real server.js with a fake DB for cross-language contract tests.
//   TEST_PORT=4700 node test/fake_server.js
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
    if (/FROM settings_versions WHERE user_id = \$1 AND version = \$2/.test(q)) {
      const v = db.settings_versions.find((x) => x.user_id === p[0] && x.version === p[1]); return rows(v ? [{ ...v }] : []);
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
process.env.PORT = process.env.TEST_PORT || '0';
process.env.ANTHROPIC_API_KEY = 'test-key-not-real';
const realListen = require('express').application.listen;
let server;
require('express').application.listen = function (port, cb) { server = realListen.call(this, Number(process.env.TEST_PORT || 0), cb); return server; };
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
// Test-only dump server on TEST_PORT+1: lets the Python Agent tests read what server.js stored.
http.createServer((q, r) => {
  const st = db.agent_status.u1 ? db.agent_status.u1.payload : null;
  r.setHeader('content-type', 'application/json');
  r.end(JSON.stringify({ status: st, events: db.agent_events.map((e) => ({ kind: e.kind, ...e.payload })) }));
}).listen(Number(process.env.TEST_PORT) + 1, '127.0.0.1');
console.log('fake server.js up on', process.env.TEST_PORT);
