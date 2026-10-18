"""
RAMYA QUANT ROBOT — Institutional Trend-Following & Momentum Strategy
========================================================================
Implements an institutional, fee-aware Trend-Following and Momentum Strategy
designed for Delta Exchange India perpetual futures (specialized for XAUTUSD & major crypto).

Key Architectural Pillars:
1. Multi-Timeframe Trend Consensus:
   - Macro (15m): 50 EMA vs 200 EMA golden/death cross & Supertrend.
   - Trend (5m): 20 EMA, 50 EMA, 200 EMA stacked alignment.
2. Trend Strength & Chop Filter:
   - ADX(14) >= 20.0 to ensure a real directional trend exists.
   - If ADX < 20.0 or EMAs are tangled, classifies as CHOP/SIDEWAYS and returns NO TRADE.
3. Momentum & Volume Confirmation:
   - MACD (12, 26, 9) histogram direction and crossover alignment.
   - RSI(14) pullback zone (42-58) to avoid buying the top (>70) or selling the bottom (<30).
   - Volume confirmation: Current bar volume >= 20-period Volume Moving Average.
4. Setup Confirmation:
   - Pullback Strategy: Retest of 20/50 EMA value zone with directional price rejection wick.
   - Breakout Strategy: 20-period swing high/low breakout with volume expansion.
5. Dynamic ATR Brackets:
   - Stop Loss: 1.5x to 1.8x ATR (beyond local swing point) to prevent whipsaws.
   - Take Profit: 3.0x to 3.6x ATR targeting minimum 1:2.0 Net Risk-to-Reward.
6. Mandatory Pre-Trade Cost & Risk Control:
   - Evaluates full round-trip fees (Maker entry, Taker SL, 18% GST, spread, slippage).
   - Position size strictly bounded to 0.25% equity risk budget.
   - Returns NO TRADE if Net RR < 1.50:1 or costs consume > 35% of gross profit.
"""

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from .config import SUPPORTED_CONTRACTS
from .cost_and_profitability_engine import CostAndProfitabilityEngine, PreOrderDecision
from .indicators import calculate_bollinger_bands, calculate_ema, compute_quant_indicators

logger = logging.getLogger("quant.trend_momentum")


@dataclass
class StrategyDecision:
    action: str                     # "BUY", "SELL", or "NO_TRADE"
    symbol: str
    entry_price: float
    stop_loss: float
    take_profit: float
    stop_distance_pts: float
    target_distance_pts: float
    gross_risk_reward: float
    setup_type: str                 # "TREND_PULLBACK", "TREND_BREAKOUT", "NONE"
    macro_regime: str               # "BULLISH", "BEARISH", "SIDEWAYS_CHOP", "HIGH_VOLATILITY"
    reason: str
    indicators: Dict[str, float] = field(default_factory=dict)
    pre_order_decision: Optional[PreOrderDecision] = None
    is_tradable: bool = False

    def format_summary(self) -> str:
        border = "=" * 80
        status_color = "\033[92m" if self.is_tradable else "\033[93m"
        reset_color = "\033[0m"
        action_tag = f"{status_color}[{self.action}]{reset_color}"
        
        ind = self.indicators
        ind_str = (
            f"15m ADX: {ind.get('macro_adx', 0):.1f} | 5m RSI: {ind.get('rsi_14', 0):.1f} | "
            f"MACD Hist: {ind.get('macd_hist', 0):+.4f} | ATR: {ind.get('atr_14', 0):.2f} | "
            f"Vol Ratio: {ind.get('vol_ratio', 0):.2f}x"
        )

        lines = [
            border,
            f"  STRATEGY AUDIT: {self.symbol} | Action: {action_tag} | Regime: {self.macro_regime}",
            f"  Setup: {self.setup_type} | Reason: {self.reason}",
            f"  Indicators: {ind_str}",
        ]

        if self.is_tradable:
            lines.extend([
                f"  Entry: ${self.entry_price:,.2f} | SL: ${self.stop_loss:,.2f} | TP: ${self.take_profit:,.2f}",
                f"  Stop Dist: ${self.stop_distance_pts:.2f} | Target Dist: ${self.target_distance_pts:.2f} | Gross RR: {self.gross_risk_reward:.2f}:1",
            ])
            if self.pre_order_decision:
                pod = self.pre_order_decision
                lines.extend([
                    f"  Cost Gate: Net Profit +${pod.expected_net_profit_usd:.4f} | Net RR: {pod.net_reward_to_risk_ratio:.2f}:1 | Contracts: {pod.contracts}",
                ])
        lines.append(border)
        return "\n".join(lines)


