"""
Unit Tests for TrendFollowingMomentumStrategy & UnifiedTradingDashboard
"""

import numpy as np
import pandas as pd
import pytest

from quant_robot.cost_and_profitability_engine import CostAndProfitabilityEngine
from quant_robot.dashboard import UnifiedTradingDashboard
from quant_robot.trend_momentum_strategy import StrategyDecision, TrendFollowingMomentumStrategy


def _create_mock_candles(trend: str = "BULLISH", n_bars: int = 60, current_price: float = 4150.0) -> pd.DataFrame:
    """Helper to generate realistic synthetic OHLCV bars with defined trend."""
    dates = pd.date_range("2026-10-15 00:00", periods=n_bars, freq="5min")
    
    if trend == "BULLISH":
        slope = 0.5
        base_prices = np.linspace(current_price - (n_bars * slope), current_price, n_bars)
    elif trend == "BEARISH":
        slope = -0.5
        base_prices = np.linspace(current_price - (n_bars * slope), current_price, n_bars)
    else:  # SIDEWAYS
        base_prices = np.full(n_bars, current_price) + np.sin(np.linspace(0, 10, n_bars)) * 1.0

    df = pd.DataFrame({
        "open": base_prices - 0.2,
        "high": base_prices + 1.5,
        "low": base_prices - 1.2,
        "close": base_prices + 0.3,
        "volume": np.random.uniform(500, 1500, n_bars),
    }, index=dates)
    df["datetime"] = df.index
    return df


def test_sideways_chop_filter_rejection():
    """Verify strategy returns NO_TRADE when ADX < 20 in sideways consolidation."""
    df_15m = _create_mock_candles("SIDEWAYS", n_bars=60, current_price=4150.0)
    df_5m = _create_mock_candles("SIDEWAYS", n_bars=60, current_price=4150.0)

    # Attach required columns so compute_quant_indicators does not overwrite
    df_15m["ema_200"] = 4150.0
    df_15m["adx_14"] = 14.5
    df_5m["ema_50"] = 4150.0
    df_5m["rsi_14"] = 50.0

    strategy = TrendFollowingMomentumStrategy(min_adx=20.0)
    mtf = {"15m": df_15m, "5m": df_5m}

    decision = strategy.evaluate("XAUTUSD", mtf, macro_tf="15m", trend_tf="5m")
    assert decision.action == "NO_TRADE"
    assert decision.macro_regime == "SIDEWAYS_CHOP"
    assert "ADX" in decision.reason or "consolidation" in decision.reason


def test_bullish_trend_pullback_approved():
    """Verify bullish pullback entry trigger with valid indicators and cost approval."""
    df_15m = _create_mock_candles("BULLISH", n_bars=60, current_price=4160.0)
    df_5m = _create_mock_candles("BULLISH", n_bars=60, current_price=4160.0)

    # Setup bullish macro indicators
    df_15m["ema_50"] = 4140.0
    df_15m["ema_200"] = 4120.0
    df_15m["plus_di"] = 28.0
    df_15m["minus_di"] = 12.0
    df_15m["adx_14"] = 28.5
    df_15m["close"] = 4160.0

    # Setup 5m pullback indicators
    df_5m["ema_20"] = 4159.0
    df_5m["ema_50"] = 4150.0
    df_5m["ema_200"] = 4130.0
    df_5m["rsi_14"] = 50.0
    df_5m["macd_hist"] = 0.15
    df_5m["atr_14"] = 3.50
    df_5m["vol_ratio"] = 1.1
    df_5m["lower_wick"] = 1.0  # Support rejection wick
    df_5m["close"] = 4160.0

    cost_engine = CostAndProfitabilityEngine(
        initial_equity=20.0,
        max_risk_per_trade_pct=0.0025,
        min_net_risk_reward=1.5,
        max_cost_consumption_pct=0.35,
        is_paper_mode=True,
    )

    strategy = TrendFollowingMomentumStrategy(cost_engine=cost_engine, min_adx=20.0)
    mtf = {"15m": df_15m, "5m": df_5m}

    decision = strategy.evaluate("XAUTUSD", mtf, macro_tf="15m", trend_tf="5m")
    assert decision.action == "BUY"
    assert decision.macro_regime == "BULLISH"
    assert decision.setup_type == "TREND_PULLBACK"
    assert decision.is_tradable is True
    assert decision.stop_loss < decision.entry_price
    assert decision.take_profit > decision.entry_price
    assert decision.gross_risk_reward >= 1.8


