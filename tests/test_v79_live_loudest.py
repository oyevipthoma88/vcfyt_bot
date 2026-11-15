import subprocess

import pytest

from helpers import audio_processor as ap
from helpers import live_mic_v79 as v79


@pytest.fixture(autouse=True)
def _reset_mode():
    old = v79._mode
    yield
    v79._mode = old


def test_live_mic_default_is_loudest_clear_chain():
    # v87: loudest = no clipper, limiter 0.7 dB under the -0.3 dBFS ceiling.
    v79._mode = None
    chain = ap.build_live_mic_filter(-0.3, loud=120)
    assert v79.get_mode() == "loudest"
    assert "asoftclip" not in chain
    assert "limit=0.8913" in chain


def test_clean_mode_has_no_softclip():
    v79.set_mode("clean")  # old name -> phone
    assert v79.get_mode() == "phone"
    chain = ap.build_live_mic_filter(-0.3, loud=120)
    assert "asoftclip" not in chain and "alimiter" in chain


def test_bad_mode_rejected():
    with pytest.raises(ValueError):
        v79.set_mode("xyz")


def test_loudest_chain_runs_in_ffmpeg():
    chain = v79.live_loudest_chain()
    r = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=1",
                        "-af", chain, "-f", "null", "-"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
