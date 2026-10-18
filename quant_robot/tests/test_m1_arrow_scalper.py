"""
Unit tests for High-Frequency M1 Scalper Strategy (Hard Cap: 200 Trades/Day)
"""

import numpy as np
import pandas as pd
import pytest

from quant_robot.indicators import compute_quant_indicators
from quant_robot.regime_selector import AdaptiveRegimeSelector, RegimeSignal
from quant_robot.risk_manager import QuantRiskManager, SizingDecision


def create_base_m1_df(n: int = 20, base_price: float = 4140.0) -> pd.DataFrame:
    """Creates a base 1-minute OHLCV DataFrame."""
    times = pd.date_range("2026-10-15 00:00:00", periods=n, freq="1min")
    prices = [base_price + np.sin(i * 0.3) * 1.5 for i in range(n)]
    df = pd.DataFrame({
        "time": times.astype(int) // 10**9,
        "datetime": times,
        "open": [p - 0.10 for p in prices],
        "high": [p + 0.50 for p in prices],
        "low": [p - 0.50 for p in prices],
        "close": prices,
        "volume": [100.0] * n,
    })
    return df


def test_m1_green_arrow_buy_trigger_relaxed_rsi():
    """
    Tests Green Arrow Buy Trigger:
    - 1-minute candle makes a local low (support test with lower wick rejection)
    - Fast RSI (7-period) pulls back under 42 and hooks back up
    - Expected: BUY signal with Hard SL -$0.20, TP1 +$0.20, TP2 +$0.35 (R:R 1.75:1)
    """
    df = create_base_m1_df(25, base_price=4140.0)
    df = compute_quant_indicators(df)

    # 1. Lower wick rejection: low drops to local support 4136.0, close bounces to 4138.5, open 4137.5
    df.loc[df.index[-1], "open"] = 4137.5
    df.loc[df.index[-1], "high"] = 4139.0
    df.loc[df.index[-1], "low"] = 4136.0
    df.loc[df.index[-1], "close"] = 4138.5

    # 2. Fast RSI(7) hooked up from < 42
    df.loc[df.index[-3], "rsi_7"] = 36.0
    df.loc[df.index[-2], "rsi_7"] = 39.0
    df.loc[df.index[-1], "rsi_7"] = 41.5  # Hooking up

    selector = AdaptiveRegimeSelector()
    signal = selector.detect_m1_arrow_signal(df, "XAUTUSD")

    assert signal is not None, "Green Arrow signal should have triggered with RSI < 42 hook"
    assert signal.action == "BUY"
    assert signal.regime == "M1_ARROW_REVERSAL"
    assert signal.strategy_type == "M1_SWING_ARROW_SCALP"
    assert signal is not None, "Green Arrow signal should have triggered with RSI < 42 hook"
    assert signal.action == "BUY"
    assert signal.regime == "M1_ARROW_REVERSAL"
    assert signal.strategy_type == "M1_SWING_ARROW_SCALP"
    assert signal.stop_loss == pytest.approx(4138.5 - 1.00, abs=0.01)
    assert signal.take_profit_1 == pytest.approx(4138.5 + 1.20, abs=0.01)
    assert signal.take_profit_2 == pytest.approx(4138.5 + 3.00, abs=0.01)
    assert signal.risk_reward_ratio == pytest.approx(3.00, abs=0.05)
    assert "GREEN ARROW BUY" in signal.reason


