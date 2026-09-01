import asyncio
import os
import tempfile
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
            volume=200, bass=10, gain=30, treble=40, echo=False, boost=0
        )
        self.assertIn("volume=2.000", af)
        self.assertIn("volume=1.30", af)
        self.assertIn("equalizer=f=3000", af)
        self.assertIn("equalizer=f=8000", af)
        self.assertNotIn("aecho=", af)

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
