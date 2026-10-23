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
        # v30: peak stops at the -1.2 dBFS ceiling (Opus decode headroom,
        # no "fat" clip after Telegram), RMS stays loud.
        chain = build_ffmpeg_filter(volume=1000)
        pk, rms = measure(chain)
        self.assertGreaterEqual(pk, -2.0)
        self.assertLessEqual(pk, -1.0)
        self.assertGreaterEqual(rms, -11.0)  # test signal is 40% silence

    def test_overdrive_peak_hits_full_scale_without_overs(self):
        pk, rms = measure(build_ffmpeg_filter(volume=2000))
        self.assertGreaterEqual(pk, -2.0)
        self.assertLessEqual(pk, -1.0)
        self.assertGreaterEqual(rms, -11.0)  # test signal is 40% silence

    def test_live_mic_default_is_loud_and_guarded(self):
        # v20 ABSOLUTE MAX loudness: quad compressor density stack, mean near 0 dBFS.
        pk, rms = measure(build_live_mic_filter(loud=120))
        self.assertLessEqual(pk, 0.1)       # v20: ceiling at 0.999
        self.assertGreaterEqual(pk, -3.0)
        self.assertGreaterEqual(rms, -11.0)  # test signal is 40% silence  # clean brick-wall: peak ke ~5 dB neeche
        # v20: with extreme density, crest can be as low as 1.5 dB.
        self.assertGreaterEqual(pk - rms, 1.5)

    def test_live_mic_quiet_phone_not_gated(self):
        # Old double gate muted a -20 dBFS phone completely (stutter/glitch).
        voice = ("aevalsrc='(0.5*sin(2*PI*150*t)+0.3*sin(2*PI*300*t)+0.15*sin(2*PI*900*t))"
                 "*(0.3+0.7*abs(sin(2*PI*2.5*t)))':s=48000:d=4")
        for lvl in (-6, -20, -32):
            r = subprocess.run(["ffmpeg", "-hide_banner", "-f", "lavfi", "-i", voice, "-af",
                                f"volume={lvl + 8}dB," + build_live_mic_filter(-1.0, loud=120)
                                + ",astats=measure_overall=Peak_level+RMS_level+Flat_factor:measure_perchannel=0",
                                "-f", "null", "-"], capture_output=True, text=True)
            rms = float(re.findall(r"RMS level dB: (\S+)", r.stderr)[-1])
            pk = float(re.findall(r"Peak level dB: (\S+)", r.stderr)[-1])
            self.assertGreater(rms, -20.0, f"input {lvl} dBFS too quiet")
            self.assertLessEqual(pk, -1.0, f"input {lvl} dBFS over the ceiling")
        noisy = ("anoisesrc=d=2:c=white:a=0.002")
        r = subprocess.run(["ffmpeg", "-hide_banner", "-f", "lavfi", "-i", noisy, "-af",
                            build_live_mic_filter() + ",atrim=start=0.5,astats=measure_overall=RMS_level:measure_perchannel=0",
                            "-f", "null", "-"], capture_output=True, text=True)
        floor = float(re.findall(r"RMS level dB: (\S+)", r.stderr)[-1])
        self.assertLess(floor, -20.0)

    def test_live_mic_cuts_other_fighter_bleed(self):
        # record_22 / v30: the other fighter heard from the phone speaker at
        # ~-50 dBFS must NOT come out of the relay.  The own-voice gate
        # (CloseVoiceGate) runs in front of FFmpeg, exactly like live_mic.
        import numpy as np
        from helpers.audio_processor import CloseVoiceGate
        sr = 48000
        t = np.arange(3 * sr) / sr
        tone = (0.5 * np.sin(2 * np.pi * 150 * t) + 0.3 * np.sin(2 * np.pi * 300 * t)
                + 0.15 * np.sin(2 * np.pi * 900 * t)) * (0.3 + 0.7 * np.abs(np.sin(2 * np.pi * 2.5 * t)))
        out = {}
        for name, lvl in (("own", -12), ("bleed", -50)):
            x = tone / np.abs(tone).max() * 10 ** (lvl / 20)
            pcm = (x * 32767).astype(np.int16).tobytes()
            g = CloseVoiceGate()
            gated = b"".join(g.process(pcm[i:i + 1920]) for i in range(0, len(pcm), 1920))
            r = subprocess.run(["ffmpeg", "-hide_banner", "-f", "s16le", "-ar", "48000", "-ac", "1",
                                "-i", "-", "-af", build_live_mic_filter(-1.0)
                                + ",atrim=start=0.5,astats=measure_overall=RMS_level:measure_perchannel=0",
                                "-f", "null", "-"], input=gated, capture_output=True)
            out[name] = float(re.findall(r"RMS level dB: (\S+)", r.stderr.decode())[-1])
        self.assertGreater(out["own"], -20.0)
        self.assertLess(out["bleed"], -45.0)
        self.assertGreater(out["own"] - out["bleed"], 30.0)

    def test_playback_does_not_pump_hiss(self):
        # Pure background hiss through the playback chain must stay far
        # below the voice (old chain lifted it to ~-2 dBFS = "khar khar").
        r = subprocess.run(["ffmpeg", "-hide_banner", "-f", "lavfi", "-i", "anoisesrc=d=3:c=pink:a=0.004",
                            "-af", build_ffmpeg_filter(volume=1000) + ",atrim=start=1,astats=measure_overall=RMS_level:measure_perchannel=0",
                            "-f", "null", "-"], capture_output=True, text=True)
        floor = float(re.findall(r"RMS level dB: (\S+)", r.stderr)[-1])
        self.assertLess(floor, -12.0)

    def test_low_saved_volume_still_plays_full_loud(self):
        # PERMANENT MAX: a low saved slider must never make playback quieter.
        self.assertEqual(build_ffmpeg_filter(volume=300), build_ffmpeg_filter(volume=3000))
        self.assertNotIn("volume=0,", build_ffmpeg_filter(volume=0) + ",")

    def test_quiet_mode_only_with_env(self):
        import os
        os.environ["PLAY_ALLOW_QUIET"] = "1"
        try:
            self.assertIn("volume=0", build_ffmpeg_filter(volume=0))
        finally:
            os.environ.pop("PLAY_ALLOW_QUIET")

    def test_fallback_chain_is_a_valid_string(self):
        import inspect
        from helpers import audio_processor
        src = inspect.getsource(audio_processor.process_audio_to_file)
        self.assertNotIn('"volume=36dB,",', src)

    def test_ogg_voice_note_streams_without_crash(self):
        import os, shlex, tempfile
        from helpers.audio_processor import build_stream_command
        ogg = os.path.join(tempfile.mkdtemp(), "v.ogg")
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=d=1", "-c:a", "libopus", ogg],
                       check=True, capture_output=True)
        cmd = build_stream_command(ogg)
        self.assertNotIn("avioflags", cmd)
        r = subprocess.run(shlex.split(cmd), capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        self.assertNotIn(b"CRC mismatch", r.stderr)
        self.assertGreater(len(r.stdout), 100000)


if __name__ == "__main__":
    unittest.main()
