"""
Unit tests for Technical Indicators Suite (quant_robot/indicators.py)
"""

import numpy as np
import pandas as pd
import pytest

from quant_robot.indicators import (
    compute_adx,
    compute_atr,
    compute_bollinger_bands,
    compute_ema,
    compute_macd,
    compute_obv,
    compute_quant_indicators,
    compute_rsi,
    compute_supertrend,
    compute_vwap,
)


@pytest.fixture
def sample_ohlcv_df():
    """Generates synthetic price data for testing indicators."""
    np.random.seed(42)
    n = 150
    base_price = 4000.0
    returns = np.random.normal(0.0005, 0.005, n)
    prices = base_price * np.exp(np.cumsum(returns))

    highs = prices * (1 + np.abs(np.random.normal(0.002, 0.001, n)))
    lows = prices * (1 - np.abs(np.random.normal(0.002, 0.001, n)))
    opens = prices * (1 + np.random.normal(0, 0.001, n))
    closes = prices
    volumes = np.random.randint(100, 5000, n).astype(float)
    times = pd.date_range("2026-10-14 00:00:00", periods=n, freq="1min")

    df = pd.DataFrame({
        "time": times.astype(int) // 10**9,
        "datetime": times,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })
    return df


def test_compute_ema(sample_ohlcv_df):
    ema20 = compute_ema(sample_ohlcv_df["close"], 20)
    ema50 = compute_ema(sample_ohlcv_df["close"], 50)
    ema200 = compute_ema(sample_ohlcv_df["close"], 200)

    assert len(ema20) == len(sample_ohlcv_df)
    assert not ema20.isna().all()
    assert abs(ema20.iloc[-1] - sample_ohlcv_df["close"].iloc[-1]) < 200.0


def test_compute_supertrend(sample_ohlcv_df):
    st_val, st_dir = compute_supertrend(sample_ohlcv_df, period=10, multiplier=3.0)
    assert len(st_val) == len(sample_ohlcv_df)
    assert set(st_dir.unique()).issubset({-1, 1})


def test_compute_rsi(sample_ohlcv_df):
    rsi = compute_rsi(sample_ohlcv_df["close"], period=14)
    valid_rsi = rsi.dropna()
    assert len(valid_rsi) > 0
    assert (valid_rsi >= 0.0).all()
    assert (valid_rsi <= 100.0).all()


def test_compute_macd(sample_ohlcv_df):
    macd_line, sig_line, hist = compute_macd(sample_ohlcv_df["close"])
    assert len(macd_line) == len(sample_ohlcv_df)
    assert not hist.isna().all()


def test_compute_atr(sample_ohlcv_df):
    atr = compute_atr(sample_ohlcv_df["high"], sample_ohlcv_df["low"], sample_ohlcv_df["close"], period=14)
    valid_atr = atr.dropna()
    assert len(valid_atr) > 0
    assert (valid_atr > 0.0).all()


def test_compute_bollinger_bands(sample_ohlcv_df):
    upper, mid, lower, bandwidth = compute_bollinger_bands(sample_ohlcv_df["close"], 20, 2.0)
    valid_mask = ~upper.isna()
    assert (upper[valid_mask] >= mid[valid_mask]).all()
    assert (mid[valid_mask] >= lower[valid_mask]).all()
    assert (bandwidth[valid_mask] >= 0.0).all()


def test_compute_vwap(sample_ohlcv_df):
    vwap = compute_vwap(sample_ohlcv_df)
    assert len(vwap) == len(sample_ohlcv_df)
    assert not vwap.isna().all()


def test_compute_adx(sample_ohlcv_df):
    adx, plus_di, minus_di = compute_adx(sample_ohlcv_df["high"], sample_ohlcv_df["low"], sample_ohlcv_df["close"], period=14)
    valid_adx = adx.dropna()
    assert len(valid_adx) > 0
    assert (valid_adx >= 0.0).all()
    assert (valid_adx <= 100.0).all()


def test_compute_quant_indicators(sample_ohlcv_df):
    df_ind = compute_quant_indicators(sample_ohlcv_df)
    expected_cols = [
        "ema_20", "ema_50", "ema_200",
        "supertrend", "supertrend_dir",
        "rsi_14", "macd", "macd_signal", "macd_hist",
        "atr_14", "bb_upper", "bb_middle", "bb_lower", "bb_bandwidth",
        "vwap", "adx_14", "obv",
    ]
    for col in expected_cols:
        assert col in df_ind.columns, f"Missing indicator column: {col}"
