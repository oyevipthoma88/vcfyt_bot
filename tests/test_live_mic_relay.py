"""End-to-end live mic relay test (no Telegram, no network account needed).

Fakes py-tgcalls, MongoDB and the user session, then drives the real aiohttp
live-mic server with a real WebSocket client:

  1. mic ON  -> PCM streams in, py-tgcalls gets exactly one play() call,
                and real (loud) audio comes out of the FFmpeg pipeline
  2. socket drops -> the relay survives the reconnect grace window
  3. reconnect    -> audio keeps flowing WITHOUT a new play() call
  4. duplicate tab -> rejected, the live session is left alone
  5. mic OFF  -> everything is cleaned up and the token is burned

Run:  python tests/test_live_mic_relay.py
"""

import asyncio
import math
import os
import re
import struct
import subprocess
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RATE = 48000
FAILURES = []


def check(name, ok, extra=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILURES.append(name)


# --- stub py-tgcalls / ntgcalls ------------------------------------------

ntgcalls = types.ModuleType("ntgcalls")


class MediaSource:
    SHELL = "shell"


ntgcalls.MediaSource = MediaSource
sys.modules["ntgcalls"] = ntgcalls

pytgcalls = types.ModuleType("pytgcalls")
pytgcalls_types = types.ModuleType("pytgcalls.types")
pytgcalls_raw = types.ModuleType("pytgcalls.types.raw")


class AudioParameters:
    def __init__(self, rate, channels):
        self.rate, self.channels = rate, channels


class AudioStream:
    def __init__(self, source, path, parameters):
        self.source, self.path, self.parameters = source, path, parameters


class Stream:
    def __init__(self, microphone=None):
        self.microphone = microphone


class GroupCallConfig:
    def __init__(self, auto_start=False):
        self.auto_start = auto_start


pytgcalls_raw.AudioParameters = AudioParameters
pytgcalls_raw.AudioStream = AudioStream
pytgcalls_raw.Stream = Stream
pytgcalls_types.GroupCallConfig = GroupCallConfig
pytgcalls_types.raw = pytgcalls_raw
pytgcalls.types = pytgcalls_types
sys.modules["pytgcalls"] = pytgcalls
sys.modules["pytgcalls.types"] = pytgcalls_types
sys.modules["pytgcalls.types.raw"] = pytgcalls_raw

# --- stub database --------------------------------------------------------

database = types.ModuleType("helpers.database")


class FakeDB:
    def __init__(self):
        self.values = {}
        self.settings = {"volume": 1000, "bass": 12, "treble": 90,
                         "gain": 180, "boost": 10, "echo": 0, "echo_level": 2}

    async def get_app_value(self, key):
        return self.values.get(key)

    async def set_app_value(self, key, value):
        self.values[key] = value

    async def delete_app_value(self, key):
        self.values.pop(key, None)

    async def get_settings(self, _uid):
        return dict(self.settings)

    async def save_settings(self, _uid, **kw):
        self.settings.update(kw)


database.db = FakeDB()
sys.modules["helpers.database"] = database

# --- stub the userbot VC session -----------------------------------------


class FakeState:
    def __init__(self):
        self.is_playing = False
        self.is_paused = False
        self.mic_enabled = False
        self.mic_boost_user_id = None
        self.source_name = "—"
        self.live_relay = False
        self.recent_live_mic = False
        self.live_volume = 20000


class FakeCalls:
    def __init__(self, outfile):
        self.play_calls = []
        self.outfile = outfile
        self.readers = []

    async def play(self, chat_id, stream, config=None):
        self.play_calls.append(chat_id)
        fifo = stream.microphone.path.split(" ", 1)[1]
        self.readers.append(await asyncio.create_subprocess_shell(
            f"cat {fifo} >> {self.outfile}"))

    async def resume(self, _chat_id):
        return True

    async def unmute(self, _chat_id):
        return True

    async def leave_call(self, chat_id):
        self.left = getattr(self, "left", [])
        self.left.append(chat_id)
        return True


class FakeUVC:
    def __init__(self, outfile, account_id=777):
        self.account_id = account_id
        self.account_name = f"acct{account_id}"
        self.calls = FakeCalls(outfile)
        self.chats = {}
        self.left = []

    def state(self, chat_id):
        return self.chats.setdefault(chat_id, FakeState())

    async def _call_input(self, _chat_id):
        return object()

    async def set_participant_volume(self, *a, **kw):
        return True

    async def _peer(self, _chat_id):
        return object()


vc_manager = types.ModuleType("helpers.vc_manager")


class FakeSessionManager:
    def __init__(self):
        self.uvc = None
        self.relay = None

    async def get(self, _user_id):
        return self.uvc

    async def get_relay(self, _user_id):
        return self.relay


vc_manager.session_manager = FakeSessionManager()
sys.modules["helpers.vc_manager"] = vc_manager

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

import helpers.live_mic as lm  # noqa: E402

USER_ID = 42
CHAT_ID = -1001234567890
OUT = "/tmp/livemic_test_out.raw"


def pcm_frames(seconds=2.0, amp=0.05, ms=20, phase=0):
    n = int(RATE * ms / 1000)
    for f in range(int(seconds * 1000 / ms)):
        buf = bytearray()
        for k in range(n):
            i = phase + f * n + k
            buf += struct.pack("<h", int(amp * 32767 * math.sin(2 * math.pi * 440 * i / RATE)))
        yield bytes(buf)


def mean_db(path):
    p = subprocess.run(["ffmpeg", "-hide_banner", "-f", "s16le", "-ar", "48000",
                        "-ac", "2", "-i", path, "-af", "volumedetect",
                        "-f", "null", "-"], capture_output=True, text=True)
    m = re.search(r"mean_volume: (-?[\d.]+) dB", p.stderr)
    return float(m.group(1)) if m else 1.0


async def stream_pcm(sock, seconds, phase=0):
    for frame in pcm_frames(seconds, phase=phase):
        await sock.send_bytes(frame)
        await asyncio.sleep(0.02)


async def raw_force_unmute_probe():
    """Verify raw force-unmute still runs after a successful wrapper call."""
    modules = {}
    for name in ("pyrogram", "pyrogram.raw",
                 "pyrogram.raw.functions",
                 "pyrogram.raw.functions.phone"):
        mod = types.ModuleType(name)
        mod.__path__ = []
        modules[name] = mod
    class EditGroupCallParticipant:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
    modules["pyrogram.raw.functions.phone"].EditGroupCallParticipant = EditGroupCallParticipant
    missing = object()
    previous = {name: sys.modules.get(name, missing) for name in modules}
    sys.modules.update(modules)
    class FakeClient:
        def __init__(self):
            self.requests = []
        async def resolve_peer(self, user_id):
            return user_id
        async def invoke(self, request):
            self.requests.append(request)
    try:
        relay = FakeUVC(OUT)
        relay.client = FakeClient()
        session = object.__new__(lm.LiveMicSession)
        session.relay = relay
        session.chat_id = CHAT_ID
        result = await session._unmute_self()
        request = relay.client.requests[0] if relay.client.requests else None
        return result, request
    finally:
        for name, old in previous.items():
            if old is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


async def main():
    if os.path.exists(OUT):
        os.unlink(OUT)
    open(OUT, "wb").close()

    raw_ok, raw_request = await raw_force_unmute_probe()
    check("raw unmute is forced after wrapper success",
          raw_ok and raw_request is not None
          and raw_request.kwargs.get("muted") is False
          and raw_request.kwargs.get("volume") == 20000)

    lm.RECONNECT_GRACE_SECONDS = 10.0
    uvc = FakeUVC(OUT)
    vc_manager.session_manager.uvc = uvc

    app = lm.create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 8799)
    await site.start()
    base = "http://127.0.0.1:8799"

    token = await lm.generate_token(USER_ID, CHAT_ID)
    session = aiohttp.ClientSession()
    try:
        # 1 --- first connection ------------------------------------------
        sock = await session.ws_connect(f"{base}/ws/mic?token={token}")
        assert (await sock.receive()).data == "ready"
        await sock.receive()  # settings
        await sock.send_str(f"rate:{RATE}")
        await stream_pcm(sock, 2.0)

        live = lm.get_session(USER_ID)
        check("session is live", live is not None and not live._closed)
        check("py-tgcalls play() called once", uvc.calls.play_calls == [CHAT_ID],
              str(uvc.calls.play_calls))
        check("chat marked as live mic", uvc.chats[CHAT_ID].mic_enabled)
        check("PCM reached the server", live._received_bytes > 100000,
              f"{live._received_bytes} bytes")

        # 2 --- abrupt socket drop ----------------------------------------
        await sock.close()
        await asyncio.sleep(1.5)
        kept = lm.get_session(USER_ID)
        check("relay survives a dropped browser", kept is live and not live._closed)
        check("no live socket reported", not lm.has_live_socket(USER_ID))

        # 3 --- reconnect with the same link -------------------------------
        sock2 = await session.ws_connect(f"{base}/ws/mic?token={token}")
        assert (await sock2.receive()).data == "ready"
        await sock2.receive()
        await stream_pcm(sock2, 1.0, phase=96000)
        check("same session resumed", lm.get_session(USER_ID) is live)
        check("no extra play() on reconnect", uvc.calls.play_calls == [CHAT_ID],
              str(uvc.calls.play_calls))
        check("audio kept flowing", live._received_bytes > 240000,
              f"{live._received_bytes} bytes")

        # A complete buffer drain must be counted so the jitter target adapts
        # after mobile WebView/network stalls instead of repeating the same chop.
        underruns_before = live._underruns
        await asyncio.sleep(0.45)
        await stream_pcm(sock2, 0.8, phase=144000)
        check("network gap grows adaptive jitter buffer",
              live._underruns > underruns_before
              and live._jitter_target_ms > lm.JITTER_TARGET_MS,
              f"underruns={live._underruns}, target={live._jitter_target_ms}ms")

        # 4 --- duplicate tab ----------------------------------------------
        sock3 = await session.ws_connect(f"{base}/ws/mic?token={token}")
        msg = await sock3.receive()
        check("duplicate tab rejected", isinstance(msg.data, str)
              and msg.data.startswith("error:already_active"), str(msg.data)[:60])
        await asyncio.sleep(0.5)
        check("duplicate did not kill the relay",
              lm.get_session(USER_ID) is live and not live._closed)
        await sock3.close()

        # 5 --- settings change is applied without dropping the VC ---------
        await sock2.send_str('settings:{"bass": 40}')
        reply = await sock2.receive()
        check("settings applied", isinstance(reply.data, str)
              and reply.data.startswith("settings:"), str(reply.data)[:40])
        await stream_pcm(sock2, 0.6, phase=192000)
        check("still same session after settings change",
              lm.get_session(USER_ID) is live and not live._closed)

        # 6 --- .mic off ----------------------------------------------------
        await lm.stop_session(USER_ID)
        await asyncio.sleep(0.5)
        check("session stopped", lm.get_session(USER_ID) is None)
        check("token burned",
              await database.db.get_app_value(f"live_mic_token_{USER_ID}") is None)
        check("mic flag cleared", not uvc.chats[CHAT_ID].mic_enabled)
        await sock2.close()

        for reader in uvc.calls.readers:
            try:
                await asyncio.wait_for(reader.wait(), 5)
            except asyncio.TimeoutError:
                reader.kill()

        size = os.path.getsize(OUT)
        seconds = size / (48000 * 2 * 2)
        level = mean_db(OUT)
        check("audio delivered to the voice chat", seconds > 2.0,
              f"{seconds:.1f}s")
        check("audio is loud (not silence)", level > -25.0, f"mean {level} dB")
    finally:
        await session.close()
        await lm.stop_server()
        await runner.cleanup()

    print("\n" + ("ALL LIVE MIC CHECKS PASSED" if not FAILURES
                  else f"FAILED: {FAILURES}"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
