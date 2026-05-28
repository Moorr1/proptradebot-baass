require('dotenv').config();
const express = require('express');
const cors = require('cors');
const crypto = require('crypto');
const stripe = require('stripe')(process.env.STRIPE_SECRET_KEY);
const { ClerkExpressRequireAuth, clerkClient } = require('@clerk/clerk-sdk-node');
const { Pool } = require('pg');

const app = express();
const PORT = process.env.PORT || 3001;

// Database connection
const pool = new Pool({
  connectionString: process.env.NEON_CONNECTION_STRING,
  ssl: { rejectUnauthorized: false }
});

// Test database connection
pool.query('SELECT NOW()', (err, res) => {
  if (err) {
    console.error('❌ Database connection failed:', err);
  } else {
    console.log('✅ Database connected:', res.rows[0].now);
  }
});

// Middleware
app.use(cors());
app.use(express.json());
app.use(express.static('public'));

// Raw body for Stripe webhooks
app.use('/webhook', express.raw({ type: 'application/json' }));

// Helper: get email from Clerk (session claims first, then API fallback)
async function getClerkEmail(auth) {
  // Try session claims first
  let email = auth.sessionClaims?.email 
    || auth.sessionClaims?.email_address
    || auth.sessionClaims?.primary_email_address;
  if (email) return email;
  
  // Fallback: fetch from Clerk API
  try {
    const user = await clerkClient.users.getUser(auth.userId);
    email = user.emailAddresses?.find(e => e.id === user.primaryEmailAddressId)?.emailAddress
      || user.emailAddresses?.[0]?.emailAddress;
    return email || null;
  } catch (e) {
    console.error('Failed to fetch Clerk user email:', e.message);
    return null;
  }
}

// Health check
app.get('/health', (req, res) => {
  res.json({ status: 'ok', timestamp: new Date().toISOString() });
});

// Stripe diagnostics (temporary)
app.get('/api/stripe-diag', async (req, res) => {
  try {
    const keyPrefix = process.env.STRIPE_SECRET_KEY?.substring(0, 25) + '...';
    const acct = await stripe.accounts.retrieve();
    const prices = await stripe.prices.list({ active: true, limit: 10 });
    res.json({
      keyPrefix,
      accountId: acct.id,
      accountName: acct.settings?.dashboard?.display_name,
      prices: prices.data.map(p => ({ id: p.id, amount: p.unit_amount/100 }))
    });
  } catch (e) {
    res.json({ error: e.message });
  }
});

// Get Stripe config (publishable key for frontend)
app.get('/api/config', (req, res) => {
  res.json({
    publishableKey: process.env.STRIPE_PUBLISHABLE_KEY,
  });
});

// Get products and prices (public)
app.get('/api/products', async (req, res) => {
  try {
    const products = await stripe.products.list({
      active: true,
      expand: ['data.default_price']
    });
    
    // Get prices for each product
    const productsWithPrices = await Promise.all(
      products.data
        .filter(p => p.name.startsWith('PropTradeBot'))
        .map(async (product) => {
          const prices = await stripe.prices.list({
            product: product.id,
            active: true
          });
          return {
            id: product.id,
            name: product.name,
            description: product.description,
            prices: prices.data.map(price => ({
              id: price.id,
              amount: price.unit_amount,
              currency: price.currency,
              interval: price.recurring?.interval
            }))
          };
        })
    );
    
    res.json({ success: true, products: productsWithPrices });
  } catch (error) {
    res.status(400).json({ success: false, error: error.message });
  }
});

// Protected routes - require Clerk auth
app.use('/api/user', ClerkExpressRequireAuth());
app.use('/api/accounts', ClerkExpressRequireAuth());

// Bot gateway routes use API key auth (below), not Clerk
// app.use('/api/bot', ClerkExpressRequireAuth());  // ← bot routes use requireApiKey instead

// Get or create user profile
app.get('/api/user/profile', async (req, res) => {
  try {
    const clerkId = req.auth.userId;
    const email = await getClerkEmail(req.auth);
    
    // Check if user exists
    let result = await pool.query(
      'SELECT * FROM users WHERE clerk_id = $1',
      [clerkId]
    );
    
    if (result.rows.length === 0) {
      // Create user
      result = await pool.query(
        `INSERT INTO users (clerk_id, email, name, plan_tier, subscription_status)
         VALUES ($1, $2, $3, 'none', 'inactive')
         RETURNING *`,
        [clerkId, email, email]
      );
    }
    
    res.json({ success: true, user: result.rows[0] });
  } catch (error) {
    res.status(400).json({ success: false, error: error.message });
  }
});

