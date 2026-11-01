"""v70: 200% participant volume must be CONFIRMED from Telegram, not assumed."""
import asyncio, os, sys, types
import pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
vm = pytest.importorskip("helpers.vc_manager")


class _St:
    boost_confirmed = None


def _fake(self_vol_after_set, account_id=1):
    o = types.SimpleNamespace()
    o.account_id = account_id
    o._st = _St()
    o.__dict__["_vol"] = 10000
    o.state = lambda cid: o._st
    o.guard_volume = lambda *a, **k: None

    async def setv(cid, uid, vol, quiet=False):
        o.__dict__["_vol"] = self_vol_after_set
        return True

    async def readv(cid, uid):
        return o.__dict__["_vol"]

    async def others(cid):
        return []
    o.set_participant_volume = setv
    o.read_participant_volume = readv
    o._other_admin_setters = others
    return o


def _run(o):
    async def nosleep(_):
        return None
    orig = vm.asyncio.sleep
    vm.asyncio.sleep = nosleep
    try:
        return asyncio.run(vm.UserVC.ensure_play_volume(o, -100))
    finally:
        vm.asyncio.sleep = orig


def test_confirmed_when_telegram_shows_200():
    o = _fake(20000)
    assert _run(o) is True and o._st.boost_confirmed is True


def test_not_confirmed_when_telegram_ignores_set():
    o = _fake(10000)  # non-admin: request "succeeds" but volume stays 100%
    assert _run(o) is False and o._st.boost_confirmed is False
