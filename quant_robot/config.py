"""
RAMYA QUANT ROBOT — Production & Demo Configuration
Delta Exchange India (Official API v2) with Strict Risk Governance
"""

import os
from pathlib import Path

# Load .env file
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"
if ENV_PATH.exists():
    with open(ENV_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip().strip("'\"")] = v.strip().strip("'\"")

# Fallback to ~/.delta-exchange-mcp/config.env if keys are missing
if not os.getenv("DELTA_API_KEY") and not os.getenv("DELTA_PROD_API_KEY"):
    mcp_env = Path.home() / ".delta-exchange-mcp" / "config.env"
    if mcp_env.exists():
        with open(mcp_env, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip().strip("'\""), v.strip().strip("'\""))

# ==============================================================================
# 1. ENVIRONMENT & EXCHANGE ENDPOINTS (PRODUCTION VS DEMO / TESTNET)
# ==============================================================================
DELTA_ENV: str = os.getenv("DELTA_ENV", "production").lower()  # 'production' or 'demo' / 'testnet'
IS_TESTNET: bool = DELTA_ENV in ("demo", "testnet")

# Production Endpoints (Delta Exchange India)
DELTA_PROD_REST_URL: str = "https://api.india.delta.exchange"
DELTA_PROD_WS_URL: str = "wss://socket.india.delta.exchange"
DELTA_PROD_API_KEY: str = os.getenv("DELTA_PROD_API_KEY", os.getenv("DELTA_API_KEY", "")).strip("'\"")
DELTA_PROD_API_SECRET: str = os.getenv("DELTA_PROD_API_SECRET", os.getenv("DELTA_API_SECRET", "")).strip("'\"")

# Demo / Testnet Endpoints
DELTA_DEMO_REST_URL: str = "https://cdn-ind.testnet.deltaex.org"
DELTA_DEMO_WS_URL: str = "wss://socket.testnet.delta.exchange"
DELTA_DEMO_API_KEY: str = os.getenv("DELTA_DEMO_API_KEY", "").strip("'\"")
DELTA_DEMO_API_SECRET: str = os.getenv("DELTA_DEMO_API_SECRET", "").strip("'\"")

# Active Credentials and Endpoints based on environment
DELTA_REST_URL: str = DELTA_DEMO_REST_URL if IS_TESTNET else DELTA_PROD_REST_URL
DELTA_WS_URL: str = DELTA_DEMO_WS_URL if IS_TESTNET else DELTA_PROD_WS_URL
DELTA_API_KEY: str = DELTA_DEMO_API_KEY if IS_TESTNET else DELTA_PROD_API_KEY
DELTA_API_SECRET: str = DELTA_DEMO_API_SECRET if IS_TESTNET else DELTA_PROD_API_SECRET

# ==============================================================================
# 2. EXECUTION MODES & LIVE SAFETY CONTROLS
# ==============================================================================
# Start with PAPER mode by default. LIVE mode is strictly OFF unless explicitly enabled.
TRADING_MODE: str = os.getenv("TRADING_MODE", "PAPER").upper()
LIVE_TRADING_ENABLED: bool = os.getenv("LIVE_TRADING_ENABLED", "False").lower() in ("true", "1")
IS_PAPER_TRADING: bool = not LIVE_TRADING_ENABLED
FORCE_PAPER_MODE: bool = not LIVE_TRADING_ENABLED
SIMULATED_SLIPPAGE: bool = False

# Double Lock: Explicit user authorization requirement (can be toggled via env or CLI)
REQUIRE_EXPLICIT_APPROVAL: bool = os.getenv("REQUIRE_EXPLICIT_APPROVAL", "false").lower() in ("true", "1")

# ==============================================================================
# 3. RISK MANAGEMENT CONTROLS (MASTER DEFAULTS)
# ==============================================================================
# Default maximum risk per trade: 0.25% of account equity
DEFAULT_RISK_PER_TRADE_PCT: float = 0.0025
RISK_PER_TRADE_PCT: float = 0.0025
MAX_RISK_PER_TRADE_PCT: float = 0.0025

# Default daily loss limit: 1.0% of starting daily equity
DEFAULT_DAILY_LOSS_LIMIT_PCT: float = 0.01
DAILY_LOSS_LIMIT_PCT: float = 0.01
DAILY_DRAWDOWN_CIRCUIT_BREAKER_PCT: float = 0.01

# Position & Leverage limits
MAX_OPEN_POSITIONS: int = 1                       # Maximum one open position initially
DEFAULT_LEVERAGE: int = 10                        # Leverage set to 10x for safe margin utilization
HARD_LOCKED_LEVERAGE: int = 10                    # 10x leverage enables 10 contracts within <= 20% margin
INITIAL_CAPITAL_USD: float = 22.97                # Operational baseline

