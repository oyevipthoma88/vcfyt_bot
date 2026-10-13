"""Loudness + control rules (run with real FFmpeg)."""
import re
import shutil
import subprocess
import unittest

from helpers.audio_processor import build_ffmpeg_filter, build_live_mic_filter

SRC = "anoisesrc=d=2:c=pink:a=0.05,volume=enable='lt(mod(t,0.5),0.3)'"


def measure(chain):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-f", "lavfi", "-i", SRC, "-af",
                        chain + ",astats=measure_overall=Peak_level+RMS_level:measure_perchannel=0",
                        "-f", "null", "-"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-400:]
    pk = float(re.findall(r"Peak level dB: (\S+)", r.stderr)[-1])
    rms = float(re.findall(r"RMS level dB: (\S+)", r.stderr)[-1])
    return pk, rms


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg missing")
class LoudnessControls(unittest.TestCase):
    def test_playback_loud_with_crackle_guard(self):
        pk, rms = measure(build_ffmpeg_filter(volume=1000))
        self.assertLessEqual(pk, -0.45)
        self.assertGreaterEqual(rms, -2.0)

    def test_overdrive_peak_stays_below_half_db(self):
        pk, _ = measure(build_ffmpeg_filter(volume=2000))
        self.assertLessEqual(pk, -0.45)

    def test_live_mic_default_is_loud_and_guarded(self):
        pk, rms = measure(build_live_mic_filter(loud=120))
        self.assertLessEqual(pk, -0.45)
        self.assertGreaterEqual(rms, -3.0)

    def test_volume_500_is_quieter_than_1000(self):
        _, half = measure(build_ffmpeg_filter(volume=500))
        _, full = measure(build_ffmpeg_filter(volume=1000))
        self.assertLess(half, full - 4.0)

    def test_volume_zero_is_silent(self):
        self.assertIn("volume=0", build_ffmpeg_filter(volume=0))

    def test_fallback_chain_is_a_valid_string(self):
        import inspect
        from helpers import audio_processor
        src = inspect.getsource(audio_processor.process_audio_to_file)
        self.assertNotIn('"volume=36dB,",', src)


if __name__ == "__main__":
    unittest.main()
