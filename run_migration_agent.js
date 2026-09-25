// Applies migrate_agent_step1.sql (Web + Agent step 1). Idempotent.
//   node run_migration_agent.js
// Prints the database HOST only, never the connection string.
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
    const pre = (await pool.query(`SELECT (SELECT count(*)::int FROM users) AS users,
        to_regclass('public.onboarding_messages') IS NOT NULL AS has_onboarding`)).rows[0];
    console.log(`Target DB host: ${host}  users=${pre.users}  onboarding tables=${pre.has_onboarding}`);
    if (!pre.has_onboarding) { console.error('❌ Onboarding tables missing: not the production DB, or the Sep 24 migration never ran. Nothing changed.'); process.exit(1); }
    console.log('Applying migrate_agent_step1.sql ...');
    await pool.query(fs.readFileSync(__dirname + '/migrate_agent_step1.sql', 'utf8'));
    const t = (await pool.query(`SELECT to_regclass('public.agent_status') IS NOT NULL AS s, to_regclass('public.agent_events') IS NOT NULL AS e`)).rows[0];
    console.log(`  tables: agent_status=${t.s} agent_events=${t.e}`);
    console.log(t.s && t.e ? '✅ MIGRATION OK' : '❌ MIGRATION INCOMPLETE');
    await pool.end();
    process.exit(t.s && t.e ? 0 : 1);
  } catch (e) {
    console.error('❌ Migration failed:', e.message, '(if "lock timeout": run it again)');
    process.exit(1);
  }
})();