def test_m1_red_arrow_sell_trigger_relaxed_rsi():
    """
    Tests Red Arrow Sell Trigger:
    - 1-minute candle makes a local high (resistance test with upper wick rejection)
    - Fast RSI (7-period) pushes above 58 and hooks down
    - Expected: SELL signal with Hard SL -$1.00, TP1 -$1.20, TP2 -$3.00 (R:R 3.0:1)
    """
    df = create_base_m1_df(25, base_price=4140.0)
    df = compute_quant_indicators(df)

    # 1. Upper wick rejection: high spikes to local resistance 4144.0, close drops to 4141.5, open 4142.5
    df.loc[df.index[-1], "open"] = 4142.5
    df.loc[df.index[-1], "high"] = 4144.0
    df.loc[df.index[-1], "low"] = 4141.0
    df.loc[df.index[-1], "close"] = 4141.5

    # 2. Fast RSI(7) hooked down from > 58
    df.loc[df.index[-3], "rsi_7"] = 62.0
    df.loc[df.index[-2], "rsi_7"] = 60.0
    df.loc[df.index[-1], "rsi_7"] = 57.5  # Hooking down

    selector = AdaptiveRegimeSelector()
    signal = selector.detect_m1_arrow_signal(df, "XAUTUSD")

    assert signal is not None, "Red Arrow signal should have triggered with RSI > 58 hook"
    assert signal.action == "SELL"
    assert signal.regime == "M1_ARROW_REVERSAL"
    assert signal.strategy_type == "M1_SWING_ARROW_SCALP"
    assert signal.stop_loss == pytest.approx(4141.5 + 1.00, abs=0.01)
    assert signal.take_profit_1 == pytest.approx(4141.5 - 1.20, abs=0.01)
    assert signal.take_profit_2 == pytest.approx(4141.5 - 3.00, abs=0.01)
    assert signal.risk_reward_ratio == pytest.approx(3.00, abs=0.05)
    assert "RED ARROW SELL" in signal.reason


def test_m1_arrow_scalp_position_sizing_and_risk():
    """
    Tests risk manager position sizing for M1 Scalping on Gold ($22.97 capital):
    - Base position size: 10 contracts (strictly <= 20% margin ceiling)
    - Target: +$3.00, Stop-Loss: -$1.00
    - Price R:R = 3.00:1
    """
    rm = QuantRiskManager(initial_equity=22.97)
    sizing = rm.calculate_position_size(
        symbol="XAUTUSD",
        entry_price=4140.0,
        stop_loss=4139.00,      # $1.00 SL
        take_profit_2=4143.00,   # $3.00 TP
        strategy_type="M1_SWING_ARROW_SCALP",
    )

    assert sizing.is_approved is True
    assert sizing.contracts == 10
    assert sizing.coin_units == 0.010
    assert sizing.stop_loss == 4139.00
    assert sizing.take_profit_1 == 4141.20
    assert sizing.take_profit_2 == 4143.00
    assert sizing.risk_reward == pytest.approx(3.00, abs=0.05)
    assert sizing.risk_pct < 0.25


def test_daily_max_200_trades_hard_cap():
    """
    Tests that if a finite trade cap (e.g. 200 trades) is explicitly set,
    once daily_trade_count reaches 200, all new orders must be rejected.
    """
    rm = QuantRiskManager(initial_equity=22.98)
    rm.max_daily_trades = 200
    rm.daily_trade_count = 199

    # Trade 200 should still be allowed
    sizing1 = rm.calculate_position_size(
        symbol="XAUTUSD",
        entry_price=4140.0,
        stop_loss=4139.84,
        take_profit_2=4140.25,
        strategy_type="M1_SWING_ARROW_SCALP",
    )
    assert sizing1.is_approved is True

    # Simulate hitting 200 trades
    rm.daily_trade_count = 200

    # Trade 201 must be REJECTED
    sizing2 = rm.calculate_position_size(
        symbol="XAUTUSD",
        entry_price=4140.0,
        stop_loss=4139.84,
        take_profit_2=4140.25,
        strategy_type="M1_SWING_ARROW_SCALP",
    )
    assert sizing2.is_approved is False
    assert "Daily maximum trades limit" in sizing2.reason


