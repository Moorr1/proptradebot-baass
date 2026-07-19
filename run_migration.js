// Node migration runner — uses the SAME connection server.js uses (Neon + SSL).
// Robust vs psql (no password prompt hangs). Idempotent.
const { Pool } = require('pg');
const fs = require('fs');
require('dotenv').config();

const cs = process.env.NEON_CONNECTION_STRING || process.env.DATABASE_URL;
if (!cs) { console.error('❌ NEON_CONNECTION_STRING not set'); process.exit(1); }
const pool = new Pool({ connectionString: cs, ssl: { rejectUnauthorized: false } });

(async () => {
  try {
    const sql = fs.readFileSync(__dirname + '/migrate_tv_webhook.sql', 'utf8');
    console.log('Applying migrate_tv_webhook.sql ...');
    await pool.query(sql);
    const cols = await pool.query(
      "SELECT column_name FROM information_schema.columns WHERE table_name='users' AND column_name IN ('tv_token','tv_passphrase') ORDER BY column_name");
    const tbl = await pool.query("SELECT to_regclass('public.pending_alerts') AS t");
    console.log('✅ users columns added:', cols.rows.map(r => r.column_name).join(', ') || '(none!)');
    console.log('✅ pending_alerts table:', tbl.rows[0].t || '(missing!)');
    const ok = cols.rows.length === 2 && tbl.rows[0].t;
    await pool.end();
    process.exit(ok ? 0 : 1);
  } catch (e) { console.error('❌ Migration failed:', e.message); process.exit(1); }
})();
