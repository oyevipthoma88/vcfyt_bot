"""v61: ultra fight chain - louder than v60 after Opus, still not crackly."""
import re
import shutil
import subprocess
import numpy as np
import pytest
import helpers  # noqa: F401
from helpers import audio_processor as ap


def _chain():
    return ap.build_ffmpeg_filter().split(",")


def test_ceiling_is_minus_1_5_dbfs():
    lims = [f for f in _chain() if f.startswith("alimiter")]
    assert lims and all("limit=0.8414" in f for f in lims)


def test_mud_cut_and_presence_push():
    c = _chain()
    assert "equalizer=f=280:t=q:w=1.0:g=-5" in c
    assert "equalizer=f=2000:t=q:w=1.2:g=7" in c


def test_two_peak_stages_around_clipper():
    peaks = [f for f in _chain() if f.startswith("acompressor") and "detection=peak" in f]
    assert len(peaks) >= 2


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg missing")
def test_post_opus_loud_and_clean(tmp_path):
    src, ogg = tmp_path / "s.wav", tmp_path / "s.ogg"
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
    assert np.sqrt((x ** 2).mean()) > 0.15         # louder than v60's 0.1 bar
    assert (np.abs(x) > 0.99).mean() < 0.005       # < 0.5 % clipped at receiver
