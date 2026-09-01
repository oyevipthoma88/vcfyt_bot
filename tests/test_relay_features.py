import asyncio
import os
import tempfile
import pathlib
import unittest

os.environ["OWNER_ID"] = "101"
os.environ["OWNER_IDS"] = "202,303"

from config import Config
from helpers.audio_processor import build_ffmpeg_filter
from helpers.database import Database


class RelayFeatureTests(unittest.TestCase):
    def test_multiple_owners(self):
        self.assertEqual(Config.primary_owner(), 101)
        self.assertTrue(Config.is_owner(101))
        self.assertTrue(Config.is_owner(202))
        self.assertTrue(Config.is_owner(303))
        self.assertFalse(Config.is_owner(404))

    def test_relay_filter_contains_controls(self):
        af = build_ffmpeg_filter(
            volume=200, bass=10, gain=30, treble=40, echo=False, boost=0,
            relay_volume=200
        )
        self.assertIn("volume=-4.80dB", af)
        self.assertIn("volume=-1.20dB", af)
        self.assertIn("equalizer=f=3000", af)
        self.assertIn("equalizer=f=8000", af)
        self.assertNotIn("aecho=", af)

    def test_microphone_bridge_is_wired(self):
        source = pathlib.Path(__file__).parents[1] / "helpers" / "vc_manager.py"
        text = source.read_text()
        self.assertIn("MediaDevices.microphone_devices()", text)
        self.assertIn("async def play_microphone", text)
        self.assertIn("MediaSource.SHELL", text)
        self.assertIn("AudioStream(", text)
        self.assertIn("build_ffmpeg_filter(", text)
        self.assertIn('"-f", "s16le"', text)

    def test_mic_command_is_registered(self):
        source = pathlib.Path(__file__).parents[1] / "plugins" / "vc_commands.py"
        text = source.read_text()
        self.assertIn("async def cmd_mic", text)
        self.assertIn("/mic devices", text)
        self.assertIn("/mic on", text)

    def test_live_join_uses_saved_volume(self):
        source = pathlib.Path(__file__).parents[1] / "helpers" / "vc_manager.py"
        text = source.read_text()
        self.assertIn("st.live_volume, quiet=True", text)
        self.assertNotIn("VOL_MAX if st.auto else Config.LIVE_BOOST_DEFAULT", text)

    def test_unsupported_claims_and_noops_are_removed(self):
        root = pathlib.Path(__file__).parents[1]
        combined = "\n".join(
            (root / name).read_text()
            for name in ("README.md", "config.py", "helpers/vc_manager.py",
                         "helpers/logger_channel.py", "plugins/vc_commands.py")
        )
        self.assertNotIn("speechnorm", combined.lower())
        self.assertNotIn("log_auto_pause", combined)
        self.assertNotIn("log_mute_action", combined)
        self.assertNotIn("log_external_mute", combined)
        self.assertNotIn("gain=80", combined.replace(" ", ""))

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

        asyncio.run(run())

    def test_transport_aliases_and_start_picture_are_wired(self):
        root = pathlib.Path(__file__).parents[1]
        commands = (root / "plugins" / "vc_commands.py").read_text()
        config = (root / "config.py").read_text()
        start = (root / "plugins" / "start.py").read_text()
        self.assertIn("(stop|end|leave)", commands)
        self.assertIn("START_PIC", config)
        self.assertIn("reply_photo", start)

    def test_settings_round_trip(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                db = Database()
                db._sqlite_path = os.path.join(directory, "bot.db")
                await db.connect()
                await db.save_settings(
                    101, relay_volume=275, gain=90, bass=60,
                    treble=15, voice="male", live_volume=18000
                )
                settings = await db.get_settings(101)
                self.assertEqual(settings["relay_volume"], 275)
                self.assertEqual(settings["gain"], 90)
                self.assertEqual(settings["bass"], 60)
                self.assertEqual(settings["treble"], 15)
                self.assertEqual(settings["voice"], "male")
                self.assertEqual(settings["live_volume"], 18000)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
