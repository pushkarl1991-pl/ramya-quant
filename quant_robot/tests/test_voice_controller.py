"""
Unit tests for Voice Controller and Command Parser (quant_robot/voice_controller.py)
"""

import pytest

from quant_robot.voice_controller import VoiceController


def test_voice_command_parsing():
    vc = VoiceController()

    action, sym = vc.parse_command("Please buy Gold right now")
    assert action == "BUY"
    assert sym == "XAUTUSD"

    action, sym = vc.parse_command("Sell Bitcoin immediately")
    assert action == "SELL"
    assert sym == "BTCUSD"

    action, sym = vc.parse_command("Emergency close all positions")
    assert action == "PANIC_EXIT"

    action, sym = vc.parse_command("Pause trading engine")
    assert action == "PAUSE"

    action, sym = vc.parse_command("Resume trading")
    assert action == "RESUME"


def test_voice_handler_dispatch():
    spoken_messages = []

    def mock_tts(msg):
        spoken_messages.append(msg)

    vc = VoiceController(tts_callback=mock_tts)
    res = vc.handle_voice_input("Pause trading")
    assert res.success is True
    assert res.action_type == "PAUSE"
    assert len(spoken_messages) == 1
    assert "paused" in spoken_messages[0].lower()
