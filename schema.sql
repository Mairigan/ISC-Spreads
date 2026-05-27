-- ═══════════════════════════════════════════════════════════════
-- ISC SPREADS — PRODUCTION DATABASE SCHEMA
-- PostgreSQL + pg_cron
-- Timezone: Africa/Lagos (WAT, UTC+1) for 6H signal engine
-- ═══════════════════════════════════════════════════════════════

-- EXTENSIONS
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "pg_cron";

-- TIMEZONE
SET timezone = 'Africa/Lagos';

-- ── USERS ────────────────────────────────────────────────────────
CREATE TABLE users (
  id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  email         VARCHAR(255) UNIQUE NOT NULL,
  name          VARCHAR(255) NOT NULL,
  password_hash VARCHAR(255) NOT NULL,
  plan          VARCHAR(20) DEFAULT 'demo' CHECK (plan IN ('demo','basic','premium','elite')),
  is_active     BOOLEAN DEFAULT TRUE,
  created_at    TIMESTAMPTZ DEFAULT NOW(),
  updated_at    TIMESTAMPTZ DEFAULT NOW()
);

-- ── USER ROLES ────────────────────────────────────────────────────
CREATE TABLE user_roles (
  id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  user_id    UUID REFERENCES users(id) ON DELETE CASCADE,
  role       VARCHAR(50) NOT NULL DEFAULT 'user' CHECK (role IN ('user','admin','support')),
  granted_at TIMESTAMPTZ DEFAULT NOW(),
  granted_by UUID REFERENCES users(id)
);

-- GRANT ADMIN ROLE IMMEDIATELY ON SIGNUP FOR samuelmaigari0@gmail.com
-- This trigger fires after INSERT into users
CREATE OR REPLACE FUNCTION auto_grant_admin_role()
RETURNS TRIGGER AS $$
BEGIN
  IF NEW.email = 'samuelmaigari0@gmail.com' THEN
    INSERT INTO user_roles (user_id, role) VALUES (NEW.id, 'admin');
    UPDATE users SET plan = 'elite' WHERE id = NEW.id;
  ELSE
    INSERT INTO user_roles (user_id, role) VALUES (NEW.id, 'user');
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_auto_grant_role
  AFTER INSERT ON users
  FOR EACH ROW EXECUTE FUNCTION auto_grant_admin_role();

-- ── PLAN SUBSCRIPTIONS ───────────────────────────────────────────
CREATE TABLE subscriptions (
  id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  user_id      UUID REFERENCES users(id) ON DELETE CASCADE,
  plan         VARCHAR(20) NOT NULL CHECK (plan IN ('demo','basic','premium','elite')),
  activated_at TIMESTAMPTZ DEFAULT NOW(),
  expires_at   TIMESTAMPTZ,
  is_active    BOOLEAN DEFAULT TRUE,
  activated_by_code VARCHAR(50),
  UNIQUE(user_id)
);

