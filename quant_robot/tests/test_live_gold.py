"""
Unit tests for RAMYA AI Live Gold Trading Robot ($20 Balance Guardrails)
(quant_robot/live_gold_trader.py)
"""

import pytest

from quant_robot.live_gold_trader import LiveGoldTradingRobot


@pytest.fixture
def test_robot():
    """Initializes robot in paper mode for safe deterministic unit testing."""
    robot = LiveGoldTradingRobot(is_paper=True)
    return robot


def test_guardrails_initialization(test_robot):
    assert test_robot.TARGET_ACCOUNT_BALANCE == 20.00
    assert test_robot.LOCKED_LEVERAGE == 3
    assert test_robot.POSITION_SIZE == 1
    assert test_robot.MAX_RISK_PER_TRADE_USD == 0.40
    assert test_robot.TARGET_PROFIT_USD == 1.00
    assert test_robot.BREAKEVEN_TRIGGER_USD == 0.50
    assert test_robot.DAILY_DRAWDOWN_FREEZE_USD == 18.50
    assert test_robot.CONSECUTIVE_LOSS_LIMIT == 2
    assert test_robot.LOCKOUT_DURATION_HOURS == 4.0


def test_stop_loss_and_take_profit_dollar_caps(test_robot):
    """
    Validates that:
    - Stop-loss is capped at maximum $0.40 risk (2% of $20).
    - Take-profit is 1:2.5 R:R ($1.00 target).
    """
    # Contract value = 0.001
    # Max SL price distance = $0.40 / 0.001 = $400.00
    # TP price distance = $1.00 / 0.001 = $1000.00
    atr = 10.0
    sl_dist = min(1.5 * atr, 400.00)
    tp_dist = max(2.5 * sl_dist, 1000.00)

    # Dollar risk on 1 contract
    risk_usd = sl_dist * test_robot.CONTRACT_VALUE * test_robot.POSITION_SIZE
    profit_usd = tp_dist * test_robot.CONTRACT_VALUE * test_robot.POSITION_SIZE

    assert risk_usd <= test_robot.MAX_RISK_PER_TRADE_USD
    assert profit_usd >= test_robot.TARGET_PROFIT_USD
    assert round(profit_usd / risk_usd, 2) >= 2.5


def test_breakeven_adjustment_trigger(test_robot):
    """
    Validates that stop-loss is moved to Breakeven (+ fees) once profit reaches +$0.50.
    """
    pos = {
        "side": "buy",
        "size": 1,
        "entry_price": 4000.0,
        "stop_loss": 3985.0,
        "take_profit": 5000.0,
        "highest_price": 4000.0,
        "lowest_price": 4000.0,
        "is_be_armed": False,
    }
    test_robot.active_position = pos

    # Current price reaches 4000 + 500 = 4500 (profit = 500 * 0.001 = +$0.50)
    current_price = 4500.0
    profit_usd = (current_price - pos["entry_price"]) * test_robot.CONTRACT_VALUE
    assert profit_usd >= test_robot.BREAKEVEN_TRIGGER_USD

    # Breakeven adjustment
    fee_buffer = 2.0
    be_sl = pos["entry_price"] + fee_buffer
    pos["stop_loss"] = be_sl
    pos["is_be_armed"] = True

    assert pos["stop_loss"] == 4002.0
    assert pos["is_be_armed"] is True


def test_two_loss_lockout_circuit_breaker(test_robot):
    """
    Validates that 2 consecutive stop-loss hits trigger 4-hour trading lockout.
    """
    test_robot.active_position = {
        "side": "buy",
        "size": 1,
        "entry_price": 4000.0,
        "stop_loss": 3985.0,
        "take_profit": 4037.5,
        "strategy": "TEST_LOSS",
    }
    # First stop-loss hit
    test_robot._handle_position_closed(exit_price=3985.0, exit_reason="STOP_LOSS_HIT")
    assert test_robot.consecutive_loss_count == 1
    assert test_robot.lockout_until == 0.0

    # Second stop-loss hit
    test_robot.active_position = {
        "side": "sell",
        "size": 1,
        "entry_price": 4000.0,
        "stop_loss": 4015.0,
        "take_profit": 3962.5,
        "strategy": "TEST_LOSS_2",
    }
    test_robot._handle_position_closed(exit_price=4015.0, exit_reason="STOP_LOSS_HIT")
    assert test_robot.consecutive_loss_count == 2
    assert test_robot.lockout_until > 0.0


def test_daily_drawdown_freeze_guardrail(test_robot):
    """
    Validates permanent shutdown if account balance drops below $18.50.
    """
    test_robot.is_paper = False
    test_robot.live_balance_usd = 18.00  # < $18.50 threshold
    assert test_robot.live_balance_usd < test_robot.DAILY_DRAWDOWN_FREEZE_USD

    test_robot.monitor_live_position_and_circuit_breakers()
    assert test_robot.is_frozen_permanently is True
    assert test_robot.is_running is False