def test_capital_protection_circuit_breaker_minus_3_usd():
    """
    Tests Capital Protection Circuit Breaker:
    - Max daily net loss: -$3.00 USD
    - When daily net loss reaches or exceeds -$3.00, trading is halted.
    """
    rm = QuantRiskManager(initial_equity=22.98)
    rm.daily_realized_net_pnl = -2.50
    rm.daily_unrealized_pnl = 0.0

    tripped, reason = rm.is_circuit_breaker_tripped()
    assert tripped is False

    # Drawdown breaches -$3.00 USD
    rm.daily_realized_net_pnl = -3.05
    tripped, reason = rm.is_circuit_breaker_tripped()
    assert tripped is True
    assert "Capital Protection Circuit Breaker" in reason
    assert "-$3.00" in reason


def test_pure_m1_buy_trigger_lower_wick_alone():
    """Verifies BUY triggers immediately on lower wick bounce even if RSI has no hook."""
    df = create_base_m1_df(25, base_price=4140.0)
    df = compute_quant_indicators(df)
    # Neutral RSI around 50
    df["rsi_7"] = 50.0

    # Strong lower wick rejection on latest candle
    df.loc[df.index[-1], "open"] = 4138.0
    df.loc[df.index[-1], "high"] = 4138.5
    df.loc[df.index[-1], "low"] = 4136.0   # lower wick = 2.0
    df.loc[df.index[-1], "close"] = 4138.2

    selector = AdaptiveRegimeSelector()
    signal = selector.detect_m1_arrow_signal(df, "XAUTUSD")
    assert signal is not None
    assert signal.action == "BUY"
    assert "GREEN ARROW BUY" in signal.reason
    assert signal.stop_loss == pytest.approx(4138.2 - 1.00, abs=0.01)
    assert signal.take_profit_2 == pytest.approx(4138.2 + 3.00, abs=0.01)


def test_pure_m1_buy_trigger_rsi_above_35_alone():
    """Verifies BUY triggers immediately when RSI(7) crosses above 35 even with flat wicks."""
    df = create_base_m1_df(25, base_price=4140.0)
    df = compute_quant_indicators(df)

    # Flat candles (no wick rejection)
    df.loc[df.index[-1], "open"] = 4140.0
    df.loc[df.index[-1], "high"] = 4140.2
    df.loc[df.index[-1], "low"] = 4140.0
    df.loc[df.index[-1], "close"] = 4140.2

    # RSI(7) crosses above 35
    df.loc[df.index[-3], "rsi_7"] = 32.0
    df.loc[df.index[-2], "rsi_7"] = 34.0
    df.loc[df.index[-1], "rsi_7"] = 36.5  # Crossed above 35

    selector = AdaptiveRegimeSelector()
    signal = selector.detect_m1_arrow_signal(df, "XAUTUSD")
    assert signal is not None
    assert signal.action == "BUY"
    assert "GREEN ARROW BUY" in signal.reason


def test_pure_m1_sell_trigger_upper_wick_alone():
    """Verifies SELL triggers immediately on upper wick rejection even if RSI has no hook."""
    df = create_base_m1_df(25, base_price=4140.0)
    df = compute_quant_indicators(df)
    df["rsi_7"] = 50.0

    # Strong upper wick rejection on latest candle
    df.loc[df.index[-1], "open"] = 4142.0
    df.loc[df.index[-1], "high"] = 4144.5  # upper wick = 2.3
    df.loc[df.index[-1], "low"] = 4141.8
    df.loc[df.index[-1], "close"] = 4142.2

    selector = AdaptiveRegimeSelector()
    signal = selector.detect_m1_arrow_signal(df, "XAUTUSD")
    assert signal is not None
    assert signal.action == "SELL"
    assert "RED ARROW SELL" in signal.reason
    assert signal.stop_loss == pytest.approx(4142.2 + 1.00, abs=0.01)
    assert signal.take_profit_2 == pytest.approx(4142.2 - 3.00, abs=0.01)


