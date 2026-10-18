"""
Unit tests for Risk Management Engine (quant_robot/risk_manager.py)
"""

import pytest

from quant_robot.risk_manager import QuantRiskManager, SizingDecision


def test_position_sizing_gold_calculation():
    rm = QuantRiskManager(initial_equity=10000.0, risk_per_trade_pct=0.01)
    # Entry 4000, SL 3980 (dist 20), TP 4050 (dist 50 -> RR 2.5:1)
    sizing = rm.calculate_position_size(
        symbol="XAUTUSD",
        entry_price=4000.0,
        stop_loss=3980.0,
        take_profit_2=4050.0,
    )

    assert sizing.is_approved is True
    assert sizing.contracts >= 1
    # Risk USD should be ~100.0 (1% of 10000)
    assert sizing.risk_pct <= 2.0
    assert sizing.risk_reward >= 2.0


def test_mandatory_risk_reward_rejection():
    rm = QuantRiskManager(initial_equity=10000.0, min_rr=2.0)
    # Entry 4000, SL 3980 (dist 20), TP 4020 (dist 20 -> RR 1.0:1 < 2.0)
    sizing = rm.calculate_position_size(
        symbol="XAUTUSD",
        entry_price=4000.0,
        stop_loss=3980.0,
        take_profit_2=4020.0,
    )

    assert sizing.is_approved is False
    assert "Risk-to-Reward" in sizing.reason
    assert sizing.contracts == 0


def test_daily_drawdown_circuit_breaker():
    rm = QuantRiskManager(initial_equity=10000.0, daily_dd_limit_pct=0.04)
    # Record loss of $450 (> 4% of $10,000 = $400)
    rm.record_trade_exit("XAUTUSD", -450.0)

    tripped, reason = rm.is_circuit_breaker_tripped()
    assert tripped is True
    assert "Daily Drawdown Breach" in reason

    # New trade should be automatically rejected
    sizing = rm.calculate_position_size(
        symbol="BTCUSD",
        entry_price=80000.0,
        stop_loss=79000.0,
        take_profit_2=83000.0,
    )
    assert sizing.is_approved is False
    assert "HALTED: Circuit Breaker" in sizing.reason


def test_consecutive_losses_circuit_breaker():
    rm = QuantRiskManager(initial_equity=10000.0)
    # 3 consecutive losses
    rm.record_trade_exit("XAUTUSD", -20.0)
    rm.record_trade_exit("BTCUSD", -30.0)
    rm.record_trade_exit("ETHUSD", -25.0)

    tripped, reason = rm.is_circuit_breaker_tripped()
    assert tripped is True
    assert "consecutive losses" in reason


def test_trailing_stop_breakeven_and_trail():
    rm = QuantRiskManager(initial_equity=10000.0)
    pos = {
        "direction": "LONG",
        "entry_price": 4000.0,
        "stop_loss": 3980.0,
        "is_breakeven_armed": False,
    }
    atr = 10.0

    # High watermark reaches +1.5 ATR = +15 -> 4015
    new_sl, updated, reason = rm.update_trailing_stop(
        position=pos,
        current_price=4015.0,
        high_watermark=4015.0,
        low_watermark=3995.0,
        atr=atr,
    )

    assert updated is True
    assert pos["is_breakeven_armed"] is True
    # Breakeven buffer or trail level
    assert new_sl >= 4002.0

    # High watermark increases further to 4030 (+3 ATR)
    new_sl2, updated2, reason2 = rm.update_trailing_stop(
        position=pos,
        current_price=4025.0,
        high_watermark=4030.0,
        low_watermark=3995.0,
        atr=atr,
    )
    assert updated2 is True
    # 4030 - 1.0 ATR (10) = 4020
    assert new_sl2 == 4020.0


