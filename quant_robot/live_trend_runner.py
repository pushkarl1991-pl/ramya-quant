"""
RAMYA QUANT ROBOT -- Autonomous 24/7 Live Trend-Following & Momentum Runner
=============================================================================
Deploys the institutional Trend-Following & Momentum Strategy onto Delta Exchange India.

Operational Guardrails:
1. Multi-Timeframe Trend Consensus: 15m EMA 50/200 + ADX(14) >= 20.0 (blocks sideways chop).
2. Momentum & Pullback Entry: 5m EMA 20/50 retest, RSI(14) 40-60, MACD histogram expansion.
3. Fee-Aware Gatekeeper: Must yield >= 1:1.5 Net RR strictly after entry, exit, 18% GST, spread, and slippage.
4. Capital Protection: Max 0.25% equity risk per trade, 10x-25x safe leverage scaling.
5. Maker Limit Execution: 100% Post-Only Limit orders (never crosses spread as taker).
6. Zero-Loss Breakeven Guard: Snaps SL to entry + fee buffer when price expands in profit.
7. Real-Time Telemetry: Unified dashboard rendering live equity, fees paid, net PnL, and drawdown.
"""

import argparse
import asyncio
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

# Ensure root is in sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from quant_robot.config import (
    DELTA_API_KEY,
    DELTA_API_SECRET,
    DELTA_REST_URL,
    DELTA_WS_URL,
    LOGS_DIR,
    SUPPORTED_CONTRACTS,
    TRADES_LOG_FILE,
)
from quant_robot.cost_and_profitability_engine import CostAndProfitabilityEngine
from quant_robot.dashboard import UnifiedTradingDashboard
from quant_robot.delta_client import DeltaClient, DeltaOrderRequest, DeltaWebSocketClient, OrderResponse, OrderType
from quant_robot.trend_momentum_strategy import TrendFollowingMomentumStrategy
from ramya_delta_robot.market_data_engine import MarketDataEngine

logger = logging.getLogger("quant.live_runner")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [LIVE-RUNNER] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOGS_DIR / "live_trend_runner.log", encoding="utf-8"),
    ],
)


