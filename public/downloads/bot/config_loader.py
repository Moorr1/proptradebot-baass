"""
PropTradeBot Configuration Loader
=================================
Loads, validates, and exposes bot configuration from config.json.
Replaces hardcoded constants in server_projectx.py.

Usage:
    from config_loader import config
    port = config.server.port
    accounts = config.brokers.projectx.accounts
"""

import json
import os
from typing import Any, Dict, List, Optional

DEFAULT_CONFIG_PATH = os.path.expanduser("~/.config/proptradebot/config.json")

# =============================================================================
# DEFAULTS — used when config.json is missing or values are absent
# =============================================================================

DEFAULTS = {
    "server": {
        "port": 5555,
        "host": "localhost"
    },
    "brokers": {
        "projectx": {
            "enabled": True,
            "base_url": "https://api.topstepx.com",
            "credentials_file": "~/.config/projectx/credentials.json",
            "accounts": []
        },
        "tradovate": {
            "enabled": False,
            "demo": True,
            "dry_run": True,
            "credentials_file": "~/.config/tradovate/credentials.json",
            "account_id": None,
            "account_spec": None
        },
        "rithmic": {
            "enabled": False,
            "env": "live",
            "exchange": "CME",
            "ladder_enabled": True,
            "mirror_enabled": False
        }
    },
    "contracts": {
        "mes": {
            "search_text": "MES",
            "tradovate_name": "MESM6",
            "rithmic_name": "MESM6"
        },
        "mnq": {
            "search_text": "MNQ",
            "tradovate_name": "MNQM6",
            "rithmic_name": "MNQM6"
        }
    },
    "strategy": {
        "test_mode": False,
        "practice_account_id": None,
        "ladder_111_enabled": True,
        "independent_tp_enabled": False,
        "flatten_at_first_trim": False,
        "mes_catalyst_rule": False,
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
            "contracts": 5,
            "trailing_stop_points": 35,
            "first_trim_stop_points": 35,
            "first_trim_contracts": 3,
            "second_trim_contracts": 1,
            "runner_contracts": 1,
            "runner_auto_close_points": 60,
            "runner_breakeven_offset": 18,
            "tp_points": 33,
            "t1_points": 20,
            "t2_points": 40
        },
        "eod_flatten": {
            "enabled": True,
            "hour": 16,
            "minute": 10
        },
        "auto_trim": {
            "enabled": False,
            "t1_points": 7,
            "t2_points": 10,
            "check_interval": 2
        }
    },
    "alerts": {
        "max_age_seconds": 45,
        "rate_limit_seconds": 0.1
    },
    "notifications": {
        "telegram": {
            "enabled": False,
            "config_file": "~/.config/projectx/telegram.json"
        }
    },
    "logging": {
        "log_file": "~/trading_bot.log",
        "trade_log_file": "~/Desktop/discord-trading-alerts/trades.jsonl",
        "position_state_file": "~/position_state.json",
        "notification_file": "~/trade_notifications.jsonl"
    }
}


class ConfigNode:
    """Dot-accessible config node. Supports nested access and dict-like iteration."""

    def __init__(self, data: Dict[str, Any]):
        self._data = data
        for key, value in data.items():
            if isinstance(value, dict):
                setattr(self, key, ConfigNode(value))
            elif isinstance(value, list):
                setattr(self, key, [ConfigNode(item) if isinstance(item, dict) else item for item in value])
            else:
                setattr(self, key, value)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def to_dict(self) -> Dict[str, Any]:
        """Convert back to plain dict (for JSON serialization)."""
        result = {}
        for key, value in self._data.items():
            if isinstance(value, ConfigNode):
                result[key] = value.to_dict()
            elif isinstance(value, list):
                result[key] = [item.to_dict() if isinstance(item, ConfigNode) else item for item in value]
            else:
                result[key] = value
        return result

    def __repr__(self) -> str:
        return f"ConfigNode({self._data})"


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge override into base. Override wins."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _expand_paths(data: Dict[str, Any]) -> Dict[str, Any]:
    """Expand ~ to home directory in any string values ending with '_file' or 'path'."""
    result = {}
    for key, value in data.items():
        if isinstance(value, dict):
            result[key] = _expand_paths(value)
        elif isinstance(value, list):
            result[key] = [_expand_paths(item) if isinstance(item, dict) else item for item in value]
        elif isinstance(value, str) and (key.endswith("_file") or key.endswith("_path") or "file" in key.lower()):
            result[key] = os.path.expanduser(value)
        else:
            result[key] = value
    return result


