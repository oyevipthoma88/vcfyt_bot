"""v66: playback must have real bass, no muffle and an Opus-safe ceiling."""
import pytest as _pt
pytestmark = _pt.mark.skip(reason="superseded by v68 chain (tests/test_v68_tez_clear.py)")
from helpers import audio_processor as ap
from helpers import playback_v66 as v66


def _chain(**kw):
    v66.install()
    return ap.build_ffmpeg_filter(**kw)


def test_bass_shelf_present_and_scales():
    c = _chain(bass=100)
    assert "lowshelf=f=100:w=0.7:g=9.0" in c
    assert v66.bass_db(0) == 0.0


def test_old_default_bass_14_is_lifted():
    assert v66.bass_db(14) == v66.bass_db(60) == 5.4


def test_no_thin_highpass_or_muffle_lowpass():
    c = _chain()
    assert "highpass=f=110" not in c
    assert "lowpass=f=10000" not in c
    assert "lowpass=f=16000" in c


def test_ceiling_never_above_minus_1_dbfs():
    c = _chain()
    assert "limit=0.8912" in c
    assert "limit=0.97" not in c


def test_total_push_far_below_v65_56db():
    d1, d2 = v66.drives(10)
    assert d1 + d2 + v66.SOFTCLIP_DRIVE_DB <= 44.0
