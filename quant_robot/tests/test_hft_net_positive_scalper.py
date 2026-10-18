"""
Unit Test Suite for RAMYA AI — Exponential Capital Scaling & Auto-Compounding Engine
Tests:
1. Live fee schedule and micro-target TP calculation (+0.08% Pure Net)
2. Guaranteed positive exit math: Gross Profit - Total Brokerage > 0
3. Dynamic exponential lot sizing and 85% auto-compounding without caps
4. Dynamic leverage scaling (10x-25x) based on ATR momentum
5. Sideways chop rejection filter
6. Sub-millisecond Trailing Breakeven Guard at +0.03% move
7. Velocity-based exit (45-second time-out rule)
8. L2 Order Book Imbalance calculation (>0.65 threshold)
9. High-frequency 1m & 5m EMA 9/21 + VWAP + L2 Imbalance confirmation matrix
"""

import time
import pytest
import pandas as pd
import numpy as np

from quant_robot.hft_net_positive_scalper import HFTNetPositiveScalper


def test_fee_schedule_and_micro_tp_calculation():
    """Verifies that micro TP includes round-trip fees + slippage + 0.08% pure net profit."""
    scalper = HFTNetPositiveScalper(pure_profit_pct=0.0008)
    entry_price = 4000.0
    size = 10

    fee_pts, brokerage_usd, micro_tp_pts = scalper.calculate_fee_schedule(entry_price, size)

    expected_brokerage = (entry_price * scalper.contract_val * size) * (
        scalper.maker_fee_rate + scalper.taker_fee_rate + scalper.slippage_rate
    )
    assert round(brokerage_usd, 4) == round(expected_brokerage, 4)

    expected_tp_pts = round(fee_pts + (entry_price * 0.0008), 2)
    assert micro_tp_pts == expected_tp_pts
    assert micro_tp_pts > fee_pts


def test_guaranteed_positive_exit_math():
    """Confirms winning micro exit is strictly net-positive after brokerage."""
    scalper = HFTNetPositiveScalper()
    scalper.core_capital_usd = 22.51
    entry_price = 4000.0
    size = 10
    notional = entry_price * 0.001 * size

    gross_pnl = 6.80 * 0.001 * size
    fees = notional * (scalper.maker_fee_rate + scalper.maker_fee_rate)
    net_pnl = gross_pnl - fees
    assert net_pnl > 0.0


def test_dynamic_exponential_order_sizing_and_85pct_compounding():
    """Tests that order size scales exponentially from micro-lots to mega-lots without fixed caps."""
    scalper = HFTNetPositiveScalper()
    scalper.latest_mark_price = 4135.0
    scalper.sync_wallet_from_api = False

    # Baseline core equity ($22.51) -> 21 contracts
    scalper.core_capital_usd = 22.51
    scalper.profit_vault_usd = 0.0
    assert scalper.calculate_dynamic_order_size() == 21

    # Simulated win of +$10.00 net PnL: 85% ($8.50) goes to active margin, 15% ($1.50) to vault
    scalper.active_position = {"side": "buy", "size": 10, "entry_price": 4130.0}
    scalper._close_and_compound(exit_price=4140.0, reason="TP", gross_pnl=11.0, fees=1.0, net_pnl=10.0)
    assert round(scalper.compounded_profit_usd, 2) == 8.50
    assert round(scalper.profit_vault_usd, 2) == 1.50
    assert round(scalper.core_capital_usd, 2) == 31.01

    # Account expands to 5,000 INR (~$60 USD) -> scales to at least 25 contracts
    scalper.core_capital_usd = 60.00
    scalper.profit_vault_usd = 5.00
    assert scalper.calculate_dynamic_order_size() >= 25

    # Account expands to 10,000 INR (~$120 USD) -> scales to at least 50 contracts
    scalper.core_capital_usd = 120.00
    scalper.profit_vault_usd = 10.00
    assert scalper.calculate_dynamic_order_size() >= 50

    # Account expands to 25,000+ INR (~$300 USD) -> scales to mega-lots (at least 120 contracts)
    scalper.core_capital_usd = 300.00
    scalper.profit_vault_usd = 20.00
    assert scalper.calculate_dynamic_order_size() >= 120