def test_bearish_trend_pullback_approved():
    """Verify bearish pullback entry trigger with valid indicators and cost approval."""
    df_15m = _create_mock_candles("BEARISH", n_bars=60, current_price=4130.0)
    df_5m = _create_mock_candles("BEARISH", n_bars=60, current_price=4130.0)

    # Setup bearish macro indicators
    df_15m["ema_50"] = 4150.0
    df_15m["ema_200"] = 4170.0
    df_15m["plus_di"] = 11.0
    df_15m["minus_di"] = 30.0
    df_15m["adx_14"] = 31.0
    df_15m["close"] = 4130.0

    # Setup 5m pullback indicators
    df_5m["ema_20"] = 4131.0
    df_5m["ema_50"] = 4140.0
    df_5m["ema_200"] = 4160.0
    df_5m["rsi_14"] = 48.0
    df_5m["macd_hist"] = -0.18
    df_5m["atr_14"] = 3.50
    df_5m["vol_ratio"] = 1.1
    df_5m["upper_wick"] = 1.0  # Resistance rejection wick
    df_5m["close"] = 4130.0

    cost_engine = CostAndProfitabilityEngine(
        initial_equity=20.0,
        max_risk_per_trade_pct=0.0025,
        min_net_risk_reward=1.5,
        max_cost_consumption_pct=0.35,
        is_paper_mode=True,
    )

    strategy = TrendFollowingMomentumStrategy(cost_engine=cost_engine, min_adx=20.0)
    mtf = {"15m": df_15m, "5m": df_5m}

    decision = strategy.evaluate("XAUTUSD", mtf, macro_tf="15m", trend_tf="5m")
    assert decision.action == "SELL"
    assert decision.macro_regime == "BEARISH"
    assert decision.setup_type == "TREND_PULLBACK"
    assert decision.is_tradable is True
    assert decision.stop_loss > decision.entry_price
    assert decision.take_profit < decision.entry_price


def test_overbought_rsi_rejection():
    """Verify that buying into overbought RSI (>= 70) is strictly rejected."""
    df_15m = _create_mock_candles("BULLISH", n_bars=60, current_price=4170.0)
    df_5m = _create_mock_candles("BULLISH", n_bars=60, current_price=4170.0)

    df_15m["ema_50"] = 4140.0
    df_15m["ema_200"] = 4120.0
    df_15m["plus_di"] = 30.0
    df_15m["minus_di"] = 10.0
    df_15m["adx_14"] = 28.0

    df_5m["ema_20"] = 4165.0
    df_5m["ema_50"] = 4150.0
    df_5m["ema_200"] = 4130.0
    df_5m["rsi_14"] = 74.5  # Overbought!

    strategy = TrendFollowingMomentumStrategy()
    decision = strategy.evaluate("XAUTUSD", {"15m": df_15m, "5m": df_5m})
    assert decision.action == "NO_TRADE"
    assert "overbought" in decision.reason


def test_dashboard_wallet_update_and_circuit_breaker():
    """Verify dashboard metrics, drawdown calculation, and circuit breaker trip."""
    dash = UnifiedTradingDashboard(symbol="XAUTUSD", initial_equity=20.00, max_daily_loss_pct=0.02)
    assert dash.current_equity == 20.00
    assert dash.circuit_breaker_active is False

    # Simulate balance update after a small loss to $19.50 (2.5% loss > 2.0% limit)
    dash.update_wallet(wallet_balance=19.50, available_balance=19.50)
    assert dash.current_equity == 19.50
    assert dash.daily_drawdown_pct == 2.50
    assert dash.circuit_breaker_active is True
    assert "Daily Drawdown" in dash.circuit_breaker_reason

    # Verify console output renders cleanly
    rendered = dash.render_console(mode="PAPER")
    assert "RAMYA QUANT ROBOT" in rendered
    assert "$19.50" in rendered
    assert "CIRCUIT BREAKER ENGAGED" in rendered


