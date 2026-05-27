# ISC SPREADS — Production Trading Bot Platform
## Institutional Signal Confluence | v1.0.0

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                   ISC SPREADS PLATFORM                   │
├──────────────┬──────────────────┬───────────────────────┤
│   FRONTEND   │     BACKEND      │      DATABASE         │
│  index.html  │   main.py        │   PostgreSQL 16       │
│  Pure HTML   │   FastAPI        │   + pg_cron           │
│  Vanilla JS  │   APScheduler    │   schema.sql          │
│  Chart.js    │   Binance API    │   Africa/Lagos TZ     │
└──────────────┴──────────────────┴───────────────────────┘
```

---

## File Structure

```
isc-spreads/
├── index.html          ← Complete frontend SPA (self-contained)
├── main.py             ← FastAPI backend + signal engine
├── schema.sql          ← PostgreSQL schema + pg_cron setup
├── deploy-config.txt   ← Dockerfile / docker-compose / nginx
├── .env.example        ← Environment variable template
└── README.md           ← This file
```

---

## Quick Start (Development)

### 1. Clone & Configure
```bash
cp deploy-config.txt .env.example
# Edit .env with your values
```

### 2. Database Setup
```bash
createdb iscspreads
psql iscspreads < schema.sql
```

### 3. Install Python deps
```bash
pip install -r requirements.txt
```

### 4. Run backend
```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### 5. Open frontend
Open `index.html` directly in browser (no build step needed).

---

## Production Deployment (Docker)

```bash
# Build and start all services
docker-compose up -d

# Check logs
docker-compose logs -f api

# Apply schema (first time)
docker exec -i isc-spreads-db-1 psql -U isc iscspreads < schema.sql
```

---

## Admin Account

Admin role is auto-granted to `samuelmaigari0@gmail.com` on signup.

The trigger in `schema.sql` handles this:
```sql
CREATE TRIGGER trigger_auto_grant_role
  AFTER INSERT ON users
  FOR EACH ROW EXECUTE FUNCTION auto_grant_admin_role();
```

Admin capabilities:
- View all pending payments
- Approve payments → auto-dispatch activation codes
- Generate codes manually for any plan
- View/modify all users and plans
- Update wallet address
- Toggle signal engine modules
- Manual signal engine trigger

---

## Signal Engine

Runs every 6 hours on WAT schedule (Africa/Lagos, UTC+1):
- 00:00 WAT (23:00 UTC)
- 06:00 WAT (05:00 UTC)
- 12:00 WAT (11:00 UTC)
- 18:00 WAT (17:00 UTC)

pg_cron job:
```sql
SELECT cron.schedule(
  'isc-signal-engine-6h',
  '0 23,5,11,17 * * *',
  $$SELECT run_signal_engine()$$
);
```

Signal stack per pair:
1. Fetch live 6H klines from Binance public API
2. Compute RSI(14) on closes
3. Compute volume ratio (last vs 5-candle avg)
4. Detect Order Block (displacement candle)
5. Detect Liquidity Sweep (equal H/L + volume spike)
6. Detect CRT pattern (wick ratio analysis)
7. Detect FVG (3-candle imbalance)
8. Compute ICS score (0–100)
9. Detect Institutional Footprint (composite)
10. Store signal + institutional zone in DB

ICS Scoring:
| Factor                | Points |
|-----------------------|--------|
| Liquidity Sweep       | +25    |
| Order Block           | +20    |
| 6H Structure Aligned  | +15    |
| FVG/Imbalance         | +15    |
| Volume CVD Spike      | +15    |
| RSI Divergence        | +10    |
| **Total**             | **100**|

Thresholds:
- < 50 → Signal discarded
- 50–74 → Standard (Basic/Premium)
- 75–100 → High-conviction (Elite only)

---

## Plan Access Control

