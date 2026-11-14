"""v85: Live mic link must never point at a dead / foreign host.

Bug (screenshot): `.mic on` link opened Heroku "There's nothing here, yet."
because an unverified host (stale Mongo value / wrong env / guess) was used.
"""
import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from config import Config
from helpers import live_mic, mic_url


class FakeDB:
    def __init__(self, data=None):
        self.data = dict(data or {})

    async def get_app_value(self, key):
        return self.data.get(key)

    async def set_app_value(self, key, value):
        self.data[key] = value


def _heroku_nothing_here():
    app = web.Application()

    async def any_path(request):
        return web.Response(status=404, text="<h1>There's nothing here, yet.</h1>",
                            content_type="text/html")
    app.router.add_get("/{tail:.*}", any_path)
    return app


def _old_bot_without_id():
    app = web.Application()
    app.router.add_get("/health", lambda r: web.Response(text="vcfyt bot is running\n"))
    return app


@pytest.fixture
def env(monkeypatch):
    import helpers.database as hdb
    fake = FakeDB()
    monkeypatch.setattr(hdb, "db", fake, raising=False)
    monkeypatch.setattr(Config, "BOT_TOKEN", "123:test-token", raising=False)
    monkeypatch.setattr(Config, "_LIVE_MIC_OVERRIDE", "", raising=False)
    monkeypatch.setattr(Config, "_HEROKU_DEFAULT_DOMAIN", "", raising=False)
    monkeypatch.setattr(Config, "_HEROKU_APP_NAME", "", raising=False)
    monkeypatch.setenv("DYNO", "web.1")          # behave like Heroku
    monkeypatch.delenv("HEROKU_API_KEY", raising=False)
    monkeypatch.setattr(mic_url, "_verified", "")
    monkeypatch.setattr(mic_url, "_seen_host", "")
    monkeypatch.setattr(mic_url, "_lock", None)
    monkeypatch.setattr(mic_url, "PROBE_TIMEOUT", 2.0)
    return fake


async def _servers(*apps):
    servers = [TestServer(a, host="127.0.0.1") for a in apps]
    for s in servers:
        await s.start_server()
    return servers, [f"http://127.0.0.1:{s.port}" for s in servers]


def test_stale_stored_host_is_skipped_real_one_used(env, monkeypatch):
    async def go():
        servers, (ours, dead) = await _servers(live_mic.create_app(), _heroku_nothing_here())
        try:
            env.data[mic_url.DB_LAST_GOOD] = dead            # old Heroku app
            monkeypatch.setattr(Config, "_LIVE_MIC_OVERRIDE", ours)
            assert await live_mic.resolve_base_url() == ours
            assert env.data[mic_url.DB_LAST_GOOD] == ours     # stale value replaced
        finally:
            for s in servers:
                await s.close()
    asyncio.run(go())


def test_wrong_env_override_never_sent(env, monkeypatch):
    async def go():
        servers, (ours, dead) = await _servers(live_mic.create_app(), _heroku_nothing_here())
        try:
            monkeypatch.setattr(Config, "_LIVE_MIC_OVERRIDE", dead)   # copied wrong URL
            env.data[mic_url.DB_LAST_GOOD] = ours
            assert await live_mic.resolve_base_url() == ours
        finally:
            for s in servers:
                await s.close()
    asyncio.run(go())


def test_no_working_host_gives_empty_and_report(env, monkeypatch):
    async def go():
        servers, (dead, old) = await _servers(_heroku_nothing_here(), _old_bot_without_id())
        try:
            monkeypatch.setattr(Config, "_LIVE_MIC_OVERRIDE", dead)
            env.data[mic_url.DB_LAST_GOOD] = old
            assert await live_mic.resolve_base_url() == ""
            report = mic_url.explain_failure()
            assert dead in report and old in report
        finally:
            for s in servers:
                await s.close()
    asyncio.run(go())


def test_verified_url_rechecked_when_app_dies(env, monkeypatch):
    async def go():
        servers, (ours,) = await _servers(live_mic.create_app())
        monkeypatch.setattr(Config, "_LIVE_MIC_OVERRIDE", ours)
        assert await live_mic.resolve_base_url() == ours
        await servers[0].close()                            # app deleted / renamed
        monkeypatch.setattr(mic_url, "_verified_at", 0.0)
        assert await live_mic.resolve_base_url() == ""
    asyncio.run(go())


def test_mic_url_command_rejects_foreign_and_saves_real(env):
    async def go():
        servers, (ours, dead) = await _servers(live_mic.create_app(), _heroku_nothing_here())
        try:
            assert await mic_url.set_override(dead) == (False, mic_url.FOREIGN)
            ok, base = await mic_url.set_override(ours + "/?token=abc")
            assert ok and base == ours
            assert env.data[mic_url.DB_OVERRIDE] == ours
            assert await live_mic.resolve_base_url() == ours
        finally:
            for s in servers:
                await s.close()
    asyncio.run(go())


def test_forwarded_host_header_cannot_poison_link(env):
    class Req:
        headers = {"Host": "real-app-1a2b3c4d5e6f.herokuapp.com",
                   "X-Forwarded-Host": "evil.example.com",
                   "X-Forwarded-Proto": "https"}
    mic_url.remember_request(Req())
    assert mic_url._seen_host == "https://real-app-1a2b3c4d5e6f.herokuapp.com"


def test_health_has_fingerprint_not_token(env):
    text = mic_url.health_text()
    assert "vcfyt" in text and f"id: {mic_url.fingerprint()}" in text
    assert "test-token" not in text


def test_normalize():
    n = mic_url.normalize
    assert n("my-app-1a2b.herokuapp.com/?token=1:2:3") == "https://my-app-1a2b.herokuapp.com"
    assert n("http://my-app.herokuapp.com/") == "https://my-app.herokuapp.com"
    assert n("http://127.0.0.1:8080") == "http://127.0.0.1:8080"
    assert n("") == "" and n("not a url") == ""


def test_mic_on_never_sends_unverified_link():
    src = open("plugins/web_mic.py").read()
    i = src.index("base = await live_mic.resolve_base_url()\n    if not base:")
    assert "_no_url_text()" in src[i:i + 600]
    assert '"url"' in open("plugins/vc_commands.py").read()


def test_new_page_keeps_mic_controls():
    h = live_mic.LIVE_MIC_HTML
    for el in ('id="toggleBtn"', 'id="vuBar"', 'id="boostR"', 'id="mFight"', 'id="mSpk"',
               'id="liveTimer"', 'id="tgBar"', 'id="permHelp"', 'id="retryBtn"'):
        assert el in h
