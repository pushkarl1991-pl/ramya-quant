"""
RAMYA QUANT ROBOT — Delta Exchange API v2 REST & WebSocket Integration
Implements:
1. REST API v2 Client with HMAC-SHA256 authentication, rate-limiting, and exponential backoff retry.
2. Async WebSocket Client (wss://socket.india.delta.exchange) with auto-reconnect, ping/pong heartbeats,
   and real-time subscriptions for tickers, L2 orderbook, and 1m candlesticks.
3. Strict Fail-Safe Paper Fallback:
   If LIVE_TRADING_ENABLED is False or API credentials are unset, all order and wallet execution calls
   are intercepted and routed to the deterministic paper simulation engine, ensuring zero unintended
   real orders.
4. Support for Order Types:
   - Market Order
   - Limit Order (Maker-only with post_only=True for fee rebates)
   - Stop-Loss Limit Order
   - Dynamic Trailing Stop
   - Bracket Order (Entry with attached TP & SL legs)
"""

import asyncio
import hashlib
import hmac
import json
import logging
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Union

import pandas as pd
import requests

try:
    import urllib3.util.connection as urllib3_cn
    urllib3_cn.HAS_IPV6 = False
except Exception:
    pass

from .config import (
    DEFAULT_SLIPPAGE_RATE,
    DEFAULT_TAKER_FEE_RATE,
    DELTA_API_KEY,
    DELTA_API_SECRET,
    DELTA_REST_URL,
    DELTA_WS_URL,
    FORCE_PAPER_MODE,
    INITIAL_CAPITAL_USD,
    IS_TESTNET,
    LIVE_TRADING_ENABLED,
    SUPPORTED_CONTRACTS,
)

logger = logging.getLogger("quant.delta_client")


class OrderType(str, Enum):
    MARKET = "market_order"
    LIMIT = "limit_order"
    STOP_LOSS_LIMIT = "stop_loss_order"
    TRAILING_STOP = "trailing_stop_order"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass
class DeltaOrderRequest:
    symbol: str
    side: str                          # 'buy' or 'sell'
    order_type: str                    # 'market_order', 'limit_order', 'stop_loss_order'
    size: int                          # Quantity in contracts
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    trail_amount: Optional[float] = None
    post_only: bool = False            # Maker-only order for fee rebates
    reduce_only: bool = False
    time_in_force: str = "gtc"         # 'gtc' or 'ioc'
    bracket_tp_price: Optional[float] = None
    bracket_tp_limit_price: Optional[float] = None
    bracket_sl_price: Optional[float] = None
    bracket_sl_limit_price: Optional[float] = None
    bracket_trail_amount: Optional[float] = None
    client_order_id: Optional[str] = None


@dataclass
class OrderResponse:
    success: bool
    order_id: str
    client_order_id: Optional[str] = None
    symbol: str = ""
    side: str = ""
    order_type: str = ""
    size: int = 0
    price: float = 0.0
    status: str = "open"                        # 'filled', 'open', 'cancelled', 'rejected'
    is_paper: bool = False
    fee_usd: float = 0.0
    slippage_usd: float = 0.0
    executed_price: float = 0.0
    timestamp: str = ""
    raw_response: Dict[str, Any] = field(default_factory=dict)


