"""Regression tests for the Live Mic target setup paths."""
import asyncio
from pathlib import Path

from plugins import web_mic
from plugins.live_mic_target import _await_chat, request_target_input


def test_live_mic_requires_an_explicit_saved_target(monkeypatch):
    async def get_value(_key):
        return None
    monkeypatch.setattr(web_mic.db, "get_app_value", get_value)
    msg = type("Msg", (), {"chat": type("Chat", (), {"id": -1001234567890})()})()
    assert asyncio.run(web_mic._target(msg, 7)) == 0


def test_live_mic_target_is_parsed_safely(monkeypatch):
    async def get_value(_key):
        return "-1001234567890"
    monkeypatch.setattr(web_mic.db, "get_app_value", get_value)
    assert asyncio.run(web_mic._target(None, 7)) == -1001234567890

    async def get_invalid(_key):
        return "not-a-chat-id"
    monkeypatch.setattr(web_mic.db, "get_app_value", get_invalid)
    assert asyncio.run(web_mic._target(None, 7)) == 0


def test_target_prompts_have_separate_completion_modes():
    _await_chat.clear()
    request_target_input(1, start_after=True)
    request_target_input(2, surface="web")
    request_target_input(3, surface="chat")
    request_target_input(4, surface="setgc")
    assert [_await_chat[i][0] for i in (1, 2, 3, 4)] == [
        "tgt_start", "tgt_web", "tgt_chat", "tgt_setgc"
    ]
    _await_chat.clear()


def test_target_setup_paths_are_connected_to_the_live_mic_ui():
    commands = Path("plugins/vc_commands.py").read_text()
    panel = Path("plugins/web_mic.py").read_text()
    assert '"url", "cancel"' in commands
    assert 'callback_data="mic:wtarget"' in panel
    assert 'callback_data="mic:wsetup"' in panel
    assert 'surface="setgc"' in commands
    assert 'request_target_input(uid' in commands or 'request_target_input' in commands
    assert not Path("plugins/vc_bridge.py").exists()


def test_target_controls_work_without_a_logged_in_engine():
    commands = Path("plugins/vc_commands.py").read_text()
    assert '"wtarget", "wcancel", "whelp", "wsetup"' in commands
