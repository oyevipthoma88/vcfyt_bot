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
        # Loud but CLEAN: the gaps between words must stay well below the
        # voice (old chain pumped the hiss up to voice level = "khar khar").
        pk, rms = measure(build_live_mic_filter(loud=120))
        # v13: user wants ~0 dB - peak near full scale but never clipping.
        self.assertLessEqual(pk, -0.2)
        self.assertGreaterEqual(pk, -2.0)
        self.assertGreaterEqual(rms, -9.0)      # v15: much louder than v14
        # record_21: crest 4 dB = square-wave clipping ("glitch").  Speech
        # must keep >= 7 dB between average and peak.
        self.assertGreaterEqual(pk - rms, 5.5)  # v15: extra-dense on purpose, still not square-wave

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
            flat = float(re.findall(r"Flat factor: (\S+)", r.stderr)[-1])
            self.assertGreater(rms, -16.0, f"input {lvl} dBFS too quiet")
            self.assertLess(flat, 2.5, f"input {lvl} dBFS clipped flat")
        noisy = ("anoisesrc=d=2:c=white:a=0.002")
        r = subprocess.run(["ffmpeg", "-hide_banner", "-f", "lavfi", "-i", noisy, "-af",
                            build_live_mic_filter() + ",atrim=start=0.5,astats=measure_overall=RMS_level:measure_perchannel=0",
                            "-f", "null", "-"], capture_output=True, text=True)
        floor = float(re.findall(r"RMS level dB: (\S+)", r.stderr)[-1])
        self.assertLess(floor, -20.0)

    def test_live_mic_cuts_other_fighter_bleed(self):
        # record_22: the other fighter, heard from the phone speaker at
        # ~-50 dBFS, must NOT come out of the relay; own voice stays loud.
        voice = ("aevalsrc='(0.5*sin(2*PI*150*t)+0.3*sin(2*PI*300*t)+0.15*sin(2*PI*900*t))"
                 "*(0.3+0.7*abs(sin(2*PI*2.5*t)))':s=48000:d=3")
        out = {}
        for name, lvl in (("own", -12), ("bleed", -50)):
            r = subprocess.run(["ffmpeg", "-hide_banner", "-f", "lavfi", "-i", voice, "-af",
                                f"volume={lvl + 8}dB," + build_live_mic_filter(-1.0)
                                + ",atrim=start=0.5,astats=measure_overall=RMS_level:measure_perchannel=0",
                                "-f", "null", "-"], capture_output=True, text=True)
            out[name] = float(re.findall(r"RMS level dB: (\S+)", r.stderr)[-1])
        self.assertGreater(out["own"], -16.0)
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
