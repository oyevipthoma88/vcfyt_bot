import helpers  # noqa: F401  (installs all playback versions)
from helpers import audio_processor as ap
from helpers import playback_v73 as v73


def _limiters(chain):
    return [x for x in chain.split(",") if x.startswith("alimiter")]


def test_played_audio_ceiling_is_0dbfs_even_with_stale_env(monkeypatch):
    monkeypatch.setenv("PLAY_CEILING_DB", "-1.5")   # old .env.example / saved value
    chain = ap.build_ffmpeg_filter(volume=3000, boost=10)
    assert "limit=1.0000" in _limiters(chain)[-1]


def test_live_mic_ceiling_is_0dbfs(monkeypatch):
    monkeypatch.setenv("PLAY_CEILING_DB", "-1.5")
    assert "limit=1.0000" in _limiters(ap.build_live_mic_filter())[-1]


def test_live_mic_drive_never_below_18db(monkeypatch):
    monkeypatch.setenv("MASTER_DRIVE_DB", "12")
    assert "volume=18.00dB" in ap.build_live_mic_filter()


def test_push_at_max_boost_is_24db(monkeypatch):
    monkeypatch.setenv("PLAY_PUSH_DB", "18")
    assert v73.push_db(ap.LEVEL_MAX) == 24.0


import pytest as _pytest  # noqa: E402


@_pytest.fixture(autouse=True)
def _old_bass_chain():
    """These tests cover the v73-v75 chain, which v77 keeps as `.loudmode bass`."""
    from helpers import playback_v77 as _v77
    _v77.set_mode("bass")
    yield
    _v77.set_mode("phone")
