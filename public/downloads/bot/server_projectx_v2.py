"""
🎯 MES + MNQ TRADING BOT - PROJECTX + TRADOVATE API
3 MES + 3 MNQ contracts side-by-side → 1-1-1 STRATEGY
Entry: 3 MES + 3 MNQ → T1: -1 each → T2: -1 each → 1 runner each
MES: 11pt stop → 7pt after T1 → breakeven runner (entry-3) → auto-close +20
MNQ: 33pt stop → 33pt after T1 → breakeven runner (entry-21) → auto-close +60
LINKED: MES is catalyst — MES flat at broker = flatten everything
Direct ProjectX/Topstep API + Tradovate (personal funded, currently disabled)
"""

import json
import time
import hashlib
import requests
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta
import re
import os
import sys

# Rithmic client (loaded conditionally below)
try:
    from rithmic_client import RithmicBridge, load_config as rithmic_load_config
    RITHMIC_AVAILABLE = True
except ImportError:
    RITHMIC_AVAILABLE = False

# Cloud client (loaded conditionally — SaaS gating)
try:
    from cloud_client import CloudClient
    CLOUD_CLIENT_AVAILABLE = True
except ImportError:
    CLOUD_CLIENT_AVAILABLE = False
    CloudClient = None

# ==================== TRADE LOGGING SYSTEM ====================
TRADE_LOG_PATH = os.path.expanduser("~/Desktop/discord-trading-alerts/trades.jsonl")

# Apex account mapping for dashboard
APEX_ACCOUNT_NAMES = {
    "PA-APEX-373034-02": "APEX-02-PA",
    "PA-APEX-373034-03": "APEX-03-PA",
    "PA-APEX-373034-04": "APEX-04-PA",
    "PA-APEX-373034-05": "APEX-05-PA",
}

def log_trade(account_id, instrument, direction, entry_price, exit_price, 
              contracts, ladder_level, exit_type, position_key):
    """Log a completed trade."""
    multiplier = 2.0 if instrument == "MNQ" else 0.5
    points = (exit_price - entry_price) if direction == "long" else (entry_price - exit_price)
    gross_pnl = points * contracts * multiplier
    commission = contracts * 5.0  # $2.50/side * 2 sides
    net_pnl = gross_pnl - commission
    
    # Find account name - handle both Topstep numeric IDs and Apex string IDs
    if isinstance(account_id, str):  # Apex account
        acc_name = APEX_ACCOUNT_NAMES.get(account_id, account_id)
    else:  # Topstep numeric ID
        acc_name = f"Account-{account_id}"
        for acc in PROJECTX_ACCOUNTS:
            if acc["id"] == account_id:
                acc_name = acc.get("name", acc_name)
                break
    
    trade = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "account_id": account_id,
        "account_name": acc_name,
        "instrument": instrument,
        "direction": direction,
        "entry": round(entry_price, 2),
        "exit": round(exit_price, 2),
        "contracts": contracts,
        "level": ladder_level,
        "exit_type": exit_type,
        "points": round(points, 2),
        "pnl": round(net_pnl, 2),
        "position_key": position_key
    }
    
    try:
        with open(TRADE_LOG_PATH, "a") as f:
            f.write(json.dumps(trade) + "\n")
        logger.info(f"📊 Trade logged: {acc_name} {instrument} {ladder_level} {contracts}c @ {exit_price} = ${net_pnl:.2f}")
    except Exception as e:
        logger.error(f"Trade log write failed: {e}")

    # Report to cloud dashboard (non-blocking, fire-and-forget)
    _cloud_report_trade(trade)

def on_rithmic_fill(account_id, symbol, side, qty, price, basket_id):
    """Callback fired when Apex fill occurs. Log to trades.jsonl for dashboard.
    Added 2026-05-15 to track Apex P&L alongside Topstep.
    
    Callback signature from rithmic_client.py:
        (account_id, symbol, side, qty, price, basket_id)
    Where:
        account_id = "PA-APEX-373034-02" etc.
        symbol = "MESM6" or "MNQM6"
        side = "BUY" or "SELL"
        qty = contracts filled
        price = fill price
        basket_id = order basket ID
    """
    try:
        # Get active position to determine entry price and direction
        position = get_active_position()
        if not position:
            # No active position - likely an entry fill or orphaned fill
            logger.info(f"📊 Apex fill (no position): {account_id} {symbol} {side} {qty}@{price}")
            return
        
        # Determine instrument
        instrument = "MNQ" if "MNQ" in symbol else "MES"
        direction = position["direction"]
        
        # Check if this is an exit (opposite side of position)
        is_exit = (direction == "long" and side == "SELL") or (direction == "short" and side == "BUY")
        
        if not is_exit:
            # Entry fill - log for tracking but don't calculate P&L yet
            logger.info(f"📊 Apex ENTRY: {account_id} {instrument} {qty}@{price}")
            return
        
        # Exit fill - determine ladder level by matching price to targets
        ladder_targets = position.get("ladder_targets", {})
        leg_targets = ladder_targets.get("mnq" if instrument == "MNQ" else "mes", {})
        
        t1 = leg_targets.get("t1")
        t2 = leg_targets.get("t2")
        runner = leg_targets.get("runner")
        
        # Match price to level (with 0.75pt tolerance for slippage)
        level = "UNKNOWN"
        if t1 and abs(price - t1) <= 0.75:
            level = "T1"
        elif t2 and abs(price - t2) <= 0.75:
            level = "T2"
        elif runner and abs(price - runner) <= 0.75:
            level = "RUNNER"
        
        # Get entry price
        if instrument == "MNQ":
            entry_price = position.get("mnq_entry_price", 0)
        else:
            entry_price = position.get("mes_entry_price", 0)
        
        if entry_price == 0:
            entry_price = position.get("entry_price", 0)
        
        if entry_price == 0:
            logger.warning(f"⚠️ Apex fill but no entry price: {account_id} {instrument} {level} {qty}@{price}")
            return
        
        # Log the trade
        log_trade(
            account_id=account_id,  # string like "PA-APEX-373034-02"
            instrument=instrument,
            direction=direction,
            entry_price=entry_price,
            exit_price=price,
            contracts=qty,
            ladder_level=level,
            exit_type="tp_filled",
            position_key=position.get("key", "")
        )
        
    except Exception as e:
        logger.error(f"❌ on_rithmic_fill error: {e}")
        import traceback
        logger.error(traceback.format_exc())

# ==================== CONFIGURATION SYSTEM (Phase 1 MVP) ====================
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config_loader as _config_loader
from config_loader import get_accounts, get_leader_account, get_order_routing_accounts

# Initialize config (creates default ~/.config/proptradebot/config.json if missing)
_config_loader.init_config()
_cfg = _config_loader.config  # Access through module to avoid import-time None issue

# Bridge: expose config values as module-level constants for backward compatibility
# The rest of the 4,900+ line file uses these names unchanged

PORT = _cfg.server.port
LOG_FILE = _cfg.logging.log_file
TRADE_LOG_PATH = _cfg.logging.trade_log_file
POSITION_STATE_FILE = _cfg.logging.position_state_file
NOTIFICATION_FILE = _cfg.logging.notification_file

# ProjectX
PROJECTX_BASE_URL = _cfg.brokers.projectx.base_url
CREDENTIALS_FILE = _cfg.brokers.projectx.credentials_file
ACCOUNTS = _cfg.brokers.projectx.accounts

# Tradovate
TRADOVATE_ENABLED = _cfg.brokers.tradovate.enabled
TRADOVATE_DRY_RUN = _cfg.brokers.tradovate.dry_run
TRADOVATE_DEMO = _cfg.brokers.tradovate.demo
TRADOVATE_BASE_URL = "https://demo.tradovateapi.com/v1" if TRADOVATE_DEMO else "https://live.tradovateapi.com/v1"
TRADOVATE_CREDENTIALS_FILE = _cfg.brokers.tradovate.credentials_file
TRADOVATE_ACCOUNT_ID = _cfg.brokers.tradovate.account_id
TRADOVATE_ACCOUNT_SPEC = _cfg.brokers.tradovate.account_spec

# Rithmic
RITHMIC_ENABLED = _cfg.brokers.rithmic.enabled
RITHMIC_ENV = _cfg.brokers.rithmic.env
RITHMIC_EXCHANGE = _cfg.brokers.rithmic.exchange
RITHMIC_LADDER_ENABLED = _cfg.brokers.rithmic.ladder_enabled
RITHMIC_MIRROR_ENABLED = _cfg.brokers.rithmic.mirror_enabled

# Bridge instances (initialized in main())
_rithmic_bridge = None
_CLOUD_CLIENT = None

# Contracts
MES_CONTRACT_SEARCH_TEXT = _cfg.contracts.mes.search_text
MNQ_CONTRACT_SEARCH_TEXT = _cfg.contracts.mnq.search_text
TRADOVATE_MES_CONTRACT_NAME = _cfg.contracts.mes.tradovate_name
TRADOVATE_MNQ_CONTRACT_NAME = _cfg.contracts.mnq.tradovate_name
TRADOVATE_CONTRACT_NAME = TRADOVATE_MES_CONTRACT_NAME
RITHMIC_MES_CONTRACT = _cfg.contracts.mes.rithmic_name
RITHMIC_MNQ_CONTRACT = _cfg.contracts.mnq.rithmic_name

# Strategy
TEST_MODE = _cfg.strategy.test_mode
PRACTICE_ACCOUNT = _cfg.strategy.practice_account_id
LADDER_111_ENABLED = _cfg.strategy.ladder_111_enabled
INDEPENDENT_TP_ENABLED = _cfg.strategy.independent_tp_enabled
FLATTEN_AT_FIRST_TRIM = _cfg.strategy.flatten_at_first_trim
MES_CATALYST_RULE = _cfg.strategy.mes_catalyst_rule

# MES params
MES_CONTRACTS = _cfg.strategy.mes.contracts
MES_TRAILING_STOP_POINTS = _cfg.strategy.mes.trailing_stop_points
MES_FIRST_TRIM_STOP_POINTS = _cfg.strategy.mes.first_trim_stop_points
MES_FIRST_TRIM_CONTRACTS = _cfg.strategy.mes.first_trim_contracts
MES_SECOND_TRIM_CONTRACTS = _cfg.strategy.mes.second_trim_contracts
MES_RUNNER_CONTRACTS = _cfg.strategy.mes.runner_contracts
MES_RUNNER_AUTO_CLOSE_POINTS = _cfg.strategy.mes.runner_auto_close_points
MES_RUNNER_BREAKEVEN_OFFSET = _cfg.strategy.mes.runner_breakeven_offset
MES_TP_POINTS = _cfg.strategy.mes.tp_points
MES_T1_POINTS = _cfg.strategy.mes.t1_points
MES_T2_POINTS = _cfg.strategy.mes.t2_points

# MNQ params
MNQ_CONTRACTS = _cfg.strategy.mnq.contracts
MNQ_TRAILING_STOP_POINTS = _cfg.strategy.mnq.trailing_stop_points
MNQ_FIRST_TRIM_STOP_POINTS = _cfg.strategy.mnq.first_trim_stop_points
MNQ_FIRST_TRIM_CONTRACTS = _cfg.strategy.mnq.first_trim_contracts
MNQ_SECOND_TRIM_CONTRACTS = _cfg.strategy.mnq.second_trim_contracts
MNQ_RUNNER_CONTRACTS = _cfg.strategy.mnq.runner_contracts
MNQ_RUNNER_AUTO_CLOSE_POINTS = _cfg.strategy.mnq.runner_auto_close_points
MNQ_RUNNER_BREAKEVEN_OFFSET = _cfg.strategy.mnq.runner_breakeven_offset
MNQ_TP_POINTS = _cfg.strategy.mnq.tp_points
MNQ_T1_POINTS = _cfg.strategy.mnq.t1_points
MNQ_T2_POINTS = _cfg.strategy.mnq.t2_points

# EOD
EOD_FLATTEN_HOUR = _cfg.strategy.eod_flatten.hour
EOD_FLATTEN_MINUTE = _cfg.strategy.eod_flatten.minute

# Shared / legacy aliases
CONTRACTS = MES_CONTRACTS
TRAILING_STOP_POINTS = MES_TRAILING_STOP_POINTS
FIRST_TRIM_CONTRACTS = MES_FIRST_TRIM_CONTRACTS
SECOND_TRIM_CONTRACTS = MES_SECOND_TRIM_CONTRACTS
RUNNER_CONTRACTS = MES_RUNNER_CONTRACTS
RUNNER_AUTO_CLOSE_POINTS = MES_RUNNER_AUTO_CLOSE_POINTS
FIRST_TRIM_STOP_POINTS = MES_FIRST_TRIM_STOP_POINTS

# Alerts
MAX_ALERT_AGE_SECONDS = _cfg.alerts.max_age_seconds
RATE_LIMIT_SECONDS = _cfg.alerts.rate_limit_seconds

# Auto-trim
AUTO_TRIM_ENABLED = _cfg.strategy.auto_trim.enabled
AUTO_TRIM_T1_POINTS = _cfg.strategy.auto_trim.t1_points
AUTO_TRIM_T2_POINTS = _cfg.strategy.auto_trim.t2_points
AUTO_TRIM_CHECK_INTERVAL = _cfg.strategy.auto_trim.check_interval

# Telegram
TELEGRAM_CONFIG_FILE = _cfg.notifications.telegram.config_file

# Test mode overrides
if TEST_MODE:
    MES_T1_POINTS = 2
    MES_T2_POINTS = 4
    MES_RUNNER_AUTO_CLOSE_POINTS = 6
    MES_TRAILING_STOP_POINTS = 5
    MES_FIRST_TRIM_STOP_POINTS = 3
    MES_RUNNER_BREAKEVEN_OFFSET = 1
    MNQ_T1_POINTS = 8
    MNQ_T2_POINTS = 16
    MNQ_RUNNER_AUTO_CLOSE_POINTS = 24
    MNQ_TRAILING_STOP_POINTS = 20
    MNQ_FIRST_TRIM_STOP_POINTS = 20
    MNQ_RUNNER_BREAKEVEN_OFFSET = 8
    print("⚠️  TEST_MODE active — using TIGHT ladder targets for fast verification on PRAC")

# ==================== ALERT NORMALIZER ====================
from alert_normalizer import normalize_alert

# ==================== CLOUD GATEWAY (SaaS v1) ====================
# Gate the bot behind PropTradeBot BaaS subscription.
# Set PTB_API_KEY env var to enable. Without it, bot runs in local mode.
_CLOUD_CLIENT = None
_CLOUD_SUBSCRIPTION_ACTIVE = True  # False = hard gate (block new trades)

def _init_cloud():
    """Authenticate with cloud on startup. Exits if key set but invalid."""
    global _CLOUD_CLIENT
    api_key = os.environ.get("PTB_API_KEY")
    if not api_key or not CLOUD_CLIENT_AVAILABLE:
        return False
    try:
        _CLOUD_CLIENT = CloudClient(api_key=api_key)
        if not _CLOUD_CLIENT.authenticate():
            print("\n❌ Cloud auth failed — subscription inactive or invalid API key.")
            print("   Get your key at: https://proptradebot.com/dashboard")
            sys.exit(1)
        # Optional: override strategy params from cloud config
        _apply_cloud_strategy(_CLOUD_CLIENT.config or {})
        _CLOUD_CLIENT.start_heartbeat()
        # Start periodic re-check thread (every 5 min)
        threading.Thread(target=_cloud_recheck_loop, daemon=True).start()
        print(f"\n☁️  Cloud connected: {_CLOUD_CLIENT.user.get('email')} "
              f"({_CLOUD_CLIENT.user.get('plan_tier')} plan)")
        return True
    except SystemExit:
        raise
    except Exception as e:
        print(f"⚠️  Cloud init error (running local): {e}")
        _CLOUD_CLIENT = None
        return False

def _cloud_recheck_loop():
    """Background thread: re-check subscription every 5 minutes."""
    global _CLOUD_SUBSCRIPTION_ACTIVE
    while True:
        time.sleep(300)
        if _CLOUD_CLIENT is None:
            continue
        try:
            ok = _CLOUD_CLIENT.is_subscription_active()
            if not ok and _CLOUD_SUBSCRIPTION_ACTIVE:
                print("\n🚨 SUBSCRIPTION LAPSED — new trades BLOCKED")
                print("   Renew at: https://proptradebot.com/dashboard")
                _CLOUD_SUBSCRIPTION_ACTIVE = False
            elif ok and not _CLOUD_SUBSCRIPTION_ACTIVE:
                print("\n✅ SUBSCRIPTION RESTORED — trades enabled")
                _CLOUD_SUBSCRIPTION_ACTIVE = True
        except Exception as e:
            logger.warning(f"Cloud recheck error: {e}")

def _cloud_gate_check(alert_type: str) -> bool:
    """Returns True if this alert type is allowed through the cloud gate."""
    if _CLOUD_CLIENT is None:
        return True  # local mode = no gate
    if _CLOUD_SUBSCRIPTION_ACTIVE:
        return True
    # Subscription lapsed: allow trims/stops/close on existing positions,
    # but block NEW entries
    if alert_type == "entry":
        return False
    return True

def _apply_cloud_strategy(cloud_cfg):
    """Override local strategy constants from cloud bot_config values."""
    global MNQ_CONTRACTS, MNQ_TRAILING_STOP_POINTS, MNQ_FIRST_TRIM_STOP_POINTS
    global MNQ_FIRST_TRIM_CONTRACTS, MNQ_SECOND_TRIM_CONTRACTS, MNQ_RUNNER_CONTRACTS
    if not cloud_cfg:
        return
    cc = cloud_cfg.get("contract_count")
    if cc is not None:
        MNQ_CONTRACTS = int(cc)
        MNQ_FIRST_TRIM_CONTRACTS = max(1, (int(cc) - 1) // 2)
        MNQ_SECOND_TRIM_CONTRACTS = 1
        MNQ_RUNNER_CONTRACTS = max(0, int(cc) - MNQ_FIRST_TRIM_CONTRACTS - 1)
    sl = cloud_cfg.get("stop_loss_ticks")
    if sl is not None:
        MNQ_TRAILING_STOP_POINTS = int(sl)
        MNQ_FIRST_TRIM_STOP_POINTS = int(sl)
    if cc is not None or sl is not None:
        print(f"☁️  Strategy from cloud: {MNQ_CONTRACTS}c MNQ, "
              f"stop={MNQ_TRAILING_STOP_POINTS}pts")

def _cloud_report_trade(trade_dict):
    """Report a completed trade to the cloud dashboard (non-blocking)."""
    if _CLOUD_CLIENT is None:
        return
    import threading
    def _send():
        try:
            _CLOUD_CLIENT.report_trade({
                "symbol":         trade_dict.get("instrument", "MNQ"),
                "trade_direction": trade_dict.get("direction"),
                "contracts":      trade_dict.get("contracts"),
                "entry_price":    trade_dict.get("entry"),
                "exit_price":     trade_dict.get("exit"),
                "realized_pnl":   trade_dict.get("pnl"),
                "commission":     trade_dict.get("contracts", 1) * 5.0,
                "status":         "closed",
                "metadata":       {
                    "level":      trade_dict.get("level"),
                    "exit_type":  trade_dict.get("exit_type"),
                    "account_id": str(trade_dict.get("account_id")),
                }
            })
        except Exception as _e:
            pass  # never block the bot
    threading.Thread(target=_send, daemon=True).start()

# ==================== GLOBALS ====================
active_positions = {}  # key: direction_entryprice, value: position info
recent_trades = {}
recent_trim_alerts = {}
recent_entry_alerts = {}

_api_token = None
_api_token_expires = 0
_api_lock = threading.Lock()
_last_api_call = 0
_mes_contract_id = None
_mes_contract_info = None
_mnq_contract_id = None
_mnq_contract_info = None

_tv_token = None
_tv_token_expires = 0
_tv_lock = threading.Lock()
_tv_last_api_call = 0
_tv_contract_id = None
_tv_contract_info = None
_tv_mes_contract_id = None
_tv_mes_contract_info = None
_tv_mnq_contract_id = None
_tv_mnq_contract_info = None
_tv_dry_run_counter = 0

def _tv_next_dry_id():
    """Generate a unique fake order ID for dry-run mode (999000000+)."""
    global _tv_dry_run_counter
    _tv_dry_run_counter += 1
    return 999000000 + _tv_dry_run_counter

# ==================== ORIGINAL CODE BELOW ====================
# ================= SETUP =================
for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)
# NOTE 2026-05-01: removed `logger.propagate = False` — it was silently dropping all
# logger.info() messages because basicConfig attaches handlers to the ROOT logger,
# not to this named logger. Without propagation, nothing reached the FileHandler.
# logger.propagate = False

# ================= RATE LIMITER =================
def rate_limit():
    """Enforce minimum delay between API calls"""
    global _last_api_call
    now = time.time()
    elapsed = now - _last_api_call
    if elapsed < RATE_LIMIT_SECONDS:
        time.sleep(RATE_LIMIT_SECONDS - elapsed)
    _last_api_call = time.time()

# ================= PROJECTX AUTH =================
def load_credentials():
    """Load ProjectX credentials from file"""
    try:
        with open(CREDENTIALS_FILE, "r") as f:
            creds = json.load(f)
        return creds.get("username"), creds.get("api_key")
    except Exception as e:
        logger.error(f"❌ Failed to load credentials: {e}")
        return None, None

def px_login():
    """Login to ProjectX API and cache token"""
    global _api_token, _api_token_expires
    username, api_key = load_credentials()
    if not username or not api_key:
        logger.error("❌ No credentials available")
        return False
    
    rate_limit()
    try:
        resp = requests.post(f"{PROJECTX_BASE_URL}/api/Auth/loginKey",
                             json={"userName": username, "apiKey": api_key},
                             timeout=15)
        data = resp.json()
        if data.get("success") and data.get("token"):
            _api_token = data["token"]
            _api_token_expires = time.time() + 20 * 3600  # Refresh after 20h
            logger.info("✅ ProjectX login successful")
            return True
        else:
            logger.error(f"❌ ProjectX login failed: {data}")
            return False
    except Exception as e:
        logger.error(f"❌ ProjectX login error: {e}")
        return False

def px_get_token():
    """Get valid API token, refreshing if needed"""
    global _api_token, _api_token_expires
    with _api_lock:
        if _api_token and time.time() < _api_token_expires:
            return _api_token
        # Try validate first
        if _api_token:
            rate_limit()
            try:
                resp = requests.post(f"{PROJECTX_BASE_URL}/api/Auth/validate",
                                     headers={"Authorization": f"Bearer {_api_token}"},
                                     timeout=10)
                data = resp.json()
                if data.get("success"):
                    new_token = data.get("token") or data.get("newToken")
                    if new_token:
                        _api_token = new_token
                    _api_token_expires = time.time() + 20 * 3600
                    logger.info("✅ Token validated/refreshed")
                    return _api_token
            except Exception:
                pass
        # Full re-login
        if px_login():
            return _api_token
        return None

def px_api(endpoint, payload=None):
    """Make authenticated ProjectX API call with rate limiting"""
    token = px_get_token()
    if not token:
        logger.error(f"❌ No valid token for {endpoint}")
        return None
    
    rate_limit()
    url = f"{PROJECTX_BASE_URL}{endpoint}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    
    try:
        resp = requests.post(url, json=payload or {}, headers=headers, timeout=15)
        if resp.status_code == 401:
            # Token expired mid-session, force re-login
            global _api_token_expires
            _api_token_expires = 0
            token = px_get_token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
                rate_limit()
                resp = requests.post(url, json=payload or {}, headers=headers, timeout=15)
        
        data = resp.json()
        return data
    except Exception as e:
        logger.error(f"❌ API call failed {endpoint}: {e}")
        return None

# ================= CONTRACT LOOKUP =================
def _find_contract(search_text, label):
    """Generic contract search on ProjectX. Returns (contract_id, contract_info) or (None, None)."""
    logger.info(f"🔍 Searching for active {label} contract...")
    
    # ProjectX simulated accounts need live=False for contract search
    data = px_api("/api/Contract/search", {"searchText": search_text, "live": False})
    if not data or not data.get("contracts"):
        data = px_api("/api/Contract/search", {"searchText": search_text, "live": True})
    
    if not data or not data.get("contracts"):
        logger.error(f"❌ No {label} contracts found!")
        return None, None
    
    contracts = data["contracts"]
    active = [c for c in contracts if c.get("activeContract")]
    contract = active[0] if active else contracts[0]
    
    logger.info(f"✅ Found {label} contract: {contract.get('name', label)} (ID: {contract['id']})")
    logger.info(f"   Tick size: {contract.get('tickSize')}, Tick value: {contract.get('tickValue')}")
    return contract["id"], contract

def find_mes_contract():
    """Find active MES contract ID on startup"""
    global _mes_contract_id, _mes_contract_info
    _mes_contract_id, _mes_contract_info = _find_contract(MES_CONTRACT_SEARCH_TEXT, "MES")
    return _mes_contract_id is not None

def find_mnq_contract():
    """Find active MNQ contract ID on startup"""
    global _mnq_contract_id, _mnq_contract_info
    _mnq_contract_id, _mnq_contract_info = _find_contract(MNQ_CONTRACT_SEARCH_TEXT, "MNQ")
    return _mnq_contract_id is not None

# ================= TRADOVATE AUTH =================
def tv_load_credentials():
    """Load Tradovate credentials from file"""
    try:
        with open(TRADOVATE_CREDENTIALS_FILE, "r") as f:
            creds = json.load(f)
        return creds
    except Exception as e:
        logger.error(f"❌ Failed to load Tradovate credentials: {e}")
        return None

def tv_login():
    """Login to Tradovate API and cache token"""
    global _tv_token, _tv_token_expires
    creds = tv_load_credentials()
    if not creds:
        logger.error("❌ No Tradovate credentials available")
        return False
    
    tv_rate_limit()
    try:
        resp = requests.post(f"{TRADOVATE_BASE_URL}/auth/accesstokenrequest",
                             json={
                                 "name": creds["username"],
                                 "password": creds["password"],
                                 "appId": "Tradovate Bot",
                                 "appVersion": "1.0",
                                 "cid": creds["cid"],
                                 "sec": creds["secret"]
                             },
                             timeout=15)
        data = resp.json()
        if data.get("accessToken"):
            _tv_token = data["accessToken"]
            # Parse expiration time
            exp_str = data.get("expirationTime", "")
            if exp_str:
                from datetime import timezone
                exp_dt = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
                _tv_token_expires = exp_dt.timestamp() - 300  # Refresh 5 min early
            else:
                _tv_token_expires = time.time() + 20 * 3600
            logger.info(f"✅ Tradovate login successful (user: {data.get('name')}, userId: {data.get('userId')})")
            return True
        else:
            logger.error(f"❌ Tradovate login failed: {data}")
            return False
    except Exception as e:
        logger.error(f"❌ Tradovate login error: {e}")
        return False

def tv_get_token():
    """Get valid Tradovate API token, refreshing if needed"""
    global _tv_token, _tv_token_expires
    with _tv_lock:
        if _tv_token and time.time() < _tv_token_expires:
            return _tv_token
        # Token expired or missing, re-login
        if tv_login():
            return _tv_token
        return None

def tv_rate_limit():
    """Enforce minimum delay between Tradovate API calls"""
    global _tv_last_api_call
    now = time.time()
    elapsed = now - _tv_last_api_call
    if elapsed < RATE_LIMIT_SECONDS:
        time.sleep(RATE_LIMIT_SECONDS - elapsed)
    _tv_last_api_call = time.time()

def tv_api_get(endpoint):
    """Make authenticated Tradovate GET request"""
    token = tv_get_token()
    if not token:
        logger.error(f"❌ No valid Tradovate token for GET {endpoint}")
        return None
    
    tv_rate_limit()
    url = f"{TRADOVATE_BASE_URL}{endpoint}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 401:
            global _tv_token_expires
            _tv_token_expires = 0
            token = tv_get_token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
                tv_rate_limit()
                resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200 and resp.text:
            return resp.json()
        else:
            logger.error(f"❌ Tradovate GET {endpoint} returned {resp.status_code}: {resp.text[:200]}")
            return None
    except Exception as e:
        logger.error(f"❌ Tradovate GET failed {endpoint}: {e}")
        return None

def tv_api_post(endpoint, payload=None):
    """Make authenticated Tradovate POST request"""
    token = tv_get_token()
    if not token:
        logger.error(f"❌ No valid Tradovate token for POST {endpoint}")
        return None
    
    tv_rate_limit()
    url = f"{TRADOVATE_BASE_URL}{endpoint}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    
    try:
        resp = requests.post(url, json=payload or {}, headers=headers, timeout=15)
        if resp.status_code == 401:
            global _tv_token_expires
            _tv_token_expires = 0
            token = tv_get_token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
                tv_rate_limit()
                resp = requests.post(url, json=payload or {}, headers=headers, timeout=15)
        if resp.status_code in (200, 204):
            if resp.text:
                return resp.json()
            return True
        logger.error(f"❌ Tradovate POST {endpoint} returned {resp.status_code}: {resp.text[:200]}")
        return None

    except Exception as e:
        logger.error(f"❌ Tradovate POST failed {endpoint}: {e}")
        return None

# ================= TRADOVATE CONTRACT LOOKUP =================
def tv_find_mes_contract():
    """Find active MES contract on Tradovate"""
    global _tv_contract_id, _tv_contract_info, _tv_mes_contract_id, _tv_mes_contract_info
    logger.info(f"🔍 Tradovate: Looking up contract {TRADOVATE_MES_CONTRACT_NAME}...")
    
    data = tv_api_get(f"/contract/find?name={TRADOVATE_MES_CONTRACT_NAME}")
    if not data or not data.get("id"):
        logger.error(f"❌ Tradovate: Contract {TRADOVATE_MES_CONTRACT_NAME} not found!")
        return False
    
    _tv_mes_contract_id = data["id"]
    _tv_mes_contract_info = data
    _tv_contract_id = data["id"]  # Legacy alias
    _tv_contract_info = data
    logger.info(f"✅ Tradovate: Found {TRADOVATE_MES_CONTRACT_NAME} (ID: {_tv_mes_contract_id})")
    return True

