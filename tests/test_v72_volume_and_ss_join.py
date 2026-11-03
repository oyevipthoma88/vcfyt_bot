"""v72: 200% volume race + screen-share join root fixes."""
import asyncio, os, sys, types
from types import SimpleNamespace
import pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
vm = pytest.importorskip("helpers.vc_manager")
CLS = vm.UserVC


def _nosleep():
    async def nosleep(_):
        return None
    return nosleep


def _run(coro):
    orig = vm.asyncio.sleep
    vm.asyncio.sleep = _nosleep()
    try:
        return asyncio.run(coro)
    finally:
        vm.asyncio.sleep = orig


# ---------------------------------------------------------------- volume race
def _vol_engine(join_after_polls):
    """Account appears in the VC only after N participant polls (slow join)."""
    o = SimpleNamespace(account_id=1, owner_id=0, _keepers={})
    o._st = SimpleNamespace(boost_confirmed=None)
    o.state = lambda cid: o._st
    o.calls_order = []
    o.polls = 0
    o.vol = 10000

    def guard(cid, uid, target, restore=None):
        o.calls_order.append("guard")
    o.guard_volume = guard

    async def parts(cid):
        o.polls += 1
        return [{"uid": 1}] if o.polls > join_after_polls else []
    o.vc_participants = parts

    async def setv(cid, uid, vol, quiet=False):
        if o.polls > join_after_polls:      # Telegram only accepts once joined
            o.vol = vol
            return True
        return False
    o.set_participant_volume = setv

    async def readv(cid, uid):
        return o.vol if o.polls > join_after_polls else None
    o.read_participant_volume = readv

    async def others(cid):
        return []
    o._other_admin_setters = others
    o._wait_in_vc = lambda cid, timeout=20.0: CLS._wait_in_vc(o, cid, timeout)
    return o


def test_slow_join_still_gets_200_percent():
    # join takes 10 polls (~8 s) - old code gave up after ~5 s at 100 %
    o = _vol_engine(join_after_polls=10)
    assert _run(CLS.ensure_play_volume(o, -100)) is True
    assert o.vol == 20000 and o._st.boost_confirmed is True


def test_guard_installed_before_any_attempt():
    o = _vol_engine(join_after_polls=0)
    _run(CLS.ensure_play_volume(o, -100))
    assert o.calls_order[0] == "guard"


# ---------------------------------------------------------------- SS join
class _TSE(Exception):
    pass


def _ss_engine(fail_first):
    ntg = pytest.importorskip("ntgcalls")
    log = []
    state = {"init": 0, "open": False}

    class Binding:
        async def get_connection_mode(self, cid):
            return ntg.ConnectionMode.RTC

        async def init_presentation(self, cid):
            if state["open"]:
                raise ConnectionError("Screen sharing already initialized")
            state["open"] = True
            state["init"] += 1
            log.append("init")
            return "{}"

        async def connect(self, cid, params, pres):
            log.append("connect")
            fut = calls._wait_connect[cid]
            if state["init"] <= fail_first:
                fut.set_exception(_TSE())
            else:
                fut.set_result(None)

        async def stop_presentation(self, cid):
            log.append("stop")
            state["open"] = False

    class App:
        async def join_presentation(self, cid, payload):
            return "{}"

        async def leave_presentation(self, cid):
            log.append("leave")

    calls = SimpleNamespace(_binding=Binding(), _app=App(), _presentations=set(),
                            _wait_connect={}, loop=None)
    m = object.__new__(CLS)
    m.calls = calls
    m.chats = {}
    return m, calls, log


def test_ss_join_retries_after_closing_half_open_presentation():
    m, calls, log = _ss_engine(fail_first=1)

    async def go():
        calls.loop = asyncio.get_running_loop()
        await m._ss_join_robust(-100)
    _run(go())
    assert -100 in calls._presentations
    # failed attempt is torn down BEFORE the second init (old bug: 2nd init raised)
    assert log == ["init", "connect", "stop", "leave", "init", "connect"]


def test_ss_join_gives_clear_error_after_3_failures():
    m, calls, log = _ss_engine(fail_first=99)

    async def go():
        calls.loop = asyncio.get_running_loop()
        await m._ss_join_robust(-100)
    with pytest.raises(Exception):
        _run(go())
    assert log.count("init") == 3 and -100 not in calls._presentations


def test_presentation_drop_does_not_wipe_whole_call():
    ntg = pytest.importorskip("ntgcalls")
    seen = []

    async def orig(cid, net):
        seen.append(cid)   # = pytgcalls _clear_cache path (bot "left" the VC)

    m = object.__new__(CLS)
    m.chats = {-100: SimpleNamespace(ss_on=False)}
    m.calls = SimpleNamespace(_handle_connection_changed=orig, _presentations={-100},
                              _wait_connect={})
    m._install_presentation_conn_guard()
    pres_closed = SimpleNamespace(kind=ntg.ConnectionKind.PRESENTATION,
                                  state=ntg.ConnectionState.CLOSED)
    asyncio.run(m.calls._handle_connection_changed(-100, pres_closed))
    assert seen == [] and -100 not in m.calls._presentations
    main_closed = SimpleNamespace(kind=ntg.ConnectionKind.NORMAL,
                                  state=ntg.ConnectionState.CLOSED)
    asyncio.run(m.calls._handle_connection_changed(-100, main_closed))
    assert seen == [-100]   # real call drop still handled by pytgcalls
