require('dotenv').config();
const { Pool } = require('pg');

const pool = new Pool({
  connectionString: process.env.NEON_CONNECTION_STRING,
  ssl: { rejectUnauthorized: false }
});

async function fix() {
  try {
    await pool.query(`
      ALTER TABLE users
      ADD COLUMN IF NOT EXISTS api_key text UNIQUE,
      ADD COLUMN IF NOT EXISTS api_key_created_at timestamptz;
    `);
    console.log('✅ api_key column added');

    const res = await pool.query(`SELECT column_name FROM information_schema.columns WHERE table_name = 'users' ORDER BY ordinal_position`);
    console.log('Users table columns:', res.rows.map(r => r.column_name));

    await pool.end();
  } catch (e) {
    console.error('❌ Error:', e.message);
    process.exit(1);
  }
}

fix();
