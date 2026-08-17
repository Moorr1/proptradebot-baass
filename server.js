require('dotenv').config();
const express = require('express');
const cors = require('cors');
const crypto = require('crypto');
const stripe = require('stripe')(process.env.STRIPE_SECRET_KEY);
const { ClerkExpressRequireAuth, clerkClient } = require('@clerk/clerk-sdk-node');
const { Pool } = require('pg');
const { Webhook } = require('standardwebhooks');

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

// Raw body for Stripe + Whop webhooks — MUST be before express.json()
app.use('/webhook', express.raw({ type: 'application/json' }));
app.use('/whop-webhook', express.raw({ type: 'application/json' }));
app.use('/tv', express.text({ type: '*/*', limit: '16kb' }));  // TradingView webhook raw body

app.use(express.json());
app.use(express.static('public', { extensions: ['html'] }));

// Download redirects — generic URL → versioned file
// The stable download URL. Every page, email and Discord message points here,
// so a release is only actually SHIPPED once this line moves.
//
// v1.6.4 and v1.6.6 were both built, signed, notarized and then left on disk
// while this still said an older version. Nothing compared the two, so from the
// outside everything looked fine and customers quietly kept downloading a build
// with known bugs in it. fbd_preflight.py now fails when the newest local build
// is ahead of what lives in public/downloads.
app.get('/downloads/PropTradeBot.dmg', (req, res) => {
  res.redirect(302, '/downloads/PropTradeBot-v1.6.10-notarized.dmg');
});

// Friendly routes → Clerk handles auth client-side
app.get('/sign-up', (req, res) => res.redirect('/?action=sign-up'));
app.get('/login', (req, res) => res.redirect('/?action=sign-in'));
app.get('/sign-in', (req, res) => res.redirect('/?action=sign-in'));

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

// Protected routes - require Clerk auth (with proper 401 instead of 500)
function clerkAuth(req, res, next) {
  ClerkExpressRequireAuth()(req, res, (err) => {
    if (err) {
      return res.status(401).json({
        success: false,
        error: 'Authentication required. Please sign in at https://proptradebot.com/dashboard',
        code: 'UNAUTHORIZED'
      });
    }
    next();
  });
}
app.use('/api/user', clerkAuth);
app.use('/api/accounts', clerkAuth);

// Bot gateway routes use API key auth (below), not Clerk
// app.use('/api/bot', ClerkExpressRequireAuth());  // ← bot routes use requireApiKey instead

