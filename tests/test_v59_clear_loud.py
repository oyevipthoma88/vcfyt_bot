"""v59/v60: playback must be loud without lifting the noise floor."""
import helpers  # noqa: F401  (installs playback patches)
from helpers import audio_processor as ap


def _chain():
    return ap.build_ffmpeg_filter().split(",")


def test_no_noise_pumping_levellers():
    chain = _chain()
    assert not any(f.startswith("speechnorm") for f in chain)
    # a leveler is allowed only with a noise threshold (pauses never lifted)
    assert all(":t=" in f for f in chain if f.startswith("dynaudnorm"))


def test_gate_runs_before_push_stages():
    # v65: gate before the 3 push stages (pauses stay quiet, words never chopped
    # after the clipper)
    chain = _chain()
    if not ap._has_filter("agate") or not ap._has_filter("asoftclip"):
        return
    assert chain.index(next(f for f in chain if f.startswith("agate"))) < \
        chain.index(next(f for f in chain if f.startswith("asoftclip")))


def test_ceiling_is_opus_safe():
    lims = [f for f in _chain() if f.startswith("alimiter")]
    assert lims and all(float(f.split("limit=")[1].split(":")[0]) <= 0.8913 for f in lims[-2:])  # v65 -1 dBFS