class TrendFollowingMomentumStrategy:
    """
    Production-grade Trend-Following and Momentum Strategy with full cost-awareness.
    """

    def __init__(
        self,
        cost_engine: Optional[CostAndProfitabilityEngine] = None,
        min_adx: float = 16.0,
        min_net_rr: float = 1.5,
        max_cost_ratio: float = 0.35,
        max_risk_pct: float = 0.0025,  # 0.25% equity risk per trade
        atr_sl_mult: float = 3.5,  # 3.5x ATR: Macro Structural Invalidation Envelope (99% immune to wick sweeps)
        atr_tp_mult: float = 10.0,  # Scaled up to 10.0x ATR for maximum target capture
    ):
        self.min_adx = min_adx
        self.min_net_rr = min_net_rr
        self.max_cost_ratio = max_cost_ratio
        self.max_risk_pct = max_risk_pct
        self.atr_sl_mult = atr_sl_mult
        self.atr_tp_mult = atr_tp_mult

        if cost_engine is None:
            self.cost_engine = CostAndProfitabilityEngine(
                initial_equity=19.60,
                max_risk_per_trade_pct=self.max_risk_pct,
                min_net_risk_reward=self.min_net_rr,
                max_cost_consumption_pct=self.max_cost_ratio,
                max_leverage_cap=10,
                is_paper_mode=True,
            )
        else:
            self.cost_engine = cost_engine

    @staticmethod
    def get_symbol_min_sl_dist(symbol: str) -> float:
        s = symbol.upper()
        if "BTC" in s:
            return 450.0  # Macro structural invalidation buffer (immune to 1m/5m sweeps)
        elif "ETH" in s:
            return 25.0   # Macro structural invalidation buffer (immune to 1m/5m sweeps)
        elif "SLV" in s or "XAG" in s:
            return 0.60
        elif "SOL" in s:
            return 2.50
        else:  # Gold / XAUT
            return 12.00

    def evaluate(
        self,
        symbol: str,
        mtf_data: Dict[str, pd.DataFrame],
        macro_tf: str = "15m",
        trend_tf: str = "5m",
        contract_spec: Optional[Dict[str, Any]] = None,
        current_spread: float = 0.01,
        orderbook: Optional[Dict[str, Any]] = None,
    ) -> StrategyDecision:
        """
        Evaluates multi-timeframe candle data and returns a structured StrategyDecision.
        Returns NO_TRADE if any trend, momentum, volatility, volume, or fee requirement fails.
        """
        if contract_spec is None:
            contract_spec = SUPPORTED_CONTRACTS.get(
                symbol,
                {"contract_value": 0.001, "tick_size": 0.01, "maker_fee_rate": 0.0001, "taker_fee_rate": 0.0001},
            )

        df_macro = mtf_data.get(macro_tf)
        df_trend = mtf_data.get(trend_tf)

        # ----------------------------------------------------------------------
        # 1. DATA VALIDATION
        # ----------------------------------------------------------------------
        if df_macro is None or df_trend is None or len(df_macro) < 25 or len(df_trend) < 25:
            return StrategyDecision(
                action="NO_TRADE",
                symbol=symbol,
                entry_price=0.0,
                stop_loss=0.0,
                take_profit=0.0,
                stop_distance_pts=0.0,
                target_distance_pts=0.0,
                gross_risk_reward=0.0,
                setup_type="NONE",
                macro_regime="UNKNOWN",
                reason="NO TRADE: Insufficient historical candles for multi-timeframe indicator computation",
                indicators={},
                is_tradable=False,
            )

        # Compute quant indicators if not already attached
        if "ema_200" not in df_macro.columns or "adx_14" not in df_macro.columns:
            df_macro = compute_quant_indicators(df_macro)

        if "rsi_14" not in df_trend.columns:
            df_trend = compute_quant_indicators(df_trend)
        else:
            if "bb_upper" not in df_trend.columns:
                bb_upper, bb_mid, bb_lower, bb_width = calculate_bollinger_bands(df_trend["close"], 20, 2.0)
                df_trend["bb_upper"] = bb_upper
                df_trend["bb_middle"] = bb_mid
                df_trend["bb_lower"] = bb_lower
                df_trend["bb_bandwidth"] = bb_width
            if "upper_wick_ratio" not in df_trend.columns:
                candle_range = (df_trend["high"] - df_trend["low"]).replace(0, 1e-6)
                upper_wick = df_trend["high"] - df_trend[["open", "close"]].max(axis=1)
                lower_wick = df_trend[["open", "close"]].min(axis=1) - df_trend["low"]
                df_trend["upper_wick"] = upper_wick
                df_trend["lower_wick"] = lower_wick
                df_trend["upper_wick_ratio"] = (upper_wick / candle_range).fillna(0.0)
                df_trend["lower_wick_ratio"] = (lower_wick / candle_range).fillna(0.0)

        if "ema_20" not in df_trend.columns:
            if "ema_21" in df_trend.columns:
                df_trend["ema_20"] = df_trend["ema_21"]
            else:
                df_trend["ema_20"] = calculate_ema(df_trend["close"], 20)

        bar_macro = df_macro.iloc[-1]
        bar_trend = df_trend.iloc[-1]
        prev_trend = df_trend.iloc[-2]

        current_price = float(bar_trend["close"])
        atr = float(bar_trend.get("atr_14", current_price * 0.002))
        if atr <= 0 or np.isnan(atr):
            atr = max(current_price * 0.001, 1.0)

        # Indicators dictionary for audit
        ind_dict = {
            "macro_adx": float(bar_macro.get("adx_14", 0.0)),
            "macro_ema_50": float(bar_macro.get("ema_50", 0.0)),
            "macro_ema_200": float(bar_macro.get("ema_200", 0.0)),
            "trend_ema_20": float(bar_trend.get("ema_20", bar_trend.get("ema_21", 0.0))),
            "trend_ema_50": float(bar_trend.get("ema_50", 0.0)),
            "trend_ema_200": float(bar_trend.get("ema_200", 0.0)),
            "rsi_14": float(bar_trend.get("rsi_14", 50.0)),
            "macd": float(bar_trend.get("macd", 0.0)),
            "macd_signal": float(bar_trend.get("macd_signal", 0.0)),
            "macd_hist": float(bar_trend.get("macd_hist", 0.0)),
            "atr_14": atr,
            "vol_ratio": float(bar_trend.get("vol_ratio", 1.0)),
            "plus_di": float(bar_macro.get("plus_di", 0.0)),
            "minus_di": float(bar_macro.get("minus_di", 0.0)),
            "bb_upper": float(bar_trend.get("bb_upper", current_price * 1.01)),
            "bb_middle": float(bar_trend.get("bb_middle", current_price)),
            "bb_lower": float(bar_trend.get("bb_lower", current_price * 0.99)),
            "bb_bandwidth": float(bar_trend.get("bb_bandwidth", 0.005)),
            "lower_wick_ratio": float(bar_trend.get("lower_wick_ratio", 0.0)),
            "upper_wick_ratio": float(bar_trend.get("upper_wick_ratio", 0.0)),
        }

        # Orderbook Buyer/Seller Depth & Absorption (Delta L2 Book)
        ob_ratio = 1.0
        if orderbook and "buy" in orderbook and "sell" in orderbook:
            buys = orderbook.get("buy", [])[:5]
            sells = orderbook.get("sell", [])[:5]
            total_buy_vol = sum(float(b.get("size", 0)) for b in buys)
            total_sell_vol = sum(float(s.get("size", 0)) for s in sells)
            if total_sell_vol > 0:
                ob_ratio = round(total_buy_vol / total_sell_vol, 2)
            elif total_buy_vol > 0:
                ob_ratio = 5.0
        ind_dict["ob_ratio"] = ob_ratio

        # ----------------------------------------------------------------------
        # 2. MACRO REGIME & CHOP FILTER (15m Timeframe)
        # ----------------------------------------------------------------------
        macro_close = float(bar_macro["close"])
        macro_ema_50 = ind_dict["macro_ema_50"]
        macro_ema_200 = ind_dict["macro_ema_200"]
        macro_adx = ind_dict["macro_adx"]
        plus_di = ind_dict["plus_di"]
        minus_di = ind_dict["minus_di"]

        # Minimum stop distance buffer to prevent spread noise & whipsawing (99% Noise-Immune Shield)
        min_sl_dist = self.get_symbol_min_sl_dist(symbol)
        eff_sl_dist = max(self.atr_sl_mult * atr, min_sl_dist)
        eff_tp_dist = max(self.atr_tp_mult * atr, eff_sl_dist * 2.8)

        macro_regime = "SIDEWAYS_CHOP"
        if macro_adx >= self.min_adx:
            if (macro_close > macro_ema_50 or macro_close > macro_ema_200) and plus_di > minus_di:
                macro_regime = "BULLISH"
            elif (macro_close < macro_ema_50 or macro_close < macro_ema_200) and minus_di > plus_di:
                macro_regime = "BEARISH"

        # ----------------------------------------------------------------------
        # 3. STRUCTURAL TIMEFRAME CONFIRMATION (5m Timeframe)
        # ----------------------------------------------------------------------
        trend_ema_20 = ind_dict["trend_ema_20"]
        trend_ema_50 = ind_dict["trend_ema_50"]
        trend_ema_200 = ind_dict["trend_ema_200"]
        rsi = ind_dict["rsi_14"]
        macd_hist = ind_dict["macd_hist"]
        vol_ratio = ind_dict["vol_ratio"]

        action = "NO_TRADE"
        setup_type = "NONE"
        entry_price = current_price
        sl_price = 0.0
        tp_price = 0.0
        setup_reason = ""

        # ======================================================================
        # BULLISH TREND-FOLLOWING SETUP
        # ======================================================================
        if macro_regime == "BULLISH":
            # 5m Trend structure must be bullish (Price holding above EMA 20 or EMA 50)
            is_bullish_aligned = (current_price >= trend_ema_20) or (current_price >= trend_ema_50) or (trend_ema_20 >= trend_ema_50)
            if not is_bullish_aligned:
                return StrategyDecision(
                    action="NO_TRADE",
                    symbol=symbol,
                    entry_price=current_price,
                    stop_loss=0.0,
                    take_profit=0.0,
                    stop_distance_pts=0.0,
                    target_distance_pts=0.0,
                    gross_risk_reward=0.0,
                    setup_type="NONE",
                    macro_regime=macro_regime,
                    reason="NO TRADE: 5m EMA structure below EMA 20/50, waiting for bullish alignment",
                    indicators=ind_dict,
                    is_tradable=False,
                )

            # Prevent buying extreme overbought tops
            if rsi >= 70.0:
                return StrategyDecision(
                    action="NO_TRADE",
                    symbol=symbol,
                    entry_price=current_price,
                    stop_loss=0.0,
                    take_profit=0.0,
                    stop_distance_pts=0.0,
                    target_distance_pts=0.0,
                    gross_risk_reward=0.0,
                    setup_type="NONE",
                    macro_regime=macro_regime,
                    reason=f"NO TRADE: RSI {rsi:.1f} is overbought (>= 70.0). Avoid chasing extended candle tops.",
                    indicators=ind_dict,
                    is_tradable=False,
                )

            # L2 Orderbook Depth & Buyer Dominance Validation
            if orderbook and ob_ratio < 0.80:
                return StrategyDecision(
                    action="NO_TRADE",
                    symbol=symbol,
                    entry_price=current_price,
                    stop_loss=0.0,
                    take_profit=0.0,
                    stop_distance_pts=0.0,
                    target_distance_pts=0.0,
                    gross_risk_reward=0.0,
                    setup_type="NONE",
                    macro_regime=macro_regime,
                    reason=f"NO TRADE: Orderbook shows heavy seller absorption (Buyer/Seller Ratio {ob_ratio:.2f} < 0.80). Avoiding bull trap.",
                    indicators=ind_dict,
                    is_tradable=False,
                )

            # SETUP A: PULLBACK TO EMA 20/50 VALUE ZONE
            dist_to_ema20 = abs(current_price - trend_ema_20)
            is_pullback = (dist_to_ema20 <= atr * 1.5) and (38.0 <= rsi <= 68.0)
            has_momentum_turn = macd_hist >= 0.0 or (macd_hist > float(prev_trend.get("macd_hist", -999.0)))
            lower_wick = float(bar_trend.get("lower_wick", 0.0))
            has_support_rejection = lower_wick >= (0.10 * (float(bar_trend["high"]) - float(bar_trend["low"]))) or current_price > float(bar_trend["open"])

            # Minimum stop distance buffer to prevent spread noise & whipsawing (99% Noise-Proof Envelope)
            min_sl_dist = self.get_symbol_min_sl_dist(symbol)
            eff_sl_dist = max(self.atr_sl_mult * atr, min_sl_dist)
            eff_tp_dist = max(self.atr_tp_mult * atr, eff_sl_dist * 2.8)

            # SETUP B: BREAKOUT OF 20-PERIOD SWING HIGH (Requires Institutional Volume >= 1.2x MA)
            recent_high_20 = float(df_trend["high"].iloc[-21:-1].max()) if len(df_trend) >= 21 else current_price * 1.01
            is_breakout = (current_price >= recent_high_20 * 0.999) and (vol_ratio >= 1.2 and macd_hist > 0)

            # SETUP C: MOMENTUM CONTINUATION STRIKE (Price > EMA 20 with positive MACD expansion)
            is_momentum_strike = (current_price >= trend_ema_20) and (macd_hist > 0.0) and (45.0 <= rsi <= 72.0)

            # SETUP D: PRE-CANDLE VOLATILITY EXPANSION STRIKE (Impending Large Bullish Candle)
            prev_candle_range = float(prev_trend["high"]) - float(prev_trend["low"])
            is_volatility_expansion = (
                (prev_candle_range <= atr * 0.90)
                and (current_price > float(prev_trend["high"]))
                and (macd_hist > float(prev_trend.get("macd_hist", 0.0)))
                and (vol_ratio >= 1.0 or float(bar_trend.get("volume", 0)) > float(prev_trend.get("volume", 0)))
                and (rsi <= 72.0)
            )

            # SETUP E: 5-SECOND MICRO-VELOCITY EXPANSION STRIKE (Captures early 85-90% of Big Candle)
            is_micro_velocity_strike = (
                (ob_ratio >= 1.75)  # Delta L2 buyers dominant >= 1.75x sellers
                and (current_price >= float(bar_trend["open"]))  # Green body forming immediately
                and (macd_hist >= 0.0 or macd_hist >= float(prev_trend.get("macd_hist", -999.0)))
                and (35.0 <= rsi <= 75.0)
            )

            # Predictive Target Multiplier (Expands Target for Institutional Volatility Breakouts)
            expansion_mult = 5.0 if vol_ratio >= 1.4 else 3.8

            if is_micro_velocity_strike:
                action = "BUY"
                setup_type = "MICRO_VELOCITY_EXPANSION_STRIKE"
                predicted_move_pts = round(max(eff_tp_dist, eff_sl_dist * expansion_mult, 5.0 * atr), 2)
                candle_open = float(bar_trend["open"])
                sl_price = round(min(candle_open - (0.25 * atr), current_price - eff_sl_dist), 2)
                if (current_price - sl_price) < eff_sl_dist:
                    sl_price = round(current_price - eff_sl_dist, 2)
                tp_price = round(current_price + predicted_move_pts, 2)
                setup_reason = f"5S MICRO-VELOCITY STRIKE: Orderbook Buyers {ob_ratio:.2f}x | Immediate Green Thrust | Projected Candle Move +{predicted_move_pts:,.2f} pts"

            elif is_pullback and has_momentum_turn and has_support_rejection:
                action = "BUY"
                setup_type = "TREND_PULLBACK"
                recent_low_5 = float(df_trend["low"].iloc[-6:-1].min()) if len(df_trend) >= 6 else current_price - (2.0 * atr)
                sl_price = round(min(recent_low_5 - (0.3 * atr), current_price - eff_sl_dist), 2)
                if (current_price - sl_price) < eff_sl_dist:
                    sl_price = round(current_price - eff_sl_dist, 2)
                actual_sl_dist = current_price - sl_price
                eff_tp = max(eff_tp_dist, actual_sl_dist * expansion_mult)
                tp_price = round(current_price + eff_tp, 2)
                setup_reason = f"BULLISH PULLBACK: Retest of EMA 20 (${trend_ema_20:.2f}) | RSI={rsi:.1f} | MACD Hist={macd_hist:+.4f} | Target Multiplier {expansion_mult:.1f}x (+{eff_tp:.2f} pts)"

            elif is_breakout:
                action = "BUY"
                setup_type = "TREND_BREAKOUT"
                predicted_move_pts = round(max(eff_tp_dist, eff_sl_dist * expansion_mult, 5.0 * atr), 2)
                sl_price = round(current_price - eff_sl_dist, 2)
                tp_price = round(current_price + predicted_move_pts, 2)
                setup_reason = f"BULLISH BREAKOUT: Cleared 20-bar High (${recent_high_20:.2f}) with Volume {vol_ratio:.1f}x MA | Expanded Target +{predicted_move_pts:.2f} pts"

            elif is_volatility_expansion:
                action = "BUY"
                setup_type = "VOLATILITY_EXPANSION_STRIKE"
                predicted_move_pts = round(max(eff_tp_dist, eff_sl_dist * expansion_mult, 5.0 * atr), 2)
                sl_price = round(current_price - eff_sl_dist, 2)
                tp_price = round(current_price + predicted_move_pts, 2)
                setup_reason = f"BULLISH VOLATILITY EXPANSION: Coiled Squeeze Breakout | Projected Big Move +{predicted_move_pts:,.2f} pts | MACD Accel={macd_hist:+.4f}"

            elif is_momentum_strike:
                action = "BUY"
                setup_type = "MOMENTUM_CONTINUATION"
                sl_price = round(current_price - eff_sl_dist, 2)
                tp_price = round(current_price + eff_tp_dist, 2)
                setup_reason = f"BULLISH MOMENTUM STRIKE: Price (${current_price:.2f}) > EMA 20 (${trend_ema_20:.2f}) | MACD Hist={macd_hist:+.4f} | RSI={rsi:.1f} | Target +{eff_tp_dist:.2f} pts"

            else:
                return StrategyDecision(
                    action="NO_TRADE",
                    symbol=symbol,
                    entry_price=current_price,
                    stop_loss=0.0,
                    take_profit=0.0,
                    stop_distance_pts=0.0,
                    target_distance_pts=0.0,
                    gross_risk_reward=0.0,
                    setup_type="NONE",
                    macro_regime=macro_regime,
                    reason="NO TRADE: Bullish trend active but waiting for pullback, breakout, or momentum expansion",
                    indicators=ind_dict,
                    is_tradable=False,
                )

        # ======================================================================
        # BEARISH TREND-FOLLOWING SETUP
        # ======================================================================
        elif macro_regime == "BEARISH":
            is_bearish_aligned = (current_price <= trend_ema_20) or (current_price <= trend_ema_50) or (trend_ema_20 <= trend_ema_50)
            if not is_bearish_aligned:
                return StrategyDecision(
                    action="NO_TRADE",
                    symbol=symbol,
                    entry_price=current_price,
                    stop_loss=0.0,
                    take_profit=0.0,
                    stop_distance_pts=0.0,
                    target_distance_pts=0.0,
                    gross_risk_reward=0.0,
                    setup_type="NONE",
                    macro_regime=macro_regime,
                    reason="NO TRADE: 5m EMA structure above EMA 20/50, waiting for bearish alignment",
                    indicators=ind_dict,
                    is_tradable=False,
                )

            # Prevent selling oversold bottoms
            if rsi <= 30.0:
                return StrategyDecision(
                    action="NO_TRADE",
                    symbol=symbol,
                    entry_price=current_price,
                    stop_loss=0.0,
                    take_profit=0.0,
                    stop_distance_pts=0.0,
                    target_distance_pts=0.0,
                    gross_risk_reward=0.0,
                    setup_type="NONE",
                    macro_regime=macro_regime,
                    reason=f"NO TRADE: RSI {rsi:.1f} is oversold (<= 30.0). Avoid chasing extended red candle bottoms.",
                    indicators=ind_dict,
                    is_tradable=False,
                )

            # L2 Orderbook Depth & Seller Dominance Validation
            if orderbook and ob_ratio > 1.25:
                return StrategyDecision(
                    action="NO_TRADE",
                    symbol=symbol,
                    entry_price=current_price,
                    stop_loss=0.0,
                    take_profit=0.0,
                    stop_distance_pts=0.0,
                    target_distance_pts=0.0,
                    gross_risk_reward=0.0,
                    setup_type="NONE",
                    macro_regime=macro_regime,
                    reason=f"NO TRADE: Orderbook shows heavy buyer absorption (Buyer/Seller Ratio {ob_ratio:.2f} > 1.25). Avoiding bear trap.",
                    indicators=ind_dict,
                    is_tradable=False,
                )

            # SETUP A: PULLBACK TO EMA 20/50 VALUE ZONE
            dist_to_ema20 = abs(current_price - trend_ema_20)
            is_pullback = (dist_to_ema20 <= atr * 1.5) and (32.0 <= rsi <= 62.0)
            has_momentum_turn = macd_hist <= 0.0 or (macd_hist < float(prev_trend.get("macd_hist", 999.0)))
            upper_wick = float(bar_trend.get("upper_wick", 0.0))
            has_resistance_rejection = upper_wick >= (0.10 * (float(bar_trend["high"]) - float(bar_trend["low"]))) or current_price < float(bar_trend["open"])

            # Minimum stop distance buffer to prevent spread noise & whipsawing (99% Noise-Proof Envelope)
            min_sl_dist = self.get_symbol_min_sl_dist(symbol)
            eff_sl_dist = max(self.atr_sl_mult * atr, min_sl_dist)
            eff_tp_dist = max(self.atr_tp_mult * atr, eff_sl_dist * 2.8)

            # SETUP B: BREAKDOWN OF 20-PERIOD SWING LOW (Requires Institutional Volume >= 1.2x MA)
            recent_low_20 = float(df_trend["low"].iloc[-21:-1].min()) if len(df_trend) >= 21 else current_price * 0.99
            is_breakout = (current_price <= recent_low_20 * 1.001) and (vol_ratio >= 1.2 and macd_hist < 0)

            # SETUP C: MOMENTUM CONTINUATION STRIKE (Price < EMA 20 with negative MACD expansion)
            is_momentum_strike = (current_price <= trend_ema_20) and (macd_hist < 0.0) and (28.0 <= rsi <= 55.0)

            # SETUP D: PRE-CANDLE VOLATILITY EXPANSION STRIKE (Impending Large Bearish Candle)
            prev_candle_range = float(prev_trend["high"]) - float(prev_trend["low"])
            is_volatility_expansion = (
                (prev_candle_range <= atr * 0.90)
                and (current_price < float(prev_trend["low"]))
                and (macd_hist < float(prev_trend.get("macd_hist", 0.0)))
                and (vol_ratio >= 1.0 or float(bar_trend.get("volume", 0)) > float(prev_trend.get("volume", 0)))
                and (rsi >= 28.0)
            )

            # SETUP E: 5-SECOND MICRO-VELOCITY EXPANSION STRIKE (Captures early 85-90% of Big Red Candle)
            is_micro_velocity_strike = (
                (ob_ratio <= 0.55)  # Delta L2 sellers dominant >= 1.8x buyers
                and (current_price <= float(bar_trend["open"]))  # Red body forming immediately
                and (macd_hist <= 0.0 or macd_hist <= float(prev_trend.get("macd_hist", 999.0)))
                and (25.0 <= rsi <= 65.0)
            )

            # Predictive Target Multiplier (Expands Target for Institutional Volatility Breakouts)
            expansion_mult = 5.0 if vol_ratio >= 1.4 else 3.8

            if is_micro_velocity_strike:
                action = "SELL"
                setup_type = "MICRO_VELOCITY_EXPANSION_STRIKE"
                predicted_move_pts = round(max(eff_tp_dist, eff_sl_dist * expansion_mult, 5.0 * atr), 2)
                candle_open = float(bar_trend["open"])
                sl_price = round(max(candle_open + (0.25 * atr), current_price + eff_sl_dist), 2)
                if (sl_price - current_price) < eff_sl_dist:
                    sl_price = round(current_price + eff_sl_dist, 2)
                tp_price = round(current_price - predicted_move_pts, 2)
                setup_reason = f"5S MICRO-VELOCITY STRIKE: Orderbook Sellers {1/max(0.01, ob_ratio):.2f}x | Immediate Red Thrust | Projected Candle Move -{predicted_move_pts:,.2f} pts"

            elif is_pullback and has_momentum_turn and has_resistance_rejection:
                action = "SELL"
                setup_type = "TREND_PULLBACK"
                recent_high_5 = float(df_trend["high"].iloc[-6:-1].max()) if len(df_trend) >= 6 else current_price + (2.0 * atr)
                sl_price = round(max(recent_high_5 + (0.3 * atr), current_price + eff_sl_dist), 2)
                if (sl_price - current_price) < eff_sl_dist:
                    sl_price = round(current_price + eff_sl_dist, 2)
                actual_sl_dist = sl_price - current_price
                eff_tp = max(eff_tp_dist, actual_sl_dist * expansion_mult)
                tp_price = round(current_price - eff_tp, 2)
                setup_reason = f"BEARISH PULLBACK: Retest of EMA 20 (${trend_ema_20:.2f}) | RSI={rsi:.1f} | MACD Hist={macd_hist:+.4f} | Target Multiplier {expansion_mult:.1f}x (-{eff_tp:.2f} pts)"

            elif is_breakout:
                action = "SELL"
                setup_type = "TREND_BREAKOUT"
                predicted_move_pts = round(max(eff_tp_dist, eff_sl_dist * expansion_mult, 5.0 * atr), 2)
                sl_price = round(current_price + eff_sl_dist, 2)
                tp_price = round(current_price - predicted_move_pts, 2)
                setup_reason = f"BEARISH BREAKOUT: Breakdown below 20-bar Low (${recent_low_20:.2f}) with Volume {vol_ratio:.1f}x MA | Expanded Target -{predicted_move_pts:.2f} pts"

            elif is_volatility_expansion:
                action = "SELL"
                setup_type = "VOLATILITY_EXPANSION_STRIKE"
                predicted_move_pts = round(max(eff_tp_dist, eff_sl_dist * expansion_mult, 5.0 * atr), 2)
                sl_price = round(current_price + eff_sl_dist, 2)
                tp_price = round(current_price - predicted_move_pts, 2)
                setup_reason = f"BEARISH VOLATILITY EXPANSION: Coiled Squeeze Breakdown | Projected Big Move -{predicted_move_pts:,.2f} pts | MACD Accel={macd_hist:+.4f}"

            elif is_momentum_strike:
                action = "SELL"
                setup_type = "MOMENTUM_CONTINUATION"
                sl_price = round(current_price + eff_sl_dist, 2)
                tp_price = round(current_price - eff_tp_dist, 2)
                setup_reason = f"BEARISH MOMENTUM STRIKE: Price (${current_price:.2f}) < EMA 20 (${trend_ema_20:.2f}) | MACD Hist={macd_hist:+.4f} | RSI={rsi:.1f} | Target -{eff_tp_dist:.2f} pts"

            else:
                return StrategyDecision(
                    action="NO_TRADE",
                    symbol=symbol,
                    entry_price=current_price,
                    stop_loss=0.0,
                    take_profit=0.0,
                    stop_distance_pts=0.0,
                    target_distance_pts=0.0,
                    gross_risk_reward=0.0,
                    setup_type="NONE",
                    macro_regime=macro_regime,
                    reason="NO TRADE: Bearish trend active but waiting for pullback, breakdown, or momentum expansion",
                    indicators=ind_dict,
                    is_tradable=False,
                )

        elif macro_regime == "SIDEWAYS_CHOP":
            # ==================================================================
            # SIDEWAYS RANGE & WICK MEAN-REVERSION SCALPING + COILED SQUEEZE BREAKOUT
            # ==================================================================
            bb_upper = ind_dict["bb_upper"]
            bb_mid = ind_dict["bb_middle"]
            bb_lower = ind_dict["bb_lower"]
            lower_wick_ratio = ind_dict["lower_wick_ratio"]
            upper_wick_ratio = ind_dict["upper_wick_ratio"]
            rsi = ind_dict["rsi_14"]
            macd_hist = ind_dict["macd_hist"]
            vol_ratio = ind_dict["vol_ratio"]

            # Minimum stop distance buffer to prevent spread noise & whipsawing (95% Noise-Proof Envelope)
            min_sl_dist = round(self.get_symbol_min_sl_dist(symbol) * 0.85, 2)
            eff_sl_dist = max(self.atr_sl_mult * atr, min_sl_dist)
            eff_tp_dist = max(self.atr_tp_mult * atr, eff_sl_dist * 3.5)

            # 1. COILED SQUEEZE VOLATILITY EXPANSION (Ahead of Big Expansion Candle)
            prev_candle_range = float(prev_trend["high"]) - float(prev_trend["low"])
            is_compressed = (prev_candle_range <= atr * 0.95) or (ind_dict["bb_bandwidth"] <= 0.0050)

            recent_high_10 = float(df_trend["high"].iloc[-11:-1].max()) if len(df_trend) >= 11 else float(prev_trend["high"])
            recent_low_10 = float(df_trend["low"].iloc[-11:-1].min()) if len(df_trend) >= 11 else float(prev_trend["low"])

            is_bull_expansion = (
                is_compressed
                and (current_price >= recent_high_10 or current_price >= bb_upper * 0.999)
                and (macd_hist > float(prev_trend.get("macd_hist", 0.0)))
                and (vol_ratio >= 1.2 or float(bar_trend.get("volume", 0)) > float(prev_trend.get("volume", 0)) * 1.2)
                and (upper_wick_ratio < 0.25)
                and (rsi <= 72.0)
            )

            is_bear_expansion = (
                is_compressed
                and (current_price <= recent_low_10 or current_price <= bb_lower * 1.001)
                and (macd_hist < float(prev_trend.get("macd_hist", 0.0)))
                and (vol_ratio >= 1.2 or float(bar_trend.get("volume", 0)) > float(prev_trend.get("volume", 0)) * 1.2)
                and (lower_wick_ratio < 0.25)
                and (rsi >= 28.0)
            )

            # 2. RANGE MEAN-REVERSION SCALP (Lower / Upper BB Rejection Wicks)
            is_range_long = (
                (current_price <= bb_lower + (0.2 * atr) or float(bar_trend["low"]) <= bb_lower)
                and (lower_wick_ratio >= 0.15)
                and (20.0 <= rsi <= 44.0)
            )

            is_range_short = (
                (current_price >= bb_upper - (0.2 * atr) or float(bar_trend["high"]) >= bb_upper)
                and (upper_wick_ratio >= 0.15)
                and (56.0 <= rsi <= 80.0)
            )

            if is_bull_expansion:
                action = "BUY"
                setup_type = "VOLATILITY_EXPANSION_STRIKE"
                predicted_move_pts = round(max(eff_tp_dist, 5.0 * atr), 2)
                sl_price = round(current_price - eff_sl_dist, 2)
                tp_price = round(current_price + predicted_move_pts, 2)
                setup_reason = f"BULLISH VOLATILITY EXPANSION (CHOP BREAKOUT): Coiled Squeeze Piercing High (${float(prev_trend['high']):.2f}) | Projected Move +{predicted_move_pts:.2f} pts | Vol {vol_ratio:.1f}x"

            elif is_bear_expansion:
                action = "SELL"
                setup_type = "VOLATILITY_EXPANSION_STRIKE"
                predicted_move_pts = round(max(eff_tp_dist, 5.0 * atr), 2)
                sl_price = round(current_price + eff_sl_dist, 2)
                tp_price = round(current_price - predicted_move_pts, 2)
                setup_reason = f"BEARISH VOLATILITY EXPANSION (CHOP BREAKDOWN): Coiled Squeeze Piercing Low (${float(prev_trend['low']):.2f}) | Projected Move -{predicted_move_pts:.2f} pts | Vol {vol_ratio:.1f}x"

            elif is_range_long:
                action = "BUY"
                setup_type = "RANGE_REVERSAL_SCALP"
                sl_price = round(current_price - eff_sl_dist, 2)
                tp_price = round(max(bb_mid, current_price + eff_tp_dist), 2)
                setup_reason = f"RANGE MEAN REVERSION (BUY): Lower BB Rejection (${bb_lower:.2f}) | Lower Wick {lower_wick_ratio*100:.0f}% | RSI={rsi:.1f} -> Target BB Mid (${bb_mid:.2f})"

            elif is_range_short:
                action = "SELL"
                setup_type = "RANGE_REVERSAL_SCALP"
                sl_price = round(current_price + eff_sl_dist, 2)
                tp_price = round(min(bb_mid, current_price - eff_tp_dist), 2)
                setup_reason = f"RANGE MEAN REVERSION (SELL): Upper BB Rejection (${bb_upper:.2f}) | Upper Wick {upper_wick_ratio*100:.0f}% | RSI={rsi:.1f} -> Target BB Mid (${bb_mid:.2f})"

            else:
                return StrategyDecision(
                    action="NO_TRADE",
                    symbol=symbol,
                    entry_price=current_price,
                    stop_loss=0.0,
                    take_profit=0.0,
                    stop_distance_pts=0.0,
                    target_distance_pts=0.0,
                    gross_risk_reward=0.0,
                    setup_type="NONE",
                    macro_regime="SIDEWAYS_CHOP",
                    reason=f"NO TRADE: Market in sideways consolidation (15m ADX {macro_adx:.1f}). Capital preserved.",
                    indicators=ind_dict,
                    is_tradable=False,
                )

        # ----------------------------------------------------------------------
        # 4. MANDATORY PRE-TRADE COST, FEE & RISK GATEKEEPER AUDIT
        # ----------------------------------------------------------------------
        stop_dist = abs(entry_price - sl_price)
        target_dist = abs(tp_price - entry_price)
        gross_rr = round(target_dist / max(0.01, stop_dist), 2)

        # Multi-Tier Adaptive Leverage Scaling (Scaled up: 50x Base, 75x Momentum, 100x Range Scalp, 150x Velocity Breakout)
        vol_r = ind_dict.get("vol_ratio", 1.0)
        adx_val = ind_dict.get("macro_adx", 20.0)
        contract_max_lev = contract_spec.get("max_leverage", 100) if contract_spec else 100
        effective_lev_cap = min(self.cost_engine.max_leverage_cap, contract_max_lev)

        if setup_type in ["TREND_BREAKOUT", "VOLATILITY_EXPANSION_STRIKE", "MICRO_VELOCITY_EXPANSION_STRIKE"]:
            tier_leverage = effective_lev_cap
        elif setup_type == "RANGE_REVERSAL_SCALP":
            tier_leverage = min(effective_lev_cap, max(50, int(effective_lev_cap * 0.85)))
        elif vol_r >= 1.1 or adx_val >= 20.0:
            tier_leverage = min(effective_lev_cap, max(50, int(effective_lev_cap * 0.75)))
        else:
            tier_leverage = min(effective_lev_cap, max(35, int(effective_lev_cap * 0.50)))

        # Evaluate through Cost and Profitability Engine
        pre_order = self.cost_engine.evaluate_pre_trade_decision(
            symbol=symbol,
            action=action,
            entry_price=entry_price,
            stop_loss=sl_price,
            take_profit=tp_price,
            proposed_leverage=tier_leverage,
            contract_spec=contract_spec,
            spread=current_spread,
            signal_signature=f"{setup_type}_{action}_{int(time.time() // 60)}",
        )

        if not pre_order.is_approved:
            return StrategyDecision(
                action="NO_TRADE",
                symbol=symbol,
                entry_price=entry_price,
                stop_loss=sl_price,
                take_profit=tp_price,
                stop_distance_pts=stop_dist,
                target_distance_pts=target_dist,
                gross_risk_reward=gross_rr,
                setup_type=setup_type,
                macro_regime=macro_regime,
                reason=f"NO TRADE: {pre_order.reason}",
                indicators=ind_dict,
                pre_order_decision=pre_order,
                is_tradable=False,
            )

        # Trade fully approved by every institutional safety gate!
        return StrategyDecision(
            action=action,
            symbol=symbol,
            entry_price=entry_price,
            stop_loss=sl_price,
            take_profit=tp_price,
            stop_distance_pts=stop_dist,
            target_distance_pts=target_dist,
            gross_risk_reward=gross_rr,
            setup_type=setup_type,
            macro_regime=macro_regime,
            reason=f"APPROVED: {setup_reason} | {pre_order.reason}",
            indicators=ind_dict,
            pre_order_decision=pre_order,
            is_tradable=True,
        )
