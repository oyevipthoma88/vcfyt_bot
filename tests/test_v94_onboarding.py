"""v94: Smart Setup Guide, saaf command menu, secrets kabhi log nahi, config crash fixes."""
import asyncio
import importlib
import os

import pytest


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _cbs(rows):
    return [cb for row in rows for (_, cb, _) in row]


def _state(**kw):
    base = {"main": False, "fight": False, "sudo": False, "voice": False, "group": 0,
            "trial_available": False}
    base.update(kw)
    base["voice"] = bool(base["main"] or base["fight"] or base["sudo"])
    return base


# ── Setup Guide ─────────────────────────────────────────────────

def test_new_user_first_button_is_login():
    from helpers.onboarding import build_guide
    _, rows = build_guide(_state(), "A")
    assert rows[0][0][1] == "menu:login"
    assert "mic:acct" in _cbs(rows)


def test_new_user_sees_trial_only_when_available():
    from helpers.onboarding import build_guide
    assert "sudo:trial" not in _cbs(build_guide(_state(), "A")[1])
    assert "sudo:trial" in _cbs(build_guide(_state(trial_available=True), "A")[1])


def test_logged_in_without_group_next_is_group_set():
    from helpers.onboarding import build_guide, progress
    st = _state(main=True)
    _, rows = build_guide(st, "A")
    assert rows[0][0][1] == "guide:setgc"
    assert progress(st) == (1, 3)


def test_sudo_user_without_group_uses_sudo_group_flow():
    from helpers.onboarding import next_step
    assert next_step(_state(sudo=True))[1] == "sudo:gc"


def test_setup_complete_when_voice_and_group():
    from helpers.onboarding import build_guide, home_hint_row, is_complete, progress
    st = _state(main=True, group=-1001234567890)
    assert is_complete(st)
    assert progress(st) == (2, 3)
    assert home_hint_row(st) is None
    text, rows = build_guide(st, "A")
    assert "mic:panel" in _cbs(rows) or "vc:list:0" in _cbs(rows)
    assert "-1001234567890" in text


def test_home_hint_shown_while_incomplete():
    from helpers.onboarding import home_hint_row
    row = home_hint_row(_state(main=True))
    assert row and row[0][1] == "guide:open"


def test_guide_text_escapes_name():
    from helpers.onboarding import build_guide
    text, _ = build_guide(_state(), "<b>x</b>")
    assert "<b>x</b>" not in text and "&lt;b&gt;x&lt;/b&gt;" in text


def test_guide_command_is_known_to_help():
    from plugins.cmd_help import ALIAS
    assert ALIAS["guide"] == "guide" and ALIAS["setup"] == "guide"


# ── Command menu ────────────────────────────────────────────────

def test_user_menu_has_no_owner_commands():
    import main
    user = {c.command for c in main.USER_COMMANDS}
    owner = {c.command for c in main.OWNER_COMMANDS}
    assert "guide" in user and "start" in user and "login" in user
    assert not (user & owner)
    assert {"broadcast", "restart", "addsudo", "cleandb"} <= owner
    for c in main.USER_COMMANDS + main.OWNER_COMMANDS:
        assert c.description.strip() == c.description and c.description  # no blank/space-led text


# ── Security: secrets never logged ──────────────────────────────



def _reload_config(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import config
    return importlib.reload(config).Config


def test_owner_ids_semicolon_and_junk_do_not_crash(monkeypatch):
    C = _reload_config(monkeypatch, OWNER_ID="3", OWNER_IDS="5; 7,abc 9")
    try:
        assert C.OWNER_IDS == (3, 5, 7, 9)
        assert C.primary_owner() == 3
    finally:
        monkeypatch.delenv("OWNER_IDS"); monkeypatch.delenv("OWNER_ID")
        import config; importlib.reload(config)


def test_bad_float_env_uses_default(monkeypatch):
    C = _reload_config(monkeypatch, PLAY_PUSH_DB="loud", DEFAULT_VOLUME="99999")
    try:
        assert C.PLAY_PUSH_DB == 18.0
        assert C.DEFAULT_VOLUME == 3000
    finally:
        monkeypatch.delenv("PLAY_PUSH_DB"); monkeypatch.delenv("DEFAULT_VOLUME")
        import config; importlib.reload(config)


# ── Default group survives restart (DB) ─────────────────────────

def test_default_group_roundtrip_through_db(monkeypatch):
    import plugins.vc_commands as vc
    store = {}

    class FakeDB:
        async def set_app_value(self, k, v):
            store[k] = v

        async def get_app_value(self, k):
            return store.get(k)
    monkeypatch.setattr(vc, "db", FakeDB())
    monkeypatch.setattr(vc, "_GC_FILE", os.devnull)
    monkeypatch.setattr(vc, "DEFAULT_GC", {})

    async def save():
        vc.DEFAULT_GC[42] = -1001234567890
        vc._save_gc()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    _run(save())
    assert vc._GC_DB_KEY in store
    vc.DEFAULT_GC.clear()  # "restart"
    assert _run(vc.load_gc_from_db()) == 1
    assert vc.DEFAULT_GC[42] == -1001234567890


# ── Live state from the real (sqlite) DB ────────────────────────

@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    from helpers import access_control as ac
    from helpers import database as dbmod
    from helpers import sudo
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
    import plugins.live_mic_target as lmt
    for mod in (ac, sudo, dbmod, ps, pstart, wm, vc, lmt):
        monkeypatch.setattr(mod, "db", d)
    monkeypatch.setattr(ac.Config, "is_owner", staticmethod(lambda uid: uid == 1))
    monkeypatch.setattr(ac.Config, "ASSISTANT_SESSION", "", raising=False)
    monkeypatch.setattr(vc, "DEFAULT_GC", {})
    sudo._cache_clear()
    return d


def test_setup_state_follows_login_then_group(fresh_db):
    from helpers import sudo
    from helpers.onboarding import setup_state, next_step
    from plugins.live_mic_target import save_target
    st = _run(setup_state(900))
    assert not st["voice"] and next_step(st)[1] == "menu:login"
    _run(fresh_db.add_user(900, "u", "U"))
    _run(fresh_db.update_string(900, "M" * 300))
    sudo._cache_clear()
    st = _run(setup_state(900))
    assert st["main"] and not st["group"] and next_step(st)[1] == "guide:setgc"
    _run(save_target(900, -1001234567890))
    st = _run(setup_state(900))
    assert st["group"] == -1001234567890
