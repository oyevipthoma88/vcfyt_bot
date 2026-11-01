import unittest

from helpers import audio_processor as ap
from helpers import playback_v71 as v71


class V71AntiDuck(unittest.TestCase):
    def setUp(self):
        v71.install()

    def test_bass_cut_at_150hz(self):
        self.assertIn("highpass=f=150:p=2", ap.build_ffmpeg_filter())

    def test_no_bass_boost_in_fight_chain(self):
        self.assertNotIn("lowshelf", ap.build_ffmpeg_filter())

    def test_mono_coherent_and_ceiling(self):
        f = ap.build_ffmpeg_filter()
        self.assertIn("pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1", f)
        self.assertIn("limit=0.8912", f)

    def test_max_push_16db(self):
        self.assertEqual(v71.push_db(None), 16.0)


if __name__ == "__main__":
    unittest.main()