// Get user's trading accounts
app.get('/api/accounts', async (req, res) => {
  try {
    const clerkId = req.auth.userId;
    
    // Get user
    const userResult = await pool.query(
      'SELECT id FROM users WHERE clerk_id = $1',
      [clerkId]
    );
    
    if (userResult.rows.length === 0) {
      return res.status(404).json({ success: false, error: 'User not found' });
    }
    
    const userId = userResult.rows[0].id;
    
    // Get accounts
    const accountsResult = await pool.query(
      'SELECT * FROM accounts WHERE user_id = $1 ORDER BY created_at DESC',
      [userId]
    );
    
    res.json({ success: true, accounts: accountsResult.rows });
  } catch (error) {
    res.status(400).json({ success: false, error: error.message });
  }
});

// Add trading account
app.post('/api/accounts', async (req, res) => {
  try {
    const clerkId = req.auth.userId;
    const { prop_firm, account_number, platform, starting_balance } = req.body;
    
    // Get user
    const userResult = await pool.query(
      'SELECT id FROM users WHERE clerk_id = $1',
      [clerkId]
    );
    
    if (userResult.rows.length === 0) {
      return res.status(404).json({ success: false, error: 'User not found' });
    }
    
    const userId = userResult.rows[0].id;
    
    // Create account
    const result = await pool.query(
      `INSERT INTO accounts (user_id, prop_firm, account_number, platform, starting_balance, current_balance, status)
       VALUES ($1, $2, $3, $4, $5, $5, 'active')
       RETURNING *`,
      [userId, prop_firm, account_number, platform, starting_balance]
    );
    
    res.json({ success: true, account: result.rows[0] });
  } catch (error) {
    res.status(400).json({ success: false, error: error.message });
  }
});

// Get bot configuration
app.get('/api/bot/config', async (req, res) => {
  try {
    const clerkId = req.auth.userId;
    
    // Get user
    const userResult = await pool.query(
      'SELECT id FROM users WHERE clerk_id = $1',
      [clerkId]
    );
    
    if (userResult.rows.length === 0) {
      return res.status(404).json({ success: false, error: 'User not found' });
    }
    
    const userId = userResult.rows[0].id;
    
    // Get or create bot config
    let result = await pool.query(
      'SELECT * FROM bot_configs WHERE user_id = $1',
      [userId]
    );
    
    if (result.rows.length === 0) {
      result = await pool.query(
        `INSERT INTO bot_configs (user_id, strategy, contract_count, auto_trading)
         VALUES ($1, 'auto', 1, false)
         RETURNING *`,
        [userId]
      );
    }
    
    res.json({ success: true, config: result.rows[0] });
  } catch (error) {
    res.status(400).json({ success: false, error: error.message });
  }
});

// Update bot configuration
app.put('/api/bot/config', async (req, res) => {
  try {
    const clerkId = req.auth.userId;
    const { strategy, contract_count, auto_trading, stop_loss_ticks, risk_per_trade } = req.body;
    
    // Get user
    const userResult = await pool.query(
      'SELECT id FROM users WHERE clerk_id = $1',
      [clerkId]
    );
    
    if (userResult.rows.length === 0) {
      return res.status(404).json({ success: false, error: 'User not found' });
    }
    
    const userId = userResult.rows[0].id;
    
    // Update config
    const result = await pool.query(
      `UPDATE bot_configs 
       SET strategy = COALESCE($2, strategy),
           contract_count = COALESCE($3, contract_count),
           auto_trading = COALESCE($4, auto_trading),
           stop_loss_ticks = COALESCE($5, stop_loss_ticks),
           risk_per_trade = COALESCE($6, risk_per_trade)
       WHERE user_id = $1
       RETURNING *`,
      [userId, strategy, contract_count, auto_trading, stop_loss_ticks, risk_per_trade]
    );
    
    res.json({ success: true, config: result.rows[0] });
  } catch (error) {
    res.status(400).json({ success: false, error: error.message });
  }
});

