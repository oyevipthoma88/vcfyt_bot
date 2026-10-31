"""v64: played audio drive never drops below the fight floor."""
import helpers  # noqa: F401
from helpers import audio_processor as ap
from helpers.playback_v64 import fight_drive_db


def test_max_boost_drive_is_20_db(monkeypatch):
    monkeypatch.delenv("PLAY_DRIVE_DB", raising=False)
    assert fight_drive_db(10) == 20.0


def test_saved_low_playdb_cannot_lower_drive(monkeypatch):
    monkeypatch.setenv("PLAY_DRIVE_DB", "10")
    assert fight_drive_db(10) == 20.0


def test_saved_high_playdb_pushes_higher_up_to_30(monkeypatch):
    monkeypatch.setenv("PLAY_DRIVE_DB", "26")
    assert fight_drive_db(10) == 26.0
    monkeypatch.setenv("PLAY_DRIVE_DB", "40")
    assert fight_drive_db(10) == 30.0


def test_chain_uses_new_drive(monkeypatch):
    monkeypatch.setenv("PLAY_DRIVE_DB", "10")
    from helpers.playback_v64 import playback_fight_stage
    assert "volume=20.00dB" in playback_fight_stage(boost=10)
