"""v95 CLEAR-LOUDEST: loudest mode stays loud but must not end in a hard
clip (that square-wave tail made Telegram's Opus overshoot ~+7 dB and the
listener's phone re-clipped ~20 % of samples -> "aawaz fatti hai")."""
import re
import shutil
import subprocess

import pytest

import helpers  # noqa: F401
from helpers import audio_processor as ap
from helpers import playback_v77 as v77


def _loudest(**kw):
    v77.set_mode("loudest")
    try:
        return ap.build_ffmpeg_filter(volume=1000, boost=10, gain=500, treble=100, **kw)
    finally:
        v77.set_mode("phone")


def test_clip_harmonics_are_filtered_before_the_final_limiter():
    parts = _loudest().split(",")
    last_clip = max(i for i, p in enumerate(parts) if p.startswith("asoftclip"))
    limiter = max(i for i, p in enumerate(parts) if p.startswith("alimiter"))
    between = parts[last_clip + 1:limiter]
    assert between.count("lowpass=f=4300:p=2") == 2


def test_final_stage_is_a_lookahead_limiter_not_a_hard_clip():
    last = [p for p in _loudest().split(",") if p.startswith("alimiter")][-1]
    assert float(re.search(r"attack=([\d.]+)", last).group(1)) == 5
    assert float(re.search(r"limit=([\d.]+)", last).group(1)) == 0.98


def test_clippers_are_8x_oversampled():
    clips = [p for p in _loudest().split(",") if p.startswith("asoftclip")]
    assert clips and all("oversample=8" in c for c in clips)


def test_loudest_keeps_its_loud_push():
    c = _loudest()
    assert "mcompand" in c and "volume=28.00dB" in c and "volume=6dB" in c


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg missing")
def test_ffmpeg_accepts_the_loudest_chain():
    af = ap._sanitize_ffmpeg_filter(_loudest())
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=f=440:d=1", "-af", af, "-f", "null", "-"],
        capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-400:]
