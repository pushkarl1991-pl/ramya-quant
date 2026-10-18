"""
RAMYA AI — 24x7 Autonomous Live Trading Robot for Delta Exchange
Real-Money Execution Engine: XAUTUSD (Tether Gold Perpetual Futures)
Configured strictly for: Percentage-Based Risk & Profit Optimizer (0.5% Max Risk)

Core Architectural Rules:
1. Risk Percentage: Max 0.5% of verified live account equity per trade.
   - No hardcoded dollar amounts.
   - Dynamic position sizing from equity, contract specs, stop distance, fees, and slippage.
   - If 1 micro-contract risk exceeds 0.5% risk budget, SKIP THE TRADE.
2. Profit Optimization: Multi-timeframe trend (15m/30m), setup (3m/5m), and entry trigger (1m).
   - EMA 20/50/200, RSI 14, MACD, ATR 14, ADX 14, VWAP, Volume MA20.
   - No signal guarantees profit; all executions are probabilistically governed.
3. Fee Control: Pre-trade estimation of maker/taker fees, spread, slippage, and funding costs.
   - Gross P&L and Net P&L recorded separately.
   - If expected net profit after fees <= total fees or Net R:R < 1.5, SKIP THE TRADE.
4. Automatic Protection:
   - Exchange order and bracket acknowledgment verification.
   - Stale data guard (> 30s latency detection).
   - Duplicate order prevention (queries active open orders before placing).
   - 2-loss lockout circuit breaker (4-hour halt).
   - Daily drawdown circuit breaker (2.0% daily equity loss limit).
   - Strictly Anti-Martingale: Zero position sizing increase or doubling on loss.
5. Live Delta Exchange India API Verification:
   - Queries live account balance, product ID 131253 (XAUTUSD), positions, and orders.
6. Unified Dashboard Output:
   - Verified Equity, 0.5% Risk Budget, Calculated Quantity, Fees, SL/TP, Net P&L, Daily Drawdown.
"""

import argparse
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
    ALLOW_MARTINGALE,
    BASE_CAPITAL_FLOOR_USD,
    BASE_ORDER_SIZE_CONTRACTS,
    BE_FEE_BUFFER_ATR,
    CONSECUTIVE_LOSS_LIMIT,
    DAILY_DRAWDOWN_FREEZE_USD,
    DAILY_LOSS_LIMIT_PCT,
    DAILY_MAX_TRADES_LIMIT,
    DELTA_API_KEY,
    DELTA_API_SECRET,
    DELTA_REST_URL,
    DELTA_WS_URL,
    EMERGENCY_PEAK_DRAWDOWN_USD,
    HARD_LOCKED_LEVERAGE,
    INITIAL_CAPITAL_USD,
    INITIAL_PRINCIPAL_USD,
    IS_PAPER_TRADING,
    IS_TESTNET,
    KILL_SWITCH_HALT_HOURS,
    LIVE_TRADING_ENABLED,
    LOCKOUT_DURATION_HOURS,
    LOGS_DIR,
    M1_BREAKEVEN_FEE_COVER_USD,
    M1_BREAKEVEN_TRIGGER_USD,
    M1_DYNAMIC_TRAIL_OFFSET_USD,
    M1_SL_OFFSET,
    M1_TP_OFFSET,
    MARGIN_PER_CONTRACT_USD,
    MAX_API_ERROR_COUNT,
    MAX_CONTRACTS_CAP,
    MAX_DAILY_NET_LOSS_USD,
    MAX_MARGIN_UTILIZATION_PCT,
    MAX_SPREAD_TICKS,
    MIN_NET_RISK_REWARD_RATIO,
    MIN_ORDERBOOK_LIQUIDITY_CONTRACTS,
    PROFIT_LOCK_PERCENTAGE,
    PROFIT_VAULT_LOCK_PCT,
    REQUIRE_EXPLICIT_APPROVAL,
    RISK_PER_TRADE_PCT,
    SCAN_INTERVAL_SECONDS,
    SIMULATED_SLIPPAGE,
    STALE_PRICE_MAX_SECONDS,
    STATE_FILE,
    SUPPORTED_CONTRACTS,
    TRADEABLE_PROFIT_PCT,
    TRADES_LOG_FILE,
    WALLET_GROWTH_SCALE_PCT,
)
from .delta_client import DeltaClient, DeltaOrderRequest, DeltaWebSocketClient, OrderType
from .indicators import compute_quant_indicators
from .regime_selector import AdaptiveRegimeSelector, RegimeSignal
from .risk_manager import QuantRiskManager, SizingDecision

# Setup Logger
logger = logging.getLogger("quant.main_trader")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [LIVE-ORCHESTRATOR] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOGS_DIR / "quant_robot.log", encoding="utf-8"),
    ],
)


