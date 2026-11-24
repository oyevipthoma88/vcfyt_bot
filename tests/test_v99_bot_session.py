"""v99: bot login Telegram ke cold-login outage (500 RPC_CALL_FAIL on
auth.ImportBotAuthorization) se bachta hai — session MongoDB me save hota hai."""
import asyncio

import pytest
from pyrogram import raw
from pyrogram.errors import AuthKeyUnregistered, InternalServerError

from helpers import bot_login as bl

TOKEN = "123456:ABCdef"
API_ID = 111


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class FakeDB:
    def __init__(self):
        self.data = {}

    async def get_app_value(self, key):
        return self.data.get(key)

    async def set_app_value(self, key, value):
        self.data[key] = value

    async def delete_app_value(self, key):
        self.data.pop(key, None)


def _rpc_call_fail():
    e = InternalServerError("RPC_CALL_FAIL")
    e.ID = "RPC_CALL_FAIL"
    return e


class FakeClient:
    def __init__(self, session_string, warm_error=None, cold_errors=None, log=None):
        self.session_string = session_string
        self.warm_error = warm_error
        self.cold_errors = cold_errors if cold_errors is not None else []
        self.log = log if log is not None else []
        self.stopped = False

    async def invoke(self, query, retries=10, *a, **kw):
        self.log.append(("invoke", type(query).__name__, retries))
        if self.cold_errors:
            raise self.cold_errors.pop(0)
        return "ok"

    async def start(self):
        if self.session_string:
            self.log.append(("warm", self.session_string))
            if self.warm_error:
                raise self.warm_error
            return self
        self.log.append(("cold",))
        await self.invoke(raw.functions.auth.ImportBotAuthorization(
            flags=0, api_id=API_ID, api_hash="h", bot_auth_token=TOKEN))
        return self

    async def export_session_string(self):
        return "SESSION-NEW"

    async def stop(self):
        self.stopped = True


@pytest.fixture
def fake_db(monkeypatch):
    d = FakeDB()
    monkeypatch.setattr(bl, "_db", lambda: d)
    monkeypatch.delenv("BOT_SESSION_STRING", raising=False)
    return d


def _factory(log, **kw):
    made = []

    def make(session_string=None):
        c = FakeClient(session_string, log=log, **kw)
        made.append(c)
        return c
    return make, made


def test_saved_session_restart_needs_no_new_telegram_login(fake_db):
    log = []
    _run(bl.save_session("main", TOKEN, API_ID, "SESSION-OLD"))
    make, _ = _factory(log)
    _run(bl.start_once(make, key="main", bot_token=TOKEN, api_id=API_ID))
    assert log == [("warm", "SESSION-OLD")]  # ImportBotAuthorization kabhi nahi bheja


def test_first_login_saves_session_for_next_restart(fake_db):
    log = []
    make, _ = _factory(log)
    _run(bl.start_once(make, key="main", bot_token=TOKEN, api_id=API_ID))
    assert ("cold",) in log
    assert _run(bl.load_saved_session("main", TOKEN, API_ID)) == "SESSION-NEW"


def test_telegram_500_on_saved_session_keeps_the_session(fake_db):
    _run(bl.save_session("main", TOKEN, API_ID, "SESSION-OLD"))
    make, _ = _factory([], warm_error=_rpc_call_fail())
    with pytest.raises(InternalServerError):
        _run(bl.start_once(make, key="main", bot_token=TOKEN, api_id=API_ID))
    assert _run(bl.load_saved_session("main", TOKEN, API_ID)) == "SESSION-OLD"


def test_dead_saved_session_is_dropped_and_bot_logs_in_fresh(fake_db):
    log = []
    _run(bl.save_session("main", TOKEN, API_ID, "SESSION-OLD"))
    make, _ = _factory(log, warm_error=AuthKeyUnregistered())
    _run(bl.start_once(make, key="main", bot_token=TOKEN, api_id=API_ID))
    assert ("cold",) in log
    assert _run(bl.load_saved_session("main", TOKEN, API_ID)) == "SESSION-NEW"


def test_changed_bot_token_ignores_old_session(fake_db):
    _run(bl.save_session("main", "999:OLDTOKEN", API_ID, "SESSION-OLD"))
    assert _run(bl.load_saved_session("main", TOKEN, API_ID)) is None


def test_new_login_retries_telegram_500_only_once_internally(fake_db):
    log = []
    make, _ = _factory(log)
    _run(bl.start_once(make, key="main", bot_token=TOKEN, api_id=API_ID))
    calls = [e for e in log if e[0] == "invoke"]
    assert calls == [("invoke", "ImportBotAuthorization", 1)]


def test_backoff_grows_from_15s_to_5min_cap(monkeypatch):
    monkeypatch.setattr(bl.random, "uniform", lambda a, b: 0)
    assert [bl.backoff_seconds(n) for n in range(1, 8)] == [15, 30, 60, 120, 240, 300, 300]


def test_start_forever_survives_rpc_call_fail_outage(fake_db, monkeypatch):
    monkeypatch.setattr(bl.random, "uniform", lambda a, b: 0)
    log, sleeps = [], []
    errors = [_rpc_call_fail(), _rpc_call_fail()]

    def make(session_string=None):
        return FakeClient(session_string, log=log, cold_errors=errors)

    async def fake_sleep(s):
        sleeps.append(s)

    _run(bl.start_forever(make, key="main", bot_token=TOKEN, api_id=API_ID, sleep=fake_sleep))
    assert sleeps == [15, 30]
    assert _run(bl.load_saved_session("main", TOKEN, API_ID)) == "SESSION-NEW"


def test_main_new_bot_is_in_memory_with_saved_session():
    import main
    bot = main._new_bot("")
    assert bot.in_memory is True
