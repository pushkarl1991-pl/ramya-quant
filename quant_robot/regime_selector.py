"""
RAMYA QUANT ROBOT — Adaptive Multi-Timeframe & Market Regime Classifier
Identifies 3 distinct market regimes:
1. Trending Market: EMA 20 > EMA 50 > EMA 200 + Supertrend Confirmation
2. Ranging/Consolidation: Bollinger Bands Squeeze + Flat ADX (< 20)
3. High Volatility/News Spike: ATR expanding beyond 2.5x Standard Deviation

Switches Strategies Dynamically:
- Trend: Trend-following pullbacks on 3m/5m aligned with 15m/30m macro trend
- Range: Mean-reversion scalping on RSI extremes (<30 / >70) & VWAP bounces
- High Volatility: Stand down / Defensive Mode
"""

import logging
from dataclasses import dataclass
from typing import Dict, Optional, Tuple
import pandas as pd

from .config import (
    ADX_RANGE_THRESHOLD,
    ADX_TREND_THRESHOLD,
    M1_ARROW_STRATEGY,
    M1_DISABLE_15M_TREND_FILTER,
    M1_PURE_BIDIRECTIONAL,
    M1_RSI_CROSS_BUY,
    M1_RSI_CROSS_SELL,
    M1_RSI_OVERBOUGHT,
    M1_RSI_OVERSOLD,
    M1_SL_OFFSET,
    M1_SWING_LOOKBACK,
    M1_TP1_OFFSET,
    M1_TP2_OFFSET,
    M1_WICK_BODY_RATIO,
    M1_WICK_RATIO_MIN,
    MIN_RISK_REWARD_RATIO,
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
    VOLATILITY_SPIKE_ATR_MULT,
)
from .indicators import compute_quant_indicators

logger = logging.getLogger("quant.regime")


@dataclass
class RegimeSignal:
    symbol: str
    regime: str              # 'TRENDING_BULLISH', 'TRENDING_BEARISH', 'RANGING', 'HIGH_VOLATILITY'
    strategy_type: str       # 'TREND_FOLLOWING_PULLBACK', 'MEAN_REVERSION_SCALP', 'STAND_DOWN'
    action: str              # 'BUY', 'SELL', 'NEUTRAL'
    entry_price: float
    stop_loss: float
    take_profit_1: float     # Target 1: Breakeven trigger level (+1.5 ATR)
    take_profit_2: float     # Target 2: Full target (Minimum 1:2 Risk:Reward)
    risk_reward_ratio: float
    atr: float
    vwap: float
    rsi: float
    adx: float
    supertrend_dir: int
    bb_bandwidth: float
    timestamp: str
    reason: str


