"""
Unit tests for Delta Exchange REST Client and Paper Simulation Mode (quant_robot/delta_client.py)
"""

import pytest

from quant_robot.delta_client import DeltaClient, DeltaOrderRequest, OrderType


def test_delta_client_safety_lock():
    """Verifies that live trading safety lock prevents any real order execution."""
    client = DeltaClient(live_enabled=False)
    assert client.live_enabled is False

    req = DeltaOrderRequest(
        symbol="XAUTUSD",
        side="buy",
        order_type=OrderType.MARKET.value,
        size=5,
    )
    resp = client.place_order(req)
    assert resp.success is True
    assert resp.is_paper is True
    assert resp.status == "filled"
    assert resp.fee_usd > 0.0
    assert resp.executed_price > 0.0


def test_paper_wallet_and_positions():
    client = DeltaClient(live_enabled=False)
    wallet = client.get_wallet_balances()
    assert wallet["success"] is True
    assert wallet["is_paper"] is True
    assert wallet["available_balance"] in (20.0, 10000.0)

    # Place paper order
    req = DeltaOrderRequest(
        symbol="BTCUSD",
        side="buy",
        order_type=OrderType.MARKET.value,
        size=2,
    )
    client.place_order(req)
    positions = client.get_positions("BTCUSD")
    assert len(positions) == 1
    assert positions[0]["symbol"] == "BTCUSD"
    assert positions[0]["size"] == 2


def test_delta_public_ticker_and_candles():
    """Verifies live API connection to Delta India."""
    client = DeltaClient(live_enabled=False)
    ticker = client.get_ticker("XAUTUSD")
    assert "close" in ticker or "symbol" in ticker

    candles = client.get_candles("XAUTUSD", "1m")
    assert not candles.empty
    assert "close" in candles.columns
    assert "open" in candles.columns
    assert "datetime" in candles.columns


def test_bracket_order_request_fields():
    """Verifies DeltaOrderRequest handles bracket orders with TP limit price without TypeError."""
    req = DeltaOrderRequest(
        symbol="XAUTUSD",
        side="sell",
        order_type="limit_order",
        size=1,
        limit_price=4096.70,
        post_only=True,
        bracket_sl_price=4099.70,
        bracket_tp_price=4089.20,
        bracket_tp_limit_price=4089.20,
        client_order_id="TEST_123",
    )
    assert req.bracket_tp_price == 4089.20
    assert req.bracket_tp_limit_price == 4089.20
    assert req.bracket_sl_price == 4099.70
    assert req.post_only is True
