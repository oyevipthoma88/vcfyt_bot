"""VC Bridge (live mic) loudness test — real FFmpeg + Opus round-trip.

Checks:
  * normal AND very soft speech (-40 dB) both come out loud (LUFS > -9),
  * < 5 % samples clipped after the Opus round-trip (v30: no square wave),
  * pauses between words are silent (hiss cut works),
  * every preset / phatna / echo / gate combination builds a valid chain.

Run:  python -m pytest tests/test_vc_bridge_loudness.py   (or python file)
"""
import os
import re
import shutil
import subprocess
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers import vc_bridge as v  # noqa: E402

SR = 48000
HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def _speechlike(seconds: float, rng) -> np.ndarray:
    """Voiced syllables (pitch 120-180 Hz + harmonics) with syllable envelope."""
    t = np.arange(int(seconds * SR)) / SR
    f0 = 140 + 30 * np.sin(2 * np.pi * 0.7 * t)
    ph = 2 * np.pi * np.cumsum(f0) / SR
    x = sum((1.0 / k) * np.sin(k * ph) for k in range(1, 25))
    env = np.clip(np.sin(2 * np.pi * 3.5 * t), 0, None) ** 1.5
    x = x * env * (0.7 + 0.3 * rng.random(len(t)))
    return x / np.sqrt(np.mean(x ** 2)) * 10 ** (-20 / 20)


def _build_input():
    rng = np.random.default_rng(7)
    parts, mask = [], []
    for k in range(6):
        g = 1.0 if k < 3 else 10 ** (-40 / 20)          # last half: 40 dB softer
        s = _speechlike(2.5, rng) * g
        parts += [s, np.zeros(int(1.5 * SR))]
        mask += [np.zeros(len(s)), np.ones(int(1.5 * SR))]
    x = np.concatenate(parts)
    x = x + rng.standard_normal(len(x)) * 10 ** (-80 / 20)   # mic hiss
    pcm = (np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes()
    # Telegram source VC = Opus
    enc = subprocess.run(["ffmpeg", "-v", "error", "-f", "s16le", "-ar", "48000", "-ac", "1",
                          "-i", "-", "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", "-"],
                         input=pcm, capture_output=True, check=True).stdout
    dec = subprocess.run(["ffmpeg", "-v", "error", "-i", "-", "-f", "s16le", "-ar", "48000",
                          "-ac", "1", "-"], input=enc, capture_output=True, check=True).stdout
    return dec, np.concatenate(mask)


def _run_chain(raw: bytes, cfg=None) -> np.ndarray:
    agc = v.PcmAgc(gate=bool(v.clean_loud(cfg)["gate"]))
    out = bytearray()
    for i in range(0, len(raw), 960):
        out += agc.process(raw[i:i + 960])
    p = subprocess.run(["ffmpeg", "-v", "error", "-f", "s16le", "-ar", "48000", "-ac", "1",
                        "-i", "-", "-af", v.build_bridge_filter(cfg),
                        "-c:a", "libopus", "-b:a", "48k", "-f", "ogg", "-"],
                       input=bytes(out), capture_output=True)
    assert p.returncode == 0, p.stderr.decode()[-500:]
    d = subprocess.run(["ffmpeg", "-v", "error", "-i", "-", "-f", "s16le", "-ar", "48000",
                        "-ac", "1", "-"], input=p.stdout, capture_output=True, check=True).stdout
    return np.frombuffer(d, np.int16).astype(np.float64) / 32768


def _lufs(x: np.ndarray) -> float:
    pcm = (x * 32767).astype(np.int16).tobytes()
    r = subprocess.run(["ffmpeg", "-hide_banner", "-f", "s16le", "-ar", "48000", "-ac", "1",
                        "-i", "-", "-af", "ebur128", "-f", "null", "-"],
                       input=pcm, capture_output=True).stderr.decode()
    return float(re.findall(r"I:\s+(-?[\d.]+) LUFS", r)[-1])


def test_every_combo_builds():
    for name in list(v.LOUD_PRESETS) + ["reset"]:
        for extra in ({}, {"crunch": 10}, {"echo": 5}, {"gate": 0}, {"clip": "soft", "drive": 0}):
            cfg = dict(v.LOUD_PRESETS.get(name, v.LOUD_DEFAULT), **extra)
            f = v.build_bridge_filter(cfg)
            assert "asoftclip" in f or "alimiter" in f
    assert v.clean_loud({"crunch": 99, "echo": -3, "gate": 7}) == dict(
        v.LOUD_DEFAULT, crunch=10, echo=0, gate=1)
    # preset keeps the user's echo / hiss-cut choice
    assert v.apply_preset({"echo": 3, "gate": 0}, "fight")["echo"] == 3


def test_mix_numpy_clamps():
    class F:
        def __init__(self, a):
            self.frame = np.array(a, dtype=np.int16).tobytes()
    out = np.frombuffer(v._mix([F([30000, -30000, 5]), F([30000, -30000, 5])]), np.int16)
    assert list(out) == [32767, -32768, 10]


def test_soft_voice_reaches_full_scale_and_pauses_silent():
    if not HAVE_FFMPEG:
        return
    raw, mask = _build_input()
    x = _run_chain(raw)
    n = min(len(x), len(mask))
    x, mask = x[:n], mask[:n]
    half = n // 2
    for sl, label in ((slice(0, half), "normal"), (slice(half, n), "soft -40 dB")):
        xs, ms = x[sl], mask[sl]
        lufs = _lufs(xs[ms == 0])
        print(label, "LUFS", lufs)
        assert lufs > -9.0, f"{label}: {lufs} LUFS — too quiet"
    # v30: no square wave -> after Opus decode almost nothing clips ("fat")
    clip = float(np.mean(np.abs(x) > 0.99))
    print("post-opus clipped samples", clip)
    assert clip < 0.05, f"{clip:.1%} samples clipped after Opus"
    # pause frames after a 400 ms tail: mostly digital silence
    pm = mask.copy()
    for i in np.where(np.diff(np.r_[0, mask]) == 1)[0]:
        pm[i:i + int(0.4 * SR)] = 0
    q = x[pm == 1]
    q = q[: len(q) // 480 * 480].reshape(-1, 480)
    silent = np.mean(np.abs(q).max(1) < 1e-3)
    print("silent pause frames", silent)
    assert silent > 0.6, f"pause hiss: only {silent:.0%} silent"


def test_non_admin_boost_and_extra_layer_build():
    for name in v.LOUD_PRESETS:
        f = v.build_bridge_filter(v.LOUD_PRESETS[name], focus=True)
        assert "highpass=f=300" in f
        assert v.EXTRA_LAYER.split(",")[0] in v.build_bridge_filter(
            v.LOUD_PRESETS[name], focus=True, layer=True)
    assert v.EXTRA_LAYER not in v.build_bridge_filter(v.LOUD_DEFAULT)


if __name__ == "__main__":
    test_non_admin_boost_and_extra_layer_build()
    test_every_combo_builds()
    test_mix_numpy_clamps()
    test_soft_voice_reaches_full_scale_and_pauses_silent()
    print("OK")
