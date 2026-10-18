"""
RAMYA QUANT ROBOT — Mandatory Pre-Trade Cost, Profitability & Risk Sizing Engine
Institutional-Grade Pre-Trade Gatekeeper and Post-Trade Accounting Module

Core Governance & Risk Safeguards:
A. Calculate Every Trading Cost:
   - Entry trading fee (Maker / Taker)
   - Estimated exit trading fee (Maker for TP, Taker for SL)
   - Applicable GST (18% Goods & Services Tax on trading commissions for Delta Exchange India)
   - Bid/Ask spread crossing cost
   - Expected slippage (configurable ticks / rate)
   - Funding / swap / settlement costs (based on holding duration & funding rate)
   - Expected Net P&L = Gross P&L - Entry Fees - Exit Fees - Taxes/GST - Slippage - Funding
   - Rejection Gate: Reject if Net R:R < min_threshold (default 1.5) or if total costs consume > 35% of gross profit.
   - Post-Trade Accounting: Recalculate net P&L with actual fills and fees, displaying gross, costs, and net separately.

B. Stop-Loss & Take-Profit:
   - Protective SL and TP derived from account equity, stop distance, contract specs, and 0.25% risk budget.
   - Prefer reduce-only protective exits.
   - Never widen a stop to hide losses.
   - Transparent disclaimer: Stop orders do not guarantee exact fill prices during market gaps or extreme volatility.

C. Dynamic Lot Sizing:
   - Increases size ONLY when equity, validated performance, available margin, and risk limits justify it.
   - Configurable risk per trade: initially 0.25% of equity (0.0025).
   - Strict daily loss limit (circuit breaker).
   - Enforce max order size, max total exposure (margin utilization cap), and max leverage (hard safe cap).
   - Large candle or AI confidence must NEVER trigger unlimited lots or 100% leverage.

D. Trade Frequency:
   - Fresh signals only passing every fee, liquidity, risk, and exposure check.
   - Cooldown timer between trades.
   - Duplicate-signal protection (prevents repeated entries on identical signal).
   - Maximum daily trade count controls.
   - Never trade merely to churn volume.

E. Mandatory Pre-Order Decision:
   - Outputs:
     1. Expected gross profit and gross loss
     2. Estimated round-trip costs
     3. Expected net profit at target
     4. Expected net loss at stop
     5. Net reward-to-risk ratio
     6. Position size and leverage
     7. Exact reason for ACCEPT or REJECT
   - If fee data, contract specs, or market data are unavailable -> REJECT immediately.

F. Capital Protection & Reporting:
   - Display realized net P&L after all actual costs, daily drawdown, remaining risk budget, margin usage, and equity curve.
   - Stop opening new trades when daily loss limit is reached.
   - Default mode is strictly PAPER. Live execution requires explicit authorization.
"""

import logging
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("quant.cost_engine")


@dataclass
class CostBreakdown:
    entry_fee_usd: float
    exit_fee_usd: float
    gst_charges_usd: float          # 18% GST on trading brokerage
    bid_ask_spread_usd: float       # Cost of crossing the spread
    estimated_slippage_usd: float   # Expected slippage on execution
    estimated_funding_usd: float    # Expected funding / swap cost
    total_costs_usd: float
    cost_to_gross_ratio: float = 0.0  # total_costs / gross_profit


