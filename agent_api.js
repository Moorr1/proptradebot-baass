// =============================================================================
// Web + Agent, step 1 (ops: web_agent_spec.md): the app on the customer's
// computer reports its live status and events; the dashboard and the setup
// assistant read them. READ-ONLY with respect to trading: nothing here can
// send an instruction to the Agent.
// =============================================================================

const { _test: { redact } } = require('./onboarding_agent');

const STATUS_MAX_BYTES = 32 * 1024;
const EVENT_MAX_BYTES = 4 * 1024;
const EVENTS_PER_POST = 100;
const EVENTS_PER_MINUTE = 300;          // per user; a busy day is far below this
const EVENT_RETENTION_DAYS = 30;
const ONLINE_SECONDS = 60;              // status every 15 s; 4 misses = offline

const EVENT_KINDS = new Set([
  // from the app's notifications (queue_notification event types)
  'ENTRY', 'ENTRY_REJECTED', 'ENTRY_BLOCKED_TIME', 'CIRCUIT_BREAKER', 'STOPPED', 'MES_STOPPED',
  'MES_T1_FILLED', 'MES_T2_FILLED', 'MES_TP_FILLED', 'MNQ_T1_FILLED', 'MNQ_T2_FILLED', 'MNQ_TP_FILLED',
  'AUTO_TRIM_T1', 'AUTO_TRIM_T2', 'RUNNER_ACTIVE', 'RUNNER_AUTO_CLOSE', 'RUNNER_CLOSED', 'MNQ_EXTERNAL_FLAT',
  'FLATTEN', 'EOD_FLATTEN', 'EMERGENCY_CLOSE', 'PHANTOM_DETECTED', 'SYNC_CORRECTION',
  'APEX_CUTOFF', 'APEX_EARLY_CLOSE', 'RITHMIC_DISCONNECTED', 'RITHMIC_ENTRY_FAILED',
  // from the reporter itself
  'ALERT', 'ERROR', 'WARNING', 'STARTED', 'CONFIG', 'OTHER',
]);

// Account ids and similar long digit runs are masked to the last 4, whatever
// the Agent sent: defence in depth in case an older Agent forgets.
// Token by token (linear time; a single regex over the whole string backtracked
// quadratically). A token with a run of 6+ digits is an id: keep the last 4 of
// its longest digit run. "50KTC-SKU-V2-DLL-308812-28064472" -> "50KTC-…4472",
// "PA-APEX-123456-01" -> "PA-…3456", "12345678" -> "…5678".
function maskIds(s) {
  return String(s).replace(/[A-Za-z0-9-]+/g, (tok) => {
    const runs = tok.match(/\d+/g);
    if (!runs) return tok;
    const longest = runs.reduce((a, b) => (b.length > a.length ? b : a));
    if (longest.length < 6) return tok;
    const head = tok.split('-')[0];
    const prefix = /[A-Za-z]/.test(head) && tok.includes('-') ? head + '-' : '';
    return prefix + '…' + longest.slice(-4);
  });
}

function clean(v, depth = 0) {
  if (depth > 5) return null;
  if (v == null) return v;
  if (typeof v === 'string') return maskIds(redact(v.slice(0, 500)).replace(/\/Users\/[^/\s]+/g, '/Users/…'));
  if (typeof v === 'number' || typeof v === 'boolean') return Number.isFinite(v) || typeof v === 'boolean' ? v : null;
  if (Array.isArray(v)) return v.slice(0, 50).map((x) => clean(x, depth + 1));
  if (typeof v === 'object') {
    const o = {};
    for (const [k, x] of Object.entries(v).slice(0, 60)) {
      if (/key|token|secret|password|passphrase|credential/i.test(k)) continue;
      o[String(k).slice(0, 60)] = clean(x, depth + 1);
    }
    return o;
  }
  return null;
}

const EVENTS_PER_DAY = 5000;             // a heavy trading day is a few hundred
const STATUS_MIN_INTERVAL_MS = 5000;     // the app sends every 15 s
const rate = new Map();   // user_id -> { minute, n, day, d, lastStatus }
function bucket(userId) {
  const minute = Math.floor(Date.now() / 60000), day = Math.floor(Date.now() / 86400000);
  let r = rate.get(userId);
  if (!r) { r = { minute, n: 0, day, d: 0, lastStatus: 0 }; rate.set(userId, r); }
  if (r.minute !== minute) { r.minute = minute; r.n = 0; }
  if (r.day !== day) { r.day = day; r.d = 0; }
  return r;
}
function allowEvents(userId, n) {
  const r = bucket(userId);
  if (r.n + n > EVENTS_PER_MINUTE || r.d + n > EVENTS_PER_DAY) return false;
  r.n += n; r.d += n;
  return true;
}
function allowStatus(userId) {
  const r = bucket(userId), now = Date.now();
  if (now - r.lastStatus < STATUS_MIN_INTERVAL_MS) return false;
  r.lastStatus = now;
  return true;
}