# Anti-Martingale & Loss Protection
ALLOW_MARTINGALE: bool = False                    # Strictly forbidden: zero position size increase on loss
ALLOW_LOSS_DOUBLING: bool = False                 # Zero loss recovery doubling
MIN_NET_RISK_REWARD_RATIO: float = 1.5            # Reject setup if Net R:R after costs < 1.5
MAX_SPREAD_TICKS: int = 10                        # Reject entry if spread > 10 ticks ($0.10 for Gold)
MIN_ORDERBOOK_LIQUIDITY_CONTRACTS: int = 10       # Reject entry if book depth < 10 contracts

# Circuit Breaker & Freshness Thresholds
STALE_PRICE_MAX_SECONDS: int = 30                 # Data freshness threshold (max 30s latency)
MAX_API_ERROR_COUNT: int = 3                      # Halt if >= 3 consecutive API failures
CONSECUTIVE_LOSS_LIMIT: int = 2                   # Lock out on 2 consecutive losses
LOCKOUT_DURATION_HOURS: float = 4.0               # 4 hours halt duration
CIRCUIT_BREAKER_HALT_HOURS: float = 4.0

# Execution Fees & Modeling
DEFAULT_TAKER_FEE_RATE: float = 0.0005            # 0.05%
DEFAULT_MAKER_FEE_RATE: float = 0.0002            # 0.02%
DEFAULT_SLIPPAGE_RATE: float = 0.0002             # 0.02%
EXECUTION_TYPE_MAKER_ONLY: bool = True            # Post_only=True Maker limit entries

# ==============================================================================
# 4. SUPPORTED ASSETS & TIMEFRAMES (DYNAMIC DISCOVERY FALLBACKS)
# ==============================================================================
SUPPORTED_CONTRACTS = {
    "XAUTUSD": {
        "symbol": "XAUTUSD",
        "product_id": 131253,
        "name": "Gold (Tether Gold)",
        "underlying": "XAUT",
        "quoting_asset": "USD",
        "contract_type": "perpetual_futures",
        "tick_size": 0.01,
        "contract_value": 0.001,       # 1 contract = 0.001 fine troy oz Gold
        "min_order_size": 1,
        "lot_size": 1,
        "max_leverage": 100,
        "taker_fee_rate": 0.0001,
        "maker_fee_rate": 0.0001
    },
    "SLVONUSD": {
        "symbol": "SLVONUSD",
        "product_id": 124058,
        "name": "Silver (iShares Silver Trust)",
        "underlying": "SLVON",
        "quoting_asset": "USD",
        "contract_type": "perpetual_futures",
        "tick_size": 0.01,
        "contract_value": 0.1,       # 1 contract = 0.1 oz of Silver (~$5.19 notional)
        "min_order_size": 1,
        "lot_size": 1,
        "max_leverage": 50,
        "taker_fee_rate": 0.0001,
        "maker_fee_rate": 0.0001
    },
    "XAGUSD": {
        "symbol": "SLVONUSD",
        "product_id": 124058,
        "name": "Silver (iShares Silver Trust)",
        "underlying": "SLVON",
        "quoting_asset": "USD",
        "contract_type": "perpetual_futures",
        "tick_size": 0.01,
        "contract_value": 0.1,
        "min_order_size": 1,
        "lot_size": 1,
        "max_leverage": 50,
        "taker_fee_rate": 0.0001,
        "maker_fee_rate": 0.0001
    },
    "SILVER": {
        "symbol": "SLVONUSD",
        "product_id": 124058,
        "name": "Silver (iShares Silver Trust)",
        "underlying": "SLVON",
        "quoting_asset": "USD",
        "contract_type": "perpetual_futures",
        "tick_size": 0.01,
        "contract_value": 0.1,
        "min_order_size": 1,
        "lot_size": 1,
        "max_leverage": 50,
        "taker_fee_rate": 0.0001,
        "maker_fee_rate": 0.0001
    },
    "BTCUSD": {
        "symbol": "BTCUSD",
        "product_id": 27,
        "name": "Bitcoin Perpetual",
        "underlying": "BTC",
        "quoting_asset": "USD",
        "contract_type": "perpetual_futures",
        "tick_size": 0.50,
        "contract_value": 0.001,
        "min_order_size": 1,
        "lot_size": 1,
        "max_leverage": 200,
        "taker_fee_rate": 0.0005,
        "maker_fee_rate": 0.0002
    },
    "ETHUSD": {
        "symbol": "ETHUSD",
        "product_id": 3136,
        "name": "Ethereum Perpetual",
        "underlying": "ETH",
        "quoting_asset": "USD",
        "contract_type": "perpetual_futures",
        "tick_size": 0.05,
        "contract_value": 0.01,
        "min_order_size": 1,
        "lot_size": 1,
        "max_leverage": 200,
        "taker_fee_rate": 0.0005,
        "maker_fee_rate": 0.0002
    },
    "SOLUSD": {
        "symbol": "SOLUSD",
        "product_id": 14823,
        "name": "Solana Perpetual",
        "underlying": "SOL",
        "quoting_asset": "USD",
        "contract_type": "perpetual_futures",
        "tick_size": 0.0001,
        "contract_value": 1.0,
        "min_order_size": 1,
        "lot_size": 1,
        "max_leverage": 100,
        "taker_fee_rate": 0.0005,
        "maker_fee_rate": 0.0002
    }
}