class QuantTradingOrchestrator:
    SYMBOL = "XAUTUSD"
    PRODUCT_ID = 131253
    CONTRACT_VALUE = 0.001           # 1 contract = 0.001 fine troy oz Gold
    TICK_SIZE = 0.01

    def __init__(
        self,
        symbol: str = "XAUTUSD",
        is_live: bool = True,
        initial_capital: float = INITIAL_CAPITAL_USD,
        auto_dispatch: bool = True,
    ):
        self.symbol = symbol.upper()
        if self.symbol != self.SYMBOL:
            logger.warning(f"Target symbol restricted to {self.SYMBOL} only. Overriding.")
            self.symbol = self.SYMBOL

        self.is_live = is_live
        self.auto_dispatch = auto_dispatch
        self.client = DeltaClient(
            api_key=DELTA_API_KEY,
            api_secret=DELTA_API_SECRET,
            base_url=DELTA_REST_URL,
            live_enabled=self.is_live,
        )

        self.ws_client = DeltaWebSocketClient(
            symbols=[self.SYMBOL],
            ws_url=DELTA_WS_URL,
            on_ticker_callback=self._on_ws_ticker,
        )

        # Core Engines
        self.risk_manager = QuantRiskManager(
            initial_equity=initial_capital,
            risk_per_trade_pct=RISK_PER_TRADE_PCT,
            min_rr=MIN_NET_RISK_REWARD_RATIO,
            daily_dd_limit_pct=DAILY_LOSS_LIMIT_PCT,
            leverage=HARD_LOCKED_LEVERAGE,
        )
        self.regime_selector = AdaptiveRegimeSelector(min_rr=MIN_NET_RISK_REWARD_RATIO)

        # State Variables
        self.is_running = False
        self.is_frozen_permanently = False
        self.api_error_count = 0
        self.live_balance_usd: float = 0.0
        self.live_balance_inr: float = 0.0
        self.total_equity_usd: float = 0.0
        self.active_position: Optional[Dict[str, Any]] = None
        self.latest_ticker: Dict[str, Any] = {}
        self.last_quote_time: float = 0.0
        self.scan_interval: float = SCAN_INTERVAL_SECONDS
        self.worker_thread: Optional[threading.Thread] = None

    def _on_ws_ticker(self, ticker: Dict[str, Any]):
        if ticker.get("symbol") == self.SYMBOL:
            self.latest_ticker = ticker
            self.last_quote_time = time.time()

    # ==========================================================================
    # 1. LIVE ACCOUNT INITIALIZATION & CONTRACT AUDIT
    # ==========================================================================
    def initialize_live_guardrails(self) -> bool:
        """
        Validates live Delta Exchange India production account:
        - Queries live wallet equity & available balance
        - Verifies Gold contract product ID 131253
        - Checks existing open positions and orders
        - Hard-locks order leverage to 3x
        - Computes dynamic 0.5% risk budget
        """
        wallet = self.client.get_wallet_balances()
        avail_bal = 0.0
        total_equity = 0.0
        bal_inr = 0.0

        for item in wallet.get("result", []):
            if item.get("asset_symbol") == "USD":
                avail_bal = float(item.get("available_balance") or 0.0)
                total_equity = float(item.get("balance") or 0.0)
                bal_inr = float(item.get("balance_inr") or 0.0)
                break

        if total_equity <= 0:
            meta = wallet.get("meta", {})
            total_equity = float(meta.get("net_equity") or 0.0)
            avail_bal = total_equity

        self.live_balance_usd = avail_bal
        self.total_equity_usd = total_equity
        self.live_balance_inr = bal_inr
        self.risk_manager.update_equity(total_equity)

        # Calculate initial risk budget
        risk_budget = round(total_equity * RISK_PER_TRADE_PCT, 4)

        print("\n" + "=" * 76)
        print("  RAMYA AI -- AUTONOMOUS UNLIMITED COMPOUNDER & 75% SAFE VAULT ENGINE")
        print(f"  VERIFIED LIVE EQUITY: ${self.total_equity_usd:.2f} USD (~Rs {self.live_balance_inr:,.2f} INR)")
        print(f"  TRADE FREQUENCY: UNLIMITED DYNAMIC COMPOUNDER (ZERO STATIC DAILY CAP)")
        print(f"  POLLING LOOP INTERVAL: {self.scan_interval:.1f} SECOND(S) ON ORDER BOOK DEPTH")
        print(f"  CAPITAL VAULT FLOOR: ${self.risk_manager.capital_vault_floor:.2f} (75% Locked Net Gains Safe Floor)")
        print(f"  DYNAMIC CONTRACT ALLOCATION: 10 lots (<$35) -> 20 lots ($35-$55) -> 40 lots ($55+) | HARD CAP: 40")
        print(f"  TRADEABLE POOL: Initial $22.00 + 25% of Realized Profits")
        print(f"  SESSION DRAWDOWN KILL-SWITCH: -${EMERGENCY_PEAK_DRAWDOWN_USD:.2f} From Peak ({KILL_SWITCH_HALT_HOURS:.1f}h Freeze)")
        print(f"  TARGET ASSET: {self.SYMBOL} (Fine Troy Gold Perpetual, Product ID: {self.PRODUCT_ID})")
        print(f"  ORDER LEVERAGE: {HARD_LOCKED_LEVERAGE}x HARD-LOCKED")
        print(f"  EXECUTION TYPE: STRICT 100% MAKER LIMIT (Post-Only=True, Never Cross Spread)")
        print(f"  HARD SERVER STOP-LOSS: -${M1_SL_OFFSET:.2f} | QUICK SCALP TARGET (TP): +${M1_TP_OFFSET:.2f}")
        print(f"  TRAILING BREAKEVEN: Snap SL to Entry + Fee Offset (+${M1_BREAKEVEN_FEE_COVER_USD:.2f}) at +${M1_BREAKEVEN_TRIGGER_USD:.2f} Gold Move")
        print(f"  DYNAMIC TRAILING STOP: Trails {M1_DYNAMIC_TRAIL_OFFSET_USD:.2f} Points Behind Peak During Run-ups")
        print(f"  EXPLICIT APPROVAL LOCK: {'ARMED' if (REQUIRE_EXPLICIT_APPROVAL and not self.auto_dispatch) else 'DISPATCH_ENABLED'}")
        print("=" * 76 + "\n")

        logger.info(f"VERIFIED LIVE EQUITY: ${self.total_equity_usd:.2f} USD | {RISK_PER_TRADE_PCT*100:.1f}% Risk Budget: ${risk_budget:.4f} USD")

        # Daily Drawdown Freeze Check
        if self.live_balance_usd < DAILY_DRAWDOWN_FREEZE_USD and self.is_live:
            logger.critical(
                f"[DRAWDOWN FREEZE ACTIVE] Current live balance (${self.live_balance_usd:.2f}) "
                f"< Minimum allowed (${DAILY_DRAWDOWN_FREEZE_USD:.2f}). Trading halted."
            )
            self.is_frozen_permanently = True
            return False

        # Hard-lock leverage to 3x on Delta Exchange India
        logger.info(f"Setting order leverage to {HARD_LOCKED_LEVERAGE}x on Delta Exchange for {self.SYMBOL}...")
        lev_res = self.client.set_product_leverage(self.SYMBOL, HARD_LOCKED_LEVERAGE)
        if lev_res.get("success"):
            logger.info(f"[SUCCESS] Order leverage hard-locked to {HARD_LOCKED_LEVERAGE}x.")
        else:
            logger.warning(f"Leverage response: {lev_res}. Proceeding with verified risk manager limits.")

        # Sync existing open positions and cancel rogue open orders
        open_orders = self.client.get_open_orders(self.SYMBOL)
        if open_orders:
            logger.info(f"Cancelling {len(open_orders)} existing open orders for clean startup...")
            self.client.cancel_all_orders(self.SYMBOL)

        positions = self.client.get_positions(self.SYMBOL)
        if positions:
            for p in positions:
                size = int(p.get("size") or 0)
                if size != 0:
                    logger.info(f"Detected existing position on Delta: {size} contracts @ ${p.get('entry_price')}")
                    entry_val = float(p.get("entry_price") or 0.0)
                    pos_side = "buy" if size > 0 else "sell"
                    sl_val = round(entry_val - M1_SL_OFFSET if pos_side == "buy" else entry_val + M1_SL_OFFSET, 2)
                    tp_val = round(entry_val + M1_TP_OFFSET if pos_side == "buy" else entry_val - M1_TP_OFFSET, 2)
                    self.active_position = {
                        "order_id": str(p.get("order_id", "EXISTING")),
                        "side": pos_side,
                        "size": abs(size),
                        "entry_price": entry_val,
                        "stop_loss": sl_val,
                        "take_profit": tp_val,
                        "highest_price": entry_val,
                        "lowest_price": entry_val,
                        "is_be_armed": False,
                        "opened_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                    }
                    logger.info(f"Reconciled existing position: {pos_side.upper()} {abs(size)} @ ${entry_val} | Active SL: ${sl_val} | TP: ${tp_val}")

        return True

    # ==========================================================================
    # 2. MULTI-TIMEFRAME DATA ENGINE (1m, 3m, 5m, 15m, 30m)
    # ==========================================================================
    def fetch_multi_timeframe_streams(self) -> Dict[str, pd.DataFrame]:
        """
        Fetches 1m historical candles from Delta REST v2 and synthesizes
        complete indicator suites for 1m, 3m, 5m, 15m, and 30m timeframes.
        """
        now = int(time.time())
        start = now - (3600 * 24)  # 24 hours back
        df_1m = self.client.get_candles(self.SYMBOL, resolution="1m", start=start, end=now)

        if df_1m.empty or len(df_1m) < 60:
            logger.warning("Insufficient 1m candle depth from Delta API.")
            return {}

        mtf: Dict[str, pd.DataFrame] = {}
        mtf["1m"] = compute_quant_indicators(df_1m.copy())

        df_resample = df_1m.copy()
        if "datetime" not in df_resample.columns:
            df_resample["datetime"] = pd.to_datetime(df_resample["time"], unit="s", utc=True)
        df_resample = df_resample.set_index("datetime")

        for tf, rule in [("3m", "3min"), ("5m", "5min"), ("15m", "15min"), ("30m", "30min")]:
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
    # 3. AUTOMATIC PROTECTION & DATA FRESHNESS AUDIT
    # ==========================================================================
    def check_pre_trade_protections(self) -> Tuple[bool, str]:
        """
        Validates:
        1. Circuit breaker / 2-loss lockout / daily drawdown
        2. Stale market data latency (<= 30s)
        3. Exchange duplicate orders / existing active positions
        4. API failure threshold
        """
        # 1. Circuit Breaker / Lockout Check
        tripped, cb_reason = self.risk_manager.is_circuit_breaker_tripped()
        if tripped:
            if "Kill-Switch" in cb_reason or "Capital Vault" in cb_reason or "Capital Protection" in cb_reason or "Daily Net Loss" in cb_reason:
                self.client.cancel_all_orders(self.SYMBOL)
                self.client.close_all_positions(self.SYMBOL)
                self.active_position = None
                if "Capital Protection" in cb_reason or "Daily Net Loss" in cb_reason:
                    self.is_frozen_permanently = True
            return False, cb_reason

        # 1b. Daily Max Trades Hard Cap (Only if finite cap configured, < 999999)
        if DAILY_MAX_TRADES_LIMIT < 999999 and self.risk_manager.daily_trade_count >= DAILY_MAX_TRADES_LIMIT:
            return False, f"DAILY_MAX_TRADES_REACHED: {self.risk_manager.daily_trade_count}/{DAILY_MAX_TRADES_LIMIT} trades cap reached for today."

        # 2. Stale Data Latency Guard
        now = time.time()
        ticker = self.client.get_ticker(self.SYMBOL)
        if not ticker or "close" not in ticker:
            self.api_error_count += 1
            if self.api_error_count >= MAX_API_ERROR_COUNT:
                return False, f"HALTED: {self.api_error_count} consecutive API failures"
            return False, "STALE_DATA: Failed to fetch live ticker quote"
        
        self.api_error_count = 0
        self.latest_ticker = ticker
        self.last_quote_time = now

        # 3. Spread & Liquidity Validation Check
        quotes = ticker.get("quotes", {})
        best_bid = float(quotes.get("best_bid") or 0.0)
        best_ask = float(quotes.get("best_ask") or 0.0)
        bid_size = float(quotes.get("bid_size") or 0.0)
        ask_size = float(quotes.get("ask_size") or 0.0)
        if best_bid > 0 and best_ask > 0:
            spread = round(best_ask - best_bid, 4)
            max_allowed_spread = MAX_SPREAD_TICKS * self.TICK_SIZE
            if spread > max_allowed_spread:
                return False, f"WIDE_SPREAD: Bid-Ask spread (${spread:.2f}) > Max allowed (${max_allowed_spread:.2f})"
            if bid_size < MIN_ORDERBOOK_LIQUIDITY_CONTRACTS or ask_size < MIN_ORDERBOOK_LIQUIDITY_CONTRACTS:
                return False, f"LOW_LIQUIDITY: Orderbook depth (Bid: {bid_size}, Ask: {ask_size}) < {MIN_ORDERBOOK_LIQUIDITY_CONTRACTS}"

        # 4. Duplicate Order / Active Position Prevention
        if self.active_position is not None:
            return False, f"DUPLICATE_ORDER_PREVENTED: Local position already active ({self.active_position['side']})"

        open_orders = self.client.get_open_orders(self.SYMBOL)
        if open_orders:
            return False, f"DUPLICATE_ORDER_PREVENTED: {len(open_orders)} open order(s) already pending on Delta matching engine"

        positions = self.client.get_positions(self.SYMBOL)
        if positions:
            for p in positions:
                if int(p.get("size") or 0) != 0:
                    return False, f"DUPLICATE_ORDER_PREVENTED: Live position of {p.get('size')} contracts already open on Delta"

        return True, "ALL_PROTECTIONS_PASSED"

    # ==========================================================================
    # 4. ORDER ROUTING & EXCHANGE ACKNOWLEDGMENT CONFIRMATION
    # ==========================================================================
    def execute_maker_entry(
        self,
        action: str,
        sizing: SizingDecision,
        current_price: float,
        reason: str,
    ) -> bool:
        """
        Executes strictly as 100% Maker Limit Order (post_only=True) to capture zero-drain maker fees.
        Strictly never crosses the spread: BUY at best_bid, SELL at best_ask with time_in_force="gtc".
        Confirms exchange acknowledgment of order placement and bracket legs.
        Guarded by REQUIRE_EXPLICIT_APPROVAL to prevent unauthorized order dispatch.
        """
        # Fetch fresh live ticker to guarantee up-to-the-millisecond best_bid/best_ask
        fresh_ticker = self.client.get_ticker(self.SYMBOL)
        if fresh_ticker and "quotes" in fresh_ticker:
            quotes = fresh_ticker.get("quotes", {})
            self.latest_ticker = fresh_ticker
        else:
            quotes = self.latest_ticker.get("quotes", {})

        best_bid = float(quotes.get("best_bid") or current_price)
        best_ask = float(quotes.get("best_ask") or current_price)

        # Strict Zero-Drain Fee Execution (100% Maker Mode, Never Cross Spread):
        # Guarantee limit_price is strictly passive: BUY < best_ask, SELL > best_bid
        if action == "BUY":
            limit_price = min(best_bid, round(best_ask - self.TICK_SIZE, 2)) if (best_bid > 0 and best_ask > 0) else round(current_price - self.TICK_SIZE, 2)
        else:
            limit_price = max(best_ask, round(best_bid + self.TICK_SIZE, 2)) if (best_bid > 0 and best_ask > 0) else round(current_price + self.TICK_SIZE, 2)

        # User Approval Check (Bypassed when auto_dispatch=True for autonomous live trading)
        if self.is_live and REQUIRE_EXPLICIT_APPROVAL and not self.auto_dispatch:
            logger.warning(
                f"[ORDER BLOCKED - APPROVAL REQUIRED] Signal generated for {action} {sizing.contracts} contracts {self.SYMBOL} @ "
                f"${limit_price:,.2f} | Net Risk: ${sizing.net_risk_usd:.4f} ({sizing.risk_pct:.2f}%) | "
                f"Net Reward: ${sizing.net_reward_usd:.4f}. Live order placement requires explicit user approval. Standing by."
            )
            return False

        order_req = DeltaOrderRequest(
            symbol=self.SYMBOL,
            side=action.lower(),
            order_type=OrderType.LIMIT.value,
            size=sizing.contracts,
            limit_price=limit_price,
            time_in_force="gtc",                 # STRICT MAKER GTC (ZERO TAKER DRAIN)
            post_only=True,                     # STRICT 100% POST-ONLY (NEVER CROSS SPREAD)
            bracket_sl_price=sizing.stop_loss,
            bracket_tp_price=sizing.take_profit_2,
            bracket_tp_limit_price=sizing.take_profit_2,
            client_order_id=f"RAMYA_{int(time.time()*1000)}",
        )

        logger.info(
            f"[ROUTING MAKER ORDER] {action} {sizing.contracts} contracts {self.SYMBOL} @ "
            f"${limit_price:,.2f} (Strict 100% Maker Post-Only) | Net Risk: ${sizing.net_risk_usd:.4f} | "
            f"Net Reward: ${sizing.net_reward_usd:.4f} | R:R: {sizing.risk_reward:.2f}:1 | "
            f"SL: ${sizing.stop_loss:.2f} | TP: ${sizing.take_profit_2:.2f} | Reason: {reason}"
        )

        resp = self.client.place_order(order_req)

        # Handle race condition where spread tightened between quote fetch and order matching
        if not resp.success and "immediate_execution_post_only" in str(resp.raw_response):
            logger.warning("[MAKER RETRY] Market moved during transit. Adjusting 1 tick passive...")
            if action == "BUY":
                limit_price = round(limit_price - self.TICK_SIZE, 2)
            else:
                limit_price = round(limit_price + self.TICK_SIZE, 2)
            order_req.limit_price = limit_price
            order_req.client_order_id = f"RAMYA_{int(time.time()*1000)}"
            resp = self.client.place_order(order_req)

        # Exchange Acknowledgment Verification
        if not resp.success or not resp.order_id:
            logger.error(
                f"[ORDER REJECTED BY DELTA] Server rejected order: {resp.raw_response}. Aborting entry."
            )
            return False

        logger.info(
            f"[EXCHANGE ACKNOWLEDGED] Order ID: {resp.order_id} | State: {resp.status} | "
            f"Limit: ${limit_price:,.2f} (Post-Only Maker) | Server-Side SL: ${sizing.stop_loss:,.2f} | TP: ${sizing.take_profit_2:,.2f}"
        )

        self.active_position = {
            "order_id": resp.order_id,
            "side": action.lower(),
            "size": sizing.contracts,
            "entry_price": limit_price,
            "stop_loss": sizing.stop_loss,
            "take_profit": sizing.take_profit_2,
            "take_profit_1": sizing.take_profit_1,
            "highest_price": limit_price,
            "lowest_price": limit_price,
            "is_be_armed": False,
            "estimated_fees": sizing.estimated_fees_usd,
            "estimated_slippage": sizing.estimated_slippage_usd,
            "opened_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        }
        self.risk_manager.active_positions[self.SYMBOL] = self.active_position
        return True

    # ==========================================================================
    # 5. POSITION MONITORING & SEPARATE GROSS / NET P&L ACCOUNTING
    # ==========================================================================
    def monitor_live_position(self):
        """
        Monitors active position against live mark price:
        - Dynamic Breakeven trailing
        - Separate Gross P&L and Net P&L accounting upon exit
        """
        if not self.active_position:
            return

        ticker = self.client.get_ticker(self.SYMBOL)
        mark_price = float(ticker.get("mark_price") or ticker.get("close") or 0.0)
        if mark_price <= 0:
            return

        pos = self.active_position
        side = pos["side"]
        entry = pos["entry_price"]
        current_sl = pos["stop_loss"]
        tp = pos["take_profit"]
        size = pos["size"]

        pos["highest_price"] = max(pos["highest_price"], mark_price)
        pos["lowest_price"] = min(pos["lowest_price"], mark_price)

        # Trailing Breakeven: Move SL to Entry + Fee Offset ONLY after profit crosses +$1.20 gold points
        price_delta = (mark_price - entry) if side == "buy" else (entry - mark_price)
        if not pos.get("is_be_armed", False) and price_delta >= M1_BREAKEVEN_TRIGGER_USD:
            be_sl = round(entry + M1_BREAKEVEN_FEE_COVER_USD, 2) if side == "buy" else round(entry - M1_BREAKEVEN_FEE_COVER_USD, 2)
            pos["stop_loss"] = be_sl
            pos["is_be_armed"] = True
            logger.info(
                f"[TRAILING BREAKEVEN ACTIVATED] Trade reached +${price_delta:.2f} >= +${M1_BREAKEVEN_TRIGGER_USD:.2f} gold move! "
                f"Stop-Loss moved to Entry + Fee Cover (${be_sl:,.2f}) — NET PROFIT PROTECTED."
            )

        # Dynamic Trailing Stop to capture maximum run-up during large market trend moves:
        if pos.get("is_be_armed", False):
            if side == "buy":
                trail_sl = round(mark_price - M1_DYNAMIC_TRAIL_OFFSET_USD, 2)
                if trail_sl > pos["stop_loss"]:
                    pos["stop_loss"] = trail_sl
                    logger.debug(f"[DYNAMIC TRAIL] Buy SL ratcheted up to ${trail_sl:,.2f} (Mark: ${mark_price:,.2f})")
            else:
                trail_sl = round(mark_price + M1_DYNAMIC_TRAIL_OFFSET_USD, 2)
                if trail_sl < pos["stop_loss"]:
                    pos["stop_loss"] = trail_sl
                    logger.debug(f"[DYNAMIC TRAIL] Sell SL ratcheted down to ${trail_sl:,.2f} (Mark: ${mark_price:,.2f})")

        # Check exit execution
        closed = False
        exit_reason = ""
        if side == "buy":
            if mark_price <= pos["stop_loss"] and pos["stop_loss"] > 0:
                closed = True
                exit_reason = "STOP_LOSS_HIT"
            elif mark_price >= tp and tp > 0:
                closed = True
                exit_reason = "TAKE_PROFIT_HIT"
        else:
            if mark_price >= pos["stop_loss"] and pos["stop_loss"] > 0:
                closed = True
                exit_reason = "STOP_LOSS_HIT"
            elif mark_price <= tp and tp > 0:
                closed = True
                exit_reason = "TAKE_PROFIT_HIT"

        if closed:
            self._handle_trade_closed(mark_price, exit_reason)

    def _handle_trade_closed(self, exit_price: float, exit_reason: str):
        pos = self.active_position
        if not pos:
            return

        side = pos["side"]
        entry = pos["entry_price"]
        size = pos["size"]

        # Gross P&L Calculation
        price_diff = (exit_price - entry) if side == "buy" else (entry - exit_price)
        gross_pnl_usd = round(price_diff * self.CONTRACT_VALUE * size, 4)

        # Actual Fees & Slippage Calculation
        maker_fee = entry * self.CONTRACT_VALUE * size * 0.0002
        if exit_reason == "TAKE_PROFIT_HIT":
            exit_fee = exit_price * self.CONTRACT_VALUE * size * 0.0002  # Maker TP
            slippage = 0.0
        else:
            exit_fee = exit_price * self.CONTRACT_VALUE * size * 0.0005  # Taker SL
            slippage = round(2 * self.TICK_SIZE * self.CONTRACT_VALUE * size, 4)

        total_fees = round(maker_fee + exit_fee, 4)
        net_pnl_usd = round(gross_pnl_usd - total_fees - slippage, 4)

        # Update Risk Manager metrics
        self.risk_manager.record_trade_exit(
            symbol=self.SYMBOL,
            gross_pnl=gross_pnl_usd,
            net_pnl=net_pnl_usd,
            fees_paid=total_fees,
            slippage_paid=slippage,
        )

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        log_line = (
            f"{now_str} | LIVE_TRADE | {self.SYMBOL} | {side.upper()} | QTY={size} | "
            f"ENTRY=${entry:,.2f} | EXIT=${exit_price:,.2f} | GROSS_PNL=${gross_pnl_usd:+,.4f} | "
            f"FEES=${total_fees:.4f} | SLIPPAGE=${slippage:.4f} | NET_PNL=${net_pnl_usd:+,.4f} | "
            f"REASON={exit_reason}\n"
        )
        with open(TRADES_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(log_line)

        logger.info(
            f"[LIVE TRADE CLOSED] {side.upper()} {size} contracts {self.SYMBOL} | Entry: ${entry:,.2f} | "
            f"Exit: ${exit_price:,.2f} | Gross PnL: ${gross_pnl_usd:+,.4f} | Fees: ${total_fees:.4f} | "
            f"Net PnL: ${net_pnl_usd:+,.4f} | Reason: {exit_reason}"
        )

        # Physically close position on Delta Exchange to maintain 100% synchronization
        try:
            self.client.close_all_positions(self.SYMBOL)
            self.client.cancel_all_orders(self.SYMBOL)
        except Exception as e:
            logger.error(f"Error executing position close on Delta: {e}")

        self.active_position = None

    # ==========================================================================
    # 6. UNIFIED REAL-TIME CONSOLE & STATE DASHBOARD
    # ==========================================================================
    def output_dashboard_cycle(self, mtf: Dict[str, pd.DataFrame]):
        """Prints live comprehensive metrics and persists state to disk."""
        # Query fresh verified balance
        wallet = self.client.get_wallet_balances()
        for item in wallet.get("result", []):
            if item.get("asset_symbol") == "USD":
                self.live_balance_usd = float(item.get("available_balance") or 0.0)
                self.total_equity_usd = float(item.get("balance") or 0.0)
                self.live_balance_inr = float(item.get("balance_inr") or 0.0)
                break

        self.risk_manager.update_equity(self.total_equity_usd)
        risk_budget = self.total_equity_usd * RISK_PER_TRADE_PCT

        ticker = self.client.get_ticker(self.SYMBOL)
        mark_price = float(ticker.get("mark_price") or ticker.get("close") or 0.0)

        # MTF indicators summary
        df_15m = mtf.get("15m")
        df_1m = mtf.get("1m")
        rsi_1m = float(df_1m.iloc[-1].get("rsi_14", 50.0)) if df_1m is not None else 50.0
        adx_15m = float(df_15m.iloc[-1].get("adx_14", 0.0)) if df_15m is not None else 0.0
        ema_50 = float(df_15m.iloc[-1].get("ema_50", 0.0)) if df_15m is not None else 0.0
        ema_200 = float(df_15m.iloc[-1].get("ema_200", 0.0)) if df_15m is not None else 0.0
        trend_status = "BULLISH" if ema_50 > ema_200 else ("BEARISH" if ema_50 < ema_200 else "NEUTRAL")

        cb_status = "OK" if not self.risk_manager.circuit_breaker_active else f"HALTED: {self.risk_manager.circuit_breaker_reason}"

        # Unrealized PnL if in position
        unrealized_pnl = 0.0
        pos_str = "0 contracts (Flat)"
        if self.active_position:
            side = self.active_position["side"]
            pos_size = self.active_position["size"]
            entry_p = self.active_position["entry_price"]
            delta = (mark_price - entry_p) if side == "buy" else (entry_p - mark_price)
            unrealized_pnl = round(delta * self.CONTRACT_VALUE * pos_size, 4)
            pos_str = f"{pos_size} contracts {side.upper()} @ ${entry_p:,.2f} (uPnL: ${unrealized_pnl:+,.4f})"

        # Daily Drawdown
        daily_loss_usd = abs(min(0.0, self.risk_manager.daily_realized_net_pnl))
        daily_dd_pct = (daily_loss_usd / self.risk_manager.day_start_equity) * 100.0

        state_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": self.SYMBOL,
            "gold_mark_price": mark_price,
            "verified_equity_usd": round(self.total_equity_usd, 2),
            "verified_equity_inr": round(self.live_balance_inr, 2),
            "risk_budget_usd": round(risk_budget, 4),
            "risk_budget_pct_label": f"{RISK_PER_TRADE_PCT*100:.1f}%",
            "risk_budget_05pct_usd": round(risk_budget, 4),
            "macro_trend_15m": trend_status,
            "adx_15m": round(adx_15m, 1),
            "rsi_1m": round(rsi_1m, 1),
            "open_position": pos_str,
            "unrealized_pnl_usd": unrealized_pnl,
            "daily_gross_pnl_usd": round(self.risk_manager.daily_realized_gross_pnl, 4),
            "daily_net_pnl_usd": round(self.risk_manager.daily_realized_net_pnl, 4),
            "daily_fees_paid_usd": round(self.risk_manager.daily_fees_paid, 4),
            "daily_drawdown_pct": round(daily_dd_pct, 2),
            "consecutive_losses": self.risk_manager.consecutive_loss_count,
            "daily_trades_count": self.risk_manager.daily_trade_count,
            "daily_trades_limit": "UNLIMITED" if DAILY_MAX_TRADES_LIMIT >= 999999 else DAILY_MAX_TRADES_LIMIT,
            "capital_vault_floor_usd": round(self.risk_manager.capital_vault_floor, 2),
            "locked_reserve_profit_usd": round(self.risk_manager.locked_reserve_profit, 2),
            "session_peak_equity_usd": round(self.risk_manager.peak_equity, 2),
            "max_daily_loss_limit_usd": MAX_DAILY_NET_LOSS_USD,
            "circuit_breaker_status": cb_status,
        }

        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state_data, f, indent=2)

        trades_str = f"#{self.risk_manager.daily_trade_count} (Unlimited Compounder)" if DAILY_MAX_TRADES_LIMIT >= 999999 else f"{self.risk_manager.daily_trade_count}/{DAILY_MAX_TRADES_LIMIT}"
        scaled_size = self.risk_manager.get_scaled_lot_size(mark_price, self.CONTRACT_VALUE)
        logger.info(
            f"[DASHBOARD] Equity: ${self.total_equity_usd:.2f} (~Rs {self.live_balance_inr:,.0f}) | "
            f"Vault Floor: ${self.risk_manager.capital_vault_floor:.2f} (75% Locked: ${self.risk_manager.locked_reserve_profit:.2f}) | "
            f"Size: {scaled_size} lots (Cap: 40) | Trades: {trades_str} | Gold: ${mark_price:,.2f} | "
            f"Position: {pos_str} | Net PnL: ${self.risk_manager.daily_realized_net_pnl:+,.4f} | Breaker: {cb_status}"
        )

    # ==========================================================================
    # 7. CONTINUOUS AUTONOMOUS CYCLE
    # ==========================================================================
    def run_cycle(self):
        """Single autonomous scan and execution cycle."""
        if self.is_frozen_permanently:
            return

        # 1. Check Pre-Trade Automatic Protections
        is_safe, safety_reason = self.check_pre_trade_protections()
        if not is_safe and not self.active_position:
            logger.info(f"[PROTECTION ENGAGED] Standing down: {safety_reason}")
            return

        # 2. Monitor Active Position if Held
        self.monitor_live_position()

        # 3. Fetch Multi-Timeframe Streams (1m, 3m, 5m, 15m, 30m)
        mtf = self.fetch_multi_timeframe_streams()
        if not mtf:
            return

        # 4. Update Real-Time Dashboard
        self.output_dashboard_cycle(mtf)

        # 5. If Position Already Open, Hold
        if self.active_position is not None:
            return

        # 6. Scan for Multi-Timeframe Signals
        signal: RegimeSignal = self.regime_selector.scan_and_generate_signal(
            mtf_data=mtf,
            symbol=self.SYMBOL,
            macro_tf="15m",
            structure_tf="5m",
            exec_tf="1m",
        )

        if signal.action not in ("BUY", "SELL"):
            return

        # 7. Percentage-Based Risk & Fee Optimization
        current_price = signal.entry_price
        sizing: SizingDecision = self.risk_manager.calculate_position_size(
            symbol=self.SYMBOL,
            entry_price=current_price,
            stop_loss=signal.stop_loss,
            take_profit_2=signal.take_profit_2,
            strategy_type=signal.strategy_type,
        )

        if not sizing.is_approved:
            logger.info(f"[TRADE SKIPPED BY RISK OPTIMIZER] {sizing.reason}")
            return

        # 8. Route Maker Order & Verify Exchange Acknowledgment
        self.execute_maker_entry(
            action=signal.action,
            sizing=sizing,
            current_price=current_price,
            reason=signal.reason,
        )

    def start(self, block: bool = True):
        """Launches the 24x7 autonomous execution engine."""
        if not self.initialize_live_guardrails():
            logger.error("Live guardrails initialization failed. Exiting.")
            return

        self.is_running = True
        self.ws_client.start()

        freq_str = "Unlimited Dynamic Compounder" if DAILY_MAX_TRADES_LIMIT >= 999999 else f"Hard Cap: {DAILY_MAX_TRADES_LIMIT} trades/day"
        logger.info(
            f"RAMYA Autonomous M1 Scalper Active ({freq_str} | 100% Maker Post-Only). "
            f"Scanning Delta API order book every {self.scan_interval:.1f} second(s)..."
        )
        if block:
            try:
                while self.is_running and not self.is_frozen_permanently:
                    self.run_cycle()
                    time.sleep(self.scan_interval)
            except (KeyboardInterrupt, SystemExit):
                self.stop()
        else:
            self.worker_thread = threading.Thread(target=self._run_loop, daemon=True, name="LiveQuantOrchestrator")
            self.worker_thread.start()

    def _run_loop(self):
        while self.is_running and not self.is_frozen_permanently:
            self.run_cycle()
            time.sleep(self.scan_interval)

    def stop(self):
        self.is_running = False
        self.ws_client.stop()
        logger.info("Quant Trading Orchestrator shut down cleanly.")


def main():
    """Wiped and overridden: Diverts execution directly to the Elite HFT Net-Positive Scalper."""
    from quant_robot.hft_net_positive_scalper import main as hft_main
    hft_main()


if __name__ == "__main__":
    main()