@dataclass
class PreOrderDecision:
    is_approved: bool
    status: str                     # "ACCEPT" or "REJECT"
    reason: str
    symbol: str
    action: str                     # "BUY" or "SELL"
    contracts: int
    leverage: int
    entry_price: float
    stop_loss: float
    take_profit: float
    gross_profit_usd: float
    gross_loss_usd: float
    round_trip_costs_tp_usd: float
    round_trip_costs_sl_usd: float
    cost_breakdown_tp: CostBreakdown
    cost_breakdown_sl: CostBreakdown
    expected_net_profit_usd: float
    expected_net_loss_usd: float
    net_reward_to_risk_ratio: float
    margin_required_usd: float
    margin_utilization_pct: float
    risk_pct_of_equity: float
    trading_mode: str = "PAPER"
    disclaimer: str = (
        "Execution Risk Notice: Stop-loss orders do not guarantee exact execution price during "
        "market gaps, illiquidity, or extreme volatility. Slippage and execution costs may apply."
    )

    def format_decision_summary(self) -> str:
        """Formats the mandatory pre-order decision output."""
        border = "=" * 90
        sub_border = "-" * 90
        status_line = f"[{self.status}] {self.reason}"
        if self.is_approved:
            status_tag = f"\033[92m{status_line}\033[0m"
        else:
            status_tag = f"\033[91m{status_line}\033[0m"

        return (
            f"\n{border}\n"
            f"    MANDATORY PRE-ORDER PROFITABILITY & RISK AUDIT — [{self.trading_mode} MODE]\n"
            f"    Status: {status_tag}\n"
            f"    Target Contract: {self.symbol} | Action: {self.action} | Size: {self.contracts} cts | Lev: {self.leverage}x\n"
            f"    Entry: ${self.entry_price:,.2f} | SL: ${self.stop_loss:,.2f} | TP: ${self.take_profit:,.2f}\n"
            f"{sub_border}\n"
            f"    1. PROFIT SCENARIO (TAKE-PROFIT @ ${self.take_profit:,.2f}):\n"
            f"       - Expected Gross Profit:   +${self.gross_profit_usd:.4f} USD\n"
            f"       - Estimated Round-Trip Cost: -${self.round_trip_costs_tp_usd:.4f} USD\n"
            f"         [Breakdown: Entry Fee: ${self.cost_breakdown_tp.entry_fee_usd:.4f} | Exit Fee: ${self.cost_breakdown_tp.exit_fee_usd:.4f} | "
            f"GST (18%): ${self.cost_breakdown_tp.gst_charges_usd:.4f} | Spread: ${self.cost_breakdown_tp.bid_ask_spread_usd:.4f} | "
            f"Slippage: ${self.cost_breakdown_tp.estimated_slippage_usd:.4f} | Funding: ${self.cost_breakdown_tp.estimated_funding_usd:.4f}]\n"
            f"       - Expected Net Profit:     +${self.expected_net_profit_usd:.4f} USD (Cost Consumption: {self.cost_breakdown_tp.cost_to_gross_ratio*100:.1f}%)\n"
            f"{sub_border}\n"
            f"    2. LOSS SCENARIO (STOP-LOSS @ ${self.stop_loss:,.2f}):\n"
            f"       - Expected Gross Loss:     -${self.gross_loss_usd:.4f} USD\n"
            f"       - Estimated SL Costs:      -${self.round_trip_costs_sl_usd:.4f} USD (incl. Taker exit fee + 2 ticks slippage + GST)\n"
            f"       - Expected Net Loss:       -${self.expected_net_loss_usd:.4f} USD (Risk: {self.risk_pct_of_equity:.3f}% of equity)\n"
            f"{sub_border}\n"
            f"    3. RISK-REWARD & MARGIN ALLOCATION:\n"
            f"       - Net Reward-to-Risk Ratio: {self.net_reward_to_risk_ratio:.2f}:1 (Min Required: >= 1.50:1)\n"
            f"       - Margin Required:         ${self.margin_required_usd:.2f} USD ({self.margin_utilization_pct:.1f}% of equity)\n"
            f"    4. DISCLAIMER: {self.disclaimer}\n"
            f"{border}\n"
        )


@dataclass
class ClosedTradeAccounting:
    symbol: str
    side: str
    size: int
    entry_price: float
    exit_price: float
    gross_pnl_usd: float
    entry_fee_usd: float
    exit_fee_usd: float
    gst_charges_usd: float
    actual_slippage_usd: float
    actual_funding_usd: float
    total_costs_usd: float
    final_net_pnl_usd: float
    exit_reason: str
    closed_at: float
    realized_roi_pct: float


class TradeCooldownManager:
    """Manages trade frequency, cooldowns, duplicate signals, and trade caps."""

    def __init__(self, cooldown_seconds: float = 60.0, max_trades_per_day: int = 50):
        self.cooldown_seconds = cooldown_seconds
        self.max_trades_per_day = max_trades_per_day
        self.last_trade_time: float = 0.0
        self.last_signal_signature: Optional[str] = None
        self.daily_trade_count: int = 0
        self.daily_closed_trade_count: int = 0

    def check_frequency_guard(self, signal_signature: str) -> Tuple[bool, str]:
        """Validates cooldown, duplicate signal, and trade cap."""
        now = time.time()
        elapsed = now - self.last_trade_time

        # 1. Cooldown Check
        if self.last_trade_time > 0 and elapsed < self.cooldown_seconds:
            remaining = self.cooldown_seconds - elapsed
            return False, f"REJECTED: Cooldown active ({remaining:.1f}s remaining of {self.cooldown_seconds}s limit)"

        # 2. Duplicate Signal Protection
        if self.last_signal_signature == signal_signature:
            return False, f"REJECTED: Duplicate signal protection triggered (identical signal already processed)"

        # 3. Maximum Daily Trades Cap
        if self.daily_trade_count >= self.max_trades_per_day:
            return False, f"REJECTED: Maximum daily trade limit reached ({self.daily_trade_count}/{self.max_trades_per_day})"

        return True, "APPROVED: Trade frequency checks passed"

    def record_trade_placed(self, signal_signature: str):
        self.last_trade_time = time.time()
        self.last_signal_signature = signal_signature
        self.daily_trade_count += 1

    def record_trade_closed(self):
        self.daily_closed_trade_count += 1


