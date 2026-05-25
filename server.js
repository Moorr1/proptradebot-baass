require('dotenv').config();
const express = require('express');
const cors = require('cors');
const stripe = require('stripe')(process.env.STRIPE_SECRET_KEY);

const app = express();
const PORT = process.env.PORT || 3001;

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

// Create a customer
app.post('/api/customers', async (req, res) => {
  try {
    const { email, name } = req.body;
    const customer = await stripe.customers.create({
      email,
      name,
    });
    res.json({ success: true, customer });
  } catch (error) {
    res.status(400).json({ success: false, error: error.message });
  }
});

// Create checkout session for subscription
app.post('/api/checkout', async (req, res) => {
  try {
    const { priceId, customerId, successUrl, cancelUrl } = req.body;
    
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
      success_url: successUrl || 'http://localhost:3001/success',
      cancel_url: cancelUrl || 'http://localhost:3001/cancel',
    });

    res.json({ success: true, sessionId: session.id, url: session.url });
  } catch (error) {
    res.status(400).json({ success: false, error: error.message });
  }
});

// Get customer subscriptions
app.get('/api/customers/:customerId/subscriptions', async (req, res) => {
  try {
    const subscriptions = await stripe.subscriptions.list({
      customer: req.params.customerId,
      status: 'all',
    });
    res.json({ success: true, subscriptions });
  } catch (error) {
    res.status(400).json({ success: false, error: error.message });
  }
});

// Cancel subscription
app.post('/api/subscriptions/:subscriptionId/cancel', async (req, res) => {
  try {
    const subscription = await stripe.subscriptions.cancel(
      req.params.subscriptionId
    );
    res.json({ success: true, subscription });
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
    console.log(`Webhook signature verification failed:`, err.message);
    return res.status(400).send(`Webhook Error: ${err.message}`);
  }

  // Handle events
  switch (event.type) {
    case 'checkout.session.completed':
      console.log('✅ Checkout completed:', event.data.object.id);
      // TODO: Activate user account
      break;
    
    case 'invoice.paid':
      console.log('💰 Invoice paid:', event.data.object.id);
      // TODO: Extend subscription
      break;
    
    case 'invoice.payment_failed':
      console.log('❌ Payment failed:', event.data.object.id);
      // TODO: Notify user, suspend account
      break;
    
    case 'customer.subscription.deleted':
      console.log('🚫 Subscription cancelled:', event.data.object.id);
      // TODO: Deactivate account
      break;
    
    default:
      console.log(`Unhandled event type: ${event.type}`);
  }

  res.json({ received: true });
});

// Get Stripe config (publishable key for frontend)
app.get('/api/config', (req, res) => {
  res.json({
    publishableKey: process.env.STRIPE_PUBLISHABLE_KEY,
  });
});

app.listen(PORT, () => {
  console.log(`🚀 Server running on http://localhost:${PORT}`);
  console.log(`📊 Health check: http://localhost:${PORT}/health`);
});