class AdaptiveRegimeSelector:
    def __init__(
        self,
        min_rr: float = MIN_RISK_REWARD_RATIO,
        adx_trend_thresh: float = ADX_TREND_THRESHOLD,
        adx_range_thresh: float = ADX_RANGE_THRESHOLD,
    ):
        self.min_rr = min_rr
        self.adx_trend_thresh = adx_trend_thresh
        self.adx_range_thresh = adx_range_thresh

    def classify_regime(self, df_macro: pd.DataFrame) -> Tuple[str, str]:
        """
        Classifies market regime on macro timeframe (15m / 30m):
        Returns: (regime_name, detailed_reason)
        """
        if len(df_macro) < 50:
            return "UNKNOWN", "Insufficient candle depth for regime classification"

        latest = df_macro.iloc[-1]
        close = float(latest["close"])
        ema_20 = float(latest.get("ema_20", close))
        ema_50 = float(latest.get("ema_50", close))
        ema_200 = float(latest.get("ema_200", close))
        st_dir = int(latest.get("supertrend_dir", 0))
        adx = float(latest.get("adx_14", 0.0))
        atr = float(latest.get("atr_14", close * 0.01))
        atr_std = float(latest.get("atr_std_50", 1.0))
        bb_width = float(latest.get("bb_bandwidth", 0.05))

        # 1. High Volatility / News Spike Check:
        # ATR > 2.5x 50-period STD above median ATR or single bar range > 2.5 * ATR
        bar_range = float(latest["high"]) - float(latest["low"])
        median_atr = float(df_macro["atr_14"].tail(50).median()) if "atr_14" in df_macro else atr
        if bar_range > (VOLATILITY_SPIKE_ATR_MULT * atr) or (atr_std > 0.05 and atr > (median_atr + VOLATILITY_SPIKE_ATR_MULT * atr_std)):
            return "HIGH_VOLATILITY", f"Volatility Spike detected: Bar Range (${bar_range:.2f}) > 2.5x ATR (${atr:.2f})"

        # 2. Trending Regime Check:
        # Bullish: EMA 20 > EMA 50 > EMA 200 + Supertrend +1 + ADX >= 20
        if (ema_20 > ema_50 > ema_200) and (st_dir == 1) and (adx >= self.adx_trend_thresh):
            return "TRENDING_BULLISH", f"Bullish Trend: EMA20>50>200, Supertrend Bullish (+1), ADX={adx:.1f}"

        # Bearish: EMA 20 < EMA 50 < EMA 200 + Supertrend -1 + ADX >= 20
        if (ema_20 < ema_50 < ema_200) and (st_dir == -1) and (adx >= self.adx_trend_thresh):
            return "TRENDING_BEARISH", f"Bearish Trend: EMA20<50<200, Supertrend Bearish (-1), ADX={adx:.1f}"

        # 3. Ranging / Consolidation Regime Check:
        # Bollinger Bands Squeeze + Flat ADX (< 20)
        # Check if bandwidth is low (< 25th percentile of past 50 bars)
        recent_bandwidths = df_macro["bb_bandwidth"].tail(50)
        bandwidth_p25 = recent_bandwidths.quantile(0.25)
        is_bb_squeeze = bb_width <= (bandwidth_p25 * 1.15) or bb_width < 0.025

        if is_bb_squeeze or (adx < self.adx_range_thresh):
            return "RANGING", f"Consolidation/Range: BB Bandwidth={bb_width:.4f} (Squeeze), ADX={adx:.1f} < 20"

        # Default fallback: Ranging or Transition
        return "RANGING", f"Market in Transition: ADX={adx:.1f}, EMA Alignment incomplete"

    def scan_and_generate_signal(
        self,
        mtf_data: Dict[str, pd.DataFrame],
        symbol: str,
        macro_tf: str = "15m",
        structure_tf: str = "5m",
        exec_tf: str = "1m",
    ) -> RegimeSignal:
        """
        Scans across 1m, 3m, 5m, 15m, 30m, classifies regime, and switches strategy dynamically.
        """
        df_macro = mtf_data.get(macro_tf)
        df_struct = mtf_data.get(structure_tf)
        df_exec = mtf_data.get(exec_tf)

        if df_macro is None or df_struct is None or df_exec is None:
            return self._neutral(symbol, "Missing MTF streams")

        # Classify Macro Regime
        regime, regime_reason = self.classify_regime(df_macro)

        latest_exec = df_exec.iloc[-1]
        latest_struct = df_struct.iloc[-1]
        time_str = str(latest_exec.get("datetime", latest_exec.get("time", "")))
        current_price = float(latest_exec["close"])
        atr = float(latest_exec.get("atr_14", current_price * 0.01))
        if atr <= 0 or pd.isna(atr):
            atr = current_price * 0.01

        vwap = float(latest_exec.get("vwap", current_price))
        rsi = float(latest_exec.get("rsi_14", 50.0))
        latest_macro = df_macro.iloc[-1]
        adx = float(latest_macro.get("adx_14", 0.0))
        st_dir = int(latest_macro.get("supertrend_dir", 0))
        bb_width = float(latest_macro.get("bb_bandwidth", 0.0))

        # ==============================================================================
        # STRATEGY 0: M1 FAST SWING / ARROW REVERSAL SCALPING (MT5 ARROW LOGIC)
        # ==============================================================================
        if M1_ARROW_STRATEGY and df_exec is not None:
            m1_signal = self.detect_m1_arrow_signal(df_exec, symbol)
            if m1_signal is not None:
                return m1_signal
            return self._neutral(
                symbol=symbol,
                reason=f"M1 Scalper: Monitoring candle wick & RSI(7) hook ({regime})",
                timestamp=time_str,
                price=current_price,
                atr=atr,
                vwap=vwap,
                rsi=rsi,
                adx=adx,
                regime=regime,
            )

        # ==============================================================================
        # STRATEGY 1: HIGH VOLATILITY DEFENSE MODE
        # ==============================================================================
        if regime == "HIGH_VOLATILITY":
            return RegimeSignal(
                symbol=symbol,
                regime=regime,
                strategy_type="STAND_DOWN",
                action="NEUTRAL",
                entry_price=current_price,
                stop_loss=0.0,
                take_profit_1=0.0,
                take_profit_2=0.0,
                risk_reward_ratio=0.0,
                atr=atr,
                vwap=vwap,
                rsi=rsi,
                adx=adx,
                supertrend_dir=st_dir,
                bb_bandwidth=bb_width,
                timestamp=time_str,
                reason=f"DEFENSIVE HALT: {regime_reason}. Avoiding news/volatility whipsaw."
            )

        # ==============================================================================
        # STRATEGY 2: TREND-FOLLOWING PULLBACK STRATEGY (In Trending Regime)
        # ==============================================================================
        if regime in ("TRENDING_BULLISH", "TRENDING_BEARISH"):
            struct_rsi = float(latest_struct.get("rsi_14", 50.0))
            struct_ema_20 = float(latest_struct.get("ema_20", current_price))
            exec_macd_hist = float(latest_exec.get("macd_hist", 0.0))
            exec_vol_ratio = float(latest_exec.get("vol_ratio", 1.0))

            if regime == "TRENDING_BULLISH":
                # Pullback on 5m: RSI cooled to 38-55 or price near EMA 20
                pullback_active = (38.0 <= struct_rsi <= 55.0) or (abs(current_price - struct_ema_20) <= atr * 0.8)
                # Re-entry trigger on 1m: MACD hist > 0 and volume confirmation
                trigger_confirmed = (exec_macd_hist > 0) and (rsi > 48.0) and (exec_vol_ratio >= 0.9)

                if pullback_active and trigger_confirmed:
                    sl = round(current_price - (1.5 * atr), 2)
                    tp1 = round(current_price + (1.5 * atr), 2)  # Target 1: Breakeven Trigger
                    tp2 = round(current_price + (3.0 * atr), 2)  # Target 2: 1:2 R:R
                    rr = round((tp2 - current_price) / (current_price - sl), 2)

                    return RegimeSignal(
                        symbol=symbol,
                        regime=regime,
                        strategy_type="TREND_FOLLOWING_PULLBACK",
                        action="BUY",
                        entry_price=current_price,
                        stop_loss=sl,
                        take_profit_1=tp1,
                        take_profit_2=tp2,
                        risk_reward_ratio=rr,
                        atr=atr,
                        vwap=vwap,
                        rsi=rsi,
                        adx=adx,
                        supertrend_dir=st_dir,
                        bb_bandwidth=bb_width,
                        timestamp=time_str,
                        reason=f"TREND PULLBACK BUY: 15m Bullish | 5m RSI={struct_rsi:.1f} Pullback | 1m MACD Hist={exec_macd_hist:+.4f} Re-entry | R:R={rr}:1"
                    )

            elif regime == "TRENDING_BEARISH":
                # Pullback on 5m: RSI bounced to 45-62 or price near EMA 20
                pullback_active = (45.0 <= struct_rsi <= 62.0) or (abs(current_price - struct_ema_20) <= atr * 0.8)
                trigger_confirmed = (exec_macd_hist < 0) and (rsi < 52.0) and (exec_vol_ratio >= 0.9)

                if pullback_active and trigger_confirmed:
                    sl = round(current_price + (1.5 * atr), 2)
                    tp1 = round(current_price - (1.5 * atr), 2)
                    tp2 = round(current_price - (3.0 * atr), 2)
                    rr = round((current_price - tp2) / (sl - current_price), 2)

                    return RegimeSignal(
                        symbol=symbol,
                        regime=regime,
                        strategy_type="TREND_FOLLOWING_PULLBACK",
                        action="SELL",
                        entry_price=current_price,
                        stop_loss=sl,
                        take_profit_1=tp1,
                        take_profit_2=tp2,
                        risk_reward_ratio=rr,
                        atr=atr,
                        vwap=vwap,
                        rsi=rsi,
                        adx=adx,
                        supertrend_dir=st_dir,
                        bb_bandwidth=bb_width,
                        timestamp=time_str,
                        reason=f"TREND PULLBACK SELL: 15m Bearish | 5m RSI={struct_rsi:.1f} Bounce | 1m MACD Hist={exec_macd_hist:+.4f} Re-entry | R:R={rr}:1"
                    )

        # ==============================================================================
        # STRATEGY 3: MEAN-REVERSION SCALPING (In Ranging / Consolidation Regime)
        # ==============================================================================
        if regime == "RANGING":
            # Mean Reversion criteria:
            # BUY when RSI oversold (< 30) AND price below VWAP by > 0.5 ATR, rejecting lower BB
            bb_lower = float(latest_exec.get("bb_lower", current_price - atr))
            bb_upper = float(latest_exec.get("bb_upper", current_price + atr))

            # Scalp Long:
            if (rsi < RSI_OVERSOLD or current_price <= bb_lower) and (current_price < vwap):
                sl = round(current_price - (1.0 * atr), 2)
                tp1 = round(current_price + (1.5 * atr), 2)
                tp2 = round(current_price + (2.0 * atr), 2)  # Target 2: Mean Reversion to VWAP/Upper BB
                rr = round((tp2 - current_price) / (current_price - sl), 2)

                return RegimeSignal(
                    symbol=symbol,
                    regime=regime,
                    strategy_type="MEAN_REVERSION_SCALP",
                    action="BUY",
                    entry_price=current_price,
                    stop_loss=sl,
                    take_profit_1=tp1,
                    take_profit_2=tp2,
                    risk_reward_ratio=rr,
                    atr=atr,
                    vwap=vwap,
                    rsi=rsi,
                    adx=adx,
                    supertrend_dir=st_dir,
                    bb_bandwidth=bb_width,
                    timestamp=time_str,
                    reason=f"RANGE SCALP BUY: RSI={rsi:.1f} Oversold | Below VWAP (${vwap:,.2f}) | Lower BB Touch | R:R={rr}:1"
                )

            # Scalp Short:
            elif (rsi > RSI_OVERBOUGHT or current_price >= bb_upper) and (current_price > vwap):
                sl = round(current_price + (1.0 * atr), 2)
                tp1 = round(current_price - (1.5 * atr), 2)
                tp2 = round(current_price - (2.0 * atr), 2)
                rr = round((current_price - tp2) / (sl - current_price), 2)

                return RegimeSignal(
                    symbol=symbol,
                    regime=regime,
                    strategy_type="MEAN_REVERSION_SCALP",
                    action="SELL",
                    entry_price=current_price,
                    stop_loss=sl,
                    take_profit_1=tp1,
                    take_profit_2=tp2,
                    risk_reward_ratio=rr,
                    atr=atr,
                    vwap=vwap,
                    rsi=rsi,
                    adx=adx,
                    supertrend_dir=st_dir,
                    bb_bandwidth=bb_width,
                    timestamp=time_str,
                    reason=f"RANGE SCALP SELL: RSI={rsi:.1f} Overbought | Above VWAP (${vwap:,.2f}) | Upper BB Touch | R:R={rr}:1"
                )

        return self._neutral(symbol, f"Regime: {regime} ({regime_reason}) — No high-probability trigger", time_str, current_price, atr, vwap, rsi, adx)

    def detect_m1_arrow_signal(
        self,
        df_1m: pd.DataFrame,
        symbol: str,
    ) -> Optional[RegimeSignal]:
        """
        M1 Fast Swing Reversal Scalper (Autonomous Unlimited Compounder):
        - Buy Trigger (Green Arrow logic):
          * 1-minute candle makes a local low (support test with lower wick rejection).
          * Fast RSI (7-period) pulls back under 35 and hooks back up.
          * Stop-Loss: Hard -$0.16 on Delta server. Take Profit: +$0.10 (TP1/BE), +$0.25 (TP2).
        - Sell Trigger (Red Arrow logic):
          * 1-minute candle makes a local high (resistance test with upper wick rejection).
          * Fast RSI (7-period) pushes above 65 and hooks down.
          * Stop-Loss: Hard -$0.16 on Delta server. Take Profit: -$0.10 (TP1/BE), -$0.25 (TP2).
        """
        if df_1m is None or len(df_1m) < 6:
            return None

        latest = df_1m.iloc[-1]
        prev = df_1m.iloc[-2]
        prev2 = df_1m.iloc[-3] if len(df_1m) >= 3 else prev

        current_price = float(latest["close"])
        time_str = str(latest.get("datetime", latest.get("time", "")))
        atr = float(latest.get("atr_14", 1.0))
        vwap = float(latest.get("vwap", current_price))
        adx = float(latest.get("adx_14", 20.0))

        # Fast RSI (7)
        rsi7_now = float(latest.get("rsi_7", latest.get("rsi_14", 50.0)))
        rsi7_prev = float(prev.get("rsi_7", prev.get("rsi_14", 50.0)))
        rsi7_prev2 = float(prev2.get("rsi_7", prev2.get("rsi_14", 50.0)))

        # Candle Wick Geometry
        range_now = float(latest["high"] - latest["low"])
        body_now = abs(float(latest["close"] - latest["open"]))
        lower_wick_now = float(min(latest["close"], latest["open"]) - latest["low"])
        upper_wick_now = float(latest["high"] - max(latest["close"], latest["open"]))

        range_prev = float(prev["high"] - prev["low"])
        body_prev = abs(float(prev["close"] - prev["open"]))
        lower_wick_prev = float(min(prev["close"], prev["open"]) - prev["low"])
        upper_wick_prev = float(prev["high"] - max(prev["close"], prev["open"]))

        # Lower wick rejection bounce (support bounce / hammer: lower wick is dominant and significant)
        current_lower_wick = (
            (lower_wick_now >= body_now * M1_WICK_BODY_RATIO or (range_now > 0 and lower_wick_now / range_now >= M1_WICK_RATIO_MIN))
            and (lower_wick_now > upper_wick_now)
        )
        prev_lower_wick = (
            (lower_wick_prev >= body_prev * M1_WICK_BODY_RATIO or (range_prev > 0 and lower_wick_prev / range_prev >= M1_WICK_RATIO_MIN))
            and (lower_wick_prev > upper_wick_prev)
        )
        lower_wick_reject = current_lower_wick or (prev_lower_wick and lower_wick_now >= upper_wick_now)

        # Upper wick rejection drop (resistance drop / shooting star: upper wick is dominant and significant)
        current_upper_wick = (
            (upper_wick_now >= body_now * M1_WICK_BODY_RATIO or (range_now > 0 and upper_wick_now / range_now >= M1_WICK_RATIO_MIN))
            and (upper_wick_now > lower_wick_now)
        )
        prev_upper_wick = (
            (upper_wick_prev >= body_prev * M1_WICK_BODY_RATIO or (range_prev > 0 and upper_wick_prev / range_prev >= M1_WICK_RATIO_MIN))
            and (upper_wick_prev > lower_wick_prev)
        )
        upper_wick_reject = current_upper_wick or (prev_upper_wick and upper_wick_now >= lower_wick_now)

        # Fast RSI(7) Triggers:
        # 1. Crosses above 35 (or hooks up from oversold <= 42)
        rsi_bullish_trigger = (
            (rsi7_now > M1_RSI_CROSS_BUY and min(rsi7_prev, rsi7_prev2) <= M1_RSI_CROSS_BUY)
            or (rsi7_prev <= M1_RSI_CROSS_BUY and rsi7_now > M1_RSI_CROSS_BUY)
            or ((min(rsi7_now, rsi7_prev, rsi7_prev2) <= M1_RSI_OVERSOLD) and (rsi7_now > rsi7_prev))
        ) and (rsi7_now >= rsi7_prev)

        # 2. Crosses below 65 (or hooks down from overbought >= 58)
        rsi_bearish_trigger = (
            (rsi7_now < M1_RSI_CROSS_SELL and max(rsi7_prev, rsi7_prev2) >= M1_RSI_CROSS_SELL)
            or (rsi7_prev >= M1_RSI_CROSS_SELL and rsi7_now < M1_RSI_CROSS_SELL)
            or ((max(rsi7_now, rsi7_prev, rsi7_prev2) >= M1_RSI_OVERBOUGHT) and (rsi7_now < rsi7_prev))
        ) and (rsi7_now <= rsi7_prev)

        sl_offset = M1_SL_OFFSET      # 0.20
        tp1_offset = M1_TP1_OFFSET    # 0.20 (Breakeven trigger)
        tp2_offset = M1_TP2_OFFSET    # 0.35 (Target TP)
        rr = round(tp2_offset / sl_offset, 2)  # 1.75:1

        # Pure 1-Minute Momentum & Arrow Triggers (15m Trend Filter Bypassed):
        # BUY (Instant Long): Whenever 1m candle forms a lower wick rejection bounce OR RSI(7) crosses above 35
        buy_candidate = lower_wick_reject or rsi_bullish_trigger

        # SELL (Instant Short): Whenever 1m candle forms an upper wick rejection drop OR RSI(7) crosses below 65
        sell_candidate = upper_wick_reject or rsi_bearish_trigger

        # Resolve bidirectional conflict if both wicks/triggers activate simultaneously
        if buy_candidate and sell_candidate:
            if rsi_bullish_trigger and not rsi_bearish_trigger:
                sell_candidate = False
            elif rsi_bearish_trigger and not rsi_bullish_trigger:
                buy_candidate = False
            elif lower_wick_now > upper_wick_now:
                sell_candidate = False
            elif upper_wick_now > lower_wick_now:
                buy_candidate = False
            elif rsi7_now >= 50.0:
                sell_candidate = False
            else:
                buy_candidate = False

        # BUY TRIGGER (Green Arrow)
        if buy_candidate:
            sl = round(current_price - sl_offset, 2)
            tp1 = round(current_price + tp1_offset, 2)
            tp2 = round(current_price + tp2_offset, 2)
            triggers = []
            if lower_wick_reject:
                triggers.append(f"Lower Wick Rejection (${lower_wick_now:.2f})")
            if rsi_bullish_trigger:
                triggers.append(f"RSI(7) Cross/Hook Above 35 ({rsi7_prev:.1f}->{rsi7_now:.1f})")
            reason_str = " + ".join(triggers) if triggers else "M1 Momentum Bounce"

            return RegimeSignal(
                symbol=symbol,
                regime="M1_ARROW_REVERSAL",
                strategy_type="M1_SWING_ARROW_SCALP",
                action="BUY",
                entry_price=current_price,
                stop_loss=sl,
                take_profit_1=tp1,
                take_profit_2=tp2,
                risk_reward_ratio=rr,
                atr=atr,
                vwap=vwap,
                rsi=rsi7_now,
                adx=adx,
                supertrend_dir=1,
                bb_bandwidth=0.0,
                timestamp=time_str,
                reason=(
                    f"GREEN ARROW BUY: {reason_str} | Hard SL -${sl_offset:.2f} | TP +${tp2_offset:.2f} (R:R {rr}:1) | 15m Trend Bypassed"
                ),
            )

        # SELL TRIGGER (Red Arrow)
        if sell_candidate:
            sl = round(current_price + sl_offset, 2)
            tp1 = round(current_price - tp1_offset, 2)
            tp2 = round(current_price - tp2_offset, 2)
            triggers = []
            if upper_wick_reject:
                triggers.append(f"Upper Wick Rejection (${upper_wick_now:.2f})")
            if rsi_bearish_trigger:
                triggers.append(f"RSI(7) Cross/Hook Below 65 ({rsi7_prev:.1f}->{rsi7_now:.1f})")
            reason_str = " + ".join(triggers) if triggers else "M1 Momentum Drop"

            return RegimeSignal(
                symbol=symbol,
                regime="M1_ARROW_REVERSAL",
                strategy_type="M1_SWING_ARROW_SCALP",
                action="SELL",
                entry_price=current_price,
                stop_loss=sl,
                take_profit_1=tp1,
                take_profit_2=tp2,
                risk_reward_ratio=rr,
                atr=atr,
                vwap=vwap,
                rsi=rsi7_now,
                adx=adx,
                supertrend_dir=-1,
                bb_bandwidth=0.0,
                timestamp=time_str,
                reason=(
                    f"RED ARROW SELL: {reason_str} | Hard SL -${sl_offset:.2f} | TP -${tp2_offset:.2f} (R:R {rr}:1) | 15m Trend Bypassed"
                ),
            )

        return None

    def _neutral(
        self, symbol: str, reason: str, timestamp: str = "", price: float = 0.0, atr: float = 0.0, vwap: float = 0.0, rsi: float = 50.0, adx: float = 0.0, regime: str = "NEUTRAL"
    ) -> RegimeSignal:
        return RegimeSignal(
            symbol=symbol,
            regime=regime,
            strategy_type="NONE",
            action="NEUTRAL",
            entry_price=price,
            stop_loss=0.0,
            take_profit_1=0.0,
            take_profit_2=0.0,
            risk_reward_ratio=0.0,
            atr=atr,
            vwap=vwap,
            rsi=rsi,
            adx=adx,
            supertrend_dir=0,
            bb_bandwidth=0.0,
            timestamp=timestamp,
            reason=reason
        )
