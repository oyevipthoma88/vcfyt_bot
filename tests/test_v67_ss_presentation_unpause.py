"""v67: SS ON ho to Telegram ko kabhi presentation_paused=True na jaye
(warna sabko sirf blurred DP dikhta hai), aur Telegram pe paused dikhe to
bot khud unpause kare."""
import asyncio, os, sys, time
from types import SimpleNamespace
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from helpers import vc_manager as vm

CLS = next(c for c in vars(vm).values()
           if isinstance(c, type) and hasattr(c, "_install_presentation_guard"))


def _mgr(ss_on):
    m = object.__new__(CLS)
    sent = []

    async def orig(chat_id, state):
        sent.append(state)

    m.calls = SimpleNamespace(_update_status=orig, _presentations={-100})
    m.chats = {-100: SimpleNamespace(ss_on=ss_on)}
    m._install_presentation_guard()
    return m, sent


def _state():
    return SimpleNamespace(muted=True, video_paused=True, video_stopped=True,
                           presentation_paused=True)


def test_status_update_never_pauses_presentation_while_ss_on():
    m, sent = _mgr(ss_on=True)
    asyncio.run(m.calls._update_status(-100, _state()))
    assert sent[0].presentation_paused is False
    assert sent[0].muted is True  # mic mute state untouched


def test_status_update_untouched_when_ss_off():
    m, sent = _mgr(ss_on=False)
    asyncio.run(m.calls._update_status(-100, _state()))
    assert sent[0].presentation_paused is True


def test_health_check_unpauses_when_telegram_shows_paused():
    m, _ = _mgr(ss_on=True)
    m.account_id = 7
    called = []

    async def unpause(chat_id, why="", force=False):
        called.append(chat_id)
        return True

    m._ss_unpause_presentation = unpause
    m.ss_busy = lambda cid: False
    parts = [{"uid": 7, "presenting": True, "pres_paused": True}]
    asyncio.run(m._ss_health_check(-100, parts))
    assert called == [-100]


def test_health_check_rejoins_at_most_twice_in_5_minutes():
    m, _ = _mgr(ss_on=True)
    m.account_id = 7
    m.ss_busy = lambda cid: False
    revives = []

    async def revive(chat_id, why="", rejoin=False):
        revives.append(rejoin)

    m._ss_revive = revive
    st = m.chats[-100]
    parts = [{"uid": 7, "presenting": False, "pres_paused": False}]

    async def run():
        for _ in range(6):
            st.ss_joined_at = 0.0
            st.ss_missing_since = time.monotonic() - 7
            await m._ss_health_check(-100, parts)
            await asyncio.sleep(0)

    asyncio.run(run())
    assert revives == [True, True]
