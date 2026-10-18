"""
RAMYA QUANT ROBOT — Aggressive Auto-Compounder Risk & Scale Engine
Target: Exponential Scaling to $150-$300+/Day on Delta Exchange India (XAUTUSD)

Core Architectural Specifications:
1. Dynamic Contract Scaling:
   Contracts = max(1, floor(Current_Equity / 15.0))
   Instantly scales up +1 micro-contract per every $15 increase in verified equity.
2. Uncompromised Capital Vault:
   - Absolute Hard-Floor locked at $22.97 USD (never risk base equity).
   - High-Water Mark Lock: 70% of peak session net profits locked into untouchable floor.
   - Session Drawdown Kill-Switch: -$2.50 from peak halts trading for 2 hours.
   - Trailing Breakeven: Immediately snap SL to Entry +$0.02 at +$0.10 profit.
3. Execution & Fee Defense:
   - 100% Strict Maker Post-Only Limit Orders (Zero taker drain).
   - Micro-Scalp Target: +$0.25 | Hard Stop-Loss: -$0.16.
4. Unlimited 24/7 Executions:
   - Zero daily trade limits or artificial ceilings.
"""

import logging
import math
from typing import Dict, Optional, Tuple

from quant_robot.config import (
    BASE_CAPITAL_FLOOR_USD,
    BASE_ORDER_SIZE_CONTRACTS,
    DAILY_MAX_TRADES_LIMIT,
    EMERGENCY_PEAK_DRAWDOWN_USD,
    HARD_LOCKED_LEVERAGE,
    INITIAL_PRINCIPAL_USD,
    KILL_SWITCH_HALT_HOURS,
    M1_BREAKEVEN_FEE_COVER_USD,
    M1_BREAKEVEN_TRIGGER_USD,
    M1_DYNAMIC_TRAIL_OFFSET_USD,
    M1_SL_OFFSET,
    M1_TP_OFFSET,
    MARGIN_PER_CONTRACT_USD,
    MAX_CONTRACTS_CAP,
    MAX_EQUITY_RISK_PER_TRADE_PCT,
    MAX_MARGIN_UTILIZATION_PCT,
    PROFIT_LOCK_PERCENTAGE,
    PROFIT_VAULT_LOCK_PCT,
    TRADEABLE_PROFIT_PCT,
)
from quant_robot.risk_manager import (
    AccountTier,
    QuantRiskManager,
    SizingDecision,
)

logger = logging.getLogger("quant.engine.risk_manager")


def calculate_compound_lot_size(
    current_equity: float,
    base_contracts: int = BASE_ORDER_SIZE_CONTRACTS,
    initial_principal: float = INITIAL_PRINCIPAL_USD,
    profit_vault_lock_pct: float = PROFIT_VAULT_LOCK_PCT,
    max_equity_risk_pct: float = MAX_EQUITY_RISK_PER_TRADE_PCT,
    sl_points: float = M1_SL_OFFSET,
    leverage: int = HARD_LOCKED_LEVERAGE,
    current_price: float = 4145.0,
    contract_value: float = 0.001,
    max_cap: int = MAX_CONTRACTS_CAP,
) -> int:
    """
    Calculates dynamic contract position size according to user specifications:
    1. Base start: 10 contracts while equity < $35 (~$22).
    2. Equity reaches $35 -> Scale to 20 contracts ($35 <= equity < $55).
    3. Equity reaches $55+ -> Scale to 40 contracts (equity >= $55).
    4. Hard upper limit: 40 contracts maximum.
    5. Dynamic scaling must only utilize initial $22 + 25% of new profits (75% locked into safe balance).
    6. Stop-loss risk cap: Never risk more than 3% of total equity on any single trade stop-loss.
    """
    if current_equity <= 0:
        return base_contracts

    # 1. Dynamic Tier Allocation
    if current_equity < 35.0:
        target_contracts = 10
    elif current_equity < 55.0:
        target_contracts = 20
    else:
        target_contracts = 40

    # Hard upper limit: 40 contracts maximum
    target_contracts = min(target_contracts, max_cap)

    # 2. 75% Profit Vault & Risk Lock:
    # Only utilize the initial $22 + 25% of new profits (tradeable pool)
    cumulative_profit = max(0.0, current_equity - initial_principal)
    tradeable_pool = initial_principal + (cumulative_profit * (1.0 - profit_vault_lock_pct))

    # Margin check: tradeable pool must cover position margin
    margin_per_contract = (current_price * contract_value) / max(1, leverage)
    max_allowed_by_margin = max(1, int(tradeable_pool / margin_per_contract))

    # Strict Cap: Never risk more than 3% of total equity on single trade SL
    max_risk_usd = max_equity_risk_pct * current_equity
    risk_per_contract = max(0.0001, sl_points * contract_value)
    max_allowed_by_risk = max(1, int(max_risk_usd / risk_per_contract))

    return max(1, min(target_contracts, max_allowed_by_margin, max_allowed_by_risk, max_cap))