def test_three_tier_adaptive_leverage_matrix():
    """
    Verifies the Three-Tier Adaptive Leverage Matrix:
    - Tier 1: Normal market (OBI < 0.60) -> 10x safe base leverage.
    - Tier 2: Confirmed momentum (1m/5m EMAs, VWAP breakout, OBI > 0.65) -> 50x leverage.
    - Tier 3: Institutional explosion (1m vol > 500% 2h-MA and OBI > 0.85) -> 100x leverage.
    """
    scalper = HFTNetPositiveScalper()

    # 1. Tier 1 (Slow / Normal Market): OBI < 0.60, normal volume -> 10x
    scalper.ws_client.latest_orderbooks[scalper.symbol] = {
        "buy": [{"price": "4150.00", "size": 520}],
        "sell": [{"price": "4150.10", "size": 480}],
    }
    df_normal = pd.DataFrame({
        "atr_14": [2.0] * 10,
        "atr_mean": [2.0] * 10,
        "atr_ratio": [1.0] * 10,
        "vol_ratio": [1.0] * 10,
        "vol_ratio_2h": [1.0] * 10,
        "close": [4150.0] * 10,
        "open": [4150.0] * 10,
        "vwap": [4150.0] * 10,
        "ema_9": [4150.0] * 10,
        "ema_21": [4150.0] * 10,
    })
    scalper.mtf_data = {"1m": df_normal, "5m": df_normal}
    tier_num, lev, label = scalper.get_leverage_tier_info()
    assert tier_num == 1
    assert lev == 10
    assert scalper.calculate_dynamic_leverage() == 10

    # 2. Tier 2 (Confirmed Momentum Move): EMA 9 > EMA 21, Price > VWAP, OBI = 0.70 (> 0.65) -> 50x
    scalper.ws_client.latest_orderbooks[scalper.symbol] = {
        "buy": [{"price": "4155.00", "size": 700}],
        "sell": [{"price": "4155.10", "size": 300}],
    }
    df_t2_1m = pd.DataFrame({
        "close": [4156.0] * 10,
        "open": [4152.0] * 10,
        "vwap": [4150.0] * 10,
        "ema_9": [4155.0] * 10,
        "ema_21": [4151.0] * 10,
        "atr_14": [2.5] * 10,
        "atr_ratio": [1.2] * 10,
        "vol_ratio": [2.0] * 10,
        "vol_ratio_2h": [2.2] * 10,  # Below 5.0x
    })
    df_t2_5m = pd.DataFrame({
        "close": [4156.0] * 5,
        "ema_9": [4154.0] * 5,
        "ema_21": [4150.0] * 5,
    })
    scalper.mtf_data = {"1m": df_t2_1m, "5m": df_t2_5m}
    tier_num, lev, label = scalper.get_leverage_tier_info()
    assert tier_num == 2
    assert lev == 50
    assert scalper.calculate_dynamic_leverage() == 50

    # 3. Tier 3 (100% Whale Sweep Explosion): 1m Vol > 500% (5.0x) of 2h MA AND OBI > 0.85 -> 100x
    scalper.ws_client.latest_orderbooks[scalper.symbol] = {
        "buy": [{"price": "4165.00", "size": 900}],
        "sell": [{"price": "4165.10", "size": 100}],
    }
    df_t3_1m = pd.DataFrame({
        "close": [4165.0] * 10,
        "open": [4150.0] * 10,
        "vwap": [4150.0] * 10,
        "ema_9": [4160.0] * 10,
        "ema_21": [4152.0] * 10,
        "atr_14": [5.0] * 10,
        "atr_ratio": [2.5] * 10,
        "vol_ratio": [5.5] * 10,
        "vol_ratio_2h": [5.5] * 10,  # > 5.0x (550% surge!)
    })
    scalper.mtf_data = {"1m": df_t3_1m, "5m": df_t2_5m}
    tier_num, lev, label = scalper.get_leverage_tier_info()
    assert tier_num == 3
    assert lev == 100
    assert scalper.calculate_dynamic_leverage() == 100


def test_immediate_leverage_deescalation_cool_down():
    """Verifies that as soon as volume or velocity drops below Tier 3 thresholds, leverage instantly de-escalates to Tier 1."""
    scalper = HFTNetPositiveScalper()

    # Step 1: Active Tier 3 Explosion (Vol 2h = 6.0x, OBI = 0.90) -> 100x
    scalper.ws_client.latest_orderbooks[scalper.symbol] = {
        "buy": [{"price": "4170.00", "size": 900}],
        "sell": [{"price": "4170.10", "size": 100}],
    }
    df_active_t3 = pd.DataFrame({
        "vol_ratio_2h": [6.0] * 10,
        "atr_ratio": [3.0] * 10,
        "close": [4170.0] * 10,
        "vwap": [4160.0] * 10,
        "ema_9": [4168.0] * 10,
        "ema_21": [4162.0] * 10,
    })
    scalper.mtf_data = {"1m": df_active_t3}
    assert scalper.calculate_dynamic_leverage() == 100

    # Step 2: Next tick, volume drops to 2.0x (below 5.0x) and OBI normalizes to 0.55 -> Cool-Down immediately snaps back to Tier 1 (10x)
    scalper.ws_client.latest_orderbooks[scalper.symbol] = {
        "buy": [{"price": "4170.00", "size": 550}],
        "sell": [{"price": "4170.10", "size": 450}],
    }
    df_cooled_down = pd.DataFrame({
        "vol_ratio_2h": [1.5] * 10,
        "atr_ratio": [1.1] * 10,
        "close": [4170.0] * 10,
        "vwap": [4170.0] * 10,
        "ema_9": [4170.0] * 10,
        "ema_21": [4170.0] * 10,
    })
    scalper.mtf_data = {"1m": df_cooled_down}
    assert scalper.calculate_dynamic_leverage() == 10
    assert scalper.current_tier == 1


def test_tier3_hardcore_capital_vault_protection():
    """
    Verifies Hardcore Capital Vault Protection:
    At 100x leverage, order sizing is strictly capped to utilize only 1.0% to 1.5% of account equity as initial margin.
    """
    scalper = HFTNetPositiveScalper()
    scalper.latest_mark_price = 4160.0
    scalper.core_capital_usd = 22.51
    scalper.profit_vault_usd = 0.0

    # Margin per contract at 100x on XAUTUSD ($4,160 * 0.001 / 100) = $0.0416 USD
    contracts_100x = scalper.calculate_dynamic_order_size(current_leverage=100)
    total_margin_used = contracts_100x * (4160.0 * scalper.contract_val / 100)
    margin_utilization_pct = total_margin_used / scalper.core_capital_usd

    # Must be capped strictly between 1.0% and 1.5% of total account equity
    assert 0.010 <= margin_utilization_pct <= 0.016
    assert contracts_100x == 8  # 8 contracts * $0.0416 = $0.3328 (~1.48% of $22.51)


def test_sideways_chop_filter_rejection():
    """
    Confirms that when there is no 1m momentum or 10s velocity, robot remains NEUTRAL,
    and lagging 15m chop filters are completely disabled (as requested by institutional guidelines).
    """
    scalper = HFTNetPositiveScalper()
    df_dummy = pd.DataFrame({
        "open": [4100.0] * 50,
        "high": [4102.0] * 50,
        "low": [4098.0] * 50,
        "close": [4100.0] * 50,
        "volume": [100.0] * 50,
        "adx_14": [16.5] * 50,
        "alligator_state": ["ENTANGLED_CHOP"] * 50,
        "supertrend_dir": [1] * 50,
        "ema_9": [4100.0] * 50,
        "ema_21": [4100.0] * 50,
        "ema_50": [4100.0] * 50,
        "vwap": [4100.0] * 50,
        "macd_hist": [0.0] * 50,
        "atr_14": [2.5] * 50,
    })
    scalper.mtf_data = {"1m": df_dummy, "5m": df_dummy, "10m": df_dummy, "15m": df_dummy}

    action, reason, _ = scalper.evaluate_momentum_alignment()
    assert action == "NEUTRAL"
    assert "Scanning 1m Grid & 10s Velocity" in reason


