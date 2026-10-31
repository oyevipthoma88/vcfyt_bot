"""v62: ultra pro max fight chain - multiband dense, loud and clean after Opus."""
import shutil, subprocess
import numpy as np
import pytest
import helpers  # noqa: F401
from helpers import audio_processor as ap


def _chain():
    return ap.build_ffmpeg_filter()


def test_multiband_compressor_present():
    if ap._has_filter("mcompand"):
        assert "mcompand=" in _chain()


def test_ceiling_minus_3_dbfs():
    lims = [f for f in _chain().split(",") if f.startswith("alimiter")]
    assert lims and all("limit=0.7079" in f for f in lims)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg missing")
def test_post_opus_loud_no_clip(tmp_path):
    src, ogg = tmp_path / "s.wav", tmp_path / "s.ogg"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=f=220:d=8,volume=-30dB,apulsator=hz=4:amount=1", str(src)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-af",
                    ap.build_ffmpeg_filter(input_gain_db=12), "-ar", "48000", "-ac", "2",
                    "-c:a", "libopus", "-b:a", "48k", "-application", "voip", str(ogg)], check=True)
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(ogg), "-ac", "1", "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    x = np.frombuffer(pcm, dtype=np.float32)
    assert np.sqrt((x ** 2).mean()) > 0.15
    assert (np.abs(x) > 0.99).mean() < 0.002


def _opus_roundtrip(tmp_path, chain):
    src, ogg = tmp_path / "v.wav", tmp_path / "v.ogg"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=f=900:d=6,volume=-25dB,apulsator=hz=3:amount=1", str(src)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-af", chain,
                    "-ar", "48000", "-ac", "2", "-c:a", "libopus", "-b:a", "48k",
                    "-application", "voip", str(ogg)], check=True)
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(ogg), "-ac", "1", "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(pcm, dtype=np.float32)


def test_live_mic_and_bridge_use_pro_max_master():
    from helpers import vc_bridge as vb
    for c in (ap.build_live_mic_filter(), vb.build_bridge_filter(None)):
        assert "aresample=192000" in c and c.rstrip().endswith("asetnsamples=n=960:p=0")
    assert "highpass=f=200" not in vb.build_bridge_filter(None)   # no thin 200 Hz cut


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg missing")
def test_live_mic_and_bridge_loud_no_clip_after_opus(tmp_path):
    from helpers import vc_bridge as vb
    for c in (ap.build_live_mic_filter(), vb.build_bridge_filter(None)):
        x = _opus_roundtrip(tmp_path, c)
        assert np.sqrt((x ** 2).mean()) > 0.1
        assert (np.abs(x) > 0.99).mean() < 0.005