def tv_find_mnq_contract():
    """Find active MNQ contract on Tradovate"""
    global _tv_mnq_contract_id, _tv_mnq_contract_info
    logger.info(f"🔍 Tradovate: Looking up contract {TRADOVATE_MNQ_CONTRACT_NAME}...")
    
    data = tv_api_get(f"/contract/find?name={TRADOVATE_MNQ_CONTRACT_NAME}")
    if not data or not data.get("id"):
        logger.error(f"❌ Tradovate: Contract {TRADOVATE_MNQ_CONTRACT_NAME} not found!")
        return False
    
    _tv_mnq_contract_id = data["id"]
    _tv_mnq_contract_info = data
    logger.info(f"✅ Tradovate: Found {TRADOVATE_MNQ_CONTRACT_NAME} (ID: {_tv_mnq_contract_id})")
    return True

# ================= TRADOVATE ORDER EXECUTION =================
def tv_place_market_order(side, size, symbol=None):
    """Place market order on Tradovate. side: 'Buy' or 'Sell'. Returns orderId or None."""
    if not TRADOVATE_ENABLED:
        return None
    if symbol is None:
        symbol = TRADOVATE_MES_CONTRACT_NAME
    if TRADOVATE_DRY_RUN:
        fake_id = _tv_next_dry_id()
        logger.info(f"🧪 DRY RUN — would place MARKET: {side} {size} {symbol} (fake_id={fake_id})")
        return fake_id
    
    payload = {
        "accountSpec": TRADOVATE_ACCOUNT_SPEC,
        "accountId": TRADOVATE_ACCOUNT_ID,
        "action": side,
        "symbol": symbol,
        "orderQty": size,
        "orderType": "Market",
        "isAutomated": True,
    }
    logger.info(f"📤 Tradovate MARKET: {side} {size} {symbol}")
    data = tv_api_post("/order/placeorder", payload)
    if data and data.get("orderId"):
        logger.info(f"✅ Tradovate market order placed: orderId={data['orderId']}")
        return data["orderId"]
    else:
        logger.error(f"❌ Tradovate market order FAILED: {data}")
        return None

def tv_place_stop_order(side, size, stop_price, symbol=None):
    """Place stop order on Tradovate. Returns orderId or None."""
    if not TRADOVATE_ENABLED:
        return None
    if symbol is None:
        symbol = TRADOVATE_MES_CONTRACT_NAME
    if TRADOVATE_DRY_RUN:
        fake_id = _tv_next_dry_id()
        logger.info(f"🧪 DRY RUN — would place STOP: {side} {size} {symbol} @ {stop_price} (fake_id={fake_id})")
        return fake_id
    
    payload = {
        "accountSpec": TRADOVATE_ACCOUNT_SPEC,
        "accountId": TRADOVATE_ACCOUNT_ID,
        "action": side,
        "symbol": symbol,
        "orderQty": size,
        "orderType": "Stop",
        "stopPrice": stop_price,
        "isAutomated": True,
    }
    logger.info(f"🛑 Tradovate STOP: {side} {size} {symbol} @ {stop_price}")
    data = tv_api_post("/order/placeorder", payload)
    if data and data.get("orderId"):
        logger.info(f"✅ Tradovate stop order placed: orderId={data['orderId']}")
        return data["orderId"]
    else:
        logger.error(f"❌ Tradovate stop order FAILED: {data}")
        return None

def tv_place_limit_order(side, size, limit_price, symbol=None):
    """Place limit order on Tradovate. Returns orderId or None."""
    if not TRADOVATE_ENABLED:
        return None
    if symbol is None:
        symbol = TRADOVATE_MES_CONTRACT_NAME
    if TRADOVATE_DRY_RUN:
        fake_id = _tv_next_dry_id()
        logger.info(f"🧪 DRY RUN — would place LIMIT: {side} {size} {symbol} @ {limit_price} (fake_id={fake_id})")
        return fake_id
    
    payload = {
        "accountSpec": TRADOVATE_ACCOUNT_SPEC,
        "accountId": TRADOVATE_ACCOUNT_ID,
        "action": side,
        "symbol": symbol,
        "orderQty": size,
        "orderType": "Limit",
        "price": limit_price,
        "isAutomated": True,
    }
    logger.info(f"📋 Tradovate LIMIT: {side} {size} {symbol} @ {limit_price}")
    data = tv_api_post("/order/placeorder", payload)
    if data and data.get("orderId"):
        logger.info(f"✅ Tradovate limit order placed: orderId={data['orderId']}")
        return data["orderId"]
    else:
        logger.error(f"❌ Tradovate limit order FAILED: {data}")
        return None

def tv_cancel_order(order_id):
    """Cancel order on Tradovate by orderId."""
    if not TRADOVATE_ENABLED:
        return True
    if TRADOVATE_DRY_RUN:
        logger.info(f"🧪 DRY RUN — would cancel order {order_id}")
        return True
    
    logger.info(f"🚫 Tradovate cancel order: {order_id}")
    data = tv_api_post("/order/cancelorder", {"orderId": order_id})
    if data and data.get("commandStatus") != "AtServer":
        # Tradovate returns the order object on cancel
        logger.info(f"✅ Tradovate order cancelled: {order_id}")
        return True
    elif data:
        logger.info(f"✅ Tradovate cancel submitted: {order_id}")
        return True
    else:
        logger.error(f"❌ Tradovate cancel FAILED: {order_id}")
        return False

def tv_modify_order(order_id, new_size=None, new_stop_price=None):
    """Modify existing order on Tradovate."""
    if not TRADOVATE_ENABLED:
        return True
    
    payload = {"orderId": order_id}
    if new_size is not None:
        payload["orderQty"] = new_size
    if new_stop_price is not None:
        payload["stopPrice"] = new_stop_price
    
    logger.info(f"🔧 Tradovate modify: orderId={order_id} qty={new_size} stop={new_stop_price}")
    data = tv_api_post("/order/modifyorder", payload)
    if data:
        logger.info(f"✅ Tradovate order modified: {order_id}")
        return True
    else:
        logger.error(f"❌ Tradovate modify FAILED: {order_id}")
        return False

def tv_partial_close(size, direction, symbol=None):
    """Partial close on Tradovate by placing opposing market order."""
    if not TRADOVATE_ENABLED:
        return True
    if symbol is None:
        symbol = TRADOVATE_MES_CONTRACT_NAME
    
    # Opposite side to close
    close_side = "Sell" if direction == "long" else "Buy"
    logger.info(f"✂️ Tradovate partial close: {close_side} {size} {symbol}")
    order_id = tv_place_market_order(close_side, size, symbol=symbol)
    return order_id is not None

def tv_flatten():
    """Flatten all positions on Tradovate account."""
    if not TRADOVATE_ENABLED:
        return True
    if TRADOVATE_DRY_RUN:
        logger.info(f"🧪 DRY RUN — would flatten account {TRADOVATE_ACCOUNT_ID}")
        return True
    
    logger.info(f"🧹 Tradovate flattening account {TRADOVATE_ACCOUNT_ID}")
    data = tv_api_post("/order/liquidateposition", {"accountId": TRADOVATE_ACCOUNT_ID, "admin": True})
    if data:
        logger.info(f"✅ Tradovate flatten submitted")
        return True
    else:
        logger.error(f"❌ Tradovate flatten FAILED")
        return False

def tv_cancel_all_orders():
    """Cancel all open orders on Tradovate."""
    if not TRADOVATE_ENABLED:
        return True
    if TRADOVATE_DRY_RUN:
        logger.info("🧪 DRY RUN — would cancel all open orders")
        return True
    
    orders = tv_api_get("/order/list")
    if not orders:
        return True
    
    cancelled = 0
    for order in orders:
        status = order.get("ordStatus", "")
        if status in ("Working", "Accepted"):
            tv_cancel_order(order["id"])
            cancelled += 1
    logger.info(f"🚫 Tradovate: cancelled {cancelled} open orders")
    return True


def tv_cancel_all_stops():
    """Cancel all working STOP orders on Tradovate. Leaves limits and other orders alone."""
    if not TRADOVATE_ENABLED:
        return True
    if TRADOVATE_DRY_RUN:
        logger.info("🧪 DRY RUN — would cancel all stop orders")
        return True
    
    orders = tv_api_get("/order/list")
    if not orders:
        return True
    
    cancelled = 0
    for order in orders:
        status = order.get("ordStatus", "")
        order_type = order.get("orderType", "")
        if status in ("Working", "Accepted") and order_type == "Stop":
            tv_cancel_order(order["id"])
            cancelled += 1
    logger.info(f"Tradovate: cancelled {cancelled} stop orders (non-stop orders preserved)")
    return True

def tv_get_positions():
    """Get open positions from Tradovate."""
    if not TRADOVATE_ENABLED:
        return []
    return tv_api_get("/position/list") or []

def tv_get_open_orders():
    """Get open orders from Tradovate."""
    if not TRADOVATE_ENABLED:
        return []
    orders = tv_api_get("/order/list") or []
    return [o for o in orders if o.get("ordStatus") in ("Working", "Accepted")]

def tv_verify_state(position, step_name=""):
    """Verify Tradovate position and orders match expected state. Send summary to Telegram."""
    if not TRADOVATE_ENABLED:
        return True
    if TRADOVATE_DRY_RUN:
        logger.info(f"🧪 DRY RUN — skipping Tradovate verify ({step_name})")
        return True
    
    try:
        # Get current Tradovate state
        tv_positions = tv_get_positions()
        tv_orders = tv_get_open_orders()
        
        # Find our MES position
        tv_net_pos = 0
        for p in tv_positions:
            if p.get("contractId") == _tv_contract_id:
                tv_net_pos = p.get("netPos", 0)
                break
        
        # Count stop orders
        stop_orders = [o for o in tv_orders if o.get("orderType") == "Stop" or o.get("ordType") == "STP"]
        # Also check by action (Sell stops for longs, Buy stops for shorts)
        working_stops = [o for o in tv_orders if o.get("ordStatus") in ("Working", "Accepted") and (o.get("orderType") in ("Stop",) or "stop" in str(o.get("ordType", "")).lower())]
        
        # Determine expected state
        if position:
            expected_contracts = position["remaining_contracts"]
            expected_direction = position["direction"]
            expected_stop_count = 1 if expected_contracts > 0 else 0
            expected_stop_size = expected_contracts
            stop_price = position.get("current_stop_price", "?")
        else:
            expected_contracts = 0
            expected_direction = "flat"
            expected_stop_count = 0
            expected_stop_size = 0
            stop_price = "N/A"
        
        # Build verification message
        position_match = (tv_net_pos != 0 and expected_contracts > 0) or (tv_net_pos == 0 and expected_contracts == 0)
        stop_count_ok = len(working_stops) == expected_stop_count or len(tv_orders) == expected_stop_count
        
        status_emoji = "✅" if (position_match and stop_count_ok) else "⚠️"
        
        msg_lines = [
            f"{status_emoji} <b>Tradovate Verify — {step_name}</b>",
            f"",
            f"<b>Position:</b> {tv_net_pos} contracts (expected: {expected_contracts} {expected_direction})",
            f"<b>Open orders:</b> {len(tv_orders)} (expected stops: {expected_stop_count})",
        ]
        
        if tv_orders:
            for o in tv_orders:
                o_action = o.get("action", "?")
                o_qty = o.get("orderQty", "?")
                o_stop = o.get("stopPrice", o.get("price", "?"))
                o_type = o.get("orderType", o.get("ordType", "?"))
                o_status = o.get("ordStatus", "?")
                msg_lines.append(f"  • {o_action} {o_qty} @ {o_stop} ({o_type}, {o_status})")
        
        if not position_match:
            msg_lines.append(f"\n❌ POSITION MISMATCH")
        if not stop_count_ok:
            msg_lines.append(f"\n❌ STOP COUNT MISMATCH: have {len(tv_orders)} orders, expected {expected_stop_count}")
        
        msg = "\n".join(msg_lines)
        logger.info(f"🔍 TV Verify [{step_name}]: pos={tv_net_pos}, orders={len(tv_orders)}, match={position_match and stop_count_ok}")
        send_telegram_alert(msg)
        
        return position_match and stop_count_ok
    except Exception as e:
        logger.error(f"❌ Tradovate verify failed: {e}")
        send_telegram_alert(f"❌ <b>Tradovate Verify FAILED</b> — {step_name}\n{e}")
        return False

# Test mode configuration
TEST_MODE = False  # LIVE 2026-05-21 12:42 ET — 5 new evals, MNQ 3-1-1

# When TEST_MODE is True, override the ladder targets/stops with very tight levels so the
# state machine fully cycles (T1 → T2 → Runner) within minutes on the PRAC account, instead
# of waiting hours for a +20 MES move. Flip TEST_MODE = False to use the live targets
# defined at the top of the file.
if TEST_MODE:
    # MES tight targets
    MES_T1_POINTS = 2                  # was 5
    MES_T2_POINTS = 4                  # was 10
    MES_RUNNER_AUTO_CLOSE_POINTS = 6   # was 20
    MES_TRAILING_STOP_POINTS = 5       # was 11
    MES_FIRST_TRIM_STOP_POINTS = 3     # was 7
    MES_RUNNER_BREAKEVEN_OFFSET = 1    # was 3
    # MNQ tight targets
    MNQ_T1_POINTS = 8                  # was 20
    MNQ_T2_POINTS = 16                 # was 40
    MNQ_RUNNER_AUTO_CLOSE_POINTS = 24  # was 60
    MNQ_TRAILING_STOP_POINTS = 20      # was 40
    MNQ_FIRST_TRIM_STOP_POINTS = 20    # was 40
    MNQ_RUNNER_BREAKEVEN_OFFSET = 8    # was 21
    print("⚠️  TEST_MODE active — using TIGHT ladder targets for fast verification on PRAC")
PRACTICE_ACCOUNT = 20574609  # PRAC-V2-308812-52844352

def get_active_accounts():
    """Return list of enabled account IDs (all active accounts, incl. followers).
    Use this for QUERIES/MONITORING (broker positions, open orders, sync checks).
    For ORDER PLACEMENT/MODIFICATION, use get_order_routing_accounts() instead."""
    # Test mode - only use practice account
    if TEST_MODE:
        logger.info(f"🧪 TEST MODE: Using only practice account {PRACTICE_ACCOUNT}")
        return [PRACTICE_ACCOUNT]
    return [a["id"] for a in ACCOUNTS if a.get("enabled")]

def get_order_routing_accounts():
    """Return list of accounts the bot may PLACE/MODIFY orders on directly.
    Excludes 'follower' accounts — those are filled by Topstep's internal copier
    cascading from the leader. Calling order APIs on followers returns
    errorCode=10 'Follower accounts cannot place orders' and pollutes logs with
    false-alarm 'No stop tracked' warnings and 'Order FAILED' errors.
    Added 2026-05-12 after follower-warning incident threw user off."""
    if TEST_MODE:
        return [PRACTICE_ACCOUNT]
    return [a["id"] for a in ACCOUNTS if a.get("enabled") and not a.get("follower")]

# ================= POSITION VERIFICATION =================
def verify_broker_position(account_id, expected_direction=None, expected_size=None, contract_id=None):
    """Verify broker position matches expected state. Returns (matches, broker_position)"""
    cid = contract_id or _mes_contract_id
    data = px_api("/api/Position/searchOpen", {"accountId": account_id})
    if data is None:
        logger.error(f"❌ Failed to query positions for account {account_id}")
        return None, None
    
    positions = data.get("positions", [])
    
    # Find position for the specified contract
    target_pos = None
    for p in positions:
        if p.get("contractId") == cid:
            target_pos = p
            break
    
    if expected_direction is None and expected_size is None:
        return True, target_pos
    
    if expected_size == 0:
        if target_pos is None or target_pos.get("size", 0) == 0:
            return True, target_pos
        else:
            logger.error(f"❌ MISMATCH account {account_id}: expected FLAT, broker has {target_pos.get('size')} contracts")
            return False, target_pos
    
    if target_pos is None:
        logger.error(f"❌ MISMATCH account {account_id}: expected {expected_size} {expected_direction}, broker is FLAT")
        return False, None
    
    broker_size = target_pos.get("size", 0)
    broker_type = target_pos.get("type", 0)  # 1=Long, 2=Short
    broker_dir = "long" if broker_type == 1 else "short" if broker_type == 2 else "unknown"
    
    size_match = (expected_size is None or broker_size == expected_size)
    dir_match = (expected_direction is None or broker_dir == expected_direction)
    
    if size_match and dir_match:
        return True, target_pos
    
    logger.error(f"❌ MISMATCH account {account_id}: expected {expected_size} {expected_direction}, "
                 f"broker has {broker_size} {broker_dir}")
    return False, target_pos

def verify_all_accounts(expected_direction=None, expected_size=None):
    """Verify all active accounts. Returns True only if ALL match."""
    all_ok = True
    for acct_id in get_active_accounts():
        match, _ = verify_broker_position(acct_id, expected_direction, expected_size)
        if match is None or match is False:
            all_ok = False
    return all_ok

# ================= ORDER EXECUTION (MULTI-ACCOUNT) =================
def px_place_market_order(account_id, side, size, contract_id=None, label="MES"):
    """Place market order. side: 0=Buy, 1=Sell. Returns orderId or None."""
    if size is None or size <= 0:
        return None  # MNQ-only mode: skip MES (size=0) silently
    cid = contract_id or _mes_contract_id
    payload = {
        "accountId": account_id,
        "contractId": cid,
        "type": 2,  # Market
        "side": side,
        "size": size,
    }
    logger.info(f"📤 Placing {label} MARKET order: acct={account_id} side={'BUY' if side==0 else 'SELL'} size={size}")
    data = px_api("/api/Order/place", payload)
    if data and data.get("success"):
        order_id = data.get("orderId")
        logger.info(f"✅ {label} Market order placed: orderId={order_id} acct={account_id}")
        return order_id
    else:
        error_code = data.get("errorCode") if data else "no response"
        logger.error(f"❌ {label} Market order FAILED acct={account_id}: {error_code} — {data}")
        return None

def px_place_stop_order(account_id, side, size, stop_price, contract_id=None, label="MES"):
    """Place stop order. Returns orderId or None."""
    if size is None or size <= 0:
        return None
    cid = contract_id or _mes_contract_id
    payload = {
        "accountId": account_id,
        "contractId": cid,
        "type": 4,  # Stop
        "side": side,
        "size": size,
        "stopPrice": stop_price,
    }
    logger.info(f"🛑 Placing {label} STOP order: acct={account_id} side={'BUY' if side==0 else 'SELL'} "
                f"size={size} stopPrice={stop_price}")
    data = px_api("/api/Order/place", payload)
    if data and data.get("success"):
        order_id = data.get("orderId")
        logger.info(f"✅ {label} Stop order placed: orderId={order_id} acct={account_id}")
        return order_id
    else:
        error_code = data.get("errorCode") if data else "no response"
        logger.error(f"❌ {label} Stop order FAILED acct={account_id}: {error_code} — {data}")
        return None

def px_place_limit_order(account_id, side, size, limit_price, contract_id=None, label="MES"):
    """Place limit order. Returns orderId or None."""
    if size is None or size <= 0:
        return None
    cid = contract_id or _mes_contract_id
    payload = {
        "accountId": account_id,
        "contractId": cid,
        "type": 1,  # Limit
        "side": side,
        "size": size,
        "limitPrice": limit_price,
    }
    logger.info(f"Placing {label} LIMIT order: acct={account_id} side={'BUY' if side==0 else 'SELL'} "
                f"size={size} limitPrice={limit_price}")
    data = px_api("/api/Order/place", payload)
    if data and data.get("success"):
        order_id = data.get("orderId")
        logger.info(f"{label} Limit order placed: orderId={order_id} acct={account_id}")
        return order_id
    else:
        error_code = data.get("errorCode") if data else "no response"
        logger.error(f"{label} Limit order FAILED acct={account_id}: {error_code} — {data}")
        return None

def px_modify_stop(account_id, order_id, new_size=None, new_stop_price=None):
    """Modify existing stop order size/price."""
    payload = {"accountId": account_id, "orderId": order_id}
    if new_size is not None:
        payload["size"] = new_size
    if new_stop_price is not None:
        payload["stopPrice"] = new_stop_price
    
    logger.info(f"🔧 Modifying stop: acct={account_id} orderId={order_id} "
                f"newSize={new_size} newStopPrice={new_stop_price}")
    data = px_api("/api/Order/modify", payload)
    if data and data.get("success"):
        logger.info(f"✅ Stop modified: orderId={order_id}")
        return True
    else:
        logger.error(f"❌ Stop modify FAILED: {data}")
        return False

def px_cancel_order(account_id, order_id):
    """Cancel order by ID."""
    logger.info(f"🚫 Cancelling order: acct={account_id} orderId={order_id}")
    data = px_api("/api/Order/cancel", {"accountId": account_id, "orderId": order_id})
    if data and data.get("success"):
        logger.info(f"✅ Order cancelled: {order_id}")
        return True
    else:
        logger.error(f"❌ Cancel FAILED: {data}")
        return False

def px_partial_close(account_id, size, contract_id=None, label="MES"):
    """Partial close using native partialCloseContract. The killer feature."""
    cid = contract_id or _mes_contract_id
    payload = {
        "accountId": account_id,
        "contractId": cid,
        "size": size,
    }
    logger.info(f"✂️ {label} Partial close: acct={account_id} size={size}")
    data = px_api("/api/Position/partialCloseContract", payload)
    if data and data.get("success"):
        logger.info(f"✅ {label} Partial close success: {size} contracts on acct={account_id}")
        return True
    else:
        logger.error(f"❌ {label} Partial close FAILED acct={account_id}: {data}")
        return False

def px_flatten(account_id, contract_id=None, label="MES"):
    """Flatten entire position on account for a specific contract."""
    cid = contract_id or _mes_contract_id
    payload = {
        "accountId": account_id,
        "contractId": cid,
    }
    logger.info(f"🧹 {label} Flattening acct={account_id}")
    data = px_api("/api/Position/closeContract", payload)
    if data and data.get("success"):
        logger.info(f"✅ {label} Flatten success: acct={account_id}")
        return True
    else:
        logger.error(f"❌ {label} Flatten FAILED acct={account_id}: {data}")
        return False

def px_flatten_all_contracts(account_id):
    """Flatten both MES and MNQ positions on account."""
    mes_ok = px_flatten(account_id, _mes_contract_id, "MES")
    mnq_ok = px_flatten(account_id, _mnq_contract_id, "MNQ") if _mnq_contract_id else True
    return mes_ok and mnq_ok

def px_cancel_all_stops(account_id):
    """Cancel all open orders for an account (both MES and MNQ)."""
    data = px_api("/api/Order/searchOpen", {"accountId": account_id})
    if not data:
        return False
    orders = data.get("orders", [])
    cancelled = 0
    target_contracts = {_mes_contract_id, _mnq_contract_id}
    for order in orders:
        if order.get("contractId") in target_contracts:
            px_cancel_order(account_id, order["id"])
            cancelled += 1
    logger.info(f"🚫 Cancelled {cancelled} open orders (MES+MNQ) on acct={account_id}")
    return True

# ================= MULTI-ACCOUNT EXECUTION =================
def execute_entry_all_accounts(side, mes_size, mnq_size):
    """Place market entry for BOTH MES and MNQ on all active accounts in PARALLEL.
    Returns dict of {account_id: {"mes": order_id, "mnq": order_id}}."""
    import threading
    results = {}
    results_lock = threading.Lock()
    
    def place_orders(acct_id):
        mes_oid = px_place_market_order(acct_id, side, mes_size, _mes_contract_id, "MES")
        mnq_oid = px_place_market_order(acct_id, side, mnq_size, _mnq_contract_id, "MNQ") if _mnq_contract_id else None
        with results_lock:
            results[acct_id] = {"mes": mes_oid, "mnq": mnq_oid}
    
    threads = [threading.Thread(target=place_orders, args=(a,)) for a in get_order_routing_accounts()]
    for t in threads: t.start()
    for t in threads: t.join()
    return results

def execute_stop_all_accounts(side, mes_size, mes_stop_price, mnq_size=None, mnq_stop_price=None):
    """Place stop for BOTH MES and MNQ on all active accounts in PARALLEL.
    Returns dict of {account_id: {"mes": order_id, "mnq": order_id}}."""
    import threading
    results = {}
    results_lock = threading.Lock()
    
    def place_stops(acct_id):
        mes_oid = px_place_stop_order(acct_id, side, mes_size, mes_stop_price, _mes_contract_id, "MES")
        mnq_oid = None
        if mnq_size and mnq_stop_price and _mnq_contract_id:
            mnq_oid = px_place_stop_order(acct_id, side, mnq_size, mnq_stop_price, _mnq_contract_id, "MNQ")
        with results_lock:
            results[acct_id] = {"mes": mes_oid, "mnq": mnq_oid}
    
    threads = [threading.Thread(target=place_stops, args=(a,)) for a in get_order_routing_accounts()]
    for t in threads: t.start()
    for t in threads: t.join()
    return results

def execute_partial_close_all_accounts(mes_size, mnq_size=None):
    """Partial close BOTH MES and MNQ on all active accounts in PARALLEL."""
    import threading
    results = {}
    results_lock = threading.Lock()
    
    def close_account(acct_id):
        mes_ok = px_partial_close(acct_id, mes_size, _mes_contract_id, "MES")
        mnq_ok = True
        if mnq_size and _mnq_contract_id:
            # Phantom-trade guard: only trim MNQ if broker actually has an open MNQ position.
            # Previously if MNQ was wicked out but MES still active, trim alerts would send
            # a partial-close on zero position and ProjectX could fill it as opposite-direction.
            _, mnq_broker = verify_broker_position(acct_id, contract_id=_mnq_contract_id)
            broker_mnq_size = mnq_broker.get("size", 0) if mnq_broker else 0
            if broker_mnq_size <= 0:
                logger.warning(f"⚠️ MNQ trim SKIPPED on acct={acct_id}: broker shows MNQ flat (phantom-trade guard)")
                mnq_ok = True  # not an error — just nothing to do
            else:
                safe_size = min(mnq_size, broker_mnq_size)
                if safe_size < mnq_size:
                    logger.warning(f"⚠️ MNQ trim size clamped {mnq_size}→{safe_size} on acct={acct_id} (broker has {broker_mnq_size})")
                mnq_ok = px_partial_close(acct_id, safe_size, _mnq_contract_id, "MNQ")
        with results_lock:
            results[acct_id] = {"mes": mes_ok, "mnq": mnq_ok}
    
    threads = [threading.Thread(target=close_account, args=(a,)) for a in get_order_routing_accounts()]
    for t in threads: t.start()
    for t in threads: t.join()
    return results

def execute_flatten_all_accounts():
    """Flatten BOTH MES and MNQ on all active accounts in PARALLEL."""
    import threading
    results = {}
    results_lock = threading.Lock()
    
    def flatten_account(acct_id):
        px_cancel_all_stops(acct_id)
        ok = px_flatten_all_contracts(acct_id)
        with results_lock:
            results[acct_id] = ok
    
    threads = [threading.Thread(target=flatten_account, args=(a,)) for a in get_order_routing_accounts()]
    for t in threads: t.start()
    for t in threads: t.join()
    return results

def modify_stop_all_accounts(position, new_mes_size=None, new_mes_stop=None, new_mnq_size=None, new_mnq_stop=None):
    """Modify MES and MNQ stops on order-routing accounts using tracked order IDs.
    Followers are intentionally excluded — their stops cascade via Topstep's copier."""
    mes_states = position.get("mes_account_states", {})
    mnq_states = position.get("mnq_account_states", {})
    results = {}
    side = 1 if position["direction"] == "long" else 0
    
    for acct_id in get_order_routing_accounts():
        acct_key = str(acct_id)
        acct_ok = True
        
        # MES stop
        mes_state = mes_states.get(acct_key, {})
        mes_stop_id = mes_state.get("stop_order_id")
        if new_mes_size is not None or new_mes_stop is not None:
            if mes_stop_id:
                ok = px_modify_stop(acct_id, mes_stop_id, new_size=new_mes_size, new_stop_price=new_mes_stop)
                if not ok:
                    acct_ok = False
            else:
                logger.warning(f"⚠️ No MES stop tracked for acct={acct_id}, placing new")
                sz = new_mes_size or position.get("mes_remaining", MES_CONTRACTS)
                sp = new_mes_stop or position.get("mes_stop_price", 0)
                oid = px_place_stop_order(acct_id, side, sz, sp, _mes_contract_id, "MES")
                if oid:
                    mes_state["stop_order_id"] = oid
                    mes_states[acct_key] = mes_state
                else:
                    acct_ok = False
        
        # MNQ stop
        mnq_state = mnq_states.get(acct_key, {})
        mnq_stop_id = mnq_state.get("stop_order_id")
        if (new_mnq_size is not None or new_mnq_stop is not None) and _mnq_contract_id:
            if mnq_stop_id:
                ok = px_modify_stop(acct_id, mnq_stop_id, new_size=new_mnq_size, new_stop_price=new_mnq_stop)
                if not ok:
                    acct_ok = False
            else:
                logger.warning(f"⚠️ No MNQ stop tracked for acct={acct_id}, placing new")
                sz = new_mnq_size or position.get("mnq_remaining", MNQ_CONTRACTS)
                sp = new_mnq_stop or position.get("mnq_stop_price", 0)
                oid = px_place_stop_order(acct_id, side, sz, sp, _mnq_contract_id, "MNQ")
                if oid:
                    mnq_state["stop_order_id"] = oid
                    mnq_states[acct_key] = mnq_state
                else:
                    acct_ok = False
        
        results[acct_id] = acct_ok
    
    position["mes_account_states"] = mes_states
    position["mnq_account_states"] = mnq_states
    return results