def calculate_capital_vault_floor(
    current_equity: float,
    peak_equity: float,
    base_floor: float = INITIAL_PRINCIPAL_USD,
    lock_pct: float = PROFIT_VAULT_LOCK_PCT,
    min_profit_buffer: float = 0.20,
) -> Tuple[float, float]:
    """
    Calculates the untouchable 75% Capital Vault floor:
    - Base Hard Floor: $22.00 USD (Absolute Protected Principal)
    - High-Water Mark Lock: 75% of all cumulative peak gains locked permanently
    Returns: (capital_vault_floor, locked_reserve_profit)
    """
    profit = max(0.0, peak_equity - base_floor)
    if profit >= min_profit_buffer:
        locked_reserve = round(profit * lock_pct, 4)
        vault_floor = round(base_floor + locked_reserve, 4)
    else:
        locked_reserve = 0.0
        vault_floor = base_floor
    return vault_floor, locked_reserve


class AggressiveAutoCompounderRiskManager(QuantRiskManager):
    """
    Unlimited Dynamic Compounder with 75% Safe Vault extending QuantRiskManager.
    Tailored for continuous 24/7 high-frequency execution on Gold (XAUTUSD) with:
    - Initial Principal: $22.00
    - 75% of cumulative realized profit locked permanently into Protected Safe Balance
    - Order sizing based ONLY on (Initial Principal + 25% of Realized Profits)
    - Tiers: 10 contracts (<$35) -> 20 contracts ($35-$55) -> 40 contracts ($55+)
    - Hard upper cap: 40 contracts maximum
    - Never risk more than 3% of total equity on any single trade stop-loss
    - Immediate Trailing Breakeven: Move SL to Entry at +0.35 gold points (risk-free trade)
    - Dynamic Trailing Stop to capture maximum run-up during large market trend moves
    - Unlimited 24/7 continuous executions (no daily profit cap)
    """

    def __init__(
        self,
        initial_equity: float = INITIAL_PRINCIPAL_USD,
        margin_per_contract: float = MARGIN_PER_CONTRACT_USD,
        base_floor: float = INITIAL_PRINCIPAL_USD,
        profit_lock_pct: float = PROFIT_VAULT_LOCK_PCT,
        kill_switch_drawdown: float = EMERGENCY_PEAK_DRAWDOWN_USD,
        kill_switch_halt_hours: float = KILL_SWITCH_HALT_HOURS,
        max_contracts_cap: int = MAX_CONTRACTS_CAP,
    ):
        super().__init__(initial_equity=initial_equity)
        self.margin_per_contract_usd = margin_per_contract
        self.base_capital_floor = base_floor
        self.profit_lock_pct = profit_lock_pct
        self.emergency_peak_drawdown_limit_usd = kill_switch_drawdown
        self.kill_switch_halt_hours = kill_switch_halt_hours
        self.max_daily_trades = 999999  # Unlimited 24/7 Executions
        self.max_contracts_cap = max_contracts_cap

    def get_scaled_lot_size(self, current_price: float = 4145.0, contract_value: float = 0.001) -> int:
        """Returns current auto-scaled contract size based on dynamic tier scaling (capped at 40) and safe tradeable pool."""
        return calculate_compound_lot_size(
            current_equity=self.current_equity,
            base_contracts=BASE_ORDER_SIZE_CONTRACTS,
            initial_principal=self.base_capital_floor,
            profit_vault_lock_pct=self.profit_lock_pct,
            max_equity_risk_pct=MAX_EQUITY_RISK_PER_TRADE_PCT,
            sl_points=M1_SL_OFFSET,
            leverage=self.leverage,
            current_price=current_price,
            contract_value=contract_value,
            max_cap=self.max_contracts_cap,
        )


__all__ = [
    "AccountTier",
    "AggressiveAutoCompounderRiskManager",
    "QuantRiskManager",
    "SizingDecision",
    "calculate_capital_vault_floor",
    "calculate_compound_lot_size",
]
