// Applies migrate_agent_step2.sql (Web + Agent step 2). Idempotent.
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
        to_regclass('public.agent_status') IS NOT NULL AS has_onboarding`)).rows[0];
    console.log(`Target DB host: ${host}  users=${pre.users}  step-1 tables=${pre.has_onboarding}`);
    if (!pre.has_onboarding) { console.error('❌ Step-1 tables missing: run the step-1 migration first. Nothing changed.'); process.exit(1); }
    console.log('Applying migrate_agent_step2.sql ...');
    await pool.query(fs.readFileSync(__dirname + '/migrate_agent_step2.sql', 'utf8'));
    const t = (await pool.query(`SELECT to_regclass('public.settings_versions') IS NOT NULL AS s, true AS e`)).rows[0];
    console.log(`  tables: settings_versions=${t.s}`);
    console.log(t.s && t.e ? '✅ MIGRATION OK' : '❌ MIGRATION INCOMPLETE');
    await pool.end();
    process.exit(t.s && t.e ? 0 : 1);
  } catch (e) {
    console.error('❌ Migration failed:', e.message, '(if "lock timeout": run it again)');
    process.exit(1);
  }
})();
