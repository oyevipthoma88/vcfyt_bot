import asyncio
import os
import pathlib
import re
import subprocess
import tempfile
import unittest

os.environ["OWNER_ID"] = "101"
os.environ["OWNER_IDS"] = "202,303"

from config import Config
from helpers.audio_processor import (
    _sanitize_ffmpeg_filter, build_ffmpeg_filter, build_live_mic_filter,
    process_audio_to_file, volume_to_db,
)
from helpers.database import Database
from plugins.ui import B, ButtonStyle
from helpers.bot_api_styles import markup_payload
from helpers.styled_client import _transport_markup

ROOT = pathlib.Path(__file__).parents[1]

def _mean_volume(path: str) -> float:
    probe = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", path, "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    return float(re.search(r"mean_volume: (-?[\d.]+) dB", probe.stderr).group(1))

class RelayFeatureTests(unittest.TestCase):
    def test_multiple_owners(self):
        self.assertEqual(Config.primary_owner(), 101)
        self.assertTrue(Config.is_owner(101))
        self.assertTrue(Config.is_owner(202))
        self.assertTrue(Config.is_owner(303))
        self.assertFalse(Config.is_owner(404))

    def test_volume_curve_is_monotonic_and_real_db(self):
        levels = [volume_to_db(v) for v in (0, 250, 500, 750, 1000)]
        self.assertEqual(levels, sorted(levels))
        self.assertEqual(volume_to_db(500), 0.0)
        self.assertEqual(volume_to_db(1000), 30.0)

    def test_filter_chain_shape(self):
        af = build_ffmpeg_filter(volume=1000, gain=150, boost=10, echo=False)
        stages = [s.split("=")[0] for s in af.split(",")]
        self.assertEqual(stages[:3], ["highpass", "aresample", "dynaudnorm"])
        self.assertEqual(stages[-1], "alimiter")
        self.assertIn("acompressor", stages)
        self.assertIn("volume=42.00dB", af)
        self.assertNotIn("aecho=", af)
        self.assertIn("loudnorm=I=-5", af)

    def test_live_mic_filter_is_aggressive_and_ffmpeg_valid(self):
        af = build_live_mic_filter()
        self.assertIn("volume=30dB", af)
        self.assertIn("ratio=20", af)
        self.assertIn("alimiter=limit=0.995", af)
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "mic.wav")
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
                 "-af", af, "-ar", "48000", "-ac", "2", out], check=True,
            )
            self.assertTrue(os.path.exists(out))

    def test_filter_uses_ffmpeg_compatible_compressor_and_limiter_ranges(self):
        af = build_ffmpeg_filter(volume=500, gain=0, boost=0, echo=False)
        self.assertNotIn("knee=0", af)
        self.assertIn("knee=1", af)
        self.assertNotIn("attack=0:", af)
        self.assertIn("attack=0.1", af)

    def test_legacy_invalid_filter_options_are_sanitized(self):
        af = _sanitize_ffmpeg_filter("acompressor=knee=0,alimiter=attack=0")
        self.assertEqual(af, "acompressor=knee=1,alimiter=attack=0.1")

    def test_button_style_enum_and_custom_emoji_id_are_compatible(self):
        button = B("Support", url="https://example.com",
                   style=ButtonStyle.SUCCESS,
                   icon_custom_emoji_id=5443038326535759644)
        self.assertEqual(button.text, "✅ Support")
        self.assertEqual(button.url, "https://example.com")

    def test_auto_button_semantics_are_visible_without_emoji(self):
        button = B("Logout", callback_data="menu:logout")
        self.assertEqual(button.text, "❌ Logout")
        self.assertEqual(button.callback_data, "menu:logout")

    def test_bot_api_bridge_emits_native_style_without_numeric_emoji_id(self):
        from pyrogram.types import InlineKeyboardMarkup
        markup = InlineKeyboardMarkup([[
            B("Support", url="https://example.com",
              style=ButtonStyle.SUCCESS,
              icon_custom_emoji_id=5443038326535759644),
        ]])
        payload = markup_payload(markup)
        self.assertEqual(payload["inline_keyboard"][0][0]["style"], "success")
        self.assertNotIn("icon_custom_emoji_id", payload["inline_keyboard"][0][0])
        self.assertEqual(payload["inline_keyboard"][0][0]["text"], "Support")

    def test_styled_dm_transport_omits_initial_markup(self):
        from plugins.ui import home_kb
        self.assertIsNone(_transport_markup(home_kb()))

    def test_echo_only_when_enabled(self):
        self.assertIn("aecho=", build_ffmpeg_filter(echo=True, echo_level=5))
        self.assertNotIn("aecho=", build_ffmpeg_filter(echo=True, echo_level=0))

    def test_ffmpeg_filter_is_valid_and_makes_quiet_audio_loud(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                src = os.path.join(d, "quiet.wav")
                subprocess.run(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                     "-i", "sine=frequency=440:duration=3", "-af", "volume=0.02", src],
                    check=True,
                )
                before = _mean_volume(src)
                out = await process_audio_to_file(src, os.path.join(d, "loud.wav"),
                                                  volume=1000, gain=150, boost=10, echo=False)
                after = _mean_volume(out)
                self.assertLess(before, -30)
                self.assertGreater(after, -8)
                self.assertGreater(after - before, 25)

        asyncio.run(run())

    def test_vc_manager_uses_real_pytgcalls_api(self):
        from pytgcalls.types import ChatUpdate
        text = (ROOT / "helpers" / "vc_manager.py").read_text()
        for status in re.findall(r"ChatUpdate\.Status\.([A-Z_]+)", text):
            self.assertTrue(hasattr(ChatUpdate.Status, status), status)
        self.assertIn("NoActiveGroupCall", text)
        self.assertIn("change_volume_call", text)

    def test_no_dead_button_hack(self):
        self.assertFalse((ROOT / "helpers" / "buttons.py").exists())
        for py in (ROOT / "plugins").glob("*.py"):
            self.assertNotIn("helpers.buttons", py.read_text(), py.name)

    def test_shared_audio_round_trip_and_scope(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                db = Database()
                db._sqlite_path = os.path.join(directory, "bot.db")
                await db.connect()
                owner_id = await db.add_audio(101, "Owner Intro", "file-owner", "audio")
                user_id = await db.add_audio(404, "Private Clip", "file-user", "audio")
                owner_items = await db.list_bot_audio(101)
                user_items = await db.list_user_audio(404)
                self.assertEqual([x["audio_id"] for x in owner_items], [owner_id])
                self.assertEqual([x["audio_id"] for x in user_items], [user_id])
                self.assertEqual((await db.get_audio(owner_id))["file_id"], "file-owner")
                self.assertTrue(await db.delete_audio(404, user_id))
                self.assertFalse(await db.list_user_audio(404))
                available = await db.list_available_audio(404, 101)
                self.assertEqual(len(available), 1)
                self.assertEqual(available[0]["file_id"], "file-owner")

        asyncio.run(run())

    def test_external_media_integration_is_removed(self):
        downloader = "yt-" + "dlp"
        for path in (ROOT / "README.md", ROOT / "requirements.txt"):
            self.assertNotIn(downloader, path.read_text().lower())
        removed_fn = "download_" + "yt"
        self.assertNotIn(removed_fn, (ROOT / "helpers" / "audio_processor.py").read_text())
        self.assertNotIn(removed_fn, (ROOT / "plugins" / "vc_commands.py").read_text())

if __name__ == "__main__":
    unittest.main()
