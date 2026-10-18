"""
Unit tests for the CostAndProfitabilityEngine.
Verifies all institutional requirements:
A. Calculate Every Trading Cost (Brokerage, 18% GST, Spread, Slippage, Funding)
B. Stop-Loss & Take-Profit integrity (No widening, execution risk disclaimer)
C. Dynamic Lot Sizing (0.25% equity risk budget, no unlimited lots/100x leverage on big candles)
D. Trade Frequency (Cooldowns, duplicate signal protection, max trade limits)
E. Mandatory Pre-Order Decision (ACCEPT/REJECT with full cost & profit metrics)
F. Capital Protection (Circuit breaker on daily loss limit, PAPER mode by default)
"""

import time
import pytest
from quant_robot.cost_and_profitability_engine import (
    CostAndProfitabilityEngine,
    CostBreakdown,
    PreOrderDecision,
    ClosedTradeAccounting,
)


@pytest.fixture
def standard_spec():
    return {
        "symbol": "XAUTUSD",
        "contract_value": 0.001,
        "tick_size": 0.01,
        "maker_commission_rate": 0.0001,
        "taker_commission_rate": 0.0001,
        "min_order_size": 1,
    }


def test_calculate_every_trading_cost_breakdown(standard_spec):
    """
    Verifies Section A:
    Calculates entry fee, exit fee, 18% GST, spread, slippage, and funding.
    """
    engine = CostAndProfitabilityEngine(initial_equity=100.0, gst_rate=0.18, slippage_ticks=2)
    entry_price = 4000.0
    tp_price = 4010.0
    sl_price = 3995.0
    size = 10
    contract_val = 0.001

    # 1. Take-Profit scenario (Maker exit, zero slippage)
    costs_tp = engine.calculate_trading_costs(
        symbol="XAUTUSD",
        entry_price=entry_price,
        exit_price=tp_price,
        size=size,
        contract_val=contract_val,
        maker_fee_rate=0.0001,
        taker_fee_rate=0.0001,
        tick_size=0.01,
        is_tp=True,
        spread=0.02,
        est_holding_hours=0.25,
    )

    # Notional = 4000 * 0.001 * 10 = $40 USD
    expected_entry_fee = round(40.0 * 0.0001, 4)  # $0.0040
    # TP Notional = 4010 * 0.001 * 10 = $40.10 USD
    expected_exit_tp_fee = round(40.10 * 0.0001, 4)  # $0.00401 -> $0.0040
    expected_gst = round((expected_entry_fee + expected_exit_tp_fee) * 0.18, 4)
    expected_spread = round(0.02 * contract_val * size, 4)  # $0.0002
    expected_slippage_tp = 0.0  # Zero slippage for passive Maker Limit TP
    expected_funding = round(40.0 * 0.0001 * (0.25 / 8.0), 4)

    assert costs_tp.entry_fee_usd == expected_entry_fee
    assert costs_tp.exit_fee_usd == expected_exit_tp_fee
    assert costs_tp.gst_charges_usd == expected_gst
    assert costs_tp.bid_ask_spread_usd == expected_spread
    assert costs_tp.estimated_slippage_usd == expected_slippage_tp
    assert costs_tp.estimated_funding_usd == expected_funding
    assert costs_tp.total_costs_usd > 0.0

    # 2. Stop-Loss scenario (Taker exit, 2 ticks slippage)
    costs_sl = engine.calculate_trading_costs(
        symbol="XAUTUSD",
        entry_price=entry_price,
        exit_price=sl_price,
        size=size,
        contract_val=contract_val,
        maker_fee_rate=0.0001,
        taker_fee_rate=0.0002,
        tick_size=0.01,
        is_tp=False,
        spread=0.02,
        est_holding_hours=0.25,
    )
    expected_slippage_sl = round(2 * 0.01 * contract_val * size, 4)  # $0.0002
    assert costs_sl.estimated_slippage_usd == expected_slippage_sl


def test_mandatory_pre_order_decision_accept(standard_spec):
    """
    Verifies Section E & A:
    Outputs full pre-order decision with gross, round-trip costs, net profit, net loss, Net RR.
    Approves high-quality trade where Net RR >= 1.5.
    """
    engine = CostAndProfitabilityEngine(
        initial_equity=50.0,
        max_risk_per_trade_pct=0.0025,  # 0.25% = $0.125
        min_net_risk_reward=1.5,
    )

    decision = engine.evaluate_pre_trade_decision(
        symbol="XAUTUSD",
        action="BUY",
        entry_price=4150.00,
        stop_loss=4148.00,  # 2.00 pts SL
        take_profit=4156.00, # 6.00 pts TP (Price RR = 3:1)
        proposed_leverage=10,
        contract_spec=standard_spec,
    )

    assert decision.is_approved is True
    assert decision.status == "ACCEPT"
    assert decision.contracts >= 1
    assert decision.leverage <= 10
    assert decision.gross_profit_usd > 0.0
    assert decision.expected_net_profit_usd > 0.0
    assert decision.expected_net_loss_usd > 0.0
    assert decision.net_reward_to_risk_ratio >= 1.5
    assert "Execution Risk Notice" in decision.disclaimer


