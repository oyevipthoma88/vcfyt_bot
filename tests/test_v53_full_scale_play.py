"""v53: every played file (quiet / normal / loud) is pushed to 0.00 dBFS."""
import os, re, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from helpers import audio_processor as ap


def _peak_after_chain(level_db: float) -> float:
    src = tempfile.mktemp(suffix=".wav")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "sine=f=440:d=4", "-af", f"volume={level_db}dB", "-ar", "48000", src], check=True)
    af = ap._sanitize_ffmpeg_filter(ap.build_ffmpeg_filter(input_gain_db=ap.measure_input_gain(src)))
    err = subprocess.run(["ffmpeg", "-hide_banner", "-i", src, "-af",
                          af + ",astats=measure_perchannel=0:measure_overall=Peak_level",
                          "-f", "null", "-"], capture_output=True, text=True).stderr
    os.unlink(src)
    return float(re.findall(r"Peak level dB: (-?[\d.]+)", err)[-1])


def test_default_ceiling_is_zero_dbfs():
    assert ap.play_ceiling_db() == 0.0
    tail = ap.full_scale_push_tail()
    assert tail[-1].startswith("alimiter") and "limit=1.0000" in tail[-1]


def test_quiet_and_loud_files_both_reach_full_scale():
    for lvl in (-40.0, -20.0, -1.0):
        assert _peak_after_chain(lvl) > -0.1, lvl
