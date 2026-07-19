// Create/refresh a throwaway test user for live-testing the TV relay. Prints creds.
const { Pool } = require('pg');
const crypto = require('crypto');
require('dotenv').config();
const cs = process.env.NEON_CONNECTION_STRING || process.env.DATABASE_URL;
const pool = new Pool({ connectionString: cs, ssl: { rejectUnauthorized: false } });
(async () => {
  try {
    const email = 'tvtest@proptradebot.local';
    const apiKey = 'ptb_' + crypto.randomBytes(16).toString('hex');
    const tvToken = crypto.randomBytes(24).toString('hex');
    const tvPass = 'ptv_' + crypto.randomBytes(8).toString('hex');
    const clerkId = 'test_' + crypto.randomBytes(6).toString('hex');
    await pool.query(
      `INSERT INTO users (clerk_id, email, name, plan_tier, subscription_status, billing_source, api_key, tv_token, tv_passphrase)
       VALUES ($1,$2,$2,'pro','trialing','stripe',$3,$4,$5)
       ON CONFLICT (email) DO UPDATE SET subscription_status='trialing', api_key=EXCLUDED.api_key, tv_token=EXCLUDED.tv_token, tv_passphrase=EXCLUDED.tv_passphrase`,
      [clerkId, email, apiKey, tvToken, tvPass]);
    console.log(`TEST_API_KEY=${apiKey}`);
    console.log(`TEST_TV_TOKEN=${tvToken}`);
    console.log(`TEST_TV_PASS=${tvPass}`);
    await pool.end(); process.exit(0);
  } catch (e) { console.error('setup fail:', e.message); process.exit(1); }
})();