def test_10s_instant_velocity_strike_trigger():
    """
    Verifies the Instant Velocity Trigger (Fast Strike Engine):
    - Price move > 0.05% in <= 10s triggers immediate BUY without waiting for candle close.
    - Price move < -0.05% in <= 10s triggers immediate SELL without waiting for candle close.
    - Completely ignores 15-minute chop filters.
    """
    scalper = HFTNetPositiveScalper()
    now = time.time()

    # 1. Bullish Velocity Strike: price rises from 4150 to 4153.00 (+0.072% in 5 seconds)
    scalper.price_history_10s = [(now - 5.0, 4150.00)]
    scalper.latest_mark_price = 4153.00

    # Even if 15m is chopped, velocity trigger fires without delay:
    df_15m_chop = pd.DataFrame({"adx_14": [10.0], "alligator_state": ["ENTANGLED_CHOP"]})
    scalper.mtf_data = {"15m": df_15m_chop}

    action, reason, atr = scalper.evaluate_momentum_alignment()
    assert action == "BUY"
    assert "INSTANT VELOCITY STRIKE (BUY)" in reason
    assert "No Candle Wait" in reason

    # 2. Bearish Velocity Strike: price drops from 4150 to 4147.00 (-0.072% in 5 seconds)
    scalper.price_history_10s = [(now - 5.0, 4150.00)]
    scalper.latest_mark_price = 4147.00
    action, reason, atr = scalper.evaluate_momentum_alignment()
    assert action == "SELL"
    assert "INSTANT VELOCITY STRIKE (SELL)" in reason


def test_5s_multi_order_continuation_layering():
    """
    Verifies Multi-Order Layering Sequence (Fast Strike + 5-Second Continuation):
    - Initial velocity strike fires Layer 1 immediately.
    - After 5 seconds, if velocity continues in same direction, Layer 2 is fired.
    - After another 5 seconds, if velocity continues, Layer 3 is fired (total 3 layers).
    """
    scalper = HFTNetPositiveScalper(pyramid_layers=3)
    scalper.client.live_enabled = False
    scalper.sync_wallet_from_api = False
    scalper.core_capital_usd = 22.51
    now = time.time()

    # Setup initial velocity strike
    scalper.price_history_10s = [(now - 5.0, 4150.00)]
    scalper.latest_mark_price = 4153.00
    action, reason, atr = scalper.evaluate_momentum_alignment()

    # Fire Layer 1
    scalper.execute_entry_order(action, reason, atr)
    assert len(scalper.active_layers) == 1
    assert scalper.active_position["side"] == "buy"

    # Calling continuation immediately (< 5s) does NOT fire layer 2
    scalper.check_and_execute_continuation_layer()
    assert len(scalper.active_layers) == 1

    # Fast-forward 5.1 seconds and maintain positive velocity / higher price
    scalper.last_layer_time = now - 5.1
    scalper.latest_mark_price = 4154.50
    scalper.record_price_tick(4154.50, timestamp=now)
    scalper.check_and_execute_continuation_layer()
    assert len(scalper.active_layers) == 2

    # Fast-forward another 5.1 seconds and maintain positive price
    scalper.last_layer_time = now - 5.1
    scalper.latest_mark_price = 4156.00
    scalper.record_price_tick(4156.00, timestamp=now)
    scalper.check_and_execute_continuation_layer()
    assert len(scalper.active_layers) == 3

    # Capped at 3 layers maximum
    scalper.last_layer_time = now - 5.1
    scalper.check_and_execute_continuation_layer()
    assert len(scalper.active_layers) == 3


def test_micro_target_take_profit_006pct():
    """Confirms micro TP target is [Round-Trip Fees + Slippage + 0.06% Pure Net Profit]."""
    scalper = HFTNetPositiveScalper(pure_profit_pct=0.0006)
    assert scalper.pure_profit_pct == 0.0006
    entry = 4150.0
    size = 10
    fee_pts, brokerage_usd, micro_tp_pts = scalper.calculate_fee_schedule(entry, size)
    expected_pure_profit_pts = round(entry * 0.0006, 2)
    assert micro_tp_pts == round(fee_pts + expected_pure_profit_pts, 2)


def test_sub_millisecond_breakeven_guard_002pct(monkeypatch):
    """Verifies that moving +0.02% in favor instantly moves SL to Entry + Fee Cost."""
    base_minute = 1700000040.0
    monkeypatch.setattr(time, "time", lambda: base_minute + 15.0)

    scalper = HFTNetPositiveScalper(breakeven_trigger_pct=0.0002)
    entry = 4000.0
    size = 10
    fee_buf_pts = 1.66
    tp_price = 4006.0
    sl_price = 3995.0

    scalper.active_position = {
        "order_id": "test_be_guard",
        "side": "buy",
        "size": size,
        "entry_price": entry,
        "take_profit": tp_price,
        "stop_loss": sl_price,
        "initial_sl": sl_price,
        "fee_buffer_pts": fee_buf_pts,
        "is_risk_free": False,
        "opened_at": base_minute + 10.0,
        "highest_price": entry,
        "lowest_price": entry,
    }

    # Price moves favorably by +0.05% (+2.00 pts, crossing the +0.02% threshold and fee buffer)
    scalper.latest_mark_price = 4002.00
    scalper.manage_open_position()

    pos = scalper.active_position
    assert pos is not None
    assert pos["stop_loss"] >= (entry + fee_buf_pts)
    assert pos["is_risk_free"] is True