def test_sideways_range_lower_wick_scalp_approved():
    """Verify that lower Bollinger Band test with rejection wick triggers RANGE_REVERSAL_SCALP BUY in sideways chop."""
    df_15m = _create_mock_candles("SIDEWAYS", n_bars=60, current_price=4150.0)
    df_5m = _create_mock_candles("SIDEWAYS", n_bars=60, current_price=4145.0)

    df_15m["ema_200"] = 4150.0
    df_15m["adx_14"] = 12.0  # Sideways chop!

    df_5m["ema_50"] = 4145.0
    df_5m["bb_lower"] = 4145.0
    df_5m["bb_middle"] = 4155.0
    df_5m["bb_upper"] = 4165.0
    df_5m["rsi_14"] = 34.0
    df_5m["lower_wick_ratio"] = 0.30
    df_5m["atr_14"] = 3.00
    df_5m["vol_ratio"] = 1.1

    cost_engine = CostAndProfitabilityEngine(
        initial_equity=20.0,
        max_risk_per_trade_pct=0.015,
        min_net_risk_reward=1.4,
        max_cost_consumption_pct=0.35,
        max_leverage_cap=75,
        is_paper_mode=True,
    )

    strategy = TrendFollowingMomentumStrategy(cost_engine=cost_engine, min_adx=20.0)
    decision = strategy.evaluate("XAUTUSD", {"15m": df_15m, "5m": df_5m})

    assert decision.action == "BUY"
    assert decision.setup_type == "RANGE_REVERSAL_SCALP"
    assert decision.macro_regime == "SIDEWAYS_CHOP"
    assert decision.is_tradable is True
    assert decision.take_profit > decision.entry_price


def test_sideways_range_upper_wick_scalp_approved():
    """Verify that upper Bollinger Band test with rejection wick triggers RANGE_REVERSAL_SCALP SELL in sideways chop."""
    df_15m = _create_mock_candles("SIDEWAYS", n_bars=60, current_price=4150.0)
    df_5m = _create_mock_candles("SIDEWAYS", n_bars=60, current_price=4165.0)

    df_15m["ema_200"] = 4150.0
    df_15m["adx_14"] = 12.0  # Sideways chop!

    df_5m["ema_50"] = 4165.0
    df_5m["bb_lower"] = 4135.0
    df_5m["bb_middle"] = 4150.0
    df_5m["bb_upper"] = 4165.0
    df_5m["rsi_14"] = 66.0
    df_5m["upper_wick_ratio"] = 0.30
    df_5m["atr_14"] = 3.00
    df_5m["vol_ratio"] = 1.1

    cost_engine = CostAndProfitabilityEngine(
        initial_equity=20.0,
        max_risk_per_trade_pct=0.015,
        min_net_risk_reward=1.4,
        max_cost_consumption_pct=0.35,
        max_leverage_cap=75,
        is_paper_mode=True,
    )

    strategy = TrendFollowingMomentumStrategy(cost_engine=cost_engine, min_adx=20.0)
    decision = strategy.evaluate("XAUTUSD", {"15m": df_15m, "5m": df_5m})

    assert decision.action == "SELL"
    assert decision.setup_type == "RANGE_REVERSAL_SCALP"
    assert decision.macro_regime == "SIDEWAYS_CHOP"
    assert decision.is_tradable is True
    assert decision.take_profit < decision.entry_price


def test_expanded_profit_targets_and_crypto_support():
    """Verify that profit targets are expanded to 8.0x ATR / 3.5x RR for high reward-to-risk."""
    df_15m = _create_mock_candles("BULLISH", n_bars=60, current_price=82000.0)
    df_5m = _create_mock_candles("BULLISH", n_bars=60, current_price=82000.0)

    df_15m["ema_50"] = 81000.0
    df_15m["ema_200"] = 80000.0
    df_15m["plus_di"] = 32.0
    df_15m["minus_di"] = 10.0
    df_15m["adx_14"] = 30.0
    df_15m["close"] = 82000.0

    df_5m["ema_20"] = 81950.0
    df_5m["ema_50"] = 81500.0
    df_5m["ema_200"] = 80500.0
    df_5m["rsi_14"] = 52.0
    df_5m["macd_hist"] = 15.0
    df_5m["atr_14"] = 150.0
    df_5m["vol_ratio"] = 1.5
    df_5m["lower_wick"] = 50.0
    df_5m["close"] = 82000.0

    cost_engine = CostAndProfitabilityEngine(
        initial_equity=25.0,
        max_risk_per_trade_pct=0.025,
        min_net_risk_reward=1.35,
        max_cost_consumption_pct=0.35,
        max_leverage_cap=100,
        is_paper_mode=True,
    )

    strategy = TrendFollowingMomentumStrategy(cost_engine=cost_engine, atr_tp_mult=10.0)
    decision = strategy.evaluate("BTCUSD", {"15m": df_15m, "5m": df_5m})

    assert decision.action == "BUY"
    assert decision.is_tradable is True
    assert decision.target_distance_pts >= 1800.0  # BTC target >= 1800 pts (10x ATR)
    assert decision.gross_risk_reward >= 4.0


