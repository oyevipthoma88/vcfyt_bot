from helpers import live_mic


def test_browser_disables_phone_processing():
    h = live_mic.LIVE_MIC_HTML
    assert "noiseSuppression: false" in h and "autoGainControl: false" in h


def test_fight_prechain_default_on():
    h = live_mic.LIVE_MIC_HTML
    assert 'get("boost") !== "clean"' in h
    assert "pres.gain.value=FIGHT?6:0" in h and "pre.gain.value = FIGHT?4.0:2.0" in h


def test_mic_command_uses_web_mic():
    src = open("plugins/vc_commands.py").read()
    assert "run_web_mic(msg" in src and "run_web_mic(_CbMsg(cq), \"on\")" in src
