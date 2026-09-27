// Applies migrate_waitlist.sql (ops #4). Idempotent.
//   node run_migration_waitlist.js           # show target, apply, verify, print signup count
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
    // Prove this is the live database before changing it.
    const pre = await pool.query(`SELECT (SELECT count(*)::int FROM users) AS users,
        to_regclass('public.pending_alerts') IS NOT NULL AS has_alerts`);
    const p = pre.rows[0];
    console.log(`Target DB host: ${host}`);
    console.log(`  users=${p.users} pending_alerts table=${p.has_alerts}`);
    if (!p.has_alerts) { console.error('❌ No pending_alerts table: this is not the production database. Nothing changed.'); process.exit(1); }
    console.log('Applying migrate_waitlist.sql ...');
    await pool.query(fs.readFileSync(__dirname + '/migrate_waitlist.sql', 'utf8'));
    const t = await pool.query(`SELECT to_regclass('public.waitlist') IS NOT NULL AS ok,
        (SELECT count(*)::int FROM waitlist) AS signups`);
    const ok = t.rows[0].ok;
    console.log(`  waitlist table=${ok} signups=${t.rows[0].signups}`);
    console.log(ok ? '✅ MIGRATION OK' : '❌ MIGRATION INCOMPLETE');
    await pool.end();
    process.exit(ok ? 0 : 1);
  } catch (e) {
    console.error('❌ Migration failed:', e.message);
    process.exit(1);
  }
})();
