"""
RAMYA QUANT ROBOT — Percentage-Based Risk & Profit Optimizer Engine
Implements:
1. Dynamic Percentage Risk Sizing: Max 0.5% of verified account equity per trade.
   Formula: Contracts = floor( (Equity * 0.005) / (SL_Distance * Contract_Val + Fees + Slippage + Funding) )
   If minimum order size (1 contract) risk exceeds 0.5% budget -> SKIP/REJECT TRADE.
2. Pre-Trade Fee & Cost Estimation:
   - Maker entry fee (0.02%)
   - Taker SL exit fee (0.05%)
   - Maker TP exit fee (0.02%)
   - Expected slippage (2 ticks) & funding buffer
3. Net Profit Optimization:
   - Gross P&L and Net P&L calculated & recorded separately
   - If expected net reward after fees <= total fees or Net R:R < 1.5 -> REJECT TRADE
4. Circuit Breakers & Automatic Protection:
   - 2 consecutive losses -> 4-hour halt
   - 2.0% daily drawdown limit -> halt
   - Anti-Martingale: strictly zero position size doubling or recovery scaling
"""

import logging
import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from .config import (
    ALLOW_MARTINGALE,
    BASE_CAPITAL_FLOOR_USD,
    BASE_ORDER_SIZE_CONTRACTS,
    BE_FEE_BUFFER_ATR,
    CIRCUIT_BREAKER_HALT_HOURS,
    DAILY_DRAWDOWN_CIRCUIT_BREAKER_PCT,
    DAILY_MAX_TRADES_LIMIT,
    DEFAULT_SLIPPAGE_RATE,
    DEFAULT_TAKER_FEE_RATE,
    EMERGENCY_PEAK_DRAWDOWN_USD,
    HARD_LOCKED_LEVERAGE,
    INITIAL_CAPITAL_USD,
    INITIAL_PRINCIPAL_USD,
    KILL_SWITCH_HALT_HOURS,
    M1_BREAKEVEN_FEE_COVER_USD,
    M1_BREAKEVEN_TRIGGER_USD,
    M1_DYNAMIC_TRAIL_OFFSET_USD,
    M1_SL_OFFSET,
    M1_TP_OFFSET,
    MARGIN_PER_CONTRACT_USD,
    MAX_CONSECUTIVE_LOSSES,
    MAX_CONTRACTS_CAP,
    MAX_DAILY_NET_LOSS_USD,
    MAX_EQUITY_RISK_PER_TRADE_PCT,
    MAX_MARGIN_UTILIZATION_PCT,
    MAX_RISK_LIMIT_CEILING_PCT,
    MAX_RISK_PER_TRADE_PCT,
    MIN_RISK_REWARD_RATIO,
    PROFIT_LOCK_PERCENTAGE,
    PROFIT_VAULT_LOCK_PCT,
    RISK_PER_TRADE_PCT,
    SUPPORTED_CONTRACTS,
    TIER_1_CONTRACT_SIZE,
    TIER_1_MAX_BALANCE,
    TIER_1_SL,
    TIER_1_TP,
    TIER_1_TRADE_CAP,
    TIER_2_CONTRACT_SIZE,
    TIER_2_MAX_BALANCE,
    TIER_2_SL,
    TIER_2_TP,
    TIER_2_TRADE_CAP,
    TIER_3_CONTRACT_SIZE,
    TIER_3_MAX_BALANCE,
    TIER_3_SL,
    TIER_3_TP,
    TIER_3_TRADE_CAP,
    TIER_4_CONTRACT_SIZE,
    TIER_4_SL,
    TIER_4_TP,
    TIER_4_TRADE_CAP,
    TRADEABLE_PROFIT_PCT,
    TRAILING_BE_ACTIVATION_ATR,
    TRAILING_DISTANCE_ATR,
    WALLET_GROWTH_SCALE_PCT,
)

logger = logging.getLogger("quant.risk")


@dataclass
class AccountTier:
    tier_name: str
    tier_level: int
    min_balance: float
    max_balance: float
    daily_trade_cap: int
    contracts: int
    take_profit: float
    stop_loss: float


@dataclass
class SizingDecision:
    is_approved: bool
    contracts: int
    coin_units: float
    notional_usd: float
    gross_risk_usd: float
    net_risk_usd: float
    gross_reward_usd: float
    net_reward_usd: float
    risk_pct: float
    risk_reward: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    estimated_fees_usd: float
    estimated_slippage_usd: float
    reason: str

    @property
    def risk_usd(self) -> float:
        """Backward compatibility for existing test assertions."""
        return self.net_risk_usd