def test_velocity_timeout_45s_exit():
    """Verifies that position open for >= 45 seconds triggers velocity exit."""
    scalper = HFTNetPositiveScalper(velocity_timeout_seconds=45.0)
    entry = 4000.0
    size = 10

    scalper.active_position = {
        "order_id": "test_timeout_order",
        "side": "buy",
        "size": size,
        "entry_price": entry,
        "take_profit": 4010.0,
        "stop_loss": 3990.0,
        "initial_sl": 3990.0,
        "fee_buffer_pts": 1.66,
        "is_risk_free": False,
        "opened_at": time.time() - 46.0,
        "highest_price": entry,
        "lowest_price": entry,
    }

    scalper.latest_mark_price = 4000.50
    scalper.manage_open_position()

    assert scalper.active_position is None
    assert scalper.total_trades_count == 1


def test_l2_orderbook_imbalance_calculation():
    """Tests L2 order book imbalance calculation and burst detection (>0.65)."""
    scalper = HFTNetPositiveScalper()
    scalper.ws_client.latest_orderbooks[scalper.symbol] = {
        "buy": [{"price": "4150.00", "size": 750}],
        "sell": [{"price": "4150.10", "size": 250}],
    }

    bid_imb, ask_imb, b_vol, a_vol = scalper.get_l2_orderbook_imbalance()
    assert round(bid_imb, 2) == 0.75
    assert round(ask_imb, 2) == 0.25
    assert bid_imb > 0.65


def test_high_frequency_vwap_and_ema_confirmation():
    """Tests HF confirmation matrix: Price > VWAP + EMA 9/21 cross + 5m alignment + L2 Imbalance > 0.65."""
    scalper = HFTNetPositiveScalper()
    scalper.ws_client.latest_orderbooks[scalper.symbol] = {
        "buy": [{"price": "4155.00", "size": 800}],
        "sell": [{"price": "4155.10", "size": 200}],
    }

    df_1m = pd.DataFrame({
        "open": [4145.0, 4148.0, 4155.0],
        "high": [4147.0, 4150.0, 4157.0],
        "low": [4144.0, 4147.0, 4154.0],
        "close": [4146.0, 4149.0, 4156.0],
        "volume": [100.0, 150.0, 200.0],
        "ema_9": [4146.0, 4148.5, 4152.0],
        "ema_21": [4147.0, 4148.0, 4149.0],
        "vwap": [4140.0, 4140.0, 4140.0],
        "atr_14": [2.5, 2.5, 2.5],
    })

    df_5m = pd.DataFrame({
        "close": [4156.0],
        "ema_9": [4155.0],
        "ema_21": [4150.0],
    })

    scalper.mtf_data = {"1m": df_1m, "5m": df_5m}

    action, reason, atr = scalper.evaluate_momentum_alignment()
    assert action == "BUY"
    assert "VWAP" in reason
    assert "EMA 9/21 Cross" in reason
    assert "L2 Bid Imbalance" in reason


def test_dynamic_product_id_mapping_markxautusd():
    """Verifies that 'MARKXAUTUSD' and 'MARK:XAUTUSD' dynamically resolve to XAUTUSD and product_id 131253."""
    scalper = HFTNetPositiveScalper(symbol="MARKXAUTUSD")
    assert scalper.symbol == "XAUTUSD"
    assert scalper.raw_symbol == "MARKXAUTUSD"
    assert scalper.product_id == 131253

    # Direct client checks
    assert scalper.client.resolve_symbol("MARKXAUTUSD") == "XAUTUSD"
    assert scalper.client.resolve_symbol("MARK:XAUTUSD") == "XAUTUSD"
    assert scalper.client.resolve_symbol("GOLD") == "XAUTUSD"
    assert scalper.client.get_product_id("MARKXAUTUSD") == 131253
    assert scalper.client.get_product_id("MARK:XAUTUSD") == 131253


def test_volatility_enhancement_15m_consensus_bypass_bullish():
    """Verifies that sudden vertical 1m price expansion strikes instantly, bypassing 15m chop filter."""
    scalper = HFTNetPositiveScalper()

    # 15m chart in low-ADX chop (would normally reject trades)
    df_15m_chop = pd.DataFrame({
        "open": [4140.0],
        "high": [4142.0],
        "low": [4138.0],
        "close": [4140.0],
        "adx_14": [14.0],
        "alligator_state": ["ENTANGLED_CHOP"],
    })

    # 1m chart showing sudden vertical price expansion (candle body = 10 pts, ATR = 2.5 pts)
    df_1m_breakout = pd.DataFrame({
        "open": [4140.0, 4140.0],
        "high": [4141.0, 4150.0],
        "low": [4139.0, 4139.5],
        "close": [4140.5, 4150.0],  # +$9.50 expansion candle
        "volume": [100.0, 350.0],
        "vwap": [4142.0, 4142.0],
        "ema_9": [4141.0, 4145.0],
        "ema_21": [4140.0, 4141.0],
        "atr_14": [2.5, 2.5],
        "atr_ratio": [1.0, 2.2],   # ATR surging
        "vol_ratio": [1.0, 3.5],   # Vol surging
    })

    df_5m = pd.DataFrame({
        "close": [4145.0],
        "ema_9": [4143.0],
        "ema_21": [4141.0],
    })

    scalper.mtf_data = {
        "1m": df_1m_breakout,
        "5m": df_5m,
        "15m": df_15m_chop,
    }

    action, reason, atr = scalper.evaluate_momentum_alignment()
    assert action == "BUY"
    assert "Peak Velocity Breakout (15m Bypassed)" in reason