// Create checkout session for subscription
app.post('/api/checkout', ClerkExpressRequireAuth(), async (req, res) => {
  try {
    const { priceId } = req.body;
    console.log('🛒 Checkout request — priceId:', priceId);
    const clerkId = req.auth.userId;
    const email = await getClerkEmail(req.auth);
    console.log('🛒 Checkout user — clerkId:', clerkId, 'email:', email);
    
    if (!email) {
      return res.status(400).json({ success: false, error: 'Could not retrieve email from auth provider' });
    }
    
    // Get or create user
    let userResult = await pool.query(
      'SELECT * FROM users WHERE clerk_id = $1',
      [clerkId]
    );
    
    if (userResult.rows.length === 0) {
      userResult = await pool.query(
        `INSERT INTO users (clerk_id, email, name)
         VALUES ($1, $2, $3)
         RETURNING *`,
        [clerkId, email, email]
      );
    }
    
    const user = userResult.rows[0];
    
    // Get or create Stripe customer
    let customerId = user.stripe_customer_id;
    
    if (!customerId) {
      const customer = await stripe.customers.create({
        email,
        metadata: { clerk_id: clerkId }
      });
      customerId = customer.id;
      
      // Update user with customer ID
      await pool.query(
        'UPDATE users SET stripe_customer_id = $1 WHERE id = $2',
        [customerId, user.id]
      );
    }
    
    // Create checkout session
    const session = await stripe.checkout.sessions.create({
      customer: customerId,
      payment_method_types: ['card'],
      line_items: [
        {
          price: priceId,
          quantity: 1,
        },
      ],
      mode: 'subscription',
      success_url: `${process.env.FRONTEND_URL || 'http://localhost:3001'}/success?session_id={CHECKOUT_SESSION_ID}`,
      cancel_url: `${process.env.FRONTEND_URL || 'http://localhost:3001'}/cancel`,
      metadata: {
        clerk_id: clerkId,
        user_id: user.id
      }
    });

    res.json({ success: true, sessionId: session.id, url: session.url });
  } catch (error) {
    console.error('🛒 Checkout FAILED — priceId:', req.body?.priceId, 'error:', error.message);
    res.status(400).json({ success: false, error: error.message, receivedPriceId: req.body?.priceId });
  }
});

// Checkout success page
app.get('/success', async (req, res) => {
  const sessionId = req.query.session_id;
  if (!sessionId) {
    return res.redirect('/dashboard');
  }
  try {
    const session = await stripe.checkout.sessions.retrieve(sessionId);
    if (session.payment_status === 'paid' || session.status === 'complete') {
      res.send(`
        <!DOCTYPE html>
        <html>
        <head>
          <meta charset="UTF-8">
          <meta name="viewport" content="width=device-width, initial-scale=1.0">
          <title>Payment Successful — PropTradeBot</title>
          <style>
            body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #0a0e1a; color: #e2e8f0; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }
            .card { background: #111827; border: 1px solid #1e293b; border-radius: 16px; padding: 48px; text-align: center; max-width: 420px; }
            .icon { font-size: 64px; margin-bottom: 16px; }
            h1 { margin: 0 0 8px; font-size: 24px; }
            p { color: #94a3b8; margin: 0 0 24px; }
            .btn { background: #3b82f6; color: white; padding: 12px 24px; border-radius: 8px; text-decoration: none; display: inline-block; font-weight: 500; }
            .btn:hover { background: #2563eb; }
          </style>
        </head>
        <body>
          <div class="card">
            <div class="icon">✅</div>
            <h1>Payment Successful!</h1>
            <p>Your subscription is active. Welcome to PropTradeBot!</p>
            <a href="/dashboard" class="btn">Go to Dashboard</a>
          </div>
        </body>
        </html>
      `);
    } else {
      res.redirect('/dashboard');
    }
  } catch (e) {
    console.error('Success page error:', e.message);
    res.redirect('/dashboard');
  }
});

// Checkout cancel page
app.get('/cancel', (req, res) => {
  res.send(`
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>Cancelled — PropTradeBot</title>
      <style>
        body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #0a0e1a; color: #e2e8f0; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }
        .card { background: #111827; border: 1px solid #1e293b; border-radius: 16px; padding: 48px; text-align: center; max-width: 420px; }
        .icon { font-size: 64px; margin-bottom: 16px; }
        h1 { margin: 0 0 8px; font-size: 24px; }
        p { color: #94a3b8; margin: 0 0 24px; }
        .btn { background: #3b82f6; color: white; padding: 12px 24px; border-radius: 8px; text-decoration: none; display: inline-block; font-weight: 500; }
        .btn:hover { background: #2563eb; }
      </style>
    </head>
    <body>
      <div class="card">
        <div class="icon">❌</div>
        <h1>Payment Cancelled</h1>
        <p>No worries — you can subscribe anytime from your dashboard.</p>
        <a href="/dashboard" class="btn">Go to Dashboard</a>
      </div>
    </body>
    </html>
  `);
});

