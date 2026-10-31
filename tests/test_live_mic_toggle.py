import asyncio

import helpers.feature_flags as ff
from plugins import ui


def _labels(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


def test_live_mic_off_hides_button_for_users(monkeypatch):
    monkeypatch.setitem(ff._cache, "live_mic", False)
    assert not any("Live Mic" in t for t in _labels(ui.home_kb(False, True)))
    assert "Live mic" not in ui.home_text("A", True)


def test_live_mic_on_shows_button_for_users(monkeypatch):
    monkeypatch.setitem(ff._cache, "live_mic", True)
    assert any("Live Mic" in t for t in _labels(ui.home_kb(False, True)))


def test_owner_still_sees_mic_when_off(monkeypatch):
    monkeypatch.setitem(ff._cache, "live_mic", False)
    assert any("users OFF" in t for t in _labels(ui.home_kb(True, True)))


def test_set_live_mic_saves_value(monkeypatch):
    saved = {}

    async def fake_set(k, v):
        saved[k] = v

    monkeypatch.setattr(ff.db, "set_app_value", fake_set)
    asyncio.run(ff.set_live_mic(False))
    assert saved[ff.LIVE_MIC_KEY] == "0" and ff.live_mic_on() is False
    asyncio.run(ff.set_live_mic(True))
    assert saved[ff.LIVE_MIC_KEY] == "1"
