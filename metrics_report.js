// READ-ONLY PropTradeBot metrics: waitlist and founding members (ops #8, #35).
//   node metrics_report.js            # markdown report (counts only, no emails)
//   node metrics_report.js --json     # machine-readable
// Founding members are read from whop_events (every Whop webhook payload is
// stored there); the 12-month price lock ends 12 months after they joined.

const { Pool } = require('pg');
require('dotenv').config();

const FOUNDING_PLAN = 'plan_L1WL2pR12CuEP';
const LOCK_WARN_DAYS = 30;

async function report(pool, now = new Date()) {
  const w = (await pool.query(`SELECT
      count(*)::int AS total,
      count(*) FILTER (WHERE created_at > now() - interval '24 hours')::int AS last_24h,
      count(*) FILTER (WHERE created_at > now() - interval '7 days')::int AS last_7d,
      count(*) FILTER (WHERE invited_at IS NOT NULL)::int AS invited
    FROM waitlist`)).rows[0];
  const f = (await pool.query(`
    SELECT payload->>'id' AS membership,
           min(COALESCE((payload->>'joined_at')::timestamptz, created_at)) AS joined
    FROM whop_events
    WHERE event_type IN ('membership.activated', 'membership.went_valid')
      AND payload->'plan'->>'id' = $1
    GROUP BY 1 ORDER BY 2`, [FOUNDING_PLAN])).rows;
  const members = f.map((r, i) => {
    const lockEnds = new Date(r.joined); lockEnds.setUTCMonth(lockEnds.getUTCMonth() + 12);
    const daysLeft = Math.ceil((lockEnds - now) / 86400000);
    return { n: i + 1, joined: r.joined.toISOString().slice(0, 10), lockEnds: lockEnds.toISOString().slice(0, 10),
             daysLeft, due: daysLeft <= LOCK_WARN_DAYS };
  });
  // Signups by where they came from (ops #39): link tag and "How did you hear".
  w.by_source = (await pool.query(`SELECT coalesce(source, 'unknown') AS k, count(*)::int AS n
    FROM waitlist GROUP BY 1 ORDER BY 2 DESC`)).rows;
  w.by_heard = (await pool.query(`SELECT coalesce(heard, 'not given') AS k, count(*)::int AS n
    FROM waitlist GROUP BY 1 ORDER BY 2 DESC`)).rows;
  // App downloads via /downloads/PropTradeBot.dmg (ops #36), humans only. Counted from
  // 2026-10-06; null until the site has created the table.
  let downloads = null;
  try {
    downloads = (await pool.query(`SELECT
        count(*)::int AS total,
        count(*) FILTER (WHERE created_at > now() - interval '24 hours')::int AS last_24h,
        count(*) FILTER (WHERE created_at > now() - interval '7 days')::int AS last_7d
      FROM download_events WHERE NOT is_bot`)).rows[0];
    downloads.by_referrer = (await pool.query(`SELECT coalesce(referrer_host, 'direct') AS k, count(*)::int AS n
      FROM download_events WHERE NOT is_bot AND created_at > now() - interval '7 days' GROUP BY 1 ORDER BY 2 DESC`)).rows;
  } catch (e) {
    downloads = null;
  }
  return { waitlist: w, founding: { taken: members.length, of: 20, members }, downloads };
}

function markdown(r, stamp) {
  const w = r.waitlist, f = r.founding;
  const lines = [
    `[Jarvis] PropTradeBot metrics, ${stamp}`,
    '',
    `**Waitlist:** ${w.total} total · ${w.last_24h} in the last 24h · ${w.last_7d} in the last 7 days · ${w.invited} invited`,
    `**Founding seats:** ${f.taken} of ${f.of} taken`,
  ];
  const fmt = (xs) => (xs || []).map((x) => `${x.k} ${x.n}`).join(' · ');
  const d = r.downloads;
  lines.push(d ? `**App downloads (humans, since 2026-10-06):** ${d.total} total · ${d.last_24h} in the last 24h · ${d.last_7d} in the last 7 days` +
                 (d.by_referrer && d.by_referrer.length ? ` · from (7d): ${fmt(d.by_referrer)}` : '')
               : '**App downloads:** not available yet');
  if (w.total) {
    lines.push(`**By link (utm_source):** ${fmt(w.by_source)}`);
    lines.push(`**How they heard:** ${fmt(w.by_heard)}`);
  }
  for (const m of f.members) {
    lines.push(`- Founding member #${m.n}: joined ${m.joined}, price lock ends ${m.lockEnds}` +
      (m.due ? ` ⚠️ **${m.daysLeft} days left: move to the public price after notice**` : ''));
  }
  return lines.join('\n');
}

module.exports = { report, markdown, FOUNDING_PLAN };

if (require.main === module) {
  const pool = new Pool({ connectionString: process.env.NEON_CONNECTION_STRING || process.env.DATABASE_URL,
                          ssl: { rejectUnauthorized: false } });
  report(pool).then((r) => {
    const stamp = new Date().toLocaleString('en-US', { timeZone: 'America/New_York', dateStyle: 'medium', timeStyle: 'short' }) + ' ET';
    console.log(process.argv.includes('--json') ? JSON.stringify(r) : markdown(r, stamp));
    return pool.end();
  }).catch((e) => { console.error('metrics failed:', e.message); process.exit(1); });
}