def place_runner_limits_all_accounts(position):
    """Place limit take-profit orders for the runner (1 MES + 1 MNQ) on all accounts."""
    direction = position["direction"]
    mes_entry = position.get("mes_entry_price", position["entry_price"])
    mnq_entry = position.get("mnq_entry_price", 0)
    
    # Calculate target prices
    if direction == "long":
        mes_target = round(mes_entry + MES_RUNNER_AUTO_CLOSE_POINTS, 2)
        mnq_target = round(mnq_entry + MNQ_RUNNER_AUTO_CLOSE_POINTS, 2) if mnq_entry else 0
        limit_side = 1  # Sell to close long
    else:
        mes_target = round(mes_entry - MES_RUNNER_AUTO_CLOSE_POINTS, 2)
        mnq_target = round(mnq_entry - MNQ_RUNNER_AUTO_CLOSE_POINTS, 2) if mnq_entry else 0
        limit_side = 0  # Buy to close short
    
    logger.info(f"📈 RUNNER LIMIT ORDERS: MES target @ {mes_target}, MNQ target @ {mnq_target}")
    
    runner_limits = {}
    for acct_id in get_order_routing_accounts():
        mes_oid = px_place_limit_order(acct_id, limit_side, 1, mes_target, _mes_contract_id, "MES")
        mnq_oid = None
        if MNQ_CONTRACTS > 0 and mnq_entry and _mnq_contract_id:
            mnq_oid = px_place_limit_order(acct_id, limit_side, 1, mnq_target, _mnq_contract_id, "MNQ")
        runner_limits[str(acct_id)] = {"mes": mes_oid, "mnq": mnq_oid}
        logger.info(f"📈 Runner limits placed: acct={acct_id} MES={mes_oid} MNQ={mnq_oid}")
    
    position["runner_limit_orders"] = runner_limits
    position["mes_runner_target"] = mes_target
    position["mnq_runner_target"] = mnq_target
    save_position_state()
    return runner_limits


def cancel_runner_limits(position):
    """Cancel all tracked runner limit orders across all accounts."""
    runner_limits = position.get("runner_limit_orders", {})
    if not runner_limits:
        return
    cancelled = 0
    for acct_id_str, orders in runner_limits.items():
        acct_id = int(acct_id_str)
        if orders.get("mes"):
            px_cancel_order(acct_id, orders["mes"])
            cancelled += 1
        if orders.get("mnq"):
            px_cancel_order(acct_id, orders["mnq"])
            cancelled += 1
    position["runner_limit_orders"] = {}
    logger.info(f"🚫 Cancelled {cancelled} runner limit orders")


# ================= INDEPENDENT TP LIMIT ORDERS =================
def place_tp_limits_all_accounts(position):
    """Place broker-side limit take-profit orders for the FULL position
    (all MES_CONTRACTS + all MNQ_CONTRACTS) at entry+MES_TP_POINTS / entry+MNQ_TP_POINTS.
    Each leg fills independently — MNQ can take profit while MES keeps running, and
    vice versa. The Discord trim alert remains as a safety override.
    Stores order IDs in position['tp_limit_orders'] = {acct_id_str: {'mes': oid, 'mnq': oid}}."""
    direction = position["direction"]
    mes_entry = position.get("mes_entry_price", position.get("entry_price", 0))
    mnq_entry = position.get("mnq_entry_price", 0)

    if direction == "long":
        mes_target = round(mes_entry + MES_TP_POINTS, 2)
        mnq_target = round(mnq_entry + MNQ_TP_POINTS, 2) if mnq_entry else 0
        limit_side = 1  # Sell to close long
    else:
        mes_target = round(mes_entry - MES_TP_POINTS, 2)
        mnq_target = round(mnq_entry - MNQ_TP_POINTS, 2) if mnq_entry else 0
        limit_side = 0  # Buy to close short

    mes_size = position.get("mes_remaining", MES_CONTRACTS)
    mnq_size = position.get("mnq_remaining", MNQ_CONTRACTS)

    logger.info(f"🎯 INDEPENDENT TP LIMITS: MES {mes_size}@{mes_target} (+{MES_TP_POINTS}pts), "
                f"MNQ {mnq_size}@{mnq_target} (+{MNQ_TP_POINTS}pts)")

    tp_limits = {}
    for acct_id in get_order_routing_accounts():
        mes_oid = px_place_limit_order(acct_id, limit_side, mes_size, mes_target,
                                        _mes_contract_id, "MES-TP") if mes_size > 0 else None
        mnq_oid = None
        if mnq_entry and _mnq_contract_id and mnq_size > 0:
            mnq_oid = px_place_limit_order(acct_id, limit_side, mnq_size, mnq_target,
                                            _mnq_contract_id, "MNQ-TP")
        tp_limits[str(acct_id)] = {"mes": mes_oid, "mnq": mnq_oid}
        logger.info(f"🎯 TP limits placed: acct={acct_id} MES={mes_oid} MNQ={mnq_oid}")

    position["tp_limit_orders"] = tp_limits
    position["mes_tp_target"] = mes_target
    position["mnq_tp_target"] = mnq_target
    save_position_state()
    return tp_limits


def cancel_tp_limits(position):
    """Cancel all tracked independent-TP limit orders across all accounts.
    Idempotent — safe to call when TPs are already filled or never placed."""
    tp_limits = position.get("tp_limit_orders", {})
    if not tp_limits:
        return 0
    cancelled = 0
    for acct_id_str, orders in list(tp_limits.items()):
        try:
            acct_id = int(acct_id_str)
        except (TypeError, ValueError):
            continue
        if orders.get("mes"):
            px_cancel_order(acct_id, orders["mes"])
            cancelled += 1
        if orders.get("mnq"):
            px_cancel_order(acct_id, orders["mnq"])
            cancelled += 1
    position["tp_limit_orders"] = {}
    if cancelled:
        logger.info(f"🚫 Cancelled {cancelled} TP limit orders")
    return cancelled


def _is_order_still_open(account_id, order_id):
    """Return True if order_id appears in account's open orders, False otherwise.
    Used to differentiate TP-filled vs other exits (stop, manual close).
    A TP that filled will NOT appear in open orders."""
    if not order_id:
        return False
    try:
        data = px_api("/api/Order/searchOpen", {"accountId": account_id})
        if not data:
            return False  # API failed — assume not open (safer to trigger catalyst)
        for o in data.get("orders", []):
            if o.get("id") == order_id:
                return True
        return False
    except Exception as e:
        logger.error(f"❌ _is_order_still_open error: {e}")
        return False  # On error, assume not open


def _detect_leg_exit_type(position, leg):
    """For a leg ('mes' or 'mnq') that's been detected flat at the broker,
    determine whether it exited via TP-fill or stop/manual.
    Returns 'tp_filled' if the tracked TP limit is no longer open (filled),
    'stopped_or_other' otherwise."""
    if not INDEPENDENT_TP_ENABLED:
        return "stopped_or_other"
    tp_limits = position.get("tp_limit_orders", {})
    if not tp_limits:
        return "stopped_or_other"
    leader_id = get_active_accounts()[0]
    acct_orders = tp_limits.get(str(leader_id), {})
    leg_oid = acct_orders.get(leg)
    if not leg_oid:
        return "stopped_or_other"
    if _is_order_still_open(leader_id, leg_oid):
        # TP still in book → didn't fill → leg was closed by stop/manual
        return "stopped_or_other"
    # TP not in open orders → filled (or was cancelled externally)
    return "tp_filled"


# ================= 1-1-1 BROKER LADDER =================
def place_ladder_all_accounts(position):
    """Place a true 1-1-1 ladder per leg, per account:
      MES: 1 limit @ entry+5 (T1), 1 limit @ entry+10 (T2), 1 limit @ entry+20 (runner cap)
      MNQ: 1 limit @ entry+20 (T1), 1 limit @ entry+40 (T2), 1 limit @ entry+60 (runner cap)
    Position stops are placed separately by place_stop_for_position().
    Each leg is fully independent: MES catalyst rule is dropped.
    Stores order IDs in position['ladder_orders'] = {acct_id_str: {'mes': {'t1','t2','runner'}, 'mnq': {...}}}.
    Initialises position['ladder_state'] tracking phase per leg."""
    direction = position["direction"]
    mes_entry = position.get("mes_entry_price", position.get("entry_price", 0))
    mnq_entry = position.get("mnq_entry_price", 0)

    if direction == "long":
        mes_t1 = round(mes_entry + MES_T1_POINTS, 2)
        mes_t2 = round(mes_entry + MES_T2_POINTS, 2)
        mes_runner = round(mes_entry + MES_RUNNER_AUTO_CLOSE_POINTS, 2)
        mnq_t1 = round(mnq_entry + MNQ_T1_POINTS, 2) if mnq_entry else 0
        mnq_t2 = round(mnq_entry + MNQ_T2_POINTS, 2) if mnq_entry else 0
        mnq_runner = round(mnq_entry + MNQ_RUNNER_AUTO_CLOSE_POINTS, 2) if mnq_entry else 0
        limit_side = 1  # Sell-to-close long
    else:
        mes_t1 = round(mes_entry - MES_T1_POINTS, 2)
        mes_t2 = round(mes_entry - MES_T2_POINTS, 2)
        mes_runner = round(mes_entry - MES_RUNNER_AUTO_CLOSE_POINTS, 2)
        mnq_t1 = round(mnq_entry - MNQ_T1_POINTS, 2) if mnq_entry else 0
        mnq_t2 = round(mnq_entry - MNQ_T2_POINTS, 2) if mnq_entry else 0
        mnq_runner = round(mnq_entry - MNQ_RUNNER_AUTO_CLOSE_POINTS, 2) if mnq_entry else 0
        limit_side = 0  # Buy-to-close short

    logger.info(f"🪜 LADDER — MES: T1@{mes_t1} (+{MES_T1_POINTS}) / T2@{mes_t2} (+{MES_T2_POINTS}) / R@{mes_runner} (+{MES_RUNNER_AUTO_CLOSE_POINTS})")
    logger.info(f"🪜 LADDER — MNQ: T1@{mnq_t1} (+{MNQ_T1_POINTS}) / T2@{mnq_t2} (+{MNQ_T2_POINTS}) / R@{mnq_runner} (+{MNQ_RUNNER_AUTO_CLOSE_POINTS})")

    # MNQ-only 4-1-1 banner
    logger.info(f"🪶 MNQ size split: T1={MNQ_FIRST_TRIM_CONTRACTS}c / T2={MNQ_SECOND_TRIM_CONTRACTS}c / Runner={MNQ_RUNNER_CONTRACTS}c (total {MNQ_CONTRACTS})")
    if MES_CONTRACTS > 0:
        logger.info(f"🪶 MES size split: T1={MES_FIRST_TRIM_CONTRACTS}c / T2={MES_SECOND_TRIM_CONTRACTS}c / Runner={MES_RUNNER_CONTRACTS}c (total {MES_CONTRACTS})")
    else:
        logger.info("🪶 MES leg DISABLED (MES_CONTRACTS=0)")

    ladder_orders = {}
    for acct_id in get_order_routing_accounts():
        # MES leg — only if MES_CONTRACTS > 0; primitives are also size-guarded
        mes_t1_oid = mes_t2_oid = mes_run_oid = None
        if MES_CONTRACTS > 0:
            mes_t1_oid = px_place_limit_order(acct_id, limit_side, MES_FIRST_TRIM_CONTRACTS, mes_t1, _mes_contract_id, "MES-T1")
            mes_t2_oid = px_place_limit_order(acct_id, limit_side, MES_SECOND_TRIM_CONTRACTS, mes_t2, _mes_contract_id, "MES-T2")
            mes_run_oid = px_place_limit_order(acct_id, limit_side, MES_RUNNER_CONTRACTS, mes_runner, _mes_contract_id, "MES-RUN")
        # MNQ leg — sizes per 4-1-1 (or whatever MNQ_*_CONTRACTS dictates)
        # GUARD: MNQ_CONTRACTS must be > 0 AND have a valid entry price + contract ID
        mnq_t1_oid = mnq_t2_oid = mnq_run_oid = None
        if MNQ_CONTRACTS > 0 and mnq_entry and _mnq_contract_id:
            mnq_t1_oid = px_place_limit_order(acct_id, limit_side, MNQ_FIRST_TRIM_CONTRACTS, mnq_t1, _mnq_contract_id, "MNQ-T1")
            mnq_t2_oid = px_place_limit_order(acct_id, limit_side, MNQ_SECOND_TRIM_CONTRACTS, mnq_t2, _mnq_contract_id, "MNQ-T2")
            mnq_run_oid = px_place_limit_order(acct_id, limit_side, MNQ_RUNNER_CONTRACTS, mnq_runner, _mnq_contract_id, "MNQ-RUN")
        ladder_orders[str(acct_id)] = {
            "mes": {"t1": mes_t1_oid, "t2": mes_t2_oid, "runner": mes_run_oid},
            "mnq": {"t1": mnq_t1_oid, "t2": mnq_t2_oid, "runner": mnq_run_oid},
        }
        logger.info(f"🪜 Ladder placed acct={acct_id} — MES(T1={mes_t1_oid}, T2={mes_t2_oid}, R={mes_run_oid}) MNQ(T1={mnq_t1_oid}, T2={mnq_t2_oid}, R={mnq_run_oid})")

    position["ladder_orders"] = ladder_orders
    position["ladder_targets"] = {
        "mes": {"t1": mes_t1, "t2": mes_t2, "runner": mes_runner},
        "mnq": {"t1": mnq_t1, "t2": mnq_t2, "runner": mnq_runner},
    }
    # MNQ-only mode: pre-mark MES leg as runner_done so size-monitor doesn't fire
    # spurious 'MES runner done' events from a leg that was never placed.
    position["ladder_state"] = {
        "mes": {"phase": 0 if MES_CONTRACTS == 0 else 1, "t1_filled": MES_CONTRACTS == 0,
                "t2_filled": MES_CONTRACTS == 0, "runner_done": MES_CONTRACTS == 0,
                "last_seen_size": MES_CONTRACTS},
        "mnq": {"phase": 1, "t1_filled": False, "t2_filled": False, "runner_done": False,
                "last_seen_size": MNQ_CONTRACTS},
    }
    save_position_state()
    return ladder_orders


def cancel_ladder(position, leg=None):
    """Cancel ladder limit orders. If leg='mes' or 'mnq', cancel only that leg.
    If leg=None, cancel all. Idempotent."""
    ladder_orders = position.get("ladder_orders", {})
    if not ladder_orders:
        return 0
    cancelled = 0
    legs = [leg] if leg else ["mes", "mnq"]
    for acct_id_str, by_leg in list(ladder_orders.items()):
        try:
            acct_id = int(acct_id_str)
        except (TypeError, ValueError):
            continue
        for L in legs:
            leg_orders = by_leg.get(L, {})
            for slot in ("t1", "t2", "runner"):
                oid = leg_orders.get(slot)
                if oid:
                    px_cancel_order(acct_id, oid)
                    cancelled += 1
                    leg_orders[slot] = None
    if cancelled:
        logger.info(f"🚫 Cancelled {cancelled} ladder limit orders (legs={legs})")
    return cancelled


def _ladder_handle_t1_fill(position, leg):
    """T1 just filled on leg. Tighten the position stop to the post-T1 level
    on the remaining 2 contracts.
    MES: stop tightens from -11 to -7. MNQ: stop stays at -40."""
    state = position.get("ladder_state", {}).get(leg, {})
    if state.get("t1_filled"):
        return  # idempotent
    state["t1_filled"] = True
    state["phase"] = 2
    position["ladder_state"][leg] = state
    direction = position["direction"]
    if leg == "mes":
        entry = position.get("mes_entry_price", position.get("entry_price", 0))
        new_stop = round(entry - MES_FIRST_TRIM_STOP_POINTS, 2) if direction == "long" else round(entry + MES_FIRST_TRIM_STOP_POINTS, 2)
        # Modify MES stop on remaining 2 contracts (Topstep)
        modify_stop_all_accounts(position, new_mes_size=2, new_mes_stop=new_stop)
        position["mes_remaining"] = 2
        position["mes_stop_price"] = new_stop
        # Mirror onto Apex (dispatches based on feature flag: ladder mode = stop modify only,
        # mirror mode = market trim + stop modify; legacy = no-op)
        try:
            rt_apex_handle_t_fill(position, "mes", 2, new_stop)
        except Exception as e:
            logger.error(f"❌ Apex MES T1 mirror error: {e}")
        logger.info(f"🪜 MES T1 FILLED — stop tightened to {new_stop} (-{MES_FIRST_TRIM_STOP_POINTS}pt) on 2 remaining")
        queue_notification("MES_T1_FILLED",
            f"🎯 MES T1 FILLED @ {position['ladder_targets']['mes']['t1']} (+{MES_T1_POINTS}pts)\n"
            f"Stop tightened to {new_stop} on 2 remaining contracts.")
    elif leg == "mnq":
        entry = position.get("mnq_entry_price", 0)
        if entry:
            new_stop = round(entry - MNQ_FIRST_TRIM_STOP_POINTS, 2) if direction == "long" else round(entry + MNQ_FIRST_TRIM_STOP_POINTS, 2)
            modify_stop_all_accounts(position, new_mnq_size=2, new_mnq_stop=new_stop)
            position["mnq_remaining"] = 2
            position["mnq_stop_price"] = new_stop
            try:
                rt_apex_handle_t_fill(position, "mnq", 2, new_stop)
            except Exception as e:
                logger.error(f"❌ Apex MNQ T1 mirror error: {e}")
            logger.info(f"🪜 MNQ T1 FILLED — stop maintained @ {new_stop} on 2 remaining")
            queue_notification("MNQ_T1_FILLED",
                f"🎯 MNQ T1 FILLED @ {position['ladder_targets']['mnq']['t1']} (+{MNQ_T1_POINTS}pts)\n"
                f"Stop @ {new_stop} on 2 remaining contracts.")
            # Log T1 fills for all routing accounts
            t1_exit = position['ladder_targets']['mnq']['t1']
            for acct_id in get_order_routing_accounts():
                log_trade(acct_id, "MNQ", direction, entry, t1_exit, 
                          MNQ_FIRST_TRIM_CONTRACTS, "T1", "tp_filled", position.get("key", ""))
    save_position_state()


def _ladder_handle_t2_fill(position, leg):
    """T2 just filled on leg. Move the runner stop to breakeven minus offset.
    MES: BE-3. MNQ: BE-21."""
    state = position.get("ladder_state", {}).get(leg, {})
    if state.get("t2_filled"):
        return
    state["t2_filled"] = True
    state["phase"] = 3
    position["ladder_state"][leg] = state
    direction = position["direction"]
    if leg == "mes":
        entry = position.get("mes_entry_price", position.get("entry_price", 0))
        be_stop = round(entry - MES_RUNNER_BREAKEVEN_OFFSET, 2) if direction == "long" else round(entry + MES_RUNNER_BREAKEVEN_OFFSET, 2)
        modify_stop_all_accounts(position, new_mes_size=1, new_mes_stop=be_stop)
        position["mes_remaining"] = 1
        position["mes_stop_price"] = be_stop
        try:
            rt_apex_handle_t_fill(position, "mes", 1, be_stop)
        except Exception as e:
            logger.error(f"❌ Apex MES T2 mirror error: {e}")
        logger.info(f"🪜 MES T2 FILLED — runner stop → BE-{MES_RUNNER_BREAKEVEN_OFFSET} @ {be_stop}")
        queue_notification("MES_T2_FILLED",
            f"🎯 MES T2 FILLED @ {position['ladder_targets']['mes']['t2']} (+{MES_T2_POINTS}pts)\n"
            f"Runner @ BE-{MES_RUNNER_BREAKEVEN_OFFSET} stop {be_stop} — risk-free, target +{MES_RUNNER_AUTO_CLOSE_POINTS}")
    elif leg == "mnq":
        entry = position.get("mnq_entry_price", 0)
        if entry:
            be_stop = round(entry - MNQ_RUNNER_BREAKEVEN_OFFSET, 2) if direction == "long" else round(entry + MNQ_RUNNER_BREAKEVEN_OFFSET, 2)
            modify_stop_all_accounts(position, new_mnq_size=1, new_mnq_stop=be_stop)
            position["mnq_remaining"] = 1
            position["mnq_stop_price"] = be_stop
            try:
                rt_apex_handle_t_fill(position, "mnq", 1, be_stop)
            except Exception as e:
                logger.error(f"❌ Apex MNQ T2 mirror error: {e}")
            logger.info(f"🪜 MNQ T2 FILLED — runner stop → BE-{MNQ_RUNNER_BREAKEVEN_OFFSET} @ {be_stop}")
            queue_notification("MNQ_T2_FILLED",
                f"🎯 MNQ T2 FILLED @ {position['ladder_targets']['mnq']['t2']} (+{MNQ_T2_POINTS}pts)\n"
                f"Runner @ BE-{MNQ_RUNNER_BREAKEVEN_OFFSET} stop {be_stop} — risk-free, target +{MNQ_RUNNER_AUTO_CLOSE_POINTS}")
            # Log T2 fills
            t2_exit = position['ladder_targets']['mnq']['t2']
            for acct_id in get_order_routing_accounts():
                log_trade(acct_id, "MNQ", direction, entry, t2_exit, 
                          MNQ_SECOND_TRIM_CONTRACTS, "T2", "tp_filled", position.get("key", ""))
    save_position_state()


def _ladder_handle_runner_done(position, leg, exit_type):
    """Runner of leg has been closed (TP fill, stop hit, or other).
    exit_type ∈ {'tp_filled', 'stopped_or_other', 'eod_flatten'}"""
    state = position.get("ladder_state", {}).get(leg, {})
    if state.get("runner_done"):
        return
    state["runner_done"] = True
    state["phase"] = 0
    position["ladder_state"][leg] = state
    if leg == "mes":
        position["mes_remaining"] = 0
    else:
        position["mnq_remaining"] = 0
    # Cancel any leftover orders for this leg (its own stop, leftover ladder limits)
    cancel_ladder(position, leg=leg)
    # Cancel leftover stop too
    leg_states = position.get(f"{leg}_account_states", {})
    for acct_id in get_order_routing_accounts():
        ls = leg_states.get(str(acct_id), {})
        if ls.get("stop_order_id"):
            px_cancel_order(acct_id, ls["stop_order_id"])
            ls["stop_order_id"] = None
    label = leg.upper()
    target = position.get("ladder_targets", {}).get(leg, {}).get("runner", "N/A")
    if exit_type == "tp_filled":
        msg = f"🎯 {label} RUNNER TP FILLED @ {target}"
    elif exit_type == "eod_flatten":
        msg = f"🌆 {label} RUNNER FLATTENED at EOD"
    else:
        msg = f"🔴 {label} RUNNER STOPPED OUT (or manually closed)"
    logger.info(msg)
    queue_notification(f"{label}_RUNNER_DONE", msg)
    
    # Log runner close
    entry = position.get(f"{leg}_entry_price", position.get("entry_price", 0))
    direction = position["direction"]
    
    # Determine exit price and contracts
    if exit_type == "tp_filled":
        exit_price = position.get("ladder_targets", {}).get(leg, {}).get("runner", entry)
    else:
        # Stopped out - use stop price
        exit_price = position.get(f"{leg}_stop_price", entry)
    
    # Determine how many contracts closed
    # If T1 already filled, we closed those earlier, now closing the rest
    # If T1 never filled, we're closing the full position at stop
    if leg == "mnq":
        if state.get("t1_filled"):
            if state.get("t2_filled"):
                contracts_closed = MNQ_RUNNER_CONTRACTS
            else:
                # T1 filled but not T2 - so T2+runner both stopped
                contracts_closed = MNQ_SECOND_TRIM_CONTRACTS + MNQ_RUNNER_CONTRACTS
        else:
            # Full position stopped before T1
            contracts_closed = MNQ_CONTRACTS
    else:  # MES
        if state.get("t1_filled"):
            if state.get("t2_filled"):
                contracts_closed = MES_RUNNER_CONTRACTS
            else:
                contracts_closed = MES_SECOND_TRIM_CONTRACTS + MES_RUNNER_CONTRACTS
        else:
            contracts_closed = MES_CONTRACTS
    
    level_label = "RUNNER" if state.get("t2_filled") else ("T2+R" if state.get("t1_filled") else "FULL_STOP")
    
    for acct_id in get_order_routing_accounts():
        log_trade(acct_id, leg.upper(), direction, entry, exit_price,
                  contracts_closed, level_label, exit_type, position.get("key", ""))

    # Mirror Topstep's runner exit onto Apex immediately (was: relied on sync_check delay)
    # tp_filled -> Topstep TP cap hit, Apex still long with BE-stop -> close at market now
    # stopped_or_other -> Topstep stop fired, Apex stop should fire too but exit defensively
    # eod_flatten -> EOD flatten path also fires rt_flatten elsewhere; this is belt+suspenders
    try:
        rt_apex_close_runner_leg(position, leg, exit_type=exit_type)
    except Exception as e:
        logger.error(f"❌ Apex runner-close mirror error for {leg}: {e}")

    # If both legs are done, close the position
    other = "mnq" if leg == "mes" else "mes"
    other_state = position.get("ladder_state", {}).get(other, {})
    if other_state.get("runner_done"):
        logger.info("🪜 Both legs complete — closing position")
        cancel_ladder(position)
        cancel_stops_all_accounts(position)
        close_position(position, "ladder_complete")
    save_position_state()


def cancel_stops_all_accounts(position):
    """Cancel tracked MES and MNQ stop orders on order-routing accounts.
    Followers excluded — their cancel cascades via Topstep's copier when leader cancels."""
    mes_states = position.get("mes_account_states", {})
    mnq_states = position.get("mnq_account_states", {})
    for acct_id in get_order_routing_accounts():
        acct_key = str(acct_id)
        # MES
        mes_state = mes_states.get(acct_key, {})
        if mes_state.get("stop_order_id"):
            px_cancel_order(acct_id, mes_state["stop_order_id"])
            mes_state["stop_order_id"] = None
        # MNQ
        mnq_state = mnq_states.get(acct_key, {})
        if mnq_state.get("stop_order_id"):
            px_cancel_order(acct_id, mnq_state["stop_order_id"])
            mnq_state["stop_order_id"] = None
        # Fallback: cancel everything for this account if no tracked IDs
        if not mes_state.get("stop_order_id") and not mnq_state.get("stop_order_id"):
            px_cancel_all_stops(acct_id)

# ================= TRADOVATE EXECUTION HELPERS =================
def tv_entry(direction, contracts, symbol=None):
    """Place entry on Tradovate. Returns orderId or None."""
    if not TRADOVATE_ENABLED:
        return None
    side = "Buy" if direction == "long" else "Sell"
    return tv_place_market_order(side, contracts, symbol=symbol)

def tv_stop(direction, contracts, stop_price, symbol=None):
    """Place stop on Tradovate. Returns orderId or None."""
    if not TRADOVATE_ENABLED:
        return None
    side = "Sell" if direction == "long" else "Buy"
    return tv_place_stop_order(side, contracts, stop_price, symbol=symbol)

def tv_trim(direction, contracts, symbol=None):
    """Partial close (trim) on Tradovate."""
    if not TRADOVATE_ENABLED:
        return True
    return tv_partial_close(contracts, direction, symbol=symbol)

# ================= RITHMIC/APEX EXECUTION HELPERS =================
def _rt_summarize_result(result, label):
    """Inspect a Rithmic per-account result dict. Returns (ok_count, fail_count, ok_bool).

    Classification:
    - whole-call None: ambiguous (asyncio loop timeout) — orders MAY have gone through.
    - dict with mixed values: partial fill, some succeeded.
    - dict with all-None/all-Exception values: per-account WS-send failures, orders definitely
      did NOT reach Rithmic — safe to retry after reconnect.
    """
    if result is None:
        logger.error(f"❌ Rithmic {label}: returned None (loop timeout — orders may have leaked through)")
        return (0, 0, False)
    if not isinstance(result, dict):
        logger.error(f"❌ Rithmic {label}: unexpected result type {type(result).__name__}: {result}")
        return (0, 0, False)
    ok, fail = 0, 0
    for acct, r in result.items():
        if isinstance(r, Exception):
            logger.error(f"❌ Rithmic {label} [{acct}]: exception {type(r).__name__}: {r}")
            fail += 1
        elif r is None or r is False:
            logger.error(f"❌ Rithmic {label} [{acct}]: returned {r}")
            fail += 1
        else:
            ok += 1
    return (ok, fail, ok > 0)

def _rt_is_safe_to_retry(result):
    """Determine if a failed result is safe to retry without risk of double-fill.

    Safe to retry: result is a dict and ALL values are None/False/Exception.
      → each per-account ws.send() raised, orders definitely did NOT reach Rithmic.
    NOT safe: whole-call None (loop timeout, ambiguous) or partial success.
    """
    if result is None or not isinstance(result, dict) or not result:
        return False
    for r in result.values():
        if isinstance(r, Exception):
            continue
        if r is None or r is False:
            continue
        # Any truthy non-Exception value = success on that account = NOT safe to retry
        return False
    return True  # All accounts had per-account failures

def _rt_kick_reconnect_and_wait(timeout_seconds=3.0):
    """If the bridge looks disconnected, force the auto-reconnect loop and wait briefly."""
    if not RITHMIC_ENABLED or not _rithmic_bridge:
        return False
    if _rithmic_bridge.connected:
        return True
    # Force the run() loop to enter its reconnect branch by clearing the stale flag.
    try:
        _rithmic_bridge.client._connected = False
    except Exception as e:
        logger.error(f"❌ Rithmic: failed to kick reconnect: {e}")
        return False
    logger.info(f"🔄 Rithmic: waiting up to {timeout_seconds}s for reconnect...")
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if _rithmic_bridge.connected:
            logger.info("✅ Rithmic: reconnect successful")
            return True
        time.sleep(0.25)
    logger.error(f"❌ Rithmic: still disconnected after {timeout_seconds}s")
    return False