def test_scaled_leverage_and_target_150x():
    """Verify that leverage scales dynamically to 150x on BTC/ETH for high-momentum breakouts."""
    df_15m = _create_mock_candles("BULLISH", n_bars=60, current_price=82000.0)
    df_5m = _create_mock_candles("BULLISH", n_bars=60, current_price=82000.0)

    df_15m["ema_50"] = 81000.0
    df_15m["ema_200"] = 80000.0
    df_15m["plus_di"] = 35.0
    df_15m["minus_di"] = 8.0
    df_15m["adx_14"] = 32.0
    df_15m["close"] = 82000.0

    df_5m["ema_20"] = 81950.0
    df_5m["ema_50"] = 81500.0
    df_5m["ema_200"] = 80500.0
    df_5m["rsi_14"] = 55.0
    df_5m["macd_hist"] = 20.0
    df_5m["atr_14"] = 140.0
    df_5m["vol_ratio"] = 1.6
    df_5m["lower_wick"] = 40.0
    df_5m["close"] = 82000.0

    cost_engine = CostAndProfitabilityEngine(
        initial_equity=25.0,
        max_risk_per_trade_pct=0.03,
        min_net_risk_reward=1.35,
        max_cost_consumption_pct=0.35,
        max_leverage_cap=150,
        is_paper_mode=True,
    )

    strategy = TrendFollowingMomentumStrategy(cost_engine=cost_engine, atr_tp_mult=10.0)
    decision = strategy.evaluate("BTCUSD", {"15m": df_15m, "5m": df_5m})

    assert decision.action == "BUY"
    assert decision.is_tradable is True
    assert decision.pre_order_decision.leverage >= 100
    assert decision.target_distance_pts >= 1600.0
    assert decision.gross_risk_reward >= 4.5


def test_5s_micro_velocity_expansion_strike():
    """Verify that 5-Second Micro-Velocity strike triggers early on orderbook buyer dominance."""
    df_15m = _create_mock_candles("BULLISH", n_bars=60, current_price=2430.0)
    df_5m = _create_mock_candles("BULLISH", n_bars=60, current_price=2430.0)

    df_15m["ema_50"] = 2400.0
    df_15m["ema_200"] = 2350.0
    df_15m["plus_di"] = 30.0
    df_15m["minus_di"] = 12.0
    df_15m["adx_14"] = 28.0
    df_15m["close"] = 2430.0

    df_5m["ema_20"] = 2420.0
    df_5m["ema_50"] = 2410.0
    df_5m["ema_200"] = 2380.0
    df_5m["rsi_14"] = 52.0
    df_5m["macd_hist"] = 2.5
    df_5m["atr_14"] = 8.0
    df_5m["vol_ratio"] = 1.3
    df_5m["open"] = 2428.0
    df_5m["close"] = 2430.0

    mock_ob = {
        "buy": [{"price": "2430.0", "size": "300"}, {"price": "2429.5", "size": "250"}],
        "sell": [{"price": "2430.5", "size": "100"}, {"price": "2431.0", "size": "100"}],
    }

    cost_engine = CostAndProfitabilityEngine(
        initial_equity=20.0,
        max_risk_per_trade_pct=0.10,
        min_net_risk_reward=1.35,
        max_cost_consumption_pct=0.35,
        max_leverage_cap=200,
        is_paper_mode=True,
    )

    strategy = TrendFollowingMomentumStrategy(cost_engine=cost_engine, atr_tp_mult=10.0)
    decision = strategy.evaluate(
        symbol="ETHUSD",
        mtf_data={"15m": df_15m, "5m": df_5m},
        orderbook=mock_ob,
    )

    assert decision.action == "BUY"
    assert decision.is_tradable is True
    assert decision.setup_type == "MICRO_VELOCITY_EXPANSION_STRIKE"
    assert "5S MICRO-VELOCITY STRIKE" in decision.reason




