"""
PropTradeBot Cloud Client
=========================
Connects the local bot to the BaaS backend.
- Validates API key on startup
- Fetches config from cloud (replaces local config.json)
- Reports trades to cloud dashboard
- Heartbeats every 5 minutes
- Gracefully stops if subscription lapses

Usage:
    from cloud_client import CloudClient
    client = CloudClient(api_key="ptb_...")
    if not client.authenticate():
        print("Subscription expired or invalid key")
        sys.exit(1)
    config = client.get_config()
    client.start_heartbeat()
"""

import json
import time
import threading
import requests
from datetime import datetime
from typing import Optional, Dict, Any

# BaaS base URL
CLOUD_BASE_URL = "https://proptradebot-baass.onrender.com"
HEARTBEAT_INTERVAL_SECONDS = 300  # 5 minutes
API_TIMEOUT = 15


class CloudClient:
    """Client for PropTradeBot BaaS API."""

    def __init__(self, api_key: str, base_url: str = None):
        self.api_key = api_key
        self.base_url = base_url or CLOUD_BASE_URL
        self.user = None
        self.config = None
        self.accounts = []
        self.subscription = None
        self._heartbeat_thread = None
        self._stop_heartbeat = threading.Event()
        self._alerts_processed = 0
        self._positions_active = 0
        self._start_time = time.time()

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-API-Key": self.api_key
        }

    def authenticate(self) -> bool:
        """
        Validate API key and fetch user config.
        Returns True if authenticated and subscription is active.
        """
        try:
            resp = requests.post(
                f"{self.base_url}/api/bot/auth",
                headers=self._headers(),
                timeout=API_TIMEOUT
            )
            data = resp.json()

            if not data.get("success"):
                error = data.get("error", "Unknown error")
                code = data.get("code", "UNKNOWN")
                print(f"❌ Cloud auth failed: {error} (code: {code})")
                return False

            self.user = data.get("user")
            self.config = data.get("config")
            self.accounts = data.get("accounts", [])
            self.subscription = data.get("subscription")

            print(f"✅ Cloud auth success: {self.user.get('email')} ({self.user.get('plan_tier')})")
            print(f"   Subscription: {self.user.get('subscription_status')}")
            print(f"   Accounts: {len(self.accounts)}")

            return True

        except requests.exceptions.ConnectionError:
            print(f"❌ Cannot connect to cloud: {self.base_url}")
            print("   Check internet connection or try again later.")
            return False
        except requests.exceptions.Timeout:
            print(f"❌ Cloud request timed out")
            return False
        except Exception as e:
            print(f"❌ Cloud auth error: {e}")
            return False

    def get_config(self) -> Optional[Dict[str, Any]]:
        """Fetch latest bot config from cloud."""
        try:
            resp = requests.get(
                f"{self.base_url}/api/bot/config",
                headers=self._headers(),
                timeout=API_TIMEOUT
            )
            data = resp.json()
            if data.get("success"):
                self.config = data.get("config")
                self.accounts = data.get("accounts", [])
                return self.config
        except Exception as e:
            print(f"⚠️ Config fetch failed: {e}")
        return self.config

    def report_trade(self, trade: Dict[str, Any]) -> bool:
        """Report a completed trade to the cloud dashboard."""
        try:
            resp = requests.post(
                f"{self.base_url}/api/bot/trades",
                headers=self._headers(),
                json=trade,
                timeout=API_TIMEOUT
            )
            data = resp.json()
            if data.get("success"):
                print(f"📊 Trade reported to cloud: {trade.get('symbol')} {trade.get('status')}")
                return True
            else:
                print(f"⚠️ Trade report failed: {data.get('error')}")
                return False
        except Exception as e:
            print(f"⚠️ Trade report error: {e}")
            return False

    def _heartbeat(self):
        """Send heartbeat to cloud."""
        try:
            payload = {
                "version": "4.0.0",
                "uptime_seconds": int(time.time() - self._start_time),
                "alerts_processed": self._alerts_processed,
                "positions_active": self._positions_active
            }
            resp = requests.post(
                f"{self.base_url}/api/bot/heartbeat",
                headers=self._headers(),
                json=payload,
                timeout=API_TIMEOUT
            )
            data = resp.json()

            if not data.get("success"):
                error = data.get("error", "")
                if "subscription" in error.lower() or data.get("subscription_status") not in ["active", "trialing"]:
                    print(f"🚨 SUBSCRIPTION LAPSED: {error}")
                    self._stop_heartbeat.set()
                    return False

            # Update config if cloud has newer version
            if data.get("config"):
                self.config = data["config"]

            return True

        except requests.exceptions.ConnectionError:
            print("⚠️ Heartbeat: no connection to cloud")
            return True  # Don't kill bot on temporary connection loss
        except Exception as e:
            print(f"⚠️ Heartbeat error: {e}")
            return True

    def _heartbeat_loop(self):
        """Background thread for periodic heartbeats."""
        while not self._stop_heartbeat.is_set():
            self._heartbeat()
            # Wait with interruptibility
            self._stop_heartbeat.wait(HEARTBEAT_INTERVAL_SECONDS)

    def start_heartbeat(self):
        """Start background heartbeat thread."""
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return
        self._stop_heartbeat.clear()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()
        print("💓 Heartbeat started (every 5 min)")

    def stop_heartbeat(self):
        """Stop heartbeat thread."""
        self._stop_heartbeat.set()
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=2)

    def is_subscription_active(self) -> bool:
        """Check if subscription is still active."""
        if not self.user:
            return False
        return self.user.get("subscription_status") in ["active", "trialing"]

    def increment_alerts(self, count: int = 1):
        """Increment alerts processed counter."""
        self._alerts_processed += count

    def update_positions(self, count: int):
        """Update active positions count."""
        self._positions_active = count

    def get_cloud_config_as_bot_config(self) -> Dict[str, Any]:
        """
        Convert cloud bot_config to local config.json format.
        This bridges the cloud schema to the local bot's expected format.
        """
        if not self.config:
            return {}

        # Map cloud config fields to local config format
        # This is a simplified mapping — expand as needed
        cloud_cfg = self.config

        return {
            "server": {"port": 5555, "host": "localhost"},
            "brokers": {
                "projectx": {
                    "enabled": True,
                    "base_url": "https://api.topstepx.com",
                    "credentials_file": "~/.config/projectx/credentials.json",
                    "accounts": [
                        {
                            "id": acc.get("account_number"),
                            "name": acc.get("prop_firm", "Account"),
                            "enabled": acc.get("status") == "active",
                            "leader": i == 0
                        }
                        for i, acc in enumerate(self.accounts)
                    ]
                },
                "tradovate": {"enabled": False, "demo": True, "dry_run": True},
                "rithmic": {"enabled": False, "env": "live", "ladder_enabled": True}
            },
            "contracts": {
                "mes": {"search_text": "MES", "tradovate_name": "MESM6", "rithmic_name": "MESM6"},
                "mnq": {"search_text": "MNQ", "tradovate_name": "MNQM6", "rithmic_name": "MNQM6"}
            },
            "strategy": {
                "test_mode": False,
                "ladder_111_enabled": True,
                "mes": {
                    "contracts": 0,
                    "trailing_stop_points": 9,
                    "first_trim_stop_points": 7,
                    "first_trim_contracts": 1,
                    "second_trim_contracts": 1,
                    "runner_contracts": 1,
                    "runner_auto_close_points": 20,
                    "runner_breakeven_offset": 3,
                    "tp_points": 7,
                    "t1_points": 7,
                    "t2_points": 12
                },
                "mnq": {
                    "contracts": cloud_cfg.get("contract_count", 5),
                    "trailing_stop_points": cloud_cfg.get("stop_loss_ticks", 35) or 35,
                    "first_trim_stop_points": cloud_cfg.get("stop_loss_ticks", 35) or 35,
                    "first_trim_contracts": max(1, (cloud_cfg.get("contract_count", 5) - 1) // 2),
                    "second_trim_contracts": 1,
                    "runner_contracts": 1,
                    "runner_auto_close_points": 60,
                    "runner_breakeven_offset": 18,
                    "tp_points": 33,
                    "t1_points": 20,
                    "t2_points": 40
                },
                "eod_flatten": {"enabled": True, "hour": 16, "minute": 10},
                "auto_trim": {"enabled": False, "t1_points": 7, "t2_points": 10, "check_interval": 2}
            },
            "alerts": {"max_age_seconds": 45, "rate_limit_seconds": 0.1},
            "notifications": {"telegram": {"enabled": False}},
            "logging": {
                "log_file": "~/trading_bot.log",
                "trade_log_file": "~/Desktop/discord-trading-alerts/trades.jsonl",
                "position_state_file": "~/position_state.json",
                "notification_file": "~/trade_notifications.jsonl"
            }
        }


# =============================================================================
# STANDALONE TEST
# =============================================================================
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python cloud_client.py <api_key>")
        sys.exit(1)

    client = CloudClient(api_key=sys.argv[1])
    if client.authenticate():
        print("\nConfig:", json.dumps(client.config, indent=2, default=str))
        print("\nAccounts:", json.dumps(client.accounts, indent=2, default=str))
        client.start_heartbeat()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            client.stop_heartbeat()
            print("\nStopped")