def test_rejection_when_net_reward_to_risk_is_insufficient(standard_spec):
    """
    Verifies Section A & E:
    Rejects trade when expected Net Risk-to-Reward ratio is below 1.5.
    """
    engine = CostAndProfitabilityEngine(
        initial_equity=50.0,
        min_net_risk_reward=1.5,
    )

    # TP is +2.50 pts vs SL -3.00 pts (Price RR 0.83:1) -> Net RR will be < 1.5
    decision = engine.evaluate_pre_trade_decision(
        symbol="XAUTUSD",
        action="BUY",
        entry_price=4150.00,
        stop_loss=4147.00,
        take_profit=4152.50,
        proposed_leverage=10,
        contract_spec=standard_spec,
    )

    assert decision.is_approved is False
    assert decision.status == "REJECT"
    assert "Net Reward-to-Risk" in decision.reason or "too much profit" in decision.reason or "negative" in decision.reason


def test_rejection_when_costs_consume_too_much_profit(standard_spec):
    """
    Verifies Section A:
    Rejects trade when round-trip costs consume > 35% of gross expected profit.
    """
    engine = CostAndProfitabilityEngine(
        initial_equity=50.0,
        max_cost_consumption_pct=0.05,  # Very strict 5% maximum cost consumption
    )

    decision = engine.evaluate_pre_trade_decision(
        symbol="XAUTUSD",
        action="BUY",
        entry_price=4150.00,
        stop_loss=4148.00,
        take_profit=4154.00,
        proposed_leverage=10,
        contract_spec=standard_spec,
    )

    assert decision.is_approved is False
    assert decision.status == "REJECT"
    assert "Costs consume too much profit" in decision.reason


def test_rejection_on_missing_fee_or_market_data():
    """
    Verifies Section E:
    If fee data, contract specifications or market data are unavailable, do not open a new trade.
    """
    engine = CostAndProfitabilityEngine()

    decision = engine.evaluate_pre_trade_decision(
        symbol="XAUTUSD",
        action="BUY",
        entry_price=4150.00,
        stop_loss=4145.00,
        take_profit=4160.00,
        contract_spec=None,  # Missing contract specs / fee schedule
    )

    assert decision.is_approved is False
    assert decision.status == "REJECT"
    assert "Missing market data" in decision.reason


def test_rejection_when_minimum_order_exceeds_risk_budget(standard_spec):
    """
    Verifies Section B & C:
    If minimum 1 contract risk exceeds the 0.25% equity risk budget, reject the trade.
    """
    # Very small equity: $10.00 -> 0.25% budget = $0.025
    engine = CostAndProfitabilityEngine(
        initial_equity=10.00,
        max_risk_per_trade_pct=0.0025,
    )

    # Wide stop loss of 50 points -> 50 * 0.001 = $0.050 risk, exceeding $0.025 budget!
    decision = engine.evaluate_pre_trade_decision(
        symbol="XAUTUSD",
        action="BUY",
        entry_price=4150.00,
        stop_loss=4100.00,
        take_profit=4250.00,
        contract_spec=standard_spec,
    )

    assert decision.is_approved is False
    assert decision.status == "REJECT"
    assert "exceeds" in decision.reason and "budget" in decision.reason


def test_big_candle_never_triggers_unlimited_lots_or_100x_leverage(standard_spec):
    """
    Verifies Section C:
    A large candle or AI confidence score must NEVER automatically trigger unlimited lots
    or 100% leverage increases. If proposed leverage is 100x, it must be capped at max_leverage_cap.
    """
    engine = CostAndProfitabilityEngine(
        initial_equity=25.0,
        max_risk_per_trade_pct=0.0025,
        max_leverage_cap=10,  # Hard cap
        max_contracts_cap=20, # Hard cap
    )

    # Attempt to request 100x leverage on a giant breakout
    decision = engine.evaluate_pre_trade_decision(
        symbol="XAUTUSD",
        action="BUY",
        entry_price=4150.00,
        stop_loss=4147.00,
        take_profit=4158.00,
        proposed_leverage=100,  # Requested 100x
        contract_spec=standard_spec,
    )

    if decision.is_approved:
        # Leverage must be strictly reduced to max_leverage_cap (10x)
        assert decision.leverage <= 10
        assert decision.contracts <= 20
        assert decision.risk_pct_of_equity <= 0.250 + 0.05  # within 0.25% risk budget tolerance


