// Page-view counting for the daily metrics, no cookies and no script.
// Counted on the server as each HTML page is served (Cloudflare passes HTML
// through uncached, cf-cache-status DYNAMIC, so every view reaches here).
// Stores the time, the page, the referring site's host ('(self)' when it's one
// of our own pages, null when there was none), the link's utm_source, and two bot signals. No IP, no user
// agent string, nothing personal. Fire-and-forget: the page never waits on
// the database, so a DB problem can't break the site.
//
// "Human" = UA doesn't look like a bot AND the browser sent
// Sec-Fetch-Dest: document (every current browser does on a page load; most
// crawlers and scripts don't). A "visit" = a human view that arrived from
// outside the site, so clicking between our own pages isn't counted twice.

const fs = require('fs');
const path = require('path');

const BOT_RE = /bot|crawl|spider|curl|wget|python|preview|monitor|headless|scan|fetch|http-client|axios|node|go-http|java|okhttp|facebookexternalhit|slurp/i;
const OWN_HOSTS = new Set(['proptradebot.com', 'www.proptradebot.com']);

function pagesIn(dir) {
  return new Set(fs.readdirSync(dir).filter((f) => f.endsWith('.html')).map((f) => f.slice(0, -5)));
}

// '/' -> 'index', '/support' and '/support.html' -> 'support', anything else -> null
function pageFor(urlPath, pages) {
  let p = urlPath.replace(/\/+$/, '').replace(/^\/+/, '') || 'index';
  if (p.endsWith('.html')) p = p.slice(0, -5);
  return pages.has(p) ? p : null;
}

function mountPageviews(app, pool, { publicDir = path.join(__dirname, 'public') } = {}) {
  const pages = pagesIn(publicDir);
  pool.query(`CREATE TABLE IF NOT EXISTS page_views (
      id serial PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
      page text NOT NULL, referrer_host text, utm_source text,
      is_bot boolean NOT NULL DEFAULT false, is_doc boolean NOT NULL DEFAULT false)`)
    .catch((e) => console.error('page_views table:', e.message));

  app.use((req, res, next) => {
    if (req.method !== 'GET') return next();
    const page = pageFor(req.path, pages);
    if (!page) return next();
    let host = null;
    try { host = new URL(req.get('referer') || '').host || null; } catch (e) { host = null; }
    // Clicks between our own pages store '(self)' so they can be told apart from direct arrivals (null).
    if (host && OWN_HOSTS.has(host.toLowerCase())) host = '(self)';
    const utm = String(req.query.utm_source || '').toLowerCase().replace(/[^a-z0-9_.-]/g, '').slice(0, 40) || null;
    const isBot = BOT_RE.test(req.get('user-agent') || '');
    const isDoc = req.get('sec-fetch-dest') === 'document';
    pool.query('INSERT INTO page_views (page, referrer_host, utm_source, is_bot, is_doc) VALUES ($1, $2, $3, $4, $5)',
               [page, host, utm, isBot, isDoc])
      .catch((e) => console.error('page_views insert:', e.message));
    next();
  });
}

module.exports = { mountPageviews, pageFor, BOT_RE };
