// Waitlist signups while sales are closed (ops #4).
// POST /api/waitlist {email, handle?, source?, heard?} -> stores the email once.
// source = the link's utm_source (or the page), heard = "How did you hear about us?" (ops #39).
// The reply is the same whether or not the email was already on the list, so
// the form can't be used to find out who signed up.

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;
const WINDOW_MS = 60 * 60 * 1000;
const MAX_PER_WINDOW = 5;
const HEARD = new Set(['x', 'youtube', 'whop', 'reddit', 'discord', 'tradingview', 'topstep', 'search', 'friend', 'other']);
const OK_MESSAGE = "You're on the list. We'll email you when your invite is ready.";

function clientIp(req) {
  const fwd = String(req.headers['x-forwarded-for'] || '').split(',')[0].trim();
  return fwd || (req.socket && req.socket.remoteAddress) || '?';
}

function mountWaitlist(app, pool, { now = () => Date.now() } = {}) {
  const hits = new Map();
  app.post('/api/waitlist', async (req, res) => {
    const body = req.body || {};
    // Hidden field real visitors never fill in; bots do. Pretend success.
    if (body.website) return res.json({ success: true, message: OK_MESSAGE });

    const email = String(body.email || '').trim().toLowerCase();
    const handle = String(body.handle || '').trim().slice(0, 100) || null;
    const source = String(body.source || 'site').trim().toLowerCase().replace(/[^a-z0-9_.-]/g, '').slice(0, 40) || 'site';
    const heard = HEARD.has(String(body.heard || '')) ? String(body.heard) : null;
    if (email.length > 254 || !EMAIL_RE.test(email)) {
      return res.status(400).json({ success: false, error: 'Enter a valid email address.' });
    }

    const ip = clientIp(req);
    const t = now();
    const recent = (hits.get(ip) || []).filter((x) => t - x < WINDOW_MS);
    if (recent.length >= MAX_PER_WINDOW) {
      return res.status(429).json({ success: false, error: 'Too many signups from here. Try again in an hour.' });
    }
    recent.push(t);
    hits.set(ip, recent);
    if (hits.size > 10000) hits.clear();

    try {
      await pool.query(
        'INSERT INTO waitlist (email, handle, source, heard) VALUES ($1, $2, $3, $4) ON CONFLICT (email) DO NOTHING',
        [email, handle, source, heard]);
      return res.json({ success: true, message: OK_MESSAGE });
    } catch (e) {
      console.error('waitlist insert failed:', e.message);
      return res.status(503).json({ success: false,
        error: 'The waitlist is not available right now. Email support@proptradebot.com and we will add you.' });
    }
  });
}

module.exports = { mountWaitlist, EMAIL_RE, HEARD };