def test_percentage_risk_sizing_01pct():
    """Verifies that risk sizing enforces 0.1% initial planned risk on verified equity."""
    rm = QuantRiskManager(initial_equity=22.98, risk_per_trade_pct=0.001)
    assert rm.risk_per_trade_pct == 0.001

    # Equity = $22.98 -> 0.1% budget = $0.02298
    # For small stop distance ($3.00), 1 contract risk = 3 * 0.001 + fees ~ $0.0063 <= $0.02298
    sizing = rm.calculate_position_size(
        symbol="XAUTUSD",
        entry_price=4100.0,
        stop_loss=4097.0,
        take_profit_2=4118.0,
    )
    assert sizing.is_approved is True
    assert sizing.contracts >= 1
    assert sizing.net_risk_usd <= 22.98 * 0.001 + 0.0001
    assert sizing.risk_pct <= 0.1 + 0.01


def test_skip_trade_when_min_contract_risk_exceeds_01pct():
    """Verifies that if 1 minimum contract's risk exceeds 0.1% equity budget, the trade is skipped."""
    # Equity = $22.98 -> 0.1% budget = $0.02298
    # Stop distance: SL = $25.00 away -> 1 contract price risk = 25 * 0.001 = $0.025 + fees ~$0.0033 = $0.0283 > $0.023 budget
    rm = QuantRiskManager(initial_equity=22.98, risk_per_trade_pct=0.001)
    sizing = rm.calculate_position_size(
        symbol="XAUTUSD",
        entry_price=4100.0,
        stop_loss=4075.0,
        take_profit_2=4160.0,
    )
    assert sizing.is_approved is False
    assert sizing.contracts == 0
    assert "exceeds 0.10% equity budget" in sizing.reason
    assert "Trade skipped" in sizing.reason


def test_fee_control_insufficient_net_reward_rejection():
    """Verifies trade rejection when expected net reward after fees is insufficient or net R:R < 1.5."""
    rm = QuantRiskManager(initial_equity=1000.0, risk_per_trade_pct=0.005, min_rr=1.5)
    # SL distance = $10.0, TP distance = $12.0 (Gross RR = 1.2:1 < 1.5)
    sizing = rm.calculate_position_size(
        symbol="XAUTUSD",
        entry_price=4100.0,
        stop_loss=4090.0,
        take_profit_2=4112.0,
    )
    assert sizing.is_approved is False
    assert sizing.contracts == 0
    assert "insufficient after fees" in sizing.reason or "Risk-to-Reward" in sizing.reason


def test_separate_gross_and_net_pnl_accounting():
    """Verifies that Gross P&L and Net P&L are recorded separately."""
    rm = QuantRiskManager(initial_equity=100.0)
    # Gross +$5.00, Net +$4.50, Fees $0.50
    rm.record_trade_exit(
        symbol="XAUTUSD",
        gross_pnl=5.00,
        net_pnl=4.50,
        fees_paid=0.50,
        slippage_paid=0.00,
    )
    assert rm.daily_realized_gross_pnl == 5.00
    assert rm.daily_realized_net_pnl == 4.50
    assert rm.daily_fees_paid == 0.50
    assert rm.current_equity == 104.50


def test_anti_martingale_enforcement():
    """Verifies that losing trades decrease or safely bound subsequent position sizes, never increasing."""
    rm = QuantRiskManager(initial_equity=100.0, risk_per_trade_pct=0.005)
    initial_decision = rm.calculate_position_size("XAUTUSD", 4100.0, 4095.0, 4115.0)
    assert initial_decision.is_approved is True
    initial_contracts = initial_decision.contracts

    # Record a loss
    rm.record_trade_exit("XAUTUSD", gross_pnl=-0.30, net_pnl=-0.35, fees_paid=0.05)
    assert rm.current_equity < 100.0

    # Next trade position size MUST be <= initial size (never doubled or scaled up)
    next_decision = rm.calculate_position_size("XAUTUSD", 4100.0, 4095.0, 4115.0)
    if next_decision.is_approved:
        assert next_decision.contracts <= initial_contracts
        assert next_decision.net_risk_usd <= rm.current_equity * 0.005
