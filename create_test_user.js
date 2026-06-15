require('dotenv').config();
const { Pool } = require('pg');
const crypto = require('crypto');

const pool = new Pool({
  connectionString: process.env.NEON_CONNECTION_STRING,
  ssl: { rejectUnauthorized: false }
});

async function create() {
  const email = process.argv[2] || 'test@proptradebot.com';
  const apiKey = 'ptb_' + crypto.randomBytes(32).toString('hex');
  const clerkId = 'test_' + crypto.randomBytes(16).toString('hex');

  try {
    const result = await pool.query(`
      INSERT INTO users (clerk_id, email, name, plan_tier, subscription_status, api_key, api_key_created_at)
      VALUES ($1, $2, $3, $4, $5, $6, NOW())
      ON CONFLICT (email) DO UPDATE SET
        api_key = EXCLUDED.api_key,
        api_key_created_at = NOW(),
        subscription_status = 'trialing',
        updated_at = NOW()
      RETURNING id, email, api_key, subscription_status;
    `, [clerkId, email, 'Test User', 'pro', 'trialing', apiKey]);

    const user = result.rows[0];
    console.log('\n✅ Test user created/updated');
    console.log('  ID:', user.id);
    console.log('  Email:', user.email);
    console.log('  API Key:', user.api_key);
    console.log('  Status:', user.subscription_status);
    console.log('\nSave this API key for setup wizard testing.\n');

    await pool.end();
  } catch (e) {
    console.error('❌ Error:', e.message);
    process.exit(1);
  }
}

create();