SUPPORTED_TIMEFRAMES = ["1m", "3m", "5m", "15m", "30m"]
MACRO_TIMEFRAME = "15m"
STRUCTURE_TIMEFRAME = "5m"
EXECUTION_TIMEFRAME = "1m"

# Indicator Settings
EMA_TREND_FAST: int = 50
EMA_TREND_SLOW: int = 200
RSI_PERIOD: int = 14
RSI_OVERSOLD: float = 32.0
RSI_OVERBOUGHT: float = 68.0
MACD_FAST: int = 12
MACD_SLOW: int = 26
MACD_SIGNAL: int = 9
SUPERTREND_PERIOD: int = 10
SUPERTREND_MULTIPLIER: float = 3.0
ATR_PERIOD: int = 14
ADX_PERIOD: int = 14
ADX_TREND_THRESHOLD: float = 20.0
ADX_RANGE_THRESHOLD: float = 20.0
VOLATILITY_SPIKE_ATR_MULT: float = 2.5
VOLUME_MA_PERIOD: int = 20
BE_FEE_BUFFER_ATR: float = 1.0

# ==============================================================================
# AUTONOMOUS UNLIMITED COMPOUNDER & MAKER-FEE DEFENSE ENGINE SETTINGS
# ==============================================================================
# ==============================================================================
# UNLIMITED DYNAMIC COMPOUNDER WITH 75% SAFE VAULT CONFIGURATION
# ==============================================================================
# 1. 75% Safe Profit Vault (Capital Protection)
INITIAL_PRINCIPAL_USD: float = 22.00          # Base Initial Principal Balance ($22.00)
BASE_CAPITAL_FLOOR_USD: float = 22.00         # Absolute untouchable capital baseline
PROFIT_VAULT_LOCK_PCT: float = 0.75           # Permanently lock 75% of cumulative realized profits into Protected Safe Balance
TRADEABLE_PROFIT_PCT: float = 0.25            # Only 25% of realized profits + Initial Principal can be used for trade margin & risk
MAX_EQUITY_RISK_PER_TRADE_PCT: float = 0.03   # Strict Cap: Never risk more than 3% of total equity on any single trade stop-loss
MAX_MARGIN_UTILIZATION_PCT: float = 0.70      # Margin ceiling: strictly limited by tradeable pool
EMERGENCY_PEAK_DRAWDOWN_USD: float = 2.50     # Drawdown Kill-Switch: Halt if equity drops -$2.50 from current session peak
KILL_SWITCH_HALT_HOURS: float = 2.0           # Freeze new trades for 2 hours on kill-switch trigger
PROFIT_LOCK_PERCENTAGE: float = 0.75          # Backward-compat alias (75% lock)
MARGIN_PER_CONTRACT_USD: float = 15.0         # Safe reference margin

# 2. Unlimited Dynamic Executions (No Daily Profit Cap)
DAILY_MAX_TRADES_LIMIT: int = 999999          # Unlimited Dynamic Trade Frequency (Continuous 24/7 execution)
DAILY_PROFIT_TARGET_USD: float = float("inf") # NO hard daily profit ceiling (no pauses at $11.50 or Rs 1,000)
SCAN_INTERVAL_SECONDS: float = 1.0           # 1.0s fast orderbook depth scan on XAUTUSD

# 3. Dynamic Risk-Scaled Order Sizing Parameters (Max Cap 40 Contracts)
MAX_CONTRACTS_CAP: int = 40                   # Hard upper limit: 40 contracts maximum
BASE_ORDER_SIZE_CONTRACTS: int = 10           # Base start: 10 contracts while equity is ~$22 (< $35)
WALLET_GROWTH_SCALE_PCT: float = 0.15         # Backward compatibility alias

TIER_1_MAX_BALANCE: float = 35.0              # Tier 1: Equity < $35 -> 10 contracts
TIER_1_TRADE_CAP: int = 999999
TIER_1_CONTRACT_SIZE: int = 10
TIER_1_TP: float = 3.00
TIER_1_SL: float = 1.00

