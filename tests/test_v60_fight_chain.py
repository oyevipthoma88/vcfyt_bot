"""v60: fight chain present and receiver (post-Opus) output not clipped."""
import shutil
import subprocess
import numpy as np
import pytest
import helpers  # noqa: F401
from helpers import audio_processor as ap


def _chain():
    return ap.build_ffmpeg_filter().split(",")


def test_fight_chain_present():
    c = _chain()
    assert any(f.startswith("equalizer=f=2200") and "g=9" in f for f in c)   # presence
    assert any("detection=peak" in f for f in c if f.startswith("acompressor"))  # fight
    assert "highpass=f=150:p=2" in c                                          # mud/rumble


def test_expander_does_not_chop_words():
    g = [f for f in _chain() if f.startswith("agate")]
    if g:
        assert float(g[0].split("range=")[1].split(":")[0]) >= 0.2   # max -14 dB


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg missing")
def test_post_opus_clipping_low(tmp_path):
    src, ogg = tmp_path / "s.wav", tmp_path / "s.ogg"
    # quiet syllable-like bursts (4 Hz) of a voiced tone + noise floor
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=f=220:d=8,volume=-30dB,apulsator=hz=4:amount=1",
                    str(src)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-af",
                    ap.build_ffmpeg_filter(input_gain_db=12), "-ar", "48000", "-ac", "2",
                    "-c:a", "libopus", "-b:a", "48k", "-application", "voip", str(ogg)],
                   check=True)
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(ogg), "-ac", "1",
                          "-f", "f32le", "-"], capture_output=True, check=True).stdout
    x = np.frombuffer(pcm, dtype=np.float32)
    assert np.sqrt((x ** 2).mean()) > 0.1          # loud
    assert (np.abs(x) > 0.99).mean() < 0.005       # < 0.5 % clipped at receiver
