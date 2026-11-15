"""v90: live mic = same 3 modes as playback at 0 dBFS, Web Studio, no VC bridge."""
import asyncio
import os
import re
import shutil
import subprocess
import tempfile

import numpy as np
import pytest

from helpers import audio_processor as ap
from helpers import live_mic, live_mic_v79 as v79, web_studio

HAVE_FFMPEG = bool(shutil.which("ffmpeg"))
VOICE = ("aevalsrc='(0.5*sin(2*PI*150*t)+0.3*sin(2*PI*300*t)+0.15*sin(2*PI*900*t)"
         "+0.1*sin(2*PI*2500*t))*(0.2+0.8*abs(sin(2*PI*2.5*t)))':s=48000:d=5")


def _lufs(chain, in_db=-30):
    with tempfile.TemporaryDirectory() as d:
        wav = os.path.join(d, "o.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", VOICE, "-af",
                        f"volume={in_db}dB," + chain, "-ar", "48000", "-ac", "1", wav], check=True)
        r = subprocess.run(["ffmpeg", "-hide_banner", "-i", wav, "-af", "ebur128",
                            "-f", "null", "-"], capture_output=True, text=True).stderr
        return float(re.findall(r"I:\s+(-?[\d.]+) LUFS", r)[-1])


# ------------------------------------------------------------------ modes
def test_live_mic_has_same_three_modes_as_playback():
    from helpers import playback_v77
    assert tuple(v79.MODES) == tuple(playback_v77.MODES) == ("loudest", "phone", "bass")


def test_old_mode_names_map_to_new_modes():
    assert v79.normalize_mode("clean") == "phone"
    assert v79.normalize_mode("tez") == "loudest"
    assert v79.normalize_mode("xyz") == ""
    with pytest.raises(ValueError):
        v79.set_mode("xyz")


def test_every_mode_ends_at_0_dbfs_limiter():
    for m in v79.MODES:
        chain = ap.build_live_mic_filter(mode=m)
        assert "limit=0.9990" in chain and "alimiter" in chain, m
        assert "loudnorm" not in chain and "dynaudnorm" not in chain, m   # no live delay


def test_default_mode_is_loudest():
    v79._mode = None
    assert v79.get_mode() == "loudest"


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg missing")
@pytest.mark.parametrize("mode", ["loudest", "phone", "bass"])
def test_quiet_phone_mic_comes_out_near_full_scale(mode):
    # -30 dB phone-level voice in -> within ~4 dB of full scale out
    assert _lufs(ap.build_live_mic_filter(mode=mode)) > -4.0


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg missing")
def test_live_loudest_is_as_loud_as_playback_loudest():
    from helpers import playback_v77
    playback_v77.set_mode("loudest")
    live = _lufs(ap.build_live_mic_filter(mode="loudest"))
    play = _lufs(ap._sanitize_ffmpeg_filter(ap.build_ffmpeg_filter(volume=3000)))
    assert live >= play - 2.0, f"live {live} vs playback {play}"


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg missing")
def test_silence_is_not_pumped_into_hiss():
    with tempfile.TemporaryDirectory() as d:
        wav = os.path.join(d, "n.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "anoisesrc=d=3:c=pink:a=0.003", "-af", ap.build_live_mic_filter(mode="loudest"),
                        "-ar", "48000", "-ac", "1", "-f", "f32le", wav], check=True)
        x = np.fromfile(wav, np.float32)[48000:]
        assert 20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-9) < -25.0


# ------------------------------------------------------------------ session
def test_session_mode_is_per_user_and_used_in_filter():
    s = live_mic.LiveMicSession(1, None, -100, {})
    s.mic_mode = "bass"
    assert s.public_settings()["mode"] == "bass"
    assert "virtualbass" in s._build_filter()
    s.mic_mode = "phone"
    assert "virtualbass" not in s._build_filter()


# ------------------------------------------------------------------ studio
def test_screen_cfg_is_clamped():
    assert web_studio.clamp_cfg({"q": "720", "fps": 99, "kind": "screen"}) == (1280, 720, 15, "screen")
    w, h, fps, kind = web_studio.clamp_cfg({"q": "bad", "fps": -3, "kind": "x"})
    assert (w, h) in web_studio.SIZES.values() and 1 <= fps <= 15 and kind in ("screen", "camera")


def test_jpeg_frame_becomes_exact_i420_size():
    from io import BytesIO
    from PIL import Image
    buf = BytesIO()
    Image.new("RGB", (300, 500), (200, 30, 30)).save(buf, "JPEG")
    out = web_studio.jpeg_to_i420(buf.getvalue(), 640, 360)
    assert len(out) == 640 * 360 * 3 // 2


def test_page_has_studio_tabs_screen_socket_and_modes():
    h = live_mic.LIVE_MIC_HTML
    for tab in ("mic", "screen", "chat", "vc"):
        assert f'data-tab="{tab}"' in h
    for m in v79.MODES:
        assert f'data-mode="{m}"' in h
    assert "/ws/screen" in h and "getDisplayMedia" in h
    assert "noiseSuppression: false" in h and "autoGainControl: false" in h


def test_screen_route_registered():
    src = open("helpers/live_mic.py").read()
    assert '"/ws/screen"' in src and "display-capture=(self)" in src


# ------------------------------------------------------------------ bridge removed
def test_vc_bridge_is_gone():
    for f in ("helpers/vc_bridge.py", "plugins/vc_bridge.py",
              "helpers/playback_v79.py", "helpers/playback_v80.py"):
        assert not os.path.exists(f), f
    for root in ("helpers", "plugins"):
        for name in os.listdir(root):
            if name.endswith(".py"):
                src = open(os.path.join(root, name)).read()
                assert "import vc_bridge" not in src and "plugins.vc_bridge" not in src, name


def test_mic_panel_has_three_modes_and_no_bridge():
    import plugins.web_mic as wm

    class _DB:
        async def get_app_value(self, k):
            return None
    old = wm.db
    wm.db = _DB()
    try:
        _, kb = asyncio.run(wm.web_mic_panel(5))
    finally:
        wm.db = old
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert {"mic:wmode:loudest", "mic:wmode:phone", "mic:wmode:bass"} <= set(cbs)
    assert not any("bridge" in c for c in cbs)


def test_home_and_more_have_no_duplicate_buttons():
    from plugins.ui import home_kb, more_kb, back_kb
    home = [b.callback_data for r in home_kb(False, True).inline_keyboard for b in r]
    more = [b.callback_data for r in more_kb().inline_keyboard for b in r]
    assert len(home) == len(set(home))
    assert not (set(more) - {"menu:home"}) & set(home)
    assert len(back_kb().inline_keyboard[0]) == 1