TIER_2_MAX_BALANCE: float = 55.0              # Tier 2: Equity reaches $35 ($35-$55) -> 20 contracts
TIER_2_TRADE_CAP: int = 999999
TIER_2_CONTRACT_SIZE: int = 20
TIER_2_TP: float = 3.00
TIER_2_SL: float = 1.00

TIER_3_MAX_BALANCE: float = float("inf")      # Tier 3: Equity reaches $55+ -> 40 contracts (Max Cap 40)
TIER_3_TRADE_CAP: int = 999999
TIER_3_CONTRACT_SIZE: int = 40
TIER_3_TP: float = 3.00
TIER_3_SL: float = 1.00

TIER_4_TRADE_CAP: int = 999999
TIER_4_CONTRACT_SIZE: int = 40
TIER_4_TP: float = 3.00
TIER_4_SL: float = 1.00

# 4. Trade Execution, Zero-Risk Breakeven & Quick Scalping Engine
M1_EXECUTION_POST_ONLY: bool = True          # Strict 100% Maker Post-Only Limit Orders
M1_EXECUTION_TIME_IN_FORCE: str = "gtc"      # Strict Maker GTC (Zero taker fee drain)
M1_SL_OFFSET: float = 1.00                   # Hard Stop-Loss: $1.00 gold move (strictly <= 3% total equity risk)
M1_TP_OFFSET: float = 3.00                   # Quick Scalp Target: +$2.50 to +$3.00 gold move (net profit ~$0.25 to $0.30 per trade)
M1_BREAKEVEN_TRIGGER_USD: float = 1.20        # Increased Breakeven Buffer: Move SL ONLY after profit crosses +$1.20 gold points
M1_BREAKEVEN_FEE_COVER_USD: float = 0.15      # Breakeven + Fee Offset: Snap SL to Entry + $0.15 gold points (covers all exchange fees)
M1_DYNAMIC_TRAIL_OFFSET_USD: float = 0.50     # Dynamic Trailing buffer behind peak run-up
M1_TP1_OFFSET: float = 1.20                  # Breakeven trigger level (+1.20 gold points)
M1_TP2_OFFSET: float = 3.00                  # Quick Scalp Target (+3.00 gold points)

# High-Frequency M1 Strategy Setup Triggers
M1_ARROW_STRATEGY: bool = True
M1_DISABLE_15M_TREND_FILTER: bool = True     # Completely bypass higher timeframe 15m trend filter
M1_PURE_BIDIRECTIONAL: bool = True           # Trade BOTH Long and Short purely based on 1-Minute price action
M1_RSI_PERIOD: int = 7                       # Fast RSI (7-period)
M1_RSI_CROSS_BUY: float = 42.0               # Buy Trigger: RSI(7) crosses above 42
M1_RSI_CROSS_SELL: float = 58.0              # Sell Trigger: RSI(7) crosses below 58
M1_RSI_OVERSOLD: float = 46.0                # Buy Trigger RSI hook
M1_RSI_OVERBOUGHT: float = 54.0              # Sell Trigger RSI hook
M1_WICK_RATIO_MIN: float = 0.12              # Wick ratio to capture micro-wick bounces
M1_WICK_BODY_RATIO: float = 0.25             # Wick/body ratio
M1_SWING_LOOKBACK: int = 3                   # Lookback bars for local low / high support & resistance test
MAX_DAILY_NET_LOSS_USD: float = 3.00         # Capital Protection Circuit Breaker: -$3.00 USD

# Backward-compatibility aliases
MIN_RISK_REWARD_RATIO: float = MIN_NET_RISK_REWARD_RATIO
MAX_CONSECUTIVE_LOSSES: int = CONSECUTIVE_LOSS_LIMIT
MAX_RISK_LIMIT_CEILING_PCT: float = MAX_RISK_PER_TRADE_PCT
INITIAL_CAPITAL_USD: float = 20.0                 # Target operational margin baseline ($20 guardrail)
TRAILING_BE_ACTIVATION_ATR: float = 1.5
TRAILING_DISTANCE_ATR: float = 1.0
MAX_RISK_PER_TRADE_USD: float = 0.40
PROFIT_TARGET_USD: float = 1.00
BREAKEVEN_PROFIT_USD: float = 0.50
DAILY_DRAWDOWN_FREEZE_USD: float = 18.50
MICRO_POSITION_SIZE: int = 1

# Persistence paths
DATA_DIR = Path(__file__).resolve().parent / "data"
LOGS_DIR = Path(__file__).resolve().parent / "logs"
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "quant_robot_state.json"
TRADES_LOG_FILE = LOGS_DIR / "trades.log"
APP_LOG_FILE = LOGS_DIR / "quant_robot.log"
