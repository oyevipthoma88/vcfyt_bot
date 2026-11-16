"""v93: Login first — /start par Fight ID nahi maanga jaata, login ke baad dobara login nahi.

  * Bina login home: pehla button 🔐 Login, Fight ID sirf optional
  * /login already logged-in user ko "already logged in" dikhata hai (phone prompt nahi)
  * /login naye user ko seedha number step par le jata hai, Fight ID prompt hata ke
  * Live Mic: Login wale user ko seedha MIC ON (Fight ID zaroori nahi)
  * Fight ID flow me apni hi ID aayi -> wo Login ban jati hai (rejected nahi)
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
    import plugins.sudo as ps
    import plugins.start as pstart
    import plugins.web_mic as wm
    import plugins.vc_commands as vc
    for mod in (ac, sudo, dbmod, ps, pstart, wm, vc):
        monkeypatch.setattr(mod, "db", d)
    monkeypatch.setattr(ac.Config, "is_owner", staticmethod(lambda uid: uid == 1))
    monkeypatch.setattr(ac.Config, "ASSISTANT_SESSION", "", raising=False)
    sudo._cache_clear()
    sudo._trial_locks.clear()
    sudo._engines.clear()
    sudo._dead.clear()
    return d


def _login(d, uid):
    _run(d.add_user(uid, "u", "U"))
    _run(d.update_string(uid, "M" * 300))
    sudo._cache_clear()


def test_logged_out_home_button_one_is_login():
    from plugins.ui import home_kb
    cbs = _callbacks(home_kb(False, False))
    assert cbs[0] == "menu:login"
    assert "mic:acct" in cbs  # Fight ID sirf optional


def test_home_override_new_user_login_first(fresh_db):
    import plugins.sudo as ps
    text, kb = _run(ps.home_override(800, "A"))
    cbs = _callbacks(kb)
    assert cbs[0] == "menu:login"
    assert "mic:acct" in cbs
    assert "sudo:trial" not in cbs  # owner ne Sudo ID add nahi ki


def test_home_override_with_sudo_ids_offers_trial_after_login(fresh_db):
    import plugins.sudo as ps
    _run(sudo.add_account("S" * 300, {"id": 9}))
    _, kb = _run(ps.home_override(801, "A"))
    cbs = _callbacks(kb)
    assert cbs[0] == "menu:login"
    assert cbs[1] == "sudo:trial"


def test_login_when_already_logged_in_does_not_ask_again(fresh_db, monkeypatch):
    import plugins.login as pl
    _login(fresh_db, 802)
    user = SimpleNamespace(id=802, username="u", first_name="U")
    text, kb = _run(pl._login_entry(user))
    assert "already logged in" in text
    assert 802 not in pl.CONVERSATION
    assert "mic:panel" in _callbacks(kb)


def test_login_new_user_goes_straight_to_phone_step(fresh_db, monkeypatch):
    import plugins.login as pl
    import plugins.vc_commands as vc

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(pl, "log_login_step", _noop)
    vc._mic_login[803] = {"stage": "phone"}  # purana Fight ID prompt
    user = SimpleNamespace(id=803, username="u", first_name="U")
    text, kb = _run(pl._login_entry(user))
    assert pl.CONVERSATION[803]["step"] == "pyro_waiting_phone"
    assert 803 not in vc._mic_login
    assert kb is pl.CANCEL_KB
    pl.CONVERSATION.pop(803, None)


def test_live_mic_panel_logged_in_user_gets_mic_on_without_fight_id(fresh_db):
    from plugins.web_mic import web_mic_panel
    _login(fresh_db, 804)
    _, kb = _run(web_mic_panel(804))
    cbs = _callbacks(kb)
    assert "mic:won" in cbs
    assert "menu:login" not in cbs


def test_live_mic_panel_without_any_id_offers_login_and_fight(fresh_db):
    from plugins.web_mic import web_mic_panel
    _, kb = _run(web_mic_panel(805))
    cbs = _callbacks(kb)
    assert "mic:won" not in cbs
    assert "menu:login" in cbs and "mic:acct" in cbs


def test_own_account_in_fight_flow_becomes_login(fresh_db, monkeypatch):
    import plugins.vc_commands as vc
    from helpers.setup_flow import has_fight_id, has_main_id
    from helpers.vc_manager import session_manager

    async def fake_add(uid, s):
        return SimpleNamespace(account_id=uid)

    monkeypatch.setattr(session_manager, "add", fake_add)
    _run(fresh_db.add_user(806, "u", "U"))
    ok, message = _run(vc._spare_save(806, "O" * 300, account_id=806))
    assert ok is True
    assert "Login ho gaya" in message
    assert _run(has_main_id(806)) is True
    assert _run(has_fight_id(806)) is False


def test_mic_engine_without_any_id_offers_login_and_fight(fresh_db, monkeypatch):
    import plugins.vc_commands as vc
    from helpers.vc_manager import session_manager
    sent = []

    async def no_relay(uid, slot=1):
        return None

    async def notify(msg, text, **kw):
        sent.append(kw.get("reply_markup"))

    monkeypatch.setattr(session_manager, "get_relay", no_relay)
    monkeypatch.setattr(vc, "mic_notify", notify)
    msg = SimpleNamespace(from_user=SimpleNamespace(id=807))
    assert _run(vc.get_mic_engine(msg)) is None
    cbs = _callbacks(sent[0])
    assert "menu:login" in cbs and "mic:acct" in cbs