| Feature                    | Demo | Basic | Premium | Elite |
|----------------------------|------|-------|---------|-------|
| ICT Killzone               | ✓    | ✓     | ✓       | ✓     |
| 6H Structure               | ✓    | ✓     | ✓       | ✓     |
| FVG Visual (read-only)     | ✓    | ✓     | ✓       | ✓     |
| SMC (ChoCH/BoS)            | ✗    | ✓     | ✓       | ✓     |
| Liquidity Sweep            | ✗    | ✓     | ✓       | ✓     |
| RSI Divergence             | ✗    | ✓     | ✓       | ✓     |
| CRT Movement               | ✗    | ✗     | ✓       | ✓     |
| Volume CVD                 | ✗    | ✗     | ✓       | ✓     |
| Order Block Tracker        | ✗    | ✗     | ✓       | ✓     |
| Institutional ICS 50-74    | ✗    | ✗     | ✓       | ✓     |
| 1H+15M Scalp               | ✗    | ✗     | ✓       | ✓     |
| High-Conviction ICS 75+    | ✗    | ✗     | ✗       | ✓     |
| Iceberg Detection          | ✗    | ✗     | ✗       | ✓     |
| Delta Divergence           | ✗    | ✗     | ✗       | ✓     |
| Stoichiometric R:R Engine  | ✗    | ✗     | ✗       | ✓     |

---

## Payment & Activation Flow

1. User selects plan on `/upgrade` page
2. Platform displays wallet: `0x3fec94ae357d5f4921f04a7a7e335dd21df74330`
3. User sends crypto (USDT/ETH/BTC)
4. User submits email + TX hash
5. Payment stored with status `pending`
6. Admin sees pending payment in Admin Panel → Payments tab
7. Admin clicks "Approve & Send Code"
8. Backend calls `approve_payment_and_send_code()` in DB
9. Unique code generated: `ISC-XXXX-XXXX-XXXX`
10. Code emailed to user via Resend
11. User enters code on `/activate` page
12. `activate_plan()` SQL function validates + unlocks plan
13. User dashboard reflects new plan immediately

---

## Wallet Configuration

Primary wallet: `0x3fec94ae357d5f4921f04a7a7e335dd21df74330`
Network: ERC-20 (Ethereum)

Admin can rotate wallet address via Admin Panel → Wallet Config.
All addresses served dynamically from `wallet_config` table.
Never hardcoded in frontend (fetched from `/wallet/active` API).

---

## Community Links

- Telegram: https://t.me/iscspreadscommunity
- X: https://x.com/iscspreadss
- Instagram: https://instagram.com/iscspreads
- WhatsApp: https://chat.whatsapp.com/iscspreadsvip
- Email: support@iscspreads.io

---

## Security Checklist

- [x] Passwords hashed with bcrypt
- [x] JWT with 7-day expiry
- [x] Row Level Security on all user tables
- [x] Admin role via DB trigger (not user-configurable)
- [x] Activation codes are single-use + time-limited (48h)
- [x] Wallet address served from DB (not hardcoded in JS)
- [x] pg_notify for async email dispatch
- [x] HTTPS enforced via nginx
- [x] CORS locked to domain in production
- [x] SQL injection protected via parameterised queries (asyncpg)

---

## Competitive Differentiators

| Limitation (Other Bots)       | ISC Spreads Strength               |
|--------------------------------|------------------------------------|
| React to price, no context     | Liquidity sweep read before entry  |
| Single timeframe               | 6H + 1H + 15M multi-TF engine     |
| Fixed R:R 1:1 or 1:2           | Stoichiometric R:R minimum 1:3     |
| RSI-only signals               | RSI + Volume + FVG confluence      |
| No institutional awareness     | Full ICT/OB/ICS detection          |
| No demo/live separation        | Demo-first trust funnel            |
| No trade scoring               | ICS score on every signal          |
| No smart money tracking        | Full institutional footprint engine|

---

## License & IP

All signal logic, ICS methodology, institutional tracking engine,
and platform architecture are the exclusive intellectual property
of ISC Spreads. All rights reserved.

Document: ISC-MVP-001 | Version: 1.0 | Status: Production Ready
Admin: samuelmaigari0@gmail.com
