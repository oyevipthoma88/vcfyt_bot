"""v32 root fixes — run standalone (other test files stub ntgcalls/pytgcalls):

    python -m pytest tests/test_v32_fixes.py

Bug 1  200 % boost: main ID must learn the spare's peer and really send 200 %.
Bug 2  SS on/off: the running mic reader must be kept open (no audio restart).
Bug 3  Live mic target: a new target moves the running stream; an old
       target's link is never used for a new target.
"""
import asyncio
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ntgcalls = pytest.importorskip("ntgcalls")
pytest.importorskip("pytgcalls")
if getattr(ntgcalls, "__file__", None) is None:  # stubbed by another test file
    pytest.skip("real ntgcalls required (run this file on its own)", allow_module_level=True)

from pyrogram import raw  # noqa: E402

from helpers import vc_manager as vm  # noqa: E402
import helpers.mic_target as pb  # noqa: E402


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# --------------------------------------------------------------------- Bug 2
def test_real_ntgcalls_hides_media_source():
    """The root cause: reading desc.media_source raises on ntgcalls objects."""
    d = ntgcalls.AudioDescription(media_source=ntgcalls.MediaSource.SHELL, sample_rate=48000,
                                  channel_count=2, input="cat /tmp/x", keep_open=False)
    assert not hasattr(d, "media_source")


def test_ss_toggle_copy_keeps_mic_open(monkeypatch):
    from pytgcalls.methods.utilities.stream_params import StreamParams
    from pytgcalls.types.raw import AudioParameters, AudioStream

    cmd = "cat /tmp/livemic_proc_v32test"
    live = AudioStream(ntgcalls.MediaSource.SHELL, cmd, AudioParameters(48000, 2))
    running = StreamParams._parse_media_description(live)   # what play() sent

    made = {}

    def fake_desc(**kw):
        made.update(kw)
        return types.SimpleNamespace(**kw)

    monkeypatch.setattr(ntgcalls, "AudioDescription", fake_desc)
    copy = vm._keep_open_copy(running)
    assert made["keep_open"] is True
    assert made["media_source"] == ntgcalls.MediaSource.SHELL
    assert made["input"] == cmd
    assert (made["sample_rate"], made["channel_count"]) == (48000, 2)
    assert copy is not running


def test_unknown_shell_input_defaults_to_shell():
    d = ntgcalls.AudioDescription(media_source=ntgcalls.MediaSource.SHELL, sample_rate=48000,
                                  channel_count=2, input="ffmpeg -i never_seen_v32 pipe:1",
                                  keep_open=False)
    assert vm._media_source_for(d) == ntgcalls.MediaSource.SHELL


# --------------------------------------------------------------------- Bug 3
class FakeDB:
    def __init__(self):
        self.d = {}

    async def get_app_value(self, k):
        return self.d.get(k)

    async def set_app_value(self, k, v):
        self.d[k] = v


def test_old_target_link_is_never_reused_for_new_target(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(pb, "db", db)
    run(pb.save_target(7, -1001111111111, "https://t.me/+OLDLINK"))
    run(pb.save_target(7, -1002222222222, None))          # new target by ID
    assert run(pb.target_ref(7, -1002222222222)) == (None, False)
    run(pb.save_target(7, -1003333333333, "https://t.me/+NEWLINK"))
    assert run(pb.target_ref(7, -1003333333333)) == ("https://t.me/+NEWLINK", True)
    assert run(pb.target_ref(7, -1001111111111)) == (None, False)


def test_legacy_link_is_untrusted(monkeypatch):
    db = FakeDB()
    db.d["mic_chat_ref_7"] = "https://t.me/+LEGACY"        # saved before v32
    monkeypatch.setattr(pb, "db", db)
    assert run(pb.target_ref(7, -1002222222222)) == ("https://t.me/+LEGACY", False)


class _Calls:
    def __init__(self):
        self.left = []

    async def leave_call(self, cid):
        self.left.append(cid)

    async def change_volume_call(self, cid, v):
        return None


class _Relay:
    account_id = 555

    def __init__(self):
        self.calls = _Calls()
        self.chats = {-1001111111111: object()}
        self.client = None

    def unguard_volume(self, *a):
        return None


class _St:
    pass


class _Uvc:
    client = None

    def __init__(self):
        self.chats = {}

    def state(self, cid):
        return self.chats.setdefault(cid, _St())


class _Session:
    def __init__(self, chat):
        self.chat_id = chat
        self._closed = False
        self._admin_boost = True
        self._health_warned = {"x"}
        self.ffmpeg_proc = None
        self.played_in = []
        self.started = 0

    async def _play_stream(self):
        self.played_in.append(self.chat_id)

    def _mark_started(self):
        self.started += 1


# --------------------------------------------------------------------- Bug 1
def test_main_id_learns_unknown_spare_and_sets_200_percent(monkeypatch):
    """in-memory session: spare's peer unknown until the VC participant list
    is fetched -> volume must still be set to 20000 (= 200 %)."""
    from helpers import peer_guard
    peer_guard._USER_PEER_MISS.clear()
    SPARE, CHAT = 4242, -1005555555555
    chan_peer = raw.types.InputPeerChannel(channel_id=5555555555, access_hash=1)
    spare_peer = raw.types.InputPeerUser(user_id=SPARE, access_hash=99)
    edits = []

    class Client:
        is_connected = True

        def __init__(self):
            self.known = set()

        async def resolve_peer(self, pid):
            if pid == CHAT:
                return chan_peer
            if pid == SPARE and SPARE in self.known:
                return spare_peer
            raise peer_guard.PeerIdInvalid()

        async def get_chat(self, pid):
            raise peer_guard.PeerIdInvalid()

        async def invoke(self, q):
            if isinstance(q, raw.functions.channels.GetFullChannel):
                return types.SimpleNamespace(full_chat=types.SimpleNamespace(call="CALL"))
            if isinstance(q, raw.functions.phone.GetGroupParticipants):
                self.known.add(SPARE)          # pyrogram invoke() -> fetch_peers
                return types.SimpleNamespace(
                    users=[types.SimpleNamespace(id=SPARE, min=False)], next_offset="")
            if type(q).__name__ == "EditGroupCallParticipant":
                edits.append(q)
                return None
            raise AssertionError(type(q))

    uvc = vm.UserVC.__new__(vm.UserVC)
    uvc.client = Client()
    uvc.account_id = 1
    uvc.owner_id = 1
    uvc.chats = {}

    async def call_input(cid):
        return "CALL"

    uvc._call_input = call_input
    ok = run(uvc.set_participant_volume(CHAT, SPARE, 20000, quiet=True))
    assert ok is True
    assert len(edits) == 1
    assert edits[0].volume == 20000
    assert edits[0].participant == spare_peer
