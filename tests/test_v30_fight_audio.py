"""v30 fight-audio rules: clean-loud chains, own-voice gate, volume guard.

Run: python -m pytest tests/test_v30_fight_audio.py
"""
import asyncio
import os
import shutil
import subprocess
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers import audio_processor as ap  # noqa: E402

SR = 48000
HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def _speech(seconds=6.0, seed=3):
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * SR)) / SR
    f0 = 140 + 30 * np.sin(2 * np.pi * 0.7 * t)
    ph = 2 * np.pi * np.cumsum(f0) / SR
    x = sum((1.0 / k) * np.sin(k * ph) for k in range(1, 30))
    env = np.clip(np.sin(2 * np.pi * 3.0 * t), 0, None) ** 1.5
    x = x * env * (0.7 + 0.3 * rng.random(len(t)))
    return x / np.abs(x).max() * 0.5


def _through(chain, x):
    pcm = (np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes()
    p = subprocess.run(["ffmpeg", "-v", "error", "-f", "s16le", "-ar", "48000", "-ac", "1", "-i", "-",
                        "-af", chain, "-ac", "1", "-f", "s16le", "-"], input=pcm, capture_output=True)
    assert p.returncode == 0, p.stderr.decode()[-400:]
    return p.stdout


def _opus_rt(pcm: bytes) -> np.ndarray:
    enc = subprocess.run(["ffmpeg", "-v", "error", "-f", "s16le", "-ar", "48000", "-ac", "1", "-i", "-",
                          "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", "-"],
                         input=pcm, capture_output=True, check=True).stdout
    dec = subprocess.run(["ffmpeg", "-v", "error", "-i", "-", "-f", "s16le", "-ar", "48000", "-ac", "1", "-"],
                         input=enc, capture_output=True, check=True).stdout
    return np.frombuffer(dec, np.int16).astype(np.float64) / 32768


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg missing")
@pytest.mark.parametrize("name", ["playback", "live", "bridge"])
def test_chain_peak_below_ceiling_and_opus_does_not_clip(name):
    from helpers import vc_bridge as v
    chain = {"playback": ap.build_ffmpeg_filter(volume=3000),
             "live": ap.build_live_mic_filter(loud=120),
             "bridge": v.build_bridge_filter(v.apply_preset(None, "fight"))}[name]
    out = _through(ap._sanitize_ffmpeg_filter(chain), _speech())
    y = np.frombuffer(out, np.int16).astype(np.float64) / 32768
    peak_db = 20 * np.log10(np.abs(y).max())
    assert peak_db <= -1.0, f"{name}: peak {peak_db:.2f} dBFS above the -1 dBFS ceiling"
    act = y[np.abs(y) > 0.01]
    rms_db = 20 * np.log10(np.sqrt(np.mean(act ** 2)))
    assert rms_db >= -14.0, f"{name}: voice only {rms_db:.1f} dBFS RMS"
    clip = float(np.mean(np.abs(_opus_rt(out)) > 0.99))
    assert clip < 0.05, f"{name}: {clip:.1%} samples clip after Opus (old chains: 12-20 %)"


def test_voice_gate_pushes_opponent_bleed_down_keeps_own_words():
    own = _speech(8.0)
    own[int(2 * SR):int(3 * SR)] = 0          # you pause ...
    own[int(5 * SR):int(6 * SR)] = 0
    bleed = _speech(8.0, seed=9) * 10 ** (-15 / 20)   # ... opponent 15 dB quieter
    def gate(x):
        pcm = (np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes()
        g = ap.CloseVoiceGate(focus_db=12)
        out = b"".join(g.process(pcm[i:i + 1920]) for i in range(0, len(pcm), 1920))
        return np.frombuffer(out, np.int16).astype(np.float64) / 32768
    y = gate(own + bleed)
    gap = slice(int(2.4 * SR), int(3 * SR))   # after the 300 ms hold
    before = 10 * np.log10(np.mean((own + bleed)[gap] ** 2))
    after = 10 * np.log10(np.mean(y[gap] ** 2) + 1e-12)
    assert before - after >= 20.0, f"opponent only {before - after:.1f} dB down"
    words = slice(0, int(2 * SR))
    loss = 10 * np.log10(np.mean(gate(own)[words] ** 2) / np.mean(own[words] ** 2))
    assert loss > -1.0, f"own words lost {loss:.2f} dB"


def test_voice_gate_off_with_focus_zero():
    g = ap.CloseVoiceGate(focus_db=0)
    data = (np.ones(960) * 100).astype(np.int16).tobytes()
    assert g.process(data) == data


def test_stream_command_starts_at_zero_by_default():
    cmd = ap.build_stream_command("/tmp/x.ogg")
    assert " -ss " not in cmd


def _manager():
    from helpers.vc_manager import UserVC
    m = UserVC.__new__(UserVC)
    m._bg_tasks = set()
    m._call_chats = {111: -100}
    m._vol_guards = {}
    return m


def test_volume_guard_restores_200_percent_when_lowered():
    async def run():
        m = _manager()
        calls = []
        async def restore():
            calls.append(1)
        m.guard_volume(-100, 42, 20000, restore=restore)
        assert m._on_volume_seen(-100, 42, 20000) is False      # at 200 %: nothing
        assert m._on_volume_seen(-100, 42, 3000) is True        # lowered to 30 %
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert calls == [1]
        assert m._on_volume_seen(-100, 42, 3000) is False       # < 1 s later: rate-limited
        assert m._on_volume_seen(-100, 7, 100) is False         # not guarded user
    asyncio.run(run())


def test_volume_guard_default_volume_counts_as_lowered():
    async def run():
        m = _manager()
        hit = []
        async def restore():
            hit.append(1)
        m.guard_volume(-100, 42, 20000, restore=restore)
        assert m._on_volume_seen(-100, 42, None) is True        # None = Telegram default 100 %
        await asyncio.sleep(0)
        m.unguard_volume(-100)
        assert m._on_volume_seen(-100, 42, 100) is False
        assert hit == [1]
    asyncio.run(run())
