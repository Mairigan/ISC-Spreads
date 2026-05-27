# ═══════════════════════════════════════════════════════════════
# ISC SPREADS — PRODUCTION BACKEND API
# FastAPI + PostgreSQL + Binance API + Email + Signal Engine
# ═══════════════════════════════════════════════════════════════

# requirements.txt:
# fastapi==0.111.0
# uvicorn[standard]==0.29.0
# asyncpg==0.29.0
# httpx==0.27.0
# python-jose[cryptography]==3.3.0
# passlib[bcrypt]==1.7.4
# python-dotenv==1.0.1
# resend==0.7.0
# apscheduler==3.10.4
# numpy==1.26.4
# pydantic==2.7.0
# pydantic-settings==2.2.1

import os, math, random, string, json, asyncio, logging
from datetime import datetime, timedelta, timezone
from typing import Optional, List
from contextlib import asynccontextmanager

import httpx
import asyncpg
import numpy as np
import resend
import uvicorn

from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from pydantic_settings import BaseSettings
from passlib.context import CryptContext
from jose import JWTError, jwt
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ── CONFIG ────────────────────────────────────────────────────────
class Settings(BaseSettings):
    DATABASE_URL:    str = "postgresql://user:pass@localhost:5432/iscspreads"
    SECRET_KEY:      str = "isc-spreads-super-secret-jwt-key-change-in-prod"
    ALGORITHM:       str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days
    RESEND_API_KEY:  str = ""
    FROM_EMAIL:      str = "noreply@iscspreads.io"
    ADMIN_EMAIL:     str = "samuelmaigari0@gmail.com"
    WALLET_ADDRESS:  str = "0x3fec94ae357d5f4921f04a7a7e335dd21df74330"
    BINANCE_BASE:    str = "https://api.binance.com/api/v3"
    TIMEZONE:        str = "Africa/Lagos"  # WAT UTC+1

    class Config:
        env_file = ".env"

settings = Settings()
pwd_ctx  = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer   = HTTPBearer()
resend.api_key = settings.RESEND_API_KEY

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("isc-spreads")

PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "LINKUSDT"]

PLAN_STRATEGIES = {
    "demo":    ["ICT Killzone", "FVG Mapping", "6H Structure"],
    "basic":   ["ICT Killzone", "FVG Mapping", "6H Structure", "SMC (ChoCH/BoS)", "Liquidity Sweep", "RSI Divergence"],
    "premium": ["ICT Killzone", "FVG Mapping", "6H Structure", "SMC (ChoCH/BoS)", "Liquidity Sweep",
                "RSI Divergence", "CRT Movement", "Volume CVD", "Order Block Tracker",
                "Institutional ICS 50-74", "1H+15M Scalp"],
    "elite":   ["ALL_STRATEGIES"],
}

PLAN_ICS_THRESHOLD = {"demo": 99, "basic": 65, "premium": 50, "elite": 50}  # demo sees only preview
PLAN_PRICES        = {"basic": 49, "premium": 99, "elite": 149}

# ── DB POOL ───────────────────────────────────────────────────────
db_pool: asyncpg.Pool = None

async def get_db() -> asyncpg.Connection:
    async with db_pool.acquire() as conn:
        yield conn

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
    db_pool = await asyncpg.create_pool(settings.DATABASE_URL, min_size=2, max_size=10)
    log.info("DB pool created")
    scheduler.start()
    log.info("APScheduler started")
    yield
    scheduler.shutdown()
    await db_pool.close()

