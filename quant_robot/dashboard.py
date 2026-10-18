"""
RAMYA QUANT ROBOT -- Unified Live Institutional Trading Dashboard
===================================================================
Provides real-time terminal and programmatic dashboard displaying:
1. Actual Exchange Cash Balance & Available Balance (from Delta Exchange API v2)
2. Verified Account Equity
3. Cumulative Realized Gross P&L
4. Total Actual Fees & GST Paid
5. Cumulative Realized Net P&L
6. Current Session Drawdown (%)
7. Open Positions Telemetry (Product, Size, Entry Price, Mark Price, Unrealized PnL)
8. Risk Budget Allocation & Margin Utilization
9. Recent Trade Logs & Execution Reasons
"""

import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("quant.dashboard")


class UnifiedTradingDashboard:
    def __init__(
        self,
        symbol: str = "XAUTUSD",
        initial_equity: float = 19.60,
        max_daily_loss_pct: float = 0.02,  # 2% daily loss limit ($0.392 on $19.60)
        max_risk_pct: float = 0.0025,
        max_leverage: int = 25,
    ):
        self.symbol = symbol
        self.initial_equity = initial_equity
        self.current_equity = initial_equity
        self.available_balance = initial_equity
        self.wallet_balance = initial_equity
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_risk_pct = max_risk_pct
        self.max_leverage = max_leverage

        # Telemetry metrics
        self.gross_pnl_usd = 0.0
        self.fees_paid_usd = 0.0
        self.net_pnl_usd = 0.0
        self.peak_equity = initial_equity
        self.max_drawdown_pct = 0.0
        self.daily_drawdown_pct = 0.0

        # Positions and Logs
        self.open_position: Optional[Dict[str, Any]] = None
        self.open_positions: Dict[str, Dict[str, Any]] = {}
        self.trade_logs: List[Dict[str, Any]] = []
        self.circuit_breaker_active = False
        self.circuit_breaker_reason = "NORMAL"
        self.market_regime = "INITIALIZING"
        self.scan_reason = "Scanning multi-timeframe structure..."
        self.scan_indicators: Dict[str, Any] = {}

    def update_market_scan(self, regime: str, reason: str, indicators: Optional[Dict[str, Any]] = None):
        """Updates live strategy scan reason and market regime."""
        self.market_regime = regime
        self.scan_reason = reason
        self.scan_indicators = indicators or {}

    def update_wallet(self, wallet_balance: float, available_balance: float):
        """Updates live wallet balance from exchange query."""
        self.wallet_balance = round(wallet_balance, 4)
        self.available_balance = round(available_balance, 4)
        self.current_equity = round(wallet_balance, 4)
        if self.current_equity > self.peak_equity:
            self.peak_equity = self.current_equity

        # Update drawdown
        if self.initial_equity > 0:
            dd = ((self.initial_equity - self.current_equity) / self.initial_equity) * 100.0
            self.daily_drawdown_pct = max(0.0, round(dd, 2))
        if self.peak_equity > 0:
            peak_dd = ((self.peak_equity - self.current_equity) / self.peak_equity) * 100.0
            self.max_drawdown_pct = max(0.0, round(peak_dd, 2))

        # Check circuit breaker
        if (self.daily_drawdown_pct / 100.0) >= self.max_daily_loss_pct:
            self.circuit_breaker_active = True
            self.circuit_breaker_reason = f"HALTED: Daily Drawdown ({self.daily_drawdown_pct:.2f}%) >= {self.max_daily_loss_pct*100:.1f}% Limit"

    def update_position(self, position: Optional[Dict[str, Any]]):
        """Updates currently open position details (single asset fallback)."""
        self.open_position = position
        if position:
            sym = position.get("symbol", "DEFAULT")
            self.open_positions[sym] = position
        else:
            self.open_positions.clear()

    def update_positions(self, positions: Dict[str, Dict[str, Any]]):
        """Updates currently open positions for concurrent multi-asset execution."""
        self.open_positions = dict(positions)
        self.open_position = next(iter(positions.values())) if positions else None

    def record_trade(
        self,
        symbol: str,
        side: str,
        size: int,
        entry_price: float,
        exit_price: float,
        gross_pnl: float,
        fees: float,
        net_pnl: float,
        reason: str,
    ):
        """Records completed trade telemetry."""
        self.gross_pnl_usd = round(self.gross_pnl_usd + gross_pnl, 4)
        self.fees_paid_usd = round(self.fees_paid_usd + fees, 4)
        self.net_pnl_usd = round(self.net_pnl_usd + net_pnl, 4)

        trade_entry = {
            "timestamp": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
            "symbol": symbol,
            "side": side.upper(),
            "size": size,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "gross_pnl": gross_pnl,
            "fees": fees,
            "net_pnl": net_pnl,
            "reason": reason,
        }
        self.trade_logs.append(trade_entry)
        if len(self.trade_logs) > 20:
            self.trade_logs.pop(0)

    def render_console(self, mode: str = "PAPER") -> str:
        """Renders institutional ANSI terminal dashboard."""
        border = "=" * 92
        sub_border = "-" * 92

        # Color codes
        GREEN = "\033[92m"
        RED = "\033[91m"
        CYAN = "\033[96m"
        YELLOW = "\033[93m"
        BOLD = "\033[1m"
        RESET = "\033[0m"

        mode_badge = f"{GREEN}[LIVE EXECUTION]{RESET}" if mode.upper() == "LIVE" else f"{YELLOW}[PAPER SIMULATION]{RESET}"
        net_color = GREEN if self.net_pnl_usd >= 0 else RED
        dd_color = RED if self.daily_drawdown_pct >= (self.max_daily_loss_pct * 100 * 0.8) else RESET

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        # Position Lines (Supports concurrent multi-asset holdings)
        if self.open_positions:
            pos_lines = []
            total_upnl = 0.0
            for sym, pos in self.open_positions.items():
                p_side = pos.get("side", "").upper()
                p_size = pos.get("size", 0)
                p_entry = pos.get("entry_price", 0.0)
                p_mark = pos.get("mark_price", p_entry)
                p_upnl = pos.get("unrealized_pnl", 0.0)
                total_upnl += p_upnl
                p_color = GREEN if p_upnl >= 0 else RED
                pos_lines.append(
                    f"  - {BOLD}{sym}{RESET}: {p_side} {p_size} contracts @ ${p_entry:,.2f} | "
                    f"Mark: ${p_mark:,.2f} | Unrealized: {p_color}${p_upnl:+,.4f} USD{RESET}"
                )
            tot_color = GREEN if total_upnl >= 0 else RED
            pos_lines.append(f"  - {BOLD}COMBINED PORTFOLIO UNREALIZED P&L:{RESET} {tot_color}${total_upnl:+,.4f} USD{RESET}")
            pos_line = "\n".join(pos_lines)
        elif self.open_position:
            pos = self.open_position
            p_side = pos.get("side", "").upper()
            p_size = pos.get("size", 0)
            p_entry = pos.get("entry_price", 0.0)
            p_mark = pos.get("mark_price", p_entry)
            p_upnl = pos.get("unrealized_pnl", 0.0)
            p_color = GREEN if p_upnl >= 0 else RED
            pos_line = (
                f"{BOLD}{p_side} {p_size} contracts{RESET} @ ${p_entry:,.2f} | Mark: ${p_mark:,.2f} | "
                f"Unrealized P&L: {p_color}${p_upnl:+,.4f} USD{RESET}"
            )
        else:
            pos_line = f"{CYAN}FLAT (0 Open Positions | Capital 100% Protected){RESET}"

        # Circuit Breaker Line
        if self.circuit_breaker_active:
            cb_line = f"{RED}{BOLD}[CIRCUIT BREAKER ENGAGED] {self.circuit_breaker_reason}{RESET}"
        else:
            cb_line = f"{GREEN}[RISK SHIELD ACTIVE] Loss Limits & Drawdown Guards Normal{RESET}"

        # Trade Logs summary
        recent_trades_lines = []
        if self.trade_logs:
            for t in self.trade_logs[-5:]:
                t_color = GREEN if t["net_pnl"] >= 0 else RED
                recent_trades_lines.append(
                    f"  [{t['timestamp']}] {t['side']} {t['size']}x {t['symbol']} | "
                    f"Entry ${t['entry_price']:,.2f} -> Exit ${t['exit_price']:,.2f} | "
                    f"Net: {t_color}${t['net_pnl']:+,.4f}{RESET} | Reason: {t['reason']}"
                )
        else:
            recent_trades_lines.append("  (No trades executed in current session)")

        logs_block = "\n".join(recent_trades_lines)

        dashboard_str = (
            f"\n{border}\n"
            f"  {BOLD}RAMYA QUANT ROBOT -- INSTITUTIONAL LIVE RISK & PERFORMANCE DASHBOARD{RESET}\n"
            f"  Mode: {mode_badge} | Target: {BOLD}{self.symbol}{RESET} | Max Lev: {self.max_leverage}x | Time: {now_str}\n"
            f"{sub_border}\n"
            f"  {BOLD}CAPITAL & MARGIN TELEMETRY:{RESET}\n"
            f"  - Verified Exchange Balance:  ${self.wallet_balance:,.2f} USD (${self.wallet_balance * 85.0:,.1f} INR)\n"
            f"  - Available Free Margin:      ${self.available_balance:,.2f} USD\n"
            f"  - Verified Current Equity:    ${self.current_equity:,.2f} USD\n"
            f"  - Remaining {self.max_risk_pct*100:.2f}% Risk Budget: ${max(0.0, self.current_equity * self.max_risk_pct):.4f} USD per trade\n"
            f"  - Daily Drawdown:             {dd_color}{self.daily_drawdown_pct:.2f}% (Limit: {self.max_daily_loss_pct*100:.1f}%){RESET}\n"
            f"{sub_border}\n"
            f"  {BOLD}PERFORMANCE & FEE ACCOUNTING:{RESET}\n"
            f"  - Cumulative Gross P&L:       ${self.gross_pnl_usd:+,.4f} USD\n"
            f"  - Total Actual Brokerage/GST: ${self.fees_paid_usd:,.4f} USD\n"
            f"  - Realized Final Net P&L:     {net_color}${self.net_pnl_usd:+,.4f} USD{RESET}\n"
            f"  - Status:                     {cb_line}\n"
            f"{sub_border}\n"
            f"  {BOLD}ACTIVE POSITION & STRATEGY TELEMETRY:{RESET}\n"
            f"  - Position:       {pos_line}\n"
            f"  - Market Regime:  {BOLD}{self.market_regime}{RESET}\n"
            f"  - Strategy Scan:  {self.scan_reason}\n"
            f"{sub_border}\n"
            f"  {BOLD}RECENT SESSION TRADE AUDIT (LAST 5):{RESET}\n"
            f"{logs_block}\n"
            f"{border}\n"
        )
        return dashboard_str
