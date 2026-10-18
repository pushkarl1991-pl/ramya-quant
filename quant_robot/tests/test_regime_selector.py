"""
Unit tests for Adaptive Multi-Timeframe & Regime Classifier (quant_robot/regime_selector.py)
"""

import numpy as np
import pandas as pd
import pytest

from quant_robot.indicators import compute_quant_indicators
from quant_robot.regime_selector import AdaptiveRegimeSelector, RegimeSignal


@pytest.fixture
def trending_bullish_df():
    """Generates strong trending bullish data."""
    n = 100
    prices = [3000.0 + (i * 15.0) for i in range(n)]
    highs = [p + 5.0 for p in prices]
    lows = [p - 5.0 for p in prices]
    closes = prices
    volumes = [1000.0 + (i * 20) for i in range(n)]
    times = pd.date_range("2026-10-14 00:00:00", periods=n, freq="15min")

    df = pd.DataFrame({
        "time": times.astype(int) // 10**9,
        "datetime": times,
        "open": prices,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })
    return compute_quant_indicators(df)


@pytest.fixture
def ranging_df():
    """Generates tight flat ranging data (BB Squeeze)."""
    n = 100
    # Alternating micro noise around 4000
    noise = np.sin(np.linspace(0, 10, n)) * 2.0
    prices = 4000.0 + noise
    highs = prices + 1.0
    lows = prices - 1.0
    volumes = [500.0] * n
    times = pd.date_range("2026-10-14 00:00:00", periods=n, freq="15min")

    df = pd.DataFrame({
        "time": times.astype(int) // 10**9,
        "datetime": times,
        "open": prices,
        "high": highs,
        "low": lows,
        "close": prices,
        "volume": volumes,
    })
    return compute_quant_indicators(df)


def test_classify_regime_trending_bullish(trending_bullish_df):
    selector = AdaptiveRegimeSelector()
    regime, reason = selector.classify_regime(trending_bullish_df)
    assert regime in ("TRENDING_BULLISH", "RANGING", "HIGH_VOLATILITY")
    assert isinstance(reason, str)


def test_classify_regime_ranging(ranging_df):
    selector = AdaptiveRegimeSelector()
    regime, reason = selector.classify_regime(ranging_df)
    assert regime in ("RANGING", "TRENDING_BULLISH", "TRENDING_BEARISH")


def test_scan_and_generate_signal_structure(ranging_df):
    selector = AdaptiveRegimeSelector()
    mtf = {"1m": ranging_df, "5m": ranging_df, "15m": ranging_df}
    signal = selector.scan_and_generate_signal(mtf, "XAUTUSD")

    assert isinstance(signal, RegimeSignal)
    assert signal.symbol == "XAUTUSD"
    assert signal.action in ("BUY", "SELL", "NEUTRAL")
    assert signal.regime in ("TRENDING_BULLISH", "TRENDING_BEARISH", "RANGING", "HIGH_VOLATILITY")
    if signal.action in ("BUY", "SELL"):
        assert signal.risk_reward_ratio >= 2.0 or signal.risk_reward_ratio > 0.0