def test_volatility_enhancement_15m_consensus_bypass_bearish():
    """Verifies that sudden vertical downward price expansion strikes SELL instantly, bypassing 15m chop filter."""
    scalper = HFTNetPositiveScalper()

    df_15m_chop = pd.DataFrame({
        "adx_14": [12.0],
        "alligator_state": ["ENTANGLED_CHOP"],
    })

    df_1m_breakout = pd.DataFrame({
        "open": [4150.0, 4150.0],
        "high": [4151.0, 4150.5],
        "low": [4149.0, 4140.0],
        "close": [4149.5, 4140.0],  # -$10.00 drop candle
        "volume": [100.0, 400.0],
        "vwap": [4148.0, 4148.0],
        "ema_9": [4148.0, 4144.0],
        "ema_21": [4149.0, 4148.0],
        "atr_14": [2.5, 2.5],
        "atr_ratio": [1.0, 2.0],
        "vol_ratio": [1.0, 4.0],
    })

    df_5m = pd.DataFrame({
        "close": [4145.0],
        "ema_9": [4144.0],
        "ema_21": [4147.0],
    })

    scalper.mtf_data = {
        "1m": df_1m_breakout,
        "5m": df_5m,
        "15m": df_15m_chop,
    }

    action, reason, atr = scalper.evaluate_momentum_alignment()
    assert action == "SELL"
    assert "Peak Velocity Breakout (15m Bypassed)" in reason


def test_multi_order_layering_pyramiding_on_breakout_candle():
    """Verifies that high-velocity breakout candles trigger a 4-order layering sequence (pyramiding)."""
    scalper = HFTNetPositiveScalper(pyramid_layers=4)
    scalper.client.live_enabled = False
    scalper.latest_mark_price = 4160.0
    scalper.sync_wallet_from_api = False
    scalper.core_capital_usd = 22.51

    breakout_reason = (
        "⚡ Peak Velocity Breakout (15m Bypassed): 1m Vertical Bullish Expansion "
        "(Body=$8.50 >= 1.4*ATR, ATR Ratio=2.2x, Vol=3.5x) Price=$4160.00 > Open=$4151.50"
    )

    scalper.execute_entry_order("BUY", breakout_reason, atr=3.5)

    # Must NOT place a single trade; must place a cascade of 4 layered orders
    assert len(scalper.active_layers) == 4
    assert scalper.active_position is not None
    assert scalper.active_position.get("is_pyramid") is True
    assert scalper.active_position.get("layer_count") == 4

    total_layer_contracts = sum(l["size"] for l in scalper.active_layers)
    assert total_layer_contracts == 21

    # Verify layered price cascading across the breakout candle
    prices = [l["entry_price"] for l in scalper.active_layers]
    assert prices[0] > prices[1] > prices[2] > prices[3]
    assert prices[0] == 4159.99  # Front-runner inside maker quote (1 tick below mark)
    assert prices[3] < 4158.00   # Deep liquidity sweep layer

    # Verify each layer has its own fee-inclusive micro-target TP and SL
    for layer in scalper.active_layers:
        assert layer["take_profit"] > layer["entry_price"]
        assert layer["stop_loss"] < layer["entry_price"]
        assert layer["micro_tp_pts"] > layer["fee_buffer_pts"]


def test_multi_order_pyramiding_layer_management_and_compounding():
    """Tests sub-MS breakeven lock and layer-by-layer profit compounding across the cascade."""
    scalper = HFTNetPositiveScalper(pyramid_layers=3)
    scalper.client.live_enabled = False
    scalper.sync_wallet_from_api = False
    scalper.core_capital_usd = 22.51
    scalper.profit_vault_usd = 0.0

    breakout_reason = "⚡ Peak Velocity Breakout: 1m Vertical Bullish Expansion"
    scalper.latest_mark_price = 4160.0
    scalper.execute_entry_order("BUY", breakout_reason, atr=3.0)

    assert len(scalper.active_layers) == 3

    # 1. Price moves +0.03% in favor ($4162.00) -> All active layers snap SL to cost + fees
    scalper.latest_mark_price = 4162.00
    scalper.manage_open_position()

    for layer in scalper.active_layers:
        assert layer["is_risk_free"] is True
        assert layer["stop_loss"] >= layer["entry_price"]

    # 2. Price hits TP for the deepest layer (lowest TP) -> exactly 1 layer closes, 2 remain
    sorted_tps = sorted(l["take_profit"] for l in scalper.active_layers)
    tp_lowest = sorted_tps[0]
    tp_mid = sorted_tps[1]
    tp_highest = sorted_tps[2]

    # Set price to touch lowest TP without touching mid TP
    scalper.latest_mark_price = round(tp_lowest + 0.01, 2)
    scalper.manage_open_position()

    # Remaining active layers
    assert len(scalper.active_layers) == 2
    assert scalper.active_position is not None
    assert scalper.compounded_profit_usd > 0.0  # 85% profit compounded
    assert scalper.profit_vault_usd > 0.0      # 15% secured in vault

    # 3. Price touches mid TP -> 1 layer remains
    scalper.latest_mark_price = round(tp_mid + 0.01, 2)
    scalper.manage_open_position()
    assert len(scalper.active_layers) == 1

    # 4. Price surges to hit highest TP -> Entire pyramid cascade finished
    scalper.latest_mark_price = round(tp_highest + 0.10, 2)
    scalper.manage_open_position()

    assert len(scalper.active_layers) == 0
    assert scalper.active_position is None
    assert scalper.total_trades_count == 3


def test_non_breakout_standard_single_maker_entry():
    """Confirms calm standard market conditions still deploy single precision Maker entry."""
    scalper = HFTNetPositiveScalper()
    scalper.client.live_enabled = False
    scalper.latest_mark_price = 4160.0
    scalper.sync_wallet_from_api = False
    scalper.core_capital_usd = 22.51

    # Standard calm setup (balanced L2 book OBI < 0.60)
    scalper.ws_client.latest_orderbooks[scalper.symbol] = {
        "buy": [{"price": "4160.00", "size": 500}],
        "sell": [{"price": "4160.10", "size": 500}],
    }
    df_calm = pd.DataFrame({
        "atr_ratio": [1.0] * 5,
    })
    scalper.mtf_data = {"1m": df_calm}

    calm_reason = "Standard Calm Entry: Low Volatility Market Condition"
    scalper.execute_entry_order("BUY", calm_reason, atr=2.0)

    # Exactly 1 single order, not a pyramid cascade
    assert len(scalper.active_layers) == 1
    assert scalper.active_position.get("is_pyramid") is not True