function mountAgentApi(app, pool, { requireApiKey, auth }) {
  // ---- Agent -> server --------------------------------------------------------
  app.post('/api/agent/status', requireApiKey, async (req, res) => {
    try {
      const raw = JSON.stringify(req.body || {});
      if (raw.length > STATUS_MAX_BYTES) return res.status(413).json({ success: false, error: 'status too large' });
      if (!allowStatus(req.botUser.id)) return res.status(429).json({ success: false, error: 'too frequent' });
      const s = clean(req.body || {});
      await pool.query(
        `INSERT INTO agent_status (user_id, payload, updated_at) VALUES ($1, $2, now())
         ON CONFLICT (user_id) DO UPDATE SET payload = EXCLUDED.payload, updated_at = now()`,
        [req.botUser.id, JSON.stringify(s)]);
      res.json({ success: true });
    } catch (e) {
      console.error('agent status error:', e.message);
      res.status(500).json({ success: false, error: 'internal' });
    }
  });

  app.post('/api/agent/events', requireApiKey, async (req, res) => {
    try {
      const list = Array.isArray(req.body && req.body.events) ? req.body.events : null;
      if (!list) return res.status(400).json({ success: false, error: 'events[] required' });
      if (list.length > EVENTS_PER_POST) return res.status(413).json({ success: false, error: `max ${EVENTS_PER_POST} events per call` });
      if (!allowEvents(req.botUser.id, list.length)) return res.status(429).json({ success: false, error: 'too many events' });
      // One multi-row INSERT: all or nothing, so a retried batch can't half-duplicate.
      const vals = [], params = [];
      for (const ev of list) {
        if (!ev || typeof ev !== 'object') continue;
        if (JSON.stringify(ev).length > EVENT_MAX_BYTES) continue;
        const k = String(ev.kind || '').toUpperCase();
        const kind = EVENT_KINDS.has(k) ? k : 'OTHER';
        let ts = new Date(ev.ts || Date.now());
        if (isNaN(ts) || Math.abs(Date.now() - ts) > 7 * 86400e3) ts = new Date();
        const i = params.length;
        vals.push(`($${i + 1}, $${i + 2}, $${i + 3}, $${i + 4})`);
        params.push(req.botUser.id, ts.toISOString(), kind, JSON.stringify(clean(ev)));
      }
      if (vals.length) await pool.query(`INSERT INTO agent_events (user_id, ts, kind, payload) VALUES ${vals.join(', ')}`, params);
      const stored = vals.length;
      res.json({ success: true, stored });
    } catch (e) {
      console.error('agent events error:', e.message);
      res.status(500).json({ success: false, error: 'internal' });
    }
  });

  // ---- Dashboard ----------------------------------------------------------------
  app.get('/api/user/agent', auth, async (req, res) => {
    try {
      const u = (await pool.query('SELECT id FROM users WHERE clerk_id = $1', [req.auth.userId])).rows[0];
      if (!u) return res.json({ success: true, status: null, events: [] });
      res.json({ success: true, ...(await readAgent(pool, u.id, 20)) });
    } catch (e) {
      console.error('user agent error:', e.message);
      res.status(500).json({ success: false, error: 'Could not load live status.' });
    }
  });

  // Retention: once a day, for everyone.
  const purge = () => pool.query(
    `DELETE FROM agent_events WHERE ts < now() - ($1 || ' days')::interval`, [String(EVENT_RETENTION_DAYS)]
  ).catch((e) => console.error('agent_events purge failed:', e.message));
  setTimeout(purge, 90 * 1000).unref();
  setInterval(purge, 24 * 3600 * 1000).unref();
}

async function readAgent(pool, userId, limit = 20, hours = null) {
  const st = (await pool.query('SELECT payload, updated_at FROM agent_status WHERE user_id = $1', [userId])).rows[0] || null;
  const ev = hours
    ? await pool.query(`SELECT ts, kind, payload FROM agent_events WHERE user_id = $1 AND ts > now() - ($2 || ' hours')::interval
                         ORDER BY ts DESC LIMIT $3`, [userId, String(hours), limit])
    : await pool.query('SELECT ts, kind, payload FROM agent_events WHERE user_id = $1 ORDER BY ts DESC LIMIT $2', [userId, limit]);
  const age = st ? (Date.now() - new Date(st.updated_at).getTime()) / 1000 : null;
  return {
    status: st ? { ...st.payload, reported_at: st.updated_at, online: age != null && age < ONLINE_SECONDS } : null,
    events: ev.rows.map((r) => {
      const p = r.payload || {};
      return { ...p, reported_kind: p.kind, ts: r.ts, kind: r.kind };   // server-validated columns win
    }),
  };
}

module.exports = { mountAgentApi, readAgent, _test: { clean, maskIds, allowEvents, allowStatus } };