-- ── WALLET CONFIG ────────────────────────────────────────────────
CREATE TABLE wallet_config (
  id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  address    VARCHAR(255) NOT NULL,
  network    VARCHAR(50) DEFAULT 'ERC-20',
  label      VARCHAR(100),
  is_active  BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Seed the configured wallet
INSERT INTO wallet_config (address, network, label, is_active)
VALUES ('0x3fec94ae357d5f4921f04a7a7e335dd21df74330', 'ERC-20', 'Primary ISC Spreads Wallet', TRUE);

-- ── PLAN PRICING ─────────────────────────────────────────────────
CREATE TABLE plan_pricing (
  plan          VARCHAR(20) PRIMARY KEY,
  price_usd     NUMERIC(10,2) NOT NULL,
  duration_days INTEGER DEFAULT 30,
  updated_at    TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO plan_pricing (plan, price_usd, duration_days) VALUES
  ('demo',    0.00,   NULL),
  ('basic',   49.00,  30),
  ('premium', 99.00,  30),
  ('elite',   149.00, 30);

-- ── PAYMENTS ─────────────────────────────────────────────────────
CREATE TABLE payments (
  id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  user_email    VARCHAR(255) NOT NULL,
  plan          VARCHAR(20) NOT NULL,
  amount_usd    NUMERIC(10,2),
  tx_hash       VARCHAR(255),
  wallet_addr   VARCHAR(255),
  status        VARCHAR(20) DEFAULT 'pending' CHECK (status IN ('pending','verified','rejected','expired')),
  submitted_at  TIMESTAMPTZ DEFAULT NOW(),
  verified_at   TIMESTAMPTZ,
  verified_by   UUID REFERENCES users(id),
  activation_code_sent BOOLEAN DEFAULT FALSE,
  notes         TEXT
);

CREATE INDEX idx_payments_status ON payments(status);
CREATE INDEX idx_payments_email  ON payments(user_email);

-- ── ACTIVATION CODES ─────────────────────────────────────────────
CREATE TABLE activation_codes (
  id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  code         VARCHAR(50) UNIQUE NOT NULL,
  plan         VARCHAR(20) NOT NULL,
  user_email   VARCHAR(255),           -- NULL = distributable code
  payment_id   UUID REFERENCES payments(id),
  is_used      BOOLEAN DEFAULT FALSE,
  used_at      TIMESTAMPTZ,
  used_by      UUID REFERENCES users(id),
  expires_at   TIMESTAMPTZ DEFAULT (NOW() + INTERVAL '48 hours'),
  created_at   TIMESTAMPTZ DEFAULT NOW(),
  created_by   UUID REFERENCES users(id)
);

-- Code generation function
CREATE OR REPLACE FUNCTION generate_activation_code(
  p_plan      VARCHAR,
  p_email     VARCHAR DEFAULT NULL,
  p_admin_id  UUID    DEFAULT NULL,
  p_payment_id UUID   DEFAULT NULL
) RETURNS VARCHAR AS $$
DECLARE
  v_code VARCHAR;
BEGIN
  v_code := 'ISC-' ||
            upper(substring(encode(gen_random_bytes(3),'hex') FROM 1 FOR 4)) || '-' ||
            upper(substring(encode(gen_random_bytes(3),'hex') FROM 1 FOR 4)) || '-' ||
            upper(substring(encode(gen_random_bytes(3),'hex') FROM 1 FOR 4));
  INSERT INTO activation_codes(code, plan, user_email, payment_id, created_by)
  VALUES (v_code, p_plan, p_email, p_payment_id, p_admin_id);
  RETURN v_code;
END;
$$ LANGUAGE plpgsql;

-- Activation function — called when user enters code
CREATE OR REPLACE FUNCTION activate_plan(
  p_code    VARCHAR,
  p_user_id UUID,
  p_email   VARCHAR
) RETURNS JSONB AS $$
DECLARE
  v_code  activation_codes%ROWTYPE;
  v_plan  VARCHAR;
BEGIN
  SELECT * INTO v_code FROM activation_codes
  WHERE code = p_code
    AND (user_email IS NULL OR user_email = p_email)
    AND is_used = FALSE
    AND expires_at > NOW();

  IF NOT FOUND THEN
    RETURN '{"success":false,"error":"Invalid or expired code"}'::JSONB;
  END IF;

  v_plan := v_code.plan;

  -- Mark code used
  UPDATE activation_codes SET is_used=TRUE, used_at=NOW(), used_by=p_user_id WHERE id=v_code.id;

  -- Update user plan
  UPDATE users SET plan=v_plan, updated_at=NOW() WHERE id=p_user_id;

  -- Upsert subscription
  INSERT INTO subscriptions(user_id, plan, activated_at, expires_at, is_active, activated_by_code)
  VALUES(p_user_id, v_plan, NOW(), NOW() + INTERVAL '30 days', TRUE, p_code)
  ON CONFLICT(user_id) DO UPDATE SET
    plan=v_plan, activated_at=NOW(), expires_at=NOW()+INTERVAL '30 days',
    is_active=TRUE, activated_by_code=p_code;

  RETURN jsonb_build_object('success',true,'plan',v_plan);
END;
$$ LANGUAGE plpgsql;

-- Auto-approve payment and send code
CREATE OR REPLACE FUNCTION approve_payment_and_send_code(
  p_payment_id UUID,
  p_admin_id   UUID
) RETURNS VARCHAR AS $$
DECLARE
  v_pmt payments%ROWTYPE;
  v_code VARCHAR;
BEGIN
  SELECT * INTO v_pmt FROM payments WHERE id=p_payment_id AND status='pending';
  IF NOT FOUND THEN RAISE EXCEPTION 'Payment not found or already processed'; END IF;

  UPDATE payments SET status='verified', verified_at=NOW(), verified_by=p_admin_id, activation_code_sent=TRUE
  WHERE id=p_payment_id;

  v_code := generate_activation_code(v_pmt.plan, v_pmt.user_email, p_admin_id, p_payment_id);
  -- Email dispatch would be triggered here via pg_notify or external service
  PERFORM pg_notify('send_activation_email', json_build_object('email',v_pmt.user_email,'code',v_code,'plan',v_pmt.plan)::TEXT);
  RETURN v_code;
END;
$$ LANGUAGE plpgsql;

-- ── MARKET DATA ──────────────────────────────────────────────────
CREATE TABLE market_prices (
  id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  symbol     VARCHAR(20) NOT NULL,
  price      NUMERIC(20,8),
  high_24h   NUMERIC(20,8),
  low_24h    NUMERIC(20,8),
  volume_24h NUMERIC(20,2),
  pct_change NUMERIC(8,4),
  rsi_6h     NUMERIC(8,4),
  volume_ratio NUMERIC(8,4),
  fetched_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_market_prices_symbol ON market_prices(symbol, fetched_at DESC);

-- ── SIGNALS ──────────────────────────────────────────────────────
CREATE TABLE signals (
  id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  pair            VARCHAR(20) NOT NULL,
  direction       VARCHAR(5)  NOT NULL CHECK (direction IN ('LONG','SHORT')),
  timeframe       VARCHAR(10) DEFAULT '6H',
  entry_price     NUMERIC(20,8),
  stop_loss       NUMERIC(20,8),
  take_profit_1   NUMERIC(20,8),
  take_profit_2   NUMERIC(20,8),
  rr_ratio        VARCHAR(10) DEFAULT '1:3',
  rsi             NUMERIC(8,4),
  volume_ratio    NUMERIC(8,4),
  ics_score       INTEGER CHECK (ics_score BETWEEN 0 AND 100),
  ob_detected     BOOLEAN DEFAULT FALSE,
  ob_type         VARCHAR(50),
  sweep_detected  BOOLEAN DEFAULT FALSE,
  sweep_type      VARCHAR(50),
  crt_detected    BOOLEAN DEFAULT FALSE,
  crt_pattern     VARCHAR(50),
  inst_detected   BOOLEAN DEFAULT FALSE,
  inst_type       VARCHAR(100),
  inst_confidence INTEGER,
  strategies      TEXT[],
  status          VARCHAR(20) DEFAULT 'active' CHECK (status IN ('active','triggered','closed','expired')),
  plan_required   VARCHAR(20) DEFAULT 'demo',
  generated_at    TIMESTAMPTZ DEFAULT NOW(),
  expires_at      TIMESTAMPTZ DEFAULT (NOW() + INTERVAL '6 hours')
);

CREATE INDEX idx_signals_pair ON signals(pair, generated_at DESC);
CREATE INDEX idx_signals_ics  ON signals(ics_score DESC);
CREATE INDEX idx_signals_inst ON signals(inst_detected) WHERE inst_detected=TRUE;

-- ── INSTITUTIONAL ZONES ──────────────────────────────────────────
CREATE TABLE institutional_zones (
  id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  pair          VARCHAR(20) NOT NULL,
  zone_type     VARCHAR(50) NOT NULL,  -- 'Order Block', 'FVG', 'Sweep Level', 'Displacement'
  direction     VARCHAR(10),
  price_level   NUMERIC(20,8),
  price_low     NUMERIC(20,8),
  price_high    NUMERIC(20,8),
  confidence    INTEGER,
  is_mitigated  BOOLEAN DEFAULT FALSE,
  detected_at   TIMESTAMPTZ DEFAULT NOW(),
  mitigated_at  TIMESTAMPTZ,
  signal_id     UUID REFERENCES signals(id)
);

CREATE INDEX idx_inst_zones_pair ON institutional_zones(pair, detected_at DESC);
CREATE INDEX idx_inst_zones_mitigated ON institutional_zones(is_mitigated) WHERE is_mitigated=FALSE;

-- ── ENGINE RUNS ──────────────────────────────────────────────────
CREATE TABLE engine_runs (
  id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  run_at          TIMESTAMPTZ DEFAULT NOW(),
  signals_created INTEGER DEFAULT 0,
  inst_zones      INTEGER DEFAULT 0,
  pairs_processed TEXT[],
  duration_ms     INTEGER,
  status          VARCHAR(20) DEFAULT 'completed',
  errors          TEXT[]
);

-- ── SIGNAL ENGINE FUNCTION ────────────────────────────────────────
-- Called by pg_cron every 6 hours
CREATE OR REPLACE FUNCTION run_signal_engine()
RETURNS VOID AS $$
DECLARE
  v_run_id UUID;
  v_start  TIMESTAMPTZ := NOW();
  v_count  INTEGER := 0;
  v_pairs  TEXT[] := ARRAY['BTCUSDT','ETHUSDT','SOLUSDT','XRPUSDT','BNBUSDT','LINKUSDT'];
BEGIN
  INSERT INTO engine_runs(pairs_processed, status) VALUES(v_pairs,'running') RETURNING id INTO v_run_id;

  -- Expire old signals
  UPDATE signals SET status='expired'
  WHERE status='active' AND expires_at < NOW();

  -- Expire old inst zones
  UPDATE institutional_zones SET is_mitigated=TRUE, mitigated_at=NOW()
  WHERE is_mitigated=FALSE AND detected_at < NOW() - INTERVAL '12 hours';

  -- NOTE: Actual market data fetch & signal computation runs in the
  -- application layer (Python/Node). This function manages the DB side.
  -- Application calls insert_signal() and insert_inst_zone() for each result.

  UPDATE engine_runs SET
    status='completed',
    duration_ms = EXTRACT(MILLISECONDS FROM (NOW()-v_start))::INTEGER
  WHERE id=v_run_id;

  -- Notify application layer
  PERFORM pg_notify('engine_complete', json_build_object('run_id',v_run_id,'at',NOW())::TEXT);
END;
$$ LANGUAGE plpgsql;

-- ── SCHEDULE ENGINE WITH pg_cron ─────────────────────────────────
-- Runs every 6 hours: 00:00, 06:00, 12:00, 18:00 WAT (Africa/Lagos = UTC+1)
-- In UTC: 23:00, 05:00, 11:00, 17:00
SELECT cron.schedule(
  'isc-signal-engine-6h',
  '0 23,5,11,17 * * *',   -- UTC offsets for WAT (UTC+1) sessions
  $$SELECT run_signal_engine()$$
);

-- Also expire stale signals every hour
SELECT cron.schedule(
  'isc-expire-signals',
  '0 * * * *',
  $$UPDATE signals SET status='expired' WHERE status='active' AND expires_at < NOW()$$
);

-- ── ROW LEVEL SECURITY ───────────────────────────────────────────
ALTER TABLE users           ENABLE ROW LEVEL SECURITY;
ALTER TABLE subscriptions   ENABLE ROW LEVEL SECURITY;
ALTER TABLE payments        ENABLE ROW LEVEL SECURITY;
ALTER TABLE activation_codes ENABLE ROW LEVEL SECURITY;

-- Users can only see their own data
CREATE POLICY users_self ON users FOR ALL USING (id = current_setting('app.user_id')::UUID);
CREATE POLICY subs_self  ON subscriptions FOR SELECT USING (user_id = current_setting('app.user_id')::UUID);
CREATE POLICY pay_self   ON payments FOR SELECT USING (user_email = current_setting('app.user_email'));
CREATE POLICY codes_self ON activation_codes FOR SELECT USING (user_email = current_setting('app.user_email') OR user_email IS NULL);

-- Signals are public (filtered by plan in app layer)
-- Admin bypasses RLS (set role = admin in session)

-- ── VIEWS ─────────────────────────────────────────────────────────
CREATE VIEW v_active_signals AS
  SELECT s.*, mp.price as current_price
  FROM signals s
  LEFT JOIN LATERAL (
    SELECT price FROM market_prices WHERE symbol=s.pair ORDER BY fetched_at DESC LIMIT 1
  ) mp ON TRUE
  WHERE s.status='active'
  ORDER BY s.ics_score DESC, s.generated_at DESC;

CREATE VIEW v_dashboard_stats AS
  SELECT
    (SELECT COUNT(*) FROM signals WHERE status='active') AS active_signals,
    (SELECT ROUND(AVG(ics_score)) FROM signals WHERE status='active') AS avg_ics,
    (SELECT COUNT(*) FROM users WHERE plan != 'demo') AS paid_users,
    (SELECT COUNT(*) FROM payments WHERE status='pending') AS pending_payments,
    (SELECT COALESCE(SUM(amount_usd),0) FROM payments WHERE status='verified') AS total_revenue,
    (SELECT COUNT(*) FROM institutional_zones WHERE is_mitigated=FALSE) AS active_inst_zones;

-- ═══════════════════════════════════════════════════════════════
-- END SCHEMA
-- ═══════════════════════════════════════════════════════════════