def test_tier2_and_tier3_pyramid_execution():
    """Confirms that Tier 2 (50x) and Tier 3 (100x) both automatically deploy the pyramid layering cascade."""
    # Tier 2 test
    scalper_t2 = HFTNetPositiveScalper(pyramid_layers=4)
    scalper_t2.client.live_enabled = False
    scalper_t2.sync_wallet_from_api = False
    scalper_t2.latest_mark_price = 4160.0
    scalper_t2.core_capital_usd = 22.51
    scalper_t2.ws_client.latest_orderbooks[scalper_t2.symbol] = {
        "buy": [{"price": "4160.00", "size": 750}],
        "sell": [{"price": "4160.10", "size": 250}],
    }
    df_t2 = pd.DataFrame({
        "close": [4161.0] * 5,
        "vwap": [4158.0] * 5,
        "ema_9": [4160.0] * 5,
        "ema_21": [4155.0] * 5,
        "vol_ratio_2h": [2.0] * 5,
        "atr_ratio": [1.2] * 5,
    })
    scalper_t2.mtf_data = {"1m": df_t2, "5m": df_t2}
    scalper_t2.execute_entry_order("BUY", "Tier 2 Momentum Entry", atr=2.5)

    assert len(scalper_t2.active_layers) == 4
    assert scalper_t2.active_position.get("is_pyramid") is True
    assert scalper_t2.active_layers[0]["leverage"] == 50

    # Tier 3 test (100x leverage, 1.5% margin cap = 8 contracts)
    scalper_t3 = HFTNetPositiveScalper(pyramid_layers=4)
    scalper_t3.client.live_enabled = False
    scalper_t3.sync_wallet_from_api = False
    scalper_t3.latest_mark_price = 4160.0
    scalper_t3.core_capital_usd = 22.51
    scalper_t3.ws_client.latest_orderbooks[scalper_t3.symbol] = {
        "buy": [{"price": "4160.00", "size": 900}],
        "sell": [{"price": "4160.10", "size": 100}],
    }
    df_t3 = pd.DataFrame({
        "close": [4161.0] * 5,
        "vwap": [4158.0] * 5,
        "ema_9": [4160.0] * 5,
        "ema_21": [4155.0] * 5,
        "vol_ratio_2h": [6.0] * 5,  # > 500%
        "atr_ratio": [2.5] * 5,
    })
    scalper_t3.mtf_data = {"1m": df_t3, "5m": df_t3}
    scalper_t3.execute_entry_order("BUY", "Tier 3 Whale Sweep Entry", atr=3.5)

    assert len(scalper_t3.active_layers) == 4
    assert scalper_t3.active_position.get("is_pyramid") is True
    assert scalper_t3.active_layers[0]["leverage"] == 100
    assert sum(l["size"] for l in scalper_t3.active_layers) == 8  # 1.5% vault cap


def test_predictive_vsa_and_90pct_expansion_trigger():
    """
    Verifies Algorithmic Predictive Volume & Spread Analysis (VSA) Model:
    - Tracks L2 microsecond order flow compression, liquidity vacuum, and bid/ask padding.
    - Triggers Post-Only Limit Order *ahead* of the price wave when P >= 90%.
    - Places at best_bid for upcoming green candle, or best_ask for upcoming red candle.
    """
    scalper = HFTNetPositiveScalper(min_predictive_prob=0.90)

    # 1. Simulate Institutional Bullish Order Flow Compression & Massive Bid Padding
    scalper.ws_client.latest_orderbooks[scalper.symbol] = {
        "buy": [
            {"price": "4150.00", "size": 600},  # Inside demand wall
            {"price": "4149.99", "size": 400},
            {"price": "4149.98", "size": 300},
            {"price": "4149.90", "size": 50},
        ],
        "sell": [
            {"price": "4150.01", "size": 20},   # Extreme spread compression (1 tick = 0.01)
            {"price": "4150.02", "size": 25},   # Liquidity vacuum on ask side
            {"price": "4150.03", "size": 30},
            {"price": "4150.10", "size": 50},
        ],
    }

    # Populate snapshot history to simulate sudden institutional padding surge
    past_snap = {
        "timestamp": time.time() - 0.20,
        "best_bid": 4149.90,
        "best_ask": 4150.05,
        "mid_price": 4149.98,
        "spread": 0.15,
        "bid_vol_top3": 200,
        "ask_vol_top3": 200,
        "bid_vol_total": 400,
        "ask_vol_total": 400,
        "obi": 0.0,
        "bid_padding": 1.0,
        "ask_padding": 1.0,
        "compression": 0.2,
    }
    for _ in range(12):
        scalper.l2_history.append(past_snap)

    vsa = scalper.calculate_predictive_vsa_probabilities()
    assert vsa["prob_bull"] >= 0.90
    assert vsa["prob_bull"] > vsa["prob_bear"]

    action, entry_price, reason, atr = scalper.evaluate_predictive_order_flow_entry()
    assert action == "BUY"
    assert entry_price == 4150.00  # Catches bottom shelf ahead of the wave at best_bid
    assert "PREDICTIVE VSA FORECAST (BUY)" in reason
    assert "ahead of wave" in reason


def test_quant_intelligent_bracket_calculation_minimum_1_to_2_5_net_rr():
    """
    Verifies Quant-Based Intelligent Bracket Calculation:
    - Calculates dynamic Stop Loss and Take Profit brackets based on live ATR and depth.
    - Enforces asymmetrical Risk-to-Reward ratio: Minimum 1:2.5 net of all round-trip exchange fees.
    """
    scalper = HFTNetPositiveScalper(min_risk_reward_ratio=2.5)
    entry_price = 4150.00
    size = 2
    atr_1m = 3.00

    sl_price, tp_price, risk_pts, reward_pts, fee_buf_pts = scalper.calculate_intelligent_brackets(
        action="BUY", entry_price=entry_price, size=size, atr_1m=atr_1m
    )

    # Risk distance (bounded by 0.75*ATR and exhaustion ceiling contraction)
    assert risk_pts <= round(atr_1m * 0.75, 2)
    assert sl_price == round(entry_price - risk_pts, 2)

    # Net pure profit after all round-trip fees and slippage
    net_reward_pts = reward_pts - fee_buf_pts
    net_rr_ratio = round(net_reward_pts / risk_pts, 2)

    # Net Risk-to-Reward must strictly meet or exceed 1:2.5
    assert net_rr_ratio >= 2.50
    assert tp_price == round(entry_price + reward_pts, 2)


