"""v88: live mic LOUDEST is louder than before, admin 200 % never makes it
quieter, page shows the fight ID, and .playmute no longer crashes."""
import ast
from pathlib import Path

from helpers import live_mic_v79 as v79
from helpers.live_mic import LIVE_MIC_HTML


def test_admin_boost_does_not_lower_loudest():
    assert v79.build_live_mic_filter(-6.5, mode="loudest") == \
        v79.build_live_mic_filter(-0.3, mode="loudest")


def test_extra_tez_slider_changes_chain():
    assert v79.build_live_mic_filter(-0.3, crunch=200, mode="loudest") != \
        v79.build_live_mic_filter(-0.3, crunch=0, mode="loudest")


def test_page_shows_fight_id_profile():
    assert 'id="relayName"' in LIVE_MIC_HTML and 'id="relayDp"' in LIVE_MIC_HTML
    assert "startsWith('relay:')" in LIVE_MIC_HTML


def test_playmute_handler_defines_its_args_and_tag_does_not_touch_uvc():
    tree = ast.parse(Path("plugins/vc_commands.py").read_text())
    fns = {n.name: n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)}
    pm = ast.unparse(fns["cmd_unmuteaudio"])
    assert "source_arg, cid_arg = _split_args(msg)" in pm
    assert "clear_playmute" in pm
    assert "uvc" not in ast.unparse(fns["cmd_tag"])
