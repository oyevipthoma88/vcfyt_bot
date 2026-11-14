"""v86: Auto Cloudflare tunnel fallback when Heroku domain is unknown + dead URL cleanup."""
import asyncio
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from config import Config
from helpers import live_mic, mic_url, mic_tunnel


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


@pytest.fixture
def env(monkeypatch):
    import helpers.database as hdb
    fake = FakeDB()
    monkeypatch.setattr(hdb, "db", fake, raising=False)
    monkeypatch.setattr(Config, "BOT_TOKEN", "123:test-token", raising=False)
    monkeypatch.setattr(Config, "_LIVE_MIC_OVERRIDE", "", raising=False)
    monkeypatch.setattr(Config, "_HEROKU_DEFAULT_DOMAIN", "", raising=False)
    monkeypatch.setattr(Config, "_HEROKU_APP_NAME", "", raising=False)
    monkeypatch.setenv("DYNO", "web.1")
    monkeypatch.delenv("HEROKU_API_KEY", raising=False)
    monkeypatch.setattr(mic_url, "_verified", "")
    monkeypatch.setattr(mic_url, "_seen_host", "")
    monkeypatch.setattr(mic_url, "_lock", None)
    monkeypatch.setattr(mic_url, "PROBE_TIMEOUT", 1.0)
    monkeypatch.setattr(mic_url, "TUNNEL_PROBES", 2)
    monkeypatch.setattr(mic_url, "TUNNEL_PROBE_GAP", 0.05)
    return fake


async def _servers(*apps):
    servers = [TestServer(a, host="127.0.0.1") for a in apps]
    for s in servers:
        await s.start_server()
    return servers, [f"http://127.0.0.1:{s.port}" for s in servers]


def test_dead_last_working_url_is_wiped_from_db(env, monkeypatch):
    """When a stored host returns FOREIGN, it must be cleared so it won't be tried again."""
    async def go():
        servers, (dead,) = await _servers(_heroku_nothing_here())
        try:
            env.data[mic_url.DB_LAST_GOOD] = dead
            # Prevent tunnel from starting in this test
            monkeypatch.setattr(mic_tunnel, "enabled", lambda: False)
            res = await mic_url.resolve(force=True)
            assert res == ""
            # Stored dead URL must be cleared from DB
            assert env.data[mic_url.DB_LAST_GOOD] == ""
        finally:
            for s in servers:
                await s.close()
    asyncio.run(go())


def test_auto_tunnel_used_when_heroku_domain_unknown(env, monkeypatch):
    """When Heroku domain is unknown, auto tunnel is launched and verified."""
    async def go():
        servers, (ours,) = await _servers(live_mic.create_app())
        try:
            # Mock ensure() to return our test server (which has real /health fingerprint)
            async def fake_ensure(force_new=False):
                return ours

            monkeypatch.setattr(mic_tunnel, "ensure", fake_ensure)
            monkeypatch.setattr(mic_tunnel, "enabled", lambda: True)

            url = await mic_url.resolve(force=True)
            assert url == ours
            assert mic_url.cached() == ours
        finally:
            for s in servers:
                await s.close()
    asyncio.run(go())


def test_tunnel_disabled_via_env(monkeypatch):
    monkeypatch.setenv("LIVE_MIC_TUNNEL", "off")
    assert not mic_tunnel.enabled()
    monkeypatch.setenv("LIVE_MIC_TUNNEL", "0")
    assert not mic_tunnel.enabled()
    monkeypatch.setenv("LIVE_MIC_TUNNEL", "auto")
    assert mic_tunnel.enabled()
