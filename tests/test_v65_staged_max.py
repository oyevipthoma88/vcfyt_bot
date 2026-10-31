"""v65: played audio gets ~56 dB staged push, ceiling never above -1 dBFS."""
import helpers  # noqa: F401
from helpers import audio_processor as ap
from helpers.playback_v65 import total_push_db, ceiling_db


def test_max_boost_total_push_is_56_db(monkeypatch):
    monkeypatch.delenv("PLAY_DRIVE_DB", raising=False)
    assert total_push_db(10) == 56.0


def test_saved_low_playdb_cannot_lower_push(monkeypatch):
    monkeypatch.setenv("PLAY_DRIVE_DB", "5")
    assert total_push_db(10) == 56.0


def test_saved_high_playdb_adds_at_most_4_db(monkeypatch):
    monkeypatch.setenv("PLAY_DRIVE_DB", "30")
    assert total_push_db(10) == 60.0


def test_ceiling_never_above_minus_1(monkeypatch):
    monkeypatch.setenv("PLAY_CEILING_DB", "-0.3")
    assert ceiling_db() == -1.0
    monkeypatch.setenv("PLAY_CEILING_DB", "-2.0")
    assert ceiling_db() == -2.0


def test_played_chain_uses_v65(monkeypatch):
    monkeypatch.delenv("PLAY_DRIVE_DB", raising=False)
    monkeypatch.delenv("PLAY_CEILING_DB", raising=False)
    from helpers.playback_v65 import playback_fight_stage
    chain = playback_fight_stage(boost=10)
    assert [x for x in chain if x.startswith("volume=")] == [
        "volume=20.00dB", "volume=16.00dB", "volume=20.00dB"]