app = FastAPI(title="ISC Spreads API", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── SCHEDULER ─────────────────────────────────────────────────────
scheduler = AsyncIOScheduler(timezone="Africa/Lagos")

@scheduler.scheduled_job(CronTrigger(hour="0,6,12,18", minute=0, timezone="Africa/Lagos"))
async def scheduled_signal_engine():
    log.info("⚡ Signal engine triggered by 6H scheduler (WAT)")
    await run_signal_engine()

# ── AUTH HELPERS ──────────────────────────────────────────────────
def hash_password(pw: str) -> str:
    return pwd_ctx.hash(pw)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_ctx.verify(plain, hashed)

def create_token(data: dict) -> str:
    payload = {**data, "exp": datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

async def current_user(
    creds: HTTPAuthorizationCredentials = Depends(bearer),
    db: asyncpg.Connection = Depends(get_db)
):
    try:
        payload = jwt.decode(creds.credentials, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        user_id = payload.get("sub")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")
    row = await db.fetchrow("SELECT * FROM users WHERE id=$1 AND is_active=TRUE", user_id)
    if not row:
        raise HTTPException(status_code=401, detail="User not found")
    return dict(row)

async def admin_user(user=Depends(current_user), db: asyncpg.Connection = Depends(get_db)):
    role = await db.fetchval("SELECT role FROM user_roles WHERE user_id=$1", user["id"])
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user

# ── PYDANTIC MODELS ───────────────────────────────────────────────
class RegisterReq(BaseModel):
    name:     str
    email:    EmailStr
    password: str

class LoginReq(BaseModel):
    email:    EmailStr
    password: str

class PaymentSubmitReq(BaseModel):
    plan:     str
    email:    EmailStr
    tx_hash:  str
    amount:   float

class ActivateCodeReq(BaseModel):
    code: str

class GenerateCodeReq(BaseModel):
    plan:         str
    email:        Optional[EmailStr] = None
    validity_days: int = 30

class UpdateWalletReq(BaseModel):
    address: str
    network: str = "ERC-20"

class ApprovePaymentReq(BaseModel):
    payment_id: str

# ═══════════════════════════════════════════════════════════════
# SIGNAL ENGINE — BINANCE API + COMPUTE
# ═══════════════════════════════════════════════════════════════

async def fetch_klines(symbol: str, interval: str = "6h", limit: int = 20) -> list:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{settings.BINANCE_BASE}/klines",
                             params={"symbol": symbol, "interval": interval, "limit": limit})
        r.raise_for_status()
        return r.json()

async def fetch_ticker(symbol: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{settings.BINANCE_BASE}/ticker/24hr", params={"symbol": symbol})
        r.raise_for_status()
        return r.json()

def compute_rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes)
    gains  = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    rs = avg_gain / (avg_loss + 1e-9)
    return round(100 - (100 / (1 + rs)), 2)

def compute_volume_ratio(volumes: list) -> float:
    if len(volumes) < 2:
        return 1.0
    avg = np.mean(volumes[:-1])
    return round(volumes[-1] / (avg + 1e-9), 3)

def detect_order_block(klines: list) -> dict:
    """Detect last opposing candle before displacement move."""
    if len(klines) < 4:
        return {"detected": False, "type": None, "quality": 0}
    bodies = []
    for k in klines[-4:]:
        o, h, l, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
        bodies.append({"o": o, "h": h, "l": l, "c": c, "body": abs(c - o), "range": h - l})
    last = bodies[-1]
    prev = bodies[-2]
    displacement = last["body"] / (last["range"] + 1e-9)
    if displacement > 0.65 and prev["body"] > 0:
        ob_type = "Bullish OB" if last["c"] > last["o"] else "Bearish OB"
        quality = min(int(displacement * 35), 30)
        return {"detected": True, "type": ob_type, "quality": quality,
                "level": (prev["h"] + prev["l"]) / 2}
    return {"detected": False, "type": None, "quality": 0, "level": None}

def detect_liquidity_sweep(klines: list, vol_ratio: float) -> dict:
    """Equal highs/lows sweep detection."""
    if len(klines) < 6:
        return {"detected": False, "type": None}
    highs = [float(k[2]) for k in klines[-6:]]
    lows  = [float(k[3]) for k in klines[-6:]]
    last_high, last_low = highs[-1], lows[-1]
    prev_highs = highs[:-1]
    prev_lows  = lows[:-1]
    tolerance = 0.002  # 0.2%
    swept_high = any(abs(last_high - h) / h < tolerance for h in prev_highs[-3:])
    swept_low  = any(abs(last_low - l)  / l < tolerance for l in prev_lows[-3:])
    if (swept_high or swept_low) and vol_ratio > 1.3:
        s_type = "EQH Sweep → Short" if swept_high else "EQL Sweep → Long"
        return {"detected": True, "type": s_type, "ratio": vol_ratio}
    return {"detected": False, "type": None, "ratio": vol_ratio}

def detect_crt(klines: list) -> dict:
    """Candle Range Theory — detect manipulation + expansion."""
    if len(klines) < 3:
        return {"detected": False, "pattern": None}
    last = klines[-1]
    o, h, l, c = float(last[1]), float(last[2]), float(last[3]), float(last[4])
    total_range = h - l
    if total_range == 0:
        return {"detected": False, "pattern": None}
    upper_wick = (h - max(o, c)) / total_range
    lower_wick = (min(o, c) - l) / total_range
    if upper_wick > 0.55:
        return {"detected": True, "pattern": "Bearish CRT — Rejection"}
    if lower_wick > 0.55:
        return {"detected": True, "pattern": "Bullish CRT — Spring"}
    return {"detected": False, "pattern": None}

def detect_fvg(klines: list) -> dict:
    """Fair Value Gap — 3-candle imbalance."""
    if len(klines) < 3:
        return {"detected": False, "type": None}
    k1 = klines[-3]; k2 = klines[-2]; k3 = klines[-1]
    h1, l1 = float(k1[2]), float(k1[3])
    h3, l3 = float(k3[2]), float(k3[3])
    if l3 > h1:  # Bullish FVG
        return {"detected": True, "type": "Bullish FVG", "gap_low": h1, "gap_high": l3}
    if h3 < l1:  # Bearish FVG
        return {"detected": True, "type": "Bearish FVG", "gap_low": h3, "gap_high": l1}
    return {"detected": False, "type": None}

def compute_ics(ob: dict, sweep: dict, vol_ratio: float, rsi: float,
                fvg: dict, crt: dict, structure_aligned: bool) -> int:
    """Institutional Confidence Score 0–100."""
    score = 0
    if sweep["detected"]:         score += 25
    if ob["detected"]:            score += 20
    if structure_aligned:         score += 15
    if fvg["detected"]:           score += 15
    if vol_ratio > 1.5:           score += 15
    elif vol_ratio > 1.2:         score += 8
    if rsi < 35 or rsi > 65:      score += 10
    if crt["detected"]:           score += 5
    return min(score, 100)

def determine_direction(rsi: float, sweep: dict, ob: dict) -> str:
    if sweep["detected"] and sweep.get("type", ""):
        if "Long" in sweep["type"]: return "LONG"
        if "Short" in sweep["type"]: return "SHORT"
    if ob["detected"] and ob.get("type", ""):
        if "Bullish" in ob["type"]: return "LONG"
        if "Bearish" in ob["type"]: return "SHORT"
    return "LONG" if rsi < 50 else "SHORT"

def get_plan_required(ics: int, inst_detected: bool) -> str:
    if inst_detected and ics >= 75: return "elite"
    if ics >= 65: return "premium"
    if ics >= 50: return "basic"
    return "demo"

async def process_pair(symbol: str) -> Optional[dict]:
    """Full signal computation for one pair from Binance live data."""
    try:
        klines_6h = await fetch_klines(symbol, "6h", 20)
        klines_1h  = await fetch_klines(symbol, "1h", 5)
        ticker     = await fetch_ticker(symbol)

        closes_6h = [float(k[4]) for k in klines_6h]
        volumes   = [float(k[5]) for k in klines_6h]
        price     = float(ticker["lastPrice"])
        high_24h  = float(ticker["highPrice"])
        low_24h   = float(ticker["lowPrice"])
        vol_24h   = float(ticker["volume"])

        rsi        = compute_rsi(closes_6h)
        vol_ratio  = compute_volume_ratio(volumes)
        vol_spike  = vol_ratio > 1.5

        ob         = detect_order_block(klines_6h)
        sweep      = detect_liquidity_sweep(klines_6h, vol_ratio)
        crt        = detect_crt(klines_6h)
        fvg        = detect_fvg(klines_6h)

        # Structure alignment (6H vs 1H)
        closes_1h  = [float(k[4]) for k in klines_1h]
        structure_aligned = (closes_1h[-1] > closes_1h[0]) == (closes_6h[-1] > closes_6h[-5])

        ics        = compute_ics(ob, sweep, vol_ratio, rsi, fvg, crt, structure_aligned)
        direction  = determine_direction(rsi, sweep, ob)

        inst_conf  = (35 if ob["detected"] else 0) + (30 if sweep["detected"] else 0) + \
                     (20 if vol_spike else 0) + (15 if vol_ratio > 2 else 0)
        inst = {"detected": inst_conf > 40, "confidence": inst_conf,
                "type": "High-Conviction Inst. Zone" if inst_conf > 70 else
                        "Institutional Interest" if inst_conf > 40 else "None"}

        if ics < 50:
            return None  # discard low-confidence signals

        sl_pct  = 0.015  # 1.5% stop
        tp1_pct = 0.045  # 4.5% TP1 = 1:3
        tp2_pct = 0.090  # 9.0% TP2 = 1:6

        sl  = price * (1 - sl_pct) if direction == "LONG" else price * (1 + sl_pct)
        tp1 = price * (1 + tp1_pct) if direction == "LONG" else price * (1 - tp1_pct)
        tp2 = price * (1 + tp2_pct) if direction == "LONG" else price * (1 - tp2_pct)

        strategies = ["ICT Killzone", "6H Structure"]
        if ob["detected"]:     strategies.append("Order Block")
        if sweep["detected"]:  strategies.append("Liquidity Sweep")
        if crt["detected"]:    strategies.append("CRT")
        if fvg["detected"]:    strategies.append("FVG")
        if inst["detected"]:   strategies.append("Institutional Zone")

        return {
            "pair":            symbol,
            "direction":       direction,
            "timeframe":       "6H",
            "entry_price":     round(price, 8),
            "stop_loss":       round(sl, 8),
            "take_profit_1":   round(tp1, 8),
            "take_profit_2":   round(tp2, 8),
            "rr_ratio":        "1:3",
            "rsi":             rsi,
            "volume_ratio":    vol_ratio,
            "ics_score":       ics,
            "ob_detected":     ob["detected"],
            "ob_type":         ob.get("type"),
            "sweep_detected":  sweep["detected"],
            "sweep_type":      sweep.get("type"),
            "crt_detected":    crt["detected"],
            "crt_pattern":     crt.get("pattern"),
            "inst_detected":   inst["detected"],
            "inst_type":       inst["type"],
            "inst_confidence": inst["confidence"],
            "strategies":      strategies,
            "plan_required":   get_plan_required(ics, inst["detected"]),
            # Market data for storage
            "_ticker": {
                "symbol": symbol, "price": price, "high_24h": high_24h,
                "low_24h": low_24h, "volume_24h": vol_24h,
                "pct_change": float(ticker["priceChangePercent"]),
                "rsi_6h": rsi, "volume_ratio": vol_ratio,
            },
            "_inst_zone": {
                "zone_type": ob.get("type") or fvg.get("type") or "OB",
                "direction": direction,
                "price_level": round(ob.get("level") or price, 8),
                "confidence": inst["confidence"],
            } if inst["detected"] else None
        }
    except Exception as e:
        log.error(f"Error processing {symbol}: {e}")
        return None

async def run_signal_engine():
    """Full signal engine run — called by scheduler and manual trigger."""
    log.info(f"🔁 Signal engine running at {datetime.now()} WAT")
    results = await asyncio.gather(*[process_pair(p) for p in PAIRS])
    signals = [r for r in results if r is not None]

    async with db_pool.acquire() as db:
        # Record engine run
        run_id = await db.fetchval(
            "INSERT INTO engine_runs(pairs_processed, signals_created, status) VALUES($1,$2,$3) RETURNING id",
            PAIRS, len(signals), "running"
        )
        # Expire old
        await db.execute("UPDATE signals SET status='expired' WHERE status='active' AND expires_at < NOW()")

        for sig in signals:
            ticker = sig.pop("_ticker")
            izone  = sig.pop("_inst_zone")

            # Store market price
            await db.execute("""
                INSERT INTO market_prices(symbol,price,high_24h,low_24h,volume_24h,pct_change,rsi_6h,volume_ratio)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8)
            """, ticker["symbol"], ticker["price"], ticker["high_24h"], ticker["low_24h"],
                ticker["volume_24h"], ticker["pct_change"], ticker["rsi_6h"], ticker["volume_ratio"])

            # Store signal
            sig_id = await db.fetchval("""
                INSERT INTO signals(pair,direction,timeframe,entry_price,stop_loss,take_profit_1,
                  take_profit_2,rr_ratio,rsi,volume_ratio,ics_score,ob_detected,ob_type,
                  sweep_detected,sweep_type,crt_detected,crt_pattern,inst_detected,
                  inst_type,inst_confidence,strategies,plan_required)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22)
                RETURNING id
            """, sig["pair"], sig["direction"], sig["timeframe"], sig["entry_price"],
                sig["stop_loss"], sig["take_profit_1"], sig["take_profit_2"], sig["rr_ratio"],
                sig["rsi"], sig["volume_ratio"], sig["ics_score"], sig["ob_detected"], sig["ob_type"],
                sig["sweep_detected"], sig["sweep_type"], sig["crt_detected"], sig["crt_pattern"],
                sig["inst_detected"], sig["inst_type"], sig["inst_confidence"], sig["strategies"],
                sig["plan_required"])

            # Store institutional zone
            if izone:
                await db.execute("""
                    INSERT INTO institutional_zones(pair,zone_type,direction,price_level,confidence,signal_id)
                    VALUES($1,$2,$3,$4,$5,$6)
                """, sig["pair"], izone["zone_type"], izone["direction"],
                    izone["price_level"], izone["confidence"], sig_id)

        await db.execute(
            "UPDATE engine_runs SET status='completed', signals_created=$1 WHERE id=$2",
            len(signals), run_id
        )
    log.info(f"✅ Engine run complete. {len(signals)} signals generated.")
    return signals

# ═══════════════════════════════════════════════════════════════
# EMAIL SERVICE
# ═══════════════════════════════════════════════════════════════

async def send_activation_email(to_email: str, code: str, plan: str):
    """Send activation code email via Resend."""
    plan_upper = plan.capitalize()
    html = f"""
    <div style="font-family:sans-serif;max-width:500px;margin:0 auto;background:#0B0D14;color:#E4E6EF;padding:32px;border-radius:12px;border:1px solid rgba(201,168,76,.3)">
      <div style="text-align:center;margin-bottom:24px">
        <div style="font-size:13px;letter-spacing:2px;color:#C9A84C;font-family:monospace">ISC SPREADS</div>
        <div style="font-size:22px;font-weight:700;margin-top:4px">Plan Activation Code</div>
      </div>
      <div style="background:#161924;border:1px solid rgba(201,168,76,.2);border-radius:8px;padding:20px;text-align:center;margin-bottom:20px">
        <div style="font-size:11px;color:#6E7280;font-family:monospace;letter-spacing:2px;margin-bottom:8px">YOUR {plan_upper.upper()} PLAN CODE</div>
        <div style="font-size:26px;font-family:monospace;letter-spacing:4px;color:#C9A84C;font-weight:700">{code}</div>
      </div>
      <p style="color:#6E7280;font-size:13px;line-height:1.7">Your <strong style="color:#C9A84C">{plan_upper}</strong> plan has been verified and is ready to activate. Enter this code in the ISC Spreads platform under <em>Activate Code</em>.</p>
      <ul style="color:#6E7280;font-size:13px;line-height:1.9">
        <li>Code expires in <strong style="color:#E4E6EF">48 hours</strong></li>
        <li>Single-use — do not share</li>
        <li>Enter exactly as shown</li>
      </ul>
      <p style="color:#6E7280;font-size:12px;margin-top:20px">Questions? Email <a href="mailto:support@iscspreads.io" style="color:#C9A84C">support@iscspreads.io</a></p>
      <div style="border-top:1px solid rgba(255,255,255,.06);margin-top:20px;padding-top:14px;font-size:11px;color:#3E404E;text-align:center;font-family:monospace">ISC SPREADS · INSTITUTIONAL SIGNAL CONFLUENCE<br>This email is confidential. Do not forward.</div>
    </div>
    """
    try:
        resend.Emails.send({
            "from": f"ISC Spreads <{settings.FROM_EMAIL}>",
            "to": [to_email],
            "subject": f"[ISC Spreads] Your {plan_upper} Activation Code",
            "html": html
        })
        log.info(f"📧 Activation email sent to {to_email}")
    except Exception as e:
        log.error(f"Email send failed: {e}")

async def send_payment_received_email(to_email: str, plan: str, amount: float):
    """Notify user their payment was received and is under review."""
    try:
        resend.Emails.send({
            "from": f"ISC Spreads <{settings.FROM_EMAIL}>",
            "to": [to_email],
            "subject": "[ISC Spreads] Payment Received — Verification in Progress",
            "html": f"""<div style="font-family:sans-serif;max-width:500px;margin:0 auto;padding:32px;background:#0B0D14;color:#E4E6EF;border-radius:12px;border:1px solid rgba(201,168,76,.2)">
              <div style="font-size:13px;letter-spacing:2px;color:#C9A84C;font-family:monospace;text-align:center;margin-bottom:16px">ISC SPREADS</div>
              <h2 style="margin-bottom:12px">Payment Received ✓</h2>
              <p style="color:#6E7280;line-height:1.7">We've received your payment submission for the <strong style="color:#C9A84C">{plan.capitalize()} Plan</strong> (${amount}).<br><br>Our team will verify your transaction within <strong style="color:#E4E6EF">30 minutes</strong>. Once verified, your activation code will be sent to this email automatically.</p>
              <p style="color:#6E7280;font-size:12px;margin-top:20px">Support: <a href="mailto:support@iscspreads.io" style="color:#C9A84C">support@iscspreads.io</a></p>
            </div>"""
        })
    except Exception as e:
        log.error(f"Payment email failed: {e}")

# ═══════════════════════════════════════════════════════════════
# API ROUTES
# ═══════════════════════════════════════════════════════════════

# ── AUTH ──────────────────────────────────────────────────────────
@app.post("/auth/register")
async def register(req: RegisterReq, db: asyncpg.Connection = Depends(get_db)):
    if await db.fetchrow("SELECT id FROM users WHERE email=$1", req.email.lower()):
        raise HTTPException(400, "Email already registered")
    if len(req.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    plan = "elite" if req.email.lower() == settings.ADMIN_EMAIL else "demo"
    uid = await db.fetchval("""
        INSERT INTO users(email,name,password_hash,plan)
        VALUES($1,$2,$3,$4) RETURNING id
    """, req.email.lower(), req.name, hash_password(req.password), plan)
    role = await db.fetchval("SELECT role FROM user_roles WHERE user_id=$1", uid)
    token = create_token({"sub": str(uid), "email": req.email.lower(), "role": role})
    return {"token": token, "user": {"id": str(uid), "name": req.name, "email": req.email.lower(), "plan": plan, "role": role}}

@app.post("/auth/login")
async def login(req: LoginReq, db: asyncpg.Connection = Depends(get_db)):
    user = await db.fetchrow("SELECT * FROM users WHERE email=$1 AND is_active=TRUE", req.email.lower())
    if not user or not verify_password(req.password, user["password_hash"]):
        raise HTTPException(401, "Invalid credentials")
    role = await db.fetchval("SELECT role FROM user_roles WHERE user_id=$1", user["id"]) or "user"
    token = create_token({"sub": str(user["id"]), "email": user["email"], "role": role})
    return {"token": token, "user": {"id": str(user["id"]), "name": user["name"], "email": user["email"], "plan": user["plan"], "role": role}}

@app.get("/auth/me")
async def me(user=Depends(current_user), db: asyncpg.Connection = Depends(get_db)):
    role = await db.fetchval("SELECT role FROM user_roles WHERE user_id=$1", user["id"]) or "user"
    sub  = await db.fetchrow("SELECT * FROM subscriptions WHERE user_id=$1 AND is_active=TRUE", user["id"])
    return {**{k: user[k] for k in ["id","name","email","plan"]}, "role": role,
            "subscription": dict(sub) if sub else None}

# ── MARKET DATA ───────────────────────────────────────────────────
@app.get("/market/prices")
async def get_prices():
    async with httpx.AsyncClient(timeout=10) as client:
        tasks = [client.get(f"{settings.BINANCE_BASE}/ticker/24hr", params={"symbol": p}) for p in PAIRS]
        resps = await asyncio.gather(*tasks, return_exceptions=True)
    data = []
    for r in resps:
        if isinstance(r, Exception): continue
        try: data.append(r.json())
        except: pass
    return data

# ── SIGNALS ───────────────────────────────────────────────────────
@app.get("/signals")
async def get_signals(user=Depends(current_user), db: asyncpg.Connection = Depends(get_db)):
    plan = user["plan"]
    threshold = PLAN_ICS_THRESHOLD.get(plan, 99)
    rows = await db.fetch("""
        SELECT * FROM signals WHERE status='active' AND ics_score >= $1
        ORDER BY ics_score DESC, generated_at DESC LIMIT 50
    """, threshold)
    return [dict(r) for r in rows]

@app.post("/signals/run")
async def manual_run(bg: BackgroundTasks, user=Depends(admin_user)):
    bg.add_task(run_signal_engine)
    return {"message": "Signal engine triggered"}

# ── INSTITUTIONAL ─────────────────────────────────────────────────
@app.get("/institutional/zones")
async def get_inst_zones(user=Depends(current_user), db: asyncpg.Connection = Depends(get_db)):
    plan = user["plan"]
    if plan not in ["premium", "elite"] and user.get("role") != "admin":
        raise HTTPException(403, "Premium or Elite plan required for institutional zone data")
    rows = await db.fetch("""
        SELECT * FROM institutional_zones WHERE is_mitigated=FALSE
        ORDER BY confidence DESC, detected_at DESC LIMIT 30
    """)
    return [dict(r) for r in rows]

# ── PAYMENTS ──────────────────────────────────────────────────────
@app.post("/payments/submit")
async def submit_payment(
    req: PaymentSubmitReq,
    bg: BackgroundTasks,
    user=Depends(current_user),
    db: asyncpg.Connection = Depends(get_db)
):
    if req.plan not in PLAN_PRICES:
        raise HTTPException(400, "Invalid plan")
    wallet = await db.fetchval("SELECT address FROM wallet_config WHERE is_active=TRUE LIMIT 1")
    pmt_id = await db.fetchval("""
        INSERT INTO payments(user_email,plan,amount_usd,tx_hash,wallet_addr,status)
        VALUES($1,$2,$3,$4,$5,'pending') RETURNING id
    """, req.email.lower(), req.plan, req.amount, req.tx_hash, wallet)
    bg.add_task(send_payment_received_email, req.email.lower(), req.plan, req.amount)
    return {"message": "Payment submitted. Activation code will be sent to your email.", "payment_id": str(pmt_id)}

@app.get("/payments/pending")
async def pending_payments(user=Depends(admin_user), db: asyncpg.Connection = Depends(get_db)):
    rows = await db.fetch("SELECT * FROM payments WHERE status='pending' ORDER BY submitted_at DESC")
    return [dict(r) for r in rows]

@app.post("/payments/approve")
async def approve_payment(
    req: ApprovePaymentReq,
    bg: BackgroundTasks,
    user=Depends(admin_user),
    db: asyncpg.Connection = Depends(get_db)
):
    pmt = await db.fetchrow("SELECT * FROM payments WHERE id=$1 AND status='pending'", req.payment_id)
    if not pmt:
        raise HTTPException(404, "Payment not found or already processed")
    code = await db.fetchval(
        "SELECT generate_activation_code($1,$2,$3,$4)",
        pmt["plan"], pmt["user_email"], str(user["id"]), str(pmt["id"])
    )
    await db.execute("""
        UPDATE payments SET status='verified', verified_at=NOW(), verified_by=$1, activation_code_sent=TRUE
        WHERE id=$2
    """, user["id"], pmt["id"])
    bg.add_task(send_activation_email, pmt["user_email"], code, pmt["plan"])
    return {"message": f"Payment approved. Code sent to {pmt['user_email']}", "code": code}

# ── ACTIVATION ────────────────────────────────────────────────────
@app.post("/activate")
async def activate(req: ActivateCodeReq, user=Depends(current_user), db: asyncpg.Connection = Depends(get_db)):
    result = await db.fetchval(
        "SELECT activate_plan($1,$2,$3)",
        req.code, str(user["id"]), user["email"]
    )
    res = json.loads(result)
    if not res.get("success"):
        raise HTTPException(400, res.get("error", "Invalid code"))
    return {"message": f"{res['plan'].capitalize()} plan activated successfully!", "plan": res["plan"]}

# ── ADMIN — CODE GENERATION ───────────────────────────────────────
@app.post("/admin/codes/generate")
async def gen_code(
    req: GenerateCodeReq,
    bg: BackgroundTasks,
    user=Depends(admin_user),
    db: asyncpg.Connection = Depends(get_db)
):
    if req.plan not in ["basic","premium","elite"]:
        raise HTTPException(400, "Invalid plan")
    code = await db.fetchval(
        "SELECT generate_activation_code($1,$2,$3)",
        req.plan, req.email, str(user["id"])
    )
    if req.email:
        bg.add_task(send_activation_email, req.email, code, req.plan)
    return {"code": code, "plan": req.plan, "email": req.email}

@app.get("/admin/codes")
async def list_codes(user=Depends(admin_user), db: asyncpg.Connection = Depends(get_db)):
    rows = await db.fetch("SELECT * FROM activation_codes ORDER BY created_at DESC LIMIT 100")
    return [dict(r) for r in rows]

# ── ADMIN — USERS ─────────────────────────────────────────────────
@app.get("/admin/users")
async def list_users(user=Depends(admin_user), db: asyncpg.Connection = Depends(get_db)):
    rows = await db.fetch("""
        SELECT u.*, ur.role FROM users u
        LEFT JOIN user_roles ur ON ur.user_id=u.id
        ORDER BY u.created_at DESC
    """)
    return [dict(r) for r in rows]

@app.put("/admin/users/{user_id}/plan")
async def update_user_plan(user_id: str, plan: str, user=Depends(admin_user), db: asyncpg.Connection = Depends(get_db)):
    if plan not in ["demo","basic","premium","elite"]:
        raise HTTPException(400, "Invalid plan")
    await db.execute("UPDATE users SET plan=$1, updated_at=NOW() WHERE id=$2", plan, user_id)
    return {"message": "Plan updated"}

@app.delete("/admin/users/{user_id}")
async def revoke_user(user_id: str, user=Depends(admin_user), db: asyncpg.Connection = Depends(get_db)):
    await db.execute("UPDATE users SET is_active=FALSE WHERE id=$1", user_id)
    return {"message": "User revoked"}

# ── ADMIN — WALLET ────────────────────────────────────────────────
@app.get("/admin/wallet")
async def get_wallet(user=Depends(admin_user), db: asyncpg.Connection = Depends(get_db)):
    row = await db.fetchrow("SELECT * FROM wallet_config WHERE is_active=TRUE LIMIT 1")
    return dict(row) if row else {}

@app.put("/admin/wallet")
async def update_wallet(req: UpdateWalletReq, user=Depends(admin_user), db: asyncpg.Connection = Depends(get_db)):
    await db.execute("UPDATE wallet_config SET is_active=FALSE")
    await db.execute("INSERT INTO wallet_config(address,network,is_active) VALUES($1,$2,TRUE)",
                     req.address, req.network)
    return {"message": "Wallet updated", "address": req.address}

@app.get("/wallet/active")
async def public_wallet(db: asyncpg.Connection = Depends(get_db)):
    addr = await db.fetchval("SELECT address FROM wallet_config WHERE is_active=TRUE LIMIT 1")
    return {"address": addr or settings.WALLET_ADDRESS}

# ── ADMIN — STATS ─────────────────────────────────────────────────
@app.get("/admin/stats")
async def admin_stats(user=Depends(admin_user), db: asyncpg.Connection = Depends(get_db)):
    row = await db.fetchrow("SELECT * FROM v_dashboard_stats")
    return dict(row) if row else {}

# ── HEALTH ────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "service": "ISC Spreads API", "time": datetime.utcnow().isoformat()}

# ── ENTRY ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False, workers=1)