def _validate_config(data: Dict[str, Any]) -> List[str]:
    """Validate config and return list of error messages. Empty = valid."""
    errors = []

    # Server
    server = data.get("server", {})
    port = server.get("port")
    if port is not None and not (1 <= port <= 65535):
        errors.append(f"server.port must be 1-65535, got {port}")

    # Brokers
    brokers = data.get("brokers", {})
    px = brokers.get("projectx", {})
    if px.get("enabled"):
        accounts = px.get("accounts", [])
        if not accounts:
            errors.append("brokers.projectx.accounts is empty but projectx is enabled")
        for i, acc in enumerate(accounts):
            if not acc.get("id"):
                errors.append(f"brokers.projectx.accounts[{i}] missing 'id'")
            if not acc.get("name"):
                errors.append(f"brokers.projectx.accounts[{i}] missing 'name'")

    tv = brokers.get("tradovate", {})
    if tv.get("enabled"):
        if not tv.get("account_id"):
            errors.append("brokers.tradovate.account_id required when enabled")
        if not tv.get("account_spec"):
            errors.append("brokers.tradovate.account_spec required when enabled")

    # Strategy
    strategy = data.get("strategy", {})
    mes = strategy.get("mes", {})
    mnq = strategy.get("mnq", {})

    total_mes = mes.get("first_trim_contracts", 0) + mes.get("second_trim_contracts", 0) + mes.get("runner_contracts", 0)
    if mes.get("contracts", 0) > 0 and total_mes > mes.get("contracts", 0):
        errors.append(f"MES trim contracts ({total_mes}) exceed total contracts ({mes.get('contracts')})")

    total_mnq = mnq.get("first_trim_contracts", 0) + mnq.get("second_trim_contracts", 0) + mnq.get("runner_contracts", 0)
    if mnq.get("contracts", 0) > 0 and total_mnq > mnq.get("contracts", 0):
        errors.append(f"MNQ trim contracts ({total_mnq}) exceed total contracts ({mnq.get('contracts')})")

    # EOD
    eod = strategy.get("eod_flatten", {})
    if eod.get("enabled"):
        hour = eod.get("hour", 0)
        minute = eod.get("minute", 0)
        if not (0 <= hour <= 23):
            errors.append(f"strategy.eod_flatten.hour must be 0-23, got {hour}")
        if not (0 <= minute <= 59):
            errors.append(f"strategy.eod_flatten.minute must be 0-59, got {minute}")

    return errors


def load_config(path: Optional[str] = None) -> ConfigNode:
    """
    Load configuration from JSON file.

    Args:
        path: Path to config.json. Defaults to ~/.config/proptradebot/config.json

    Returns:
        ConfigNode with dot-accessible configuration values.

    Raises:
        FileNotFoundError: If config file doesn't exist and no defaults can be used.
        ValueError: If config fails validation.
    """
    config_path = path or DEFAULT_CONFIG_PATH

    # Start with defaults
    merged = dict(DEFAULTS)

    # Load user config if exists
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            user_config = json.load(f)
        merged = _deep_merge(merged, user_config)
    else:
        # No config file — use defaults and create it
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w") as f:
            json.dump(merged, f, indent=2)

    # Expand ~ paths
    merged = _expand_paths(merged)

    # Validate
    errors = _validate_config(merged)
    if errors:
        raise ValueError("Config validation failed:\n  - " + "\n  - ".join(errors))

    return ConfigNode(merged)


def save_config(data: ConfigNode, path: Optional[str] = None) -> None:
    """Save configuration back to JSON file."""
    config_path = path or DEFAULT_CONFIG_PATH
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    with open(config_path, "w") as f:
        json.dump(data.to_dict(), f, indent=2)


# =============================================================================
# Module-level singleton — imported by server_projectx.py
# =============================================================================

config: ConfigNode = None  # type: ignore


def init_config(path: Optional[str] = None) -> ConfigNode:
    """Initialize the global config singleton. Call once at startup."""
    global config
    config = load_config(path)
    # Hot-patch module-level config so imported references stay valid
    import config_loader as _cl
    _cl.config = config
    return config


# Convenience accessors for common values (to reduce typing)
def get_accounts() -> List[Dict[str, Any]]:
    """Get enabled ProjectX accounts."""
    return [a for a in config.brokers.projectx.accounts if a.get("enabled", True)]


def get_leader_account() -> Optional[Dict[str, Any]]:
    """Get the leader account (first one marked leader, or first enabled)."""
    accounts = config.brokers.projectx.accounts
    for acc in accounts:
        if acc.get("leader") and acc.get("enabled", True):
            return acc
    # Fallback to first enabled
    enabled = get_accounts()
    return enabled[0] if enabled else None


def get_order_routing_accounts() -> List[Dict[str, Any]]:
    """Get accounts that can place orders (excludes followers)."""
    return [a for a in config.brokers.projectx.accounts
            if a.get("enabled", True) and not a.get("follower")]


def is_test_mode() -> bool:
    return config.strategy.test_mode


def get_test_account_id() -> Optional[int]:
    return config.strategy.practice_account_id
