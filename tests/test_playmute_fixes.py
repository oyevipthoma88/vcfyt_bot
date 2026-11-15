import asyncio
import ast
import json
import os
import tempfile
from unittest.mock import AsyncMock

from plugins.vc_commands import _parse_unmute_loops
from pathlib import Path
from helpers.vc_manager import UserVC


def test_playmute_loops_ignore_chat_target_and_clamp():
    assert _parse_unmute_loops("3") == 3
    assert _parse_unmute_loops("loop") == -1
    assert _parse_unmute_loops("1000") == 100
    assert _parse_unmute_loops("") == 1


def test_mute_preferences_write_through_to_database(monkeypatch):
    async def run():
        m = UserVC.__new__(UserVC)
        m.owner_id = 12
        m.live_volume = 1000
        m.chats = {}
        m.mute_prefs = {}
        m._mute_prefs_save_task = None
        class FakeDB:
            set_app_value = AsyncMock()
        from helpers.vc_manager import _db
        monkeypatch.setattr("helpers.vc_manager._db", lambda: FakeDB())
        st = m.state(-1001234567890)
        st.unmute_file_id = "archived-file-id"
        st.unmute_audio = "/tmp/playmute.ogg"
        m.save_mute_prefs(-1001234567890)
        await m._mute_prefs_save_task
        saved = FakeDB.set_app_value.await_args.args[1]
        payload = json.loads(saved)
        pref = payload["-1001234567890"]
        assert pref["unmute_file_id"] == "archived-file-id"
        assert pref["unmute_audio"] == "/tmp/playmute.ogg"
    asyncio.run(run())


def test_playmute_media_recovery_downloads_archive(monkeypatch):
    async def run():
        m = UserVC.__new__(UserVC)
        m.owner_id = 13
        m.live_volume = 1000
        m.chats = {}
        m.mute_prefs = {-1001234567891: {"unmute_file_id":"archive-id"}}
        m._mute_prefs_save_task = None
        from helpers.logger_channel import set_bot
        from helpers.database import db
        monkeypatch.setattr(db, "set_app_value", AsyncMock())
        fake = AsyncMock()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".ogg")
        tmp.write(b"OggSfake")
        tmp.close()
        fake.download_media.return_value = tmp.name
        monkeypatch.setattr("helpers.logger_channel._bot_client", fake)
        try:
            path = await m._ensure_unmute_audio_file(-1001234567891)
            assert path and os.path.isfile(path)
            fake.download_media.assert_awaited_once_with("archive-id")
        finally:
            try: os.unlink(path)
            except Exception: pass
    asyncio.run(run())


def test_playmute_command_handler_is_registered_not_parser():
    tree = ast.parse(Path("plugins/vc_commands.py").read_text())
    functions = {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert any(isinstance(node, ast.AsyncFunctionDef) for node in [functions["cmd_unmuteaudio"]])
    assert functions["cmd_unmuteaudio"].decorator_list, "PlayMute handler lost its Pyrogram registration"
    assert not functions["_parse_unmute_loops"].decorator_list