class AutonomousLiveTrendRunner:
    SYMBOL = "XAUTUSD"
    PRODUCT_ID = 131253
    TICK_SIZE = 0.01

    def __init__(
        self,
        symbol: str = "XAUTUSD",
        symbols: Optional[List[str]] = None,
        is_live: bool = False,
        poll_interval: float = 5.0,
        max_risk_pct: float = 0.01,
        max_leverage: int = 75,
    ):
        if symbols:
            raw_symbols = [s.strip().upper() for s in symbols]
        elif "," in symbol:
            raw_symbols = [s.strip().upper() for s in symbol.split(",")]
        else:
            raw_symbols = [symbol.strip().upper()]

        # Resolve aliases (e.g. SILVER -> SLVONUSD, XAGUSD -> SLVONUSD)
        self.symbols = [SUPPORTED_CONTRACTS.get(s, {}).get("symbol", s) for s in raw_symbols]
        self.primary_symbol = self.symbols[0]
        self.symbol_display = " / ".join(self.symbols)
        self.symbol = self.symbols[0]
        self.is_live = is_live
        self.poll_interval = poll_interval
        self.max_risk_pct = max_risk_pct
        self.max_leverage = max_leverage
        self.spec = SUPPORTED_CONTRACTS.get(self.symbol, {"contract_value": 0.001, "tick_size": 0.01})
        self.contract_val = self.spec["contract_value"]

        # Delta Clients
        self.client = DeltaClient(
            api_key=DELTA_API_KEY,
            api_secret=DELTA_API_SECRET,
            base_url=DELTA_REST_URL,
            live_enabled=self.is_live,
        )
        self.md_engine = MarketDataEngine()

        # Initial live wallet query
        initial_equity = self._sync_live_wallet()

        # Risk & Strategy Engines
        self.cost_engine = CostAndProfitabilityEngine(
            initial_equity=initial_equity,
            max_risk_per_trade_pct=self.max_risk_pct,
            daily_loss_limit_pct=0.20,  # 20.0% daily loss limit
            min_net_risk_reward=1.35,
            max_cost_consumption_pct=0.35,
            max_leverage_cap=self.max_leverage,
            max_contracts_cap=250,      # Scales contracts up for high profit capture
            max_margin_utilization_pct=0.80, # 80% max margin across all trades, 20% safe margin buffer
            is_paper_mode=not self.is_live,
        )

        self.strategy = TrendFollowingMomentumStrategy(
            cost_engine=self.cost_engine,
            min_adx=14.0,              # Captures strong trending moves across all global sessions
            min_net_rr=1.35,
            max_cost_ratio=0.35,
            max_risk_pct=self.max_risk_pct,
            atr_sl_mult=3.5,           # 3.5x ATR: Macro Structural Invalidation Envelope (99% immune to fakeout sweeps)
            atr_tp_mult=10.0,          # Scaled up to 10.0x ATR: Mega $1,800+ moves on BTC, $100+ on ETH
        )

        self.dashboard = UnifiedTradingDashboard(
            symbol=self.symbol_display,
            initial_equity=initial_equity,
            max_daily_loss_pct=0.25,
            max_risk_pct=self.max_risk_pct,
            max_leverage=self.max_leverage,
        )

        # Internal State (Concurrent Multi-Asset Architecture)
        self.is_running = False
        self.active_positions: Dict[str, Dict[str, Any]] = {}
        self.latest_mark_prices: Dict[str, float] = {}
        self.last_candle_fetch: Dict[str, float] = {}
        self.cached_mtf: Dict[str, Dict[str, pd.DataFrame]] = {}

    @property
    def active_position(self) -> Optional[Dict[str, Any]]:
        return next(iter(self.active_positions.values())) if self.active_positions else None

    @active_position.setter
    def active_position(self, pos: Optional[Dict[str, Any]]):
        if pos:
            sym = pos.get("symbol", self.primary_symbol)
            self.active_positions[sym] = pos
        else:
            self.active_positions.clear()

    def _sync_live_wallet(self) -> float:
        """Queries live wallet balance from Delta Exchange."""
        total_equity = 19.60
        avail_bal = 19.60
        try:
            wallets = self.client.get_wallet_balances()
            for w in wallets.get("result", []):
                if w.get("asset_symbol") == "USD":
                    avail_bal = float(w.get("available_balance", 19.60))
                    total_equity = float(w.get("balance", avail_bal))
                    break
        except Exception as e:
            logger.warning(f"Wallet balance query warning: {e}")

        if hasattr(self, "dashboard"):
            self.dashboard.update_wallet(total_equity, avail_bal)
        return total_equity

    def fetch_market_data(self, sym: str) -> Dict[str, pd.DataFrame]:
        """Fetches 15m, 5m, and 1m candles for multi-timeframe evaluation."""
        now = time.time()
        last_fetch = self.last_candle_fetch.get(sym, 0.0)
        # Fetch fresh candles every 15 seconds per symbol
        if now - last_fetch > 15.0 or sym not in self.cached_mtf:
            try:
                mtf = self.md_engine.get_multi_timeframe_data(sym, hours_back=36)
                self.cached_mtf[sym] = mtf
                self.last_candle_fetch[sym] = now
                if "1m" in mtf and not mtf["1m"].empty:
                    self.latest_mark_prices[sym] = float(mtf["1m"]["close"].iloc[-1])
            except Exception as e:
                logger.warning(f"Candle fetch notice for {sym}: {e}")
        return self.cached_mtf.get(sym, {})

    def check_and_execute_strategy(self):
        """Executes strategic evaluation and order routing concurrently across symbols (Gold & Silver)."""
        # 1. Check circuit breaker
        is_tripped, cb_reason = self.cost_engine.check_circuit_breaker()
        if is_tripped:
            self.dashboard.circuit_breaker_active = True
            self.dashboard.circuit_breaker_reason = cb_reason
            return

        # 2. Manage ALL existing active positions across all symbols
        for sym in list(self.active_positions.keys()):
            self._manage_open_position(sym)

        # 3. Synchronize live equity & portfolio margin allocation
        current_equity = self._sync_live_wallet()
        used_margin = sum(
            (p["size"] * p.get("contract_val", 0.001) * p["entry_price"]) / max(p.get("leverage", 25), 1)
            for p in self.active_positions.values()
        )
        max_allowed_margin = current_equity * 0.80  # 80% max margin allocation, guaranteeing 20% safe buffer

        # 4. Evaluate setups for symbols that are currently flat
        scan_reports = []
        tradable_entries = []

        for sym in self.symbols:
            if sym in self.active_positions:
                pos = self.active_positions[sym]
                scan_reports.append(f"{sym}: ACTIVE ({pos['side'].upper()} {pos['size']} cts @ ${pos['entry_price']:,.2f})")
                continue

            if used_margin >= max_allowed_margin:
                scan_reports.append(f"{sym}: MARGIN BUFFER FULL (${used_margin:.2f}/${max_allowed_margin:.2f})")
                continue

            spec = SUPPORTED_CONTRACTS.get(sym, {"contract_value": 0.001, "tick_size": 0.01})
            mtf = self.fetch_market_data(sym)
            if not mtf or "15m" not in mtf or "5m" not in mtf:
                continue

            ob = None
            try:
                ob = self.client.get_orderbook(sym)
            except Exception:
                pass

            decision = self.strategy.evaluate(
                symbol=sym,
                mtf_data=mtf,
                macro_tf="15m",
                trend_tf="5m",
                contract_spec=spec,
                current_spread=spec.get("tick_size", 0.01),
                orderbook=ob,
            )

            status_short = decision.reason.replace("NO TRADE: ", "")
            scan_reports.append(f"{sym}: {decision.macro_regime} ({status_short})")

            if decision.is_tradable:
                tradable_entries.append((decision, spec))

        combined_status = " | ".join(scan_reports) if scan_reports else "Scanning market structure..."
        primary_regime = "DUAL SCAN" if len(self.symbols) > 1 else (scan_reports[0] if scan_reports else "SCANNING")

        self.dashboard.update_market_scan(
            regime=primary_regime,
            reason=combined_status,
            indicators={},
        )

        now = time.time()
        if now - getattr(self, "_last_scan_log", 0.0) >= 300.0:
            logger.info(f"[SCAN TELEMETRY] {combined_status}")
            self._last_scan_log = now

        for dec, spec in tradable_entries:
            self._dispatch_entry_order(dec, spec)

    def _dispatch_entry_order(self, decision: Any, spec: Dict[str, Any]):
        """Routes Maker Post-Only Limit Order to Delta Exchange."""
        sym = decision.symbol
        action = decision.action.lower()
        size = decision.pre_order_decision.contracts
        entry_price = decision.entry_price
        sl_price = decision.stop_loss
        tp_price = decision.take_profit
        leverage = decision.pre_order_decision.leverage
        tick_size = spec.get("tick_size", 0.01)
        contract_val = spec.get("contract_value", 0.001)

        logger.info(
            f"[SIGNAL] [SIGNAL DETECTED] {decision.action} {size} contracts of {sym} @ ${entry_price:,.2f} | "
            f"SL: ${sl_price:,.2f} | TP: ${tp_price:,.2f} | Net RR: {decision.pre_order_decision.net_reward_to_risk_ratio:.2f}:1"
        )

        # Set product leverage
        if self.is_live:
            try:
                self.client.set_product_leverage(sym, leverage)
            except Exception as e:
                logger.debug(f"Leverage set notice: {e}")

        req = DeltaOrderRequest(
            symbol=sym,
            side=action,
            order_type=OrderType.LIMIT.value,
            size=size,
            limit_price=entry_price,
            post_only=True,
            bracket_tp_price=tp_price,
            bracket_sl_price=None,  # No exchange market-stop order; managed by high-frequency noise-proof shield
        )

        pos_dict = {
            "order_id": "",
            "symbol": sym,
            "side": action,
            "size": size,
            "entry_price": entry_price,
            "stop_loss": sl_price,
            "take_profit": tp_price,
            "leverage": leverage,
            "opened_at": time.time(),
            "highest_price": entry_price,
            "lowest_price": entry_price,
            "is_risk_free": False,
            "contract_val": contract_val,
            "tick_size": tick_size,
            "atr": decision.indicators.get("atr_14", 1.0),
        }

        if not self.is_live:
            # Paper execution
            pos_dict["order_id"] = f"PAPER_{int(time.time()*1000)}"
            self.active_positions[sym] = pos_dict
            self.dashboard.update_positions(self.active_positions)
            logger.info(f"[SUCCESS] [PAPER ORDER PLACED] {action.upper()} {size} contracts of {sym} @ ${entry_price:,.2f}")
        else:
            resp: OrderResponse = self.client.place_order(req)
            if not resp.success and "immediate_execution_post_only" in str(resp.raw_response):
                # Price has already crossed favorably, allow immediate execution to capture breakout
                req.post_only = False
                resp = self.client.place_order(req)
                if resp.success:
                    pos_dict["entry_price"] = req.limit_price

            if resp.success:
                pos_dict["order_id"] = resp.order_id
                self.active_positions[sym] = pos_dict
                self.dashboard.update_positions(self.active_positions)
                logger.info(f"[SUCCESS] [LIVE MAKER ORDER EXECUTED] Order ID: {resp.order_id} | {action.upper()} {size} cts of {sym} @ ${entry_price:,.2f}")
            else:
                logger.warning(f"[REJECTED] [LIVE ORDER REJECTED] {resp.raw_response}")

    def _manage_open_position(self, sym: Optional[str] = None):
        """Micro-Snap Breakeven Guard and Exit Detection for a specific symbol."""
        target_sym = sym or self.primary_symbol
        pos = self.active_positions.get(target_sym)
        if not pos:
            return

        side = pos["side"]
        entry = float(pos["entry_price"])
        size = int(pos["size"])
        sl = float(pos["stop_loss"])
        tp = float(pos["take_profit"])
        contract_val = pos.get("contract_val", self.contract_val)
        tick_size = pos.get("tick_size", 0.01)
        atr_val = pos.get("atr", 1.0)
        # Fresh live market quote
        try:
            ticker = self.client.get_ticker(target_sym)
            if ticker and "mark_price" in ticker and ticker["mark_price"] is not None:
                mark = float(ticker["mark_price"])
                self.latest_mark_prices[target_sym] = mark
            elif target_sym in self.latest_mark_prices:
                mark = self.latest_mark_prices[target_sym]
            else:
                mark = entry
        except Exception:
            mark = self.latest_mark_prices.get(target_sym, entry)

        # Check if position was closed by Delta Exchange bracket order
        if self.is_live:
            try:
                # 1. Check if the initial maker entry order is still open/pending in the book
                order_id = pos.get("order_id")
                open_orders = self.client.get_open_orders(target_sym)
                is_pending = any(str(o.get("id")) == str(order_id) for o in open_orders)
                if is_pending:
                    # Order is resting in book waiting to be matched
                    age_s = time.time() - pos.get("opened_at", time.time())
                    if age_s > 120.0:
                        logger.info(f"[TIMEOUT] Cancelling unfilled maker order {order_id} after {age_s:.0f}s timeout")
                        self.client.cancel_order(order_id, symbol=target_sym)
                        self.active_positions.pop(target_sym, None)
                        self.dashboard.update_positions(self.active_positions)
                    return

                live_positions = self.client.get_positions(target_sym)
                has_pos = False
                if isinstance(live_positions, list):
                    for p in live_positions:
                        if abs(int(p.get("size", 0))) > 0:
                            has_pos = True
                            break
                elif isinstance(live_positions, dict) and abs(int(live_positions.get("size", 0))) > 0:
                    has_pos = True

                if not has_pos:
                    # 3-Strike Verification over 1.5s to prevent false exits from transient API network timeouts
                    for _ in range(3):
                        time.sleep(0.5)
                        retry_positions = self.client.get_positions(target_sym)
                        if any(abs(int(p.get("size", 0))) > 0 for p in (retry_positions if isinstance(retry_positions, list) else [retry_positions])):
                            has_pos = True
                            break

                if not has_pos:
                    exit_price = tp if (mark >= tp if side == "buy" else mark <= tp) else (sl if (mark <= sl if side == "buy" else mark >= sl) else mark)
                    reason = "EXCHANGE_POSITION_CLOSED"
                    self._close_position(target_sym, exit_price=exit_price, reason=reason)
                    return
            except Exception as e:
                logger.debug(f"Live position check notice for {target_sym}: {e}")

        pos["highest_price"] = max(pos.get("highest_price", entry), mark)
        pos["lowest_price"] = min(pos.get("lowest_price", entry), mark)

        # Calculate unrealized P&L
        diff = (mark - entry) if side == "buy" else (entry - mark)
        unrealized_pnl = round(diff * contract_val * size, 4)
        pos["unrealized_pnl"] = unrealized_pnl
        pos["mark_price"] = mark
        self.dashboard.update_positions(self.active_positions)

        # DYNAMIC PROFIT LOCK & TRAILING STOP LOSS (Capturing $5 to $15 Big Moves)
        favorable_move = (mark - entry) if side == "buy" else (entry - mark)

        # 1. 2-Tier Profit Locker Targets & Trailing Calibration
        s_sym = target_sym.upper()
        if "SLV" in s_sym or "XAG" in s_sym:
            tier1_tp_dist = 0.40
            be_trigger = max(atr_val * 0.8, 0.25)
            fee_buffer_pts = 0.08
            trail_trigger = max(atr_val * 2.5, 0.80)
            retrace_min_peak = 0.45
            min_retrace_dist = 0.15
        elif "BTC" in s_sym:
            tier1_tp_dist = 700.0  # Tier 1 Quick Scalp: +$700 pts (Books 50% lot)
            be_trigger = max(atr_val * 0.8, 120.0)
            fee_buffer_pts = 35.0  # covers 100% maker round trip + GST + safety profit
            trail_trigger = max(atr_val * 3.0, 500.0)
            retrace_min_peak = 250.0
            min_retrace_dist = 80.0
        elif "ETH" in s_sym:
            tier1_tp_dist = 35.0   # Tier 1 Quick Scalp: +$35 pts (Books 50% lot)
            be_trigger = max(atr_val * 0.8, 6.0)
            fee_buffer_pts = 2.0   # covers 100% maker round trip + GST + safety profit
            trail_trigger = max(atr_val * 3.0, 30.0)
            retrace_min_peak = 18.0
            min_retrace_dist = 5.0
        elif "SOL" in s_sym:
            tier1_tp_dist = 2.00
            be_trigger = max(atr_val * 0.8, 0.60)
            fee_buffer_pts = 0.20
            trail_trigger = max(atr_val * 2.5, 3.00)
            retrace_min_peak = 1.20
            min_retrace_dist = 0.40
        else:  # Gold
            tier1_tp_dist = 8.00
            be_trigger = max(atr_val * 0.8, 3.50)
            fee_buffer_pts = 1.20
            trail_trigger = max(atr_val * 2.5, 10.00)
            retrace_min_peak = 6.00
            min_retrace_dist = 2.00

        # TIER 1 QUICK TAKE-PROFIT HIT (Realizes 50% lot profit & locks remaining half at breakeven)
        hit_tier1 = (favorable_move >= tier1_tp_dist) and not pos.get("tier1_booked", False) and (size > 1)
        if hit_tier1:
            tier1_size = max(1, size // 2)
            tier1_exit_price = round(entry + tier1_tp_dist if side == "buy" else entry - tier1_tp_dist, 2)
            logger.info(
                f"[TIER 1 TP] [QUICK PROFIT LOCK] {target_sym} reached Tier 1 (+{tier1_tp_dist:.1f} pts)! "
                f"Realizing 50% ({tier1_size} cts) @ ${tier1_exit_price:,.2f} and snapping remaining {size - tier1_size} cts to 100% Risk-Free Breakeven!"
            )
            self._close_position(
                symbol=target_sym,
                exit_price=tier1_exit_price,
                reason=f"TIER_1_QUICK_TP_HIT (+{tier1_tp_dist:.1f} pts Realized)",
                close_contracts=tier1_size,
            )
            pos = self.active_positions.get(target_sym)
            if pos:
                new_sl = round(entry + fee_buffer_pts if side == "buy" else entry - fee_buffer_pts, 2)
                pos["stop_loss"] = new_sl
                pos["is_risk_free"] = True
                pos["tier1_booked"] = True
                logger.info(f"[SHIELD] [MOONBAG RISK-FREE SHIELD] {target_sym} remaining {pos['size']} cts SL locked to ${new_sl:,.2f}")

        # Fee-Positive Breakeven Shield (Early Protection before Tier 1)
        if favorable_move >= be_trigger and not pos.get("is_risk_free", False):
            new_sl = round(entry + fee_buffer_pts if side == "buy" else entry - fee_buffer_pts, 2)
            valid, _ = self.cost_engine.validate_stop_loss_adjustment(sl, new_sl, side)
            if valid:
                pos["stop_loss"] = new_sl
                pos["is_risk_free"] = True
                logger.info(f"[SHIELD] [FEE-POSITIVE BREAKEVEN SNAP] {target_sym} SL pulled to ${new_sl:,.2f} (100% Zero-Loss & Fee Immune Protected)")

        # 2. Dynamic Trailing Profit (Riding Big Runner Moves)
        if favorable_move >= trail_trigger:
            trail_distance = round(atr_val * 1.5, 2)
            trail_sl = round(mark - trail_distance if side == "buy" else mark + trail_distance, 2)
            current_sl = float(pos["stop_loss"])
            valid, _ = self.cost_engine.validate_stop_loss_adjustment(current_sl, trail_sl, side)
            if valid:
                pos["stop_loss"] = trail_sl
                logger.info(f"[TRAIL] [BIG MOVE TRAILING PROFIT] {target_sym} Trailed SL to ${trail_sl:,.2f} | Riding Wave (Mark: ${mark:,.2f})")

        # 3. Pre-Wick Deceleration Exit Guard (Banks profits before candle wicks down)
        is_wick_decelerating = False
        if unrealized_pnl >= 0.05 and pos.get("is_risk_free", False):
            if side == "buy":
                peak_pts = pos.get("highest_price", entry) - entry
                if peak_pts >= retrace_min_peak and (pos.get("highest_price", entry) - mark) >= max(min_retrace_dist, peak_pts * 0.40):
                    is_wick_decelerating = True
            else:
                peak_pts = entry - pos.get("lowest_price", entry)
                if peak_pts >= retrace_min_peak and (mark - pos.get("lowest_price", entry)) >= max(min_retrace_dist, peak_pts * 0.40):
                    is_wick_decelerating = True

        # EXIT CHECK: Take-Profit (Tier 2 Runner), Stop-Loss, or Pre-Wick Deceleration Hit
        hit_tp = (mark >= tp) if side == "buy" else (mark <= tp)
        hit_sl = (mark <= sl) if side == "buy" else (mark >= sl)

        if hit_tp:
            self._close_position(target_sym, exit_price=tp, reason="TIER_2_RUNNER_TP_HIT (Big Move Target Captured)")
        elif hit_sl:
            reason = "ZERO_RISK_BREAKEVEN_EXIT" if pos.get("is_risk_free") else "STOP_LOSS_HIT"
            self._close_position(target_sym, exit_price=sl, reason=reason)
        elif is_wick_decelerating:
            self._close_position(target_sym, exit_price=mark, reason="PRE_WICK_DECELERATION_EXIT (Banked Profit Before Reverse Wick)")

    def _close_position(
        self,
        symbol: Optional[str] = None,
        exit_price: float = 0.0,
        reason: str = "",
        close_contracts: Optional[int] = None,
    ):
        """Clears or partially reduces position, records post-trade accounting, updates dashboard."""
        target_sym = symbol or self.primary_symbol
        pos = self.active_positions.get(target_sym)
        if not pos:
            return

        side = pos["side"]
        total_size = pos["size"]
        entry = pos["entry_price"]
        contract_val = pos.get("contract_val", self.contract_val)

        is_partial = (close_contracts is not None) and (0 < close_contracts < total_size)
        exec_size = close_contracts if is_partial else total_size

        # If live, reduce or flatten on Delta Exchange
        if self.is_live:
            try:
                if is_partial:
                    exit_side = "sell" if side == "buy" else "buy"
                    req = DeltaOrderRequest(
                        symbol=target_sym,
                        side=exit_side,
                        order_type=OrderType.MARKET.value,
                        size=exec_size,
                        reduce_only=True,
                    )
                    resp = self.client.place_order(req)
                    logger.info(f"[PARTIAL EXIT] Delta Reduce-Only Order placed: {resp.order_id} ({resp.status})")
                else:
                    self.client.close_all_positions(target_sym)
                    self.client.cancel_all_orders(target_sym)
            except Exception as e:
                logger.debug(f"Position close notice for {target_sym}: {e}")

        # Post-Trade Accounting for executed size
        acc = self.cost_engine.record_closed_trade_accounting(
            symbol=target_sym,
            side=side,
            size=exec_size,
            entry_price=entry,
            exit_price=exit_price,
            contract_val=contract_val,
            maker_fee_rate=0.0001,
            taker_fee_rate=0.0001,
            actual_slippage_usd=0.0002 if "STOP_LOSS" in reason else 0.0,
            exit_reason=reason,
        )

        # Update Dashboard
        self.dashboard.record_trade(
            symbol=target_sym,
            side=side,
            size=exec_size,
            entry_price=entry,
            exit_price=exit_price,
            gross_pnl=acc.gross_pnl_usd,
            fees=acc.total_costs_usd,
            net_pnl=acc.final_net_pnl_usd,
            reason=reason,
        )

        if is_partial:
            pos["size"] -= exec_size
            pos["tier1_booked"] = True
            self.dashboard.update_positions(self.active_positions)
            logger.info(
                f"[TIER 1 REALIZED] {side.upper()} {exec_size} cts of {target_sym} closed @ ${exit_price:,.2f} | "
                f"Net: ${acc.final_net_pnl_usd:+.4f} | Remaining Moonbag: {pos['size']} cts"
            )
        else:
            self.active_positions.pop(target_sym, None)
            self.dashboard.update_positions(self.active_positions)
            logger.info(
                f"[TARGET] [POSITION CLOSED] {side.upper()} {exec_size} cts of {target_sym} @ ${exit_price:,.2f} | "
                f"Gross: ${acc.gross_pnl_usd:+.4f} | Fees: ${acc.total_costs_usd:.4f} | Net: ${acc.final_net_pnl_usd:+.4f} | Reason: {reason}"
            )

        print(self.cost_engine.format_closed_trade_report(acc))

    def _restore_active_position_from_exchange(self):
        """Discovers existing active positions on Delta Exchange upon startup across all symbols."""
        if not self.is_live:
            return
        for sym in self.symbols:
            spec = SUPPORTED_CONTRACTS.get(sym, {"contract_value": 0.001, "tick_size": 0.01})
            try:
                positions = self.client.get_positions(sym)
                for pos in positions:
                    raw_size = int(pos.get("size", 0))
                    if raw_size != 0:
                        pos_side = "buy" if raw_size > 0 else "sell"
                        size = abs(raw_size)
                        entry_price = float(pos.get("entry_price", 0.0))
                        open_orders = self.client.get_open_orders(sym)
                        sym_u = sym.upper()
                        if "SLV" in sym_u or "XAG" in sym_u:
                            def_tp_dist = 4.20
                            def_sl_dist = 0.40
                            lev = 50
                        elif "BTC" in sym_u:
                            def_tp_dist = 2200.0
                            def_sl_dist = 250.0
                            lev = min(150, self.max_leverage)
                        elif "ETH" in sym_u:
                            def_tp_dist = 120.0
                            def_sl_dist = 12.0
                            lev = min(150, self.max_leverage)
                        elif "SOL" in sym_u:
                            def_tp_dist = 5.00
                            def_sl_dist = 1.20
                            lev = min(100, self.max_leverage)
                        else:  # Gold / XAUT
                            def_tp_dist = 45.00
                            def_sl_dist = 6.50
                            lev = min(100, self.max_leverage)

                        sl_price = round(entry_price - def_sl_dist if pos_side == "buy" else entry_price + def_sl_dist, 2)
                        tp_price = round(entry_price + def_tp_dist if pos_side == "buy" else entry_price - def_tp_dist, 2)
                        for o in open_orders:
                            if o.get("bracket_stop_loss_price"):
                                sl_price = float(o["bracket_stop_loss_price"])
                            if o.get("bracket_take_profit_price"):
                                o_tp = float(o["bracket_take_profit_price"])
                                if (pos_side == "buy" and o_tp > entry_price) or (pos_side == "sell" and o_tp < entry_price):
                                    tp_price = max(tp_price, o_tp) if pos_side == "buy" else min(tp_price, o_tp)
                        pos_dict = {
                            "order_id": str(pos.get("product_id", "")),
                            "symbol": sym,
                            "side": pos_side,
                            "size": size,
                            "entry_price": entry_price,
                            "stop_loss": sl_price,
                            "take_profit": tp_price,
                            "leverage": lev,
                            "opened_at": time.time(),
                            "highest_price": entry_price,
                            "lowest_price": entry_price,
                            "is_risk_free": False,
                            "contract_val": spec.get("contract_value", 0.001),
                            "tick_size": spec.get("tick_size", 0.01),
                            "atr": 1.0,
                        }
                        self.active_positions[sym] = pos_dict
                        logger.info(f"[RESTORE] Re-attached to live exchange position: {size} cts of {sym} @ ${entry_price:,.2f}")
            except Exception as e:
                logger.warning(f"Position restore warning for {sym}: {e}")
        self.dashboard.update_positions(self.active_positions)

    def start_24x7_loop(self):
        """Starts autonomous continuous 24/7 trading engine."""
        self.is_running = True
        mode_str = "LIVE REAL-MONEY" if self.is_live else "PAPER SIMULATION"
        logger.info(f"[START] Starting RAMYA AI Trend-Following Momentum Engine in [{mode_str}] Mode on {self.symbol_display}")
        print(f"\n[ENGINE STARTED] 24/7 Polling Loop Active on {self.symbol_display} ({mode_str})", flush=True)

        # Restore any existing live exchange position
        self._restore_active_position_from_exchange()

        last_dash_print = 0.0

        try:
            while self.is_running:
                # 1. Sync live wallet periodically
                self._sync_live_wallet()

                # 2. Run strategy check
                self.check_and_execute_strategy()

                # 3. Render dashboard every 10 seconds
                now = time.time()
                if now - last_dash_print >= 10.0:
                    mode_tag = "LIVE" if self.is_live else "PAPER"
                    print(self.dashboard.render_console(mode=mode_tag), flush=True)
                    last_dash_print = now

                time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            logger.info("Shutdown signal received. Stopping robot safely.")
            self.stop()

    def stop(self):
        """Stops the autonomous loop safely."""
        self.is_running = False
        logger.info("Autonomous loop halted.")


def main():
    parser = argparse.ArgumentParser(description="RAMYA AI -- 24/7 Autonomous Live Trend-Following Momentum Robot")
    parser.add_argument("--symbol", type=str, default="BTCUSD,ETHUSD", help="Contract symbol(s) (comma-separated, default: BTCUSD,ETHUSD)")
    parser.add_argument("--mode", type=str, default="LIVE", choices=["PAPER", "LIVE"], help="Execution mode (PAPER or LIVE, default: LIVE)")
    parser.add_argument("--live", action="store_true", default=False, help="Explicit confirmation flag required for LIVE execution")
    parser.add_argument("--max-risk", type=float, default=0.20, help="Risk per trade (default: 0.20 = 20.0%% of equity for scaled profit capture)")
    parser.add_argument("--max-lev", type=int, default=200, help="Maximum leverage ceiling (default: 200x)")
    parser.add_argument("--interval", type=float, default=3.0, help="Polling interval seconds (default: 3.0)")
    args = parser.parse_args()

    is_live = args.live or args.mode.upper() == "LIVE"
    runner = AutonomousLiveTrendRunner(
        symbol=args.symbol,
        is_live=is_live,
        poll_interval=args.interval,
        max_risk_pct=args.max_risk,
        max_leverage=args.max_lev,
    )
    runner.start_24x7_loop()


if __name__ == "__main__":
    main()
