"""v79: VC bridge voice louder on phone speakers + slim Audio Controls panel."""
import os
import re
import shutil
import subprocess
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import helpers  # noqa: F401,E402
from helpers import vc_bridge as vb  # noqa: E402

HAS_FF = shutil.which("ffmpeg") is not None


def _voice(level_db):
    sr = 48000
    t = np.arange(sr * 6) / sr
    ph = 2 * np.pi * np.cumsum(140 + 30 * np.sin(2 * np.pi * 0.7 * t)) / sr
    x = sum((1 / k) * np.sin(k * ph) for k in range(1, 25))
    env = np.clip(np.sin(2 * np.pi * 3.5 * t), 0, 1) ** 1.5
    x = x * env
    x = x / np.sqrt(np.mean(x[env > 0.1] ** 2)) * 10 ** (level_db / 20)
    return (np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes()


def _phone_lufs_after_opus(raw: bytes) -> float:
    agc = vb.PcmAgc()
    pcm = b"".join(agc.process(raw[i:i + 960]) for i in range(0, len(raw), 960))
    enc = subprocess.run(["ffmpeg", "-v", "error", "-f", "s16le", "-ar", "48000", "-ac", "1",
                          "-i", "-", "-af", vb.build_bridge_filter(vb.LOUD_DEFAULT),
                          "-c:a", "libopus", "-b:a", "48k", "-application", "voip",
                          "-f", "ogg", "-"], input=pcm, capture_output=True, check=True).stdout
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", "-", "-af",
                        "highpass=f=300,lowpass=f=4000,ebur128", "-f", "null", "-"],
                       input=enc, capture_output=True).stderr.decode()
    return float(re.findall(r"I:\s+(-?[\d.]+) LUFS", r)[-1])


@pytest.mark.skipif(not HAS_FF, reason="ffmpeg missing")
def test_bridge_voice_is_at_least_2db_louder_than_v62(monkeypatch):
    raw = _voice(-32)
    new = _phone_lufs_after_opus(raw)
    monkeypatch.setenv("BRIDGE_PHONE_FIGHT", "0")
    old = _phone_lufs_after_opus(raw)
    assert new >= old + 2.0, (new, old)
    assert new >= -7.5, new


def test_audio_panel_only_tez_modes_and_echo():
    from plugins.ui import settings_kb
    datas = [b.callback_data for row in settings_kb({}).inline_keyboard for b in row]
    for want in ("set:tez", "set:lmode:loudest", "set:lmode:phone", "set:lmode:bass",
                 "set:echo:toggle"):
        assert want in datas
    for gone in ("set:relay:-250", "set:gain:50", "set:bass:5", "set:boost:1",
                 "set:playdb:2", "set:masterdb:2", "set:ceiling:cycle", "set:reset"):
        assert gone not in datas
