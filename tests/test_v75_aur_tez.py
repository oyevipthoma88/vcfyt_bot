import helpers  # noqa: F401
from helpers import audio_processor as ap
from helpers import playback_v75 as v75


def test_default_extra_drive_is_6db(monkeypatch):
    monkeypatch.delenv("PLAY_EXTRA_DRIVE_DB", raising=False)
    assert v75.extra_drive_db() == 6.0


def test_extra_drive_capped_at_12db(monkeypatch):
    monkeypatch.setenv("PLAY_EXTRA_DRIVE_DB", "40")
    assert v75.extra_drive_db() == 12.0


def test_extra_drive_goes_before_last_clipper(monkeypatch):
    monkeypatch.delenv("PLAY_EXTRA_DRIVE_DB", raising=False)
    parts = ap.build_ffmpeg_filter(volume=1000, boost=10).split(",")
    last = max(i for i, x in enumerate(parts) if x.startswith("asoftclip=type=tanh:oversample=4"))
    assert parts[last - 1] == "volume=6.00dB"


def test_live_mic_untouched():
    assert "virtualbass" not in ap.build_ffmpeg_filter(live=True)


import pytest as _pytest  # noqa: E402


@_pytest.fixture(autouse=True)
def _old_bass_chain():
    """These tests cover the v73-v75 chain, which v77 keeps as `.loudmode bass`."""
    from helpers import playback_v77 as _v77
    _v77.set_mode("bass")
    yield
    _v77.set_mode("phone")