def rt_entry(direction, contracts, symbol=None):
    """Place entry on all Rithmic/Apex accounts. Returns dict or None.

    Recovery: if bridge looks disconnected, kick reconnect and wait up to 3s.
    Retry: if all per-account ws.send()s failed (definitely no order in flight),
    retry once after reconnect. NEVER retry on whole-call None (loop timeout) or
    partial success — those have double-fill risk.
    """
    if not RITHMIC_ENABLED:
        return None
    if contracts is None or contracts <= 0:
        return None  # MNQ-only mode: skip MES leg silently
    sym = symbol or RITHMIC_MES_CONTRACT
    side = "BUY" if direction == "long" else "SELL"

    # Pre-flight: ensure connection is healthy (or wait briefly for reconnect)
    if not _rithmic_bridge:
        logger.error(f"❌ Rithmic bridge NOT INITIALIZED — entry SKIPPED for {sym} {side} x{contracts}")
        return None
    if not _rithmic_bridge.connected:
        logger.warning(f"⚠️ Rithmic appears disconnected before entry — attempting recovery for {sym} {side} x{contracts}")
        if not _rt_kick_reconnect_and_wait(timeout_seconds=3.0):
            logger.error(f"❌ Rithmic NOT CONNECTED — entry SKIPPED for {sym} {side} x{contracts}")
            return None

    logger.info(f"📤 Rithmic entry: {sym} {side} x{contracts}")
    try:
        result = _rithmic_bridge.place_market_order_all(sym, RITHMIC_EXCHANGE, side, contracts)
        ok_count, fail_count, ok = _rt_summarize_result(result, f"entry {sym} {side}")
        if ok:
            logger.info(f"✅ Rithmic entry {sym} {side} x{contracts}: {ok_count} ok / {fail_count} fail")
            return result
        # Failure path — evaluate retry safety
        if _rt_is_safe_to_retry(result):
            logger.warning(f"⚠️ Rithmic entry: all {fail_count} accounts failed at WS-send — reconnecting and retrying ONCE")
            if _rt_kick_reconnect_and_wait(timeout_seconds=3.0):
                logger.info(f"🔁 Rithmic entry RETRY: {sym} {side} x{contracts}")
                retry_result = _rithmic_bridge.place_market_order_all(sym, RITHMIC_EXCHANGE, side, contracts)
                rok, rfail, rok_bool = _rt_summarize_result(retry_result, f"entry-retry {sym} {side}")
                if rok_bool:
                    logger.info(f"✅ Rithmic entry RETRY succeeded: {rok} ok / {rfail} fail")
                else:
                    logger.error(f"❌ Rithmic entry RETRY failed: {rok} ok / {rfail} fail")
                return retry_result
            else:
                logger.error("❌ Rithmic entry: cannot retry, reconnect failed")
        else:
            logger.error("❌ Rithmic entry: NOT retrying — either loop-timeout (ambiguous, possible leak) or partial fill (would double-up)")
        return result
    except Exception as e:
        logger.error(f"❌ Rithmic entry error: {e}")
        return None

def rt_stop(direction, contracts, stop_price, symbol=None):
    """Place stop on all Rithmic/Apex accounts."""
    if not RITHMIC_ENABLED:
        return None
    if contracts is None or contracts <= 0:
        return None
    if not _rithmic_bridge or not _rithmic_bridge.connected:
        sym = symbol or RITHMIC_MES_CONTRACT
        logger.error(f"❌ Rithmic NOT CONNECTED — stop SKIPPED for {sym} @ {stop_price}")
        return None
    sym = symbol or RITHMIC_MES_CONTRACT
    side = "SELL" if direction == "long" else "BUY"
    logger.info(f"📄 Rithmic stop: {sym} {side} x{contracts} @ {stop_price}")
    try:
        result = _rithmic_bridge.place_stop_order_all(sym, RITHMIC_EXCHANGE, side, contracts, stop_price)
        ok_count, fail_count, _ = _rt_summarize_result(result, f"stop {sym} {side} @ {stop_price}")
        if ok_count > 0:
            logger.info(f"✅ Rithmic stop {sym} @ {stop_price}: {ok_count} ok / {fail_count} fail")
        return result
    except Exception as e:
        logger.error(f"❌ Rithmic stop error: {e}")
        return None

def rt_limit(direction, contracts, limit_price, symbol=None):
    """Place limit TP on all Rithmic/Apex accounts."""
    if not RITHMIC_ENABLED:
        return None
    if contracts is None or contracts <= 0:
        return None
    if not _rithmic_bridge or not _rithmic_bridge.connected:
        sym = symbol or RITHMIC_MES_CONTRACT
        logger.error(f"❌ Rithmic NOT CONNECTED — limit SKIPPED for {sym} @ {limit_price}")
        return None
    sym = symbol or RITHMIC_MES_CONTRACT
    # Limit to take profit: sell if long, buy if short
    side = "SELL" if direction == "long" else "BUY"
    logger.info(f"🎯 Rithmic limit TP: {sym} {side} x{contracts} @ {limit_price}")
    try:
        result = _rithmic_bridge.place_limit_order_all(sym, RITHMIC_EXCHANGE, side, contracts, limit_price)
        ok_count, fail_count, _ = _rt_summarize_result(result, f"limit {sym} {side} @ {limit_price}")
        if ok_count > 0:
            logger.info(f"✅ Rithmic limit {sym} @ {limit_price}: {ok_count} ok / {fail_count} fail")
        return result
    except Exception as e:
        logger.error(f"❌ Rithmic limit error: {e}")
        return None

def rt_flatten():
    """Flatten all positions and cancel all orders on all Rithmic/Apex accounts."""
    if not RITHMIC_ENABLED or not _rithmic_bridge or not _rithmic_bridge.connected:
        return None
    try:
        return _rithmic_bridge.flatten_all(RITHMIC_MES_CONTRACT, RITHMIC_MNQ_CONTRACT, RITHMIC_EXCHANGE)
    except Exception as e:
        logger.error(f"❌ Rithmic flatten error: {e}")
        return None

def rt_cancel_all():
    """Cancel all orders on all Rithmic/Apex accounts."""
    if not RITHMIC_ENABLED or not _rithmic_bridge or not _rithmic_bridge.connected:
        return None
    try:
        return _rithmic_bridge.cancel_all_orders_all()
    except Exception as e:
        logger.error(f"❌ Rithmic cancel_all error: {e}")
        return None

def rt_place_apex_ladder(position):
    """Place full 1-1-1 ladder on Apex/Rithmic accounts mirroring Topstep:
      - cancels any existing Apex orders for safety
      - places stop on each account (size 3, captures basket_id for later modify)
      - places T1, T2, runner-cap limits on each account (size 1 each)

    Stop basket_ids stored in position['rt_stop_baskets'][leg][acct] for later
    rt_modify_apex_stop() calls on T1/T2 fill events.
    """
    if not (RITHMIC_ENABLED and RITHMIC_LADDER_ENABLED):
        return
    if not _rithmic_bridge or not _rithmic_bridge.connected:
        logger.error("❌ Rithmic LADDER: bridge not connected, skipping ladder placement")
        return

    direction = position["direction"]
    side_exit = "SELL" if direction == "long" else "BUY"
    mes_size = position.get("mes_remaining", MES_CONTRACTS)
    mnq_size = position.get("mnq_remaining", MNQ_CONTRACTS)
    mes_stop = position.get("mes_stop_price")
    mnq_stop = position.get("mnq_stop_price")

    targets = position.get("ladder_targets", {})
    mes_targets = targets.get("mes", {})
    mnq_targets = targets.get("mnq", {})
    mes_t1 = mes_targets.get("t1")
    mes_t2 = mes_targets.get("t2")
    mes_runner = mes_targets.get("runner")
    mnq_t1 = mnq_targets.get("t1")
    mnq_t2 = mnq_targets.get("t2")
    mnq_runner = mnq_targets.get("runner")

    if not (mes_t1 and mes_t2 and mes_runner and mes_stop):
        logger.error(f"❌ Apex LADDER: missing MES targets/stop — cannot place (t1={mes_t1} t2={mes_t2} r={mes_runner} stop={mes_stop})")
        return

    # Clear any pre-existing Apex orders so the ladder is the only thing resting.
    rt_cancel_all()

    # Initialize basket-ID tracking on the position
    if "rt_stop_baskets" not in position:
        position["rt_stop_baskets"] = {"mes": {}, "mnq": {}}

    accounts = list(_rithmic_bridge.client.accounts)
    logger.info(f"🪜 Apex LADDER: placing {mes_size} MES + {mnq_size} MNQ ladders on {len(accounts)} accounts")

    for acct in accounts:
        # ---- MES leg ----
        try:
            mes_stop_basket = _rithmic_bridge.place_stop_order(
                acct, RITHMIC_MES_CONTRACT, RITHMIC_EXCHANGE, side_exit, mes_size, mes_stop
            )
            position["rt_stop_baskets"]["mes"][acct] = mes_stop_basket
            logger.info(f"  ✅ [{acct}] MES stop x{mes_size} @ {mes_stop} basket={mes_stop_basket}")
        except Exception as e:
            logger.error(f"  ❌ [{acct}] MES stop error: {e}")
            position["rt_stop_baskets"]["mes"][acct] = None

        try:
            _rithmic_bridge.place_limit_order(acct, RITHMIC_MES_CONTRACT, RITHMIC_EXCHANGE, side_exit, 1, mes_t1)
            _rithmic_bridge.place_limit_order(acct, RITHMIC_MES_CONTRACT, RITHMIC_EXCHANGE, side_exit, 1, mes_t2)
            _rithmic_bridge.place_limit_order(acct, RITHMIC_MES_CONTRACT, RITHMIC_EXCHANGE, side_exit, 1, mes_runner)
            logger.info(f"  ✅ [{acct}] MES limits T1@{mes_t1}, T2@{mes_t2}, R@{mes_runner}")
        except Exception as e:
            logger.error(f"  ❌ [{acct}] MES limits error: {e}")

        # ---- MNQ leg ----
        if MNQ_CONTRACTS > 0 and mnq_size and mnq_stop and mnq_t1 and mnq_t2 and mnq_runner:
            try:
                mnq_stop_basket = _rithmic_bridge.place_stop_order(
                    acct, RITHMIC_MNQ_CONTRACT, RITHMIC_EXCHANGE, side_exit, mnq_size, mnq_stop
                )
                position["rt_stop_baskets"]["mnq"][acct] = mnq_stop_basket
                logger.info(f"  ✅ [{acct}] MNQ stop x{mnq_size} @ {mnq_stop} basket={mnq_stop_basket}")
            except Exception as e:
                logger.error(f"  ❌ [{acct}] MNQ stop error: {e}")
                position["rt_stop_baskets"]["mnq"][acct] = None
            try:
                _rithmic_bridge.place_limit_order(acct, RITHMIC_MNQ_CONTRACT, RITHMIC_EXCHANGE, side_exit, 1, mnq_t1)
                _rithmic_bridge.place_limit_order(acct, RITHMIC_MNQ_CONTRACT, RITHMIC_EXCHANGE, side_exit, 1, mnq_t2)
                _rithmic_bridge.place_limit_order(acct, RITHMIC_MNQ_CONTRACT, RITHMIC_EXCHANGE, side_exit, 1, mnq_runner)
                logger.info(f"  ✅ [{acct}] MNQ limits T1@{mnq_t1}, T2@{mnq_t2}, R@{mnq_runner}")
            except Exception as e:
                logger.error(f"  ❌ [{acct}] MNQ limits error: {e}")

    # Persist the basket_id map so a restart mid-trade doesn't lose tracking.
    try:
        save_position_state()
    except Exception:
        pass


def _rt_capture_stop_baskets(position, rt_stop_result, leg):
    """After rt_stop() places per-account stops, store basket_ids in position for
    later modification by rt_modify_apex_stop / rt_apex_trim_and_tighten.
    Idempotent: overwrites prior baskets for the same leg.
    """
    if not isinstance(rt_stop_result, dict):
        return
    if "rt_stop_baskets" not in position:
        position["rt_stop_baskets"] = {"mes": {}, "mnq": {}}
    elif leg not in position["rt_stop_baskets"]:
        position["rt_stop_baskets"][leg] = {}
    captured, missed = 0, 0
    for acct, basket in rt_stop_result.items():
        if basket and not isinstance(basket, Exception) and basket != "submitted":
            position["rt_stop_baskets"][leg][acct] = basket
            captured += 1
        else:
            position["rt_stop_baskets"][leg][acct] = None
            missed += 1
    logger.info(f"🔖 Apex {leg.upper()} stop basket_ids: captured {captured}, missed {missed}")


def rt_apex_trim_and_tighten(position, leg, new_size, new_stop_price):
    """MIRROR mode: when Topstep T1/T2 fills, mirror onto Apex via:
      1. Market trim of 1 contract (opposite of position direction)
      2. Modify the resting stop to new_size at new_stop_price (using basket_id)

    Why this order: the broker accepts the trim before the stop modify, so even if
    the stop modify fails the position is at least the right size for the OLD stop.
    Slight slippage on market trim (~1-2 ticks) is the cost of avoiding race conditions.
    """
    if not (RITHMIC_ENABLED and RITHMIC_MIRROR_ENABLED):
        return
    if not _rithmic_bridge or not _rithmic_bridge.connected:
        logger.error(f"❌ Apex MIRROR {leg.upper()}: bridge not connected")
        return

    direction = position["direction"]
    trim_side = "SELL" if direction == "long" else "BUY"
    symbol = RITHMIC_MES_CONTRACT if leg == "mes" else RITHMIC_MNQ_CONTRACT
    accounts = list(_rithmic_bridge.client.accounts)
    logger.info(f"🪞 Apex MIRROR {leg.upper()}: trim {trim_side} 1 + retighten stop → size={new_size} @ {new_stop_price} on {len(accounts)} accts")

    # Step 1: market trim 1 contract per account
    trim_ok, trim_fail = 0, 0
    for acct in accounts:
        try:
            r = _rithmic_bridge.place_market_order(acct, symbol, RITHMIC_EXCHANGE, trim_side, 1)
            if r:
                trim_ok += 1
                logger.info(f"  ✅ [{acct}] {leg.upper()} trim {trim_side} 1 (basket={r})")
            else:
                trim_fail += 1
                logger.error(f"  ❌ [{acct}] {leg.upper()} trim returned {r}")
        except Exception as e:
            trim_fail += 1
            logger.error(f"  ❌ [{acct}] {leg.upper()} trim error: {e}")
    logger.info(f"📊 Apex MIRROR {leg.upper()} trims: {trim_ok} ok / {trim_fail} fail")

    # Step 2: tighten the stop using captured basket_ids (reuses rt_modify_apex_stop logic)
    rt_modify_apex_stop(position, leg, new_size, new_stop_price, force=True)


def rt_apex_handle_t_fill(position, leg, new_size, new_stop_price):
    """Single dispatcher for what happens on the Apex side after a Topstep T1/T2 fill.
    Branches on the active feature flag. Safe to call unconditionally from T1/T2 handlers.
    """
    if not RITHMIC_ENABLED:
        return
    if RITHMIC_MIRROR_ENABLED:
        rt_apex_trim_and_tighten(position, leg, new_size, new_stop_price)
    elif RITHMIC_LADDER_ENABLED:
        rt_modify_apex_stop(position, leg, new_size, new_stop_price)
    # else: legacy entry+stop only — do nothing, sync_check handles eventual flatten


def rt_apex_close_runner_leg(position, leg, exit_type="tp_filled"):
    """Close any remaining Apex contracts for a runner leg when Topstep's runner exits.

    Why this exists (added 2026-05-07 after a live mismatch):
        - In MIRROR mode Apex has just 1 contract long (the runner) + a BE-stop, no TP limit.
        - When Topstep's runner cap (TP limit) fills, Apex doesn't auto-close — the BE-stop
          is below current price, so the runner sits open until either price reverses or
          sync_check runs (~5 min cycle). That delay = uncaptured runner profit on Apex.
        - This function closes the leg explicitly + cancels any residual orders so Apex
          tracks Topstep within ~100ms instead of waiting for sync_check.

    Steps per Apex account:
      1. Cancel the tracked stop basket_id (avoid phantom stop firing later)
      2. exit_position(symbol) — broker-level "flatten this contract" (handles any size)

    `exit_type` is informational; the close is the same in all cases. Safe no-op if Rithmic
    is disabled or the bridge is offline.
    """
    if not RITHMIC_ENABLED:
        return
    if not _rithmic_bridge or not _rithmic_bridge.connected:
        logger.error(f"❌ Apex runner close [{leg.upper()}]: bridge not connected")
        return

    symbol = RITHMIC_MES_CONTRACT if leg == "mes" else RITHMIC_MNQ_CONTRACT
    accounts = list(_rithmic_bridge.client.accounts)
    logger.info(f"🪞 Apex runner close [{leg.upper()}] ({exit_type}): cancel residual stops + exit position on {len(accounts)} accts")

    # Step 1: cancel residual stops via tracked basket_ids (defensive)
    baskets = (position.get("rt_stop_baskets") or {}).get(leg, {})
    for acct in accounts:
        bid = baskets.get(acct)
        if bid and bid != "submitted":
            try:
                _rithmic_bridge.cancel_order(acct, bid)
            except Exception as e:
                logger.warning(f"  ⚠️ Apex [{acct}] {leg.upper()} cancel residual stop error: {e}")

    # Step 2: exit position (close whatever's open at market) per account
    try:
        result = _rithmic_bridge.exit_position_all(symbol, RITHMIC_EXCHANGE)
        if isinstance(result, dict):
            ok = sum(1 for v in result.values() if v and not isinstance(v, Exception))
            fail = len(result) - ok
            logger.info(f"📊 Apex {leg.upper()} runner exit_position: {ok} ok / {fail} fail")
        elif result is None:
            logger.error(f"❌ Apex {leg.upper()} runner exit_position returned None")
    except Exception as e:
        logger.error(f"❌ Apex {leg.upper()} runner exit_position error: {e}")

    # Clear basket tracking — stops are gone, position is exiting
    if "rt_stop_baskets" in position:
        position["rt_stop_baskets"][leg] = {acct: None for acct in accounts}


def rt_modify_apex_stop(position, leg, new_size, new_stop_price, force=False):
    """Tighten the Apex stop on a leg by CANCEL-AND-REPLACE.

    History 2026-05-07: previously called Rithmic's `modify_order` to change the stop's
    quantity. On Rithmic's simulator (Apex eval tier), modify silently ignores quantity
    REDUCTIONS — the modify ACK comes back as success but the stop's size stays at the
    original value. Result: when the stop fires it sells the original size, not the new
    size, leaving the account net SHORT by (orig - new) contracts.

    Fixed by replacing modify with: cancel old stop → place new stop → update basket_id.
    Brief unstopped window (~200-500ms between cancel and place) is acceptable because
    these tightenings only happen AFTER a profitable trim, when price has just moved
    in our favor and is unlikely to immediately reverse through the new stop.

    `force=True` allows mirror-mode callers to invoke this even when LADDER_ENABLED is False.
    """
    if not RITHMIC_ENABLED:
        return
    if not force and not RITHMIC_LADDER_ENABLED:
        return
    if not _rithmic_bridge or not _rithmic_bridge.connected:
        logger.error(f"❌ Apex stop {leg.upper()} cancel-replace: bridge not connected")
        return

    baskets = (position.get("rt_stop_baskets") or {}).get(leg, {})
    if not baskets:
        logger.error(f"❌ Apex stop {leg.upper()} cancel-replace: no basket_ids tracked")
        return

    direction = position["direction"]
    side = "SELL" if direction == "long" else "BUY"
    symbol = RITHMIC_MES_CONTRACT if leg == "mes" else RITHMIC_MNQ_CONTRACT

    ok_count, fail_count = 0, 0
    new_baskets = {}
    for acct, basket_id in baskets.items():
        if not basket_id or basket_id == "submitted":
            logger.warning(f"⚠️ Apex stop [{acct}] {leg.upper()}: no usable basket_id ({basket_id}) — placing fresh stop")
            try:
                fresh = _rithmic_bridge.place_stop_order(
                    acct, symbol, RITHMIC_EXCHANGE, side, new_size, new_stop_price
                )
                new_baskets[acct] = fresh
                if fresh and fresh != "submitted":
                    ok_count += 1
                    logger.info(f"  ✅ Apex [{acct}] {leg.upper()} fresh stop size={new_size} @ {new_stop_price} basket={fresh}")
                else:
                    fail_count += 1
            except Exception as e:
                fail_count += 1
                new_baskets[acct] = None
                logger.error(f"  ❌ Apex [{acct}] {leg.upper()} fresh stop error: {e}")
            continue

        # Step 1: cancel the existing stop
        cancel_ok = False
        try:
            cancel_ok = bool(_rithmic_bridge.cancel_order(acct, basket_id))
        except Exception as e:
            logger.error(f"  ❌ Apex [{acct}] {leg.upper()} cancel old stop error: {e}")

        if not cancel_ok:
            logger.error(f"  ❌ Apex [{acct}] {leg.upper()} cancel of basket={basket_id} FAILED — NOT placing replacement (avoid double-stop). Operator should verify Apex.")
            queue_notification(
                "APEX_STOP_CANCEL_FAILED",
                f"⚠️ Apex {leg.upper()} stop cancel failed for {acct} basket={basket_id} — stop may still be at original size, position likely unprotected at desired tighter stop. Manual check needed."
            )
            new_baskets[acct] = basket_id  # keep old reference so future tries can attempt re-cancel
            fail_count += 1
            continue

        # Step 2: place fresh stop with desired size and price
        try:
            new_basket = _rithmic_bridge.place_stop_order(
                acct, symbol, RITHMIC_EXCHANGE, side, new_size, new_stop_price
            )
            new_baskets[acct] = new_basket
            if new_basket and new_basket != "submitted":
                ok_count += 1
                logger.info(f"  ✅ Apex [{acct}] {leg.upper()} stop replaced (canceled {basket_id}, new={new_basket}) size={new_size} @ {new_stop_price}")
            elif new_basket == "submitted":
                ok_count += 1
                logger.warning(f"  ⚠️ Apex [{acct}] {leg.upper()} stop placed without basket_id (ACK timeout) — future modifies on this stop will fall back to fresh-place")
            else:
                fail_count += 1
                logger.error(f"  ❌ Apex [{acct}] {leg.upper()} replacement stop returned None — ACCOUNT IS UNSTOPPED!")
                queue_notification(
                    "APEX_STOP_REPLACE_FAILED",
                    f"⚠️ Apex {leg.upper()} stop REPLACE FAILED on {acct} — account UNSTOPPED. Manual intervention needed."
                )
        except Exception as e:
            fail_count += 1
            new_baskets[acct] = None
            logger.error(f"  ❌ Apex [{acct}] {leg.upper()} replacement stop error: {e} — ACCOUNT MAY BE UNSTOPPED!")
            queue_notification(
                "APEX_STOP_REPLACE_ERROR",
                f"⚠️ Apex {leg.upper()} stop REPLACE ERROR on {acct}: {e} — manual check needed."
            )

    # Persist the new basket_ids so any subsequent T2/runner modifications find them
    if "rt_stop_baskets" not in position:
        position["rt_stop_baskets"] = {"mes": {}, "mnq": {}}
    position["rt_stop_baskets"][leg] = new_baskets
    try:
        save_position_state()
    except Exception:
        pass

    logger.info(f"📊 Apex {leg.upper()} stop cancel-replaced: {ok_count} ok / {fail_count} fail (new size={new_size} @ {new_stop_price})")


def rt_exit_position(symbol=None):
    """Exit position for a specific symbol on all Rithmic/Apex accounts."""
    if not RITHMIC_ENABLED or not _rithmic_bridge or not _rithmic_bridge.connected:
        return None
    sym = symbol or RITHMIC_MES_CONTRACT
    try:
        return _rithmic_bridge.exit_position_all(sym, RITHMIC_EXCHANGE)
    except Exception as e:
        logger.error(f"❌ Rithmic exit_position error: {e}")
        return None


# ================= POSITION PERSISTENCE =================
def save_position_state():
    """Save current position state to file for recovery"""
    try:
        state = {
            "saved_at": datetime.now().isoformat(),
            "active_positions": active_positions,
            "recent_trades": {k: v for k, v in recent_trades.items()},
            "recent_trim_alerts": {k: v for k, v in recent_trim_alerts.items()},
            "recent_entry_alerts": {k: v for k, v in recent_entry_alerts.items()}
        }
        with open(POSITION_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, default=str)
        logger.info(f"💾 Position state saved: {len(active_positions)} positions")
    except Exception as e:
        logger.error(f"Failed to save position state: {e}")

def load_position_state():
    """Load position state from file on startup"""
    global active_positions, recent_trades, recent_trim_alerts, recent_entry_alerts
    try:
        if os.path.exists(POSITION_STATE_FILE):
            with open(POSITION_STATE_FILE, "r") as f:
                state = json.load(f)
            saved_at = datetime.fromisoformat(state.get("saved_at", "2000-01-01"))
            age_seconds = (datetime.now() - saved_at).total_seconds()
            if age_seconds < 3600:
                active_positions = state.get("active_positions", {})
                recent_trades = state.get("recent_trades", {})
                recent_trim_alerts = state.get("recent_trim_alerts", {})
                recent_entry_alerts = state.get("recent_entry_alerts", {})
                logger.info(f"📂 Position state loaded: {len(active_positions)} positions (age: {age_seconds:.0f}s)")
            else:
                logger.warning(f"⚠️ Position state too old ({age_seconds:.0f}s), starting fresh")
        else:
            logger.info("📂 No position state file found, starting fresh")
    except Exception as e:
        logger.error(f"Failed to load position state: {e}")

# ================= ALERT PARSING =================
def parse_alert(text):
    """Parse Discord trading alert (JMoney + Optionsful)"""
    text = text.strip()
    logger.info(f"Parsing alert: {text[:80]}...")
    cleaned = clean_discord_text(text)
    text_upper = cleaned.upper()
    
    # ============ OPTIONSFUL PATTERNS (checked first to avoid keyword collisions) ============
    # Entry: BOUGHT SPY 5/5 722C 1.40  |  BOUGHT QQQ 5/5 676P 2.28
    of_entry = re.search(r'BOUGHT\s+(SPY|QQQ)\s+\S+\s+(\d+)([CP])\s+([\d.]+)', cleaned, re.IGNORECASE)
    if of_entry:
        ticker, strike, cp, premium = of_entry.groups()
        direction = "long" if cp.upper() == "C" else "short"
        # NOTE: alert["price"] is just a placeholder/label here. The bot fires entry at
        # MARKET, then after fill, recalculates the stop using the broker's actual
        # averagePrice (see process_entry_alert ~line 2335). So strike*10 is a fine proxy:
        # in valid range (1000-10000), unique per strike for position-key dedup.
        placeholder_price = float(strike) * 10
        logger.info(f"\u2728 OPTIONSFUL ENTRY: {ticker} {strike}{cp.upper()} @ ${premium} \u2192 MES {direction.upper()} (placeholder ref={placeholder_price}, real stop will use broker fill)")
        return {"type": "entry", "direction": direction, "price": placeholder_price, "text": cleaned, "source": "optionsful"}
    
    # Exit: SOLD SPY 700C 6.60 ALL OUT  |  SOLD SPY 700C 2.50 1/4 position
    of_exit = re.search(r'SOLD\s+(SPY|QQQ)\s+\d+[CP]\s+[\d.]+\s+(1/4 position|1/8 position|ALL OUT)', cleaned, re.IGNORECASE)
    if of_exit:
        fraction = of_exit.group(2).upper()
        if "ALL OUT" in fraction:
            logger.info(f"\u2728 OPTIONSFUL EXIT (ALL OUT): {cleaned[:80]}")
            return {"type": "close_all", "text": cleaned, "source": "optionsful"}
        if "1/4 POSITION" in fraction:
            logger.info(f"\u2728 OPTIONSFUL TRIM 1/4 (\u2192 first_trim): {cleaned[:80]}")
            return {"type": "first_trim", "text": cleaned, "source": "optionsful"}
        if "1/8 POSITION" in fraction:
            logger.info(f"\u2728 OPTIONSFUL TRIM 1/8 (\u2192 second_trim): {cleaned[:80]}")
            return {"type": "second_trim", "text": cleaned, "source": "optionsful"}
    
    # ============ JMONEY PATTERNS (original) ============
    if "TRIM 3/4" in text_upper or "TRIM 4/8" in text_upper or "TRIM 1/2" in text_upper:
        logger.info(f"Detected FIRST TRIM alert")
        return {"type": "first_trim", "text": cleaned}
    
    if "TRIM 1/8" in text_upper or "TRIM 3/8" in text_upper or "TRIM 1/4" in text_upper:
        logger.info(f"Detected SECOND TRIM alert")
        return {"type": "second_trim", "text": cleaned}
    
    # T3 — third target / trim-half-of-remaining keywords. Use \bT3\b so 'AT3', 'T30' don't match.
    t3_keywords = ["3RD TARGET", "THIRD TARGET", "TRIM 1/2 OF REMAINING", "TRIM HALF"]
    if any(k in text_upper for k in t3_keywords) or re.search(r'\bT3\b', text_upper):
        logger.info(f"Detected THIRD TRIM alert")
        return {"type": "third_trim", "text": cleaned}
    
    # T4 — fourth target. Use \bT4\b so 'T40', 'AT4' don't match.
    t4_keywords = ["4TH TARGET", "FOURTH TARGET"]
    if any(k in text_upper for k in t4_keywords) or re.search(r'\bT4\b', text_upper):
        logger.info(f"Detected FOURTH TRIM alert")
        return {"type": "fourth_trim", "text": cleaned}
    
    stop_keywords = ["STOPPED", "STOP LOSS", "HIT STOP", "STOP OUT", "STOP HIT"]
    if any(word in text_upper for word in stop_keywords):
        logger.info(f"Detected STOP alert")
        return {"type": "stopped", "text": cleaned}
    
    emergency_keywords = ["EMERGENCY", "CLOSE ALL", "EXIT ALL", "STOP ALL", "FLATTEN"]
    if any(word in text_upper for word in emergency_keywords):
        logger.info(f"Detected EMERGENCY alert")
        return {"type": "close_all", "text": cleaned}
    
    runner_keywords = ["EXIT RUNNER", "CLOSE RUNNER", "RUNNER OUT", "SMALL ACCOUNTS CAN CLOSE THE RUNNER HERE"]
    if any(word in text_upper for word in runner_keywords):
        logger.info(f"Detected RUNNER EXIT alert")
        return {"type": "exit_runner", "text": cleaned}
    
    # Entry patterns — instrument prefix required to avoid acting on trade commentary.
    # Supported instruments: SPX, ES, MES, MNQ, NQ, GC, MGC
    # [^\w]* between instrument and direction tolerates any emoji/arrow/symbol
    # that alert services insert (e.g. "SPX ▲ LONG", "ES 🔴 SHORT", "MNQ→LONG").
    # Price range covers all supported instruments:
    #   ES/MES/SPX ~1000–10000, MNQ/NQ ~10000–30000, GC/MGC ~1000–5000
    _instr = r'(?:SPX|ES|MES|MNQ|NQ|GC|MGC)'
    patterns = [
        rf'\b{_instr}[^\w]*(LONG|SHORT)\s*[:@]?\s*(\d+(?:\.\d+)?)',  # SPX ▲ LONG 7407 | ES Long 7407
        rf'\b(LONG|SHORT)\s+{_instr}\s*[:@]?\s*(\d+(?:\.\d+)?)',     # Long ES 7407
        rf'\b(LONG|SHORT)\s*[:@]?\s*(\d+(?:\.\d+)?)\s+{_instr}\b',  # Long 7407 ES
    ]

    for pattern in patterns:
        match = re.search(pattern, text_upper)
        if match:
            direction, price = match.groups()
            direction = direction.lower()
            price = float(price)
            if price < 100 or price > 100000:
                logger.warning(f"⚠️ Suspicious price: {price}. Ignoring.")
                return None
            logger.info(f"Detected ENTRY: {direction.upper()} @ {price}")
            return {"type": "entry", "direction": direction, "price": price, "text": cleaned}

    # Diagnostic: direction + price found but no recognised instrument prefix nearby.
    loose_match = re.search(r'\b(LONG|SHORT)\s+\d+', text_upper)
    if loose_match:
        window_start = max(0, loose_match.start() - 30)
        window_end = min(len(text_upper), loose_match.end() + 30)
        if not re.search(rf'\b{_instr}\b', text_upper[window_start:window_end]):
            logger.warning(f"🚫 Ignored — direction found but no instrument prefix (commentary / trade idea): {text_upper[:120]}")
            return None
    
    logger.warning(f"⚠️ Could not parse alert: {text_upper[:100]}...")
    return None