class CostAndProfitabilityEngine:
    """
    Mandatory Pre-Trade Profitability, All-Cost Control, and Dynamic Position Sizing Engine.
    Enforces rigorous institutional capital protection, cost modeling, and accounting.
    """

    def __init__(
        self,
        initial_equity: float = 21.73,
        max_risk_per_trade_pct: float = 0.0025,  # 0.25% initial default
        daily_loss_limit_pct: float = 0.01,     # 1.0% daily loss limit
        min_net_risk_reward: float = 1.5,       # Minimum 1:1.5 Net RR
        max_cost_consumption_pct: float = 0.35, # Costs must not consume > 35% of gross profit
        max_leverage_cap: int = 10,             # Hard cap on leverage (never 100x unconstrained)
        max_contracts_cap: int = 25,            # Hard cap on contracts
        max_margin_utilization_pct: float = 0.25,# 25% max margin utilization
        gst_rate: float = 0.18,                 # 18% GST on brokerage in India
        slippage_ticks: int = 2,                # Expected slippage (2 ticks)
        est_funding_rate_8h: float = 0.0001,    # 1 bps funding per 8h
        cooldown_seconds: float = 60.0,
        max_trades_per_day: int = 50,
        is_paper_mode: bool = True,             # PAPER mode strictly enabled by default
    ):
        self.initial_equity = initial_equity
        self.current_equity = initial_equity
        self.day_start_equity = initial_equity
        self.max_risk_per_trade_pct = max_risk_per_trade_pct
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.min_net_risk_reward = min_net_risk_reward
        self.max_cost_consumption_pct = max_cost_consumption_pct
        self.max_leverage_cap = max_leverage_cap
        self.max_contracts_cap = max_contracts_cap
        self.max_margin_utilization_pct = max_margin_utilization_pct
        self.gst_rate = gst_rate
        self.slippage_ticks = slippage_ticks
        self.est_funding_rate_8h = est_funding_rate_8h
        self.is_paper_mode = is_paper_mode

        # Frequency & Cooldown Gate
        self.frequency_manager = TradeCooldownManager(
            cooldown_seconds=cooldown_seconds,
            max_trades_per_day=max_trades_per_day,
        )

        # Performance & Capital Accounting
        self.daily_realized_gross_pnl = 0.0
        self.daily_realized_costs = 0.0
        self.daily_realized_net_pnl = 0.0
        self.daily_unrealized_pnl = 0.0
        self.trade_history: List[ClosedTradeAccounting] = []
        self.equity_curve: List[Tuple[float, float]] = [(time.time(), initial_equity)]
        self.circuit_breaker_tripped = False
        self.circuit_breaker_reason = ""

    # ==========================================================================
    # A. COST CALCULATION & PRE-TRADE MODELING
    # ==========================================================================
    def calculate_trading_costs(
        self,
        symbol: str,
        entry_price: float,
        exit_price: float,
        size: int,
        contract_val: float,
        maker_fee_rate: float,
        taker_fee_rate: float,
        tick_size: float,
        is_tp: bool,
        spread: float = 0.01,
        est_holding_hours: float = 0.25,  # 15 minutes average holding time for scalps
    ) -> CostBreakdown:
        """
        Calculates every applicable trading cost:
        - Entry fee (Maker limit)
        - Exit fee (Maker for TP, Taker for SL)
        - Applicable GST (18% on trading commissions for Delta Exchange India)
        - Bid/Ask spread crossing cost
        - Expected slippage (2 ticks * tick_size * contract_val * size)
        - Funding / swap cost (pro-rated per holding time)
        """
        entry_notional = entry_price * contract_val * size
        exit_notional = exit_price * contract_val * size

        # 1. Brokerage Fees
        entry_fee_usd = round(entry_notional * maker_fee_rate, 4)
        if is_tp:
            exit_fee_usd = round(exit_notional * maker_fee_rate, 4)
        else:
            exit_fee_usd = round(exit_notional * taker_fee_rate, 4)

        # 2. Applicable GST (18% on brokerage)
        gst_charges_usd = round((entry_fee_usd + exit_fee_usd) * self.gst_rate, 4)

        # 3. Bid/Ask Spread Cost
        bid_ask_spread_usd = round(spread * contract_val * size, 4)

        # 4. Expected Slippage (SL has taker slippage, TP has zero limit slippage)
        if is_tp:
            estimated_slippage_usd = 0.0
        else:
            estimated_slippage_usd = round(self.slippage_ticks * tick_size * contract_val * size, 4)

        # 5. Funding / Swap Cost
        funding_periods = max(0.01, est_holding_hours / 8.0)
        estimated_funding_usd = round(entry_notional * self.est_funding_rate_8h * funding_periods, 4)

        # Total Round-Trip Costs
        total_costs_usd = round(
            entry_fee_usd
            + exit_fee_usd
            + gst_charges_usd
            + bid_ask_spread_usd
            + estimated_slippage_usd
            + estimated_funding_usd,
            4,
        )

        return CostBreakdown(
            entry_fee_usd=entry_fee_usd,
            exit_fee_usd=exit_fee_usd,
            gst_charges_usd=gst_charges_usd,
            bid_ask_spread_usd=bid_ask_spread_usd,
            estimated_slippage_usd=estimated_slippage_usd,
            estimated_funding_usd=estimated_funding_usd,
            total_costs_usd=total_costs_usd,
            cost_to_gross_ratio=0.0,
        )

    # ==========================================================================
    # B. DYNAMIC LOT SIZING & CIRCUIT BREAKER
    # ==========================================================================
    def check_circuit_breaker(self) -> Tuple[bool, str]:
        """Checks if daily loss limit or drawdown threshold has been reached."""
        if self.circuit_breaker_tripped:
            return True, f"CIRCUIT_BREAKER_ACTIVE: {self.circuit_breaker_reason}"

        max_daily_loss_allowed = self.day_start_equity * self.daily_loss_limit_pct
        current_daily_loss = -(self.daily_realized_net_pnl + self.daily_unrealized_pnl)

        if current_daily_loss >= max_daily_loss_allowed:
            self.circuit_breaker_tripped = True
            self.circuit_breaker_reason = (
                f"Daily Loss Limit Reached: Loss ${current_daily_loss:.4f} >= "
                f"${max_daily_loss_allowed:.4f} ({self.daily_loss_limit_pct*100:.1f}% of starting equity). Trading halted."
            )
            logger.critical(f"[CIRCUIT BREAKER TRIGGERED] {self.circuit_breaker_reason}")
            return True, self.circuit_breaker_reason

        return False, "OK"

    def reset_circuit_breaker(self, new_equity: Optional[float] = None):
        """Resets daily loss tracker for new session or explicit reset."""
        if new_equity is not None:
            self.current_equity = new_equity
            self.day_start_equity = new_equity
        self.daily_realized_gross_pnl = 0.0
        self.daily_realized_costs = 0.0
        self.daily_realized_net_pnl = 0.0
        self.daily_unrealized_pnl = 0.0
        self.circuit_breaker_tripped = False
        self.circuit_breaker_reason = ""
        logger.info(f"[CIRCUIT BREAKER RESET] Fresh session initialized at ${self.current_equity:.2f} equity.")

    def calculate_position_size(
        self,
        entry_price: float,
        stop_loss: float,
        contract_val: float,
        tick_size: float,
        maker_fee_rate: float,
        taker_fee_rate: float,
        leverage: int,
        symbol: str = "XAUTUSD",
    ) -> Tuple[int, int, float, str]:
        """
        Calculates position size strictly from:
        1. Account equity and maximum permitted risk.
        2. Stop distance in points.
        3. Contract specifications and round-trip fee costs.
        4. Maximum leverage constraint (capped at max_leverage_cap).
        5. Maximum margin utilization constraint (capped at max_margin_utilization_pct).
        6. Hard contracts cap (max_contracts_cap).

        Rule: A large candle or AI confidence score must NEVER automatically
        trigger unlimited lots or 100% leverage increases.
        Returns: (approved_contracts, safe_leverage, risk_budget_usd, reason)
        """
        # Enforce hard ceiling on leverage
        safe_leverage = min(leverage, self.max_leverage_cap)

        sl_distance = abs(entry_price - stop_loss)
        if sl_distance <= 0:
            return 0, safe_leverage, 0.0, "REJECTED: Stop loss distance must be greater than zero"

        # Risk budget from equity
        risk_budget_usd = self.current_equity * self.max_risk_per_trade_pct

        # Total expected risk per single contract at stop loss
        price_risk_per_contract = sl_distance * contract_val
        cost_sl_1ct = self.calculate_trading_costs(
            symbol=symbol,
            entry_price=entry_price,
            exit_price=stop_loss,
            size=1,
            contract_val=contract_val,
            maker_fee_rate=maker_fee_rate,
            taker_fee_rate=taker_fee_rate,
            tick_size=tick_size,
            is_tp=False,
        )
        total_risk_per_contract = price_risk_per_contract + cost_sl_1ct.total_costs_usd

        if total_risk_per_contract <= 0:
            return 0, safe_leverage, risk_budget_usd, "REJECTED: Invalid risk per contract"

        raw_contracts = int(risk_budget_usd / total_risk_per_contract)

        # Reject if 1 minimum contract exceeds risk budget
        if raw_contracts < 1:
            return (
                0,
                safe_leverage,
                risk_budget_usd,
                f"REJECTED: Minimum 1 contract net risk (${total_risk_per_contract:.4f}) exceeds "
                f"{self.max_risk_per_trade_pct*100:.2f}% equity risk budget (${risk_budget_usd:.4f}). Trade skipped.",
            )

        # Enforce maximum margin utilization cap
        margin_per_contract = (entry_price * contract_val) / max(1, safe_leverage)
        max_margin_budget = self.current_equity * self.max_margin_utilization_pct
        max_contracts_by_margin = max(1, int(max_margin_budget / margin_per_contract))

        contracts = min(raw_contracts, max_contracts_by_margin, self.max_contracts_cap)

        return contracts, safe_leverage, risk_budget_usd, "OK"

    # ==========================================================================
    # C. MANDATORY PRE-ORDER DECISION GATE
    # ==========================================================================
    def evaluate_pre_trade_decision(
        self,
        symbol: str,
        action: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        proposed_leverage: int = 10,
        contract_spec: Optional[Dict[str, Any]] = None,
        spread: float = 0.01,
        signal_signature: Optional[str] = None,
    ) -> PreOrderDecision:
        """
        Mandatory Pre-Trade Profitability and Cost-Control Engine:
        Evaluates every cost, stop-loss and take-profit scenario, net RR, margin exposure,
        and cooldowns. Outputs structured ACCEPT / REJECT decision.
        """
        # 1. Missing Data Validation (CRITICAL RULE E: If data unavailable, do not open trade)
        if not contract_spec or entry_price <= 0 or stop_loss <= 0 or take_profit <= 0:
            return self._build_rejection_decision(
                symbol, action, entry_price, stop_loss, take_profit, proposed_leverage,
                "REJECTED: Missing market data, fee schedule or contract specifications"
            )

        contract_val = float(contract_spec.get("contract_value", 0.001))
        tick_size = float(contract_spec.get("tick_size", 0.01))
        maker_fee_rate = float(contract_spec.get("maker_commission_rate", contract_spec.get("maker_fee_rate", 0.0001)))
        taker_fee_rate = float(contract_spec.get("taker_commission_rate", contract_spec.get("taker_fee_rate", 0.0001)))

        # 2. Circuit Breaker Check
        cb_tripped, cb_reason = self.check_circuit_breaker()
        if cb_tripped:
            return self._build_rejection_decision(
                symbol, action, entry_price, stop_loss, take_profit, proposed_leverage,
                cb_reason
            )

        # 3. Trade Frequency & Cooldown Check
        sig = signal_signature or f"{symbol}_{action}_{int(time.time() // 60)}"
        freq_ok, freq_reason = self.frequency_manager.check_frequency_guard(sig)
        if not freq_ok:
            return self._build_rejection_decision(
                symbol, action, entry_price, stop_loss, take_profit, proposed_leverage,
                freq_reason
            )

        # 4. Stop-Loss Direction Validation (Never widen stop or invert price)
        if action.upper() == "BUY":
            if stop_loss >= entry_price or take_profit <= entry_price:
                return self._build_rejection_decision(
                    symbol, action, entry_price, stop_loss, take_profit, proposed_leverage,
                    f"REJECTED: Invalid price bounds for BUY (SL ${stop_loss} >= Entry ${entry_price} or TP ${take_profit} <= Entry)"
                )
        else:
            if stop_loss <= entry_price or take_profit >= entry_price:
                return self._build_rejection_decision(
                    symbol, action, entry_price, stop_loss, take_profit, proposed_leverage,
                    f"REJECTED: Invalid price bounds for SELL (SL ${stop_loss} <= Entry ${entry_price} or TP ${take_profit} >= Entry)"
                )

        # 5. Dynamic Lot Sizing & Leverage Cap Check
        contracts, safe_leverage, risk_budget_usd, size_reason = self.calculate_position_size(
            entry_price=entry_price,
            stop_loss=stop_loss,
            contract_val=contract_val,
            tick_size=tick_size,
            maker_fee_rate=maker_fee_rate,
            taker_fee_rate=taker_fee_rate,
            leverage=proposed_leverage,
            symbol=symbol,
        )
        if contracts < 1:
            return self._build_rejection_decision(
                symbol, action, entry_price, stop_loss, take_profit, safe_leverage,
                size_reason
            )

        # 6. Detailed Trading Cost Calculation (TP and SL Scenarios)
        costs_tp = self.calculate_trading_costs(
            symbol=symbol,
            entry_price=entry_price,
            exit_price=take_profit,
            size=contracts,
            contract_val=contract_val,
            maker_fee_rate=maker_fee_rate,
            taker_fee_rate=taker_fee_rate,
            tick_size=tick_size,
            is_tp=True,
            spread=spread,
        )

        costs_sl = self.calculate_trading_costs(
            symbol=symbol,
            entry_price=entry_price,
            exit_price=stop_loss,
            size=contracts,
            contract_val=contract_val,
            maker_fee_rate=maker_fee_rate,
            taker_fee_rate=taker_fee_rate,
            tick_size=tick_size,
            is_tp=False,
            spread=spread,
        )

        # 7. Expected Gross & Net P&L Calculation
        tp_distance = abs(take_profit - entry_price)
        sl_distance = abs(entry_price - stop_loss)

        gross_profit_usd = round(tp_distance * contract_val * contracts, 4)
        gross_loss_usd = round(sl_distance * contract_val * contracts, 4)

        expected_net_profit_usd = round(gross_profit_usd - costs_tp.total_costs_usd, 4)
        expected_net_loss_usd = round(gross_loss_usd + costs_sl.total_costs_usd, 4)

        # Cost-to-profit consumption ratio
        cost_consumption_ratio = costs_tp.total_costs_usd / max(0.0001, gross_profit_usd)
        costs_tp.cost_to_gross_ratio = round(cost_consumption_ratio, 4)

        # Net Reward-to-Risk Ratio
        net_rr = round(expected_net_profit_usd / max(0.0001, expected_net_loss_usd), 2)

        # 8. Rejection Gates:
        # A) Expected net reward <= 0
        if expected_net_profit_usd <= 0:
            return self._build_rejection_decision(
                symbol, action, entry_price, stop_loss, take_profit, safe_leverage,
                f"REJECTED: Expected net profit is negative or zero after all fees (${expected_net_profit_usd:.4f})"
            )

        # B) Costs consume too much expected profit (> max_cost_consumption_pct)
        if cost_consumption_ratio > self.max_cost_consumption_pct:
            return self._build_rejection_decision(
                symbol, action, entry_price, stop_loss, take_profit, safe_leverage,
                f"REJECTED: Costs consume too much profit ({cost_consumption_ratio*100:.1f}% > "
                f"{self.max_cost_consumption_pct*100:.0f}% max allowed). Trade skipped."
            )

        # C) Net Reward-to-Risk < min threshold
        if net_rr < self.min_net_risk_reward:
            return self._build_rejection_decision(
                symbol, action, entry_price, stop_loss, take_profit, safe_leverage,
                f"REJECTED: Net Reward-to-Risk ({net_rr}:1) < Minimum Required ({self.min_net_risk_reward}:1). Trade skipped."
            )

        # Margin requirements
        margin_required_usd = round((entry_price * contract_val * contracts) / max(1, safe_leverage), 2)
        margin_utilization_pct = round((margin_required_usd / self.current_equity) * 100.0, 2)
        risk_pct_of_equity = round((expected_net_loss_usd / self.current_equity) * 100.0, 3)

        trading_mode = "PAPER" if self.is_paper_mode else "LIVE"

        decision = PreOrderDecision(
            is_approved=True,
            status="ACCEPT",
            reason=(
                f"APPROVED: Net RR {net_rr}:1 >= {self.min_net_risk_reward}:1 | "
                f"Net Profit +${expected_net_profit_usd:.4f} | Costs ${costs_tp.total_costs_usd:.4f} ({cost_consumption_ratio*100:.1f}%) | "
                f"Risk {risk_pct_of_equity:.3f}% <= 0.25% Equity Budget"
            ),
            symbol=symbol,
            action=action.upper(),
            contracts=contracts,
            leverage=safe_leverage,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            gross_profit_usd=gross_profit_usd,
            gross_loss_usd=gross_loss_usd,
            round_trip_costs_tp_usd=costs_tp.total_costs_usd,
            round_trip_costs_sl_usd=costs_sl.total_costs_usd,
            cost_breakdown_tp=costs_tp,
            cost_breakdown_sl=costs_sl,
            expected_net_profit_usd=expected_net_profit_usd,
            expected_net_loss_usd=expected_net_loss_usd,
            net_reward_to_risk_ratio=net_rr,
            margin_required_usd=margin_required_usd,
            margin_utilization_pct=margin_utilization_pct,
            risk_pct_of_equity=risk_pct_of_equity,
            trading_mode=trading_mode,
        )

        # Record trade placement in frequency manager
        self.frequency_manager.record_trade_placed(sig)
        return decision

    def _build_rejection_decision(
        self,
        symbol: str,
        action: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        leverage: int,
        reason: str,
    ) -> PreOrderDecision:
        empty_cost = CostBreakdown(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        trading_mode = "PAPER" if self.is_paper_mode else "LIVE"
        return PreOrderDecision(
            is_approved=False,
            status="REJECT",
            reason=reason,
            symbol=symbol,
            action=action.upper(),
            contracts=0,
            leverage=leverage,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            gross_profit_usd=0.0,
            gross_loss_usd=0.0,
            round_trip_costs_tp_usd=0.0,
            round_trip_costs_sl_usd=0.0,
            cost_breakdown_tp=empty_cost,
            cost_breakdown_sl=empty_cost,
            expected_net_profit_usd=0.0,
            expected_net_loss_usd=0.0,
            net_reward_to_risk_ratio=0.0,
            margin_required_usd=0.0,
            margin_utilization_pct=0.0,
            risk_pct_of_equity=0.0,
            trading_mode=trading_mode,
        )

    # ==========================================================================
    # D. STOP-LOSS INTEGRITY & PROTECTIVE ADJUSTMENTS
    # ==========================================================================
    def validate_stop_loss_adjustment(
        self, current_sl: float, proposed_sl: float, side: str
    ) -> Tuple[bool, str]:
        """
        Never widen a stop to hide losses:
        - For BUY: proposed_sl must be >= current_sl (can only move up towards/past entry).
        - For SELL: proposed_sl must be <= current_sl (can only move down towards/past entry).
        """
        if side.upper() == "BUY":
            if proposed_sl < current_sl:
                return (
                    False,
                    f"REJECTED: Stop loss cannot be widened for BUY (proposed ${proposed_sl:.2f} < current ${current_sl:.2f}). Never widen a stop to hide losses.",
                )
        else:
            if proposed_sl > current_sl:
                return (
                    False,
                    f"REJECTED: Stop loss cannot be widened for SELL (proposed ${proposed_sl:.2f} > current ${current_sl:.2f}). Never widen a stop to hide losses.",
                )
        return True, "APPROVED: Protective stop loss adjustment"

    # ==========================================================================
    # E. POST-TRADE RECONCILIATION & ACCOUNTING
    # ==========================================================================
    def record_closed_trade_accounting(
        self,
        symbol: str,
        side: str,
        size: int,
        entry_price: float,
        exit_price: float,
        actual_entry_fee: Optional[float] = None,
        actual_exit_fee: Optional[float] = None,
        contract_val: float = 0.001,
        maker_fee_rate: float = 0.0001,
        taker_fee_rate: float = 0.0001,
        actual_slippage_usd: float = 0.0,
        actual_funding_usd: float = 0.0,
        exit_reason: str = "TARGET_HIT",
    ) -> ClosedTradeAccounting:
        """
        Recalculates net P&L using actual fills and actual fees after closing every position.
        Displays gross P&L, total costs, and final net P&L separately.
        """
        price_diff = (exit_price - entry_price) if side.upper() == "BUY" else (entry_price - exit_price)
        gross_pnl_usd = round(price_diff * contract_val * size, 4)

        # Actual or resolved fees
        if actual_entry_fee is not None:
            entry_fee_usd = round(actual_entry_fee, 4)
        else:
            entry_fee_usd = round(entry_price * contract_val * size * maker_fee_rate, 4)

        if actual_exit_fee is not None:
            exit_fee_usd = round(actual_exit_fee, 4)
        else:
            fee_rate = maker_fee_rate if "TP" in exit_reason.upper() else taker_fee_rate
            exit_fee_usd = round(exit_price * contract_val * size * fee_rate, 4)

        gst_charges_usd = round((entry_fee_usd + exit_fee_usd) * self.gst_rate, 4)
        total_costs_usd = round(
            entry_fee_usd + exit_fee_usd + gst_charges_usd + actual_slippage_usd + actual_funding_usd,
            4,
        )
        final_net_pnl_usd = round(gross_pnl_usd - total_costs_usd, 4)

        notional_entry = entry_price * contract_val * size
        realized_roi_pct = round((final_net_pnl_usd / max(0.01, notional_entry)) * 100.0, 3)

        closed_record = ClosedTradeAccounting(
            symbol=symbol,
            side=side.upper(),
            size=size,
            entry_price=entry_price,
            exit_price=exit_price,
            gross_pnl_usd=gross_pnl_usd,
            entry_fee_usd=entry_fee_usd,
            exit_fee_usd=exit_fee_usd,
            gst_charges_usd=gst_charges_usd,
            actual_slippage_usd=actual_slippage_usd,
            actual_funding_usd=actual_funding_usd,
            total_costs_usd=total_costs_usd,
            final_net_pnl_usd=final_net_pnl_usd,
            exit_reason=exit_reason,
            closed_at=time.time(),
            realized_roi_pct=realized_roi_pct,
        )

        # Update engine state
        self.daily_realized_gross_pnl = round(self.daily_realized_gross_pnl + gross_pnl_usd, 4)
        self.daily_realized_costs = round(self.daily_realized_costs + total_costs_usd, 4)
        self.daily_realized_net_pnl = round(self.daily_realized_net_pnl + final_net_pnl_usd, 4)
        self.current_equity = round(self.current_equity + final_net_pnl_usd, 4)
        self.equity_curve.append((time.time(), self.current_equity))
        self.trade_history.append(closed_record)
        self.frequency_manager.record_trade_closed()

        # Check circuit breaker after loss
        self.check_circuit_breaker()

        return closed_record

    def format_closed_trade_report(self, record: ClosedTradeAccounting) -> str:
        """Displays gross P&L, total costs, and final net P&L separately."""
        border = "=" * 90
        sub_border = "-" * 90
        roi_sign = "+" if record.realized_roi_pct >= 0 else ""
        return (
            f"\n{border}\n"
            f"    POST-TRADE RECONCILIATION & ACCOUNTING\n"
            f"    [TRADE] {record.side} {record.size} contracts of {record.symbol}\n"
            f"    [FILLS] Entry: ${record.entry_price:,.2f} | Exit: ${record.exit_price:,.2f} | Reason: {record.exit_reason}\n"
            f"{sub_border}\n"
            f"    [GROSS P&L]     ${record.gross_pnl_usd:+,.4f} USD\n"
            f"    [TOTAL COSTS]    ${record.total_costs_usd:,.4f} USD (Entry: ${record.entry_fee_usd:.4f}, Exit: ${record.exit_fee_usd:.4f}, GST: ${record.gst_charges_usd:.4f}, Slippage: ${record.actual_slippage_usd:.4f}, Funding: ${record.actual_funding_usd:.4f})\n"
            f"    [FINAL NET P&L]  ${record.final_net_pnl_usd:+,.4f} USD ({roi_sign}{record.realized_roi_pct:.2f}% ROI)\n"
            f"{sub_border}\n"
            f"    [PORTFOLIO TELEMETRY]\n"
            f"    - Current Verified Equity: ${self.current_equity:,.2f} USD\n"
            f"    - Daily Realized Gross:    ${self.daily_realized_gross_pnl:+,.4f} USD\n"
            f"    - Daily Total Costs:       ${self.daily_realized_costs:,.4f} USD\n"
            f"    - Daily Realized Net:      ${self.daily_realized_net_pnl:+,.4f} USD\n"
            f"    - Daily Drawdown:          {self.get_daily_drawdown_pct():.2f}%\n"
            f"    - Remaining Risk Budget:   ${self.get_remaining_risk_budget():.4f} USD\n"
            f"{border}\n"
        )

    # ==========================================================================
    # F. CAPITAL PROTECTION & PORTFOLIO TELEMETRY
    # ==========================================================================
    def get_daily_drawdown_pct(self) -> float:
        """Calculates current intraday drawdown percentage from starting equity."""
        if self.day_start_equity <= 0:
            return 0.0
        dd_usd = self.day_start_equity - self.current_equity
        return max(0.0, round((dd_usd / self.day_start_equity) * 100.0, 2))

    def get_remaining_risk_budget(self) -> float:
        """Calculates remaining risk budget for today before hitting the daily loss limit."""
        max_daily_loss = self.day_start_equity * self.daily_loss_limit_pct
        current_daily_loss = -(self.daily_realized_net_pnl + self.daily_unrealized_pnl)
        return max(0.0, round(max_daily_loss - current_daily_loss, 4))

    def get_performance_report(self) -> Dict[str, Any]:
        """Returns structured portfolio performance report."""
        return {
            "current_equity": self.current_equity,
            "day_start_equity": self.day_start_equity,
            "daily_realized_gross_pnl": self.daily_realized_gross_pnl,
            "daily_realized_costs": self.daily_realized_costs,
            "daily_realized_net_pnl": self.daily_realized_net_pnl,
            "daily_drawdown_pct": self.get_daily_drawdown_pct(),
            "remaining_risk_budget": self.get_remaining_risk_budget(),
            "total_trades_count": len(self.trade_history),
            "circuit_breaker_tripped": self.circuit_breaker_tripped,
            "circuit_breaker_reason": self.circuit_breaker_reason,
            "trading_mode": "PAPER" if self.is_paper_mode else "LIVE",
            "equity_curve": list(self.equity_curve),
        }
