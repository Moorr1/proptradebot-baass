require('dotenv').config();
const express = require('express');
const cors = require('cors');
const stripe = require('stripe')(process.env.STRIPE_SECRET_KEY);
const { ClerkExpressRequireAuth } = require('@clerk/clerk-sdk-node');
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

// Health check
app.get('/health', (req, res) => {
  res.json({ status: 'ok', timestamp: new Date().toISOString() });
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
app.use('/api/bot', ClerkExpressRequireAuth());

// Get or create user profile
app.get('/api/user/profile', async (req, res) => {
  try {
    const clerkId = req.auth.userId;
    const email = req.auth.sessionClaims.email;
    
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
        [clerkId, email, req.auth.sessionClaims.name || email]
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
    const clerkId = req.auth.userId;
    const email = req.auth.sessionClaims.email;
    
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
        [clerkId, email, req.auth.sessionClaims.name || email]
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
    res.status(400).json({ success: false, error: error.message });
  }
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

app.listen(PORT, () => {
  console.log(`🚀 Server running on http://localhost:${PORT}`);
  console.log(`📊 Health check: http://localhost:${PORT}/health`);
  console.log(`🔐 Protected routes require Clerk auth`);
});
