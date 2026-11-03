import helpers  # noqa: F401  (installs all playback versions)
from helpers import audio_processor as ap


def test_playback_ceiling_is_full_scale_0dbfs(monkeypatch):
    monkeypatch.delenv("PLAY_CEILING_DB", raising=False)
    chain = ap.build_ffmpeg_filter(volume=3000, boost=10)
    limiters = [x for x in chain.split(",") if x.startswith("alimiter")]
    assert limiters[-1].count("limit=1.0000") == 1


def test_push_at_max_boost_is_24db(monkeypatch):
    monkeypatch.delenv("PLAY_PUSH_DB", raising=False)
    from helpers import playback_v73 as v73
    assert v73.push_db(ap.LEVEL_MAX) == 24.0
