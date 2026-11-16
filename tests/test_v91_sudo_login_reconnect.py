"""v91: login + trial dono dikhen, aur Sudo ID 'Connection lost' se recover kare.

  * non-logged home = Login button + Free Trial button (user choose kare)
  * Sudo ID add nahi -> sirf Login (trial button nahi)
  * phone number / OTP kabhi group link nahi maana jaata (login na toote)
  * mari hui Sudo connection (is_connected=True par socket dead) restart hoti hai
  * group verify 'Connection lost' par fresh connection se dobara try karta hai
"""
import asyncio
from types import SimpleNamespace

import pytest

from helpers import access_control as ac
from helpers import database as dbmod
from helpers import sudo


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


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
    monkeypatch.setattr(ac.Config, "is_owner", staticmethod(lambda uid: uid == 1))
    import plugins.sudo as ps
    monkeypatch.setattr(ps, "db", d)
    sudo._cache_clear()
    sudo._trial_locks.clear()
    sudo._engines.clear()
    sudo._dead.clear()
    return d


def _callbacks(kb):
    return [getattr(b, "callback_data", None) for row in kb.inline_keyboard for b in row]


def test_home_shows_login_and_free_trial(fresh_db):
    import plugins.sudo as ps
    _run(sudo.add_account("S" * 300, {"id": 9}))
    _, kb = _run(ps.home_override(500, "A"))
    cbs = _callbacks(kb)
    assert "menu:login" in cbs
    assert "sudo:trial" in cbs


def test_home_without_sudo_ids_shows_only_login(fresh_db):
    import plugins.sudo as ps
    _, kb = _run(ps.home_override(501, "A"))
    cbs = _callbacks(kb)
    assert "menu:login" in cbs
    assert "sudo:trial" not in cbs


def test_phone_and_otp_are_not_group_links():
    import plugins.sudo as ps
    for text in ("+919876543210", "919876543210", "1 2 3 4 5", "12345"):
        assert ps._looks_like_group_ref(text) is False, text
    for text in ("https://t.me/+AbCdEf12", "t.me/mygroup", "@mygroup", "-1001234567890"):
        assert ps._looks_like_group_ref(text) is True, text


def test_connection_lost_is_a_connection_error():
    assert sudo.is_conn_error(OSError(ConnectionResetError("Connection lost")))
    assert sudo.is_conn_error(ConnectionError("Client has not been started yet"))

    class UserNotParticipant(Exception):
        pass
    assert not sudo.is_conn_error(UserNotParticipant("USER_NOT_PARTICIPANT"))


class _FakeUVC:
    started = 0

    def __init__(self, owner_id, session, label="uvc"):
        self.owner_id, self.string_session, self.label = owner_id, session, label
        self.client = None
        self.account_id, self.account_name, self.account_username = 9, "S", "s"
        self.chats = {}
        self.stopped = False

    async def start(self):
        _FakeUVC.started += 1

        async def get_me():
            return SimpleNamespace(id=9)
        self.client = SimpleNamespace(is_connected=True, get_me=get_me)

    async def stop(self):
        self.stopped = True

    def touch(self):
        pass


def test_dead_socket_engine_is_restarted(fresh_db, monkeypatch):
    from helpers import vc_manager
    monkeypatch.setattr(vc_manager, "UserVC", _FakeUVC)
    sess = "D" * 300
    _run(sudo.add_account(sess, {"id": 9}))
    key = sudo.account_key(sess)

    async def dead_get_me():
        raise OSError(ConnectionResetError("Connection lost"))
    dead = _FakeUVC(1, sess, "sudo")
    dead.client = SimpleNamespace(is_connected=True, get_me=dead_get_me)
    dead._sudo_ok_at = 0
    sudo._engines[key] = dead
    _FakeUVC.started = 0

    eng = _run(sudo._start_engine(key, _run(sudo.accounts())))
    assert eng is not dead
    assert dead.stopped is True
    assert _FakeUVC.started == 1


def test_verify_gc_retries_after_connection_lost(fresh_db, monkeypatch):
    import plugins.sudo as ps
    from pyrogram.enums import ChatMemberStatus, ChatType

    _run(sudo.add_account("V" * 300, {"id": 9}))
    _run(sudo.start_trial(600))
    calls = {"join": 0}
    chat = SimpleNamespace(id=-1001234567890, type=ChatType.SUPERGROUP, title="G", username=None)

    async def join_chat(_link):
        calls["join"] += 1
        if calls["join"] == 1:
            raise OSError(ConnectionResetError("Connection lost"))
        return chat

    async def get_chat_member(_cid, _who):
        return SimpleNamespace(status=ChatMemberStatus.MEMBER)

    def make_engine():
        e = SimpleNamespace(client=SimpleNamespace(join_chat=join_chat, get_chat_member=get_chat_member,
                                                   is_connected=True),
                            string_session="V" * 300, account_username="s", account_name="S")

        async def _call_input(_cid):
            return None
        e._call_input = _call_input
        return e

    async def engine_for(_uid):
        return make_engine()

    async def no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(sudo, "engine_for", engine_for)
    monkeypatch.setattr(ps.asyncio, "sleep", no_sleep)
    user = SimpleNamespace(id=600, username="u", first_name="U")
    ok, text, _ = _run(ps.verify_gc(None, user, "https://t.me/+AbCdEf12"))
    assert ok is True, text
    assert calls["join"] == 2
    assert _run(sudo.get_gc(600))[0] == -1001234567890
