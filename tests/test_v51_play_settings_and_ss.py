import re
"""v51: played audio honours user settings + SS on/off session isolation."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Other test files stub out helpers.* modules; load the real ones here.
for _name in [n for n in list(sys.modules) if n == "helpers" or n.startswith("helpers.")]:
    if not getattr(sys.modules[_name], "__file__", None):
        del sys.modules[_name]

from helpers.audio_processor import build_ffmpeg_filter, playback_drive_db


def test_boost_changes_playback_drive():
    assert playback_drive_db(0) == 14.0
    assert playback_drive_db(10) == 20.0
    assert build_ffmpeg_filter(volume=3000, boost=0) != build_ffmpeg_filter(volume=3000, boost=10)


def test_treble_and_gain_reach_playback_chain():
    bright = build_ffmpeg_filter(volume=3000, treble=100, gain=0)
    dull = build_ffmpeg_filter(volume=3000, treble=30, gain=0)
    # v62 voice stage: treble -> 6 kHz shelf, gain -> pre-amp
    shelf = lambda f: float(re.search(r"highshelf=f=6000:g=(-?[\d.]+)", f).group(1))
    assert shelf(bright) > shelf(dull)
    assert build_ffmpeg_filter(volume=3000, gain=500) != build_ffmpeg_filter(volume=3000, gain=0)


def test_voice_male_bass_is_heavier_than_female():
    male = build_ffmpeg_filter(volume=3000, bass=60, treble=15)
    female = build_ffmpeg_filter(volume=3000, bass=5, treble=70)
    body = lambda f: float(re.search(r"equalizer=f=150:t=q:w=1.2:g=([\d.]+)", f).group(1))
    assert body(male) > body(female)


def test_file_names_with_spaces_get_safe_path(tmp_path):
    import importlib
    _shell_safe_path = importlib.import_module("helpers.vc_manager")._shell_safe_path
    src = tmp_path / "my voice 2.ogg"
    src.write_bytes(b"x")
    safe = _shell_safe_path(str(src))
    assert " " not in safe and os.path.exists(safe)


def test_each_screen_share_session_gets_its_own_state_file():
    import importlib
    UserVC = importlib.import_module("helpers.vc_manager").UserVC
    uvc = UserVC.__new__(UserVC)
    uvc.owner_id = 1
    uvc.chats = {}
    uvc.live_volume = 100
    st = uvc.state(-100)
    first = uvc._ss_state_path(-100)
    st.ss_gen += 1
    assert uvc._ss_state_path(-100) != first
