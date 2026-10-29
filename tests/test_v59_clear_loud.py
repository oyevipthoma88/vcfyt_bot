"""v59: playback must be loud without lifting the noise floor."""
import helpers  # noqa: F401  (installs playback patches)
from helpers import audio_processor as ap


def _chain():
    return ap.build_ffmpeg_filter().split(",")


def test_no_noise_pumping_levellers():
    chain = _chain()
    assert not any(f.startswith(("speechnorm", "dynaudnorm")) for f in chain)


def test_gate_runs_after_drive():
    chain = _chain()
    if not ap._has_filter("agate") or not ap._has_filter("asoftclip"):
        return
    assert chain.index(next(f for f in chain if f.startswith("agate"))) > \
        chain.index(next(f for f in chain if f.startswith("asoftclip")))


def test_ceiling_is_opus_safe():
    lims = [f for f in _chain() if f.startswith("alimiter")]
    assert lims and all(float(f.split("limit=")[1].split(":")[0]) <= 0.76 for f in lims[-2:])
