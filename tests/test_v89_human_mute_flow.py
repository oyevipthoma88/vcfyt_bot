"""v89: non-admin group "bot or human?" mute check.

Admin mutes the ID  -> hand raise after 2-3 s (not instantly).
Admin unmutes       -> mic ON once, no on/off blink, silent 10-15 s,
                       then the .playmute audio completely (if set) and
                       after it the main audio again FROM THE BEGINNING.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from config import Config
from helpers import vc_manager as vm
from helpers.vc_manager import UserVC

CID = -1001234567890


def _engine(monkeypatch, tmp_path, playmute=False):
    m = UserVC.__new__(UserVC)
    m.owner_id = 7
    m.account_id = 5000
    m.live_volume = 1000
    m.chats = {}
    m.mute_prefs = {}
    m._mute_prefs_save_task = None
    m._bg_tasks = set()
    m.edits = []
    m.streams = []
    m.silences = 0

    class Calls:
        def __init__(self):
            self.paused = 0

        async def pause(self, _cid):
            self.paused += 1

        async def unmute(self, _cid):
            return True

        async def play(self, *_a, **_k):
            m.silences += 1

    m.calls = Calls()

    async def edit_self(chat_id, muted=None, raise_hand=None, volume=None):
        m.edits.append({"muted": muted, "raise_hand": raise_hand, "t": asyncio.get_running_loop().time()})
        return True

    async def stream(chat_id, path, source_name, preprocessed=None, fast=None, start_at=0.0):
        st = m.state(chat_id)
        st.is_playing, st.is_paused = True, False
        st.current_file, st.source_name = path, source_name
        m.streams.append((path, float(start_at or 0.0)))

    m._edit_self = edit_self
    m._stream = stream
    m._raw_stream = lambda st, audio: ("raw", audio)
    m._silent_mic = lambda: "silence"
    m._preprocess_next = lambda cid: None
    m.save_mute_prefs = lambda cid: None

    main = tmp_path / "main.mp3"
    main.write_bytes(b"main")
    st = m.state(CID)
    st.is_playing, st.current_file, st.source_name = True, str(main), "Main"
    st.hand_raise = True
    st.mic_blink = False
    if playmute:
        pm = tmp_path / "pm.ogg"
        pm.write_bytes(b"pm")
        st.unmute_audio = str(pm)
        st.unmute_loop = 1
    return m, st, str(main)


def _fast_timings(monkeypatch, hand=0.2, resume=0.3):
    monkeypatch.setattr(Config, "HUMAN_HAND_DELAY_MIN", hand)
    monkeypatch.setattr(Config, "HUMAN_HAND_DELAY_MAX", hand)
    monkeypatch.setattr(Config, "HUMAN_RESUME_DELAY_MIN", resume)
    monkeypatch.setattr(Config, "HUMAN_RESUME_DELAY_MAX", resume)


def test_default_timings_are_2_3s_hand_and_10_15s_resume():
    hands = [vm.human_hand_delay() for _ in range(300)]
    resumes = [vm.human_resume_delay() for _ in range(300)]
    assert min(hands) >= 2.0 and max(hands) <= 3.0
    assert min(resumes) >= 10.0 and max(resumes) <= 15.0


def test_mic_blink_is_off_by_default():
    assert Config.MIC_BLINK_DEFAULT is False
    assert vm.ChatState().mic_blink is False


def test_admin_mute_raises_hand_after_delay_not_instantly(monkeypatch, tmp_path):
    _fast_timings(monkeypatch, hand=0.3)
    m, st, _ = _engine(monkeypatch, tmp_path)

    async def run():
        t0 = asyncio.get_running_loop().time()
        m.apply_mute_state(CID, True)
        await asyncio.sleep(0.1)
        assert m.calls.paused == 1, "audio must stop as soon as the admin mutes"
        assert not any(e["raise_hand"] for e in m.edits), "hand raised too early"
        await asyncio.sleep(0.4)
        raises = [e for e in m.edits if e["raise_hand"]]
        assert len(raises) == 1
        assert raises[0]["t"] - t0 >= 0.3
        assert st.hand_raised

    asyncio.run(run())


def test_unmute_without_playmute_restarts_main_audio_from_start(monkeypatch, tmp_path):
    _fast_timings(monkeypatch, hand=0.05, resume=2.0)
    m, st, main = _engine(monkeypatch, tmp_path)

    async def run():
        m.apply_mute_state(CID, True)
        await asyncio.sleep(0.2)
        m.edits.clear()
        m.apply_mute_state(CID, False)
        await asyncio.sleep(1.7)
        assert m.streams == [], "must stay silent for the human delay first"
        assert any(e["muted"] is False for e in m.edits), "mic must be switched ON"
        await asyncio.sleep(0.6)  # speaks right at the delay, no extra wait
        assert m.streams == [(main, 0.0)]
        assert not any(e["muted"] is True for e in m.edits), "mic blinked (muted again)"

    asyncio.run(run())


def test_unmute_with_playmute_plays_it_then_main_from_start(monkeypatch, tmp_path):
    _fast_timings(monkeypatch, hand=0.05, resume=0.3)
    m, st, main = _engine(monkeypatch, tmp_path, playmute=True)

    async def run():
        m.apply_mute_state(CID, True)
        await asyncio.sleep(0.2)
        m.apply_mute_state(CID, False)
        await asyncio.sleep(2.2)
        assert m.streams == [(st.unmute_audio, 0.0)], "PlayMute audio must play first"
        assert st.playing_unmute
        # announcement finished -> main audio from the very beginning
        assert await m._after_unmute_audio(CID)
        assert m.streams[-1] == (main, 0.0)
        assert not st.playing_unmute

    asyncio.run(run())


def test_remute_during_wait_cancels_resume(monkeypatch, tmp_path):
    _fast_timings(monkeypatch, hand=0.05, resume=0.5)
    m, st, _ = _engine(monkeypatch, tmp_path)

    async def run():
        m.apply_mute_state(CID, True)
        await asyncio.sleep(0.2)
        m.apply_mute_state(CID, False)
        await asyncio.sleep(0.1)
        m.apply_mute_state(CID, True)
        await asyncio.sleep(2.5)
        assert m.streams == []

    asyncio.run(run())


def test_only_muted_and_not_self_unmutable_is_admin_mute():
    raw = lambda muted, csu: SimpleNamespace(muted=muted, can_self_unmute=csu)
    assert vm.is_admin_mute(raw(True, False))
    assert not vm.is_admin_mute(raw(True, True))      # admin allowed speaking
    assert not vm.is_admin_mute(raw(False, True))     # speaking
    ptg = lambda muted, by_admin: SimpleNamespace(muted=muted, muted_by_admin=by_admin)
    assert vm.is_admin_mute(ptg(True, True))
    assert not vm.is_admin_mute(ptg(False, True))     # pytgcalls false positive


def test_old_saved_mic_blink_is_dropped(monkeypatch):
    m = UserVC.__new__(UserVC)
    m.owner_id = 9
    m.mute_prefs = {}

    class FakeDB:
        async def get_app_value(self, _k):
            return json.dumps({str(CID): {"mic_blink": True, "hand_raise": True}})

    monkeypatch.setattr(vm, "_db", lambda: FakeDB())
    asyncio.run(m.load_mute_prefs())
    assert "mic_blink" not in m.mute_prefs[CID]
    assert m.mute_prefs[CID]["hand_raise"] is True
