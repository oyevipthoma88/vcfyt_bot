"""v58: voice presence (1-4 kHz) louder than v57 after Opus, no extra crackle."""
import os, subprocess, tempfile
import numpy as np
import helpers  # noqa: F401
import helpers.audio_processor as ap
from helpers import playback_v58

SR = 48000

def _src():
    t = np.arange(6 * SR) / SR
    env = (np.sin(2 * np.pi * 3 * t) > 0).astype(np.float32)
    x = env * (np.sin(2 * np.pi * 150 * t) + 0.6 * np.sin(2 * np.pi * 400 * t) + 0.2 * np.sin(2 * np.pi * 2500 * t))
    fd, p = tempfile.mkstemp(suffix=".wav"); os.close(fd)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", "-", p],
                   input=(0.05 * x).astype(np.float32).tobytes(), check=True)
    return p

def _presence_and_clip(af, src):
    out = src + ".ogg"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-af", af, "-ac", "1",
                    "-c:a", "libopus", "-b:a", "48k", "-application", "voip", out], check=True)
    dec = lambda extra: np.frombuffer(subprocess.run(["ffmpeg", "-loglevel", "error", "-i", out] + extra +
                                      ["-ac", "1", "-f", "f32le", "-"], capture_output=True).stdout, np.float32)
    x = dec([]); y = dec(["-af", "highpass=f=1000:p=2,lowpass=f=4000:p=2"])
    return 20 * np.log10(np.sqrt(np.mean(y ** 2))), np.mean(np.abs(x) > 1.0)

def test_presence_louder_than_v57_without_more_crackle():
    src = _src(); g = ap.measure_input_gain(src)
    new = _presence_and_clip(ap.build_ffmpeg_filter(volume=3000, boost=10, input_gain_db=g), src)
    ap.playback_fight_stage = playback_v58._v56_fight
    try:
        old = _presence_and_clip(ap.build_ffmpeg_filter(volume=3000, boost=10, input_gain_db=g), src)
    finally:
        ap.playback_fight_stage = playback_v58.playback_fight_stage
    assert new[0] > old[0] + 2.0
    assert new[1] < 0.002

def test_presence_eq_runs_before_compressor():
    parts = ap.build_ffmpeg_filter(volume=3000, boost=10).split(",")
    assert parts.index("equalizer=f=2200:t=q:w=1.4:g=9") < next(i for i, p in enumerate(parts) if p.startswith("acompressor"))