def test_pure_m1_sell_trigger_rsi_below_65_alone():
    """Verifies SELL triggers immediately when RSI(7) crosses below 65 even with flat wicks."""
    df = create_base_m1_df(25, base_price=4140.0)
    df = compute_quant_indicators(df)

    # Flat candle (no wick rejection)
    df.loc[df.index[-1], "open"] = 4140.2
    df.loc[df.index[-1], "high"] = 4140.2
    df.loc[df.index[-1], "low"] = 4140.0
    df.loc[df.index[-1], "close"] = 4140.0

    # RSI(7) crosses below 65
    df.loc[df.index[-3], "rsi_7"] = 68.0
    df.loc[df.index[-2], "rsi_7"] = 66.0
    df.loc[df.index[-1], "rsi_7"] = 63.5  # Crossed below 65

    selector = AdaptiveRegimeSelector()
    signal = selector.detect_m1_arrow_signal(df, "XAUTUSD")
    assert signal is not None
    assert signal.action == "SELL"
    assert "RED ARROW SELL" in signal.reason


def test_pure_m1_bidirectional_trend_filter_bypassed():
    """Verifies that 15m trend does not block BUY or SELL signals in scan_and_generate_signal."""
    selector = AdaptiveRegimeSelector()

    # Macro 15m is strongly BEARISH
    n = 50
    macro_prices = [4200.0 - (i * 2.0) for i in range(n)]
    times = pd.date_range("2026-10-14 00:00:00", periods=n, freq="15min")
    macro_df = compute_quant_indicators(pd.DataFrame({
        "time": times.astype(int) // 10**9,
        "datetime": times,
        "open": macro_prices,
        "high": [p + 1.0 for p in macro_prices],
        "low": [p - 1.0 for p in macro_prices],
        "close": macro_prices,
        "volume": [1000.0] * n,
    }))

    # 1m forms a lower wick BUY bounce
    m1_df = create_base_m1_df(25, base_price=4100.0)
    m1_df = compute_quant_indicators(m1_df)
    m1_df.loc[m1_df.index[-1], "open"] = 4102.0
    m1_df.loc[m1_df.index[-1], "high"] = 4102.5
    m1_df.loc[m1_df.index[-1], "low"] = 4100.0  # lower wick = 2.0
    m1_df.loc[m1_df.index[-1], "close"] = 4102.2

    mtf = {"1m": m1_df, "5m": macro_df, "15m": macro_df}
    signal = selector.scan_and_generate_signal(mtf, "XAUTUSD")

    # MUST NOT be blocked by 15m Bearish trend
    assert signal.action == "BUY"
    assert "15m Trend Bypassed" in signal.reason


def test_unlimited_trade_frequency_execution():
    """Verifies that with DAILY_MAX_TRADES_LIMIT=999999, orders are never blocked by static trade count."""
    rm = QuantRiskManager(initial_equity=22.98)
    rm.daily_trade_count = 500  # Well past previous 200 cap

    sizing = rm.calculate_position_size(
        symbol="XAUTUSD",
        entry_price=4140.0,
        stop_loss=4139.84,
        take_profit_2=4140.25,
        strategy_type="M1_SWING_ARROW_SCALP",
    )
    assert sizing.is_approved is True
    assert "Unlimited Compounder" in sizing.reason


def test_position_size_auto_scaler_tiers_cap_40():
    """
    Verifies dynamic position sizing under 10/20/40 tier scaling & 40 contract cap:
    - Base: $22.97 (< $35) -> 10 contracts
    - Equity reaches $35.00 -> 20 contracts
    - Equity reaches $55.00+ -> 40 contracts
    - Equity reaches $100.00+ -> 40 contracts (hard cap 40!)
    """
    rm = QuantRiskManager(initial_equity=22.97)
    s1 = rm.calculate_position_size("XAUTUSD", 4140.0, 4139.75, 4140.35, strategy_type="M1_SWING_ARROW_SCALP")
    assert s1.contracts == 10

    rm.update_equity(35.00)
    s2 = rm.calculate_position_size("XAUTUSD", 4140.0, 4139.75, 4140.35, strategy_type="M1_SWING_ARROW_SCALP")
    assert s2.contracts == 20

    rm.update_equity(55.00)
    s3 = rm.calculate_position_size("XAUTUSD", 4140.0, 4139.75, 4140.35, strategy_type="M1_SWING_ARROW_SCALP")
    assert s3.contracts == 40

    rm.update_equity(100.00)
    s4 = rm.calculate_position_size("XAUTUSD", 4140.0, 4139.75, 4140.35, strategy_type="M1_SWING_ARROW_SCALP")
    assert s4.contracts == 40  # Hard capped at 40 contracts