// Stripe webhook
app.post('/webhook', async (req, res) => {
  const sig = req.headers['stripe-signature'];
  const endpointSecret = process.env.STRIPE_WEBHOOK_SECRET;

  let event;

  try {
    event = stripe.webhooks.constructEvent(req.body, sig, endpointSecret);
  } catch (err) {
    console.log(`❌ Webhook signature verification failed:`, err.message);
    return res.status(400).send(`Webhook Error: ${err.message}`);
  }

  // Log event
  await pool.query(
    'INSERT INTO stripe_events (stripe_event_id, event_type, customer_id, subscription_id, payload) VALUES ($1, $2, $3, $4, $5)',
    [event.id, event.type, event.data.object.customer, event.data.object.id, event.data.object]
  );

  // Handle events
  switch (event.type) {
    case 'checkout.session.completed':
      const session = event.data.object;
      console.log('✅ Checkout completed:', session.id);
      
      // Update user subscription status
      if (session.metadata?.user_id) {
        await pool.query(
          `UPDATE users 
           SET subscription_status = 'active',
               stripe_subscription_id = $1
           WHERE id = $2`,
          [session.subscription, session.metadata.user_id]
        );
      }
      break;
    
    case 'customer.subscription.created':
    case 'customer.subscription.updated':
      const subscription = event.data.object;
      console.log('💰 Subscription updated:', subscription.id);
      
      // Determine plan tier from price
      const priceId = subscription.items.data[0].price.id;
      const prices = await stripe.prices.retrieve(priceId, { expand: ['product'] });
      const productName = prices.product.name;
      
      let planTier = 'none';
      if (productName.includes('Basic')) planTier = 'basic';
      else if (productName.includes('Pro')) planTier = 'pro';
      else if (productName.includes('Enterprise')) planTier = 'enterprise';
      
      await pool.query(
        `UPDATE users 
         SET plan_tier = $1,
             subscription_status = $2,
             stripe_subscription_id = $3
         WHERE stripe_customer_id = $4`,
        [planTier, subscription.status, subscription.id, subscription.customer]
      );
      break;
    
    case 'invoice.paid':
      console.log('💰 Invoice paid:', event.data.object.id);
      // Extend subscription, ensure active
      break;
    
    case 'invoice.payment_failed':
      console.log('❌ Payment failed:', event.data.object.id);
      const failedInvoice = event.data.object;
      
      await pool.query(
        `UPDATE users 
         SET subscription_status = 'past_due'
         WHERE stripe_customer_id = $1`,
        [failedInvoice.customer]
      );
      break;
    
    case 'customer.subscription.deleted':
      console.log('🚫 Subscription cancelled:', event.data.object.id);
      const cancelledSub = event.data.object;
      
      await pool.query(
        `UPDATE users 
         SET subscription_status = 'cancelled',
             plan_tier = 'none'
         WHERE stripe_customer_id = $1`,
        [cancelledSub.customer]
      );
      break;
    
    default:
      console.log(`Unhandled event type: ${event.type}`);
  }

  res.json({ received: true });
});

// =============================================================================
// BOT GATEWAY — API Key Auth (for local bot connections)
// =============================================================================

async function requireApiKey(req, res, next) {
  const apiKey = req.headers['x-api-key'] || req.query.apiKey;

  if (!apiKey) {
    return res.status(401).json({
      success: false,
      error: 'API key required. Get your key at https://proptradebot.com/dashboard',
      code: 'NO_API_KEY'
    });
  }

  try {
    const result = await pool.query(
      `SELECT id, clerk_id, email, plan_tier, subscription_status,
              stripe_customer_id, stripe_subscription_id, name,
              created_at as user_created_at
       FROM users WHERE api_key = $1`,
      [apiKey]
    );

    if (result.rows.length === 0) {
      return res.status(401).json({
        success: false,
        error: 'Invalid API key',
        code: 'INVALID_API_KEY'
      });
    }

    const user = result.rows[0];
    const activeStatuses = ['active', 'trialing'];
    if (!activeStatuses.includes(user.subscription_status)) {
      return res.status(403).json({
        success: false,
        error: `Subscription ${user.subscription_status}. Please renew at https://proptradebot.com/dashboard`,
        code: 'SUBSCRIPTION_INACTIVE',
        subscription_status: user.subscription_status,
        plan_tier: user.plan_tier
      });
    }

    req.botUser = user;
    next();
  } catch (error) {
    console.error('API key validation error:', error);
    res.status(500).json({ success: false, error: 'Internal error' });
  }
}