// Get or create user profile
app.get('/api/user/profile', async (req, res) => {
  try {
    const clerkId = req.auth.userId;
    const email = await getClerkEmail(req.auth);
    
    // Check if user exists
    let result = await pool.query(
      'SELECT *, COALESCE(billing_source, \'stripe\') as billing_source FROM users WHERE clerk_id = $1',
      [clerkId]
    );
    
    if (result.rows.length === 0) {
      // Create user
      result = await pool.query(
        `INSERT INTO users (clerk_id, email, name, plan_tier, subscription_status, billing_source)
         VALUES ($1, $2, $3, 'none', 'inactive', 'stripe')
         RETURNING *, COALESCE(billing_source, 'stripe') as billing_source`,
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
app.post('/api/checkout', clerkAuth, async (req, res) => {
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
    
    // Create checkout session with 7-day trial
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
      subscription_data: {
        trial_period_days: 7,
      },
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
      
      // Trial abuse check: look up card fingerprint
      if (session.subscription) {
        try {
          const subscription = await stripe.subscriptions.retrieve(session.subscription, {
            expand: ['default_payment_method']
          });
          
          const pm = subscription.default_payment_method;
          if (pm && pm.card && pm.card.fingerprint) {
            const fingerprint = pm.card.fingerprint;
            console.log('💳 Card fingerprint:', fingerprint);
            
            // Check if this fingerprint was used for a previous trial by a DIFFERENT user
            const existingFingerprint = await pool.query(
              `SELECT u.email, u.created_at 
               FROM trial_fingerprints tf
               JOIN users u ON tf.user_id = u.id
               WHERE tf.fingerprint = $1
                 AND tf.fingerprint_type = 'card'
                 AND tf.created_at > now() - interval '90 days'
                 AND tf.user_id != $2`,
              [fingerprint, session.metadata?.user_id]
            );
            
            if (existingFingerprint.rows.length > 0) {
              console.log('🚫 Trial abuse detected — card fingerprint already used');
              // Cancel the subscription immediately (no charge)
              await stripe.subscriptions.cancel(session.subscription, {
                invoice_now: false,
                prorate: false
              });
              
              // Update user status
              if (session.metadata?.user_id) {
                await pool.query(
                  `UPDATE users 
                   SET subscription_status = 'cancelled',
                       plan_tier = 'none'
                   WHERE id = $1`,
                  [session.metadata.user_id]
                );
              }
              
              // Send email or notification
              console.log('🚫 Cancelled subscription due to trial abuse');
              break;
            }
            
            // Store fingerprint for future checks
            if (session.metadata?.user_id) {
              await pool.query(
                `INSERT INTO trial_fingerprints (fingerprint, fingerprint_type, user_id)
                 VALUES ($1, 'card', $2)
                 ON CONFLICT (fingerprint, fingerprint_type) DO NOTHING`,
                [fingerprint, session.metadata.user_id]
              );
            }
          }
        } catch (err) {
          console.error('Error checking card fingerprint:', err.message);
          // Don't block checkout if fingerprint check fails
        }
      }
      
      // Update user subscription status
      if (session.metadata?.user_id) {
        await pool.query(
          `UPDATE users 
           SET subscription_status = 'trialing',
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
      
      let planTier = 'pro';  // Single plan: Pro only
      
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
              billing_source, whop_membership_id, whop_user_id,
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
    // Only fetch Stripe sub for Stripe-billed users
    if (user.billing_source !== 'whop' && user.stripe_subscription_id) {
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
        billing_source: user.billing_source || 'stripe',
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

// GET /api/bot/summary — Read-only account + P&L summary (drives daily/EOD reports)
// Auth: same x-api-key as other bot routes. Pure SELECTs, no writes.
// Query params:
//   date=YYYY-MM-DD  (optional) the trading day to report, in US/Eastern. Defaults to today ET.
//   days=N           (optional) trailing window size for the period rollup. Default 7, clamped 1..90.
// NOTE on P&L: trades.realized_pnl and trades.commission are stored separately.
//   We report realized_pnl and commission as summed from the DB, and define
//   net_pnl = realized_pnl - commission. If the bot already books realized_pnl net of
//   commission, treat `realized_pnl` as the figure of record and ignore net_pnl.
app.get('/api/bot/summary', requireApiKey, async (req, res) => {
  try {
    const user = req.botUser;

    // Resolve the reporting date in US/Eastern (the market day).
    const etToday = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York' }).format(new Date()); // YYYY-MM-DD
    let date = etToday;
    if (req.query.date) {
      if (!/^\d{4}-\d{2}-\d{2}$/.test(req.query.date)) {
        return res.status(400).json({ success: false, error: 'date must be YYYY-MM-DD' });
      }
      date = req.query.date;
    }

    let days = parseInt(req.query.days, 10);
    if (!Number.isFinite(days)) days = 7;
    days = Math.min(90, Math.max(1, days));

    const [accountsRes, dayRes, periodRes, openRes, snapRes] = await Promise.all([
      pool.query(
        `SELECT id, prop_firm, account_number, platform, status,
                starting_balance, current_balance, daily_loss_limit, max_drawdown, updated_at
         FROM accounts WHERE user_id = $1 ORDER BY created_at`,
        [user.id]
      ),
      pool.query(
        `SELECT COUNT(*)::int AS total_trades,
                COUNT(*) FILTER (WHERE realized_pnl > 0)::int AS winning_trades,
                COUNT(*) FILTER (WHERE realized_pnl < 0)::int AS losing_trades,
                COALESCE(SUM(realized_pnl), 0)::float AS realized_pnl,
                COALESCE(SUM(commission), 0)::float AS commission
         FROM trades
         WHERE user_id = $1 AND status = 'closed'
           AND (closed_at AT TIME ZONE 'America/New_York')::date = $2::date`,
        [user.id, date]
      ),
      pool.query(
        `SELECT COUNT(*)::int AS total_trades,
                COUNT(*) FILTER (WHERE realized_pnl > 0)::int AS winning_trades,
                COUNT(*) FILTER (WHERE realized_pnl < 0)::int AS losing_trades,
                COALESCE(SUM(realized_pnl), 0)::float AS realized_pnl,
                COALESCE(SUM(commission), 0)::float AS commission
         FROM trades
         WHERE user_id = $1 AND status = 'closed'
           AND (closed_at AT TIME ZONE 'America/New_York')::date >  ($2::date - $3::int)
           AND (closed_at AT TIME ZONE 'America/New_York')::date <= $2::date`,
        [user.id, date, days]
      ),
      pool.query(
        `SELECT COUNT(*)::int AS open_positions
         FROM trades WHERE user_id = $1 AND status = 'open'`,
        [user.id]
      ),
      pool.query(
        `SELECT account_id, total_trades, winning_trades, losing_trades,
                gross_pnl, net_pnl, max_drawdown, equity
         FROM performance_snapshots WHERE user_id = $1 AND date = $2::date`,
        [user.id, date]
      )
    ]);

    const num = (v) => (v == null ? null : Number(v));
    const winRate = (w, t) => (t > 0 ? Math.round((w / t) * 1000) / 10 : null);

    // Annotate accounts with a derived trailing-drawdown buffer where computable.
    const accounts = accountsRes.rows.map((a) => {
      const bal = num(a.current_balance);
      const floor = num(a.max_drawdown); // interpreted as the max-loss-limit floor if present
      const buffer = (bal != null && floor != null) ? Math.round((bal - floor) * 100) / 100 : null;
      return { ...a, drawdown_buffer: buffer };
    });

    const day = dayRes.rows[0];
    const period = periodRes.rows[0];

    res.json({
      success: true,
      generated_at: new Date().toISOString(),
      timezone: 'America/New_York',
      user: {
        email: user.email,
        plan_tier: user.plan_tier,
        subscription_status: user.subscription_status,
        billing_source: user.billing_source || 'stripe'
      },
      accounts,
      open_positions: openRes.rows[0].open_positions,
      day: {
        date,
        total_trades: day.total_trades,
        winning_trades: day.winning_trades,
        losing_trades: day.losing_trades,
        win_rate_pct: winRate(day.winning_trades, day.total_trades),
        realized_pnl: day.realized_pnl,
        commission: day.commission,
        net_pnl: Math.round((day.realized_pnl - day.commission) * 100) / 100
      },
      period: {
        days,
        end_date: date,
        total_trades: period.total_trades,
        winning_trades: period.winning_trades,
        losing_trades: period.losing_trades,
        win_rate_pct: winRate(period.winning_trades, period.total_trades),
        realized_pnl: period.realized_pnl,
        commission: period.commission,
        net_pnl: Math.round((period.realized_pnl - period.commission) * 100) / 100
      },
      performance_snapshots: snapRes.rows
    });
  } catch (error) {
    console.error('summary error:', error);
    res.status(500).json({ success: false, error: error.message });
  }
});

// =============================================================================
// USER DASHBOARD ENDPOINTS (for web UI)
// =============================================================================

// POST /api/user/api-key — Generate/regenerate API key
// ============================================================================
// TradingView webhook relay (BYO strategy) — added 2026-07-19
// TV posts to /tv/:token (token in URL + passphrase in body); we queue the alert;
// the customer's bot polls GET /api/bot/alerts and executes it locally.
// ============================================================================
const TV_ALERT_MAX_AGE_SEC = 60;

// Public receiver — TradingView cannot send an API key, so auth = token(URL) + passphrase(body)
app.post('/tv/:token', async (req, res) => {
  try {
    const token = req.params.token || '';
    if (token.length < 16) return res.status(400).json({ success: false, error: 'bad token' });

    let body;
    try { body = typeof req.body === 'string' ? JSON.parse(req.body) : (req.body || {}); }
    catch (e) { return res.status(400).json({ success: false, error: 'body must be JSON' }); }

    const found = await pool.query(
      'SELECT id, tv_passphrase, subscription_status FROM users WHERE tv_token = $1',
      [token]
    );
    if (found.rows.length === 0) return res.status(404).json({ success: false, error: 'unknown webhook token' });
    const u = found.rows[0];

    const provided = String(body.passphrase || body.secret || '');
    if (!u.tv_passphrase || provided !== u.tv_passphrase) {
      return res.status(401).json({ success: false, error: 'invalid passphrase' });
    }
    if (!['active', 'trialing'].includes(u.subscription_status)) {
      return res.status(403).json({ success: false, error: 'subscription inactive' });
    }

    const payload = { ...body };
    delete payload.passphrase; delete payload.secret;
    await pool.query(
      'INSERT INTO pending_alerts (user_id, payload, source) VALUES ($1, $2, $3)',
      [u.id, JSON.stringify(payload), 'tradingview']
    );
    res.json({ success: true, queued: true });
  } catch (error) {
    console.error('TV webhook error:', error);
    res.status(500).json({ success: false, error: error.message });
  }
});

// ============================================================================
// POST /api/signals — first-party signal fan-out
// ============================================================================
// Until 2026-08-11 the only writer to pending_alerts was the TradingView relay
// above, which means our OWN FBD signals had no server-side route to customers
// at all. They went to Discord, and a Chrome extension scraping the page was
// the only thing that could turn them into a trade. Delivery therefore depended
// on a browser being open on the right site, and one Discord markup change
// would have stopped every subscriber simultaneously with no error anywhere.
//
// This endpoint takes a signal from our publisher and writes it into every
// active subscriber's queue. The customer's bot already polls that queue, so
// nothing ships on the client.
//
// AUTH is a shared publisher secret, not a user API key — the caller is us, not
// a customer. Compared in constant time so the endpoint cannot be used as an
// oracle to recover the secret a byte at a time.
//
// IDEMPOTENCY is on the ledger seq. A retried dispatch must never open a second
// position in somebody's account; that is real money, not a duplicate row.
function safeEqual(a, b) {
  const A = Buffer.from(String(a));
  const B = Buffer.from(String(b));
  if (A.length !== B.length) return false;
  return crypto.timingSafeEqual(A, B);
}

app.post('/api/signals', async (req, res) => {
  try {
    const secret = process.env.SIGNAL_PUBLISHER_KEY;
    if (!secret) {
      console.error('SIGNAL_PUBLISHER_KEY is not set — refusing to fan out');
      return res.status(503).json({ success: false, error: 'publisher not configured' });
    }
    const provided = req.headers['x-publisher-key'] || '';
    if (!provided || !safeEqual(provided, secret)) {
      return res.status(401).json({ success: false, error: 'bad publisher key' });
    }

    const body = typeof req.body === 'string' ? JSON.parse(req.body) : (req.body || {});

    // Selftest: prove auth and reachability WITHOUT queueing anything.
    // The first version of this sent seq 0 and relied on a price below the
    // app's sanity floor to stop anyone trading it. That is two guards deep for
    // something that should simply not write to customer queues at all — a
    // connectivity check has no business inserting rows that represent trade
    // instructions. Short-circuit here instead.
    if (body.selftest === true) {
      // Report BOTH numbers. The gap between "status says subscribed" and
      // "would actually receive a signal" is the thing worth seeing — if they
      // ever diverge unexpectedly, something is wrong with entitlement.
      const raw = await pool.query(
        `SELECT count(*)::int AS n FROM users WHERE subscription_status IN ('active','trialing')`
      );
      // Return WHO, not just how many. A count cannot be audited; a list can.
      // This endpoint already requires the publisher secret, so it is no more
      // exposed than the fan-out itself, and knowing exactly whose account a
      // trade instruction would land in is the entire point.
      const all = await pool.query(
        `SELECT email, subscription_status, billing_source,
                (api_key IS NOT NULL) AS has_key,
                (whop_membership_id IS NOT NULL
                 OR stripe_subscription_id IS NOT NULL) AS has_paid
           FROM users
          WHERE subscription_status IN ('active','trialing')
          ORDER BY created_at`
      );
      const eligible = all.rows.filter(r => r.has_key && r.has_paid);
      return res.json({
        success: true, selftest: true,
        would_deliver: eligible.length,
        status_active: all.rows.length,
        recipients: eligible.map(r => r.email),
        excluded: all.rows.filter(r => !(r.has_key && r.has_paid)).map(r => ({
          email: r.email,
          why: !r.has_paid ? 'no billing record (never paid)' : 'no api key'
        })),
        note: 'auth ok, nothing queued'
      });
    }

    const seq = Number(body.seq);
    const text = String(body.text || '').trim();
    if (!Number.isInteger(seq) || seq <= 0) {
      return res.status(400).json({ success: false, error: 'seq must be a positive integer' });
    }
    if (!text) {
      return res.status(400).json({ success: false, error: 'text is required' });
    }

    // Who is entitled to a real-time signal.
    //
    // The first version of this was subscription_status IN ('active','trialing')
    // and nothing else, which on 2026-08-11 resolved to SIX accounts on a
    // business with one member. create_test_user.js and setup_test_user.js both
    // insert users with status 'trialing', so fixtures were indistinguishable
    // from customers — and the payload here is a trade instruction, not a
    // newsletter.
    //
    // The first attempt at fixing this filtered on email patterns, and it was
    // wrong within minutes: it caught test@proptradebot.com and sailed straight
    // past e2e-test@proptradebot.com and ptbtest2026@mailinator.com. Guessing
    // from a string is not a basis for deciding whose account receives a trade.
    //
    // The real question is not "does this look like a test address" but "did
    // this person actually buy something". A billing record cannot be created
    // by a seed script, so it separates customers from fixtures by construction
    // rather than by pattern, and needs no maintenance as new fixtures appear.
    //
    // api_key IS NOT NULL stays: no key means no bot can poll, so queueing for
    // them writes rows nobody will ever read.
    const subs = await pool.query(
      `SELECT id, email FROM users
        WHERE subscription_status IN ('active','trialing')
          AND api_key IS NOT NULL
          AND (whop_membership_id IS NOT NULL OR stripe_subscription_id IS NOT NULL)`
    );

    // The payload the bot will normalise. `text` is what its parser reads; the
    // rest is carried for the audit trail and for future use once the client
    // can honour our ladder rather than only its own.
    const payload = {
      text,
      source: 'fbd',
      fbd_seq: seq,
      fbd_hash: body.hash || null,
      sym: body.sym || null,
      side: body.side || null,
      entry: body.entry ?? null,
      stop: body.stop ?? null,
      t1: body.t1 ?? null,
      t2: body.t2 ?? null,
      setup: body.setup || null,
      signal_ts: body.ts || null
    };

    let delivered = 0, skipped = 0;
    for (const u of subs.rows) {
      // Dedupe per user on the ledger seq. Cheap at this scale and it needs no
      // migration; if the subscriber count ever makes this hurt, add a unique
      // index on (user_id, (payload->>'fbd_seq')) instead.
      const dup = await pool.query(
        `SELECT 1 FROM pending_alerts
          WHERE user_id = $1 AND source = 'fbd' AND payload->>'fbd_seq' = $2
          LIMIT 1`,
        [u.id, String(seq)]
      );
      if (dup.rows.length) { skipped++; continue; }

      await pool.query(
        'INSERT INTO pending_alerts (user_id, payload, source) VALUES ($1, $2, $3)',
        [u.id, JSON.stringify(payload), 'fbd']
      );
      delivered++;
    }

    // Log WHO, not just how many. When a customer says they never got a signal,
    // this is the difference between checking a log line and guessing.
    console.log(`FBD signal #${seq} fan-out: ${delivered} delivered, ${skipped} already queued, ` +
                `recipients=[${subs.rows.map(r => r.email).join(', ')}]`);
    res.json({ success: true, seq, delivered, skipped });
  } catch (error) {
    console.error('signal fan-out error:', error);
    res.status(500).json({ success: false, error: error.message });
  }
});

// Bot poll — atomically claims this user's pending alerts (no double-deliver), drops stale ones
app.get('/api/bot/alerts', requireApiKey, async (req, res) => {
  try {
    const user = req.botUser;
    const maxAge = String(TV_ALERT_MAX_AGE_SEC);
    const claimed = await pool.query(
      `UPDATE pending_alerts SET delivered_at = now()
        WHERE id IN (
          SELECT id FROM pending_alerts
           WHERE user_id = $1 AND delivered_at IS NULL
             AND created_at > now() - ($2 || ' seconds')::interval
           ORDER BY created_at ASC LIMIT 20
        )
      RETURNING id, payload, source, created_at`,
      [user.id, maxAge]
    );
    await pool.query(
      `UPDATE pending_alerts SET delivered_at = now()
        WHERE user_id = $1 AND delivered_at IS NULL
          AND created_at <= now() - ($2 || ' seconds')::interval`,
      [user.id, maxAge]
    );
    res.json({ success: true, alerts: claimed.rows });
  } catch (error) {
    res.status(500).json({ success: false, error: error.message });
  }
});

// Generate/rotate the user's TradingView webhook token + passphrase
app.post('/api/user/tv-webhook', ClerkExpressRequireAuth(), async (req, res) => {
  try {
    const clerkId = req.auth.userId;
    const tvToken = crypto.randomBytes(24).toString('hex');
    const tvPass = 'ptv_' + crypto.randomBytes(12).toString('hex');
    const result = await pool.query(
      'UPDATE users SET tv_token = $1, tv_passphrase = $2 WHERE clerk_id = $3 RETURNING tv_token, tv_passphrase',
      [tvToken, tvPass, clerkId]
    );
    if (result.rows.length === 0) return res.status(404).json({ success: false, error: 'User not found' });
    const base = process.env.PUBLIC_BASE_URL || 'https://proptradebot-baass.onrender.com';
    res.json({ success: true, webhook_url: `${base}/tv/${result.rows[0].tv_token}`, passphrase: result.rows[0].tv_passphrase });
  } catch (error) {
    res.status(500).json({ success: false, error: error.message });
  }
});

// Retrieve the user's current TradingView webhook url + passphrase
app.get('/api/user/tv-webhook', ClerkExpressRequireAuth(), async (req, res) => {
  try {
    const clerkId = req.auth.userId;
    const result = await pool.query('SELECT tv_token, tv_passphrase FROM users WHERE clerk_id = $1', [clerkId]);
    if (result.rows.length === 0) return res.status(404).json({ success: false, error: 'User not found' });
    const row = result.rows[0];
    const base = process.env.PUBLIC_BASE_URL || 'https://proptradebot-baass.onrender.com';
    res.json({ success: true, configured: !!row.tv_token, webhook_url: row.tv_token ? `${base}/tv/${row.tv_token}` : null, passphrase: row.tv_passphrase || null });
  } catch (error) {
    res.status(500).json({ success: false, error: error.message });
  }
});

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
      // Masked, for DISPLAY only.
      api_key: key ? key.substring(0, 8) + '...' + key.substring(key.length - 4) : null,
      // The real key, for the Copy button.
      //
      // Until 2026-08-13 only the masked form was returned, and the dashboard's
      // Copy button copied THAT — so the clipboard received the literal string
      // "ptb_1b7a...9fdf", ellipsis included. Every customer following the
      // documented setup ("paste this key into the wizard") stored a key that
      // could never authenticate. Because an invalid key made the app exit, and
      // the wizard is served by the app, they were then stuck with no way to
      // correct it.
      //
      // This route is already behind ClerkExpressRequireAuth and the key belongs
      // to the caller, so returning it to them is no more exposed than the
      // regenerate flow that already shows it in full.
      api_key_full: key || null,
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

// POST /api/user/billing-portal — Redirect to Stripe billing portal (cancel, update card, etc.)
app.post('/api/user/billing-portal', ClerkExpressRequireAuth(), async (req, res) => {
  try {
    const clerkId = req.auth.userId;
    const result = await pool.query(
      'SELECT stripe_customer_id FROM users WHERE clerk_id = $1',
      [clerkId]
    );
    if (result.rows.length === 0) return res.status(404).json({ success: false, error: 'User not found' });

    const customerId = result.rows[0].stripe_customer_id;
    if (!customerId) return res.status(400).json({ success: false, error: 'No billing account found. Please subscribe first.' });

    const session = await stripe.billingPortal.sessions.create({
      customer: customerId,
      return_url: `${process.env.FRONTEND_URL || 'https://proptradebot.com'}/dashboard.html`,
    });

    res.json({ success: true, url: session.url });
  } catch (error) {
    console.error('Billing portal error:', error.message);
    res.status(500).json({ success: false, error: error.message });
  }
});

// GET /api/user/subscription — Get current subscription status
app.get('/api/user/subscription', ClerkExpressRequireAuth(), async (req, res) => {
  try {
    const clerkId = req.auth.userId;
    const result = await pool.query(
      `SELECT plan_tier, subscription_status, stripe_subscription_id, stripe_customer_id
       FROM users WHERE clerk_id = $1`,
      [clerkId]
    );
    if (result.rows.length === 0) return res.status(404).json({ success: false, error: 'User not found' });

    const user = result.rows[0];
    let stripeSubscription = null;

    if (user.stripe_subscription_id) {
      try {
        stripeSubscription = await stripe.subscriptions.retrieve(user.stripe_subscription_id);
      } catch (e) {
        // test-mode sub looked up with live key — ignore, use DB
      }
    }

    res.json({
      success: true,
      plan_tier: user.plan_tier,
      subscription_status: user.subscription_status,
      cancel_at_period_end: stripeSubscription?.cancel_at_period_end || false,
      current_period_end: stripeSubscription?.current_period_end || null,
    });
  } catch (error) {
    res.status(500).json({ success: false, error: error.message });
  }
});

// =============================================================================
// WHOP WEBHOOK — membership lifecycle events
// =============================================================================

// Helper: provision API key for a Whop member (idempotent)
async function provisionWhopUser({ whopMembershipId, whopUserId, email, username }) {
  // Upsert user by whop_membership_id
  const apiKey = 'ptb_' + crypto.randomBytes(32).toString('hex');

  // Try to find existing user by whop_membership_id first
  let result = await pool.query(
    'SELECT id, api_key FROM users WHERE whop_membership_id = $1',
    [whopMembershipId]
  );

  if (result.rows.length > 0) {
    // Existing user — reactivate
    await pool.query(
      `UPDATE users
         SET subscription_status = 'active',
             plan_tier            = 'pro',
             whop_user_id         = $2
       WHERE whop_membership_id = $1`,
      [whopMembershipId, whopUserId]
    );
    console.log('✅ Whop user reactivated:', whopMembershipId);
    return result.rows[0];
  }

  // New user — create with generated API key
  // email may be null if Whop doesn't share it; fall back to username
  const userEmail = email || (username ? `${username}@whop.user` : `${whopMembershipId}@whop.user`);

  result = await pool.query(
    `INSERT INTO users
       (clerk_id, email, name, plan_tier, subscription_status,
        api_key, api_key_created_at, billing_source, whop_membership_id, whop_user_id)
     VALUES
       ($1, $2, $3, 'pro', 'active',
        $4, NOW(), 'whop', $5, $6)
     ON CONFLICT (whop_membership_id) DO UPDATE
       SET subscription_status = 'active',
           plan_tier            = 'pro',
           whop_user_id         = EXCLUDED.whop_user_id
     RETURNING id, api_key`,
    [
      'whop_' + whopMembershipId,  // synthetic clerk_id to satisfy NOT NULL
      userEmail,
      username || whopMembershipId,
      apiKey,
      whopMembershipId,
      whopUserId
    ]
  );

  console.log('✅ Whop user provisioned:', userEmail, whopMembershipId);
  return result.rows[0];
}

// Helper: revoke access for a Whop member
async function revokeWhopUser(whopMembershipId) {
  await pool.query(
    `UPDATE users
       SET subscription_status = 'cancelled',
           plan_tier            = 'none'
     WHERE whop_membership_id = $1`,
    [whopMembershipId]
  );
  console.log('🚫 Whop user access revoked:', whopMembershipId);
}

app.post('/whop-webhook', async (req, res) => {
  // Verify signature using Standard Webhooks
  const secret = process.env.WHOP_WEBHOOK_SECRET;
  if (!secret) {
    console.error('❌ WHOP_WEBHOOK_SECRET not set');
    return res.status(500).json({ error: 'Webhook secret not configured' });
  }

  let event;
  try {
    const wh = new Webhook(Buffer.from(secret).toString('base64'));
    const bodyStr = req.body.toString('utf8');
    event = wh.verify(bodyStr, req.headers);
    event = JSON.parse(bodyStr);  // parse again after verify
  } catch (err) {
    console.log('❌ Whop webhook signature invalid:', err.message);
    return res.status(400).json({ error: 'Invalid signature' });
  }

  // Log event
  try {
    await pool.query(
      `INSERT INTO whop_events (whop_event_id, event_type, membership_id, user_id, payload)
       VALUES ($1, $2, $3, $4, $5)
       ON CONFLICT (whop_event_id) DO NOTHING`,
      [
        event.id || event.data?.id || crypto.randomUUID(),
        event.type,
        event.data?.membership_id || event.data?.id,
        event.data?.user_id,
        event.data
      ]
    );
  } catch (e) {
    console.error('Failed to log Whop event:', e.message);
  }

  console.log('📨 Whop webhook:', event.type, event.data?.id);

  try {
    switch (event.type) {
      case 'membership.went_valid':
      case 'membership.activated': {
        // Member subscribed or renewed — grant access
        const mem = event.data;
        const whopMembershipId = mem.id;
        const whopUserId = mem.user_id;
        const email    = mem.user?.email;
        const username = mem.user?.username || mem.user?.name;

        await provisionWhopUser({ whopMembershipId, whopUserId, email, username });
        break;
      }

      case 'membership.went_invalid':
      case 'membership.cancelled':
      case 'membership.expired': {
        // Member cancelled or payment failed — revoke access
        const whopMembershipId = event.data?.id;
        if (whopMembershipId) await revokeWhopUser(whopMembershipId);
        break;
      }

      case 'payment.succeeded': {
        // Payment received — ensure user is active (belt-and-suspenders)
        const membershipId = event.data?.membership_id;
        if (membershipId) {
          await pool.query(
            `UPDATE users SET subscription_status = 'active' WHERE whop_membership_id = $1`,
            [membershipId]
          );
        }
        break;
      }

      case 'payment.failed': {
        const membershipId = event.data?.membership_id;
        if (membershipId) {
          await pool.query(
            `UPDATE users SET subscription_status = 'past_due' WHERE whop_membership_id = $1`,
            [membershipId]
          );
        }
        break;
      }

      default:
        console.log('Unhandled Whop event:', event.type);
    }
  } catch (e) {
    console.error('Whop webhook handler error:', e.message);
    // Still return 200 — Whop will retry on non-2xx
  }

  res.json({ received: true });
});

// GET /whop-success — Landing page after Whop checkout
// Whop redirects here; user gets their API key instructions
app.get('/whop-success', async (req, res) => {
  res.send(`
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>Welcome to PropTradeBot!</title>
      <style>
        body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #0a0e1a; color: #e2e8f0; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; padding: 16px; box-sizing: border-box; }
        .card { background: #111827; border: 1px solid #1e293b; border-radius: 16px; padding: 40px 48px; max-width: 520px; width: 100%; }
        .icon { font-size: 56px; margin-bottom: 16px; }
        h1 { margin: 0 0 8px; font-size: 26px; color: #f1f5f9; }
        p { color: #94a3b8; margin: 0 0 20px; line-height: 1.6; }
        .steps { background: #0f172a; border-radius: 10px; padding: 20px 24px; margin-bottom: 24px; }
        .step { display: flex; gap: 12px; margin-bottom: 14px; align-items: flex-start; }
        .step:last-child { margin-bottom: 0; }
        .step-num { background: #3b82f6; color: white; border-radius: 50%; width: 24px; height: 24px; display: flex; align-items: center; justify-content: center; font-size: 12px; font-weight: 700; flex-shrink: 0; margin-top: 2px; }
        .step-text { color: #cbd5e1; font-size: 14px; line-height: 1.5; }
        .step-text strong { color: #f1f5f9; }
        .btn { background: #3b82f6; color: white; padding: 12px 28px; border-radius: 8px; text-decoration: none; display: inline-block; font-weight: 600; font-size: 15px; }
        .btn:hover { background: #2563eb; }
        .note { font-size: 13px; color: #64748b; margin-top: 16px; }
      </style>
    </head>
    <body>
      <div class="card">
        <div class="icon">🎉</div>
        <h1>You're in! Welcome to PropTradeBot.</h1>
        <p>Your subscription is active. Three steps, about ten minutes:</p>
        <div class="steps">
          <!-- DISCORD GOES FIRST, DELIBERATELY.
               This page used to open with "download the Mac app" and never
               mentioned Discord at all — on a product whose primary thing is
               real-time signals posted to Discord. Worse, this redirect pulls
               the buyer off Whop the instant they pay, which is where the
               Discord link lives. Someone who does not go back never joins the
               channel they just paid for, and churns in week one blaming us
               for sending nothing. -->
          <div class="step">
            <div class="step-num">1</div>
            <div class="step-text"><strong>Join the Discord</strong> — this is where signals post in real time. Head back to your Whop account at <a href="https://whop.com/hub" style="color:#60a5fa">whop.com/hub</a> and open the Discord app to link it. Do this first, it is the part you are paying for.</div>
          </div>
          <div class="step">
            <div class="step-num">2</div>
            <div class="step-text"><strong>Get your API key</strong> — go to <a href="/dashboard" style="color:#60a5fa">proptradebot.com/dashboard</a> and sign in <strong>with the same email you used on Whop</strong>. A different email will not see your subscription. Generate your key there.</div>
          </div>
          <div class="step">
            <div class="step-num">3</div>
            <div class="step-text"><strong>Install and run the wizard</strong> — download below, drag PropTradeBot to Applications, paste your API key, then pick your prop firm and connect it. Topstep, Tradeify, Lucid, Take Profit Trader, MyFundedFutures and Bulenox are all supported.</div>
          </div>
        </div>
        <a href="/downloads/PropTradeBot.dmg" class="btn">⬇ Download PropTradeBot for Mac</a>
        <p class="note">Need help? Visit <a href="/support" style="color:#60a5fa">proptradebot.com/support</a> or email support@proptradebot.com</p>
      </div>
    </body>
    </html>
  `);
});

app.listen(PORT, () => {
  console.log(`🚀 Server running on http://localhost:${PORT}`);
  console.log(`📊 Health check: http://localhost:${PORT}/health`);
  console.log(`🔐 Protected routes require Clerk auth`);
  console.log(`🤖 Bot gateway: /api/bot/* (API key auth)`);
});
