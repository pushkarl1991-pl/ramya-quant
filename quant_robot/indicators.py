"""
RAMYA QUANT ROBOT — Complete Indicator Suite
Implements:
1. Trend: EMA (20, 50, 200), Supertrend (10, 3)
2. Momentum: RSI (14) with EMA smoothing, MACD (12, 26, 9)
3. Volatility & Risk: Average True Range (ATR 14), Bollinger Bands (20, 2)
4. Volume: Volume Weighted Average Price (VWAP), On-Balance Volume (OBV)
"""

import numpy as np
import pandas as pd
from typing import Tuple


def calculate_ema(series: pd.Series, span: int) -> pd.Series:
    """Calculates Exponential Moving Average (EMA)."""
    return series.ewm(span=span, adjust=False).mean()


def calculate_supertrend(
    df: pd.DataFrame, period: int = 10, multiplier: float = 3.0
) -> Tuple[pd.Series, pd.Series]:
    """
    Calculates Supertrend Indicator (Trend direction and Trailing Stop line).
    Returns: (supertrend_line, trend_direction) where direction is +1 (Bullish) or -1 (Bearish).
    """
    high = df["high"]
    low = df["low"]
    close = df["close"]

    # Calculate ATR for Supertrend
    atr = calculate_atr(high, low, close, period)

    hl2 = (high + low) / 2.0
    basic_upper = hl2 + (multiplier * atr)
    basic_lower = hl2 - (multiplier * atr)

    final_upper = pd.Series(index=df.index, dtype=float)
    final_lower = pd.Series(index=df.index, dtype=float)
    supertrend = pd.Series(index=df.index, dtype=float)
    direction = pd.Series(index=df.index, dtype=int)

    n = len(df)
    if n == 0:
        return supertrend, direction

    final_upper.iloc[0] = basic_upper.iloc[0]
    final_lower.iloc[0] = basic_lower.iloc[0]
    direction.iloc[0] = 1
    supertrend.iloc[0] = final_lower.iloc[0]

    for i in range(1, n):
        # Upper band logic
        if basic_upper.iloc[i] < final_upper.iloc[i - 1] or close.iloc[i - 1] > final_upper.iloc[i - 1]:
            final_upper.iloc[i] = basic_upper.iloc[i]
        else:
            final_upper.iloc[i] = final_upper.iloc[i - 1]

        # Lower band logic
        if basic_lower.iloc[i] > final_lower.iloc[i - 1] or close.iloc[i - 1] < final_lower.iloc[i - 1]:
            final_lower.iloc[i] = basic_lower.iloc[i]
        else:
            final_lower.iloc[i] = final_lower.iloc[i - 1]

        # Trend direction evaluation
        prev_dir = direction.iloc[i - 1]
        if prev_dir == 1:
            if close.iloc[i] < final_lower.iloc[i]:
                direction.iloc[i] = -1
                supertrend.iloc[i] = final_upper.iloc[i]
            else:
                direction.iloc[i] = 1
                supertrend.iloc[i] = final_lower.iloc[i]
        else:
            if close.iloc[i] > final_upper.iloc[i]:
                direction.iloc[i] = 1
                supertrend.iloc[i] = final_lower.iloc[i]
            else:
                direction.iloc[i] = -1
                supertrend.iloc[i] = final_upper.iloc[i]

    return supertrend, direction


def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Calculates Relative Strength Index (RSI) using Wilder's EMA smoothing."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.bfill().fillna(50.0)


