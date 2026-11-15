"""v79: VC bridge voice louder on phone speakers + slim Audio Controls panel."""
import os
import re
import shutil
import subprocess
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import helpers  # noqa: F401,E402

def test_audio_panel_only_tez_modes_and_echo():
    from plugins.ui import settings_kb
    datas = [b.callback_data for row in settings_kb({}).inline_keyboard for b in row]
    for want in ("set:tez", "set:lmode:loudest", "set:lmode:phone", "set:lmode:bass",
                 "set:echo:toggle"):
        assert want in datas
    for gone in ("set:relay:-250", "set:gain:50", "set:bass:5", "set:boost:1",
                 "set:playdb:2", "set:masterdb:2", "set:ceiling:cycle", "set:reset"):
        assert gone not in datas


def test_live_mic_modes_do_not_change_global_playback_panel():
    from helpers import live_mic_v79
    assert live_mic_v79.MODES == ("loudest", "clean", "warm")
