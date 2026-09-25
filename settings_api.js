// =============================================================================
// Web + Agent step 2: ladder settings stored on the website, versioned.
// The app on the customer's computer is the authority: it re-validates every
// version against its own rules and local hard limits, applies risk-reducing
// changes itself, and asks the customer on that computer before anything that
// adds risk. Nothing here can place an order or bypass that approval.
// =============================================================================

const path = require('path');
const M = require('./settings_model');

const MAX_SETTINGS_BYTES = 16 * 1024;
const HISTORY = 20;

async function latest(pool, userId) {
  return (await pool.query(
    'SELECT version, settings, source, created_at, risk_reasons FROM settings_versions WHERE user_id = $1 ORDER BY version DESC LIMIT 1',
    [userId])).rows[0] || null;
}

function mountSettingsApi(app, pool, { requireApiKey, auth }) {
  // The settings page uses the same rules as the server (and the app).
  app.get('/js/settings_model.js', (req, res) => res.sendFile(path.join(__dirname, 'settings_model.js')));

  // ---- App -> server -------------------------------------------------------------
  // First connection of a 1.8+ app: its current local settings become version 1,
  // so the website starts from what is really running, never from defaults.
  app.post('/api/agent/settings/import', requireApiKey, async (req, res) => {
    try {
      const s = req.body && req.body.settings;
      if (JSON.stringify(s || {}).length > MAX_SETTINGS_BYTES) return res.status(413).json({ success: false, error: 'too large' });
      const errs = M.validate(s);
      if (errs.length) return res.status(400).json({ success: false, errors: errs });
      // The computer is the authority for what it runs. A version is added when
      // this is the first import, or when the app reports settings changed on
      // the computer (local_change). Identical settings never add a version.
      const cur = await latest(pool, req.botUser.id);
      if (cur && M.canonical(cur.settings) === M.canonical(s)) return res.json({ success: true, imported: false, version: cur.version });
      if (cur && !(req.body && req.body.local_change === true)) return res.json({ success: true, imported: false, version: cur.version });
      const next = cur ? cur.version + 1 : 1;
      const ins = await pool.query(
        `INSERT INTO settings_versions (user_id, version, settings, source, risk_reasons)
         VALUES ($1, $2, $3, 'app', '[]'::jsonb) ON CONFLICT (user_id, version) DO NOTHING RETURNING version`,
        [req.botUser.id, next, JSON.stringify(s)]);
      if (!ins.rows.length) return res.status(409).json({ success: false, error: 'version conflict, retry' });
      res.json({ success: true, imported: true, version: next });
    } catch (e) {
      console.error('settings import error:', e.message);
      res.status(500).json({ success: false, error: 'internal' });
    }
  });

  app.get('/api/agent/settings', requireApiKey, async (req, res) => {
    try {
      const cur = await latest(pool, req.botUser.id);
      res.json({ success: true, version: cur ? cur.version : 0, settings: cur ? cur.settings : null });
    } catch (e) {
      console.error('agent settings error:', e.message);
      res.status(500).json({ success: false, error: 'internal' });
    }
  });

  // ---- Settings page -----------------------------------------------------------------
  async function sessionUserId(req) {
    const u = (await pool.query('SELECT id FROM users WHERE clerk_id = $1', [req.auth.userId])).rows[0];
    return u ? u.id : null;
  }

  app.get('/api/user/settings', auth, async (req, res) => {
    try {
      const uid = await sessionUserId(req);
      if (!uid) return res.json({ success: true, current: null, running: null, history: [] });
      const hist = (await pool.query(
        `SELECT version, source, created_at, risk_reasons FROM settings_versions WHERE user_id = $1
          ORDER BY version DESC LIMIT $2`, [uid, HISTORY])).rows;
      const cur = await latest(pool, uid);
      const st = (await pool.query('SELECT payload, updated_at FROM agent_status WHERE user_id = $1', [uid])).rows[0];
      const running = st && st.payload && st.payload.settings ? { ...st.payload.settings, reported_at: st.updated_at } : null;
      res.json({ success: true, current: cur ? { version: cur.version, settings: cur.settings } : null, running, history: hist });
    } catch (e) {
      console.error('user settings error:', e.message);
      res.status(500).json({ success: false, error: 'Could not load your settings.' });
    }
  });

  app.post('/api/user/settings', auth, async (req, res) => {
    try {
      const uid = await sessionUserId(req);
      if (!uid) return res.status(404).json({ success: false, error: 'Open your dashboard once first.' });
      const { settings, base_version } = req.body || {};
      if (JSON.stringify(settings || {}).length > MAX_SETTINGS_BYTES) return res.status(413).json({ success: false, error: 'Too large.' });
      const cur = await latest(pool, uid);
      if (!cur) return res.status(409).json({ success: false, error: 'Your app hasn\'t shared its settings yet. Open PropTradeBot 1.8 or later on your computer first.' });
      if (base_version !== cur.version)
        return res.status(409).json({ success: false, error: `Settings changed since you opened this page (now version ${cur.version}). Reload and try again.`, version: cur.version });
      const errs = M.validate(settings, { knownRefs: (cur.settings.accounts || []).map((a) => a.ref) });
      if (errs.length) return res.status(400).json({ success: false, errors: errs });
      // Labels come from the app, never from the browser.
      const labels = new Map((cur.settings.accounts || []).map((a) => [a.ref, a]));
      settings.accounts = settings.accounts.map((a) => ({ ...labels.get(a.ref), enabled: a.enabled }));
      const reasons = M.riskIncrease(cur.settings, settings);
      const next = cur.version + 1;
      const ins = await pool.query(
        `INSERT INTO settings_versions (user_id, version, settings, source, risk_reasons)
         VALUES ($1, $2, $3, 'web', $4) ON CONFLICT (user_id, version) DO NOTHING RETURNING version`,
        [uid, next, JSON.stringify(settings), JSON.stringify(reasons)]);
      if (!ins.rows.length) return res.status(409).json({ success: false, error: 'Someone else saved at the same moment. Reload and try again.' });
      res.json({ success: true, version: next, needs_approval: reasons.length > 0, reasons });
    } catch (e) {
      console.error('user settings save error:', e.message);
      res.status(500).json({ success: false, error: 'Could not save. Try again.' });
    }
  });
}

module.exports = { mountSettingsApi };