def clean_discord_text(text):
    if not text:
        return ""
    ui_patterns = [
        r'🔮︱futures chat.*', r'Use the up and down arrow keys.*',
        r'Click to react.*', r'Copy Message ID.*', r'Mark Unread.*',
        r'Pin Message.*', r'Jump to.*', r'Edited.*', r'Replying to.*',
    ]
    cleaned = text
    for pattern in ui_patterns:
        cleaned = re.sub(pattern, '', cleaned, flags=re.IGNORECASE | re.MULTILINE)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    # Split timestamp suffix mashed into instrument token by Discord scraper
    # (e.g. "10:17 AMES long 6800" -> "10:17 AM ES long 6800").
    # Without this, the strict ES-prefix parser rejects the alert as commentary.
    cleaned = re.sub(r'\b(AM|PM)(ES|MES|MNQ|NQ)\b', r'\1 \2', cleaned, flags=re.IGNORECASE)
    return cleaned

def is_alert_too_old(client_timestamp):
    if not client_timestamp:
        return False
    try:
        client_timestamp = float(client_timestamp)
        if client_timestamp > 1000000000000:
            client_timestamp = client_timestamp / 1000
        age_seconds = time.time() - client_timestamp
        logger.info(f"Message age: {age_seconds:.1f} seconds")
        if age_seconds > MAX_ALERT_AGE_SECONDS:
            logger.warning(f"Alert too old: {age_seconds:.0f} seconds")
            return True
        return False
    except Exception as e:
        logger.error(f"Error checking alert age: {e}")
        return True

def is_duplicate_alert(alert, client_timestamp):
    current_time = time.time()
    
    if alert["type"] == "entry":
        alert_key = f"entry_{alert['direction']}_{alert['price']}"
        current_position = get_active_position()
        if current_position:
            position_key = create_position_key(alert["direction"], alert["price"])
            if current_position["key"] == position_key:
                logger.warning(f"⚠️ ACTIVE POSITION EXISTS for this entry: {position_key}")
                return True
        if alert_key in recent_entry_alerts:
            last_time = recent_entry_alerts[alert_key]
            if current_time - last_time < 30:
                logger.warning(f"⚠️ DUPLICATE ENTRY ALERT: {current_time - last_time:.1f}s ago")
                return True
        return False
    
    elif alert["type"] in ["first_trim", "second_trim", "exit_runner"]:
        alert_hash = hashlib.md5(f"{alert['type']}_{alert['text'][:100]}".encode()).hexdigest()[:12]
        alert_key = f"trim_{alert_hash}"
        if alert_key in recent_trim_alerts:
            if current_time - recent_trim_alerts[alert_key] < 30:
                logger.warning(f"⚠️ DUPLICATE TRIM ALERT")
                return True
        return False
    
    elif alert["type"] in ["stopped", "close_all"]:
        alert_key = f"{alert['type']}_{current_time // 10}"
        if alert_key in recent_trades:
            logger.warning(f"⚠️ RECENT {alert['type'].upper()} in last 10s")
            return True
        return False
    
    return False

# ================= POSITION MANAGEMENT =================
def create_position_key(direction, entry_price):
    return f"{direction}_{entry_price}"

def get_active_position():
    if not active_positions:
        return None
    positions = list(active_positions.values())
    positions.sort(key=lambda x: x["created_at"], reverse=True)
    return positions[0] if positions else None

def create_position(alert):
    position_key = create_position_key(alert["direction"], alert["price"])
    
    # MES stop
    if alert["direction"] == "long":
        mes_initial_stop = round(alert["price"] - MES_TRAILING_STOP_POINTS, 2)
    else:
        mes_initial_stop = round(alert["price"] + MES_TRAILING_STOP_POINTS, 2)
    
    # MNQ entry price will be fetched after fill; use 0 as placeholder
    # MNQ stop will be calculated from actual MNQ fill price
    
    position = {
        "key": position_key,
        "source": alert.get("source", "jmoney"),  # which alert service opened this trade
        "direction": alert["direction"],
        "entry_price": alert["price"],  # MES entry (ES alert price)
        "best_price": alert["price"],   # MES best price for trailing
        "created_at": time.time(),
        "last_updated": time.time(),
        "trim_history": [],
        "requires_manual_stop": False,
        "stop_orders_placed": 0,
        "last_stop_placed": None,
        "strategy": "1-1-1 MES+MNQ",
        "status": "active",
        "first_trim": False,
        "second_trim": False,
        "third_trim": False,
        "fourth_trim": False,
        "runner_active": False,
        "fully_trimmed": False,
        
        # ===== MES state =====
        "mes_total": MES_CONTRACTS,
        "mes_remaining": MES_CONTRACTS,
        "mes_entry_price": alert["price"],
        "mes_best_price": alert["price"],
        "mes_stop_price": mes_initial_stop,
        "mes_stop_points": MES_TRAILING_STOP_POINTS,
        "mes_first_trim_size": MES_FIRST_TRIM_CONTRACTS,
        "mes_second_trim_size": MES_SECOND_TRIM_CONTRACTS,
        "mes_account_states": {},  # {acct_id: {entry_order_id, stop_order_id}}
        
        # ===== MNQ state =====
        "mnq_total": MNQ_CONTRACTS,
        "mnq_remaining": MNQ_CONTRACTS,
        "mnq_entry_price": 0,  # Set after fill
        "mnq_best_price": 0,
        "mnq_stop_price": 0,   # Set after fill
        "mnq_stop_points": MNQ_TRAILING_STOP_POINTS,
        "mnq_first_trim_size": MNQ_FIRST_TRIM_CONTRACTS,
        "mnq_second_trim_size": MNQ_SECOND_TRIM_CONTRACTS,
        "mnq_account_states": {},
        
        # Legacy compat (remaining_contracts = MES remaining, used by some shared code)
        "total_contracts": MES_CONTRACTS,
        "remaining_contracts": MES_CONTRACTS,
        "current_stop_price": mes_initial_stop,
        "trailing_stop_points": MES_TRAILING_STOP_POINTS,
        "first_trim_contracts": MES_FIRST_TRIM_CONTRACTS,
        "second_trim_contracts": MES_SECOND_TRIM_CONTRACTS,
        
        # Tradovate order tracking (MES only, Tradovate disabled for now)
        "tv_entry_order_id": None,
        "tv_stop_order_id": None,
        "tv_tp_order_id": None,
    }
    
    active_positions[position_key] = position
    logger.info(f"📝 NEW POSITION: {position_key} — {alert['direction'].upper()} @ {alert['price']}")
    logger.info(f"   Strategy: 1-1-1 (3 MES + 3 MNQ), MES stop: {mes_initial_stop}")
    return position

def validate_position_state(position):
    if position["remaining_contracts"] < 0:
        logger.error(f"❌ INVALID: Negative contracts")
        return False
    if position["remaining_contracts"] > position["total_contracts"]:
        logger.error(f"❌ INVALID: More remaining than total")
        return False
    return True

def update_position_after_trim(position, trim_type):
    """Update both MES and MNQ position state after a trim.
    Returns (success, mes_contracts_to_close, mnq_contracts_to_close)."""
    if not validate_position_state(position):
        return False, 0, 0
    
    if trim_type == "first_trim":
        if position["first_trim"]:
            logger.warning("⚠️ First trim already done")
            return False, 0, 0
        mes_trim = position.get("mes_first_trim_size", MES_FIRST_TRIM_CONTRACTS)
        mnq_trim = position.get("mnq_first_trim_size", MNQ_FIRST_TRIM_CONTRACTS)
        
        if position.get("mes_remaining", 0) >= mes_trim:
            position["mes_remaining"] -= mes_trim
            position["mnq_remaining"] = max(0, position.get("mnq_remaining", 0) - mnq_trim)
            position["remaining_contracts"] = position["mes_remaining"]  # legacy
            position["first_trim"] = True
            position["trim_history"].append({
                "type": "first_trim", "mes": mes_trim, "mnq": mnq_trim, "timestamp": time.time()
            })
            position["last_updated"] = time.time()
            logger.info(f"✂️ First trim: MES -{mes_trim} (rem:{position['mes_remaining']}), MNQ -{mnq_trim} (rem:{position['mnq_remaining']})")
            return True, mes_trim, mnq_trim
        logger.warning(f"⚠️ Not enough MES contracts for first trim: have {position.get('mes_remaining', 0)}")
        return False, 0, 0
    
    elif trim_type == "second_trim":
        if position["second_trim"]:
            logger.warning("⚠️ Second trim already done")
            return False, 0, 0
        if not position["first_trim"]:
            logger.warning("⚠️ Cannot second trim before first")
            return False, 0, 0
        mes_trim = position.get("mes_second_trim_size", MES_SECOND_TRIM_CONTRACTS)
        mnq_trim = position.get("mnq_second_trim_size", MNQ_SECOND_TRIM_CONTRACTS)
        
        if position.get("mes_remaining", 0) >= mes_trim:
            position["mes_remaining"] -= mes_trim
            position["mnq_remaining"] = max(0, position.get("mnq_remaining", 0) - mnq_trim)
            position["remaining_contracts"] = position["mes_remaining"]
            position["second_trim"] = True
            position["trim_history"].append({
                "type": "second_trim", "mes": mes_trim, "mnq": mnq_trim, "timestamp": time.time()
            })
            position["last_updated"] = time.time()
            
            if position["mes_remaining"] <= 0:
                position["fully_trimmed"] = True
                position["runner_active"] = False
                position["status"] = "closed"
                logger.info(f"✂️ Second trim: MES -{mes_trim}, MNQ -{mnq_trim}, FULLY CLOSED")
            else:
                position["runner_active"] = True
                logger.info(f"✂️ Second trim: MES -{mes_trim} (rem:{position['mes_remaining']}), MNQ -{mnq_trim} (rem:{position['mnq_remaining']}), RUNNER ACTIVE")
            return True, mes_trim, mnq_trim
        logger.warning(f"⚠️ Not enough MES contracts for second trim")
        return False, 0, 0
    
    elif trim_type == "exit_runner":
        if not position["runner_active"]:
            logger.warning("⚠️ No runner to exit")
            return False, 0, 0
        mes_close = position.get("mes_remaining", 0)
        mnq_close = position.get("mnq_remaining", 0)
        if mes_close > 0 or mnq_close > 0:
            position["mes_remaining"] = 0
            position["mnq_remaining"] = 0
            position["remaining_contracts"] = 0
            position["fully_trimmed"] = True
            position["status"] = "closed"
            position["trim_history"].append({
                "type": "runner", "mes": mes_close, "mnq": mnq_close, "timestamp": time.time()
            })
            position["last_updated"] = time.time()
            logger.info(f"🏁 Runner exit: MES -{mes_close}, MNQ -{mnq_close}")
            return True, mes_close, mnq_close
        logger.warning("⚠️ No contracts remaining")
        return False, 0, 0
    
    return False, 0, 0

def close_position(position, exit_type="exit"):
    if position["remaining_contracts"] > 0:
        logger.info(f"📤 Closing position: {position['remaining_contracts']} contracts ({exit_type})")
    if position["key"] in active_positions:
        del active_positions[position["key"]]
    # BELT-AND-SUSPENDERS (2026-05-15): every close path force-flattens Apex + Tradovate.
    # Idempotent — safe when already flat. Fixes scenario where Topstep closes (TP fill,
    # manual exit, ladder runner cap) but Apex/Rithmic mirror is left holding contracts
    # because the specific code path didn't reach rt_flatten(). Previously only the
    # MES-catalyst monitor block and process_stop_alert/process_close_all fired rt_flatten;
    # in MNQ-only 4-1-1 mode the MES catalyst is pre-marked done so that block is skipped.
    try:
        if RITHMIC_ENABLED:
            rt_flatten()
    except Exception as _rt_err:
        logger.error(f"❌ close_position rt_flatten error: {_rt_err}")
    try:
        if TRADOVATE_ENABLED:
            tv_cancel_all_orders()
            tv_flatten()
    except Exception as _tv_err:
        logger.error(f"❌ close_position tv_flatten error: {_tv_err}")
    return True

# ================= STOP MANAGEMENT =================
def calculate_stops(position):
    """Calculate MES and MNQ stop prices and side.
    Returns (stop_side, mes_stop_price, mnq_stop_price)."""
    if not position:
        return None, None, None
    
    # Determine stop points based on trim state
    if position.get("first_trim", False):
        mes_pts = MES_FIRST_TRIM_STOP_POINTS
        mnq_pts = MNQ_FIRST_TRIM_STOP_POINTS
    else:
        mes_pts = MES_TRAILING_STOP_POINTS
        mnq_pts = MNQ_TRAILING_STOP_POINTS
    
    mes_best = position.get("mes_best_price", position.get("entry_price", 0))
    mnq_best = position.get("mnq_best_price", position.get("mnq_entry_price", 0))
    
    if position["direction"] == "long":
        mes_stop = round(mes_best - mes_pts, 2)
        mnq_stop = round(mnq_best - mnq_pts, 2) if mnq_best else 0
        stop_side = 1  # Sell stop
    else:
        mes_stop = round(mes_best + mes_pts, 2)
        mnq_stop = round(mnq_best + mnq_pts, 2) if mnq_best else 0
        stop_side = 0  # Buy stop
    
    return stop_side, mes_stop, mnq_stop

def place_stop_for_position(position):
    """Place MES and MNQ stop orders on all accounts."""
    if not position:
        return False
    # MNQ-only mode fix: bail only if BOTH legs are empty (was: only checked MES)
    if position.get("mes_remaining", 0) <= 0 and position.get("mnq_remaining", 0) <= 0:
        return False
    
    stop_side, mes_stop, mnq_stop = calculate_stops(position)
    if stop_side is None:
        logger.error("❌ Failed to calculate stops")
        return False
    
    mes_size = position.get("mes_remaining", 0)
    mnq_size = position.get("mnq_remaining", 0)
    
    logger.info(f"🛑 Placing stops: MES {mes_size}@{mes_stop}, MNQ {mnq_size}@{mnq_stop}")
    
    # Cancel existing stops first
    cancel_stops_all_accounts(position)
    time.sleep(0.5)
    
    # Place new stops for both instruments
    stop_results = execute_stop_all_accounts(
        stop_side, mes_size, mes_stop,
        mnq_size=mnq_size if mnq_size > 0 else None,
        mnq_stop_price=mnq_stop if mnq_size > 0 else None
    )
    
    mes_states = position.get("mes_account_states", {})
    mnq_states = position.get("mnq_account_states", {})
    all_ok = True
    
    for acct_id, result in stop_results.items():
        acct_key = str(acct_id)
        # MES
        if acct_key not in mes_states:
            mes_states[acct_key] = {}
        if result.get("mes"):
            mes_states[acct_key]["stop_order_id"] = result["mes"]
        else:
            all_ok = False
            logger.error(f"❌ MES stop failed for acct={acct_id}")
        # MNQ
        if acct_key not in mnq_states:
            mnq_states[acct_key] = {}
        if mnq_size > 0:
            if result.get("mnq"):
                mnq_states[acct_key]["stop_order_id"] = result["mnq"]
            else:
                all_ok = False
                logger.error(f"❌ MNQ stop failed for acct={acct_id}")
    
    position["mes_account_states"] = mes_states
    position["mnq_account_states"] = mnq_states
    position["mes_stop_price"] = mes_stop
    position["mnq_stop_price"] = mnq_stop
    position["current_stop_price"] = mes_stop  # legacy
    position["stop_orders_placed"] = position.get("stop_orders_placed", 0) + 1
    position["last_stop_placed"] = time.time()
    position["requires_manual_stop"] = not all_ok
    
    # Tradovate stops (MES + MNQ)
    if TRADOVATE_ENABLED:
        tv_stop_side = "Sell" if position["direction"] == "long" else "Buy"
        tv_cancel_all_stops()
        rt_cancel_all()  # Rithmic/Apex
        tv_mes_stop_id = tv_place_stop_order(tv_stop_side, mes_size, mes_stop, symbol=TRADOVATE_MES_CONTRACT_NAME)
        position["tv_stop_order_id"] = tv_mes_stop_id
        if tv_mes_stop_id:
            logger.info(f"✅ Tradovate MES stop placed @ {mes_stop}")
        else:
            all_ok = False
        if mnq_size > 0:
            tv_mnq_stop_id = tv_place_stop_order(tv_stop_side, mnq_size, mnq_stop, symbol=TRADOVATE_MNQ_CONTRACT_NAME)
            position["tv_mnq_stop_order_id"] = tv_mnq_stop_id
            if tv_mnq_stop_id:
                logger.info(f"✅ Tradovate MNQ stop placed @ {mnq_stop}")
            else:
                all_ok = False
    
    # Rithmic/Apex stops — capture per-account basket_ids when MIRROR mode is on so
    # rt_apex_trim_and_tighten() can modify them later on T1/T2 fill events.
    rt_mes_stop_result = rt_stop(position["direction"], mes_size, mes_stop, symbol=RITHMIC_MES_CONTRACT)
    rt_mnq_stop_result = None
    if mnq_size > 0 and mnq_stop:
        rt_mnq_stop_result = rt_stop(position["direction"], mnq_size, mnq_stop, symbol=RITHMIC_MNQ_CONTRACT)
    # Stash basket_ids for later modify (no-op when neither mirror mode is on)
    if RITHMIC_ENABLED and (RITHMIC_MIRROR_ENABLED or RITHMIC_LADDER_ENABLED):
        try:
            _rt_capture_stop_baskets(position, rt_mes_stop_result, "mes")
            if rt_mnq_stop_result is not None:
                _rt_capture_stop_baskets(position, rt_mnq_stop_result, "mnq")
        except Exception as _e:
            logger.error(f"❌ Apex basket-id capture error: {_e}")

    if all_ok:
        logger.info(f"✅ Stops placed on all accounts: MES@{mes_stop}, MNQ@{mnq_stop}")
    else:
        logger.error(f"❌ Stop placement failed on some accounts")
    
    return all_ok

def update_stop_after_trim(position, trim_type=None):
    """After a trim, update MES and MNQ stops based on trim type."""
    mes_rem = position.get("mes_remaining", 0)
    mnq_rem = position.get("mnq_remaining", 0)
    
    if not position or (mes_rem <= 0 and mnq_rem <= 0):
        cancel_stops_all_accounts(position)
        return True
    
    is_first_trim = position.get("first_trim", False) and not position.get("second_trim", False)
    is_runner = position.get("runner_active", False) and mes_rem == 1
    direction = position["direction"]
    stop_side = 1 if direction == "long" else 0
    
    if is_runner:
        # ========== BREAKEVEN STOP FOR RUNNER (both MES and MNQ) ==========
        mes_entry = position.get("mes_entry_price", position["entry_price"])
        mnq_entry = position.get("mnq_entry_price", 0)
        
        if direction == "long":
            mes_be = mes_entry - MES_RUNNER_BREAKEVEN_OFFSET
            mnq_be = mnq_entry - MNQ_RUNNER_BREAKEVEN_OFFSET if mnq_entry else 0
        else:
            mes_be = mes_entry + MES_RUNNER_BREAKEVEN_OFFSET
            mnq_be = mnq_entry + MNQ_RUNNER_BREAKEVEN_OFFSET if mnq_entry else 0
        
        logger.info(f"🏃 RUNNER: MES breakeven @ {mes_be}, MNQ breakeven @ {mnq_be}")
        
        cancel_stops_all_accounts(position)
        time.sleep(0.5)
        
        stop_results = execute_stop_all_accounts(
            stop_side, 1, mes_be,
            mnq_size=1 if mnq_rem > 0 else None,
            mnq_stop_price=mnq_be if mnq_rem > 0 else None
        )
        
        mes_states = position.get("mes_account_states", {})
        mnq_states = position.get("mnq_account_states", {})
        all_ok = True
        for acct_id, result in stop_results.items():
            acct_key = str(acct_id)
            if acct_key not in mes_states: mes_states[acct_key] = {}
            if acct_key not in mnq_states: mnq_states[acct_key] = {}
            if result.get("mes"):
                mes_states[acct_key]["stop_order_id"] = result["mes"]
            else:
                all_ok = False
            if mnq_rem > 0:
                if result.get("mnq"):
                    mnq_states[acct_key]["stop_order_id"] = result["mnq"]
                else:
                    all_ok = False
        
        position["mes_account_states"] = mes_states
        position["mnq_account_states"] = mnq_states
        position["mes_stop_price"] = mes_be
        position["mnq_stop_price"] = mnq_be
        position["current_stop_price"] = mes_be
        position["stop_orders_placed"] = position.get("stop_orders_placed", 0) + 1
        position["breakeven_stop_set"] = True
        
        if TRADOVATE_ENABLED:
            tv_cancel_all_stops()
            rt_cancel_all()  # Rithmic/Apex
            tv_mes_stop_id = tv_stop(direction, 1, mes_be, symbol=TRADOVATE_MES_CONTRACT_NAME)
            position["tv_stop_order_id"] = tv_mes_stop_id
            if mnq_rem > 0 and mnq_be:
                tv_mnq_stop_id = tv_stop(direction, 1, mnq_be, symbol=TRADOVATE_MNQ_CONTRACT_NAME)
                position["tv_mnq_stop_order_id"] = tv_mnq_stop_id
        
        if all_ok:
            logger.info(f"✅ RUNNER stops moved to breakeven: MES@{mes_be}, MNQ@{mnq_be}")
        return all_ok
    
    elif is_first_trim:
        # First trim: update stops to tighter levels
        mes_trim_price = position.get("last_trim_price", position.get("mes_entry_price", position["entry_price"]))
        mnq_trim_price = position.get("mnq_last_trim_price", position.get("mnq_entry_price", 0))
        
        if direction == "long":
            new_mes_stop = round(mes_trim_price - MES_FIRST_TRIM_STOP_POINTS, 2)
            new_mnq_stop = round(mnq_trim_price - MNQ_FIRST_TRIM_STOP_POINTS, 2) if mnq_trim_price else 0
        else:
            new_mes_stop = round(mes_trim_price + MES_FIRST_TRIM_STOP_POINTS, 2)
            new_mnq_stop = round(mnq_trim_price + MNQ_FIRST_TRIM_STOP_POINTS, 2) if mnq_trim_price else 0
        
        logger.info(f"🔧 First trim stops: MES {mes_rem}@{new_mes_stop}, MNQ {mnq_rem}@{new_mnq_stop}")
        
        modify_results = modify_stop_all_accounts(
            position,
            new_mes_size=mes_rem, new_mes_stop=new_mes_stop,
            new_mnq_size=mnq_rem if mnq_rem > 0 else None,
            new_mnq_stop=new_mnq_stop if mnq_rem > 0 else None
        )
        
        all_ok = all(modify_results.values()) if modify_results else False
        
        if not all_ok:
            logger.info(f"🔧 Modify failed, cancel and re-place stops")
            cancel_stops_all_accounts(position)
            time.sleep(0.5)
            stop_results = execute_stop_all_accounts(
                stop_side, mes_rem, new_mes_stop,
                mnq_size=mnq_rem if mnq_rem > 0 else None,
                mnq_stop_price=new_mnq_stop if mnq_rem > 0 else None
            )
            # Update account states from fresh placement
            mes_states = position.get("mes_account_states", {})
            mnq_states = position.get("mnq_account_states", {})
            for acct_id, result in stop_results.items():
                acct_key = str(acct_id)
                if acct_key not in mes_states: mes_states[acct_key] = {}
                if acct_key not in mnq_states: mnq_states[acct_key] = {}
                if result.get("mes"):
                    mes_states[acct_key]["stop_order_id"] = result["mes"]
                if mnq_rem > 0 and result.get("mnq"):
                    mnq_states[acct_key]["stop_order_id"] = result["mnq"]
            position["mes_account_states"] = mes_states
            position["mnq_account_states"] = mnq_states
            all_ok = True  # Fresh placement should work
        
        if TRADOVATE_ENABLED:
            tv_cancel_all_stops()
            rt_cancel_all()  # Rithmic/Apex
            tv_stop_side = "Sell" if direction == "long" else "Buy"
            tv_mes_stop_id = tv_place_stop_order(tv_stop_side, mes_rem, new_mes_stop, symbol=TRADOVATE_MES_CONTRACT_NAME)
            position["tv_stop_order_id"] = tv_mes_stop_id
            if mnq_rem > 0 and new_mnq_stop:
                tv_mnq_stop_id = tv_place_stop_order(tv_stop_side, mnq_rem, new_mnq_stop, symbol=TRADOVATE_MNQ_CONTRACT_NAME)
                position["tv_mnq_stop_order_id"] = tv_mnq_stop_id
        
        position["mes_stop_price"] = new_mes_stop
        position["mnq_stop_price"] = new_mnq_stop
        position["current_stop_price"] = new_mes_stop
        if all_ok:
            logger.info(f"✅ First trim stops: MES@{new_mes_stop}, MNQ@{new_mnq_stop}")
        return all_ok
    
    else:
        # Regular trailing stop update
        stop_side, mes_stop, mnq_stop = calculate_stops(position)
        
        logger.info(f"🔧 Updating stops: MES {mes_rem}@{mes_stop}, MNQ {mnq_rem}@{mnq_stop}")
        
        results = modify_stop_all_accounts(
            position,
            new_mes_size=mes_rem, new_mes_stop=mes_stop,
            new_mnq_size=mnq_rem if mnq_rem > 0 else None,
            new_mnq_stop=mnq_stop if mnq_rem > 0 else None
        )
        
        all_ok = all(results.values()) if results else False
        position["mes_stop_price"] = mes_stop
        position["mnq_stop_price"] = mnq_stop
        position["current_stop_price"] = mes_stop
        
        if TRADOVATE_ENABLED:
            tv_cancel_all_stops()
            rt_cancel_all()  # Rithmic/Apex
            tv_stop_side = "Sell" if direction == "long" else "Buy"
            tv_mes_stop_id = tv_place_stop_order(tv_stop_side, mes_rem, mes_stop, symbol=TRADOVATE_MES_CONTRACT_NAME)
            position["tv_stop_order_id"] = tv_mes_stop_id
            if mnq_rem > 0 and mnq_stop:
                tv_mnq_stop_id = tv_place_stop_order(tv_stop_side, mnq_rem, mnq_stop, symbol=TRADOVATE_MNQ_CONTRACT_NAME)
                position["tv_mnq_stop_order_id"] = tv_mnq_stop_id
        
        if all_ok:
            logger.info(f"✅ Stops updated: MES@{mes_stop}, MNQ@{mnq_stop}")
        else:
            logger.error(f"❌ Stop update failed — placing fresh stops")
            place_stop_for_position(position)
        
        return all_ok

# ================= ALERT PROCESSING =================
def process_alert(alert):
    # ============ CLOUD GATE (hard subscription check) ============
    alert_type = alert.get("type", "")
    if not _cloud_gate_check(alert_type):
        logger.warning(f"\ud83d\udeab GATE: Blocked {alert_type} — subscription inactive")
        return {"success": False, "message": "Subscription inactive. Renew at proptradebot.com", "blocked_by_gate": True}
    # ============ END CLOUD GATE ============

    # ============ MULTI-SOURCE MUTEX ============
    # Only one trade in flight at a time across all alert sources.
    new_source = alert.get("source", "jmoney")
    current_position = get_active_position()
    if current_position:
        existing_source = current_position.get("source", "jmoney")
        if existing_source != new_source:
            if alert_type == "entry":
                logger.warning(f"\u23ed\ufe0f MUTEX: Skipping {new_source} ENTRY \u2014 {existing_source} trade still active")
                return {"success": False, "message": f"Trade already in progress from {existing_source}", "skipped_mutex": True}
            else:
                logger.warning(f"\u23ed\ufe0f MUTEX: Ignoring {new_source} {alert_type} \u2014 active trade is {existing_source}'s")
                return {"success": False, "message": f"Not {new_source}'s trade (active: {existing_source})", "skipped_mutex": True}
    # ============ END MUTEX ============

    if alert_type == "entry":
        return process_entry_alert(alert)
    elif alert_type == "first_trim":
        return process_trim_alert(alert, "first_trim")
    elif alert_type == "second_trim":
        return process_trim_alert(alert, "second_trim")
    elif alert_type == "exit_runner":
        return process_trim_alert(alert, "exit_runner")
    elif alert_type == "third_trim":
        return process_trim_alert(alert, "third_trim")
    elif alert_type == "fourth_trim":
        return process_trim_alert(alert, "fourth_trim")
    elif alert_type == "stopped":
        return process_stop_alert(alert)
    elif alert_type == "close_all":
        return process_close_all()
    else:
        return {"success": False, "message": f"Unknown alert type: {alert_type}"}

