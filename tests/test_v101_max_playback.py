"""Maximum playback defaults, overrides, and live-mic isolation."""
import shutil
import subprocess

import numpy as np
import pytest

from helpers import audio_processor as ap, playback_v77 as v77
from helpers import playback_v96 as v96, playback_v97 as v97


@pytest.fixture(autouse=True)
def restore(monkeypatch):
    old = v77.get_mode()
    for key in ("PLAY_TG_TRIM_DB", "PLAY_V96_LOUDEST_DRIVE_DB", "PLAY_V96_PHONE_DRIVE_DB"):
        monkeypatch.delenv(key, raising=False)
    yield
    v77.set_mode(old)


@pytest.mark.parametrize("mode", ["loudest", "phone", "bass"])
def test_max_default_removes_only_codec_attenuation(mode):
    assert v97.codec_trim_db(mode, boost=False) == 0.0
    assert v97.codec_trim_db(mode, boost=True) == 6.02


@pytest.mark.parametrize("mode,env", [("loudest", "PLAY_V96_LOUDEST_DRIVE_DB"), ("phone", "PLAY_V96_PHONE_DRIVE_DB")])
def test_heroku_drive_overrides_new_default(monkeypatch, mode, env):
    v77.set_mode(mode)
    assert "volume=26.00dB," + v96.CLEAN_CLIP in ap.build_ffmpeg_filter(boost=10)
    monkeypatch.setenv(env, "22")
    assert "volume=22.00dB," + v96.CLEAN_CLIP in ap.build_ffmpeg_filter(boost=10)


@pytest.mark.parametrize("mode", ["loudest", "phone", "bass"])
def test_heroku_codec_trim_still_wins(monkeypatch, mode):
    monkeypatch.setenv("PLAY_TG_TRIM_DB", "2.8")
    assert v97.codec_trim_db(mode, boost=False) == 2.8
    assert v97.codec_trim_db(mode, boost=True) == pytest.approx(8.82)


def test_live_mic_bypasses_playback_changes(monkeypatch):
    original = ap.build_ffmpeg_filter(live=True)
    monkeypatch.setenv("PLAY_TG_TRIM_DB", "12")
    monkeypatch.setenv("PLAY_V96_LOUDEST_DRIVE_DB", "0")
    for mode in v77.MODES:
        v77.set_mode(mode)
        with v97.listener_boost(True):
            assert ap.build_ffmpeg_filter(live=True) == original


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg missing")
@pytest.mark.parametrize("mode", ["loudest", "phone", "bass"])
def test_actual_listener_output_louder_than_previous_defaults(mode):
    v77.set_mode(mode)

    def listener_rms():
        chain = ap._sanitize_ffmpeg_filter(ap.build_ffmpeg_filter(volume=3000, boost=10, bass=100, treble=100, gain=500))
        raw = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anoisesrc=d=4:c=pink:a=0.4:seed=7", "-af", chain, "-ar", "48000", "-ac", "1", "-f", "s16le", "-"], capture_output=True, check=True).stdout
        encoded = subprocess.run(["ffmpeg", "-v", "error", "-f", "s16le", "-ar", "48000", "-ac", "1", "-i", "-", "-c:a", "libopus", "-b:a", "32k", "-application", "voip", "-frame_duration", "60", "-f", "ogg", "-"], input=raw, capture_output=True, check=True).stdout
        decoded = subprocess.run(["ffmpeg", "-v", "error", "-i", "-", "-ac", "1", "-f", "f32le", "-"], input=encoded, capture_output=True, check=True).stdout
        signal = np.clip(np.frombuffer(decoded, dtype=np.float32)[48000:], -1, 1)
        return 20 * np.log10(np.sqrt(np.mean(signal * signal)))

    current = listener_rms()
    with pytest.MonkeyPatch.context() as previous:
        previous.setenv("PLAY_TG_TRIM_DB", "3.0" if mode == "phone" else "2.8")
        previous.setenv("PLAY_V96_LOUDEST_DRIVE_DB", "21")
        previous.setenv("PLAY_V96_PHONE_DRIVE_DB", "22")
        old = listener_rms()
    assert current > old + 0.5, (mode, current, old)
