# PropTradeBot BaaS

Backend-as-a-Service for PropTradeBot - automated trading bot for prop firm accounts.

## Features

- **Stripe Subscriptions** - 3 tiers: Basic ($99/mo), Pro ($149/mo), Enterprise ($299/mo)
- **Clerk Authentication** - Secure user auth with JWT
- **Neon Postgres** - Serverless database for users, accounts, trades
- **PostHog Analytics** - Product analytics and user tracking

## API Endpoints

### Public
- `GET /health` - Health check
- `GET /api/config` - Stripe publishable key
- `GET /api/products` - Subscription plans

### Protected (requires Clerk auth)
- `GET /api/user/profile` - Get/create user profile
- `GET /api/accounts` - List trading accounts
- `POST /api/accounts` - Add trading account
- `GET /api/bot/config` - Get bot configuration
- `PUT /api/bot/config` - Update bot configuration
- `POST /api/checkout` - Create subscription checkout

### Webhooks
- `POST /webhook` - Stripe webhook handler

## Environment Variables

See `.env.example` for required variables.

## Tech Stack

- Node.js + Express
- Stripe (payments)
- Clerk (auth)
- Neon Postgres (database)
- PostHog (analytics)

## License

Private
