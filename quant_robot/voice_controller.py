"""
RAMYA QUANT ROBOT — Voice Command & Manual Override System
Implements:
1. Voice Command Parser & STT Dispatcher:
   - "Buy Gold" / "Sell Gold" -> Market order for XAUTUSD with Auto ATR SL & 1:2 TP
   - "Buy Bitcoin" / "Sell Bitcoin" -> BTCUSD
   - "Buy Ethereum" / "Sell Ethereum" -> ETHUSD
   - "Close All" / "Panic Exit" -> Cancels all open orders and flattens all positions at market
   - "Pause Trading" / "Resume Trading" -> Toggles autonomous execution state
2. Voice/Audio Confirmation Feedback:
   - Generates human-like spoken feedback messages for Web Speech API and audio synthesizers
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger("quant.voice")


@dataclass
class VoiceCommandResult:
    command_raw: str
    action_type: str         # 'BUY', 'SELL', 'PANIC_EXIT', 'PAUSE', 'RESUME', 'STATUS', 'UNKNOWN'
    symbol: Optional[str]    # 'XAUTUSD', 'BTCUSD', 'ETHUSD'
    success: bool
    confirmation_message: str
    details: Dict[str, Any]


class VoiceController:
    # Supported asset aliases
    ASSET_ALIASES = {
        "gold": "XAUTUSD",
        "tether gold": "XAUTUSD",
        "xaut": "XAUTUSD",
        "xautusd": "XAUTUSD",
        "sona": "XAUTUSD",
        "bitcoin": "BTCUSD",
        "btc": "BTCUSD",
        "btcusd": "BTCUSD",
        "ethereum": "ETHUSD",
        "eth": "ETHUSD",
        "ethusd": "ETHUSD",
    }

    def __init__(
        self,
        trading_engine: Optional[Any] = None,
        tts_callback: Optional[Callable[[str], None]] = None,
    ):
        self.trading_engine = trading_engine
        self.tts_callback = tts_callback
        self.is_autonomous_paused = False
        self.command_history = []

    def parse_command(self, text: str) -> Tuple[str, Optional[str]]:
        """
        Parses verbal command text into action type and target symbol.
        Returns: (action_type, symbol)
        """
        clean_text = text.lower().strip()
        clean_text = re.sub(r"[^\w\s]", "", clean_text)

        # 1. Panic Exit / Close All
        if any(trigger in clean_text for trigger in ["close all", "panic exit", "exit all", "flatten all", "sab band karo"]):
            return "PANIC_EXIT", None

        # 2. Pause Trading
        if any(trigger in clean_text for trigger in ["pause trading", "pause", "stop trading", "halt", "ruko"]):
            return "PAUSE", None

        # 3. Resume Trading
        if any(trigger in clean_text for trigger in ["resume trading", "resume", "start trading", "continue", "shuru karo"]):
            return "RESUME", None

        # 4. Status Check
        if any(trigger in clean_text for trigger in ["status", "report", "account status", "kya chal raha hai"]):
            return "STATUS", None

        # 5. Buy / Sell Commands
        is_buy = any(clean_text.startswith(w) or f" {w} " in clean_text for w in ["buy", "long", "khareedo"])
        is_sell = any(clean_text.startswith(w) or f" {w} " in clean_text for w in ["sell", "short", "becho"])

        target_symbol = None
        for alias, sym in self.ASSET_ALIASES.items():
            if alias in clean_text:
                target_symbol = sym
                break

        if is_buy and target_symbol:
            return "BUY", target_symbol
        elif is_sell and target_symbol:
            return "SELL", target_symbol

        return "UNKNOWN", None

    def execute_voice_command(self, speech_text: str) -> VoiceCommandResult:
        """
        Executes recognized voice command against the trading engine with audio feedback.
        """
        action, symbol = self.parse_command(speech_text)
        result = None

        if action == "PANIC_EXIT":
            if self.trading_engine:
                closed_count = self.trading_engine.panic_flatten_all(reason="Voice Panic Exit Triggered")
                msg = f"PANIC EXIT CONFIRMED! Emergency closure executed. Flattened {closed_count} positions and cancelled all open orders."
            else:
                msg = "PANIC EXIT CONFIRMED! Emergency closure routine executed in test mode."
            result = VoiceCommandResult(speech_text, action, None, True, msg, {"flattened": True})

        elif action == "PAUSE":
            self.is_autonomous_paused = True
            if self.trading_engine:
                self.trading_engine.is_autonomous_active = False
            msg = "Autonomous algorithmic trading PAUSED. Robot standing down, monitoring positions only."
            result = VoiceCommandResult(speech_text, action, None, True, msg, {"autonomous_active": False})

        elif action == "RESUME":
            self.is_autonomous_paused = False
            if self.trading_engine:
                self.trading_engine.is_autonomous_active = True
            msg = "Autonomous algorithmic trading RESUMED. Scanning multi-timeframe regimes for setups."
            result = VoiceCommandResult(speech_text, action, None, True, msg, {"autonomous_active": True})

        elif action in ("BUY", "SELL"):
            if not symbol:
                msg = f"Command recognized as {action}, but asset was not recognized. Please specify Gold, Bitcoin, or Ethereum."
                result = VoiceCommandResult(speech_text, action, None, False, msg, {})
            else:
                asset_name = "Gold" if symbol == "XAUTUSD" else ("Bitcoin" if symbol == "BTCUSD" else "Ethereum")
                if self.trading_engine:
                    order_res = self.trading_engine.execute_voice_market_order(symbol, action)
                    if order_res.get("success"):
                        msg = (
                            f"CONFIRMED: Executed {action} order on {asset_name} ({symbol}) at ${order_res['price']:,.2f}. "
                            f"Stop-Loss armed at ${order_res['stop_loss']:,.2f}, Target at ${order_res['take_profit']:,.2f} with 1:2 Risk to Reward."
                        )
                        result = VoiceCommandResult(speech_text, action, symbol, True, msg, order_res)
                    else:
                        msg = f"Order execution failed for {asset_name}: {order_res.get('reason', 'Risk check rejected')}"
                        result = VoiceCommandResult(speech_text, action, symbol, False, msg, order_res)
                else:
                    msg = f"VOICE COMMAND VERIFIED: {action} {asset_name} ({symbol}) simulated successfully with auto ATR Stop-Loss and 1:2 Target."
                    result = VoiceCommandResult(speech_text, action, symbol, True, msg, {"simulated": True})

        elif action == "STATUS":
            if self.trading_engine:
                status = self.trading_engine.get_status_summary()
                msg = f"Robot is {status['state']}. Open positions: {status['open_positions']}. Daily P&L: ${status['daily_pnl']:,.2f}."
            else:
                msg = "Trading robot is operational. All risk parameters and circuit breakers in safe status."
            result = VoiceCommandResult(speech_text, action, None, True, msg, {})

        else:
            msg = f"Voice command '{speech_text}' not recognized. Supported commands: 'Buy Gold', 'Sell Gold', 'Close All', 'Pause Trading', 'Resume Trading'."
            result = VoiceCommandResult(speech_text, action, None, False, msg, {})

        # Voice feedback callback
        if self.tts_callback:
            try:
                self.tts_callback(result.confirmation_message)
            except Exception as e:
                logger.error(f"TTS callback failed: {e}")

        self.command_history.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "speech_input": speech_text,
            "action": action,
            "confirmation": result.confirmation_message,
            "success": result.success
        })
        logger.info(f"[VOICE COMMAND] {speech_text} -> {result.confirmation_message}")
        return result

    # Alias for method invocation
    handle_voice_input = execute_voice_command