def calculate_macd(
    series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Calculates MACD Line, Signal Line, and Histogram."""
    ema_fast = calculate_ema(series, fast)
    ema_slow = calculate_ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = calculate_ema(macd_line, signal)
    macd_hist = macd_line - signal_line
    return macd_line, signal_line, macd_hist


def calculate_atr(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.Series:
    """Calculates Average True Range (ATR)."""
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()

    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = true_range.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    return atr.bfill().fillna(0.0)


def calculate_bollinger_bands(
    series: pd.Series, period: int = 20, std_dev: float = 2.0
) -> Tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """
    Calculates Bollinger Bands:
    Returns: (upper_band, middle_band, lower_band, bandwidth)
    bandwidth = (upper - lower) / middle
    """
    middle = series.rolling(window=period, min_periods=1).mean()
    std = series.rolling(window=period, min_periods=1).std().fillna(0.0)
    upper = middle + (std_dev * std)
    lower = middle - (std_dev * std)
    bandwidth = (upper - lower) / middle.replace(0, np.nan)
    return upper, middle, lower, bandwidth.fillna(0.0)


def calculate_vwap(df: pd.DataFrame) -> pd.Series:
    """
    Calculates Volume Weighted Average Price (VWAP).
    VWAP = Cumulative(Typical_Price * Volume) / Cumulative(Volume)
    Typical_Price = (High + Low + Close) / 3
    """
    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    volume = df["volume"]
    cum_vol_price = (typical_price * volume).cumsum()
    cum_volume = volume.cumsum().replace(0, np.nan)
    vwap = cum_vol_price / cum_volume
    return vwap.bfill().fillna(typical_price)


def calculate_obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """Calculates On-Balance Volume (OBV)."""
    price_change = close.diff()
    direction = np.where(price_change > 0, 1, np.where(price_change < 0, -1, 0))
    direction[0] = 0
    obv = (volume * direction).cumsum()
    return pd.Series(obv, index=close.index)


def calculate_adx(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Calculates ADX with +DI and -DI."""
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=high.index)

    atr = calculate_atr(high, low, close, period)
    smoothed_plus_dm = plus_dm.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    smoothed_minus_dm = minus_dm.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    plus_di = 100.0 * (smoothed_plus_dm / atr.replace(0, np.nan))
    minus_di = 100.0 * (smoothed_minus_dm / atr.replace(0, np.nan))

    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    return adx.bfill().fillna(0.0), plus_di.bfill().fillna(0.0), minus_di.bfill().fillna(0.0)


