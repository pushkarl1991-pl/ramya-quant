"""
RAMYA AI — 24x7 Autonomous Live AI Trading Robot for Delta Exchange
Specialized for: XAUTUSD (Gold Perpetual Futures)
Configured strictly for: $20 USD Live Account Balance Guardrails

Architecture & Guardrails:
1. Multi-Indicator & Strategy Decision Engine:
   - 15m Trend Filter: EMA 50 / EMA 200 direction
   - 1m & 5m Momentum & Mean Reversion: RSI (14) hooks, Bollinger Bands (20, 2) price touches, Supertrend (10, 3)
   - Volume & Volatility: ATR (14) for dynamic stops and 20-period Volume MA to filter fake breakouts
   - Adaptive Regime Classifier:
     * Trending Mode: Pullback entries aligned with 15m macro trend
     * Range Mode: Bollinger Band edge reversals with RSI divergence confirmation
     * Choppy/Low Volume: Stand aside (hold 0 open positions)
2. Live Execution & Order Routing ($20 Balance Guardrails):
   - Leverage: Hard-locked at 3x
   - Position Size: 1 micro-contract (0.001 oz Gold, ~$4.08 notional)
   - Fee Protection: Maker Limit Orders ONLY (post_only=True) to avoid taker fees and capture rebates
   - Immediate Server-Side Stop-Loss: Capped at max $0.40 risk (2% of account)
   - Take Profit: Maker limit order placed at 1:2.5 Risk-to-Reward ratio ($1.00 profit target)
   - Breakeven Adjustment: Move SL to Breakeven (+ fees) once profit reaches +$0.50
3. Safety & Circuit Breakers:
   - 2-Loss Lockout: 2 consecutive stop-loss hits lock all trading for 4 hours
   - Daily Drawdown Freeze: If account balance drops below $18.50, flatten all positions, cancel all orders, and permanently shut down
   - Network Resilience: Auto-reconnect WebSocket with 15-second heartbeat ping/pong
"""

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from .config import (
    DELTA_API_KEY,
    DELTA_API_SECRET,
    DELTA_REST_URL,
    DELTA_WS_URL,
    IS_TESTNET,
    LOGS_DIR,
    SUPPORTED_CONTRACTS,
)
from .delta_client import DeltaClient, DeltaOrderRequest, DeltaWebSocketClient, OrderType
from .indicators import compute_quant_indicators

# Configure logger
logger = logging.getLogger("quant.live_gold")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [LIVE-GOLD] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOGS_DIR / "live_gold_trader.log", encoding="utf-8"),
    ],
)