# ================= TIME-OF-DAY ENTRY FILTER =================
# Block entry alerts during specific ET windows (Mon-Fri). Stops/trims on existing
# positions still process — only NEW entries are blocked. Originally added to skip
# Optionsful's late-day overnight-swing alerts (~3:55-5:59 PM ET) that would leave
# positions exposed past the bot's 16:10 EOD auto-flatten.
ENTRY_BLOCK_WINDOWS_ET = [
    (15 * 60 + 55, 17 * 60 + 59),  # 15:55-17:59 ET = 3:55 PM – 5:59 PM
]

def is_entry_blocked():
    """Returns (blocked: bool, reason: str). True only Mon-Fri inside a configured window."""
    try:
        import zoneinfo
        now = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    except Exception:
        now = datetime.now()  # fallback: assume server clock is ET
    if now.weekday() > 4:  # 5=Sat, 6=Sun
        return (False, "")
    cur = now.hour * 60 + now.minute
    for start, end in ENTRY_BLOCK_WINDOWS_ET:
        if start <= cur <= end:
            label = f"{start//60:02d}:{start%60:02d}-{end//60:02d}:{end%60:02d} ET"
            return (True, label)
    return (False, "")


def process_entry_alert(alert):
    # Time-of-day filter: avoid late-day entries (e.g. Optionsful overnight option swings)
    blocked, window = is_entry_blocked()
    if blocked:
        msg = f"⏰ Entry BLOCKED — inside {window} no-entry window (overnight-swing protection)"
        logger.warning(msg)
        try:
            queue_notification("ENTRY_BLOCKED_TIME", msg)
        except Exception:
            pass
        return {"success": False, "message": "Entry blocked by time-of-day filter", "blocked_window": window}

    logger.info(f"📈 ENTRY: {alert['direction'].upper()} @ {alert['price']} (3 MES + 3 MNQ)")
    
    current_time = time.time()
    alert_key = f"entry_{alert['direction']}_{alert['price']}"
    recent_entry_alerts[alert_key] = current_time
    
    # Check existing position
    current_position = get_active_position()
    position_key = create_position_key(alert["direction"], alert["price"])
    
    if current_position and current_position["key"] == position_key:
        logger.warning(f"⚠️ Active position already exists: {position_key}")
        return {"success": False, "message": "Position already exists", "duplicate": True}
    
    # Close existing different position
    if current_position:
        position_age = time.time() - current_position["created_at"]
        if position_age > 30:
            logger.warning(f"⚠️ Closing existing {current_position['direction']} position before new entry")
            cancel_ladder(current_position)
            cancel_tp_limits(current_position)
            cancel_runner_limits(current_position)
            cancel_stops_all_accounts(current_position)
            execute_flatten_all_accounts()
            if current_position["key"] in active_positions:
                del active_positions[current_position["key"]]
            time.sleep(0.5)
        else:
            logger.warning(f"⚠️ Existing position too new ({position_age:.1f}s), skipping")
            return {"success": False, "message": "Active position too new to close"}
    
    # Create new position
    position = create_position(alert)
    
    # Step 1: Market entry for BOTH MES and MNQ on all accounts
    logger.info(f"🤖 STEP 1: Market entry ({MES_CONTRACTS} MES + {MNQ_CONTRACTS} MNQ)...")
    side = 0 if alert["direction"] == "long" else 1  # 0=Buy, 1=Sell
    entry_results = execute_entry_all_accounts(side, MES_CONTRACTS, MNQ_CONTRACTS)
    
    # Tradovate (MES + MNQ)
    tv_mes_order_id = tv_entry(alert["direction"], MES_CONTRACTS, symbol=TRADOVATE_MES_CONTRACT_NAME)
    tv_mnq_order_id = tv_entry(alert["direction"], MNQ_CONTRACTS, symbol=TRADOVATE_MNQ_CONTRACT_NAME)
    position["tv_entry_order_id"] = tv_mes_order_id
    position["tv_mnq_entry_order_id"] = tv_mnq_order_id

    # Rithmic/Apex entry (MES + MNQ on all 4 accounts)
    rt_mes_result = rt_entry(alert["direction"], MES_CONTRACTS, symbol=RITHMIC_MES_CONTRACT)
    rt_mnq_result = rt_entry(alert["direction"], MNQ_CONTRACTS, symbol=RITHMIC_MNQ_CONTRACT)

    # Verify Rithmic actually fired — alert if Topstep fired but Apex didn't
    if RITHMIC_ENABLED and _rithmic_bridge and _rithmic_bridge.connected:
        _, _, mes_ok = _rt_summarize_result(rt_mes_result, "entry-check MES")
        _, _, mnq_ok = _rt_summarize_result(rt_mnq_result, "entry-check MNQ")
        failed_legs = []
        if not mes_ok: failed_legs.append("MES")
        if not mnq_ok: failed_legs.append("MNQ")
        if failed_legs:
            warn = f"⚠️ RITHMIC/APEX ENTRY FAILED: {'+'.join(failed_legs)} — Topstep fired, Apex did NOT"
            logger.error(warn)
            try:
                queue_notification("RITHMIC_ENTRY_FAILED", warn)
            except Exception as _qn_err:
                logger.error(f"queue_notification error: {_qn_err}")
    elif RITHMIC_ENABLED:
        warn = "⚠️ RITHMIC/APEX DISCONNECTED — Topstep fired, Apex did NOT (bridge offline)"
        logger.error(warn)
        try:
            queue_notification("RITHMIC_DISCONNECTED", warn)
        except Exception as _qn_err:
            logger.error(f"queue_notification error: {_qn_err}")
    
    # Track entry order IDs for both instruments
    mes_states = position.get("mes_account_states", {})
    mnq_states = position.get("mnq_account_states", {})
    any_success = False
    for acct_id, result in entry_results.items():
        acct_key = str(acct_id)
        if acct_key not in mes_states: mes_states[acct_key] = {}
        if acct_key not in mnq_states: mnq_states[acct_key] = {}
        mes_states[acct_key]["entry_order_id"] = result.get("mes")
        mnq_states[acct_key]["entry_order_id"] = result.get("mnq")
        if result.get("mes") or result.get("mnq"):
            any_success = True
    position["mes_account_states"] = mes_states
    position["mnq_account_states"] = mnq_states
    
    if not any_success:
        logger.error("❌ Entry failed on ALL accounts")
        if position["key"] in active_positions:
            del active_positions[position["key"]]
        queue_notification("ENTRY_REJECTED", "❌ Entry rejected - market may be closed")
        return {"success": False, "message": "Entry failed on all accounts", "contracts": 0, "strategy": "1-1-1 MES+MNQ"}
    
    logger.info("✅ Entry orders placed, waiting for fill...")
    time.sleep(0.5)
    
    # Verify MES positions and get actual fill price
    logger.info("🔍 Verifying broker positions...")
    leader_id = get_active_accounts()[0]
    
    # Get MES fill price
    _, mes_broker_pos = verify_broker_position(leader_id, expected_direction=alert["direction"], expected_size=MES_CONTRACTS)
    if mes_broker_pos and mes_broker_pos.get("averagePrice"):
        actual_mes_fill = float(mes_broker_pos["averagePrice"])
        logger.info(f"📊 MES actual fill: {actual_mes_fill} (alert said {alert['price']})")
        position["entry_price"] = actual_mes_fill
        position["best_price"] = actual_mes_fill
        position["mes_entry_price"] = actual_mes_fill
        position["mes_best_price"] = actual_mes_fill
        
        # Recalculate MES stop based on actual fill
        if position["direction"] == "long":
            position["mes_stop_price"] = round(actual_mes_fill - MES_TRAILING_STOP_POINTS, 2)
        else:
            position["mes_stop_price"] = round(actual_mes_fill + MES_TRAILING_STOP_POINTS, 2)
        position["current_stop_price"] = position["mes_stop_price"]
        logger.info(f"🛑 MES stop recalculated to {position['mes_stop_price']}")
    
    # Get MNQ fill price (need to check MNQ position on broker)
    # GUARD: only process MNQ fill/stops when MNQ is actually enabled
    if MNQ_CONTRACTS > 0:
        mnq_fill = _get_mnq_fill_price(leader_id, alert["direction"])
        if mnq_fill:
            logger.info(f"📊 MNQ actual fill: {mnq_fill}")
            position["mnq_entry_price"] = mnq_fill
            position["mnq_best_price"] = mnq_fill
            if position["direction"] == "long":
                position["mnq_stop_price"] = round(mnq_fill - MNQ_TRAILING_STOP_POINTS, 2)
            else:
                position["mnq_stop_price"] = round(mnq_fill + MNQ_TRAILING_STOP_POINTS, 2)
            logger.info(f"🛑 MNQ stop recalculated to {position['mnq_stop_price']}")
        else:
            logger.warning("⚠️ Could not get MNQ fill price — using alert price as estimate")
            # MNQ will track loosely; stop may be off
            position["mnq_entry_price"] = alert["price"]  # approximation
            position["mnq_best_price"] = alert["price"]
            if position["direction"] == "long":
                position["mnq_stop_price"] = round(alert["price"] - MNQ_TRAILING_STOP_POINTS, 2)
            else:
                position["mnq_stop_price"] = round(alert["price"] + MNQ_TRAILING_STOP_POINTS, 2)
    else:
        logger.info("🪶 MNQ leg DISABLED (MNQ_CONTRACTS=0) — skipping fill price + stop placement")
    
    # Step 2: Place stops for both instruments
    logger.info(f"🤖 STEP 2: Placing stops — MES {MES_TRAILING_STOP_POINTS}pt, MNQ {MNQ_TRAILING_STOP_POINTS}pt...")
    stop_success = place_stop_for_position(position)

    # Step 3: Place broker-side ladder (1-1-1) or uniform TP limits.
    # Ladder mode: 3 limits per leg per account at T1/T2/runner_cap (data-backed for MES).
    # Independent-TP mode: uniform 3-contract limit per leg.
    # Legacy mode (both False): Discord trim alerts drive exits.
    if LADDER_111_ENABLED:
        logger.info(f"🤖 STEP 3: Placing 1-1-1 LADDER — MES +{MES_T1_POINTS}/+{MES_T2_POINTS}/+{MES_RUNNER_AUTO_CLOSE_POINTS}, MNQ +{MNQ_T1_POINTS}/+{MNQ_T2_POINTS}/+{MNQ_RUNNER_AUTO_CLOSE_POINTS}...")
        try:
            place_ladder_all_accounts(position)
        except Exception as e:
            logger.error(f"❌ Ladder placement failed: {e}")
        # Mirror onto Apex (feature-flagged — mutually exclusive modes)
        if RITHMIC_MIRROR_ENABLED:
            logger.info("🤖 STEP 3b: Apex MIRROR mode active — stop already placed by place_stop_for_position; trims fire on T1/T2 events.")
        elif RITHMIC_LADDER_ENABLED:
            logger.info("🤖 STEP 3b: Mirroring 1-1-1 ladder onto Apex/Rithmic...")
            try:
                rt_place_apex_ladder(position)
            except Exception as e:
                logger.error(f"❌ Apex ladder placement failed: {e}")
    elif INDEPENDENT_TP_ENABLED:
        logger.info(f"🤖 STEP 3: Placing TP limits — MES +{MES_TP_POINTS}pt, MNQ +{MNQ_TP_POINTS}pt...")
        try:
            place_tp_limits_all_accounts(position)
        except Exception as e:
            logger.error(f"❌ TP limit placement failed: {e}")

    # Verify Tradovate state after entry
    if TRADOVATE_ENABLED:
        time.sleep(1)
        tv_verify_state(position, f"ENTRY {alert['direction'].upper()} @ {alert['price']}")
    
    # Telegram notification with flatten link
    strategy_name = "1-1-1 MES+MNQ"
    flatten_link = "https://bot.rapatrading.trade/flatten?token=VHibqodA0IPkgNLiGddI_w"
    stop_warning = "" if stop_success else "\n\n⚠️ <b>STOP PLACEMENT FAILED</b> - PLACE STOP MANUALLY"
    
    mnq_fill_str = f"{position['mnq_entry_price']}" if position.get('mnq_entry_price') else "pending"
    queue_notification("ENTRY",
        f"🟢 {alert['direction'].upper()} {MES_CONTRACTS} MES @ {alert['price']} + {MNQ_CONTRACTS} MNQ @ {mnq_fill_str} ({strategy_name})\n"
        f"MES stop: {position['mes_stop_price']} | MNQ stop: {position['mnq_stop_price']}\n\n"
        f"🚨 <a href=\"{flatten_link}\">EMERGENCY FLATTEN</a>{stop_warning}",
        {"direction": alert["direction"], "price": alert["price"],
         "mes_contracts": MES_CONTRACTS, "mnq_contracts": MNQ_CONTRACTS,
         "mes_stop": position["mes_stop_price"], "mnq_stop": position["mnq_stop_price"],
         "strategy": strategy_name, "accounts": len(get_active_accounts()),
         "stop_failed": not stop_success})
    
    save_position_state()
    
    return {
        "success": True,
        "message": f"Entry {alert['direction'].upper()} {MES_CONTRACTS} MES + {MNQ_CONTRACTS} MNQ @ {alert['price']} — MES stop {position['mes_stop_price']}, MNQ stop {position['mnq_stop_price']}",
        "direction": alert["direction"],
        "price": alert["price"],
        "mes_contracts": MES_CONTRACTS,
        "mnq_contracts": MNQ_CONTRACTS,
        "mes_stop": position["mes_stop_price"],
        "mnq_stop": position["mnq_stop_price"],
        "manual_stop_required": not stop_success,
        "accounts_traded": len([v for v in entry_results.values() if v.get("mes") or v.get("mnq")]),
    }

def _get_mnq_fill_price(account_id, expected_direction):
    """Get MNQ fill price from broker position."""
    if not _mnq_contract_id:
        return None
    try:
        data = px_api("/api/Position/searchOpen", {"accountId": account_id})
        if not data or not data.get("positions"):
            return None
        for p in data["positions"]:
            if p.get("contractId") == _mnq_contract_id:
                avg = p.get("averagePrice")
                if avg:
                    return float(avg)
        return None
    except Exception as e:
        logger.error(f"❌ Failed to get MNQ fill price: {e}")
        return None

def process_trim_alert(alert, trim_type):
    logger.info(f"✂️ PROCESSING {trim_type.upper()} (MES + MNQ)")
    
    position = get_active_position()
    if not position:
        logger.warning("⚠️ No active position for trim")
        return {"success": False, "message": "No active position"}
    
    # Record trim to prevent duplicates
    alert_hash = hashlib.md5(f"{trim_type}_{alert['text'][:100]}".encode()).hexdigest()[:12]
    recent_trim_alerts[f"trim_{alert_hash}"] = time.time()
    
    if not validate_position_state(position):
        return {"success": False, "message": "Position state invalid"}
    
    if position["fully_trimmed"]:
        return {"success": True, "message": "Already fully trimmed", "already_processed": True}
    
    if trim_type == "first_trim" and position["first_trim"]:
        return {"success": True, "message": "First trim already done", "already_processed": True}
    if trim_type == "second_trim" and position["second_trim"]:
        return {"success": True, "message": "Second trim already done", "already_processed": True}
    if trim_type == "third_trim" and position.get("third_trim"):
        return {"success": True, "message": "Third trim already done", "already_processed": True}
    if trim_type == "fourth_trim" and position.get("fourth_trim"):
        return {"success": True, "message": "Fourth trim already done", "already_processed": True}

    # T3/T4 in non-ladder mode: behave like exit_runner (close all remaining).
    # In 3-1-1 ladder mode the LADDER_111 branch below short-circuits anyway (broker manages exits).
    _original_trim = trim_type
    if trim_type in ("third_trim", "fourth_trim"):
        position[trim_type] = True
        if not LADDER_111_ENABLED:
            # ensure update_position_after_trim's exit_runner branch is willing to fire
            position["runner_active"] = True
            trim_type = "exit_runner"

    # 1-1-1 ladder mode: trim alerts are diagnostic-only (broker manages exits)
    if LADDER_111_ENABLED:
        logger.info(f"🪜 LADDER mode — ignoring {trim_type} alert (broker manages exits)")
        return {"success": True, "message": f"{trim_type} ignored — ladder mode active", "ladder_mode": True}

    # Flatten-at-first-trim mode: treat first_trim as full close (no runner)
    if FLATTEN_AT_FIRST_TRIM and trim_type == "first_trim":
        logger.info("🚩 FLATTEN_AT_FIRST_TRIM enabled — flattening all positions on first trim alert")
        cancel_tp_limits(position)  # Independent-TP safety override
        cancel_runner_limits(position)
        cancel_stops_all_accounts(position)
        results = execute_flatten_all_accounts()
        if TRADOVATE_ENABLED:
            tv_cancel_all_orders()
            tv_flatten()
        rt_flatten()  # Rithmic/Apex
        position["first_trim"] = True
        position["second_trim"] = True
        position["fully_trimmed"] = True
        position["mes_remaining"] = 0
        position["mnq_remaining"] = 0
        position["remaining_contracts"] = 0
        position["runner_active"] = False
        position["status"] = "closed"
        position["trim_history"].append({
            "type": "flatten_at_first_trim", "timestamp": time.time()
        })
        queue_notification("Flatten at T1",
            "🏁 FLATTEN AT T1: Full position closed (no-runner mode)",
            {"direction": position["direction"]})
        close_position(position, "flatten_at_first_trim")
        return {"success": True, "message": "Flattened at first trim"}

    # Determine contracts to close (both MES and MNQ)
    updated, mes_to_close, mnq_to_close = update_position_after_trim(position, trim_type)
    if not updated:
        return {"success": False, "message": f"{trim_type} update failed"}
    
    # Execute partial close on all accounts for BOTH instruments
    if trim_type == "exit_runner":
        cancel_runner_limits(position)
        cancel_stops_all_accounts(position)
        results = execute_flatten_all_accounts()
        tv_cancel_all_orders(); tv_flatten()  # Tradovate
        rt_flatten()  # Rithmic/Apex
    else:
        results = execute_partial_close_all_accounts(mes_to_close, mnq_to_close)
        # Tradovate: partial close MES + cancel stops
        tv_trim(position["direction"], mes_to_close)
        logger.info("Tradovate: cancelling ALL stop orders after trim")
        tv_cancel_all_stops()
        rt_cancel_all()  # Rithmic/Apex
        position["tv_stop_order_id"] = None
    
    # Check success (any account succeeded for either instrument)
    any_success = False
    if results:
        for v in results.values():
            if isinstance(v, dict):
                if v.get("mes") or v.get("mnq"):
                    any_success = True
                    break
            elif v:
                any_success = True
                break
    
    if any_success:
        mes_rem = position.get("mes_remaining", 0)
        mnq_rem = position.get("mnq_remaining", 0)
        logger.info(f"✅ {trim_type}: MES -{mes_to_close} (rem:{mes_rem}), MNQ -{mnq_to_close} (rem:{mnq_rem})")
        
        # Record trim price for stop calculation
        leader_id = get_active_accounts()[0]
        mes_trim_price = position.get("mes_best_price", position.get("entry_price", 0))
        mnq_trim_price = position.get("mnq_best_price", position.get("mnq_entry_price", 0))
        position["last_trim_price"] = mes_trim_price
        position["mnq_last_trim_price"] = mnq_trim_price
        logger.info(f"📊 Trim prices — MES: {mes_trim_price}, MNQ: {mnq_trim_price}")
        
        # Update stops or close position
        if trim_type == "exit_runner":
            close_position(position, "runner_exit")
        elif mes_rem <= 0 and mnq_rem <= 0:
            logger.info("📤 Position fully closed after second trim (no runner)")
            cancel_stops_all_accounts(position)
            if TRADOVATE_ENABLED:
                tv_cancel_all_orders()
            close_position(position, "second_trim_complete")
        else:
            # Update stops (runner breakeven or post-trim tighter stops)
            update_stop_after_trim(position)
            
            if position.get("runner_active") and position.get("breakeven_stop_set"):
                mes_entry = position.get("mes_entry_price", position["entry_price"])
                mnq_entry = position.get("mnq_entry_price", 0)
                
                # Place limit take-profit orders for runner auto-close
                place_runner_limits_all_accounts(position)
                
                mes_target = position.get("mes_runner_target", "N/A")
                mnq_target = position.get("mnq_runner_target", "N/A")
                queue_notification("RUNNER_ACTIVE",
                    f"🏃 RUNNER RISK-FREE:\n"
                    f"MES: 1 @ breakeven {mes_entry}, limit TP @ {mes_target} (+{MES_RUNNER_AUTO_CLOSE_POINTS}pts)\n"
                    f"MNQ: 1 @ breakeven {mnq_entry}, limit TP @ {mnq_target} (+{MNQ_RUNNER_AUTO_CLOSE_POINTS}pts)",
                    {"mes_breakeven": mes_entry, "mnq_breakeven": mnq_entry,
                     "mes_target": mes_target, "mnq_target": mnq_target})
        
        trim_names = {"first_trim": "First Trim", "second_trim": "Second Trim", "third_trim": "Third Trim", "fourth_trim": "Fourth Trim", "exit_runner": "Runner Exit"}
        queue_notification(trim_names.get(trim_type, trim_type),
            f"✂️ {trim_names.get(trim_type)}:\n"
            f"MES: -{mes_to_close} (rem: {mes_rem}) | MNQ: -{mnq_to_close} (rem: {mnq_rem})",
            {"mes_closed": mes_to_close, "mnq_closed": mnq_to_close,
             "mes_remaining": mes_rem, "mnq_remaining": mnq_rem,
             "direction": position["direction"]})
        
        save_position_state()
        
        if TRADOVATE_ENABLED:
            time.sleep(1)
            tv_verify_state(position if trim_type != "exit_runner" else None,
                f"{trim_type.upper().replace('_', ' ')} (MES rem:{mes_rem}, MNQ rem:{mnq_rem})")
        
        return {
            "success": True,
            "message": f"{trim_type}: MES -{mes_to_close}, MNQ -{mnq_to_close}",
            "mes_sold": mes_to_close,
            "mnq_sold": mnq_to_close,
            "mes_remaining": mes_rem,
            "mnq_remaining": mnq_rem,
            "position_closed": (trim_type == "exit_runner"),
        }
    else:
        logger.error(f"❌ {trim_type} FAILED on all accounts")
        logger.warning("⚠️ Clearing position — likely already flat")
        cancel_runner_limits(position)
        close_position(position, "broker_flat_detected")
        save_position_state()
        return {"success": False, "message": f"{trim_type} failed — position cleared"}

def process_stop_alert(alert):
    logger.info("🛑 PROCESSING STOP ALERT (closing BOTH MES and MNQ)")
    position = get_active_position()
    if not position:
        return {"success": False, "message": "No active position"}
    
    mes_rem = position.get("mes_remaining", 0)
    mnq_rem = position.get("mnq_remaining", 0)
    
    if mes_rem > 0 or mnq_rem > 0:
        # Cancel stops, all limit-order ladders, and flatten BOTH instruments
        cancel_ladder(position)
        cancel_tp_limits(position)
        cancel_runner_limits(position)
        cancel_stops_all_accounts(position)
        results = execute_flatten_all_accounts()
        tv_cancel_all_orders(); tv_flatten()  # Tradovate
        rt_flatten()  # Rithmic/Apex
        
        any_success = any(results.values()) if results else False
        if any_success:
            logger.info(f"✅ Stop executed: flattened {mes_rem} MES + {mnq_rem} MNQ")
            queue_notification("STOPPED",
                f"🛑 STOPPED: {mes_rem} MES + {mnq_rem} MNQ closed",
                {"entry_price": position["entry_price"], "direction": position["direction"],
                 "mes_stop": position.get("mes_stop_price"), "mnq_stop": position.get("mnq_stop_price")})
            close_position(position, "stopped")
            save_position_state()
            if TRADOVATE_ENABLED:
                time.sleep(1)
                tv_verify_state(None, "STOPPED OUT")
            return {"success": True, "message": f"Stopped: flattened {mes_rem} MES + {mnq_rem} MNQ"}
        else:
            logger.error("❌ Failed to flatten on stop")
            return {"success": False, "message": "Failed to flatten"}
    else:
        close_position(position, "stopped")
        return {"success": True, "message": "Already flat"}

def process_close_all():
    logger.info("🚨 CLOSE ALL")
    
    if not active_positions:
        # Still flatten at broker in case of desync
        execute_flatten_all_accounts()
        tv_cancel_all_orders(); tv_flatten()  # Tradovate
        rt_flatten()  # Rithmic/Apex
        return {"success": True, "message": "No tracked positions, flattened broker"}
    
    positions_closed = 0
    for pos_key, position in list(active_positions.items()):
        cancel_ladder(position)
        cancel_tp_limits(position)
        cancel_runner_limits(position)
        cancel_stops_all_accounts(position)
    
    results = execute_flatten_all_accounts()
    tv_cancel_all_orders(); tv_flatten()  # Tradovate
    rt_flatten()  # Rithmic/Apex
    
    queue_notification("EMERGENCY_CLOSE",
        f"🚨 EMERGENCY CLOSE: {len(active_positions)} positions flattened",
        {"positions": len(active_positions)})
    
    active_positions.clear()
    save_position_state()
    
    return {"success": True, "message": f"All positions flattened on {len(get_active_accounts())} accounts"}

# ================= POSITION SYNC MONITOR =================
def sync_check_positions():
    """Check if Topstep and Apex are in sync. Auto-flatten Apex if Topstep is flat."""
    logger.info("🔄 Running position sync check...")
    
    # Check Topstep leader position
    topstep_flat = True
    topstep_position = None
    
    match, broker_pos = verify_broker_position(get_active_accounts()[0])  # Use first active account (leader)
    if broker_pos and broker_pos.get("size", 0) != 0:
        topstep_flat = False
        topstep_position = broker_pos
        logger.info(f"📊 Topstep: {broker_pos.get('size')} contracts @ {broker_pos.get('averagePrice')}")
    else:
        logger.info("📊 Topstep: FLAT")
    
    # Check bot internal state
    bot_position = get_active_position()
    if bot_position:
        logger.info(f"🤖 Bot state: {bot_position['remaining_contracts']} contracts {bot_position['direction']} @ {bot_position['entry_price']}")
    else:
        logger.info("🤖 Bot state: FLAT")
    
    # Sync logic
    if topstep_flat and bot_position:
        # Check position age - don't sync if position is too new (API may not have updated yet)
        position_age = bot_position.get("age_seconds", 0)
        if position_age < 60:
            logger.info(f"⏳ Position only {position_age}s old - skipping desync check (API may be slow)")
            return True
        
        # Topstep is flat but bot thinks we have a position - likely stopped out externally
        logger.warning("⚠️ DESYNC: Topstep flat but bot has position - clearing bot state and flattening Apex")
        
        # Clear bot position
        if bot_position["key"] in active_positions:
            del active_positions[bot_position["key"]]
        save_position_state()
        
        # Flatten Apex to be safe
        tv_cancel_all_orders(); tv_flatten()
        rt_flatten()  # Rithmic/Apex
        
        # Send notification
        queue_notification("SYNC_CORRECTION", 
            "🔄 Detected position desync - cleared bot state and flattened Apex",
            {"reason": "Topstep flat, bot had position"})
        
        logger.info("✅ Sync corrected - all flat")
        return True
    
    elif topstep_flat and not bot_position:
        # Both flat - send flatten to Apex just to be safe 
        logger.info("💡 Both Topstep and bot flat - sending safety flatten to Apex")
        tv_cancel_all_orders(); tv_flatten()
        rt_flatten()  # Rithmic/Apex
        return True
    
    elif not topstep_flat and not bot_position:
        # Topstep has position but bot doesn't - likely manual entry
        logger.info(f"💡 Topstep has position but bot doesn't - manual trade detected")
        # Don't auto-create bot position - let user manage manually
        return True
    
    else:
        # Normal case - both have positions or both flat and synced
        logger.info("✅ Positions appear synced")
        return True

