"""Volume control regression: saved low volume must not be forced back to max."""
from helpers.vc_manager import ChatState


def _st():
    try:
        return ChatState()
    except TypeError:
        return ChatState(0)


def test_low_volume_is_respected():
    st = _st()
    st.apply_settings({"volume": 200, "relay_volume": 200, "gain": 50})
    assert st.volume == 200
    assert st.relay_volume == 200
    assert st.gain == 50


def test_volume_clamped_to_range():
    st = _st()
    st.apply_settings({"volume": 9999, "relay_volume": -5, "gain": 900})
    assert st.volume == 3000
    assert st.relay_volume == 0
    assert st.gain == 500