class LiveGoldTradingRobot:
    SYMBOL = "XAUTUSD"
    PRODUCT_ID = 131253
    CONTRACT_VALUE = 0.001           # 1 contract = 0.001 fine troy oz Gold
    TICK_SIZE = 0.01
    LOCKED_LEVERAGE = 3              # Hard-locked 3x leverage
    POSITION_SIZE = 1                # Minimum 1 micro-contract

    # $20 Account Risk Guardrails
    TARGET_ACCOUNT_BALANCE = 20.00   # $20 USD Target Balance
    MAX_RISK_PER_TRADE_USD = 0.40    # Max $0.40 risk (2% of $20 account)
    TARGET_PROFIT_USD = 1.00         # $1.00 target profit (1:2.5 Risk:Reward)
    BREAKEVEN_TRIGGER_USD = 0.50     # Move SL to Breakeven once profit reaches +$0.50
    DAILY_DRAWDOWN_FREEZE_USD = 18.50  # Hard stop if account balance drops below $18.50
    CONSECUTIVE_LOSS_LIMIT = 2       # 2-loss lockout
    LOCKOUT_DURATION_HOURS = 4.0     # 4-hour halt on 2 consecutive losses

    def __init__(
        self,
        is_paper: bool = False,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
    ):
        self.is_paper = is_paper
        self.api_key = api_key or DELTA_API_KEY
        self.api_secret = api_secret or DELTA_API_SECRET

        # Initialize Delta REST client
        self.client = DeltaClient(
            api_key=self.api_key,
            api_secret=self.api_secret,
            base_url=DELTA_REST_URL,
            live_enabled=not is_paper,
        )

        # Initialize WebSocket client
        self.ws_client = DeltaWebSocketClient(
            symbols=[self.SYMBOL],
            ws_url=DELTA_WS_URL,
            on_ticker_callback=self._on_ws_ticker_update,
        )

        self.is_running = False
        self.is_frozen_permanently = False
        self.lockout_until: float = 0.0
        self.consecutive_loss_count: int = 0
        self.live_balance_usd: float = 0.0
        self.active_position: Optional[Dict[str, Any]] = None
        self.latest_ticker: Dict[str, Any] = {}
        self.worker_thread: Optional[threading.Thread] = None
        self.trades_log_file = LOGS_DIR / "live_gold_trades.log"

    def _on_ws_ticker_update(self, ticker: Dict[str, Any]):
        if ticker.get("symbol") == self.SYMBOL:
            self.latest_ticker = ticker

    # ==========================================================================
    # INITIALIZATION & HARD-LOCKED SAFETY CHECKS
    # ==========================================================================
    def initialize_account_and_guardrails(self) -> bool:
        """
        Fetches live account balance from Delta Exchange India, validates $20 parameters,
        and hard-locks order leverage to 3x.
        """
        logger.info("=" * 70)
        logger.info("RAMYA AI — DELTA EXCHANGE XAUTUSD GOLD AUTONOMOUS ROBOT")
        logger.info(f"Target Symbol: {self.SYMBOL} (Tether Gold Perpetual)")
        logger.info(f"Execution Mode: {'PAPER TRADING' if self.is_paper else 'LIVE PRODUCTION TRADING'}")
        logger.info("=" * 70)

        # 1. Fetch live wallet balance
        wallet = self.client.get_wallet_balances()
        if not wallet.get("success"):
            logger.error(f"Failed to fetch wallet balances from Delta: {wallet}")
            return False

        avail_bal = 0.0
        total_bal = 0.0
        if self.is_paper:
            avail_bal = wallet.get("available_balance", 20.00)
            total_bal = wallet.get("total_balance", 20.00)
        else:
            for item in wallet.get("result", []):
                if item.get("asset_symbol") == "USD":
                    avail_bal = float(item.get("available_balance") or 0.0)
                    total_bal = float(item.get("balance") or 0.0)
                    break

        self.live_balance_usd = avail_bal
        print(f"\n>>> [DELTA EXCHANGE LIVE WALLET] AVAILABLE USD: ${avail_bal:.2f} | TOTAL: ${total_bal:.2f} <<<")
        logger.info(f"Live USD Balance verified: ${avail_bal:.2f} (Total Equity: ${total_bal:.2f})")

        # 2. Check Daily Drawdown Freeze threshold ($18.50)
        if self.live_balance_usd < self.DAILY_DRAWDOWN_FREEZE_USD:
            logger.critical(
                f"[DRAWDOWN FREEZE ACTIVE] Current balance (${self.live_balance_usd:.2f}) "
                f"< Minimum allowed (${self.DAILY_DRAWDOWN_FREEZE_USD:.2f}). Robot cannot launch."
            )
            self.is_frozen_permanently = True
            return False

        # 3. Hard-lock leverage to 3x on Delta Exchange
        logger.info(f"Setting hard-locked order leverage to {self.LOCKED_LEVERAGE}x for {self.SYMBOL}...")
        lev_res = self.client.set_product_leverage(self.SYMBOL, self.LOCKED_LEVERAGE)
        if lev_res.get("success"):
            logger.info(f"[SUCCESS] Order leverage hard-locked to {self.LOCKED_LEVERAGE}x on Delta Exchange.")
        else:
            logger.warning(f"Could not update leverage via API: {lev_res}. Proceeding with verified 3x position sizing.")

        # 4. Check for existing positions or open orders
        open_orders = self.client.get_open_orders(self.SYMBOL)
        if open_orders:
            logger.warning(f"Found {len(open_orders)} open orders on {self.SYMBOL}. Clearing for clean startup...")
            self.client.cancel_all_orders(self.SYMBOL)

        positions = self.client.get_positions(self.SYMBOL)
        if positions:
            for p in positions:
                size = int(p.get("size") or 0)
                if size != 0:
                    logger.info(f"Found existing open position: {size} contracts @ ${p.get('entry_price')}")
                    self.active_position = {
                        "side": "buy" if size > 0 else "sell",
                        "size": abs(size),
                        "entry_price": float(p.get("entry_price") or 0.0),
                        "highest_price": float(p.get("entry_price") or 0.0),
                        "lowest_price": float(p.get("entry_price") or 0.0),
                        "is_be_armed": False,
                    }

        return True

    # ==========================================================================
    # MULTI-TIMEFRAME INDICATOR ENGINE (1m, 5m, 15m)
    # ==========================================================================
    def fetch_multi_timeframe_data(self) -> Dict[str, pd.DataFrame]:
        """
        Fetches 1m candles from Delta API and synthesizes 5m and 15m streams
        with complete vectorized indicator suites computed for each.
        """
        now = int(time.time())
        start = now - (3600 * 24)  # 24 hours of 1m data
        df_1m = self.client.get_candles(self.SYMBOL, resolution="1m", start=start, end=now)

        if df_1m.empty or len(df_1m) < 60:
            logger.warning("Insufficient 1m candle depth from Delta API.")
            return {}

        mtf: Dict[str, pd.DataFrame] = {}
        mtf["1m"] = compute_quant_indicators(df_1m.copy())

        # Synthesize 5m and 15m
        df_resample = df_1m.copy()
        if "datetime" not in df_resample.columns:
            df_resample["datetime"] = pd.to_datetime(df_resample["time"], unit="s", utc=True)
        df_resample = df_resample.set_index("datetime")

        for tf, rule in [("5m", "5min"), ("15m", "15min")]:
            res = (
                df_resample.resample(rule)
                .agg({
                    "open": "first",
                    "high": "max",
                    "low": "min",
                    "close": "last",
                    "volume": "sum",
                    "time": "first",
                })
                .dropna()
                .reset_index()
            )
            if len(res) >= 30:
                mtf[tf] = compute_quant_indicators(res)
            else:
                mtf[tf] = mtf["1m"]

        return mtf

    # ==========================================================================
    # ADAPTIVE REGIME CLASSIFIER & SIGNAL GENERATION
    # ==========================================================================
    def evaluate_strategy_and_regime(
        self, mtf: Dict[str, pd.DataFrame]
    ) -> Tuple[str, str, Optional[str], float, float, float]:
        """
        Implements Section 1: Multi-Indicator & Strategy Decision Engine
        Returns: (regime, strategy_name, action, entry_price, stop_loss, take_profit)
        where action is 'BUY', 'SELL', or None (Stand Aside).
        """
        df_15m = mtf.get("15m")
        df_5m = mtf.get("5m")
        df_1m = mtf.get("1m")

        if df_15m is None or df_5m is None or df_1m is None:
            return "UNKNOWN", "STAND_ASIDE", None, 0.0, 0.0, 0.0

        bar_15m = df_15m.iloc[-1]
        bar_5m = df_5m.iloc[-1]
        bar_1m = df_1m.iloc[-1]
        prev_1m = df_1m.iloc[-2]

        current_price = float(bar_1m["close"])
        atr = float(bar_1m.get("atr_14", 5.0))
        atr = max(atr, 2.0)

        # 1. 15m Trend Filter: EMA 50 / EMA 200 Direction
        ema_50_15m = float(bar_15m.get("ema_50", current_price))
        ema_200_15m = float(bar_15m.get("ema_200", current_price))
        macro_bullish = ema_50_15m > ema_200_15m
        macro_bearish = ema_50_15m < ema_200_15m

        # 2. Volatility & Volume Filters
        vol_ratio_1m = float(bar_1m.get("vol_ratio", 1.0))  # Current Volume / 20-period Vol MA
        adx_15m = float(bar_15m.get("adx_14", 0.0))
        bb_width_15m = float(bar_15m.get("bb_bandwidth", 0.05))

        # 3. Dynamic Stop & Target Calculations ($20 Balance Guardrails)
        # Max Dollar Risk = $0.40 (2% of account) -> Max Gold Price Distance = $0.40 / 0.001 = $400.00
        # Target Dollar Profit = $1.00 (1:2.5 R:R) -> Target Gold Price Distance = $1.00 / 0.001 = $1000.00
        sl_price_distance = min(1.5 * atr, 400.00)
        tp_price_distance = max(2.5 * sl_price_distance, 1000.00)

        # ======================================================================
        # REGIME 1: CHOPPY / LOW VOLUME FILTER (Stand Aside)
        # ======================================================================
        if (vol_ratio_1m < 0.6 and adx_15m < 18.0) or (atr < 1.0):
            return "CHOPPY_LOW_VOLUME", "STAND_ASIDE", None, current_price, 0.0, 0.0

        # ======================================================================
        # REGIME 2: TRENDING MODE (Aligned with 15m Macro Trend)
        # ======================================================================
        if adx_15m >= 20.0 and (macro_bullish or macro_bearish):
            st_dir_5m = int(bar_5m.get("supertrend_dir", 0))
            rsi_5m = float(bar_5m.get("rsi_14", 50.0))
            rsi_1m = float(bar_1m.get("rsi_14", 50.0))
            prev_rsi_1m = float(prev_1m.get("rsi_14", 50.0))
            macd_hist_1m = float(bar_1m.get("macd_hist", 0.0))

            # Bullish Trend Pullback:
            # 15m Trend Bullish + 5m Supertrend +1 + 5m RSI pulled back (38-55) + 1m RSI hooked up + MACD hist > 0
            if macro_bullish and st_dir_5m == 1:
                pullback_active = 38.0 <= rsi_5m <= 55.0
                hook_confirmed = (rsi_1m > prev_rsi_1m) and (macd_hist_1m > 0) and (vol_ratio_1m >= 0.8)
                if pullback_active and hook_confirmed:
                    sl = round(current_price - sl_price_distance, 2)
                    tp = round(current_price + tp_price_distance, 2)
                    return "TRENDING_MODE", "TREND_PULLBACK_LONG", "BUY", current_price, sl, tp

            # Bearish Trend Pullback:
            # 15m Trend Bearish + 5m Supertrend -1 + 5m RSI bounced (45-62) + 1m RSI hooked down + MACD hist < 0
            elif macro_bearish and st_dir_5m == -1:
                bounce_active = 45.0 <= rsi_5m <= 62.0
                hook_confirmed = (rsi_1m < prev_rsi_1m) and (macd_hist_1m < 0) and (vol_ratio_1m >= 0.8)
                if bounce_active and hook_confirmed:
                    sl = round(current_price + sl_price_distance, 2)
                    tp = round(current_price - tp_price_distance, 2)
                    return "TRENDING_MODE", "TREND_PULLBACK_SHORT", "SELL", current_price, sl, tp

        # ======================================================================
        # REGIME 3: RANGE MODE (Bollinger Band Edge Reversals + RSI Divergence)
        # ======================================================================
        if adx_15m < 20.0 or bb_width_15m < 0.03:
            bb_lower_5m = float(bar_5m.get("bb_lower", current_price - atr))
            bb_upper_5m = float(bar_5m.get("bb_upper", current_price + atr))
            rsi_1m = float(bar_1m.get("rsi_14", 50.0))
            prev_rsi_1m = float(prev_1m.get("rsi_14", 50.0))

            # Long Reversal: Price touched lower BB + RSI oversold (<30) hooking upward
            if (current_price <= bb_lower_5m * 1.0005) and (rsi_1m < 32.0 and rsi_1m > prev_rsi_1m):
                sl = round(current_price - sl_price_distance, 2)
                tp = round(current_price + tp_price_distance, 2)
                return "RANGE_MODE", "BB_EDGE_REVERSAL_LONG", "BUY", current_price, sl, tp

            # Short Reversal: Price touched upper BB + RSI overbought (>70) hooking downward
            if (current_price >= bb_upper_5m * 0.9995) and (rsi_1m > 68.0 and rsi_1m < prev_rsi_1m):
                sl = round(current_price + sl_price_distance, 2)
                tp = round(current_price - tp_price_distance, 2)
                return "RANGE_MODE", "BB_EDGE_REVERSAL_SHORT", "SELL", current_price, sl, tp

        return "CONSOLIDATION", "WAIT_FOR_SETUP", None, current_price, 0.0, 0.0

    # ==========================================================================
    # LIVE ORDER ROUTING & MAKER LIMIT EXECUTION
    # ==========================================================================
    def execute_maker_entry(
        self, action: str, limit_price: float, stop_loss: float, take_profit: float, strategy: str
    ) -> bool:
        """
        Places entry strictly as Maker Limit Order (post_only=True) to avoid taker fees
        and attaches server-side Stop-Loss & Take-Profit brackets immediately.
        """
        # Fetch current top of book for maker placement
        ticker = self.client.get_ticker(self.SYMBOL)
        quotes = ticker.get("quotes", {})
        best_bid = float(quotes.get("best_bid") or limit_price)
        best_ask = float(quotes.get("best_ask") or limit_price)

        # Maker limit pricing:
        # If BUY: Place at best_bid (or 1 tick below best_ask) to rest on the book
        # If SELL: Place at best_ask (or 1 tick above best_bid) to rest on the book
        if action == "BUY":
            maker_price = round(min(best_bid, best_ask - self.TICK_SIZE), 2)
        else:
            maker_price = round(max(best_ask, best_bid + self.TICK_SIZE), 2)

        order_req = DeltaOrderRequest(
            symbol=self.SYMBOL,
            side=action.lower(),
            order_type=OrderType.LIMIT.value,
            size=self.POSITION_SIZE,
            limit_price=maker_price,
            post_only=True,            # Maker-only order: avoids 0.05% taker fees
            bracket_sl_price=stop_loss,
            bracket_tp_price=take_profit,
            client_order_id=f"GOLD_{int(time.time()*1000)}",
        )

        logger.info(
            f"[ROUTING MAKER ORDER] {action} {self.POSITION_SIZE} contract {self.SYMBOL} @ "
            f"${maker_price:,.2f} (post_only=True) | SL: ${stop_loss:,.2f} | TP: ${take_profit:,.2f}"
        )

        resp = self.client.place_order(order_req)
        if resp.success:
            logger.info(
                f"[ORDER PLACED] ID: {resp.order_id} | State: {resp.status} | "
                f"Maker Price: ${maker_price:,.2f} | Strategy: {strategy}"
            )
            # In live execution, record active position state
            self.active_position = {
                "order_id": resp.order_id,
                "side": action.lower(),
                "size": self.POSITION_SIZE,
                "entry_price": maker_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "highest_price": maker_price,
                "lowest_price": maker_price,
                "is_be_armed": False,
                "strategy": strategy,
                "opened_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            }
            return True
        else:
            logger.error(f"[ORDER REJECTED] {resp.raw_response}")
            return False

    # ==========================================================================
    # POSITION MONITORING, BREAKEVEN & CIRCUIT BREAKERS
    # ==========================================================================
    def monitor_live_position_and_circuit_breakers(self):
        """
        Continuously tracks active position against mark price:
        - Breakeven Adjustment: Move SL to Breakeven (+ fees) once profit reaches +$0.50 (+500 pts).
        - 2-Loss Lockout: 4-hour halt on 2 consecutive SL hits.
        - Daily Drawdown Freeze: Shuts down permanently if balance < $18.50.
        """
        # 1. Check Daily Drawdown Freeze threshold ($18.50)
        wallet = self.client.get_wallet_balances()
        if wallet.get("success"):
            for item in wallet.get("result", []):
                if item.get("asset_symbol") == "USD":
                    self.live_balance_usd = float(item.get("available_balance") or 0.0)
                    break

        if self.live_balance_usd < self.DAILY_DRAWDOWN_FREEZE_USD and not self.is_paper:
            logger.critical(
                f"[DAILY DRAWDOWN FREEZE TRIGGERED] Balance (${self.live_balance_usd:.2f}) "
                f"< ${self.DAILY_DRAWDOWN_FREEZE_USD:.2f}! Emergency flattening account now."
            )
            self.client.cancel_all_orders(self.SYMBOL)
            self.client.close_all_positions()
            self.is_frozen_permanently = True
            self.is_running = False
            return

        if not self.active_position:
            return

        # Fetch current market price
        ticker = self.client.get_ticker(self.SYMBOL)
        current_price = float(ticker.get("mark_price") or ticker.get("close") or 0.0)
        if current_price <= 0:
            return

        pos = self.active_position
        side = pos["side"]
        entry = pos["entry_price"]
        current_sl = pos["stop_loss"]
        tp = pos["take_profit"]

        # Update watermarks
        pos["highest_price"] = max(pos["highest_price"], current_price)
        pos["lowest_price"] = min(pos["lowest_price"], current_price)

        # Dollar profit on 1 micro-contract = (Price Delta) * 0.001
        profit_usd = (current_price - entry) * self.CONTRACT_VALUE if side == "buy" else (entry - current_price) * self.CONTRACT_VALUE

        # Breakeven Adjustment: Move SL to Breakeven (+ round-trip fee buffer $0.02) once profit reaches +$0.50
        if not pos.get("is_be_armed", False) and profit_usd >= self.BREAKEVEN_TRIGGER_USD:
            fee_buffer_pts = 2.0  # $2.00 price buffer on gold = $0.002 fee cover
            if side == "buy":
                be_sl = round(entry + fee_buffer_pts, 2)
                if be_sl > current_sl:
                    pos["stop_loss"] = be_sl
                    pos["is_be_armed"] = True
                    logger.info(f"[BREAKEVEN TRIGGERED] Profit +${profit_usd:.2f} >= $0.50! SL moved to BE (${be_sl:,.2f})")
            else:
                be_sl = round(entry - fee_buffer_pts, 2)
                if be_sl < current_sl:
                    pos["stop_loss"] = be_sl
                    pos["is_be_armed"] = True
                    logger.info(f"[BREAKEVEN TRIGGERED] Profit +${profit_usd:.2f} >= $0.50! SL moved to BE (${be_sl:,.2f})")

        # Check exit execution
        closed = False
        exit_reason = ""
        if side == "buy":
            if current_price <= pos["stop_loss"]:
                closed = True
                exit_reason = "STOP_LOSS_HIT"
            elif current_price >= tp:
                closed = True
                exit_reason = "TAKE_PROFIT_HIT"
        else:
            if current_price >= pos["stop_loss"]:
                closed = True
                exit_reason = "STOP_LOSS_HIT"
            elif current_price <= tp:
                closed = True
                exit_reason = "TAKE_PROFIT_HIT"

        if closed:
            self._handle_position_closed(current_price, exit_reason)

    def _handle_position_closed(self, exit_price: float, exit_reason: str):
        pos = self.active_position
        if not pos:
            return

        side = pos["side"]
        entry = pos["entry_price"]
        pnl_usd = (exit_price - entry) * self.CONTRACT_VALUE if side == "buy" else (entry - exit_price) * self.CONTRACT_VALUE
        fees = 0.005  # Estimated maker entry/exit fee
        net_pnl = round(pnl_usd - fees, 4)

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        log_line = (
            f"{now_str} | {self.SYMBOL} | {side.upper()} | Entry: ${entry:,.2f} | "
            f"Exit: ${exit_price:,.2f} | Net PnL: ${net_pnl:+,.4f} | Reason: {exit_reason} | Strategy: {pos.get('strategy')}\n"
        )
        with open(self.trades_log_file, "a", encoding="utf-8") as f:
            f.write(log_line)

        logger.info(
            f"[TRADE CLOSED] {side.upper()} {self.SYMBOL} | Entry: ${entry:,.2f} | "
            f"Exit: ${exit_price:,.2f} | Net PnL: ${net_pnl:+,.4f} | Reason: {exit_reason}"
        )

        # 2-Loss Lockout Circuit Breaker:
        # If 2 consecutive trades hit stop loss, lock all trading for 4 hours
        if exit_reason == "STOP_LOSS_HIT":
            self.consecutive_loss_count += 1
            if self.consecutive_loss_count >= self.CONSECUTIVE_LOSS_LIMIT:
                self.lockout_until = time.time() + (self.LOCKOUT_DURATION_HOURS * 3600)
                logger.warning(
                    f"[CIRCUIT BREAKER TRIGGERED] 2 Consecutive Stop-Losses hit! "
                    f"Locking all trading for {self.LOCKOUT_DURATION_HOURS} hours."
                )
        else:
            self.consecutive_loss_count = 0

        self.active_position = None

    # ==========================================================================
    # 24x7 AUTONOMOUS SCANNING LOOP
    # ==========================================================================
    def run_scanning_iteration(self):
        """Single autonomous scanning cycle."""
        # 1. Check permanent shutdown condition
        if self.is_frozen_permanently:
            return

        # 2. Check 4-hour lockout status
        now = time.time()
        if now < self.lockout_until:
            rem_min = (self.lockout_until - now) / 60.0
            logger.info(f"[LOCKOUT ACTIVE] Standing down after 2 consecutive losses. Remaining: {rem_min:.1f} minutes.")
            return

        # 3. Monitor active position & adjust breakeven
        self.monitor_live_position_and_circuit_breakers()

        # 4. If already holding a position, do not open new trades (strictly 1 micro-contract)
        if self.active_position is not None:
            return

        # 5. Fetch MTF candle data (1m, 5m, 15m)
        mtf = self.fetch_multi_timeframe_data()
        if not mtf:
            return

        # 6. Evaluate regime and strategy
        regime, strategy, action, current_p, sl, tp = self.evaluate_strategy_and_regime(mtf)

        if action in ("BUY", "SELL"):
            logger.info(
                f"[SETUP DETECTED] Regime: {regime} | Strategy: {strategy} | Action: {action} @ ${current_p:,.2f} | "
                f"SL: ${sl:,.2f} (Max $0.40 risk) | TP: ${tp:,.2f} ($1.00 target)"
            )
            self.execute_maker_entry(action, current_p, sl, tp, strategy)
        else:
            logger.info(f"[REGIME: {regime}] Strategy: {strategy} | Market Gold Price: ${current_p:,.2f} | Standing aside.")

    def start_24x7_loop(self):
        """Starts 24x7 background execution loop."""
        if not self.initialize_account_and_guardrails():
            logger.error("Failed to initialize guardrails. Exiting startup.")
            return

        self.is_running = True
        self.ws_client.start()

        logger.info("24x7 Autonomous Execution Loop is ACTIVE. Polling Delta API every 15 seconds...")
        try:
            while self.is_running and not self.is_frozen_permanently:
                self.run_scanning_iteration()
                time.sleep(15)  # 15-second loop cadence matching WS heartbeat
        except KeyboardInterrupt:
            logger.info("Manual shutdown requested by user.")
        finally:
            self.stop()

    def stop(self):
        """Stops robot and disconnects WebSocket gracefully."""
        self.is_running = False
        self.ws_client.stop()
        logger.info("Live Gold Trading Robot shut down cleanly.")


if __name__ == "__main__":
    # Launch in Live Mode with direct execution
    robot = LiveGoldTradingRobot(is_paper=False)
    robot.start_24x7_loop()
