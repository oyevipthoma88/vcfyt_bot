import asyncio
from helpers import live_mic

def test_page_has_level_meter_and_modes():
    h = live_mic.LIVE_MIC_HTML
    assert 'id="vuBar"' in h and "vuUpdate" in h
    assert "setSoundMode('loudest')" in h and "setSoundMode('phone')" in h and "setSoundMode('bass')" in h
    assert "setMode('aec','1')" in h

def test_panel_opens_web_mic_not_bridge():
    src = open("plugins/vc_commands.py").read()
    assert "web_mic_panel" in src and 'action == "panel"' not in src
    assert 'action == "wsetup"' in src
    assert 'action == "bridge"' not in src
