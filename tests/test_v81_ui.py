from types import SimpleNamespace as NS


def _st(**kw):
    d = dict(source_name="intro.mp3", is_paused=False, is_playing=True, loop=False,
             hand_raise=False, mic_blink=False, ss_on=False, echo=False,
             relay_volume=3000, boost=10, queue=[])
    d.update(kw)
    return NS(**d)


def test_now_playing_card_shows_track_and_mode():
    from plugins.ui import now_playing_text
    t = now_playing_text(_st(), "playing", "Fyt GC", "Rahul")
    assert "intro.mp3" in t and "Fyt GC" in t and "Rahul" in t and "Mode" in t


def test_now_playing_kb_has_tez_and_controls_first():
    from plugins.vc_commands import now_playing_kb
    rows = now_playing_kb(-1001, _st()).inline_keyboard
    assert [b.callback_data for b in rows[0]] == ["vc:pause:-1001", "vc:skip:-1001", "vc:stop:-1001"]
    assert rows[1][0].callback_data == "vc:tez:-1001"


def test_help_has_tez():
    from plugins.cmd_help import help_text
    assert ".tez" in help_text("tez") and help_text("maxloud")