// POST /api/bot/auth — Validate API key, return full config
app.post('/api/bot/auth', requireApiKey, async (req, res) => {
  try {
    const user = req.botUser;

    const configResult = await pool.query(
      'SELECT * FROM bot_configs WHERE user_id = $1',
      [user.id]
    );

    const accountsResult = await pool.query(
      'SELECT * FROM accounts WHERE user_id = $1 AND status = $2',
      [user.id, 'active']
    );

    let subscription = null;
    if (user.stripe_subscription_id) {
      try {
        subscription = await stripe.subscriptions.retrieve(user.stripe_subscription_id);
      } catch (e) {
        console.log('Could not fetch subscription from Stripe:', e.message);
      }
    }

    res.json({
      success: true,
      user: {
        id: user.id,
        email: user.email,
        name: user.name,
        plan_tier: user.plan_tier,
        subscription_status: user.subscription_status,
        created_at: user.user_created_at
      },
      config: configResult.rows[0] || null,
      accounts: accountsResult.rows,
      subscription: subscription ? {
        id: subscription.id,
        status: subscription.status,
        current_period_end: subscription.current_period_end,
        cancel_at_period_end: subscription.cancel_at_period_end
      } : null
    });
  } catch (error) {
    res.status(500).json({ success: false, error: error.message });
  }
});

// POST /api/bot/heartbeat — Bot pings every 5 minutes
app.post('/api/bot/heartbeat', requireApiKey, async (req, res) => {
  try {
    const user = req.botUser;
    const { version, uptime_seconds, alerts_processed, positions_active } = req.body;

    await pool.query(
      'UPDATE users SET last_bot_heartbeat = NOW() WHERE id = $1',
      [user.id]
    );

    await pool.query(
      `INSERT INTO bot_heartbeats (user_id, version, uptime_seconds, alerts_processed, positions_active)
       VALUES ($1, $2, $3, $4, $5)`,
      [user.id, version || 'unknown', uptime_seconds || 0, alerts_processed || 0, positions_active || 0]
    );

    const configResult = await pool.query(
      'SELECT * FROM bot_configs WHERE user_id = $1',
      [user.id]
    );

    res.json({
      success: true,
      timestamp: new Date().toISOString(),
      config: configResult.rows[0] || null,
      subscription_status: user.subscription_status
    });
  } catch (error) {
    res.status(500).json({ success: false, error: error.message });
  }
});

// POST /api/bot/trades — Report completed trade
app.post('/api/bot/trades', requireApiKey, async (req, res) => {
  try {
    const user = req.botUser;
    const {
      account_id, signal_id, trade_direction, symbol, contracts,
      entry_price, exit_price, stop_price, target_prices,
      realized_pnl, commission, status, opened_at, closed_at, metadata
    } = req.body;

    const result = await pool.query(
      `INSERT INTO trades (
        user_id, account_id, signal_id, trade_direction, symbol,
        contracts, entry_price, exit_price, stop_price, target_prices,
        realized_pnl, commission, status, opened_at, closed_at, metadata
      ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
      RETURNING *`,
      [
        user.id, account_id, signal_id, trade_direction, symbol,
        contracts, entry_price, exit_price, stop_price,
        target_prices ? JSON.stringify(target_prices) : null,
        realized_pnl, commission || 0, status || 'open',
        opened_at || new Date(), closed_at,
        metadata ? JSON.stringify(metadata) : null
      ]
    );

    res.json({ success: true, trade: result.rows[0] });
  } catch (error) {
    res.status(500).json({ success: false, error: error.message });
  }
});

// GET /api/bot/config — Get full bot config (accounts + strategy)
app.get('/api/bot/config', requireApiKey, async (req, res) => {
  try {
    const user = req.botUser;

    const [configResult, accountsResult] = await Promise.all([
      pool.query('SELECT * FROM bot_configs WHERE user_id = $1', [user.id]),
      pool.query('SELECT * FROM accounts WHERE user_id = $1', [user.id])
    ]);

    res.json({
      success: true,
      config: configResult.rows[0] || null,
      accounts: accountsResult.rows
    });
  } catch (error) {
    res.status(500).json({ success: false, error: error.message });
  }
});