def test_autonomous_capital_preservation_vault_profit_lock():
    """
    Verifies that 75% of peak net gains above $22.00 base floor are locked into an untouchable equity floor:
    - Base: $22.00
    - Peak reaches $24.00 (+ $2.00 peak profit)
    - Locked reserve: 75% of $2.00 = $1.50
    - Capital vault floor: $22.00 + $1.50 = $23.50
    - Pullback to $23.20 (< $23.50 locked floor, pullback $0.80 < $2.50 kill-switch)
    - Capital Vault Floor Breach trips!
    """
    rm = QuantRiskManager(initial_equity=22.00)
    assert rm.capital_vault_floor == 22.00

    # Equity grows to $24.00
    rm.update_equity(24.00)
    assert rm.peak_equity == 24.00
    assert rm.locked_reserve_profit == pytest.approx(1.50, 0.01)
    assert rm.capital_vault_floor == pytest.approx(23.50, 0.01)

    # Pullback to $23.20 (< $23.50 locked floor)
    rm.current_equity = 23.20
    tripped, reason = rm.is_circuit_breaker_tripped()
    assert tripped is True
    assert "Capital Vault Floor Breach" in reason


def test_drawdown_kill_switch_minus_2_50_from_session_peak():
    """
    Verifies that if equity pulls back -$2.50 from its current session peak,
    the kill-switch halts trading for 2 hours.
    """
    rm = QuantRiskManager(initial_equity=22.97)
    # Base capital floor with zero profit above base
    assert rm.peak_equity == 22.97

    # Drawdown of $1.97 (< $2.50 tolerance)
    rm.current_equity = 21.00
    tripped, _ = rm.is_circuit_breaker_tripped()
    assert tripped is False

    # Drawdown of $2.57 (>= $2.50 limit)
    rm.current_equity = 20.40
    tripped, reason = rm.is_circuit_breaker_tripped()
    assert tripped is True
    assert "Drawdown Kill-Switch" in reason
    assert "2.0h" in reason


def test_aggressive_auto_compounder_engine_scale():
    """
    Verifies the Aggressive Auto-Compounder engine scaling:
    - Base position: 10 contracts while equity < $35
    - Equity reaches $35 -> 20 contracts
    - Equity reaches $55+ -> 40 contracts
    - Hard upper cap: 40 contracts maximum
    - Unbroken 24/7 unlimited executions
    """
    from quant_robot.engine.risk_manager import (
        AggressiveAutoCompounderRiskManager,
        calculate_compound_lot_size,
    )

    assert calculate_compound_lot_size(22.97) == 10
    assert calculate_compound_lot_size(34.90) == 10
    assert calculate_compound_lot_size(35.00) == 20
    assert calculate_compound_lot_size(54.90) == 20
    assert calculate_compound_lot_size(55.00) == 40
    assert calculate_compound_lot_size(100.00) == 40  # Hard capped at 40

    ac = AggressiveAutoCompounderRiskManager(initial_equity=22.00)
    assert ac.get_scaled_lot_size() == 10

    ac.update_equity(35.00)
    assert ac.get_scaled_lot_size() == 20

    ac.update_equity(55.00)
    assert ac.get_scaled_lot_size() == 40

    ac.update_equity(100.00)
    assert ac.get_scaled_lot_size() == 40


