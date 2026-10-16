"""
Flash Loan Liquidator (Module C) - Telegram Real-Time Alert Dispatcher.

Sends instant notifications to @Luis_sentinel_alert_bot when any of the 5 bots
detects or executes a profitable commission opportunity.
Enforces anti-spam cooldowns, daily quota caps (max 20/day), and zero hardcoded secrets.
"""

import os
import time
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional
import requests

logger = logging.getLogger("TelegramNotifier")


class TelegramCommissionNotifier:
    """Dispatches formatted commission alerts to Telegram with anti-spam rate limiting."""

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        cooldown_seconds: int = 300,
        max_daily_alerts: int = 20
    ):
        self.bot_token = (bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
        self.chat_id = (chat_id or os.getenv("TELEGRAM_CHAT_ID", "")).strip()
        self.cooldown_seconds = cooldown_seconds
        self.max_daily_alerts = max_daily_alerts
        self.last_sent_by_strategy: Dict[str, float] = {}
        self.daily_counts: Dict[str, int] = {}

    @property
    def is_configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def format_opportunity_message(self, opp: Dict[str, Any], is_live_execution: bool = False) -> str:
        """Formats a high-signal Telegram alert message for any of the 5 commission bots."""
        mode_banner = "💰 [COMISIÓN EJECUTADA ON-CHAIN]" if is_live_execution else "🛰️ [OPORTUNIDAD DETECTADA - MODO SOMBRA]"
        strategy_id = opp.get("strategy_id", opp.get("strategy_type", "UNKNOWN_BOT"))
        chain = opp.get("chain", "BNB")
        block_num = opp.get("block_number", "latest")
        net_profit = opp.get("net_profit_usd", 0.0)
        gas_cost = opp.get("gas_cost_usd", 0.0)

        lines = [
            f"{mode_banner}",
            f"• Motor: Sentinel 5-en-1 ({strategy_id})",
            f"• Red: {chain} (Bloque #{block_num})",
            f"• Ganancia Neta: +${net_profit:.4f} USD",
            f"• Gas Estimado: ${gas_cost:.4f} USD",
        ]
        if opp.get("tx_hash"):
            lines.append(f"• Tx Hash: {opp['tx_hash']}")
        return "\n".join(lines)

    def notify_opportunity(
        self,
        opp: Dict[str, Any],
        is_live_execution: bool = False,
        now_ts: Optional[float] = None
    ) -> Dict[str, Any]:
        """Checks rate limits and dispatches alert to Telegram if configured."""
        if now_ts is None:
            now_ts = time.time()

        if not opp.get("would_execute_atomic", False) or float(opp.get("net_profit_usd", 0.0)) <= 0.0:
            return {"sent": False, "reason": "NOT_PROFITABLE"}

        strategy_key = str(opp.get("strategy_id", "DEFAULT"))
        today_str = datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        sent_today = self.daily_counts.get(today_str, 0)

        if sent_today >= self.max_daily_alerts:
            return {"sent": False, "reason": "DAILY_QUOTA_REACHED"}

        last_ts = self.last_sent_by_strategy.get(strategy_key, 0.0)
        if (now_ts - last_ts) < self.cooldown_seconds:
            return {"sent": False, "reason": "COOLDOWN_ACTIVE"}

        text = self.format_opportunity_message(opp, is_live_execution=is_live_execution)

        if not self.is_configured:
            return {
                "sent": False,
                "reason": "TELEGRAM_CREDENTIALS_NOT_SET_IN_ENV",
                "preview_message": text
            }

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            resp = requests.post(
                url,
                json={"chat_id": self.chat_id, "text": text},
                timeout=5
            )
            if resp.status_code == 200:
                self.last_sent_by_strategy[strategy_key] = now_ts
                self.daily_counts[today_str] = sent_today + 1
                return {"sent": True, "reason": "OK", "message": text}
            return {"sent": False, "reason": f"HTTP_{resp.status_code}"}
        except Exception as exc:
            logger.warning(f"Telegram alert dispatch error: {exc}")
            return {"sent": False, "reason": "NETWORK_ERROR"}