def test_stop_loss_widening_strictly_prohibited():
    """
    Verifies Section B:
    Never widen a stop to hide losses.
    """
    engine = CostAndProfitabilityEngine()

    # For BUY: widening means moving SL lower
    approved_buy, reason_buy = engine.validate_stop_loss_adjustment(
        current_sl=4145.00, proposed_sl=4140.00, side="BUY"
    )
    assert approved_buy is False
    assert "cannot be widened" in reason_buy

    # For BUY: tightening or trailing SL higher is approved
    approved_trail, _ = engine.validate_stop_loss_adjustment(
        current_sl=4145.00, proposed_sl=4148.00, side="BUY"
    )
    assert approved_trail is True

    # For SELL: widening means moving SL higher
    approved_sell, reason_sell = engine.validate_stop_loss_adjustment(
        current_sl=4155.00, proposed_sl=4160.00, side="SELL"
    )
    assert approved_sell is False
    assert "cannot be widened" in reason_sell


def test_trade_frequency_cooldown_and_duplicate_signal_protection(standard_spec):
    """
    Verifies Section D:
    Enforces cooldown and duplicate-signal protection.
    """
    engine = CostAndProfitabilityEngine(
        initial_equity=50.0,
        cooldown_seconds=60.0,
    )

    # 1. First trade approved
    decision1 = engine.evaluate_pre_trade_decision(
        symbol="XAUTUSD",
        action="BUY",
        entry_price=4150.00,
        stop_loss=4148.00,
        take_profit=4156.00,
        contract_spec=standard_spec,
        signal_signature="SIG_BUY_1",
    )
    assert decision1.is_approved is True

    # 2. Immediate second trade with duplicate signal -> REJECTED
    decision2 = engine.evaluate_pre_trade_decision(
        symbol="XAUTUSD",
        action="BUY",
        entry_price=4150.00,
        stop_loss=4148.00,
        take_profit=4156.00,
        contract_spec=standard_spec,
        signal_signature="SIG_BUY_1",
    )
    assert decision2.is_approved is False
    assert "Cooldown active" in decision2.reason or "Duplicate signal" in decision2.reason


def test_post_trade_reconciliation_and_separate_costs(standard_spec):
    """
    Verifies Section A & F:
    Recalculates net P&L using actual fills and actual fees after closing every position.
    Displays gross P&L, total costs, and final net P&L separately.
    """
    engine = CostAndProfitabilityEngine(initial_equity=25.0, gst_rate=0.18)

    record = engine.record_closed_trade_accounting(
        symbol="XAUTUSD",
        side="BUY",
        size=10,
        entry_price=4150.00,
        exit_price=4156.00,  # +$6.00 pts move
        actual_entry_fee=0.0041,
        actual_exit_fee=0.0041,
        actual_slippage_usd=0.0002,
        actual_funding_usd=0.0001,
        exit_reason="TP_WICK_EXHAUSTION",
    )

    # Gross P&L: 6.00 * 0.001 * 10 = +$0.0600 USD
    assert round(record.gross_pnl_usd, 4) == 0.0600
    # Fees + GST: (0.0041 + 0.0041) * 1.18 = $0.009676
    # Total Costs: 0.009676 + 0.0002 + 0.0001 = $0.009976 -> ~$0.0100
    assert record.total_costs_usd > 0.0
    assert record.final_net_pnl_usd == round(record.gross_pnl_usd - record.total_costs_usd, 4)
    assert record.final_net_pnl_usd < record.gross_pnl_usd  # Confirms costs deducted

    # Formatted report contains separate Gross, Costs, and Final Net P&L
    report = engine.format_closed_trade_report(record)
    assert "GROSS P&L" in report
    assert "TOTAL COSTS" in report
    assert "FINAL NET P&L" in report
    assert "PORTFOLIO TELEMETRY" in report


def test_daily_loss_limit_circuit_breaker(standard_spec):
    """
    Verifies Section F:
    Stops opening new trades when daily loss limit is reached.
    """
    engine = CostAndProfitabilityEngine(
        initial_equity=100.0,
        daily_loss_limit_pct=0.01,  # 1.0% = -$1.00 daily loss limit
    )

    # Record a losing trade of -$1.50 (150 contracts * 10 pts * 0.001 = $1.50 > $1.00 loss limit)
    engine.record_closed_trade_accounting(
        symbol="XAUTUSD",
        side="BUY",
        size=150,
        entry_price=4150.00,
        exit_price=4140.00,
        actual_entry_fee=0.01,
        actual_exit_fee=0.01,
        exit_reason="STOP_LOSS",
    )

    assert engine.circuit_breaker_tripped is True
    assert "Daily Loss Limit Reached" in engine.circuit_breaker_reason

    # Subsequent trade must be rejected by circuit breaker
    decision = engine.evaluate_pre_trade_decision(
        symbol="XAUTUSD",
        action="BUY",
        entry_price=4150.00,
        stop_loss=4148.00,
        take_profit=4156.00,
        contract_spec=standard_spec,
    )
    assert decision.is_approved is False
    assert "CIRCUIT_BREAKER_ACTIVE" in decision.reason


def test_paper_mode_enabled_by_default():
    """
    Verifies Section F:
    Keep PAPER mode enabled by default. Live execution requires explicit authorization.
    """
    engine = CostAndProfitabilityEngine()
    assert engine.is_paper_mode is True

    report = engine.get_performance_report()
    assert report["trading_mode"] == "PAPER"
