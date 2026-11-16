"""v92: Fight-ID-only mode (still supported in v93; login-first rules in test_v93).

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


def test_more_menu_names_fight_and_main_id():
    from plugins.ui import more_kb
    cbs = _callbacks(more_kb())
    assert "mic:acct" in cbs and "menu:mainid" in cbs


def test_fight_step_order():
    from helpers.setup_flow import fight_step
    assert fight_step(False, -100123) == "fight"
    assert fight_step(True, None) == "tgt"
    assert fight_step(True, -100123) == "ready"


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