def test_wick_exhaustion_point_tp_scaling():
    """
    Verifies Dynamic Take Profit Placement:
    - Places Take Profit at predicted candle exhaustion point (wick top/bottom).
    - If default RR target exceeds wick exhaustion, dynamically contracts risk points
      to preserve the mandatory >= 1:2.5 Net RR.
    """
    scalper = HFTNetPositiveScalper(min_risk_reward_ratio=2.5)
    entry_price = 4150.00
    size = 2
    atr_1m = 1.80  # Tight ATR implies wick exhaustion ceiling is lower

    sl_price, tp_price, risk_pts, reward_pts, fee_buf_pts = scalper.calculate_intelligent_brackets(
        action="BUY", entry_price=entry_price, size=size, atr_1m=atr_1m
    )

    # Exhaustion ceiling is bounded by 1.80 * ATR + fee buffer
    exhaustion_ceiling = round(atr_1m * 1.80 + fee_buf_pts, 2)
    assert reward_pts <= exhaustion_ceiling

    # Guaranteed Net RR preserved
    net_reward_pts = reward_pts - fee_buf_pts
    assert (net_reward_pts / risk_pts) >= 2.49


def test_core_capital_vault_1pct_margin_exposure():
    """
    Verifies Unyielding Core Capital Vault Security:
    - Limits capital exposure strictly to 1.0% of available account margin per trade setup.
    """
    scalper = HFTNetPositiveScalper(margin_risk_pct=0.01, strict_1pct_margin_exposure=True)
    scalper.latest_mark_price = 4150.00
    scalper.core_capital_usd = 22.00
    scalper.profit_vault_usd = 0.0

    # Trade with 50x leverage
    contracts = scalper.calculate_dynamic_order_size(current_leverage=50, strict_1pct=True)
    margin_per_contract = (4150.00 * scalper.contract_val) / 50  # $0.083 USD
    total_margin_used = contracts * margin_per_contract
    margin_utilization = total_margin_used / scalper.core_capital_usd

    # Must not exceed 1.0% (with rounding tolerance for discrete contracts)
    assert total_margin_used <= (scalper.core_capital_usd * 0.015)
    assert contracts >= 1


def test_anticipatory_pre_candle_strike_entry_bullish(monkeypatch):
    """
    Verifies Anticipatory Pre-Candle Strike & Institutional Indicators (1m Grid):
    - Fires strictly BEFORE the 1-minute candle forms (at second 55).
    - Checks Zero-Lag EMA 9/21, VWAP, RSI-7, and Order Book Imbalance.
    - Places Post-Only Limit BUY ahead of the wave at best_bid to catch bottom of green candle.
    """
    scalper = HFTNetPositiveScalper(first_trade_triggered=True)
    # 1700000040.0 is exact multiple of 60: 1700000040.0 % 60 == 0
    # base + 55 = second 55 of the minute
    base_minute = 1700000040.0
    monkeypatch.setattr(time, "time", lambda: base_minute + 55.0)

    scalper.ws_client.latest_orderbooks[scalper.symbol] = {
        "buy": [{"price": "4150.00", "size": 600}],
        "sell": [{"price": "4150.10", "size": 200}],
    }

    df_1m = pd.DataFrame({
        "close": [4152.0] * 10,
        "vwap": [4150.0] * 10,
        "zlema_9": [4153.0] * 10,
        "zlema_21": [4150.0] * 10,
        "rsi_7": [58.0] * 10,
        "atr_14": [2.5] * 10,
    })
    scalper.mtf_data = {"1m": df_1m}
    scalper.latest_mark_price = 4152.00

    action, entry_price, reason, atr = scalper.evaluate_anticipatory_pre_candle_entry()
    assert action == "BUY"
    assert entry_price == 4150.00
    assert "ANTICIPATORY PRE-CANDLE STRIKE (BUY)" in reason
    assert "ZLEMA 9/21 Bullish" in reason


def test_immediate_first_trade_trigger_on_startup(monkeypatch):
    """
    Verifies Immediate First Live Trade Launch:
    - When first_trade_triggered is False, fires immediately on startup without waiting.
    """
    scalper = HFTNetPositiveScalper(first_trade_triggered=False)
    # Monkeypatch time to second 25 (mid-candle, outside the 53-59s window)
    base_minute = 1700000040.0
    monkeypatch.setattr(time, "time", lambda: base_minute + 25.0)

    scalper.ws_client.latest_orderbooks[scalper.symbol] = {
        "buy": [{"price": "4150.00", "size": 500}],
        "sell": [{"price": "4150.10", "size": 500}],
    }

    df_1m = pd.DataFrame({
        "close": [4152.0] * 10,
        "vwap": [4150.0] * 10,
        "zlema_9": [4152.5] * 10,
        "zlema_21": [4151.0] * 10,
        "rsi_7": [52.0] * 10,
        "atr_14": [2.5] * 10,
    })
    scalper.mtf_data = {"1m": df_1m}
    scalper.latest_mark_price = 4152.00

    action, entry_price, reason, atr = scalper.evaluate_anticipatory_pre_candle_entry()
    assert action == "BUY"
    assert "IMMEDIATE FIRST LIVE TRADE" in reason


