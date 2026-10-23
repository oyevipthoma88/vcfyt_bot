"""Volume control: playback/relay volume is permanently full (3000)."""
import os
from helpers.vc_manager import ChatState


def _st():
    try:
        return ChatState()
    except TypeError:
        return ChatState(0)


def test_low_saved_volume_is_raised_to_max():
    st = _st()
    st.apply_settings({"volume": 200, "relay_volume": 200, "gain": 50})
    assert st.volume == 3000
    assert st.relay_volume == 3000
    assert st.gain == 50


def test_low_volume_respected_only_with_env():
    os.environ["PLAY_ALLOW_QUIET"] = "1"
    try:
        st = _st()
        st.apply_settings({"volume": 200, "relay_volume": 200})
        assert st.volume == 200
    finally:
        os.environ.pop("PLAY_ALLOW_QUIET")


def test_volume_clamped_to_range():
    st = _st()
    st.apply_settings({"volume": 9999, "relay_volume": -5, "gain": 900})
    assert st.volume == 3000
    assert st.relay_volume == 3000
    assert st.gain == 500
