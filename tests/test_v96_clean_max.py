"""v96 CLEAN-MAX: teeno play mode tez + bina fate.

Root cause: FFmpeg's ``asoftclip ... :oversample=N`` lets audio out ABOVE
full scale (x2..x3.3), so the next limiter hard-clipped it ("fatti awaaz").
"""
import asyncio
import re
import shutil
import subprocess

import numpy as np
import pytest

import helpers  # noqa: F401
from helpers import audio_processor as ap
from helpers import playback_v77 as v77
from helpers import playback_v96 as v96

HAS_FF = shutil.which("ffmpeg") is not None
MAX = dict(volume=3000, boost=10, gain=500, treble=100, bass=14)


@pytest.fixture(autouse=True)
def _restore_mode():
    old = v77.get_mode()
    yield
    v77.set_mode(old)


def _chain(mode, **kw):
    v77.set_mode(mode)
    return ap.build_ffmpeg_filter(**{**MAX, **kw})


def _peak(af, amp):
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
         f"aevalsrc={amp}*sin(2*PI*440*t):s=48000:d=1", "-af",
         f"aformat=sample_fmts=flt,{af}", "-f", "f32le", "-"],
        capture_output=True, check=True).stdout
    return float(np.abs(np.frombuffer(raw, np.float32)[4800:]).max())


@pytest.mark.parametrize("mode", ["loudest", "phone", "bass"])
def test_no_mode_uses_the_broken_oversampled_softclip(mode):
    c = _chain(mode)
    assert not re.search(r"asoftclip=[^,]*oversample", c)
    assert "aresample=192000,asoftclip=type=tanh,aresample=48000" in c


@pytest.mark.skipif(not HAS_FF, reason="ffmpeg missing")
def test_clean_clip_never_goes_above_full_scale():
    assert _peak(v96.CLEAN_CLIP, 10.0) <= 1.05
    assert _peak(v96.CLEAN_CLIP, 0.05) == pytest.approx(0.05, abs=0.002)  # quiet parts untouched


def test_loudest_and_phone_end_on_lookahead_limiter_at_minus_0_3_db():
    for mode in ("loudest", "phone"):
        last = _chain(mode).split(",")[-1]
        assert last.startswith("alimiter") and "limit=0.9661" in last and "attack=1:" in last


def test_loudest_has_no_multiband_squash():
    assert "mcompand" not in _chain("loudest")


def test_full_boost_drives():
    assert "volume=21.00dB," + v96.CLEAN_CLIP in _chain("loudest")
    assert "volume=22.00dB," + v96.CLEAN_CLIP in _chain("phone")
    assert "volume=11.00dB," + v96.CLEAN_CLIP in _chain("loudest", boost=0)


def test_bass_clippers_get_6_and_7_db_less_drive():
    c = _chain("bass")
    assert c.count("volume=-6.00dB,aresample=192000,asoftclip=type=tanh") == 1
    assert c.count("volume=-7.00dB,aresample=192000,asoftclip=type=tanh") == 1
    assert "virtualbass" in c  # still the heavy-bass chain


def test_mode_band_limits():
    assert _chain("loudest").count("highpass=f=300:p=2") == 3
    assert _chain("loudest").count("lowpass=f=4300:p=2") == 2
    assert _chain("phone").count("highpass=f=250:p=2") == 3
    assert "virtualbass" not in _chain("phone")


def test_treble_and_bass_knobs_still_reach_the_chain():
    for mode in ("loudest", "phone"):
        assert _chain(mode, treble=100) != _chain(mode, treble=30)
    assert _chain("loudest", bass=60) != _chain("loudest", bass=5)


def test_live_mic_untouched():
    v77.set_mode("loudest")
    assert "aresample=192000,asoftclip=type=tanh,aresample=48000" not in ap.build_ffmpeg_filter(live=True)


@pytest.mark.skipif(not HAS_FF, reason="ffmpeg missing")
@pytest.mark.parametrize("mode", ["loudest", "phone", "bass"])
def test_every_mode_loud_after_opus(tmp_path, mode):
    v77.set_mode(mode)
    src = str(tmp_path / "s.wav")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "anoisesrc=d=6:c=pink:a=0.4:seed=7", "-af", "volume=-20dB",
                    "-ar", "48000", "-ac", "2", src], check=True)
    out = asyncio.run(ap.process_audio_to_file(src, str(tmp_path / "o.wav"), **MAX))
    ogg = str(tmp_path / "o.ogg")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", out, "-c:a", "libopus",
                    "-b:a", "48k", "-application", "voip", ogg], check=True)
    o = subprocess.run(["ffmpeg", "-hide_banner", "-i", ogg, "-af", "ebur128",
                        "-f", "null", "-"], capture_output=True, text=True).stderr
    assert float(re.findall(r"I:\s+(-?[\d.]+) LUFS", o)[-1]) >= -6.0
