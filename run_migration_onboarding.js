// Applies migrate_onboarding.sql (ops #3, #13). Idempotent.
//   node run_migration_onboarding.js           # show target, apply, verify
// Uses the same connection as run_migration.js. Prints the database HOST only,
// never the connection string.
const { Pool } = require('pg');
const fs = require('fs');
require('dotenv').config();

const cs = process.env.NEON_CONNECTION_STRING || process.env.DATABASE_URL;
if (!cs) { console.error('❌ NEON_CONNECTION_STRING not set in .env'); process.exit(1); }
let host = '?';
try { host = new URL(cs).hostname; } catch (e) {}
const pool = new Pool({ connectionString: cs, ssl: { rejectUnauthorized: false } });

(async () => {
  try {
    // Prove this is the live database before changing it: it must already
    // have the July TradingView migration and real users.
    const pre = await pool.query(`SELECT
        (SELECT count(*)::int FROM users) AS users,
        (SELECT count(*)::int FROM users WHERE subscription_status IN ('active','trialing')) AS active,
        to_regclass('public.pending_alerts') IS NOT NULL AS has_alerts,
        (SELECT max(last_bot_heartbeat) FROM users) AS last_heartbeat`);
    const p = pre.rows[0];
    console.log(`Target DB host: ${host}`);
    console.log(`  users=${p.users} active/trialing=${p.active} pending_alerts table=${p.has_alerts} last bot heartbeat=${p.last_heartbeat ? new Date(p.last_heartbeat).toISOString() : 'never'}`);
    if (!p.has_alerts) { console.error('❌ No pending_alerts table: this is not the production database. Nothing changed.'); process.exit(1); }

    console.log('Applying migrate_onboarding.sql ...');
    await pool.query(fs.readFileSync(__dirname + '/migrate_onboarding.sql', 'utf8'));

    const cols = await pool.query(`SELECT table_name||'.'||column_name AS c FROM information_schema.columns
      WHERE (table_name='users' AND column_name IN ('last_alert_poll_at','last_tv_test_at','last_tv_test_payload'))
         OR (table_name='pending_alerts' AND column_name='outcome') ORDER BY 1`);
    const tabs = await pool.query(`SELECT to_regclass('public.onboarding_messages') IS NOT NULL AS m,
      to_regclass('public.onboarding_usage') IS NOT NULL AS u, to_regclass('public.support_tickets') IS NOT NULL AS t`);
    const t = tabs.rows[0];
    console.log('  columns:', cols.rows.map((r) => r.c).join(', '));
    console.log(`  tables: onboarding_messages=${t.m} onboarding_usage=${t.u} support_tickets=${t.t}`);
    const ok = cols.rows.length === 4 && t.m && t.u && t.t;
    console.log(ok ? '✅ MIGRATION OK' : '❌ MIGRATION INCOMPLETE');
    await pool.end();
    process.exit(ok ? 0 : 1);
  } catch (e) {
    console.error('❌ Migration failed:', e.message, '(if "lock timeout": the users table was busy; just run it again)');
    process.exit(1);
  }
})();
