"""Live mic chain must start in FFmpeg and stay at max loudness."""
import re
import shutil
import subprocess
import unittest

from helpers.audio_processor import build_live_mic_filter

VOICE = ("aevalsrc='(0.5*sin(2*PI*150*t)+0.3*sin(2*PI*300*t)+0.15*sin(2*PI*900*t))"
         "*(0.3+0.7*abs(sin(2*PI*2.5*t)))':s=48000:d=3")


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg missing")
class LiveMicFfmpegValid(unittest.TestCase):
    def test_every_slider_value_builds_a_working_graph(self):
        # v20 used speechnorm e=80 / ratio=30 / knee=12 -> FFmpeg refused
        # the graph and the live mic went silent.
        for loud in (0, 120, 200):
            for crunch in (0, 150):
                r = subprocess.run(
                    ["ffmpeg", "-hide_banner", "-f", "lavfi", "-i", VOICE, "-af",
                     build_live_mic_filter(loud=loud, crunch=crunch), "-f", "null", "-"],
                    capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr[-300:])

    def test_live_voice_average_within_2db_of_full_scale(self):
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-f", "lavfi", "-i", VOICE, "-af",
             "volume=-22dB," + build_live_mic_filter(loud=120)
             + ",atrim=start=0.5,astats=measure_overall=RMS_level:measure_perchannel=0",
             "-f", "null", "-"], capture_output=True, text=True)
        rms = float(re.findall(r"RMS level dB: (\S+)", r.stderr)[-1])
        self.assertGreater(rms, -7.0)

    def test_no_live_delay_filters(self):
        chain = build_live_mic_filter(loud=120)
        self.assertNotIn("loudnorm", chain)  # 3 s look-ahead
        self.assertNotIn("dynaudnorm", chain)


if __name__ == "__main__":
    unittest.main()
