"""v100: Fight ID login ka number/OTP admin URL handler na kha jaye."""
import re


def test_owner_url_input_not_in_spare_login_group():
    src = open("plugins/admin.py").read()
    i = src.index("async def owner_live_mic_url_input")
    deco = src[src.rindex("@Client.on_message", 0, i):i]
    assert "group=3" not in deco
    assert "_awaiting_mic_url" in deco


def test_owner_url_filter_only_matches_waiting_owner():
    from types import SimpleNamespace as N
    from plugins import admin
    m = N(from_user=N(id=555))
    assert not admin._awaiting_mic_url(None, None, m)
    admin._AWAIT_LIVE_MIC_URL.add(555)
    try:
        assert admin._awaiting_mic_url(None, None, m)
    finally:
        admin._AWAIT_LIVE_MIC_URL.discard(555)
