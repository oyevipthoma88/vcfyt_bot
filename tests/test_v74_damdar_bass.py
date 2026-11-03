import helpers  # noqa: F401  (installs all playback versions)
from helpers import audio_processor as ap
from helpers import playback_v74 as v74


def test_bass_setting_changes_the_chain():
    assert ap.build_ffmpeg_filter(volume=1000, bass=0) != ap.build_ffmpeg_filter(volume=1000, bass=100)


def test_full_bass_gives_6db_shelf():
    assert "lowshelf=f=170:w=0.7:g=6.0" in ap.build_ffmpeg_filter(volume=1000, bass=100)


def test_bass_zero_has_no_shelf():
    assert "lowshelf" not in ap.build_ffmpeg_filter(volume=1000, bass=0)


def test_no_150hz_body_cut_anymore():
    assert "highpass=f=150" not in ap.build_ffmpeg_filter(volume=1000, bass=100)


def test_full_treble_is_2db_air():
    assert "highshelf=f=8000:w=0.8:g=2.0" in ap.build_ffmpeg_filter(volume=1000, treble=100)


def test_full_gain_pushes_1_5db():
    assert v74.gain_push_db(500) == 1.5


def test_ceiling_still_full_scale():
    lims = [x for x in ap.build_ffmpeg_filter(volume=3000, boost=10).split(",") if x.startswith("alimiter")]
    assert "limit=1.0000" in lims[-1]
