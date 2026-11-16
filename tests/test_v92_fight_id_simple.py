"""v92: simple setup — ⚔️ Fight ID zaroori, 👤 Main ID optional.

  * /start (bina login) = sirf EK setup button (⚔️ Fight ID), 2 login options nahi
  * Main ID screen me sirf ek login button, "Multiple ID Login" hata diya
  * Fight ID jud gayi to home par seedha 🎙 Live Mic
  * Live Mic engine: Main ID nahi hai to user ki Fight ID hi bolti hai
"""
import asyncio
from types import SimpleNamespace

import pytest

from helpers import access_control as ac
from helpers import database as dbmod
from helpers import sudo


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _callbacks(kb):
    return [getattr(b, "callback_data", None) for row in kb.inline_keyboard for b in row]


def _labels(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    d = dbmod.Database.__new__(dbmod.Database)
    d._mongo = None
    d._sqlite_path = str(tmp_path / "t.db")
    d._use_mongo = False
    d._purging = False
    d._quota_blocked_until = 0.0
    d._last_purge = 0.0
    d._init_sqlite()
    monkeypatch.setattr(ac, "db", d)
    monkeypatch.setattr(sudo, "db", d)
    monkeypatch.setattr(dbmod, "db", d)
    monkeypatch.setattr(ac.Config, "is_owner", staticmethod(lambda uid: uid == 1))
    import plugins.sudo as ps
    monkeypatch.setattr(ps, "db", d)
    sudo._cache_clear()
    sudo._trial_locks.clear()
    sudo._engines.clear()
    sudo._dead.clear()
    return d


def test_logged_out_home_has_single_setup_step():
    from plugins.ui import home_kb, home_text
    kb = home_kb(False, False)
    cbs = _callbacks(kb)
    assert cbs.count("menu:login") == 1
    assert "login:phone" not in cbs and "mic:acct" not in cbs
    assert "Fight ID" in _labels(kb)[0]
    assert "zaroori nahi" in home_text("A", False)


def test_main_id_screen_has_one_login_option_only():
    from plugins.ui import login_kb, LOGIN_INTRO
    labels = _labels(login_kb())
    assert _callbacks(login_kb()).count("login:phone") == 1
    assert not any("Multiple" in t for t in labels)
    assert "optional" in LOGIN_INTRO.lower()


def test_more_menu_names_fight_and_main_id():
    from plugins.ui import more_kb
    cbs = _callbacks(more_kb())
    assert "mic:acct" in cbs and "menu:mainid" in cbs


def test_fight_step_order():
    from helpers.setup_flow import fight_step
    assert fight_step(False, -100123) == "fight"
    assert fight_step(True, None) == "tgt"
    assert fight_step(True, -100123) == "ready"


def test_home_without_fight_id_starts_with_fight_step(fresh_db):
    import plugins.sudo as ps
    _, kb = _run(ps.home_override(700, "A"))
    cbs = _callbacks(kb)
    assert cbs[0] == "menu:login"
    assert "mic:panel" not in cbs


def test_home_with_fight_id_goes_straight_to_live_mic(fresh_db):
    import plugins.sudo as ps
    from helpers.vc_manager import session_manager
    _run(sudo.add_account("S" * 300, {"id": 9}))
    _run(fresh_db.set_app_value(session_manager.spare_key(701, 1), "F" * 300))
    text, kb = _run(ps.home_override(701, "A"))
    cbs = _callbacks(kb)
    assert cbs[0] == "mic:panel"
    assert "sudo:trial" not in cbs
    assert "Fight ID judi" in text


def test_mic_engine_uses_fight_id_without_main_id(fresh_db, monkeypatch):
    import plugins.vc_commands as vc
    from helpers.vc_manager import session_manager
    relay = SimpleNamespace(name="fight")
    _run(fresh_db.set_app_value(session_manager.spare_key(702, 1), "F" * 300))

    async def fake_relay(uid, slot=1):
        return relay

    async def no_main(msg):
        raise AssertionError("main engine must not be used without a Main ID")

    monkeypatch.setattr(session_manager, "get_relay", fake_relay)
    monkeypatch.setattr(vc, "get_engine", no_main)
    msg = SimpleNamespace(from_user=SimpleNamespace(id=702))
    assert _run(vc.get_mic_engine(msg)) is relay


def test_mic_engine_asks_for_fight_id_when_nothing_set(fresh_db, monkeypatch):
    import plugins.vc_commands as vc
    from helpers.vc_manager import session_manager
    sent = []

    async def no_relay(uid, slot=1):
        return None

    async def notify(msg, text, **kw):
        sent.append((text, kw.get("reply_markup")))

    monkeypatch.setattr(session_manager, "get_relay", no_relay)
    monkeypatch.setattr(vc, "mic_notify", notify)
    msg = SimpleNamespace(from_user=SimpleNamespace(id=703))
    assert _run(vc.get_mic_engine(msg)) is None
    assert "Fight ID" in sent[0][0]
    assert "mic:acct" in _callbacks(sent[0][1])
