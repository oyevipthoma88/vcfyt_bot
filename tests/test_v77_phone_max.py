import pytest
import helpers  # noqa: F401
from helpers import audio_processor as ap
from helpers import playback_v77 as v77


def test_default_mode_is_loudest(monkeypatch):
    monkeypatch.delenv("PLAY_LOUD_MODE", raising=False)
    v77._mode = None
    assert v77.get_mode() == "loudest"


@pytest.mark.skip(reason="superseded by v96 clean-max (tests/test_v96_clean_max.py)")
def test_loudest_chain_is_phone_band_only():
    v77.set_mode("loudest")
    try:
        c = ap.build_ffmpeg_filter(volume=1000, boost=10)
        assert c.count("highpass=f=330:p=2") == 4
        assert "mcompand" in c and "virtualbass" not in c
    finally:
        v77.set_mode("phone")


@pytest.mark.skip(reason="superseded by v96 clean-max (tests/test_v96_clean_max.py)")
def test_phone_chain_cuts_sub_bass_and_no_virtualbass():
    v77.set_mode("phone")
    c = ap.build_ffmpeg_filter(volume=1000, bass=100, boost=10)
    assert c.count("highpass=f=280:p=2") == 3
    assert "virtualbass" not in c


def test_bass_mode_restores_old_chain():
    v77.set_mode("bass")
    try:
        assert "highpass=f=280" not in ap.build_ffmpeg_filter(volume=1000, boost=10)
    finally:
        v77.set_mode("phone")


def test_live_mic_untouched():
    v77.set_mode("phone")
    assert "highpass=f=280" not in ap.build_ffmpeg_filter(live=True)


def test_volume_zero_is_silent(monkeypatch):
    monkeypatch.setenv("PLAY_ALLOW_QUIET", "1")
    assert ap.build_ffmpeg_filter(volume=0).endswith("volume=0")