def compute_quant_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Computes and attaches the full quantitative indicator suite to the DataFrame:
    - EMA 20, 50, 200
    - Supertrend line & direction
    - RSI 14
    - MACD (Line, Signal, Hist)
    - ATR 14 & ATR Standard Deviation
    - Bollinger Bands (Upper, Middle, Lower, Bandwidth)
    - VWAP
    - OBV
    - ADX (+DI, -DI)
    """
    df = df.copy()
    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = df[col].astype(float)

    # Trend Indicators
    df["ema_20"] = calculate_ema(df["close"], 20)
    df["ema_50"] = calculate_ema(df["close"], 50)
    df["ema_200"] = calculate_ema(df["close"], 200)
    st_line, st_dir = calculate_supertrend(df, period=10, multiplier=3.0)
    df["supertrend"] = st_line
    df["supertrend_dir"] = st_dir

    # Momentum Indicators
    df["rsi_7"] = calculate_rsi(df["close"], 7)
    df["rsi_14"] = calculate_rsi(df["close"], 14)
    macd, macd_sig, macd_hist = calculate_macd(df["close"], fast=12, slow=26, signal=9)
    df["macd"] = macd
    df["macd_signal"] = macd_sig
    df["macd_hist"] = macd_hist

    # Candle Wick Geometry for Reversal / Pin Bar Detection
    candle_range = (df["high"] - df["low"]).replace(0, 1e-6)
    candle_body = (df["close"] - df["open"]).abs()
    upper_wick = df["high"] - df[["open", "close"]].max(axis=1)
    lower_wick = df[["open", "close"]].min(axis=1) - df["low"]
    df["candle_body"] = candle_body
    df["upper_wick"] = upper_wick
    df["lower_wick"] = lower_wick
    df["upper_wick_ratio"] = (upper_wick / candle_range).fillna(0.0)
    df["lower_wick_ratio"] = (lower_wick / candle_range).fillna(0.0)

    # Volatility Indicators
    df["atr_14"] = calculate_atr(df["high"], df["low"], df["close"], 14)
    df["atr_std_50"] = df["atr_14"].rolling(window=50, min_periods=10).std().bfill().fillna(1.0)
    bb_upper, bb_mid, bb_lower, bb_width = calculate_bollinger_bands(df["close"], 20, 2.0)
    df["bb_upper"] = bb_upper
    df["bb_middle"] = bb_mid
    df["bb_lower"] = bb_lower
    df["bb_bandwidth"] = bb_width

    # Volume Indicators
    df["vwap"] = calculate_vwap(df)
    df["obv"] = calculate_obv(df["close"], df["volume"])
    df["vol_ma_20"] = df["volume"].rolling(window=20, min_periods=1).mean()
    df["vol_ratio"] = (df["volume"] / df["vol_ma_20"].replace(0, np.nan)).fillna(1.0)

    # ADX
    adx, plus_di, minus_di = calculate_adx(df["high"], df["low"], df["close"], 14)
    df["adx_14"] = adx
    df["plus_di"] = plus_di
    df["minus_di"] = minus_di

    # Williams Alligator
    jaw, teeth, lips, alligator_state = calculate_williams_alligator(df)
    df["alligator_jaw"] = jaw
    df["alligator_teeth"] = teeth
    df["alligator_lips"] = lips
    df["alligator_state"] = alligator_state

    # Swing Breakout Levels
    swing_hi, swing_lo = calculate_swing_levels(df, lookback=20)
    df["swing_high_20"] = swing_hi
    df["swing_low_20"] = swing_lo

    return df


def calculate_williams_alligator(
    df: pd.DataFrame,
    jaw_period: int = 13,
    jaw_offset: int = 8,
    teeth_period: int = 8,
    teeth_offset: int = 5,
    lips_period: int = 5,
    lips_offset: int = 3,
) -> Tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """
    Calculates Bill Williams Alligator indicator:
    Returns: (jaw, teeth, lips, alligator_state)
    alligator_state: 'BULLISH', 'BEARISH', or 'ENTANGLED_CHOP'
    """
    median_price = (df["high"] + df["low"]) / 2.0
    jaw_smma = median_price.ewm(alpha=1.0 / jaw_period, adjust=False).mean()
    teeth_smma = median_price.ewm(alpha=1.0 / teeth_period, adjust=False).mean()
    lips_smma = median_price.ewm(alpha=1.0 / lips_period, adjust=False).mean()

    jaw = jaw_smma.shift(jaw_offset).bfill()
    teeth = teeth_smma.shift(teeth_offset).bfill()
    lips = lips_smma.shift(lips_offset).bfill()

    spread = (lips - jaw).abs() / teeth.replace(0, np.nan)
    is_entangled = spread < 0.0006

    bullish_cond = (lips > teeth) & (teeth > jaw) & (~is_entangled)
    bearish_cond = (lips < teeth) & (teeth < jaw) & (~is_entangled)

    states = pd.Series("ENTANGLED_CHOP", index=df.index)
    states[bullish_cond] = "BULLISH"
    states[bearish_cond] = "BEARISH"

    return jaw, teeth, lips, states


def calculate_swing_levels(df: pd.DataFrame, lookback: int = 20) -> Tuple[pd.Series, pd.Series]:
    """Calculates swing highs and lows strictly from past completed bars."""
    swing_high = df["high"].rolling(window=lookback, min_periods=5).max().shift(1)
    swing_low = df["low"].rolling(window=lookback, min_periods=5).min().shift(1)
    return swing_high.bfill(), swing_low.bfill()


# Aliases for flexible import conventions
compute_ema = calculate_ema
compute_supertrend = calculate_supertrend
compute_rsi = calculate_rsi
compute_macd = calculate_macd
compute_atr = calculate_atr
compute_bollinger_bands = calculate_bollinger_bands
compute_vwap = calculate_vwap
compute_obv = calculate_obv
compute_adx = calculate_adx
compute_williams_alligator = calculate_williams_alligator
compute_swing_levels = calculate_swing_levels


def calculate_zlema(series: pd.Series, period: int = 21) -> pd.Series:
    """
    Calculates Zero-Lag Exponential Moving Average (ZLEMA).
    Used by top quantitative funds to eliminate the lag of standard moving averages:
    ZLEMA = EMA(Price + (Price - Price[Lag]), Period) where Lag = (Period - 1) / 2
    """
    if len(series) == 0:
        return series
    lag = max(1, int((period - 1) / 2))
    shifted = series.shift(lag).bfill()
    adjusted_series = series + (series - shifted)
    return adjusted_series.ewm(span=period, adjust=False).mean()


def calculate_vwap_bands(
    df: pd.DataFrame, mult: float = 1.5
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    Calculates VWAP and Volatility Standard Deviation Bands:
    Returns (vwap, upper_band, lower_band).
    """
    vwap = calculate_vwap(df)
    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["volume"].replace(0, np.nan).fillna(1.0)
    dev = (typical_price - vwap) ** 2
    variance = (dev * vol).cumsum() / vol.cumsum()
    std = np.sqrt(variance.clip(lower=0)).fillna(0.0)
    upper = vwap + (std * mult)
    lower = vwap - (std * mult)
    return vwap, upper, lower


compute_zlema = calculate_zlema
compute_vwap_bands = calculate_vwap_bands
