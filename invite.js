// Invite someone from the waitlist to a founding seat (ops #8).
//   node invite.js person@example.com --dry-run   # show what would happen
//   node invite.js person@example.com             # mark invited, print the email to send
// Sends nothing itself: a person sends the email. Refuses emails that aren't
// on the waitlist or were already invited (add --again to re-send).

const { Pool } = require('pg');
require('dotenv').config();

const CHECKOUT = 'https://whop.com/checkout/plan_L1WL2pR12CuEP';

function inviteEmail() {
  return [
    'Subject: Your PropTradeBot founding seat is ready',
    '',
    'Hi,',
    '',
    "Thanks for joining the PropTradeBot waitlist. A founding seat is open for you:",
    '$99/month, locked for your first 12 months of continuous paid service.',
    '',
    `Activate here (this link is just for you, please don't share it): ${CHECKOUT}`,
    '',
    'Your 7-day free trial starts when you activate. Founding seats are limited to 20,',
    'so the offer may close once they are taken.',
    '',
    'After activating: sign in at https://proptradebot.com/dashboard, download the Mac app,',
    'and follow the setup guide. Start on a practice account first.',
    '',
    'Terms: https://proptradebot.com/terms.html (see 7.1-7.5).',
    'Questions: support@proptradebot.com',
  ].join('\n');
}

module.exports = { inviteEmail, CHECKOUT };

if (require.main === module) {
  const email = String(process.argv[2] || '').trim().toLowerCase();
  const dry = process.argv.includes('--dry-run');
  const again = process.argv.includes('--again');
  if (!email || !email.includes('@')) { console.error('usage: node invite.js <email> [--dry-run] [--again]'); process.exit(2); }
  const pool = new Pool({ connectionString: process.env.NEON_CONNECTION_STRING || process.env.DATABASE_URL,
                          ssl: { rejectUnauthorized: false } });
  (async () => {
    const r = await pool.query('SELECT email, created_at, invited_at FROM waitlist WHERE email = $1', [email]);
    if (!r.rows.length) { console.error(`❌ ${email} is not on the waitlist. Nothing changed.`); process.exit(1); }
    const row = r.rows[0];
    if (row.invited_at && !again) {
      console.error(`❌ ${email} was already invited on ${row.invited_at.toISOString().slice(0, 10)}. Use --again to re-send.`);
      process.exit(1);
    }
    if (dry) {
      console.log(`[dry-run] would mark ${email} invited (joined the list ${row.created_at.toISOString().slice(0, 10)})`);
    } else {
      await pool.query('UPDATE waitlist SET invited_at = now() WHERE email = $1', [email]);
      console.log(`✅ ${email} marked invited`);
    }
    console.log('\n----- send this email -----\n' + inviteEmail());
    await pool.end();
  })().catch((e) => { console.error('invite failed:', e.message); process.exit(1); });
}
