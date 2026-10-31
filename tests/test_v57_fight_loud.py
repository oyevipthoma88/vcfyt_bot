"""v57: quiet/normal/loud voice all come out at the same fight level."""
import os, re, subprocess, tempfile
import numpy as np, pytest
import helpers  # noqa: F401  installs v56 + v57
import helpers.audio_processor as ap
from helpers.playback_v57 import speech_level

SR = 48000

def _voice(seconds=6, level_db=-20.0, click=False):
    t = np.arange(int(seconds * SR)) / SR
    env = (np.sin(2 * np.pi * 3 * t) > 0).astype(np.float32)          # syllables
    x = env * (np.sin(2 * np.pi * 180 * t) + 0.5 * np.sin(2 * np.pi * 900 * t) + 0.3 * np.sin(2 * np.pi * 2500 * t))
    x = x / np.sqrt(np.mean(x[env > 0] ** 2)) * 10 ** (level_db / 20)
    if click:
        x[SR:SR + 100] = 0.95
    return x.astype(np.float32)

def _wav(x):
    fd, p = tempfile.mkstemp(suffix=".wav"); os.close(fd)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", "-", p],
                   input=x.tobytes(), check=True)
    return p

def _render_lufs(p):
    out = p + ".out.wav"
    af = ap.build_ffmpeg_filter(volume=3000, boost=10, gain=500, input_gain_db=ap.measure_input_gain(p))
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", p, "-af", af, out], check=True)
    err = subprocess.run(["ffmpeg", "-nostdin", "-i", out, "-af", "ebur128", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    return float(re.findall(r"I:\s+(-?[\d.]+) LUFS", err)[-1])

def test_click_does_not_block_quiet_voice_lift():
    p = _wav(_voice(level_db=-50, click=True))
    assert ap.measure_input_gain(p) >= 25.0

def test_speech_level_ignores_single_loud_block():
    log = "\n".join(["lavfi.r128.M=-60.0"] * 20 + ["lavfi.r128.M=-5.0"])
    assert speech_level(log) == -60.0

@pytest.mark.parametrize("level", [-50, -30, -20, -8])
def test_every_input_level_reaches_fight_loudness(level):
    assert _render_lufs(_wav(_voice(level_db=level))) > -9.0

def test_quiet_and_loud_end_within_3_lu():
    assert abs(_render_lufs(_wav(_voice(level_db=-45))) - _render_lufs(_wav(_voice(level_db=-8)))) < 3.0

def test_ceiling_default_is_minus_1_dbfs():
    # v65 replaced the v57 -3 dBFS ceiling with -1 dBFS (Opus-safe max)
    lim = [f for f in ap.playback_ceiling() if f.startswith("alimiter")]
    assert lim and all(float(f.split("limit=")[1].split(":")[0]) <= 0.8913 for f in lim)