# ================= TELEGRAM NOTIFICATIONS =================
def load_telegram_config():
    try:
        if os.path.exists(TELEGRAM_CONFIG_FILE):
            with open(TELEGRAM_CONFIG_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load Telegram config: {e}")
    return None

def send_telegram_alert(message):
    config = load_telegram_config()
    if not config:
        return False
    try:
        url = f"https://api.telegram.org/bot{config['bot_token']}/sendMessage"
        payload = {"chat_id": config["chat_id"], "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}
        response = requests.post(url, json=payload, timeout=10)
        result = response.json()
        if result.get("ok"):
            logger.info("📱 Telegram alert sent")
            return True
        else:
            logger.error(f"Telegram error: {result}")
            return False
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return False

def _file_queue_notification(event_type, message, details=None):
    """Write notification to file as backup"""
    try:
        notification = {
            "timestamp": datetime.now().isoformat(),
            "epoch": time.time(),
            "type": event_type,
            "message": message,
            "details": details or {},
            "delivered": False
        }
        with open(NOTIFICATION_FILE, "a") as f:
            f.write(json.dumps(notification) + "\n")
    except Exception as e:
        logger.error(f"Failed to queue notification: {e}")

def queue_notification(event_type, message, details=None):
    """Queue notification AND send via Telegram"""
    _file_queue_notification(event_type, message, details)
    
    telegram_msg = f"<b>🤖 ProjectX Bot — {event_type}</b>\n{message}"
    if details:
        for k in ["direction", "price", "contracts", "stop_price", "remaining", "accounts"]:
            if k in details:
                telegram_msg += f"\n{k.replace('_',' ').title()}: {details[k]}"
    
    send_telegram_alert(telegram_msg)
# ================= AUTO-TRIM FAILSAFE =================
_auto_trim_thread = None
_auto_trim_running = False

def get_current_price():
    """Get current MES price from ProjectX API."""
    try:
        # Try GET request for quotes (some APIs need GET)
        token = px_get_token()
        if not token:
            return None
        
        headers = {"Authorization": f"Bearer {token}"}
        url = f"{PROJECTX_BASE_URL}/api/Quote/quotes?contractIds={_mes_contract_id}"
        
        rate_limit()
        resp = requests.get(url, headers=headers, timeout=10)
        
        if resp.status_code == 200:
            try:
                data = resp.json()
                if data and data.get("quotes"):
                    quote = data["quotes"][0]
                    last = quote.get("lastPrice") or quote.get("last")
                    if last:
                        return float(last)
                    bid = quote.get("bidPrice") or quote.get("bid") or 0
                    ask = quote.get("askPrice") or quote.get("ask") or 0
                    if bid and ask:
                        return (float(bid) + float(ask)) / 2
            except:
                pass
        
        # Fallback: try POST method
        data = px_api("/api/Quote/quotes", {"contractIds": [_mes_contract_id]})
        if data and data.get("quotes"):
            quote = data["quotes"][0]
            last = quote.get("lastPrice") or quote.get("last")
            if last:
                return float(last)
            bid = quote.get("bidPrice") or quote.get("bid") or 0
            ask = quote.get("askPrice") or quote.get("ask") or 0
            if bid and ask:
                return (float(bid) + float(ask)) / 2
        
        return None
    except Exception as e:
        logger.error(f"❌ Failed to get price: {e}")
        return None

def check_auto_trim():
    """Check if auto-trim should trigger based on current price."""
    if not AUTO_TRIM_ENABLED:
        return
    
    position = get_active_position()
    if not position:
        return  # No position, nothing to monitor
    
    current_price = get_current_price()
    if current_price is None:
        logger.warning("⚠️ Auto-trim: Could not get current price")
        return
    
    # Log price check periodically (every ~30 seconds based on 2s interval)
    check_count = getattr(check_auto_trim, '_count', 0) + 1
    check_auto_trim._count = check_count
    if check_count % 15 == 1:  # Log every 15th check (~30 seconds)
        runner_status = "active" if position.get("runner_active") else "inactive"
        logger.info(f"🤖 Auto-trim monitoring: price={current_price}, entry={position['entry_price']}, T1={not position.get('first_trim')}, T2={not position.get('second_trim')}, Runner={runner_status}")
    
    entry_price = position["entry_price"]
    direction = position["direction"]
    
    # ========== RUNNER AUTO-CLOSE (MES at +20, triggers both MES+MNQ close) ==========
    if position.get("runner_active") and position.get("mes_remaining", 0) == 1:
        runner_target = entry_price + MES_RUNNER_AUTO_CLOSE_POINTS if direction == "long" else entry_price - MES_RUNNER_AUTO_CLOSE_POINTS
        runner_hit = (current_price >= runner_target) if direction == "long" else (current_price <= runner_target)
        
        if runner_hit:
            logger.info(f"🏆 RUNNER AUTO-CLOSE: MES price {current_price} hit +{MES_RUNNER_AUTO_CLOSE_POINTS} target @ {runner_target}")
            fake_alert = {"type": "exit_runner", "text": f"AUTO-RUNNER-EXIT @ {current_price} (+{MES_RUNNER_AUTO_CLOSE_POINTS} MES points)"}
            result = process_trim_alert(fake_alert, "exit_runner")
            if result.get("success"):
                queue_notification("RUNNER_AUTO_CLOSE",
                    f"🏆 RUNNER AUTO-CLOSED: MES +{MES_RUNNER_AUTO_CLOSE_POINTS} pts @ {current_price}! (MNQ also closed)",
                    {"price": current_price, "target": runner_target, "profit_points": MES_RUNNER_AUTO_CLOSE_POINTS, "auto": True})
            return
    # ========== END RUNNER LOGIC ==========
    
    # Calculate targets for T1/T2
    if direction == "long":
        t1_target = entry_price + AUTO_TRIM_T1_POINTS
        t2_target = entry_price + AUTO_TRIM_T2_POINTS
        t1_hit = current_price >= t1_target
        t2_hit = current_price >= t2_target
    else:  # short
        t1_target = entry_price - AUTO_TRIM_T1_POINTS
        t2_target = entry_price - AUTO_TRIM_T2_POINTS
        t1_hit = current_price <= t1_target
        t2_hit = current_price <= t2_target
    
    # Check T2 first (if T2 hit, T1 should also be done)
    if t2_hit and not position.get("second_trim", False) and position.get("first_trim", False):
        logger.info(f"🤖 AUTO-TRIM T2: Price {current_price} hit target {t2_target}")
        # Create a fake alert for processing
        fake_alert = {"type": "second_trim", "text": f"AUTO-TRIM T2 @ {current_price}"}
        result = process_trim_alert(fake_alert, "second_trim")
        if result.get("success"):
            queue_notification("AUTO_TRIM_T2",
                f"🤖 AUTO-TRIM T2: Closed {result.get('contracts_sold', 4)} @ {current_price}",
                {"price": current_price, "target": t2_target, "auto": True})
        return
    
    # Check T1
    if t1_hit and not position.get("first_trim", False):
        logger.info(f"🤖 AUTO-TRIM T1: Price {current_price} hit target {t1_target}")
        fake_alert = {"type": "first_trim", "text": f"AUTO-TRIM T1 @ {current_price}"}
        result = process_trim_alert(fake_alert, "first_trim")
        if result.get("success"):
            queue_notification("AUTO_TRIM_T1",
                f"🤖 AUTO-TRIM T1: Closed {result.get('contracts_sold', 2)} @ {current_price}, stop moved to entry-7",
                {"price": current_price, "target": t1_target, "auto": True})
        return

def auto_trim_monitor():
    """Background thread that monitors price for auto-trim."""
    global _auto_trim_running
    logger.info("🤖 Auto-trim monitor started")
    
    while _auto_trim_running:
        try:
            check_auto_trim()
        except Exception as e:
            logger.error(f"❌ Auto-trim error: {e}")
        
        time.sleep(AUTO_TRIM_CHECK_INTERVAL)
    
    logger.info("🤖 Auto-trim monitor stopped")

def start_auto_trim_monitor():
    """Start the auto-trim background thread."""
    global _auto_trim_thread, _auto_trim_running
    if _auto_trim_thread and _auto_trim_thread.is_alive():
        return
    
    _auto_trim_running = True
    _auto_trim_thread = threading.Thread(target=auto_trim_monitor, daemon=True)
    _auto_trim_thread.start()
    logger.info("🤖 Auto-trim monitor thread started")

def stop_auto_trim_monitor():
    """Stop the auto-trim background thread."""
    global _auto_trim_running
    _auto_trim_running = False
    logger.info("🤖 Auto-trim monitor stopping...")
# ================= HTTP SERVER =================
class TradingHandler(BaseHTTPRequestHandler):
    
    def do_GET(self):
        if self.path == "/":
            self._send_status_page()
            return
        if self.path == "/health":
            self._json_response(200, {
                "status": "healthy",
                "server_time": datetime.now().isoformat(),
                "active_positions": len(active_positions),
                "projectx_connected": _api_token is not None,
                "tradovate_connected": _tv_token is not None,
                "tradovate_enabled": TRADOVATE_ENABLED,
                "rithmic_connected": _rithmic_bridge.connected if _rithmic_bridge else False,
                "rithmic_enabled": RITHMIC_ENABLED,
                "rithmic_env": RITHMIC_ENV,
                "rithmic_accounts": len(_rithmic_bridge.client.accounts) if _rithmic_bridge and _rithmic_bridge.connected else 0,
                "px_contract_id": _mes_contract_id,
                "tv_contract_id": _tv_contract_id,
                "px_accounts": len(get_active_accounts()),
                "tv_account": TRADOVATE_ACCOUNT_SPEC if TRADOVATE_ENABLED else None,
            })
            return
        if self.path == "/health_old":
            self._json_response(200, {
                "status": "healthy",
                "server_time": datetime.now().isoformat(),
                "active_positions": len(active_positions),
                "projectx_connected": _api_token is not None,
                "tradovate_connected": _tv_token is not None,
                "tradovate_enabled": TRADOVATE_ENABLED,
                "rithmic_connected": _rithmic_bridge.connected if _rithmic_bridge else False,
                "rithmic_enabled": RITHMIC_ENABLED,
                "rithmic_env": RITHMIC_ENV,
                "rithmic_accounts": len(_rithmic_bridge.client.accounts) if _rithmic_bridge and _rithmic_bridge.connected else 0,
                "px_contract_id": _mes_contract_id,
                "tv_contract_id": _tv_contract_id,
                "px_accounts": len(get_active_accounts()),
                "tv_account": TRADOVATE_ACCOUNT_SPEC if TRADOVATE_ENABLED else None,
            })
            return
        if self.path == "/status":
            self._send_status()
            return
        if self.path == "/dashboard":
            self._send_pnl_dashboard()
            return
        if self.path == "/positions":
            self._send_positions()
            return
        if self.path == "/clear":
            active_positions.clear()
            recent_trades.clear()
            recent_trim_alerts.clear()
            recent_entry_alerts.clear()
            save_position_state()
            self._json_response(200, {"cleared": True, "message": "All cleared"})
            return
        if self.path.startswith("/flatten"):
            # Token validation
            if "token=VHibqodA0IPkgNLiGddI_w" not in self.path:
                logger.warning("FLATTEN rejected: invalid token")
                self._json_response(403, {"error": "Invalid token"})
                return
            # Emergency flatten - close all positions at broker
            logger.info("🚨 FLATTEN requested via HTTP")
            for pos_key, position in list(active_positions.items()):
                cancel_runner_limits(position)
                cancel_stops_all_accounts(position)
            results = execute_flatten_all_accounts()
            tv_cancel_all_orders(); tv_flatten()
            rt_flatten()  # Rithmic/Apex
            active_positions.clear()
            save_position_state()
            queue_notification("FLATTEN", "🚨 EMERGENCY FLATTEN executed via link", {"source": "http"})
            self._json_response(200, {"flattened": True, "message": "All positions flattened"})
            return
        if self.path == "/clear_alerts":
            recent_trades.clear()
            recent_trim_alerts.clear()
            recent_entry_alerts.clear()
            self._json_response(200, {"cleared": True, "message": "Alerts cleared, positions preserved",
                                       "active_positions": len(active_positions)})
            return
        if self.path == "/broker_positions":
            self._send_broker_positions()
            return
        if self.path == "/broker_orders":
            self._send_broker_orders()
            return
        if self.path == "/sync_check":
            sync_success = sync_check_positions()
            self._json_response(200, {
                "sync_checked": True, 
                "success": sync_success,
                "message": "Position sync check completed",
                "timestamp": datetime.now().isoformat()
            })
            return
        if self.path == "/setup":
            self._send_setup_wizard()
            return
        if self.path == "/api/config":
            self._send_config_json()
            return
        self.send_response(404)
        self.end_headers()
    
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
    
    def do_POST(self):
        if self.path == "/api/config":
            self._handle_config_save()
            return
        if self.path != "/alert":
            self.send_response(404)
            self.end_headers()
            return
        
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        
        try:
            raw_data = json.loads(body)

            # Normalize alert format (Discord extension, TradingView webhook, generic)
            normalized = normalize_alert(raw_data, raw_data.get("source", "unknown"))
            alert_text = normalized["text"]
            client_timestamp = normalized.get("timestamp")
            alert_source = normalized["source"]

            if normalized.get("metadata", {}).get("ticker"):
                logger.info(f"📊 TradingView alert normalized: {normalized['metadata']['ticker']} {normalized['metadata'].get('direction', '')}")

            logger.info(f"📨 ALERT from {alert_source}: {alert_text[:100]}...")
            
            if is_alert_too_old(client_timestamp):
                self._json_response(409, {"error": "Alert too old", "max_age": MAX_ALERT_AGE_SECONDS})
                return
            
            alert = parse_alert(alert_text)
            if not alert:
                self._json_response(400, {"error": "Not a valid trading alert", "received": alert_text[:150]})
                return
            
            if is_duplicate_alert(alert, client_timestamp):
                self._json_response(409, {"error": "Duplicate alert", "type": alert.get("type")})
                return
            
            logger.info(f"🔄 PROCESSING: {alert['type']}")
            result = process_alert(alert)
            
            response_data = {
                "success": result.get("success", False),
                "alert_type": alert.get("type"),
                "processed": True,
                "timestamp": datetime.now().isoformat(),
            }
            response_data.update(result)
            
            self._json_response(200, response_data)
            
            save_position_state()
            logger.info(f"✅ DONE: {alert.get('type')} — {result.get('message', '')}")
            
        except Exception as e:
            logger.error(f"❌ ERROR: {e}")
            import traceback
            logger.error(traceback.format_exc())
            self._json_response(500, {"error": str(e)})
    
    def _send_setup_wizard(self):
        """Serve the setup wizard HTML."""
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        html_path = os.path.join(os.path.dirname(__file__), "setup_wizard.html")
        if os.path.exists(html_path):
            with open(html_path, "r") as f:
                html = f.read()
        else:
            html = "<!-- Setup wizard not found at " + html_path + " -->"
        self.wfile.write(html.encode('utf-8'))

    def _send_config_json(self):
        """Return current configuration as JSON."""
        self._json_response(200, _cfg.to_dict())

    def _handle_config_save(self):
        """Save configuration from POST body."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            new_config = json.loads(body)

            from config_loader import ConfigNode, save_config, init_config
            save_config(ConfigNode(new_config))
            init_config()  # reload

            self._json_response(200, {"success": True, "message": "Configuration saved. Restart bot to apply all changes."})
        except Exception as e:
            logger.error(f"Config save error: {e}")
            import traceback
            logger.error(traceback.format_exc())
            self._json_response(500, {"success": False, "error": str(e)})

    def _json_response(self, code, data):
        self.send_response(code)
        self.send_header("Content-type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2).encode())
    
    def _send_status_page(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        
        accts = ("TEST MODE — " if TEST_MODE else "") + ", ".join(str(a) for a in get_active_accounts())
        pos_count = len(active_positions)
        mes_name = _mes_contract_info.get("name", "MES") if _mes_contract_info else "MES"
        mnq_name = _mnq_contract_info.get("name", "MNQ") if _mnq_contract_info else "MNQ"
        
        html = f"""<!DOCTYPE html><html><head><title>🎯 MES+MNQ Bot — ProjectX + Tradovate + Rithmic</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }}
            .container {{ max-width: 1200px; margin: 0 auto; background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
            h1 {{ color: #2c3e50; border-bottom: 3px solid #27ae60; padding-bottom: 10px; }}
            .status {{ background: #e8f8e8; padding: 15px; border-radius: 5px; margin: 15px 0; }}
            .nav a {{ display: inline-block; background: #27ae60; color: white; padding: 10px 15px; margin: 5px; text-decoration: none; border-radius: 5px; }}
            .nav a.highlight {{ background: #e74c3c; font-weight: bold; }}
            .info-box {{ background: #f8f9fa; border-left: 4px solid #27ae60; padding: 15px; margin: 15px 0; }}
        </style></head><body><div class="container">
        <h1>🎯 MES + MNQ Trading Bot — ProjectX + Tradovate + Rithmic</h1>
        <div class="status">✅ Server running — ProjectX + {'Tradovate ✅' if TRADOVATE_ENABLED else 'Tradovate ❌'} + {'Rithmic ✅ (' + str(len(_rithmic_bridge.client.accounts)) + ' accts, ' + RITHMIC_ENV + ')' if _rithmic_bridge and _rithmic_bridge.connected else 'Rithmic ❌'} — {pos_count} active positions</div>
        <div class="nav">
            <a href="/dashboard" class="highlight">📊 P&L Dashboard</a>
            <a href="/status">Status</a>
            <a href="/positions">Positions</a>
            <a href="/broker_positions">Broker Positions</a>
            <a href="/broker_orders">Broker Orders</a>
            <a href="/sync_check">Sync Check</a>
            <a href="/clear">Clear All</a>
            <a href="/clear_alerts">Clear Alerts</a>
        </div>
        <div class="info-box">
            <h3>📊 Config</h3>
            <p><b>MES:</b> {mes_name} (ID: {_mes_contract_id}) — {MES_CONTRACTS} → {'FLATTEN AT T1 (no runner)' if FLATTEN_AT_FIRST_TRIM else f'-{MES_FIRST_TRIM_CONTRACTS} (T1) → -{MES_SECOND_TRIM_CONTRACTS} (T2) → {MES_RUNNER_CONTRACTS} runner (+{MES_RUNNER_AUTO_CLOSE_POINTS}pts)'}</p>
            <p><b>MNQ:</b> {mnq_name} (ID: {_mnq_contract_id}) — {MNQ_CONTRACTS} → {'FLATTEN AT T1 (no runner)' if FLATTEN_AT_FIRST_TRIM else f'-{MNQ_FIRST_TRIM_CONTRACTS} (T1) → -{MNQ_SECOND_TRIM_CONTRACTS} (T2) → {MNQ_RUNNER_CONTRACTS} runner (+{MNQ_RUNNER_AUTO_CLOSE_POINTS}pts)'}</p>
            <p><b>MES Stop:</b> {MES_TRAILING_STOP_POINTS}pt initial{'' if FLATTEN_AT_FIRST_TRIM else f', {MES_FIRST_TRIM_STOP_POINTS}pt after T1'}</p>
            <p><b>MNQ Stop:</b> {MNQ_TRAILING_STOP_POINTS}pt initial{'' if FLATTEN_AT_FIRST_TRIM else f', {MNQ_FIRST_TRIM_STOP_POINTS}pt after T1'}</p>
            <p><b>Mode:</b> {'🪜 1-1-1 BROKER LADDER' if LADDER_111_ENABLED else ('🏁 FLATTEN AT FIRST TRIM' if FLATTEN_AT_FIRST_TRIM else 'Standard (T1 → T2 → Runner)')}</p>
            <p><b>EOD Auto-Flatten:</b> {EOD_FLATTEN_HOUR:02d}:{EOD_FLATTEN_MINUTE:02d} ET (before Topstep 4:10pm)</p>
            <p><b>Accounts:</b> {accts}</p>
            <p><b>API:</b> Direct ProjectX — native partial close, no opposing orders</p>
            <p><b>Rithmic/Apex:</b> {'🟢 Connected — ' + RITHMIC_ENV.upper() + ' — ' + ', '.join(_rithmic_bridge.client.accounts) if _rithmic_bridge and _rithmic_bridge.connected else '🔴 Disabled/Disconnected'}</p>
            <p><b>Rithmic Contracts:</b> {RITHMIC_MES_CONTRACT} + {RITHMIC_MNQ_CONTRACT} ({RITHMIC_EXCHANGE})</p>
        </div>
        </div></body></html>"""
        self.wfile.write(html.encode('utf-8'))
    
    def _send_status(self):
        positions_list = []
        for position in active_positions.values():
            positions_list.append({
                "key": position["key"],
                "direction": position["direction"],
                "mes_entry": position.get("mes_entry_price"),
                "mnq_entry": position.get("mnq_entry_price"),
                "mes_remaining": position.get("mes_remaining", 0),
                "mnq_remaining": position.get("mnq_remaining", 0),
                "first_trim": position["first_trim"],
                "second_trim": position["second_trim"],
                "runner_active": position.get("runner_active", False),
                "mes_stop": position.get("mes_stop_price"),
                "mnq_stop": position.get("mnq_stop_price"),
                "age_seconds": int(time.time() - position["created_at"]),
                "status": position.get("status", "active"),
            })
        
        cloud_status = "local"
        if _CLOUD_CLIENT is not None:
            cloud_status = "active" if _CLOUD_SUBSCRIPTION_ACTIVE else "lapsed"
        self._json_response(200, {
            "status": "running",
            "mode": "projectx_tradovate_rithmic",
            "time": datetime.now().isoformat(),
            "api_connected": _api_token is not None,
            "cloud": {
                "enabled": _CLOUD_CLIENT is not None,
                "status": cloud_status,
                "email": _CLOUD_CLIENT.user.get("email") if _CLOUD_CLIENT else None,
                "plan": _CLOUD_CLIENT.user.get("plan_tier") if _CLOUD_CLIENT else None,
            },
            "mes_contract_id": _mes_contract_id,
            "mnq_contract_id": _mnq_contract_id,
            "accounts": len(get_active_accounts()),
            "active_positions": len(active_positions),
            "positions": positions_list,
            "config": {
                "strategy": "1-1-1 MES+MNQ",
                "mes": {
                    "contracts": MES_CONTRACTS, "first_trim": MES_FIRST_TRIM_CONTRACTS,
                    "second_trim": MES_SECOND_TRIM_CONTRACTS, "runner": MES_RUNNER_CONTRACTS,
                    "stop_points": MES_TRAILING_STOP_POINTS, "stop_after_t1": MES_FIRST_TRIM_STOP_POINTS,
                    "runner_auto_close": MES_RUNNER_AUTO_CLOSE_POINTS,
                },
                "mnq": {
                    "contracts": MNQ_CONTRACTS, "first_trim": MNQ_FIRST_TRIM_CONTRACTS,
                    "second_trim": MNQ_SECOND_TRIM_CONTRACTS, "runner": MNQ_RUNNER_CONTRACTS,
                    "stop_points": MNQ_TRAILING_STOP_POINTS, "stop_after_t1": MNQ_FIRST_TRIM_STOP_POINTS,
                    "runner_auto_close": MNQ_RUNNER_AUTO_CLOSE_POINTS,
                },
                "auto_trim_enabled": AUTO_TRIM_ENABLED,
                "breakeven_runner": True,
            },
        })
    
    def _send_pnl_dashboard(self):
        """Serve trading dashboard with P&L stats and recent trades."""
        trades = []
        if os.path.exists(TRADE_LOG_PATH):
            try:
                with open(TRADE_LOG_PATH, "r") as f:
                    for line in f:
                        if line.strip():
                            trades.append(json.loads(line))
            except Exception as e:
                logger.error(f"Dashboard trade load error: {e}")
        
        # Filter to today only
        from datetime import date
        today = date.today().isoformat()
        today_trades = [t for t in trades if t["timestamp"].startswith(today)]
        
        # Filter out practice account from dashboard display
        today_trades = [t for t in today_trades if "PRAC" not in t["account_name"]]
        
        # Calculate totals
        total_pnl = sum(t["pnl"] for t in today_trades)
        wins = len([t for t in today_trades if t["pnl"] > 0])
        losses = len([t for t in today_trades if t["pnl"] <= 0])
        win_rate = round(wins / len(today_trades) * 100, 1) if today_trades else 0
        
        # Group by account
        by_account = {}
        for t in today_trades:
            acc = t["account_name"]
            if acc not in by_account:
                by_account[acc] = {"pnl": 0, "trades": 0}
            by_account[acc]["pnl"] += t["pnl"]
            by_account[acc]["trades"] += 1
        
        # Build HTML
        html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Richard's Trading Dashboard</title>
    <meta http-equiv="refresh" content="30">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', monospace; margin: 0; padding: 20px; background: #0a0a0a; color: #e0e0e0; }}
        h1 {{ margin: 0 0 20px 0; font-size: 28px; }}
        h2 {{ margin: 30px 0 15px 0; font-size: 18px; color: #888; }}
        .summary {{ display: flex; gap: 15px; margin-bottom: 30px; flex-wrap: wrap; }}
        .card {{ background: #1a1a1a; padding: 20px; border-radius: 8px; flex: 1; min-width: 200px; border: 1px solid #2a2a2a; }}
        .card h3 {{ margin: 0 0 10px 0; color: #666; font-size: 12px; text-transform: uppercase; font-weight: 500; }}
        .card .value {{ font-size: 36px; font-weight: 700; line-height: 1; }}
        .card .subtext {{ margin-top: 10px; font-size: 14px; color: #888; }}
        .positive {{ color: #10b981; }}
        .negative {{ color: #ef4444; }}
        .neutral {{ color: #6b7280; }}
        table {{ width: 100%; border-collapse: collapse; background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 8px; overflow: hidden; }}
        th, td {{ padding: 12px 16px; text-align: left; }}
        th {{ background: #111; color: #666; font-size: 11px; text-transform: uppercase; font-weight: 600; border-bottom: 1px solid #2a2a2a; }}
        td {{ border-bottom: 1px solid #1a1a1a; }}
        tr:last-child td {{ border-bottom: none; }}
        tr:hover {{ background: #141414; }}
        .time {{ color: #666; font-family: 'Monaco', monospace; font-size: 13px; }}
        .inst {{ font-weight: 600; }}
        .level {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }}
        .level-T1 {{ background: #1e3a8a; color: #93c5fd; }}
        .level-T2 {{ background: #7c2d12; color: #fed7aa; }}
        .level-RUNNER {{ background: #14532d; color: #86efac; }}
        .level-FULL_STOP {{ background: #7f1d1d; color: #fca5a5; }}
        .level-T2R {{ background: #78350f; color: #fcd34d; }}
    </style>
</head>
<body>
    <h1>Richard's Trading Dashboard</h1>
    <div class="summary">
        <div class="card">
            <h3>Today's P&L</h3>
            <div class="value {'positive' if total_pnl >= 0 else 'negative'}">${total_pnl:,.2f}</div>
        </div>
        <div class="card">
            <h3>Trades</h3>
            <div class="value {'neutral' if not today_trades else 'positive'}">{len(today_trades)}</div>
            <div class="subtext">{wins}W / {losses}L ({'positive' if win_rate >= 50 else 'negative'}>{win_rate}%)</div>
        </div>
        <div class="card">
            <h3>Active Accounts</h3>
            <div class="value neutral">{len(by_account)}</div>
        </div>
    </div>
    
    <h2>By Account</h2>
    <table>
        <tr>
            <th>Account</th>
            <th>Trades</th>
            <th style="text-align: right;">P&L</th>
        </tr>
"""
        for acc, data in sorted(by_account.items(), key=lambda x: x[1]["pnl"], reverse=True):
            pnl_class = "positive" if data["pnl"] >= 0 else "negative"
            html += f"""
        <tr>
            <td>{acc}</td>
            <td>{data["trades"]}</td>
            <td class="{pnl_class}" style="text-align: right; font-weight: 600;">${data["pnl"]:,.2f}</td>
        </tr>
"""
        
        html += """
    </table>
    
    <h2>Recent Trades</h2>
    <table>
        <tr>
            <th>Time</th>
            <th>Account</th>
            <th>Inst</th>
            <th>Level</th>
            <th style="text-align: right;">Contracts</th>
            <th style="text-align: right;">Entry</th>
            <th style="text-align: right;">Exit</th>
            <th style="text-align: right;">Points</th>
            <th style="text-align: right;">P&L</th>
        </tr>
"""
        for t in sorted(today_trades, key=lambda x: x["timestamp"], reverse=True)[:100]:
            time_str = t["timestamp"][11:19]
            pnl_class = "positive" if t["pnl"] >= 0 else "negative"
            level_safe = t["level"].replace("+", "")
            html += f"""
        <tr>
            <td class="time">{time_str}</td>
            <td>{t["account_name"]}</td>
            <td class="inst">{t["instrument"]}</td>
            <td><span class="level level-{level_safe}">{t["level"]}</span></td>
            <td style="text-align: right;">{t["contracts"]}</td>
            <td style="text-align: right;">{t["entry"]}</td>
            <td style="text-align: right;">{t["exit"]}</td>
            <td style="text-align: right;" class="{pnl_class}">{t["points"]:+.2f}</td>
            <td style="text-align: right; font-weight: 600;" class="{pnl_class}">${t["pnl"]:+,.2f}</td>
        </tr>
"""
        
        html += """
    </table>
    <p style="margin-top: 30px; color: #444; font-size: 12px;">Auto-refresh every 30 seconds</p>
</body>
</html>"""
        
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())
    
    def _send_positions(self):
        positions_list = []
        for position in active_positions.values():
            positions_list.append({
                "key": position["key"],
                "direction": position["direction"].upper(),
                "mes_entry": position.get("mes_entry_price"),
                "mnq_entry": position.get("mnq_entry_price"),
                "mes_total": position.get("mes_total", MES_CONTRACTS),
                "mnq_total": position.get("mnq_total", MNQ_CONTRACTS),
                "mes_remaining": position.get("mes_remaining", 0),
                "mnq_remaining": position.get("mnq_remaining", 0),
                "first_trim": position["first_trim"],
                "second_trim": position["second_trim"],
                "runner_active": position.get("runner_active", False),
                "fully_trimmed": position["fully_trimmed"],
                "mes_stop": position.get("mes_stop_price"),
                "mnq_stop": position.get("mnq_stop_price"),
                "age_seconds": int(time.time() - position["created_at"]),
                "mes_account_states": position.get("mes_account_states", {}),
                "mnq_account_states": position.get("mnq_account_states", {}),
                "stage": "Entry" if not position["first_trim"] else
                         "After 1st trim" if not position["second_trim"] else "Runner",
            })
        self._json_response(200, positions_list)
    
    def _send_broker_positions(self):
        """Live broker positions from ProjectX + Tradovate (MES + MNQ)"""
        result = {"projectx": {}, "tradovate": {}, "contracts": {"mes_id": _mes_contract_id, "mnq_id": _mnq_contract_id}}
        for acct_id in get_active_accounts():
            data = px_api("/api/Position/searchOpen", {"accountId": acct_id})
            if data:
                result["projectx"][str(acct_id)] = data.get("positions", [])
            else:
                result["projectx"][str(acct_id)] = "error"
        if TRADOVATE_ENABLED:
            result["tradovate"] = tv_get_positions()
        self._json_response(200, result)
    
    def _send_broker_orders(self):
        """Live open orders from ProjectX + Tradovate"""
        result = {"projectx": {}, "tradovate": {}}
        for acct_id in get_active_accounts():
            data = px_api("/api/Order/searchOpen", {"accountId": acct_id})
            if data:
                result["projectx"][str(acct_id)] = data.get("orders", [])
            else:
                result["projectx"][str(acct_id)] = "error"
        if TRADOVATE_ENABLED:
            result["tradovate"] = tv_get_open_orders()
        self._json_response(200, result)
    
    def log_message(self, format, *args):
        logger.info(f"HTTP {self.address_string()} - {format % args}")

# ================= MAIN =================
def main():
    print(f"\n{'='*80}")
    print("🎯 MES + MNQ TRADING BOT — PROJECTX + TRADOVATE")
    print(f"{'='*80}")
    print(f"📍 Port: {PORT}")
    if LADDER_111_ENABLED:
        print(f"🪜 MODE: 1-1-1 BROKER LADDER (legs independent, no MES catalyst)")
        print(f"📊 MES ladder: T1@+{MES_T1_POINTS} / T2@+{MES_T2_POINTS} / Runner@+{MES_RUNNER_AUTO_CLOSE_POINTS} (cap)")
        print(f"📊 MNQ ladder: T1@+{MNQ_T1_POINTS} / T2@+{MNQ_T2_POINTS} / Runner@+{MNQ_RUNNER_AUTO_CLOSE_POINTS} (cap)")
        print(f"🛑 MES Stop: {MES_TRAILING_STOP_POINTS}pt initial → {MES_FIRST_TRIM_STOP_POINTS}pt post-T1 → BE-{MES_RUNNER_BREAKEVEN_OFFSET} runner")
        print(f"🛑 MNQ Stop: {MNQ_TRAILING_STOP_POINTS}pt initial → {MNQ_FIRST_TRIM_STOP_POINTS}pt post-T1 → BE-{MNQ_RUNNER_BREAKEVEN_OFFSET} runner")
        print(f"🌆 EOD auto-flatten: {EOD_FLATTEN_HOUR:02d}:{EOD_FLATTEN_MINUTE:02d} ET")
    else:
        print(f"📊 MES: {MES_CONTRACTS} → -{MES_FIRST_TRIM_CONTRACTS} (T1) → -{MES_SECOND_TRIM_CONTRACTS} (T2) → {MES_RUNNER_CONTRACTS} runner (+{MES_RUNNER_AUTO_CLOSE_POINTS}pts)")
        print(f"📊 MNQ: {MNQ_CONTRACTS} → -{MNQ_FIRST_TRIM_CONTRACTS} (T1) → -{MNQ_SECOND_TRIM_CONTRACTS} (T2) → {MNQ_RUNNER_CONTRACTS} runner (+{MNQ_RUNNER_AUTO_CLOSE_POINTS}pts)")
        print(f"🛑 MES Stop: {MES_TRAILING_STOP_POINTS}pt initial, {MES_FIRST_TRIM_STOP_POINTS}pt after T1")
        print(f"🛑 MNQ Stop: {MNQ_TRAILING_STOP_POINTS}pt initial, {MNQ_FIRST_TRIM_STOP_POINTS}pt after T1")
    print(f"🏦 ProjectX Accounts: {len(get_active_accounts())}")
    rt_status = 'ENABLED' if RITHMIC_ENABLED else 'DISABLED'
    print(f"🏦 Rithmic/Apex: {rt_status} (env: {RITHMIC_ENV})")
    tv_status_parts = ['ENABLED' if TRADOVATE_ENABLED else 'DISABLED']
    if TRADOVATE_ENABLED and TRADOVATE_DRY_RUN:
        tv_status_parts.append('🧪 DRY RUN')
    print(f"🏦 Tradovate: {' '.join(tv_status_parts)} (Account: {TRADOVATE_ACCOUNT_SPEC}, Mode: {'DEMO' if TRADOVATE_DEMO else 'LIVE'}, Contracts: {TRADOVATE_MES_CONTRACT_NAME}+{TRADOVATE_MNQ_CONTRACT_NAME})")
    print(f"{'='*80}\n")
    
    # Step 1: Login to ProjectX
    print("🔐 Logging in to ProjectX...")
    if not px_login():
        print("❌ FATAL: Cannot login to ProjectX API")
        sys.exit(1)
    
    # Step 1b: Login to Tradovate
    if TRADOVATE_ENABLED:
        print("🔐 Logging in to Tradovate...")
        if not tv_login():
            print("⚠️ WARNING: Tradovate login failed — continuing without Tradovate")
            # Don't exit — ProjectX can still work
        else:
            # Find MES + MNQ contracts on Tradovate
            print(f"🔍 Tradovate: Looking up {TRADOVATE_MES_CONTRACT_NAME} + {TRADOVATE_MNQ_CONTRACT_NAME}...")
            if not tv_find_mes_contract():
                print(f"⚠️ WARNING: Tradovate MES contract {TRADOVATE_MES_CONTRACT_NAME} not found")
            if not tv_find_mnq_contract():
                print(f"⚠️ WARNING: Tradovate MNQ contract {TRADOVATE_MNQ_CONTRACT_NAME} not found")
            
            # Verify Tradovate account
            tv_accounts = tv_api_get("/account/list")
            if tv_accounts:
                for acct in tv_accounts:
                    active_marker = "✅" if acct["id"] == TRADOVATE_ACCOUNT_ID else "  "
                    print(f"  {active_marker} Tradovate: {acct['id']} — {acct.get('name', '?')} (active: {acct.get('active')})")
    

    # Step 1c: Connect to Rithmic/Apex
    if RITHMIC_ENABLED:
        if not RITHMIC_AVAILABLE:
            print("⚠️ WARNING: Rithmic enabled but rithmic_client.py not found — skipping")
        else:
            global _rithmic_bridge
            print(f"🔐 Connecting to Rithmic ({RITHMIC_ENV})...")
            try:
                config = rithmic_load_config(RITHMIC_ENV)
                _rithmic_bridge = RithmicBridge(config)
                # Register fill callback for Apex P&L tracking (2026-05-15)
                _rithmic_bridge.set_on_fill(on_rithmic_fill)
                if _rithmic_bridge.start():
                    print(f"✅ Rithmic: connected ({len(config['accounts'])} accounts)")
                    for acct in config['accounts']:
                        print(f"  ✅ 🟢 {acct}")
                else:
                    # 2026-05-10 persistent-bridge fix: do NOT null out the bridge.
                    # RithmicBridge.start() now always launches the background run loop,
                    # which auto-reconnects with exponential backoff. This recovers
                    # cleanly from startup-during-Rithmic-maintenance-window scenarios.
                    print("⚠️ WARNING: Rithmic initial connect failed — bridge will auto-reconnect in background")
                    print("    (background run loop active; bot will start using Rithmic as soon as it comes back online)")
            except Exception as e:
                print(f"⚠️ WARNING: Rithmic startup error: {e}")
                _rithmic_bridge = None

    # Step 2: Find MES and MNQ contracts on ProjectX
    print("🔍 Finding MES contract on ProjectX...")
    if not find_mes_contract():
        print("❌ FATAL: Cannot find MES contract")
        sys.exit(1)
    
    print("🔍 Finding MNQ contract on ProjectX...")
    if not find_mnq_contract():
        print("❌ FATAL: Cannot find MNQ contract")
        sys.exit(1)
    
    # Step 3: Verify ProjectX accounts
    print("🏦 Verifying ProjectX accounts...")
    data = px_api("/api/Account/search", {"onlyActiveAccounts": True})
    if data and data.get("accounts"):
        active_ids = set(get_active_accounts())
        for acct in data["accounts"]:
            marker = "✅" if acct["id"] in active_ids else "  "
            can_trade = "🟢" if acct.get("canTrade") else "🔴"
            print(f"  {marker} {can_trade} {acct['id']} — {acct.get('name', '?')}")
    
    # Step 4: Load saved state
    load_position_state()
    
    # Step 5: Start background position monitor
    def position_monitor():
        """Background thread: per-leg state machine.
        - LADDER_111_ENABLED mode: track each leg's broker position size; on size drops,
          recognise T1/T2/runner fills and adjust stops accordingly. Legs are independent
          (no MES catalyst rule).
        - Legacy/INDEPENDENT_TP modes: original MES-catalyst logic preserved below.
        Also enforces 3:55pm EOD auto-flatten before Topstep's 4:10pm cutoff."""
        last_eod_check_min = -1
        while True:
            try:
                time.sleep(1)  # was 5s — tightened to shrink stop-fill cleanup race
                position = get_active_position()

                # ---- EOD auto-flatten (runs even with no position to clear lingering orders) ----
                try:
                    from datetime import datetime as _dt
                    import zoneinfo
                    now_et = _dt.now(zoneinfo.ZoneInfo("America/New_York"))
                    if (now_et.hour == EOD_FLATTEN_HOUR and now_et.minute >= EOD_FLATTEN_MINUTE
                            and last_eod_check_min != now_et.minute):
                        last_eod_check_min = now_et.minute
                        if position and position.get("status") == "active":
                            logger.warning(f"🌆 EOD AUTO-FLATTEN — {now_et.strftime('%H:%M ET')} — flattening all positions")
                            if LADDER_111_ENABLED:
                                cancel_ladder(position)
                            cancel_runner_limits(position)
                            cancel_tp_limits(position)
                            cancel_stops_all_accounts(position)
                            execute_flatten_all_accounts()
                            if TRADOVATE_ENABLED:
                                tv_cancel_all_orders(); tv_flatten()
                                rt_flatten()  # Rithmic/Apex
                            close_position(position, "eod_flatten")
                            queue_notification("EOD_FLATTEN",
                                f"🌆 EOD AUTO-FLATTEN at {now_et.strftime('%H:%M ET')} — all positions closed before Topstep cutoff")
                except Exception as e:
                    logger.error(f"EOD check error: {e}")

                if not position or position.get("status") != "active":
                    continue
                
                # Don't check too soon after entry or trim — give broker time to settle
                if time.time() - position.get("last_updated", 0) < 3:  # was 10s
                    continue
                
                # Check leader account MES + MNQ broker position
                leader_id = get_active_accounts()[0]
                mes_match, mes_broker = verify_broker_position(leader_id, contract_id=_mes_contract_id)
                # CRITICAL: if API call failed (returns (None, None)), do NOT treat as flat.
                # Otherwise transient timeouts trigger phantom runner-done and strip protective stops.
                if mes_match is None:
                    logger.warning(f"⚠️ MES position query failed for acct={leader_id} — skipping ladder check this cycle")
                    continue
                mes_size = mes_broker.get("size", 0) if mes_broker else 0

                # ---- 1-1-1 LADDER STATE MACHINE ----
                if LADDER_111_ENABLED and position.get("ladder_state"):
                    if _mnq_contract_id:
                        mnq_match, mnq_broker_l = verify_broker_position(leader_id, contract_id=_mnq_contract_id)
                        if mnq_match is None:
                            logger.warning(f"⚠️ MNQ position query failed for acct={leader_id} — skipping ladder check this cycle")
                            continue
                    else:
                        mnq_broker_l = None
                    mnq_size_l = mnq_broker_l.get("size", 0) if mnq_broker_l else 0
                    state = position["ladder_state"]

                    # Fetch open orders once per cycle to determine TP-vs-stop fills cheaply.
                    # Track query success separately — a failed/empty response must NOT be
                    # interpreted as "all tracked orders filled" (premature-trim bug 2026-05-12).
                    open_oids = set()
                    open_oids_ok = False
                    try:
                        oo = px_api("/api/Order/searchOpen", {"accountId": leader_id})
                        if oo and oo.get("success"):
                            for o in oo.get("orders", []):
                                open_oids.add(o.get("id"))
                            open_oids_ok = True
                    except Exception as e:
                        logger.error(f"Ladder: openOrders query failed: {e}")
                    if not open_oids_ok:
                        logger.warning("⚠️ Ladder: openOrders snapshot UNRELIABLE this cycle — skipping TP-fill detection (would risk premature trim)")

                    def _check_leg(leg_name, current_size):
                        """Check ladder fills for one leg. Uses tracked TP limit IDs to
                        differentiate TP fills vs stop fills.

                        SAFETY GUARDS (added 2026-05-12 after premature-trim incident):
                        1. Require successful openOrders snapshot (open_oids_ok). A failed/empty
                           query would make every tracked order look "filled" — do not act.
                        2. Cross-check current_size: T1 fill requires size <= 2, T2 requires <= 1.
                           If size is still 3 after "T1 fill", it's a false positive (e.g. limit
                           was cancelled by an external action). Refuse to advance."""
                        leg_state = state.get(leg_name, {})
                        if leg_state.get("runner_done"):
                            return
                        ladder = position.get("ladder_orders", {}).get(str(leader_id), {}).get(leg_name, {})
                        t1_oid = ladder.get("t1")
                        t2_oid = ladder.get("t2")
                        run_oid = ladder.get("runner")
                        # "Filled" = had an oid, no longer in open orders book AND snapshot is trustworthy
                        t1_filled_now = open_oids_ok and bool(t1_oid) and t1_oid not in open_oids
                        t2_filled_now = open_oids_ok and bool(t2_oid) and t2_oid not in open_oids
                        run_filled_now = open_oids_ok and bool(run_oid) and run_oid not in open_oids

                        # Advance through phases based on actual TP fills (NOT just size drops)
                        # Cross-check with broker size to reject false positives from cancelled limits.
                        if t1_filled_now and not leg_state.get("t1_filled"):
                            if current_size is not None and current_size > 2:
                                logger.warning(f"⚠️ Ladder {leg_name.upper()}: T1 oid missing but broker size={current_size} (>2) — REFUSING T1 advance (likely cancel, not fill)")
                            else:
                                _ladder_handle_t1_fill(position, leg_name)
                        if t2_filled_now and not leg_state.get("t2_filled"):
                            if current_size is not None and current_size > 1:
                                logger.warning(f"⚠️ Ladder {leg_name.upper()}: T2 oid missing but broker size={current_size} (>1) — REFUSING T2 advance (likely cancel, not fill)")
                            else:
                                _ladder_handle_t2_fill(position, leg_name)
                        # Runner: leg fully flat at broker = runner done.
                        # CONFIRMATION GATE: require 2 consecutive size==0 reads before declaring done.
                        # Protects against transient API hiccups that briefly mis-report size.
                        # Also requires a non-empty open_oids snapshot OR a confirmed TP fill — otherwise
                        # an empty/failed orders query could mask the truth.
                        zero_count = leg_state.get("zero_size_count", 0)
                        if current_size == 0:
                            zero_count += 1
                        else:
                            zero_count = 0
                        leg_state["zero_size_count"] = zero_count

                        ZERO_CONFIRM_THRESHOLD = 2  # require 2 consecutive cycles
                        if (current_size == 0
                                and zero_count >= ZERO_CONFIRM_THRESHOLD
                                and not state.get(leg_name, {}).get("runner_done")):
                            # Only allow tp_filled classification if open_oids snapshot is trustworthy
                            # (i.e., we successfully fetched it AND the runner oid is genuinely missing)
                            if run_filled_now and open_oids:
                                exit_type = "tp_filled"
                            else:
                                exit_type = "stopped_or_other"
                            _ladder_handle_runner_done(position, leg_name, exit_type)
                        # Update last_seen_size
                        leg_state = state.get(leg_name, {})
                        leg_state["last_seen_size"] = current_size
                        leg_state["zero_size_count"] = zero_count
                        position["ladder_state"][leg_name] = leg_state

                    _check_leg("mes", mes_size)
                    _check_leg("mnq", mnq_size_l)

                    # FAST-DESYNC GUARD (2026-05-15): If MNQ went fully flat at broker
                    # but bot still expects MNQ contracts AND the ladder hasn't already
                    # marked the MNQ runner done, treat it as an external close (manual
                    # Topstep exit, network glitch, copier hiccup) and force-flatten
                    # Apex/Tradovate immediately instead of waiting for the 2-cycle
                    # zero_size_count threshold or the 5-min sync_check. Mirrors what
                    # the MES-catalyst block used to do, adapted for MNQ-only mode.
                    mnq_expected = position.get("mnq_remaining", 0)
                    mnq_state_done = state.get("mnq", {}).get("runner_done", False)
                    if (mnq_size_l == 0 and mnq_expected > 0 and not mnq_state_done
                            and open_oids_ok):
                        # open_oids_ok guard: we trust this cycle's open-orders snapshot.
                        # Without it, a transient API failure could trigger false flatten.
                        logger.warning(f"⚡ MNQ FLAT AT BROKER (expected {mnq_expected}) — "
                                       f"external close detected, force-flattening cross-broker")
                        try:
                            cancel_ladder(position)
                            cancel_tp_limits(position)
                            cancel_runner_limits(position)
                            cancel_stops_all_accounts(position)
                        except Exception as _e:
                            logger.error(f"❌ fast-desync cancel error: {_e}")
                        # Safety sweep on routing accounts (followers cascade via copier)
                        for acct_id in get_order_routing_accounts():
                            try:
                                px_cancel_all_stops(acct_id)
                                px_flatten_all_contracts(acct_id)
                            except Exception as _e:
                                logger.error(f"❌ fast-desync flatten acct={acct_id}: {_e}")
                        # Mark MNQ runner done so _check_leg won't re-fire
                        try:
                            mnq_state_obj = position["ladder_state"].get("mnq", {})
                            mnq_state_obj["runner_done"] = True
                            mnq_state_obj["last_seen_size"] = 0
                            position["ladder_state"]["mnq"] = mnq_state_obj
                        except Exception:
                            pass
                        position["mnq_remaining"] = 0
                        position["remaining_contracts"] = 0
                        position["fully_trimmed"] = True
                        position["status"] = "closed"
                        position["last_updated"] = time.time()
                        # close_position() now unconditionally calls rt_flatten + tv_flatten
                        close_position(position, "mnq_external_flat")
                        save_position_state()
                        queue_notification("MNQ_EXTERNAL_FLAT",
                            f"⚡ MNQ FLAT AT BROKER (expected {mnq_expected} contracts)\n"
                            f"External close detected (manual exit, TP fill, or stop hit).\n"
                            f"All cross-broker positions force-flattened. Bot state cleared.")
                        continue

                    # In ladder mode, MES catalyst rule is dropped — skip the rest of monitor logic
                    continue

                # Also check MNQ broker position for independent-TP fill detection
                _, mnq_broker = verify_broker_position(leader_id, contract_id=_mnq_contract_id) if _mnq_contract_id else (None, None)
                mnq_size_broker = mnq_broker.get("size", 0) if mnq_broker else 0
                expected_mnq = position.get("mnq_remaining", 0)

                # ---- INDEPENDENT MNQ TP DETECTION ----
                # If MNQ went flat at broker but bot still thinks it has contracts,
                # check if MNQ TP filled. If so, mark MNQ leg done and cancel its
                # lingering stop. MES keeps running independently.
                if (INDEPENDENT_TP_ENABLED and mnq_size_broker == 0 and expected_mnq > 0
                        and not position.get("runner_active", False)):
                    mnq_exit_type = _detect_leg_exit_type(position, "mnq")
                    if mnq_exit_type == "tp_filled":
                        mnq_tp_target = position.get("mnq_tp_target", "N/A")
                        logger.info(f"🎯 MNQ TP FILLED @ {mnq_tp_target} — MNQ leg done, MES continues independently")
                        position["mnq_remaining"] = 0
                        mnq_states = position.get("mnq_account_states", {})
                        for acct_id in get_order_routing_accounts():
                            mnq_state = mnq_states.get(str(acct_id), {})
                            if mnq_state.get("stop_order_id"):
                                px_cancel_order(acct_id, mnq_state["stop_order_id"])
                                mnq_state["stop_order_id"] = None
                        position["mnq_account_states"] = mnq_states
                        tp_limits = position.get("tp_limit_orders", {})
                        for acct_key in tp_limits:
                            tp_limits[acct_key]["mnq"] = None
                        position["tp_limit_orders"] = tp_limits
                        position["last_updated"] = time.time()
                        save_position_state()
                        queue_notification("MNQ_TP_FILLED",
                            f"🎯 MNQ TP FILLED @ {mnq_tp_target} (+{MNQ_TP_POINTS}pts)\n"
                            f"MES leg continues running (TP @ {position.get('mes_tp_target','N/A')}, "
                            f"stop @ {position.get('mes_stop_price','N/A')})")
                        # If MES is also already flat at this point, close fully
                        if mes_size == 0 and position.get("mes_remaining", 0) == 0:
                            logger.info("MES also flat — closing position")
                            cancel_tp_limits(position)
                            cancel_stops_all_accounts(position)
                            close_position(position, "both_legs_tp_filled")
                            continue

                # Compare expected vs actual MES size
                expected_mes = position.get("mes_remaining", 0)
                
                if mes_size == 0 and expected_mes > 0:
                    # MES is flat at broker but bot thinks we have contracts.
                    # With INDEPENDENT_TP_ENABLED, MES could be flat because its TP filled.
                    # If so, do NOT flatten MNQ — let MNQ keep running with its own TP+stop.
                    is_runner = position.get("runner_active", False)
                    phase = "RUNNER" if is_runner else "POSITION"
                    mes_target = position.get("mes_runner_target", "N/A")
                    mnq_target = position.get("mnq_runner_target", "N/A")
                    mnq_rem = position.get("mnq_remaining", 0)

                    # ---- INDEPENDENT TP DIFFERENTIATION ----
                    mes_exit_type = _detect_leg_exit_type(position, "mes")
                    if mes_exit_type == "tp_filled" and not is_runner:
                        # MES TP'd. Mark MES leg done, cancel its lingering stop, but
                        # leave MNQ alone — it has its own TP+stop at the broker.
                        mes_tp_target = position.get("mes_tp_target", "N/A")
                        logger.info(f"🎯 MES TP FILLED @ {mes_tp_target} — MES leg done, MNQ continues independently")
                        position["mes_remaining"] = 0
                        # Cancel any lingering MES stop on all accounts
                        mes_states = position.get("mes_account_states", {})
                        for acct_id in get_order_routing_accounts():
                            mes_state = mes_states.get(str(acct_id), {})
                            if mes_state.get("stop_order_id"):
                                px_cancel_order(acct_id, mes_state["stop_order_id"])
                                mes_state["stop_order_id"] = None
                        position["mes_account_states"] = mes_states
                        # Clear the MES TP tracking entry
                        tp_limits = position.get("tp_limit_orders", {})
                        for acct_key in tp_limits:
                            tp_limits[acct_key]["mes"] = None
                        position["tp_limit_orders"] = tp_limits
                        position["last_updated"] = time.time()
                        save_position_state()
                        queue_notification("MES_TP_FILLED",
                            f"🎯 MES TP FILLED @ {mes_tp_target} (+{MES_TP_POINTS}pts)\n"
                            f"MNQ leg continues running (TP @ {position.get('mnq_tp_target','N/A')}, "
                            f"stop @ {position.get('mnq_stop_price','N/A')})")
                        # If MNQ is also already flat, close the position
                        _, mnq_check = verify_broker_position(leader_id, contract_id=_mnq_contract_id)
                        if (mnq_check.get("size", 0) if mnq_check else 0) == 0:
                            logger.info("MNQ also flat — closing position")
                            cancel_tp_limits(position)
                            cancel_stops_all_accounts(position)
                            close_position(position, "both_legs_tp_filled")
                        continue  # Skip the catalyst-flatten path below

                    logger.info(f"⚡ MES {phase} FLAT AT BROKER (expected {expected_mes}) — FLATTEN EVERYTHING")

                    # Step 1: Cancel ALL runner limit orders + independent TP limits (MES + MNQ)
                    cancel_runner_limits(position)
                    cancel_tp_limits(position)
                    logger.info("🚫 All runner + TP limit orders cancelled")
                    
                    # Step 2: Cancel ALL stop orders (MES + MNQ) on all accounts
                    cancel_stops_all_accounts(position)
                    logger.info("🚫 All stop orders cancelled")
                    
                    # Step 3: Flatten MNQ on all accounts (MES is already flat)
                    if mnq_rem > 0:
                        for acct_id in get_order_routing_accounts():
                            px_flatten(acct_id, _mnq_contract_id, "MNQ")
                        logger.info(f"🧹 MNQ flattened on all accounts (was {mnq_rem} contracts)")
                    
                    # Step 4: Flatten Tradovate (cancel + flatten everything)
                    tv_cancel_all_orders()
                    tv_flatten()
                    rt_flatten()  # Rithmic/Apex
                    logger.info("🧹 Tradovate + Rithmic flattened")
                    
                    # Step 5: Safety — cancel ALL open orders on routing accounts to kill phantoms
                    # (followers cascade via copier when leader cancels)
                    for acct_id in get_order_routing_accounts():
                        px_cancel_all_stops(acct_id)
                    logger.info("🧹 Safety sweep: all open orders cancelled on routing accounts")
                    
                    # Step 6: Clean up bot state
                    exit_type = "mes_runner_resolved" if is_runner else "mes_stopped_flatten_all"
                    close_position(position, exit_type)
                    save_position_state()
                    
                    if is_runner:
                        queue_notification("RUNNER_CLOSED",
                            f"⚡ MES RUNNER RESOLVED — ALL POSITIONS CLOSED\n"
                            f"MES runner target was {mes_target}, MNQ target was {mnq_target}\n"
                            f"All orders cancelled, all positions flattened.\n"
                            f"No phantom orders remaining.")
                    else:
                        queue_notification("MES_STOPPED",
                            f"🛑 MES STOPPED OUT — FLATTENED EVERYTHING\n"
                            f"MES was flat at broker (expected {expected_mes}).\n"
                            f"MNQ {mnq_rem} contracts also flattened.\n"
                            f"MES is the catalyst — when MES dies, everything dies.")
                    
                    logger.info(f"✅ MES {phase} resolved — full cleanup complete")
                    
                    # Step 7: Verify broker is truly flat (paranoia check)
                    time.sleep(2)
                    _, mes_check = verify_broker_position(leader_id, contract_id=_mes_contract_id)
                    _, mnq_check = verify_broker_position(leader_id, contract_id=_mnq_contract_id)
                    mes_remaining = mes_check.get("size", 0) if mes_check else 0
                    mnq_remaining = mnq_check.get("size", 0) if mnq_check else 0
                    
                    if mes_remaining != 0 or mnq_remaining != 0:
                        logger.error(f"🚨 PHANTOM POSITION DETECTED: MES={mes_remaining}, MNQ={mnq_remaining} — force flattening!")
                        # Force-flatten routing accounts; followers cascade via Topstep copier.
                        for acct_id in get_order_routing_accounts():
                            px_cancel_all_stops(acct_id)
                            px_flatten_all_contracts(acct_id)
                        tv_cancel_all_orders()
                        tv_flatten()
                        rt_flatten()  # Rithmic/Apex
                        queue_notification("PHANTOM_DETECTED",
                            f"🚨 PHANTOM POSITION DETECTED after cleanup!\n"
                            f"MES={mes_remaining}, MNQ={mnq_remaining}\n"
                            f"Force flatten executed. CHECK ACCOUNTS MANUALLY.")
                    else:
                        logger.info("✅ Broker verified flat — no phantom positions")
                    
            except Exception as e:
                logger.error(f"Position monitor error: {e}")
    
    monitor_thread = threading.Thread(target=position_monitor, daemon=True)
    monitor_thread.start()
    if LADDER_111_ENABLED:
        print("🔄 Position monitor started (1-1-1 ladder state machine + EOD auto-flatten)")
    else:
        print("🔄 Position monitor started (MES catalyst — flatten all if MES flat)")
    
    # Tradovate token warmer
    if TRADOVATE_ENABLED:
        def tv_token_warmer():
            while True:
                time.sleep(1800)  # Every 30 min
                try:
                    token = tv_get_token()
                    if token:
                        logger.info("🔑 Tradovate token warmed")
                    else:
                        logger.warning("⚠️ Tradovate token warming failed")
                except Exception as e:
                    logger.error(f"Tradovate token warmer error: {e}")
        
        tv_warmer = threading.Thread(target=tv_token_warmer, daemon=True)
        tv_warmer.start()
        print("🔑 Tradovate token warmer started (refreshes every 30min)")
    
    # Step 5b: Start auto-trim failsafe monitor
    if AUTO_TRIM_ENABLED:
        start_auto_trim_monitor()
        print(f"🤖 Auto-trim failsafe enabled (T1: +{AUTO_TRIM_T1_POINTS}pts, T2: +{AUTO_TRIM_T2_POINTS}pts)")
    
    # Step 5c: Cloud gateway init (SaaS gating)
    _init_cloud()

    # Step 6: Start server
    print(f"\n🌐 Endpoints:")
    print(f"   http://localhost:{PORT}/          — Dashboard")
    print(f"   http://localhost:{PORT}/status     — Status JSON")
    print(f"   http://localhost:{PORT}/positions  — Positions JSON")
    print(f"   http://localhost:{PORT}/alert      — POST alerts")
    print(f"   http://localhost:{PORT}/broker_positions — Live broker positions")
    print(f"   http://localhost:{PORT}/broker_orders    — Live broker orders")
    print(f"   http://localhost:{PORT}/sync_check — Check/fix Topstep-Apex sync")
    print(f"   http://localhost:{PORT}/clear      — Clear all")
    print(f"   http://localhost:{PORT}/clear_alerts — Clear alerts only")
    print()
    
    try:
        import socket
        class ReusableHTTPServer(HTTPServer):
            allow_reuse_address = True
            def server_bind(self):
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                super().server_bind()
        
        server = ReusableHTTPServer(('localhost', PORT), TradingHandler)
        print(f"✅ Server started at http://localhost:{PORT}")
        print("📝 Waiting for Discord alerts...\n")
        server.serve_forever()
    except OSError as e:
        print(f"❌ Server error: {e}")
        if "Address already in use" in str(e):
            print(f"   Port {PORT} already in use.")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n🛑 Server stopped")
        save_position_state()
        sys.exit(0)

if __name__ == "__main__":
    main()
