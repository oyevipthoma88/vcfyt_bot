"""v83: live mic is LOUD and CLEAR (no square wave), and browser bugs stay fixed."""
import asyncio
import os
import re
import shutil
import subprocess
import tempfile

import numpy as np
import pytest

from helpers import audio_processor as ap
from helpers import live_mic_v79 as v79

VOICE = ("aevalsrc='(0.5*sin(2*PI*150*t)+0.3*sin(2*PI*300*t)+0.15*sin(2*PI*900*t)"
         "+0.1*sin(2*PI*2500*t))*(0.2+0.8*abs(sin(2*PI*2.5*t)))':s=48000:d=6")


def _measure(chain):
    with tempfile.TemporaryDirectory() as d:
        wav, opus = os.path.join(d, "o.wav"), os.path.join(d, "o.opus")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", VOICE, "-af",
                        "volume=-18dB," + chain, "-ar", "48000", "-ac", "1", wav], check=True)
        r = subprocess.run(["ffmpeg", "-hide_banner", "-i", wav, "-af", "ebur128=peak=true",
                            "-f", "null", "-"], capture_output=True, text=True).stderr
        lufs = float(re.findall(r"I:\s+(-?[\d.]+) LUFS", r)[-1])
        tp = float(re.findall(r"Peak:\s+(-?[\d.]+)", r)[-1])
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", wav, "-c:a", "libopus", "-b:a", "48k",
                        "-application", "voip", opus], check=True)
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", opus, "-f", "f32le", "-"],
                             capture_output=True, check=True).stdout
        x = np.frombuffer(raw, dtype=np.float32)
        return lufs, tp, float(np.mean(np.abs(x) >= 0.999))


@pytest.fixture(autouse=True)
def _mode():
    old = v79._mode
    yield
    v79._mode = old


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg needed")
@pytest.mark.parametrize("mode", ["loudest", "phone", "bass"])
def test_live_mic_loud_clear_and_opus_safe(mode):
    v79.set_mode(mode)
    lufs, tp, clip = _measure(ap.build_live_mic_filter())
    assert lufs > -12.0, f"{mode}: {lufs} LUFS too quiet"
    assert lufs < -5.0, f"{mode}: {lufs} LUFS = square wave"
    assert tp <= -0.3, f"{mode}: true peak {tp}"
    assert clip < 0.002, f"{mode}: {clip:.2%} clip after Opus"


def test_mode_survives_restart(monkeypatch):
    class FakeDB:
        async def get_app_value(self, key):
            return "clean" if key == "live_mic_loud_mode" else None

    import helpers.database as hdb
    monkeypatch.setattr(hdb, "db", FakeDB(), raising=False)
    v79._mode = None
    assert asyncio.run(v79.load_mode()) == "phone"  # old saved name migrates


def test_browser_never_sends_double_audio():
    from helpers.live_mic import LIVE_MIC_HTML as h
    i = h.index("function useScriptProcessor")
    assert "workletNode.disconnect()" in h[i:i + 900]
    assert "if (!scriptNode) push(e.data, myAttempt)" in h


def test_browser_soft_clip_and_boost_control():
    from helpers.live_mic import LIVE_MIC_HTML as h
    assert "createWaveShaper" in h
    assert 'id="boostR"' in h and "function setBoost" in h
    assert "pre.gain.value = micBoost()" in h
