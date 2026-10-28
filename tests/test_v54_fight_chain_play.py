"""v55: played audio is very loud AND clean (no square-wave clipping that
Telegram's AGC ducks -> "awaaz dab jaati")."""
import os, re, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from helpers import audio_processor as ap


def test_chain_has_single_clipper_no_gate_after_comp_and_safe_ceiling():
    parts = ap.build_ffmpeg_filter(volume=3000, boost=10).split(",")
    assert sum(p.startswith("asoftclip") for p in parts) <= 1
    comp = next(i for i, p in enumerate(parts) if p.startswith("acompressor"))
    assert not any(p.startswith("agate") for p in parts[comp:])
    assert parts[-1].startswith("alimiter") and "limit=0.8400" in parts[-1]


def _stats(level_db):
    src = tempfile.mktemp(suffix=".wav")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "anoisesrc=d=4:c=pink:a=0.5", "-af",
                    f"bandpass=f=900:w=1500,loudnorm=I={level_db}:TP=-1,aresample=48000", src], check=True)
    af = ap._sanitize_ffmpeg_filter(ap.build_ffmpeg_filter(
        volume=3000, boost=10, input_gain_db=ap.measure_input_gain(src)))
    err = subprocess.run(["ffmpeg", "-hide_banner", "-i", src, "-af",
                          af + ",astats=measure_perchannel=0:measure_overall=RMS_level+Peak_level",
                          "-f", "null", "-"], capture_output=True, text=True).stderr
    os.unlink(src)
    return (float(re.findall(r"RMS level dB: (-?[\d.]+)", err)[-1]),
            float(re.findall(r"Peak level dB: (-?[\d.]+)", err)[-1]))


def test_quiet_normal_loud_all_loud_with_headroom():
    for lvl in (-45, -25, -12):
        rms, peak = _stats(lvl)
        assert rms > -9.0, (lvl, rms)
        assert peak <= -1.4, (lvl, peak)
