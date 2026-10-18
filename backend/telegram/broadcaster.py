"""
RAMYA GLOBAL FINTECH - Telegram Live Broadcast Engine
Sends rich institutional cards for every trade execution, profit lock, and daily verified performance.
"""

import time
import logging
from typing import Dict, Any

logger = logging.getLogger("TELEGRAM-BROADCASTER")

class TelegramBroadcaster:
    """Formats and dispatches automated marketing and transparent P&L cards to Telegram."""

    @staticmethod
    def format_trade_entry_card(robot_name: str, symbol: str, side: str, entry: float, tp: float, sl: float, market: str) -> str:
        arrow = "🟢 LONG / BUY" if side.upper() == "BUY" else "🔴 SHORT / SELL"
        curr = "₹" if "INDIAN" in market or "MCX" in market else "$"
        
        return (
            f"⚡ <b>RAMYA QUANT GLOBAL — LIVE TRADE ALERT</b> ⚡\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🤖 <b>Robot:</b> {robot_name}\n"
            f"🎯 <b>Asset:</b> #{symbol} ({market})\n"
            f"📈 <b>Action:</b> {arrow}\n"
            f"💵 <b>Entry Price:</b> {curr}{entry:,.2f}\n"
            f"🎯 <b>Target (TP):</b> {curr}{tp:,.2f}\n"
            f"🛡️ <b>Noise-Proof SL:</b> {curr}{sl:,.2f}\n"
            f"⏱️ <b>Trigger:</b> 5-Second Micro-Velocity Orderbook Strike\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>Powered by RQG Institutional High-Frequency Engine</i>"
        )

    @staticmethod
    def format_profit_booked_card(robot_name: str, symbol: str, net_pnl: float, commission: float, is_inr: bool = True) -> str:
        curr = "₹" if is_inr else "$"
        return (
            f"🎉 <b>PROFIT BOOKED & SETTLED!</b> 🎉\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🤖 <b>Robot:</b> {robot_name}\n"
            f"💎 <b>Instrument:</b> #{symbol}\n"
            f"💰 <b>Client Net Profit:</b> +{curr}{net_pnl:,.2f} ✅\n"
            f"🏛️ <b>10% Master Fee Auto-Deducted:</b> {curr}{commission:,.2f}\n"
            f"🔒 <b>Zero-Risk Breakeven Engine:</b> Verified\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>Join the VIP Bot: 100% Automated Trading Across India, Forex & Crypto</i>"
        )
