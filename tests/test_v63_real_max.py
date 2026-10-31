"""v63: playback must really be max loud (ceiling -0.3 dBFS), and the
old -3.0 dBFS default must never come back from DB/env."""
import os, shutil, subprocess
import numpy as np
import pytest
import helpers  # noqa: F401
from helpers import audio_processor as ap


def test_default_ceiling_is_minus_1_dbfs_since_v65(monkeypatch):
    # v65: played files cap at -1.0 dBFS (above that Opus overs = square)
    monkeypatch.delenv("PLAY_CEILING_DB", raising=False)
    lims = [f for f in ap.build_ffmpeg_filter().split(",") if f.startswith("alimiter")]
    assert lims and all(float(f.split("limit=")[1].split(":")[0]) <= 0.8913 for f in lims)


def test_no_level_eating_stages_after_drive():
    # v65 gates BEFORE the first push stage; nothing may eat level after it
    parts = ap.build_ffmpeg_filter().split(",")
    first_push = next(i for i, p in enumerate(parts) if p == "volume=20.00dB")
    tail = parts[first_push:]
    assert not any(p.startswith(("deesser", "agate")) for p in tail)


def test_old_minus_3_db_setting_is_ignored_on_startup(monkeypatch):
    from helpers.playback_v63 import load_saved_db_settings
    monkeypatch.delenv("PLAY_CEILING_DB", raising=False)
    load_saved_db_settings({"play_ceiling_db": -3.0, "play_drive_db": 14.0})
    assert "PLAY_CEILING_DB" not in os.environ
    assert os.environ["PLAY_DRIVE_DB"] == "14.0"
    monkeypatch.delenv("PLAY_DRIVE_DB", raising=False)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg missing")
def test_quiet_voice_is_loud_after_opus(tmp_path, monkeypatch):
    monkeypatch.delenv("PLAY_CEILING_DB", raising=False)
    monkeypatch.delenv("PLAY_DRIVE_DB", raising=False)
    src, ogg = tmp_path / "s.wav", tmp_path / "s.ogg"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=f=220:d=8,volume=-30dB,apulsator=hz=4:amount=1", str(src)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-af",
                    ap.build_ffmpeg_filter(input_gain_db=12), "-ar", "48000", "-ac", "2",
                    "-c:a", "libopus", "-b:a", "48k", "-application", "voip", str(ogg)], check=True)
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(ogg), "-ac", "1", "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    x = np.frombuffer(pcm, dtype=np.float32)
    assert 20 * np.log10(np.sqrt((x ** 2).mean())) > -9.0   # v62 was ~ -12