// PUT /api/bot/config — Update bot config (from local bot or dashboard)
app.put('/api/bot/config', requireApiKey, async (req, res) => {
  try {
    const user = req.botUser;
    const updates = req.body;

    const allowedFields = [
      'strategy', 'contract_count', 'stop_loss_ticks', 'target_multipliers',
      'auto_trading', 'risk_per_trade', 'max_daily_trades', 'allowed_symbols'
    ];

    const fields = [];
    const values = [];
    let paramIndex = 1;

    for (const field of allowedFields) {
      if (updates[field] !== undefined) {
        fields.push(`${field} = $${paramIndex}`);
        values.push(updates[field]);
        paramIndex++;
      }
    }

    if (fields.length === 0) {
      return res.status(400).json({ success: false, error: 'No valid fields to update' });
    }

    values.push(user.id);

    const result = await pool.query(
      `UPDATE bot_configs SET ${fields.join(', ')} WHERE user_id = $${paramIndex} RETURNING *`,
      values
    );

    res.json({ success: true, config: result.rows[0] });
  } catch (error) {
    res.status(500).json({ success: false, error: error.message });
  }
});

// =============================================================================
// USER DASHBOARD ENDPOINTS (for web UI)
// =============================================================================

// POST /api/user/api-key — Generate/regenerate API key
app.post('/api/user/api-key', ClerkExpressRequireAuth(), async (req, res) => {
  try {
    const clerkId = req.auth.userId;
    const apiKey = 'ptb_' + crypto.randomBytes(32).toString('hex');

    const result = await pool.query(
      `UPDATE users SET api_key = $1, api_key_created_at = NOW()
       WHERE clerk_id = $2
       RETURNING api_key, api_key_created_at`,
      [apiKey, clerkId]
    );

    if (result.rows.length === 0) {
      return res.status(404).json({ success: false, error: 'User not found' });
    }

    res.json({
      success: true,
      api_key: result.rows[0].api_key,
      created_at: result.rows[0].api_key_created_at
    });
  } catch (error) {
    res.status(500).json({ success: false, error: error.message });
  }
});

// GET /api/user/api-key — Get existing API key (masked)
app.get('/api/user/api-key', ClerkExpressRequireAuth(), async (req, res) => {
  try {
    const clerkId = req.auth.userId;

    const result = await pool.query(
      'SELECT api_key, api_key_created_at FROM users WHERE clerk_id = $1',
      [clerkId]
    );

    if (result.rows.length === 0) {
      return res.status(404).json({ success: false, error: 'User not found' });
    }

    const key = result.rows[0].api_key;
    res.json({
      success: true,
      has_key: !!key,
      api_key: key ? key.substring(0, 8) + '...' + key.substring(key.length - 4) : null,
      created_at: result.rows[0].api_key_created_at
    });
  } catch (error) {
    res.status(500).json({ success: false, error: error.message });
  }
});

// GET /api/user/bot-status — Bot health for dashboard
app.get('/api/user/bot-status', ClerkExpressRequireAuth(), async (req, res) => {
  try {
    const clerkId = req.auth.userId;

    const result = await pool.query(
      `SELECT last_bot_heartbeat, subscription_status, plan_tier
       FROM users WHERE clerk_id = $1`,
      [clerkId]
    );

    if (result.rows.length === 0) {
      return res.status(404).json({ success: false, error: 'User not found' });
    }

    const user = result.rows[0];
    const lastHeartbeat = user.last_bot_heartbeat;
    const isOnline = lastHeartbeat && (new Date() - new Date(lastHeartbeat)) < 10 * 60 * 1000;

    res.json({
      success: true,
      bot_online: isOnline,
      last_heartbeat: lastHeartbeat,
      subscription_status: user.subscription_status,
      plan_tier: user.plan_tier
    });
  } catch (error) {
    res.status(500).json({ success: false, error: error.message });
  }
});

app.listen(PORT, () => {
  console.log(`🚀 Server running on http://localhost:${PORT}`);
  console.log(`📊 Health check: http://localhost:${PORT}/health`);
  console.log(`🔐 Protected routes require Clerk auth`);
  console.log(`🤖 Bot gateway: /api/bot/* (API key auth)`);
});