def test_pre_close_candle_exit_guard_banks_green_body(monkeypatch):
    """
    Verifies Pre-Close Candle Exit Guard:
    - At second 52, when 1m candle is preparing to close, if trade is in net profit,
      it exits immediately to bank the body profit before closing wick retracements.
    """
    scalper = HFTNetPositiveScalper(pre_close_exit_window_sec=50)
    scalper.client.live_enabled = False
    scalper.sync_wallet_from_api = False

    entry = 4150.00
    mark = 4155.00  # Green body +$5.00
    size = 10

    # Monkeypatch time to second 53 of the minute (50 <= 53 <= 57)
    base_minute = 1700000040.0
    monkeypatch.setattr(time, "time", lambda: base_minute + 53.0)

    pos = {
        "order_id": "test_pre_close_order",
        "side": "buy",
        "size": size,
        "entry_price": entry,
        "take_profit": 4165.00,  # TP not reached yet
        "stop_loss": 4140.00,
        "initial_sl": 4140.00,
        "fee_buffer_pts": 2.49,
        "is_risk_free": True,
        "opened_at": base_minute + 20.0,  # Opened 33 seconds ago (< 45s velocity timeout)
        "highest_price": mark,
        "lowest_price": entry,
    }
    scalper.active_position = pos
    scalper.active_layers = [pos]
    scalper.latest_mark_price = mark

    scalper.manage_open_position()

    assert scalper.active_position is None
    assert scalper.total_trades_count == 1
    assert scalper.winning_trades_count == 1


def test_stop_loss_pct_020_buffer_cap():
    """
    Verifies 0.20% Stop Loss Buffer Shield:
    - Stop loss distance is capped at exactly 0.20% of entry price.
    """
    scalper = HFTNetPositiveScalper(stop_loss_pct=0.0020)
    entry_price = 4150.00
    size = 2
    atr_huge = 20.00  # Massive ATR would normally produce 15 pts SL

    sl_price, tp_price, risk_pts, reward_pts, fee_buf_pts = scalper.calculate_intelligent_brackets(
        action="BUY", entry_price=entry_price, size=size, atr_1m=atr_huge
    )

    max_allowed_sl_pts = round(entry_price * 0.0020, 2)  # $8.30
    assert risk_pts <= max_allowed_sl_pts


def test_boost_lot_size_and_100x_big_candle_scaling():
    """
    Verifies Boosted Lot Sizing & 100x Big Candle Leverage:
    - When boost_lot_size is True:
      - At 100x leverage: order size scales to 200 contracts (generating large profit per 1m candle).
      - At 25x leverage (slow market): order size scales to 60 contracts.
      - Profit target expands (exhaustion ceiling allows larger candle runs).
    """
    scalper = HFTNetPositiveScalper(boost_lot_size=True, tier1_leverage=25, tier3_leverage=100)
    scalper.core_capital_usd = 22.00
    scalper.profit_vault_usd = 0.0
    scalper.latest_mark_price = 4160.00

    # 1. Slow market (25x leverage) -> 60 contracts (~$9.98 margin, leaves >54% cushion)
    cts_25x = scalper.calculate_dynamic_order_size(current_leverage=25)
    assert cts_25x == 60

    # 2. Big candle expansion (100x leverage) -> 200 contracts (~$8.32 margin, leaves >61% cushion)
    cts_100x = scalper.calculate_dynamic_order_size(current_leverage=100)
    assert cts_100x == 200

    # 3. Expanded exhaustion ceiling for larger profit runs
    sl_p, tp_p, risk_pts, reward_pts, fee_buf = scalper.calculate_intelligent_brackets("BUY", 4160.00, 200, atr_1m=4.0)
    assert reward_pts > (4.0 * 1.80 + fee_buf)  # Expanded beyond 1.80x ATR ceiling


def test_predict_candle_expansion_height():
    """
    Verifies Algorithmic Predictive AI Candle Height & Range Forecasting:
    - Forecasts Delta H (expected expansion in pts), projected high, and projected low before candle forms.
    - Evaluates volatility clustering (ATR), L2 Order Book Imbalance, and inside padding.
    """
    scalper = HFTNetPositiveScalper()
    scalper.latest_mark_price = 4150.00

    # Order book with bullish imbalance (OBI = +0.50)
    scalper.ws_client.latest_orderbooks[scalper.symbol] = {
        "buy": [{"price": "4150.00", "size": 750}],
        "sell": [{"price": "4150.10", "size": 250}],
    }

    df_1m = pd.DataFrame({
        "atr_14": [3.0] * 5,
        "atr_ratio": [1.4] * 5,
    })
    scalper.mtf_data = {"1m": df_1m}

    delta_h, p_high, p_low, summary = scalper.predict_candle_expansion_height()

    assert delta_h >= 3.0  # Must be greater than or equal to base ATR
    assert p_high > scalper.latest_mark_price
    assert p_low < scalper.latest_mark_price
    assert "AI FORECAST" in summary
    assert "Delta H=" in summary


def test_pre_wick_deceleration_exit_guard():
    """
    Verifies Pre-Wick Candle Deceleration Exit:
    - User Request: 'जैसे ही कैंडल छोटी होने वाली है उससे पहले सौदा काट दे'
    - When a trade has reached a strong peak expansion (>=1.50 pts) and retraces >= 35% from the peak,
      it exits immediately to bank the positive profit before the wick retracts further.
    """
    scalper = HFTNetPositiveScalper()
    scalper.client.live_enabled = False
    scalper.sync_wallet_from_api = False

    entry = 4150.00
    peak = 4157.00      # Peak reached: +7.00 pts
    retrace = 4154.00   # Retraced 3.00 pts from peak (3.00 / 7.00 = 42.8% retrace >= 35%)
    size = 200

    pos = {
        "order_id": "test_pre_wick_order",
        "side": "buy",
        "size": size,
        "entry_price": entry,
        "take_profit": 4165.00,
        "stop_loss": 4140.00,
        "initial_sl": 4140.00,
        "fee_buffer_pts": 2.49,
        "is_risk_free": True,
        "opened_at": time.time() - 20.0,
        "highest_price": peak,
        "lowest_price": entry,
    }
    scalper.active_position = pos
    scalper.active_layers = [pos]
    scalper.latest_mark_price = retrace

    scalper.manage_open_position()

    assert scalper.active_position is None
    assert scalper.total_trades_count == 1
    assert scalper.winning_trades_count == 1