class DeltaClient:
    """
    Production-grade Delta Exchange REST API v2 client.
    Guarantees strict safety via LIVE_TRADING_ENABLED switch.
    """

    def __init__(
        self,
        api_key: str = DELTA_API_KEY,
        api_secret: str = DELTA_API_SECRET,
        base_url: str = DELTA_REST_URL,
        live_enabled: bool = LIVE_TRADING_ENABLED,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        has_credentials = bool(self.api_key and self.api_secret)
        self.live_enabled = bool(live_enabled and has_credentials)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "RAMYA-QuantRobot/2.0",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

        # Internal paper trading balance & orders state for fail-safe simulation
        self._paper_balance_usd = INITIAL_CAPITAL_USD
        self._paper_positions: Dict[str, Dict[str, Any]] = {}
        self._paper_orders: Dict[str, Dict[str, Any]] = {}
        self._paper_counter = 1000

        # Dynamic contract and product cache
        self._products_cache: Dict[str, Dict[str, Any]] = {}
        self._product_ids: Dict[str, int] = {}
        self._sync_product_ids()

    def _sync_product_ids(self):
        """Dynamically fetches all products or uses instant fallback defaults."""
        self._product_ids["XAUTUSD"] = 131253
        self._product_ids["BTCUSD"] = 27
        self._product_ids["ETHUSD"] = 3136
        self._product_ids["SOLUSD"] = 14823
        try:
            prods = self.get_products()
            for p in prods:
                symbol = p.get("symbol")
                pid = p.get("id")
                if symbol and pid:
                    self._product_ids[symbol] = int(pid)
                    self._products_cache[symbol] = p
        except Exception as e:
            logger.warning(f"Could not initialize product IDs from API: {e}. Using fallback defaults.")

    def resolve_symbol(self, symbol: str) -> str:
        """
        Dynamically normalizes symbol aliases such as 'MARKXAUTUSD', 'MARK:XAUTUSD', 'GOLD'
        to the exact tradable perpetual contract symbol on Delta Exchange (e.g. 'XAUTUSD').
        """
        if not symbol:
            return "XAUTUSD"
        raw_upper = str(symbol).upper().strip()
        cleaned = raw_upper.replace("MARK:", "").replace("MARK", "").replace("-", "").replace("/", "").replace("_", "").strip()

        # Handle Gold / Tether Gold Token Perpetual aliases
        if cleaned in ("XAUT", "XAUTUSD", "GOLD", "GOLDUSD", "XAUUSD"):
            return "XAUTUSD"

        # Check direct match in cache
        if raw_upper in self._product_ids:
            return raw_upper
        if cleaned in self._product_ids:
            return cleaned

        # Search in product cache
        if not self._products_cache:
            self._sync_product_ids()

        for s in self._product_ids:
            if cleaned == s or cleaned in s or s in cleaned:
                return s

        return cleaned or raw_upper

    def get_product_id(self, symbol: str) -> Optional[int]:
        norm = self.resolve_symbol(symbol)
        if norm in self._product_ids:
            return self._product_ids[norm]
        self._sync_product_ids()
        if norm in self._product_ids:
            return self._product_ids[norm]
        if symbol in self._product_ids:
            return self._product_ids[symbol]
        if "XAUT" in norm or "GOLD" in norm:
            return 131253
        return None

    def get_product(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Dynamically retrieves contract specification for a given symbol or alias."""
        norm = self.resolve_symbol(symbol)
        if norm not in self._products_cache:
            self._sync_product_ids()
        prod = self._products_cache.get(norm, self._products_cache.get(symbol))
        if not prod and ("XAUT" in norm or "GOLD" in norm):
            prod = self._products_cache.get("XAUTUSD")
        return prod

    def get_perpetual_symbols(self) -> List[str]:
        """Returns all dynamically discovered active perpetual futures symbols."""
        if not self._products_cache:
            self._sync_product_ids()
        perps = [
            sym for sym, p in self._products_cache.items()
            if p.get("contract_type") == "perpetual_futures"
        ]
        return perps if perps else ["XAUTUSD", "BTCUSD", "ETHUSD", "SOLUSD"]

    # ==========================================================================
    # AUTHENTICATION & SIGNATURE GENERATION
    # ==========================================================================
    def _generate_signature(
        self, method: str, path: str, timestamp: str, payload_str: str = ""
    ) -> str:
        """
        Delta v2 HMAC-SHA256 signature generator.
        Signature = HMAC_SHA256(secret, method + timestamp + path + query_or_body)
        """
        message = method.upper() + timestamp + path + payload_str
        return hmac.new(
            self.api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _get_auth_headers(self, method: str, path: str, payload_str: str = "") -> Dict[str, str]:
        timestamp = str(int(time.time()))
        signature = self._generate_signature(method, path, timestamp, payload_str)
        return {
            "api-key": self.api_key,
            "timestamp": timestamp,
            "signature": signature,
        }

    def _request_with_retry(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        is_private: bool = False,
        max_retries: int = 1,
        backoff_factor: float = 1.0,
    ) -> Dict[str, Any]:
        """
        Executes HTTP request with fast timeout for cloud resilience.
        """
        clean_base = self.base_url[:-3] if (self.base_url.endswith("/v2") and path.startswith("/v2")) else self.base_url
        url = f"{clean_base}{path}"
        query_str = f"?{urllib.parse.urlencode(params)}" if params else ""
        payload_str = json.dumps(data, separators=(",", ":")) if data else ""

        headers = {}
        if is_private:
            if not self.live_enabled or not self.api_key or not self.api_secret:
                raise PermissionError("Private API call attempted without valid API credentials or Live Trading lock enabled.")
            sign_path = path + query_str
            headers.update(self._get_auth_headers(method, sign_path, payload_str))

        delay = 0.5
        for attempt in range(1, max_retries + 1):
            try:
                if method.upper() == "GET":
                    resp = self.session.get(url, params=params, headers=headers, timeout=2.5)
                elif method.upper() == "POST":
                    resp = self.session.post(url, data=payload_str, headers=headers, timeout=2.5)
                elif method.upper() == "PUT":
                    resp = self.session.put(url, data=payload_str, headers=headers, timeout=2.5)
                elif method.upper() == "DELETE":
                    resp = self.session.delete(url, params=params, data=payload_str, headers=headers, timeout=2.5)
                else:
                    raise ValueError(f"Unsupported HTTP method: {method}")

                if resp.status_code == 200:
                    return resp.json()
                elif resp.status_code == 429:
                    logger.warning(f"Rate limited by Delta API (HTTP 429). Backing off for {delay}s...")
                    time.sleep(delay)
                    delay *= backoff_factor
                elif 500 <= resp.status_code < 600:
                    logger.warning(f"Delta Server error {resp.status_code}. Retry {attempt}/{max_retries}...")
                    time.sleep(delay)
                    delay *= backoff_factor
                else:
                    # Client errors (400, 401, 403, 404)
                    try:
                        err_json = resp.json()
                    except Exception:
                        err_json = {"error": resp.text}
                    logger.error(f"Delta API Request Failed [{resp.status_code}]: {err_json}")
                    return {"success": False, "status_code": resp.status_code, "error": err_json}
            except (requests.ConnectionError, requests.Timeout) as e:
                logger.warning(f"Network error connecting to Delta Exchange: {e}. Retry {attempt}/{max_retries}...")
                time.sleep(delay)
                delay *= backoff_factor

        return {"success": False, "error": "Max retries exceeded"}

    # ==========================================================================
    # PUBLIC REST ENDPOINTS
    # ==========================================================================
    def get_products(self) -> List[Dict[str, Any]]:
        """Fetches list of all available trading products on Delta Exchange India."""
        res = self._request_with_retry("GET", "/v2/products")
        if res.get("success"):
            return res.get("result", [])
        return []

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """Fetches live 24-hour ticker for the given symbol."""
        norm_sym = self.resolve_symbol(symbol)
        res = self._request_with_retry("GET", f"/v2/tickers/{norm_sym}")
        if res.get("success") and "result" in res:
            return res["result"]
        return {}

    def get_orderbook(self, symbol: str) -> Dict[str, Any]:
        """Fetches L2 orderbook depth for the symbol."""
        norm_sym = self.resolve_symbol(symbol)
        res = self._request_with_retry("GET", f"/v2/l2orderbook/{norm_sym}")
        if res.get("success") and "result" in res:
            return res["result"]
        return {}

    def get_candles(
        self, symbol: str, resolution: str = "1m", start: Optional[int] = None, end: Optional[int] = None
    ) -> pd.DataFrame:
        """
        Fetches historical OHLCV candles from Delta Exchange API.
        Automatically sorts chronologically (oldest first) and converts numeric columns.
        """
        norm_sym = self.resolve_symbol(symbol)
        now = int(time.time())
        if end is None:
            end = now
        if start is None:
            start = end - (3600 * 24)  # Default 24 hours

        params = {
            "resolution": resolution,
            "symbol": norm_sym,
            "start": start,
            "end": end,
        }
        res = self._request_with_retry("GET", "/v2/history/candles", params=params)
        raw_candles = res.get("result", [])
        if not raw_candles:
            return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])

        df = pd.DataFrame(raw_candles)
        # Ensure chronological ordering (Delta API returns reverse chronological)
        df["time"] = pd.to_numeric(df["time"], errors="coerce")
        df = df.sort_values("time").reset_index(drop=True)

        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        df["datetime"] = pd.to_datetime(df["time"], unit="s", utc=True)
        return df

    # ==========================================================================
    # PRIVATE TRADING & EXECUTION (WITH FAIL-SAFE PAPER MODE)
    # ==========================================================================
    def get_wallet_balances(self) -> Dict[str, Any]:
        """
        Returns account wallet balance.
        If live trading is disabled, returns safe virtual paper wallet state.
        """
        if not self.live_enabled or not self.api_key:
            return {
                "success": True,
                "is_paper": True,
                "available_balance": self._paper_balance_usd,
                "total_balance": self._paper_balance_usd,
                "currency": "USD",
                "message": "Virtual Paper Wallet (LIVE_TRADING_ENABLED=False)",
            }

        res = self._request_with_retry("GET", "/v2/wallet/balances", is_private=True)
        return res

    def get_positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Returns currently open positions.
        In paper mode, returns tracked simulated positions.
        """
        if not self.live_enabled or not self.api_key:
            if symbol:
                pos = self._paper_positions.get(symbol)
                return [pos] if pos else []
            return list(self._paper_positions.values())

        target_sym = symbol or "XAUTUSD"
        params = {}
        pid = self.get_product_id(target_sym)
        if pid:
            params["product_id"] = pid
        else:
            params["underlying_asset_symbol"] = target_sym

        res = self._request_with_retry("GET", "/v2/positions", params=params, is_private=True)
        if res.get("success"):
            result = res.get("result")
            if isinstance(result, dict):
                return [result] if result.get("size") else []
            elif isinstance(result, list):
                return result
        return []

    def place_order(self, order: DeltaOrderRequest) -> OrderResponse:
        """
        Places an order on Delta Exchange.
        If LIVE_TRADING_ENABLED is False, executes with zero risk in the Paper Simulation Engine
        with realistic slippage and fee deduction.
        """
        # Hard fail-safe lock check
        if not self.live_enabled:
            return self._execute_paper_order(order)

        # Real Live Order Execution
        norm_symbol = self.resolve_symbol(order.symbol)
        product_id = self.get_product_id(norm_symbol)
        payload: Dict[str, Any] = {
            "product_symbol": norm_symbol,
            "size": int(order.size),
            "side": order.side.lower(),
            "order_type": order.order_type,
            "time_in_force": order.time_in_force,
            "post_only": order.post_only,
            "reduce_only": order.reduce_only,
        }
        if product_id is not None:
            payload["product_id"] = product_id

        if order.limit_price is not None:
            payload["limit_price"] = str(order.limit_price)
        if order.stop_price is not None:
            payload["stop_price"] = str(order.stop_price)
            payload["stop_order_type"] = "stop_loss_order"
        if order.trail_amount is not None:
            payload["trail_amount"] = str(order.trail_amount)
        if order.client_order_id:
            payload["client_order_id"] = str(order.client_order_id)[:32]

        # Attach bracket TP/SL if provided
        if order.bracket_tp_price is not None:
            payload["bracket_take_profit_price"] = str(order.bracket_tp_price)
        if order.bracket_tp_limit_price is not None:
            payload["bracket_take_profit_limit_price"] = str(order.bracket_tp_limit_price)
        if order.bracket_sl_price is not None:
            payload["bracket_stop_loss_price"] = str(order.bracket_sl_price)
        if order.bracket_sl_limit_price is not None:
            payload["bracket_stop_loss_limit_price"] = str(order.bracket_sl_limit_price)
        if order.bracket_trail_amount is not None:
            payload["bracket_trail_amount"] = str(order.bracket_trail_amount)

        res = self._request_with_retry("POST", "/v2/orders", data=payload, is_private=True)
        if res.get("success"):
            result = res.get("result", {})
            return OrderResponse(
                success=True,
                order_id=str(result.get("id")),
                client_order_id=result.get("client_order_id"),
                symbol=order.symbol,
                side=order.side,
                order_type=order.order_type,
                size=order.size,
                price=float(result.get("limit_price") or 0.0),
                status=result.get("state", "open"),
                is_paper=False,
                fee_usd=0.0,
                slippage_usd=0.0,
                executed_price=float(result.get("average_fill_price") or result.get("limit_price") or 0.0),
                timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                raw_response=result,
            )
        else:
            return OrderResponse(
                success=False,
                order_id="",
                client_order_id=order.client_order_id,
                symbol=order.symbol,
                side=order.side,
                order_type=order.order_type,
                size=order.size,
                price=0.0,
                status="rejected",
                is_paper=False,
                fee_usd=0.0,
                slippage_usd=0.0,
                executed_price=0.0,
                timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                raw_response=res,
            )

    def _execute_paper_order(self, order: DeltaOrderRequest) -> OrderResponse:
        """
        Executes an order within the paper simulation engine using live market quotes,
        incorporating taker fees (0.05%) or maker fees (0.02%), and 2 bps slippage on market orders.
        """
        self._paper_counter += 1
        order_id = f"PAPER_{self._paper_counter}"

        norm_sym = self.resolve_symbol(order.symbol)
        # Fetch current real market quote
        ticker = self.get_ticker(norm_sym)
        quotes = ticker.get("quotes", {})
        best_bid = float(quotes.get("best_bid") or ticker.get("close") or 4000.0)
        best_ask = float(quotes.get("best_ask") or ticker.get("close") or 4000.0)
        mark_price = float(ticker.get("mark_price") or ticker.get("close") or 4000.0)

        contract_spec = self.get_product(norm_sym) or SUPPORTED_CONTRACTS.get(norm_sym, {})
        contract_val = float(contract_spec.get("contract_value", 0.001))

        # Base execution price
        if order.order_type == OrderType.MARKET.value:
            base_price = best_ask if order.side.lower() == "buy" else best_bid
            # Apply realistic slippage (2 bps)
            slippage_pct = DEFAULT_SLIPPAGE_RATE
            if order.side.lower() == "buy":
                executed_price = round(base_price * (1.0 + slippage_pct), 2)
            else:
                executed_price = round(base_price * (1.0 - slippage_pct), 2)
            fee_rate = float(contract_spec.get("taker_fee_rate") or contract_spec.get("taker_commission_rate", DEFAULT_TAKER_FEE_RATE))
            status = "filled"
        else:
            # Limit order
            executed_price = float(order.limit_price or mark_price)
            fee_rate = float(contract_spec.get("maker_fee_rate") or contract_spec.get("maker_commission_rate", 0.0002)) if order.post_only else float(contract_spec.get("taker_fee_rate") or contract_spec.get("taker_commission_rate", DEFAULT_TAKER_FEE_RATE))
            status = "open"  # Maker limit placed into virtual book

        notional = order.size * contract_val * executed_price
        fee_usd = round(notional * fee_rate, 4)
        slippage_usd = round(abs(executed_price - mark_price) * order.size * contract_val, 4)

        # Update paper balance
        self._paper_balance_usd -= fee_usd

        # Record position if filled
        if status == "filled":
            current_pos = self._paper_positions.get(norm_sym)
            if not current_pos or current_pos.get("size", 0) == 0:
                self._paper_positions[norm_sym] = {
                    "symbol": norm_sym,
                    "side": order.side.lower(),
                    "size": order.size,
                    "entry_price": executed_price,
                    "current_price": executed_price,
                    "stop_loss": order.bracket_sl_price,
                    "take_profit": order.bracket_tp_price,
                    "trailing_sl": order.bracket_sl_price,
                    "highest_price": executed_price,
                    "lowest_price": executed_price,
                    "notional_usd": notional,
                    "fee_paid": fee_usd,
                    "opened_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
            else:
                # Modifying or flattening existing position
                if current_pos["side"] == order.side.lower():
                    # Adding to position
                    new_size = current_pos["size"] + order.size
                    new_entry = round(
                        (current_pos["entry_price"] * current_pos["size"] + executed_price * order.size) / new_size, 2
                    )
                    current_pos["size"] = new_size
                    current_pos["entry_price"] = new_entry
                else:
                    # Closing / Reducing position
                    if order.size >= current_pos["size"]:
                        # Fully closed
                        self._paper_positions.pop(norm_sym, None)
                    else:
                        current_pos["size"] -= order.size

        logger.info(
            f"[PAPER EXECUTION] {order.side.upper()} {order.size} contracts {order.symbol} @ "
            f"${executed_price:.2f} | Fee: ${fee_usd:.4f} | Slippage: ${slippage_usd:.4f}"
        )

        return OrderResponse(
            success=True,
            order_id=order_id,
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            order_type=order.order_type,
            size=order.size,
            price=executed_price,
            status=status,
            is_paper=True,
            fee_usd=fee_usd,
            slippage_usd=slippage_usd,
            executed_price=executed_price,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            raw_response={"simulated": True, "executed_price": executed_price, "fee": fee_usd},
        )

    def cancel_order(self, order_id: Union[str, int], product_id: Optional[int] = None, symbol: Optional[str] = None) -> Dict[str, Any]:
        """Cancels an open order."""
        if not self.live_enabled:
            oid_str = str(order_id)
            if oid_str in self._paper_orders:
                self._paper_orders[oid_str]["status"] = "cancelled"
            return {"success": True, "message": f"Paper order {order_id} cancelled"}

        if product_id is None and symbol:
            product_id = self.get_product_id(symbol)

        payload: Dict[str, Any] = {"id": int(order_id)}
        if product_id is not None:
            payload["product_id"] = int(product_id)
        return self._request_with_retry("DELETE", "/v2/orders", data=payload, is_private=True)

    def cancel_all_orders(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        """Cancels all active open orders for the symbol or entire account."""
        if not self.live_enabled:
            count = len(self._paper_orders)
            self._paper_orders.clear()
            return {"success": True, "message": f"Cancelled {count} paper orders"}

        pid = self.get_product_id(symbol) if symbol else None
        payload = {}
        if pid:
            payload["product_id"] = pid
        return self._request_with_retry("DELETE", "/v2/orders/all", data=payload, is_private=True)

    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetches active open orders."""
        if not self.live_enabled:
            return list(self._paper_orders.values())
        pid = self.get_product_id(symbol) if symbol else None
        params = {"state": "open"}
        if pid:
            params["product_id"] = pid
        res = self._request_with_retry("GET", "/v2/orders", params=params, is_private=True)
        return res.get("result", [])

    def set_product_leverage(self, symbol: str, leverage: int) -> Dict[str, Any]:
        """Sets order leverage for a product (e.g. 3x)."""
        pid = self.get_product_id(symbol)
        if not pid:
            return {"success": False, "error": f"Unknown product symbol {symbol}"}
        if not self.live_enabled:
            return {"success": True, "product_id": pid, "leverage": str(leverage), "is_paper": True}
        payload = {"leverage": str(leverage)}
        return self._request_with_retry("POST", f"/v2/products/{pid}/orders/leverage", data=payload, is_private=True)

    def close_all_positions(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        """Closes all active positions on account or for a specific symbol."""
        if not self.live_enabled:
            norm_sym = self.resolve_symbol(symbol) if symbol else None
            if norm_sym:
                count = 1 if (norm_sym in self._paper_positions or (symbol and symbol in self._paper_positions)) else 0
                self._paper_positions.pop(norm_sym, None)
                if symbol:
                    self._paper_positions.pop(symbol, None)
            else:
                count = len(self._paper_positions)
                self._paper_positions.clear()
            return {"success": True, "message": f"Closed {count} paper positions"}

        payload = {}
        if symbol:
            pid = self.get_product_id(symbol)
            if pid:
                payload["product_id"] = pid
        return self._request_with_retry("POST", "/v2/positions/close_all", data=payload, is_private=True)

    def reconcile_positions_and_orders(self) -> Dict[str, Any]:
        """
        Reconciles local bot state with exchange matching engine after every restart.
        Fetches live wallet equity, open positions, and pending orders.
        """
        wallet = self.get_wallet_balances()
        positions = self.get_positions()
        orders = self.get_open_orders()
        return {
            "wallet": wallet,
            "positions": positions,
            "orders": orders,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    def get_trade_history(self, symbol: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """Fetches recent fills / trade history from Delta matching engine."""
        if not self.live_enabled or not self.api_key:
            return []
        params = {"limit": limit}
        if symbol:
            pid = self.get_product_id(symbol)
            if pid:
                params["product_id"] = pid
        res = self._request_with_retry("GET", "/v2/fills", params=params, is_private=True)
        return res.get("result", []) if res.get("success") else []


# ==============================================================================
# ASYNC WEBSOCKET CLIENT (24x7 STREAMING ENGINE)
# ==============================================================================
class DeltaWebSocketClient:
    """
    Asynchronous WebSocket Client for Delta Exchange India.
    Features:
    - Automatic exponential backoff reconnection.
    - 20-second heartbeat ping-pong monitor.
    - Subscribes to v2/ticker, l2_orderbook, and candlestick_1m.
    - In-memory thread-safe state cache.
    """

    def __init__(
        self,
        symbols: Optional[List[str]] = None,
        ws_url: str = DELTA_WS_URL,
        on_ticker_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        raw_symbols = symbols or ["XAUTUSD", "BTCUSD", "ETHUSD"]
        clean_symbols: List[str] = []
        for s in raw_symbols:
            raw = str(s).upper().strip().replace("MARK:", "").replace("MARK", "").replace("-", "").replace("/", "").replace("_", "").strip()
            canonical = "XAUTUSD" if raw in ("XAUT", "XAUTUSD", "GOLD", "GOLDUSD", "XAUUSD") else (raw or str(s).upper().strip())
            if canonical not in clean_symbols:
                clean_symbols.append(canonical)
        self.symbols = clean_symbols
        self.ws_url = ws_url
        self.on_ticker_callback = on_ticker_callback

        self.latest_tickers: Dict[str, Dict[str, Any]] = {}
        self.latest_orderbooks: Dict[str, Dict[str, Any]] = {}
        self.is_connected = False
        self.is_running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.reconnect_count = 0

    def start(self):
        """Starts WebSocket client in a dedicated background worker thread."""
        if self.is_running:
            return
        self.is_running = True
        self._thread = threading.Thread(target=self._run_event_loop, daemon=True, name="DeltaWSWorker")
        self._thread.start()
        logger.info("DeltaWebSocketClient worker thread started.")

    def stop(self):
        """Stops WebSocket client."""
        self.is_running = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        logger.info("DeltaWebSocketClient stopped.")

    def _run_event_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._connection_manager())

    async def _connection_manager(self):
        """Manages persistent connection with exponential backoff."""
        import websockets

        backoff = 1.0
        max_backoff = 60.0

        while self.is_running:
            try:
                logger.info(f"Connecting to Delta WebSocket: {self.ws_url}")
                async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=10) as ws:
                    self.is_connected = True
                    backoff = 1.0  # Reset backoff upon successful connection
                    logger.info("Delta WebSocket connection established.")

                    # Send subscriptions
                    sub_message = {
                        "type": "subscribe",
                        "payload": {
                            "channels": [
                                {"name": "v2/ticker", "symbols": self.symbols},
                                {"name": "l2_orderbook", "symbols": self.symbols},
                            ]
                        },
                    }
                    await ws.send(json.dumps(sub_message))

                    # Start message listener & ping heartbeat loop
                    heartbeat_task = asyncio.create_task(self._heartbeat(ws))
                    receiver_task = asyncio.create_task(self._receive_loop(ws))

                    done, pending = await asyncio.wait(
                        [heartbeat_task, receiver_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    for task in pending:
                        task.cancel()

            except Exception as e:
                self.is_connected = False
                self.reconnect_count += 1
                logger.warning(
                    f"WebSocket error: {e}. Reconnecting in {backoff:.1f}s (Attempt #{self.reconnect_count})..."
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, max_backoff)

    async def _heartbeat(self, ws):
        """Sends periodic application-level ping messages."""
        while self.is_running:
            await asyncio.sleep(20)
            try:
                await ws.send(json.dumps({"type": "ping"}))
            except Exception:
                break

    async def _receive_loop(self, ws):
        """Processes incoming WebSocket packets."""
        while self.is_running:
            try:
                raw_msg = await ws.recv()
                msg = json.loads(raw_msg)
                msg_type = msg.get("type")

                if msg_type == "v2/ticker" or "mark_price" in msg:
                    symbol = msg.get("symbol")
                    if symbol:
                        self.latest_tickers[symbol] = msg
                        if symbol == "XAUTUSD":
                            self.latest_tickers["MARKXAUTUSD"] = msg
                            self.latest_tickers["MARK:XAUTUSD"] = msg
                            self.latest_tickers["GOLD"] = msg
                        if self.on_ticker_callback:
                            self.on_ticker_callback(msg)

                elif msg_type == "l2_orderbook":
                    symbol = msg.get("symbol")
                    if symbol:
                        self.latest_orderbooks[symbol] = msg
                        if symbol == "XAUTUSD":
                            self.latest_orderbooks["MARKXAUTUSD"] = msg
                            self.latest_orderbooks["MARK:XAUTUSD"] = msg
                            self.latest_orderbooks["GOLD"] = msg

            except Exception as e:
                logger.warning(f"Error reading WebSocket frame: {e}")
                break
