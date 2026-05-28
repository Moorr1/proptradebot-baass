"""
Alert Normalizer — converts various signal formats to internal alert format.
============================================================================
Supported sources:
  - Discord (Chrome extension): {text, timestamp, source}
  - TradingView webhooks: {ticker, price, alert_name, time, ...}
  - Generic: any JSON with text/ticker/alert_name

Output format (internal):
  {text, timestamp, source, metadata}
"""

import json
import re
from datetime import datetime
from typing import Any, Dict, Optional


class AlertNormalizer:
    """Normalizes incoming alerts from various sources to a standard format."""

    # TradingView alert name patterns that indicate buy/sell
    BUY_PATTERNS = [
        r'\bbuy\b', r'\blong\b', r'\bbullish\b', r'\bentry\s+long\b',
        r'\bgo\s+long\b', r'\blong\s+entry\b', r'\bbuy\s+signal\b'
    ]
    SELL_PATTERNS = [
        r'\bsell\b', r'\bshort\b', r'\bbearish\b', r'\bentry\s+short\b',
        r'\bgo\s+short\b', r'\bshort\s+entry\b', r'\bsell\s+signal\b'
    ]
    FLAT_PATTERNS = [
        r'\bflatten\b', r'\bclose\b', r'\bexit\b', r'\bflat\b',
        r'\bclose\s+all\b', r'\bexit\s+all\b'
    ]

    def __init__(self):
        self.buy_regex = re.compile('|'.join(self.BUY_PATTERNS), re.IGNORECASE)
        self.sell_regex = re.compile('|'.join(self.SELL_PATTERNS), re.IGNORECASE)
        self.flat_regex = re.compile('|'.join(self.FLAT_PATTERNS), re.IGNORECASE)

    def normalize(self, data: Dict[str, Any], source_hint: str = "unknown") -> Dict[str, Any]:
        """
        Normalize any incoming alert to internal format.

        Args:
            data: Raw alert payload (parsed JSON)
            source_hint: Where this came from (discord, tradingview, webhook, etc.)

        Returns:
            {text, timestamp, source, metadata}
        """
        # Detect source type
        source = self._detect_source(data, source_hint)

        if source == "tradingview":
            return self._normalize_tradingview(data)
        elif source == "discord":
            return self._normalize_discord(data)
        else:
            return self._normalize_generic(data, source)

    def _detect_source(self, data: Dict[str, Any], hint: str) -> str:
        """Detect the source type from payload structure."""
        # TradingView webhooks typically have: ticker, price, alert_name, time
        if any(k in data for k in ["ticker", "alert_name", "exchange"]):
            return "tradingview"
        # Discord extension sends: text, timestamp, source
        if "text" in data and "source" in data:
            return "discord"
        # Fallback to hint
        return hint.lower() if hint else "unknown"

    def _normalize_tradingview(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Convert TradingView webhook to internal format."""
        ticker = data.get("ticker", "")
        price = data.get("price", data.get("close", 0))
        alert_name = data.get("alert_name", data.get("alert", ""))
        time_str = data.get("time", data.get("timestamp", ""))

        # Determine direction from alert name
        direction = self._detect_direction(alert_name)

        # Build text in Discord-like format for parser compatibility
        text = self._build_alert_text(ticker, direction, price, alert_name)

        # Parse timestamp
        timestamp = self._parse_timestamp(time_str)

        return {
            "text": text,
            "timestamp": timestamp,
            "source": "tradingview",
            "metadata": {
                "original": data,
                "ticker": ticker,
                "price": price,
                "direction": direction,
                "alert_name": alert_name
            }
        }

    def _normalize_discord(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Discord alerts are already in internal format — just pass through."""
        return {
            "text": data.get("text", ""),
            "timestamp": data.get("timestamp"),
            "source": data.get("source", "discord"),
            "metadata": {
                "original": data
            }
        }

    def _normalize_generic(self, data: Dict[str, Any], source: str) -> Dict[str, Any]:
        """Handle unknown/generic formats."""
        # Try to extract text from common fields
        text = data.get("text", data.get("message", data.get("alert", "")))

        # If no text but has ticker + direction fields, build text
        if not text and "ticker" in data:
            text = self._build_alert_text(
                data.get("ticker", ""),
                data.get("direction", ""),
                data.get("price", 0),
                data.get("alert_name", "")
            )

        return {
            "text": text,
            "timestamp": data.get("timestamp", data.get("time")),
            "source": source,
            "metadata": {
                "original": data
            }
        }

    def _detect_direction(self, text: str) -> str:
        """Detect buy/sell/flat direction from alert text."""
        if not text:
            return "unknown"
        text_lower = str(text).lower()
        if self.buy_regex.search(text_lower):
            return "buy"
        if self.sell_regex.search(text_lower):
            return "sell"
        if self.flat_regex.search(text_lower):
            return "flat"
        return "unknown"

    def _build_alert_text(self, ticker: str, direction: str, price: float, alert_name: str) -> str:
        """Build Discord-like alert text from TradingView fields."""
        # Format: "MES Long @ 5234.50 — TradingView Alert"
        direction_upper = direction.upper() if direction != "unknown" else ""
        price_str = f"@ {price}" if price else ""
        parts = [p for p in [ticker, direction_upper, price_str] if p]
        text = " ".join(parts)
        if alert_name:
            text += f" — {alert_name}"
        return text

    def _parse_timestamp(self, time_str: str) -> Optional[str]:
        """Parse various timestamp formats to ISO string."""
        if not time_str:
            return None
        # Already ISO format
        if 'T' in str(time_str) or 'Z' in str(time_str):
            return str(time_str)
        # Unix timestamp (seconds or milliseconds)
        try:
            ts = float(time_str)
            if ts > 1e12:  # milliseconds
                ts = ts / 1000
            return datetime.utcfromtimestamp(ts).isoformat() + "Z"
        except (ValueError, TypeError):
            pass
        # TradingView format: "2026-05-27T14:30:00Z" or "2026-05-27 14:30:00"
        for fmt in ["%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"]:
            try:
                dt = datetime.strptime(str(time_str), fmt)
                return dt.isoformat() + "Z"
            except ValueError:
                continue
        return str(time_str)


# Singleton instance
_normalizer = None

def get_normalizer() -> AlertNormalizer:
    """Get or create the singleton normalizer."""
    global _normalizer
    if _normalizer is None:
        _normalizer = AlertNormalizer()
    return _normalizer


def normalize_alert(data: Dict[str, Any], source_hint: str = "unknown") -> Dict[str, Any]:
    """Convenience function — normalize an alert in one call."""
    return get_normalizer().normalize(data, source_hint)


# =============================================================================
# TEST / DEMO
# =============================================================================
if __name__ == "__main__":
    # Test TradingView format
    tv_alert = {
        "ticker": "MES1!",
        "price": 5234.50,
        "alert_name": "MES Long Entry",
        "time": "2026-05-27T14:30:00Z",
        "exchange": "CME"
    }
    print("TradingView input:")
    print(json.dumps(tv_alert, indent=2))
    print("\nNormalized:")
    print(json.dumps(normalize_alert(tv_alert), indent=2))

    print("\n" + "="*60 + "\n")

    # Test Discord format
    discord_alert = {
        "text": "MES Long @ 5234.50 — JMoney",
        "timestamp": "2026-05-27T14:30:00Z",
        "source": "discord"
    }
    print("Discord input:")
    print(json.dumps(discord_alert, indent=2))
    print("\nNormalized:")
    print(json.dumps(normalize_alert(discord_alert), indent=2))