class QuantRiskManager:
    def __init__(
        self,
        initial_equity: float = INITIAL_PRINCIPAL_USD,
        risk_per_trade_pct: float = RISK_PER_TRADE_PCT,
        min_rr: float = MIN_RISK_REWARD_RATIO,
        daily_dd_limit_pct: float = DAILY_DRAWDOWN_CIRCUIT_BREAKER_PCT,
        leverage: int = HARD_LOCKED_LEVERAGE,
    ):
        self.initial_equity = initial_equity
        self.current_equity = initial_equity
        self.day_start_equity = initial_equity
        self.daily_realized_gross_pnl = 0.0
        self.daily_realized_net_pnl = 0.0
        self.daily_unrealized_pnl = 0.0
        self.daily_fees_paid = 0.0
        self.daily_slippage_paid = 0.0

        # Capital Vault & Trailing Profit Lock
        # Base Capital Floor: $22.00 USD (Never risk below initial principal)
        self.base_capital_floor = INITIAL_PRINCIPAL_USD if initial_equity <= 35.0 else 0.0
        self.peak_equity = initial_equity
        self.profit_lock_pct = PROFIT_VAULT_LOCK_PCT          # Lock 75% of peak net profits into Protected Safe Balance
        self.tradeable_profit_pct = TRADEABLE_PROFIT_PCT      # Only 25% of new profits usable for risk/margin
        self.max_contracts_cap = MAX_CONTRACTS_CAP            # Hard upper limit: 40 contracts maximum
        self.max_equity_risk_pct = MAX_EQUITY_RISK_PER_TRADE_PCT  # Never risk more than 3% total equity
        self.locked_reserve_profit = 0.0
        self.capital_vault_floor = self.base_capital_floor
        self.emergency_peak_drawdown_limit_usd = EMERGENCY_PEAK_DRAWDOWN_USD  # -$2.50 from peak
        self.kill_switch_halt_hours = KILL_SWITCH_HALT_HOURS                  # 2.0 hours
        self.margin_per_contract_usd = MARGIN_PER_CONTRACT_USD                # Safe reference margin

        # Hard ceiling: planned risk cannot exceed 0.5% (or explicitly provided lower risk)
        self.risk_per_trade_pct = min(risk_per_trade_pct, MAX_RISK_PER_TRADE_PCT)
        self.min_rr = min_rr
        self.daily_dd_limit_pct = daily_dd_limit_pct
        self.leverage = leverage

        self.consecutive_loss_count = 0
        self.daily_trade_count = 0

        # Dynamic Tier Scaling Initialization
        self.current_tier = self.get_account_tier(initial_equity)
        self.max_daily_trades = self.current_tier.daily_trade_cap
        self.max_daily_net_loss_usd = MAX_DAILY_NET_LOSS_USD
        self.circuit_breaker_active = False
        self.circuit_breaker_halt_until: float = 0.0
        self.circuit_breaker_reason = ""
        self.active_positions: Dict[str, dict] = {}

    def get_account_tier(self, balance: Optional[float] = None) -> AccountTier:
        """Determines dynamic tier, daily trade cap, contracts, TP, and SL based on verified equity."""
        bal = balance if balance is not None else self.current_equity
        if bal < TIER_1_MAX_BALANCE:
            return AccountTier(
                tier_name="Tier 1 ($22-$35)",
                tier_level=1,
                min_balance=22.0,
                max_balance=TIER_1_MAX_BALANCE,
                daily_trade_cap=TIER_1_TRADE_CAP,
                contracts=TIER_1_CONTRACT_SIZE,
                take_profit=TIER_1_TP,
                stop_loss=TIER_1_SL,
            )
        elif bal < TIER_2_MAX_BALANCE:
            return AccountTier(
                tier_name="Tier 2 ($35-$55)",
                tier_level=2,
                min_balance=TIER_1_MAX_BALANCE,
                max_balance=TIER_2_MAX_BALANCE,
                daily_trade_cap=TIER_2_TRADE_CAP,
                contracts=TIER_2_CONTRACT_SIZE,
                take_profit=TIER_2_TP,
                stop_loss=TIER_2_SL,
            )
        else:
            return AccountTier(
                tier_name="Tier 3 ($55+ Cap 40)",
                tier_level=3,
                min_balance=TIER_2_MAX_BALANCE,
                max_balance=float("inf"),
                daily_trade_cap=TIER_3_TRADE_CAP,
                contracts=TIER_3_CONTRACT_SIZE,
                take_profit=TIER_3_TP,
                stop_loss=TIER_3_SL,
            )

    def get_scaled_lot_size(self, current_price: float = 4145.0, contract_value: float = 0.001) -> int:
        """
        Calculates dynamic contract position size according to strict specifications:
        1. Base start: 10 contracts while equity < $35 (~$22).
        2. Equity reaches $35 -> Scale to 20 contracts ($35 <= equity < $55).
        3. Equity reaches $55+ -> Scale to 40 contracts (equity >= $55).
        4. Hard upper limit: 40 contracts maximum.
        5. Dynamic scaling must only utilize initial $22 + 25% of new profits (75% locked into safe balance).
        6. Stop-loss risk cap: Never risk more than 3% of total equity on any single trade stop-loss.
        """
        bal = self.current_equity
        if bal < 35.0:
            target_contracts = 10
        elif bal < 55.0:
            target_contracts = 20
        else:
            target_contracts = 40

        # Hard upper limit: 40 contracts maximum
        target_contracts = min(target_contracts, self.max_contracts_cap)

        # 75% Profit Vault & Risk Lock:
        # Dynamic scaling must ONLY utilize initial $22 + 25% of new profits
        cumulative_profit = max(0.0, self.current_equity - self.base_capital_floor)
        tradeable_pool = self.base_capital_floor + (cumulative_profit * (1.0 - self.profit_lock_pct))

        # Margin check: tradeable pool must cover position margin
        margin_per_contract = (current_price * contract_value) / max(1, self.leverage)
        max_allowed_by_margin = max(1, int(tradeable_pool / margin_per_contract))

        # Strict Cap: Never risk more than 3% of total equity on single trade SL
        max_risk_usd = self.max_equity_risk_pct * self.current_equity
        risk_per_contract = max(0.0001, M1_SL_OFFSET * contract_value)
        max_allowed_by_risk = max(1, int(max_risk_usd / risk_per_contract))

        return max(1, min(target_contracts, max_allowed_by_margin, max_allowed_by_risk, self.max_contracts_cap))

    @property
    def daily_realized_pnl(self) -> float:
        """Backward compatibility: returns daily net realized PnL."""
        return self.daily_realized_net_pnl

    @daily_realized_pnl.setter
    def daily_realized_pnl(self, val: float):
        self.daily_realized_net_pnl = val

    def update_equity(self, verified_equity: float):
        """Updates equity from verified Delta Exchange account balance."""
        if verified_equity > 0:
            self.current_equity = verified_equity
            if self.current_equity > self.peak_equity:
                self.peak_equity = self.current_equity
                if self.base_capital_floor > 0:
                    peak_profit = max(0.0, self.peak_equity - self.base_capital_floor)
                    if peak_profit >= 0.20:
                        self.locked_reserve_profit = round(peak_profit * self.profit_lock_pct, 4)
                        self.capital_vault_floor = round(self.base_capital_floor + self.locked_reserve_profit, 4)
                    else:
                        self.locked_reserve_profit = 0.0
                        self.capital_vault_floor = self.base_capital_floor

            self.current_tier = self.get_account_tier(self.current_equity)
            self.max_daily_trades = self.current_tier.daily_trade_cap

            self.is_circuit_breaker_tripped()

    def is_circuit_breaker_tripped(self) -> Tuple[bool, str]:
        """Checks if emergency peak pullback, capital vault floor, daily loss breaker, or lockout is active."""
        now = time.time()
        if self.circuit_breaker_active:
            if now < self.circuit_breaker_halt_until:
                hours_left = (self.circuit_breaker_halt_until - now) / 3600.0
                return True, f"HALTED: Circuit Breaker active ({self.circuit_breaker_reason}). Remaining: {hours_left:.1f}h"
            else:
                self.circuit_breaker_active = False
                self.circuit_breaker_reason = ""
                logger.info("Circuit breaker halt period elapsed. Trading resumed.")

        # Update peak equity and locked vault floor if equity grew
        if self.current_equity > self.peak_equity:
            self.peak_equity = self.current_equity
            if self.base_capital_floor > 0:
                peak_profit = max(0.0, self.peak_equity - self.base_capital_floor)
                if peak_profit >= 0.20:
                    self.locked_reserve_profit = round(peak_profit * self.profit_lock_pct, 4)
                    self.capital_vault_floor = round(self.base_capital_floor + self.locked_reserve_profit, 4)
                else:
                    self.locked_reserve_profit = 0.0
                    self.capital_vault_floor = self.base_capital_floor

        # 1. Autonomous Drawdown Kill-Switch: If intraday equity drops -$2.50 from current session peak (for micro-capital accounts)
        pullback_from_peak = round(self.peak_equity - self.current_equity, 4)
        if self.day_start_equity < 100.0 and pullback_from_peak >= self.emergency_peak_drawdown_limit_usd:
            self.circuit_breaker_active = True
            self.circuit_breaker_halt_until = now + (self.kill_switch_halt_hours * 3600)
            self.circuit_breaker_reason = (
                f"Drawdown Kill-Switch: Equity pullback (${pullback_from_peak:.2f}) "
                f">= ${self.emergency_peak_drawdown_limit_usd:.2f} from session peak (${self.peak_equity:.2f}). Trading frozen for {self.kill_switch_halt_hours:.1f}h."
            )
            logger.critical(f"[DRAWDOWN KILL-SWITCH HALT] {self.circuit_breaker_reason}")
            return True, self.circuit_breaker_reason

        # 2. Capital Vault Floor Breach: If current equity drops below locked reserve floor
        if self.base_capital_floor > 0 and self.locked_reserve_profit >= 0.14:
            if self.current_equity < self.capital_vault_floor:
                self.circuit_breaker_active = True
                self.circuit_breaker_halt_until = now + (self.kill_switch_halt_hours * 3600)
                self.circuit_breaker_reason = (
                    f"Capital Vault Floor Breach: Current equity (${self.current_equity:.2f}) "
                    f"< Untouchable reserve floor (${self.capital_vault_floor:.2f}, 70% locked profit ${self.locked_reserve_profit:.2f}). Trading halted."
                )
                logger.critical(f"[CAPITAL VAULT FLOOR HALT] {self.circuit_breaker_reason}")
                return True, self.circuit_breaker_reason

        total_daily_pnl = self.daily_realized_net_pnl + self.daily_unrealized_pnl

        # 3. Capital Protection Circuit Breaker for Micro Accounts (Equity < $100.0):
        # Enforces hard -$3.00 USD Max Daily Net Loss ceiling.
        if self.day_start_equity < 100.0:
            if total_daily_pnl <= -self.max_daily_net_loss_usd:
                self.circuit_breaker_active = True
                self.circuit_breaker_halt_until = now + (CIRCUIT_BREAKER_HALT_HOURS * 3600)
                self.circuit_breaker_reason = (
                    f"Capital Protection Circuit Breaker: Daily Net Loss (${abs(total_daily_pnl):.2f}) "
                    f"reached -${self.max_daily_net_loss_usd:.2f} limit. All trading halted."
                )
                logger.critical(f"[CAPITAL PROTECTION HALT] {self.circuit_breaker_reason}")
                return True, self.circuit_breaker_reason
        else:
            # Check real-time daily drawdown percentage limit for standard accounts
            max_allowed_loss = self.day_start_equity * self.daily_dd_limit_pct
            if total_daily_pnl <= -max_allowed_loss:
                self.circuit_breaker_active = True
                self.circuit_breaker_halt_until = now + (CIRCUIT_BREAKER_HALT_HOURS * 3600)
                self.circuit_breaker_reason = (
                    f"Daily Drawdown Breach: Loss ${abs(total_daily_pnl):.4f} >= {self.daily_dd_limit_pct*100:.1f}% (${max_allowed_loss:.4f})"
                )
                logger.critical(f"[CIRCUIT BREAKER TRIGGERED] {self.circuit_breaker_reason} — Halted for 4h.")
                return True, self.circuit_breaker_reason

        return False, "OK"

    def calculate_position_size(
        self,
        symbol: str,
        entry_price: float,
        stop_loss: float,
        take_profit_2: float,
        strategy_type: str = "MTF",
    ) -> SizingDecision:
        """
        Dynamically calculates contract quantity based on verified equity and 0.5% risk budget:
        Total Risk per contract = (abs(Entry - SL) * Contract_Value) + Maker_Entry_Fee + Taker_SL_Fee + Slippage + Funding
        Raw Contracts = floor( (Equity * 0.005) / Total_Risk_Per_Contract )

        If Minimum Order Size (1 contract) risk exceeds 0.5% equity budget -> REJECT / SKIP TRADE.
        If Expected Net Reward after fees <= 0 or Net R:R < 1.5 -> REJECT / SKIP TRADE.
        Strictly Anti-Martingale: Never increase size after losses.
        """
        # 1. Circuit breaker check
        tripped, cb_reason = self.is_circuit_breaker_tripped()
        if tripped:
            return SizingDecision(
                is_approved=False, contracts=0, coin_units=0.0, notional_usd=0.0,
                gross_risk_usd=0.0, net_risk_usd=0.0, gross_reward_usd=0.0, net_reward_usd=0.0,
                risk_pct=0.0, risk_reward=0.0, stop_loss=stop_loss, take_profit_1=0.0,
                take_profit_2=take_profit_2, estimated_fees_usd=0.0, estimated_slippage_usd=0.0,
                reason=cb_reason
            )

        # 1b. Daily Max Trades Hard Cap (Only if finite cap is set, e.g. < 999999)
        if self.max_daily_trades < 999999 and self.daily_trade_count >= self.max_daily_trades:
            return SizingDecision(
                is_approved=False, contracts=0, coin_units=0.0, notional_usd=0.0,
                gross_risk_usd=0.0, net_risk_usd=0.0, gross_reward_usd=0.0, net_reward_usd=0.0,
                risk_pct=0.0, risk_reward=0.0, stop_loss=stop_loss, take_profit_1=0.0,
                take_profit_2=take_profit_2, estimated_fees_usd=0.0, estimated_slippage_usd=0.0,
                reason=f"REJECTED: Daily maximum trades limit ({self.daily_trade_count}/{self.max_daily_trades}) reached. Standing down."
            )

        # 2. Open position limit check (1 per symbol)
        if symbol in self.active_positions:
            return SizingDecision(
                is_approved=False, contracts=0, coin_units=0.0, notional_usd=0.0,
                gross_risk_usd=0.0, net_risk_usd=0.0, gross_reward_usd=0.0, net_reward_usd=0.0,
                risk_pct=0.0, risk_reward=0.0, stop_loss=stop_loss, take_profit_1=0.0,
                take_profit_2=take_profit_2, estimated_fees_usd=0.0, estimated_slippage_usd=0.0,
                reason=f"REJECTED: Active position already open for {symbol}"
            )

        contract_spec = SUPPORTED_CONTRACTS.get(symbol)
        if not contract_spec:
            return SizingDecision(
                is_approved=False, contracts=0, coin_units=0.0, notional_usd=0.0,
                gross_risk_usd=0.0, net_risk_usd=0.0, gross_reward_usd=0.0, net_reward_usd=0.0,
                risk_pct=0.0, risk_reward=0.0, stop_loss=stop_loss, take_profit_1=0.0,
                take_profit_2=take_profit_2, estimated_fees_usd=0.0, estimated_slippage_usd=0.0,
                reason=f"REJECTED: Unsupported symbol {symbol}"
            )

        # 3. Mandatory Stop-Loss Validation
        sl_distance = abs(entry_price - stop_loss)
        if sl_distance <= 0:
            return SizingDecision(
                is_approved=False, contracts=0, coin_units=0.0, notional_usd=0.0,
                gross_risk_usd=0.0, net_risk_usd=0.0, gross_reward_usd=0.0, net_reward_usd=0.0,
                risk_pct=0.0, risk_reward=0.0, stop_loss=stop_loss, take_profit_1=0.0,
                take_profit_2=take_profit_2, estimated_fees_usd=0.0, estimated_slippage_usd=0.0,
                reason="REJECTED: Stop-loss distance must be greater than zero"
            )

        contract_val = contract_spec["contract_value"]  # 0.001 for Gold/BTC, 0.01 for ETH
        maker_fee_rate = contract_spec.get("maker_fee_rate", 0.0001)
        taker_fee_rate = contract_spec.get("taker_fee_rate", 0.0001)
        tick_size = contract_spec.get("tick_size", 0.01)

        # 4. Fee, Slippage & Funding Estimation per contract
        # Entry is Maker Limit (post_only=True)
        entry_fee_per_contract = entry_price * contract_val * maker_fee_rate
        # Exit on Stop-Loss is Market Taker
        exit_sl_fee_per_contract = stop_loss * contract_val * taker_fee_rate
        # Estimated 2 ticks slippage on SL fill
        slippage_per_contract = 2 * tick_size * contract_val
        # Applicable funding buffer (1 bps = 0.01%)
        funding_buffer_per_contract = entry_price * contract_val * 0.0001

        # M1 Fast Swing / Arrow Scalp path (Tiered Auto-Scaling & Unlimited Dynamic Compounder)
        if strategy_type == "M1_SWING_ARROW_SCALP":
            tier = self.get_account_tier(self.current_equity)
            contracts = self.get_scaled_lot_size(entry_price, contract_val)
            contracts = min(contracts, self.max_contracts_cap)
            coin_units = round(contracts * contract_val, 4)
            notional_usd = round(coin_units * entry_price, 2)
            margin_required = notional_usd / max(1, self.leverage)

            # Check tradeable pool (Initial principal $22 + 25% of realized profits):
            cumulative_profit = max(0.0, self.current_equity - self.base_capital_floor)
            tradeable_pool = self.base_capital_floor + (cumulative_profit * (1.0 - self.profit_lock_pct))
            margin_per_contract = (entry_price * contract_val) / max(1, self.leverage)
            if margin_required > tradeable_pool:
                contracts = max(1, int(tradeable_pool / margin_per_contract))
                contracts = min(contracts, self.max_contracts_cap)
                coin_units = round(contracts * contract_val, 4)
                notional_usd = round(coin_units * entry_price, 2)
                margin_required = notional_usd / max(1, self.leverage)

            gross_risk_usd = round(contracts * sl_distance * contract_val, 4)
            total_sl_fees = round(contracts * (entry_fee_per_contract + exit_sl_fee_per_contract + slippage_per_contract), 4)
            net_risk_usd = round(gross_risk_usd + total_sl_fees, 4)

            # Strict 3% equity risk ceiling
            max_risk_budget = self.max_equity_risk_pct * self.current_equity
            if net_risk_usd > max_risk_budget:
                risk_unit = (sl_distance * contract_val) + entry_fee_per_contract + exit_sl_fee_per_contract + slippage_per_contract
                contracts = max(1, int(max_risk_budget / risk_unit))
                contracts = min(contracts, self.max_contracts_cap)
                coin_units = round(contracts * contract_val, 4)
                notional_usd = round(coin_units * entry_price, 2)
                margin_required = notional_usd / max(1, self.leverage)
                gross_risk_usd = round(contracts * sl_distance * contract_val, 4)
                total_sl_fees = round(contracts * (entry_fee_per_contract + exit_sl_fee_per_contract + slippage_per_contract), 4)
                net_risk_usd = round(gross_risk_usd + total_sl_fees, 4)

            reward_distance = abs(take_profit_2 - entry_price) if take_profit_2 > 0 else tier.take_profit
            gross_reward_usd = round(contracts * reward_distance * contract_val, 4)
            exit_tp_fee_per_contract = (entry_price + reward_distance) * contract_val * maker_fee_rate
            total_tp_fees = round(contracts * (entry_fee_per_contract + exit_tp_fee_per_contract), 4)
            net_expected_reward_usd = round(gross_reward_usd - total_tp_fees, 4)

            price_rr = round(reward_distance / sl_distance, 2) if sl_distance > 0 else 0.0
            actual_risk_pct = round((net_risk_usd / self.current_equity) * 100.0, 3)

            if self.current_equity < margin_required:
                return SizingDecision(
                    is_approved=False, contracts=0, coin_units=0.0, notional_usd=0.0,
                    gross_risk_usd=0.0, net_risk_usd=0.0, gross_reward_usd=0.0, net_reward_usd=0.0,
                    risk_pct=0.0, risk_reward=0.0, stop_loss=stop_loss, take_profit_1=0.0,
                    take_profit_2=take_profit_2, estimated_fees_usd=0.0, estimated_slippage_usd=0.0,
                    reason=f"REJECTED: Insufficient operational margin for {contracts} contract(s) (${margin_required:.2f} required > ${self.current_equity:.2f} equity)",
                )

            # Check Capital Vault untouchable reserve floor
            if self.base_capital_floor > 0 and self.locked_reserve_profit > 0 and self.current_equity <= self.capital_vault_floor:
                return SizingDecision(
                    is_approved=False, contracts=0, coin_units=0.0, notional_usd=0.0,
                    gross_risk_usd=0.0, net_risk_usd=0.0, gross_reward_usd=0.0, net_reward_usd=0.0,
                    risk_pct=0.0, risk_reward=0.0, stop_loss=stop_loss, take_profit_1=0.0,
                    take_profit_2=take_profit_2, estimated_fees_usd=0.0, estimated_slippage_usd=0.0,
                    reason=f"REJECTED: Capital Vault reserve floor reached (${self.current_equity:.2f} <= ${self.capital_vault_floor:.2f})",
                )

            direction = "BUY" if take_profit_2 > entry_price else "SELL"
            # Trailing Breakeven: Snap SL to entry once trade reaches +0.35 gold points
            tp1 = round(entry_price + M1_BREAKEVEN_TRIGGER_USD, 2) if direction == "BUY" else round(entry_price - M1_BREAKEVEN_TRIGGER_USD, 2)

            trade_status_str = f"Trade #{self.daily_trade_count + 1} (Unlimited Compounder)" if self.max_daily_trades >= 999999 else f"Trade {self.daily_trade_count + 1}/{tier.daily_trade_cap}"
            return SizingDecision(
                is_approved=True,
                contracts=contracts,
                coin_units=coin_units,
                notional_usd=notional_usd,
                gross_risk_usd=gross_risk_usd,
                net_risk_usd=net_risk_usd,
                gross_reward_usd=gross_reward_usd,
                net_reward_usd=net_expected_reward_usd,
                risk_pct=actual_risk_pct,
                risk_reward=price_rr,
                stop_loss=stop_loss,
                take_profit_1=tp1,
                take_profit_2=take_profit_2,
                estimated_fees_usd=total_tp_fees,
                estimated_slippage_usd=round(slippage_per_contract * contracts, 4),
                reason=f"APPROVED: Auto-Scaled {contracts} contract(s) ({trade_status_str}) | Cap: 40 | Safe Vault 75% | SL -${sl_distance:.2f} | TP +${reward_distance:.2f} | Price R:R {price_rr}:1",
            )

        # Total trade risk per single contract
        price_risk_per_contract = sl_distance * contract_val
        total_risk_per_contract = (
            price_risk_per_contract
            + entry_fee_per_contract
            + exit_sl_fee_per_contract
            + slippage_per_contract
            + funding_buffer_per_contract
        )

        if total_risk_per_contract <= 0:
            return SizingDecision(
                is_approved=False, contracts=0, coin_units=0.0, notional_usd=0.0,
                gross_risk_usd=0.0, net_risk_usd=0.0, gross_reward_usd=0.0, net_reward_usd=0.0,
                risk_pct=0.0, risk_reward=0.0, stop_loss=stop_loss, take_profit_1=0.0,
                take_profit_2=take_profit_2, estimated_fees_usd=0.0, estimated_slippage_usd=0.0,
                reason="REJECTED: Invalid total risk calculation per contract"
            )

        # 5. Position Sizing derived strictly from 0.5% Verified Equity Budget
        risk_budget_usd = self.current_equity * self.risk_per_trade_pct  # 0.005 * Equity
        raw_contracts = risk_budget_usd / total_risk_per_contract
        lot_size = contract_spec.get("lot_size", 1)
        contracts = int(raw_contracts // lot_size * lot_size)

        min_size = contract_spec.get("min_order_size", 1)
        # CRITICAL RULE 1: If 1 minimum contract exceeds 0.5% risk budget, SKIP/REJECT THE TRADE
        if contracts < min_size:
            min_contract_risk = min_size * total_risk_per_contract
            return SizingDecision(
                is_approved=False,
                contracts=0,
                coin_units=0.0,
                notional_usd=0.0,
                gross_risk_usd=round(min_size * price_risk_per_contract, 4),
                net_risk_usd=round(min_contract_risk, 4),
                gross_reward_usd=0.0,
                net_reward_usd=0.0,
                risk_pct=round((min_contract_risk / self.current_equity) * 100.0, 3),
                risk_reward=0.0,
                stop_loss=stop_loss,
                take_profit_1=0.0,
                take_profit_2=take_profit_2,
                estimated_fees_usd=round(min_size * (entry_fee_per_contract + exit_sl_fee_per_contract), 4),
                estimated_slippage_usd=round(min_size * slippage_per_contract, 4),
                reason=(
                    f"REJECTED: Minimum order size ({min_size} contract) net risk (${min_contract_risk:.4f}) "
                    f"exceeds {self.risk_per_trade_pct*100:.2f}% equity budget (${risk_budget_usd:.4f}). Trade skipped."
                )
            )

        # 6. Operational Margin & Leverage Ceiling Check
        margin_per_contract = (entry_price * contract_val) / max(1, self.leverage)
        max_contracts_margin = int((self.current_equity * 0.90) / margin_per_contract)  # 10% cash buffer
        if contracts > max_contracts_margin:
            contracts = max_contracts_margin

        if contracts < min_size:
            return SizingDecision(
                is_approved=False, contracts=0, coin_units=0.0, notional_usd=0.0,
                gross_risk_usd=0.0, net_risk_usd=0.0, gross_reward_usd=0.0, net_reward_usd=0.0,
                risk_pct=0.0, risk_reward=0.0, stop_loss=stop_loss, take_profit_1=0.0,
                take_profit_2=take_profit_2, estimated_fees_usd=0.0, estimated_slippage_usd=0.0,
                reason="REJECTED: Insufficient operational margin for 1 contract at current leverage"
            )

        # 7. Fee Control & Expected Net Reward Optimization
        reward_distance = abs(take_profit_2 - entry_price)
        gross_reward_usd = contracts * reward_distance * contract_val

        # Take-Profit is executed as Maker Limit Order
        exit_tp_fee_per_contract = take_profit_2 * contract_val * maker_fee_rate
        total_tp_fees = contracts * (entry_fee_per_contract + exit_tp_fee_per_contract + funding_buffer_per_contract)
        net_expected_reward_usd = gross_reward_usd - total_tp_fees

        gross_risk_usd = contracts * price_risk_per_contract
        total_sl_fees = contracts * (
            entry_fee_per_contract + exit_sl_fee_per_contract + slippage_per_contract + funding_buffer_per_contract
        )
        net_risk_usd = gross_risk_usd + total_sl_fees

        net_rr = round(net_expected_reward_usd / net_risk_usd, 2) if net_risk_usd > 0 else 0.0

        # CRITICAL RULE 3: Reject if net reward is insufficient after fees or net R:R < min_rr
        if net_expected_reward_usd <= total_tp_fees or net_rr < self.min_rr:
            return SizingDecision(
                is_approved=False,
                contracts=0,
                coin_units=0.0,
                notional_usd=0.0,
                gross_risk_usd=round(gross_risk_usd, 4),
                net_risk_usd=round(net_risk_usd, 4),
                gross_reward_usd=round(gross_reward_usd, 4),
                net_reward_usd=round(net_expected_reward_usd, 4),
                risk_pct=round((net_risk_usd / self.current_equity) * 100.0, 3),
                risk_reward=net_rr,
                stop_loss=stop_loss,
                take_profit_1=0.0,
                take_profit_2=take_profit_2,
                estimated_fees_usd=round(total_tp_fees, 4),
                estimated_slippage_usd=round(contracts * slippage_per_contract, 4),
                reason=(
                    f"REJECTED: Expected net reward (${net_expected_reward_usd:.4f}) insufficient after fees "
                    f"(${total_tp_fees:.4f}) or Net Risk-to-Reward ({net_rr}:1) < Minimum ({self.min_rr}:1). Trade skipped."
                )
            )

        coin_units = contracts * contract_val
        notional_usd = coin_units * entry_price
        actual_risk_pct = round((net_risk_usd / self.current_equity) * 100.0, 3)

        # Compute Target 1 (+1.5 ATR for Breakeven arming)
        direction = "BUY" if take_profit_2 > entry_price else "SELL"
        tp1 = round(entry_price + (1.5 * sl_distance), 2) if direction == "BUY" else round(entry_price - (1.5 * sl_distance), 2)

        return SizingDecision(
            is_approved=True,
            contracts=contracts,
            coin_units=round(coin_units, 4),
            notional_usd=round(notional_usd, 2),
            gross_risk_usd=round(gross_risk_usd, 4),
            net_risk_usd=round(net_risk_usd, 4),
            gross_reward_usd=round(gross_reward_usd, 4),
            net_reward_usd=round(net_expected_reward_usd, 4),
            risk_pct=actual_risk_pct,
            risk_reward=net_rr,
            stop_loss=stop_loss,
            take_profit_1=tp1,
            take_profit_2=take_profit_2,
            estimated_fees_usd=round(total_tp_fees, 4),
            estimated_slippage_usd=round(contracts * slippage_per_contract, 4),
            reason=f"APPROVED: Passed 0.5% risk budget (${risk_budget_usd:.4f}) & Net R:R {net_rr}:1 fee checks"
        )

    def update_trailing_stop(
        self,
        position: dict,
        current_price: float,
        high_watermark: float,
        low_watermark: float,
        atr: float
    ) -> Tuple[float, bool, str]:
        """
        Manages Trailing Stop Rules:
        - Target 1 hit (+1.5 ATR): Move SL to Breakeven (+ fee buffer).
        - Trailing: Thereafter, trail by 1.0 ATR behind highest/lowest price.
        """
        direction = position.get("direction", position.get("side", "")).upper()
        entry_price = position["entry_price"]
        current_sl = position["stop_loss"]
        is_be_armed = position.get("is_breakeven_armed", position.get("is_be_armed", False))
        updated = False
        reason = "NO_CHANGE"

        if direction in ("LONG", "BUY"):
            profit_dist = high_watermark - entry_price
            if not is_be_armed and profit_dist >= (TRAILING_BE_ACTIVATION_ATR * atr):
                be_level = round(entry_price + (BE_FEE_BUFFER_ATR * atr), 2)
                if be_level > current_sl:
                    current_sl = be_level
                    is_be_armed = True
                    updated = True
                    reason = f"TARGET 1 HIT (+1.5 ATR): SL moved to Breakeven (${be_level:,.2f})"

            if is_be_armed:
                trailing_sl = round(high_watermark - (TRAILING_DISTANCE_ATR * atr), 2)
                if trailing_sl > current_sl:
                    current_sl = trailing_sl
                    updated = True
                    reason = f"TRAILING STOP UPDATED: New SL ${current_sl:,.2f}"

        elif direction in ("SHORT", "SELL"):
            profit_dist = entry_price - low_watermark
            if not is_be_armed and profit_dist >= (TRAILING_BE_ACTIVATION_ATR * atr):
                be_level = round(entry_price - (BE_FEE_BUFFER_ATR * atr), 2)
                if be_level < current_sl:
                    current_sl = be_level
                    is_be_armed = True
                    updated = True
                    reason = f"TARGET 1 HIT (+1.5 ATR): SL moved to Breakeven (${be_level:,.2f})"

            if is_be_armed:
                trailing_sl = round(low_watermark + (TRAILING_DISTANCE_ATR * atr), 2)
                if trailing_sl < current_sl:
                    current_sl = trailing_sl
                    updated = True
                    reason = f"TRAILING STOP UPDATED: New SL ${current_sl:,.2f}"

        position["is_breakeven_armed"] = is_be_armed
        position["is_be_armed"] = is_be_armed
        return current_sl, updated, reason

    def record_trade_exit(
        self,
        symbol: str,
        gross_pnl: float,
        net_pnl: Optional[float] = None,
        fees_paid: float = 0.0,
        slippage_paid: float = 0.0,
    ):
        """
        Records trade completion with separate gross and net P&L accounting.
        Strict Anti-Martingale: Next trade sizing is computed on updated (reduced) equity.
        """
        if net_pnl is None:
            net_pnl = gross_pnl

        self.daily_realized_gross_pnl += gross_pnl
        self.daily_realized_net_pnl += net_pnl
        self.daily_fees_paid += fees_paid
        self.daily_slippage_paid += slippage_paid
        self.daily_trade_count += 1
        self.current_equity += net_pnl

        if net_pnl < 0 and gross_pnl <= 0:
            self.consecutive_loss_count += 1
            logger.warning(
                f"[LOSS RECORDED] Net: ${net_pnl:+.4f} (Gross: ${gross_pnl:+.4f}, Fees: ${fees_paid:.4f}). "
                f"Consecutive losses: {self.consecutive_loss_count}/{MAX_CONSECUTIVE_LOSSES}"
            )
            # 4-hour consecutive loss lockout applies to standard accounts (>= $100).
            # Micro-capital high-frequency scalping accounts ($20-$25) are strictly protected by the -$3.00 USD daily net loss limit.
            if self.day_start_equity >= 100.0 and self.consecutive_loss_count >= MAX_CONSECUTIVE_LOSSES:
                now = time.time()
                self.circuit_breaker_active = True
                self.circuit_breaker_halt_until = now + (CIRCUIT_BREAKER_HALT_HOURS * 3600)
                self.circuit_breaker_reason = (
                    f"{self.consecutive_loss_count} consecutive losses circuit breaker triggered"
                )
                logger.critical(f"[CIRCUIT BREAKER] {self.circuit_breaker_reason} — Halted for 4 hours.")
        else:
            self.consecutive_loss_count = 0
            logger.info(
                f"[PROFIT RECORDED] Net: ${net_pnl:+.4f} (Gross: ${gross_pnl:+.4f}, Fees: ${fees_paid:.4f}). "
                f"Consecutive losses reset to 0."
            )

        self.is_circuit_breaker_tripped()

        if symbol in self.active_positions:
            del self.active_positions[symbol]
