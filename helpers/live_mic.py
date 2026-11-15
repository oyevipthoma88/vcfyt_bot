"""Live mic relay: browser captures mic → WebSocket → FIFO → FFmpeg → py-tgcalls → VC.

The user opens a web page on their phone, grants mic permission, and their live
voice is processed with the same FFmpeg effects (bass, echo, gain, boost) used
for file playback.  The processed audio is streamed into the Telegram voice chat
through py-tgcalls' MediaStream.

Pipeline:
  browser mic → getUserMedia → AudioWorklet (16-bit PCM 48kHz mono)
  → WebSocket → server writes to raw FIFO
  → FFmpeg reads raw FIFO, applies filter chain, writes WAV to proc FIFO
  → py-tgcalls reads proc FIFO as MediaStream → plays into VC
"""

import array
import asyncio
import errno
import json
import logging
import os
import secrets
import shlex
import tempfile
import time
from typing import Dict, Optional

from aiohttp import web, WSMsgType


def _check_ghost(session, chat_id: int) -> bool:
    try:
        relay = getattr(session, "relay", None)
        st = relay.chats.get(chat_id) if (relay and hasattr(relay, "chats")) else None
        if st and getattr(st, "ghost_mute", False):
            return True
        from plugins.ghost_mic import is_ghost_chat
        return bool(is_ghost_chat(chat_id))
    except Exception:
        return False

logger = logging.getLogger("vcbot.live_mic")

_sessions: Dict[int, "LiveMicSession"] = {}
_create_locks: Dict[int, asyncio.Lock] = {}

# Sessions whose browser socket dropped wait here for a reconnect instead of
# being torn down at once — the userbot stays in the voice chat meanwhile.
_grace_tasks: Dict[int, "asyncio.Task"] = {}
RECONNECT_GRACE_SECONDS = 240.0

# The relay must hand FFmpeg exactly as much audio as wall-clock time passes.
# Writing browser bursts straight through (and topping them up with extra
# silence) pushed more than real-time into the pipeline, so latency grew until
# Telegram dropped the userbot from the voice chat.
FRAME_MS = 20
JITTER_MAX_MS = 500
# Playout starts only once this much audio is queued, and after an underrun
# we re-buffer back to it.  Without this every tiny network hiccup inserted
# 20 ms holes of silence mid-word = the choppy "atak atak" voice.
JITTER_TARGET_MS = 60
# Each underrun grows the prebuffer (adaptive) so a user on a bad network
# stutters once, not every few seconds.
JITTER_TARGET_MAX_MS = 240
# Beyond this, latency is creeping up: drop 1 sample per ~100 (inaudible).
JITTER_HIGH_MS = 200


# Loudness comes from the levelling + limiter stages, not from piling on
# pre-amp and turbo; the old 22 dB pregain + 4 dB turbo only squashed the
# voice flat before it ever reached the voice chat.
FIXED_BEST = {"volume": 3000, "gain": 500, "boost": 10, "bass": 0,
              "treble": 120, "pregain": 200, "turbo": 24, "clarity": 35,
              "echo": 0, "echo_level": 0, "loud": 4000}


class LiveMicSession:
    """Manages a single user's live mic relay: WebSocket, FIFOs, FFmpeg, py-tgcalls."""

    def __init__(self, user_id: int, uvc, chat_id: int, settings: dict, relay=None):
        self.user_id = user_id
        self.uvc = uvc
        # `uvc` = the user's own account (owns chat state / UI).
        # `relay` = the spare account that actually joins the VC and streams.
        # Telegram allows one group-call join per account, so when relay is the
        # same account the user is listening with, one of the two gets kicked.
        self.relay = relay or uvc
        # DB / GAIN / CLARITY sliders from the mic page (override FIXED_BEST).
        self.live_overrides = {}
        self.shared_account = self.relay is uvc
        self.tg_muted = False
        self.chat_id = chat_id
        self.settings = dict(settings or {})

        self.ws: Optional[web.WebSocketResponse] = None
        self.raw_fifo: Optional[str] = None
        self.proc_fifo: Optional[str] = None
        self._raw_fd: Optional[int] = None
        self.ffmpeg_proc: Optional[asyncio.subprocess.Process] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._closed = False
        self._restarting = False
        self.created_vc = False
        self._received_bytes = 0
        self._last_audio_log = 0.0
        # VC HEALTH (record_20): last time real voice (not silence) arrived
        # from the phone, and the background checker that tells the user WHY
        # nobody hears them (phone mic silenced, muted by admin, kicked out).
        self._in_voice_at = time.monotonic()
        self._in_peak = 0
        self._health_task = None
        self._health_warned = set()
        self._first_pcm = asyncio.Event()
        self._pipeline_ready = asyncio.Event()
        self._proc_keeper_fd: Optional[int] = None
        self._voice_gate = None
        self._pacer_task: Optional[asyncio.Task] = None
        self._buf = bytearray()
        self._pending = b""
        self._pipeline_lock = asyncio.Lock()
        self.input_rate = 48000
        self._last_pcm_at = 0.0
        self._dropped_bytes = 0
        self._ffmpeg_restarts = 0
        # True only while a group ADMIN has confirmed the relay at 200 %
        # volume for every listener.  Decides the output ceiling (see
        # build_live_mic_filter): -6.5 dBFS with the x2 boost, -1 without.
        self._admin_boost = False
        self._pipeline_ceiling = None
        self._started = False
        self.token_secret: Optional[str] = None
        self.started_at = time.monotonic()
        self._underruns = 0
        self._jitter_target_ms = JITTER_TARGET_MS
        self._last_health_log = time.monotonic()
        # v90: per-user sound mode (loudest|phone|bass, same as playback)
        # and the browser screen / camera share feed (helpers.web_studio).
        self.mic_mode: Optional[str] = None
        self.screen_feed = None
        self._chat_cache = None
        self._vc_cache = None

    def _chan_log(self, event: str, details: Optional[dict] = None):
        """Live mic event -> console + LOG_CHANNEL (never raises)."""
        try:
            from helpers.logger_channel import log_live_mic, _fire
            _fire(log_live_mic(event, self.user_id, self.chat_id, details or {}))
        except Exception as exc:
            logger.debug("live mic channel log failed: %s", exc)

    def _stats(self) -> dict:
        return {
            "Duration": f"{int(time.monotonic() - self.started_at)}s",
            "Received": f"{self._received_bytes // 1024} KB",
            "Input rate": f"{self.input_rate} Hz",
            "Underruns": self._underruns,
            "Jitter target": f"{self._jitter_target_ms}ms",
            "Dropped": f"{self._dropped_bytes // 1024} KB",
            "FFmpeg restarts": self._ffmpeg_restarts,
        }

    # --- settings ---

    def public_settings(self) -> dict:
        s = self.settings
        return {
            "volume": int(s.get("volume", 1000) or 0),
            "bass": int(s.get("bass", 10) or 0),
            "treble": int(s.get("treble", 105) or 0),
            "gain": int(s.get("gain", 200) or 0),
            "boost": int(s.get("boost", 10) or 0),
            "echo": 1 if s.get("echo") else 0,
            "echo_level": int(s.get("echo_level", 2) or 0),
            "pregain": int(s.get("pregain", 80)),
            "turbo": int(s.get("turbo", 12) or 12),
            "clarity": int(s.get("clarity", 14) if s.get("clarity") is not None else 14),
            "loud": int(s.get("loud") or 120),
            "crunch": int(s.get("crunch", 0) or 0),
            "mode": self.current_mode(),
            "screen": (self.screen_feed.kind if self.screen_feed is not None
                       and self.screen_feed.alive else ""),
        }

    def current_mode(self) -> str:
        try:
            from helpers import live_mic_v79 as v79
            return v79.normalize_mode(self.mic_mode or "") or v79.get_mode()
        except Exception:
            return "loudest"

    @staticmethod
    def sanitize_settings(data: dict) -> dict:
        limits = {
            "volume": (0, 3000), "bass": (0, 100), "treble": (0, 120),
            "gain": (0, 500), "boost": (0, 10), "echo_level": (0, 10),
            "pregain": (0, 200), "turbo": (0, 24), "clarity": (0, 35),
            "loud": (0, 200), "crunch": (0, 200),
        }
        clean = {}
        for key, (low, high) in limits.items():
            if key in data:
                try:
                    clean[key] = max(low, min(high, int(float(data[key]))))
                except (TypeError, ValueError):
                    pass
        if "echo" in data:
            clean["echo"] = 1 if data["echo"] in (1, True, "1", "true", "on") else 0
        return clean

    def _build_filter(self) -> str:
        # Fixed chain — no sliders, presets or saved settings can change it.
        from helpers.audio_processor import build_live_mic_filter
        self._pipeline_ceiling = self._ceiling_db()
        loud = int(self.settings.get("loud") or 120)
        try:
            return build_live_mic_filter(self._pipeline_ceiling, loud=loud,
                                         crunch=int(self.settings.get("crunch", 0) or 0),
                                         mode=self.current_mode())
        except TypeError:   # older patched builder without `mode`
            return build_live_mic_filter(self._pipeline_ceiling, loud=loud)

    def _ceiling_db(self) -> float:
        """Loudest clip-safe output peak for the current Telegram volume.

        ROOT FIX: the -6.5 dB headroom for Telegram's x2 (200 %) boost used
        to be applied even when no admin boost reached listeners — then the
        mic was simply 6.5 dB too quiet.  Now headroom is only kept while an
        admin-set 200 % is confirmed.
        """
        def _env(name, default):
            try:
                return max(-12.0, min(-0.5, float(os.environ.get(name, "") or default)))
            except (TypeError, ValueError):
                return default
        if self._admin_boost:
            return _env("LIVE_MIC_CEILING_DB", -6.5)
        return _env("LIVE_MIC_OPEN_CEILING_DB", -0.3)

    async def _apply_ceiling(self) -> None:
        """Swap FFmpeg (VC stream stays alive) when the needed ceiling changed."""
        if self._closed or self.ffmpeg_proc is None:
            return
        if self._pipeline_ceiling is not None and abs(self._ceiling_db() - self._pipeline_ceiling) < 0.05:
            return
        saved = self._ffmpeg_restarts
        self._ffmpeg_restarts = 0
        try:
            await self._restart_ffmpeg(max_attempts=1)
        finally:
            self._ffmpeg_restarts = saved

    # --- pipeline ---

    async def _start_pipeline(self, keep_proc_fifo: bool = False):
        """Create FIFOs, launch FFmpeg and open the raw write end.

        keep_proc_fifo reuses the FIFO py-tgcalls is already reading, so a
        settings change can swap FFmpeg without touching the live VC stream.
        """
        loop = asyncio.get_running_loop()

        rate = int(self.input_rate or 48000)
        if rate < 8000 or rate > 192000:
            rate = 48000

        self.raw_fifo = tempfile.mktemp(suffix="_raw", prefix="livemic_raw_")
        os.mkfifo(self.raw_fifo)
        if not (keep_proc_fifo and self.proc_fifo and os.path.exists(self.proc_fifo)):
            self.proc_fifo = tempfile.mktemp(suffix="_proc", prefix="livemic_proc_")
            os.mkfifo(self.proc_fifo)

        # Keep a permanent handle on the processed FIFO (O_RDWR never blocks and
        # never consumes data).  Without it FFmpeg blocks on opening its output
        # until py-tgcalls' `cat` shows up — which stalls the whole relay — and
        # dies with EPIPE the moment `cat` goes away during a settings change.
        if self._proc_keeper_fd is None:
            try:
                self._proc_keeper_fd = os.open(self.proc_fifo, os.O_RDWR | os.O_NONBLOCK)
            except OSError as exc:
                logger.warning("proc FIFO keeper open failed: %s", exc)
                self._proc_keeper_fd = None

        # Shrink the processed-FIFO kernel buffer: default 64 KB = ~340 ms of
        # 48 kHz stereo audio sitting in the pipe = the "slow" voice delay.
        if self._proc_keeper_fd is not None:
            try:
                import fcntl
                fcntl.fcntl(self._proc_keeper_fd, 1031, 4096)  # F_SETPIPE_SZ
            except Exception:
                pass

        ffmpeg_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-y",
            # Keep probe minimal but non-zero — probesize=16 / analyzeduration=0
            # made FFmpeg's internal analyser overflow (ERANGE) and die instantly,
            # which then caused the 15 s FIFO-open timeout.
            "-fflags", "nobuffer", "-flags", "low_delay",
            "-analyzeduration", "1000", "-probesize", "32",
            "-f", "s16le", "-ar", str(rate), "-ac", "1",
            "-thread_queue_size", "64",
            "-i", self.raw_fifo,
            "-threads", "1",
            "-af", self._build_filter(),
            "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le",
            "-f", "s16le", "-flush_packets", "1",
            self.proc_fifo,
        ]

        try:
            self.ffmpeg_proc = await asyncio.create_subprocess_exec(
                *ffmpeg_cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "FFmpeg server par install nahi hai — live mic kaam nahi karega. "
                "Heroku par ffmpeg buildpack add karein."
            ) from exc

        async def _drain_stderr():
            proc = self.ffmpeg_proc
            if not proc or not proc.stderr:
                return
            try:
                while True:
                    chunk = await proc.stderr.read(4096)
                    if not chunk:
                        break
                    logger.warning("FFmpeg stderr for user %s: %s",
                                   self.user_id, chunk.decode(errors="replace").strip())
            except Exception:
                pass
        self._stderr_task = asyncio.create_task(_drain_stderr())

        # If FFmpeg already exited (bad filter, bad probe params, etc.) the FIFO
        # write-end open will block forever — detect that early and surface the
        # real error instead of a 15 s timeout.
        if self.ffmpeg_proc and self.ffmpeg_proc.returncode is not None:
            stderr_tail = ""
            if self.ffmpeg_proc.stderr:
                try:
                    stderr_tail = (await self.ffmpeg_proc.stderr.read(4096)).decode(errors="replace").strip()
                except Exception:
                    pass
            raise RuntimeError(
                f"FFmpeg turant crash ho gaya (exit {self.ffmpeg_proc.returncode}). "
                f"Stderr: {stderr_tail[-300:]}"
            )

        fd_holder = []

        def _open_raw():
            fd_holder.append(os.open(self.raw_fifo, os.O_WRONLY))

        try:
            await asyncio.wait_for(loop.run_in_executor(None, _open_raw), timeout=15.0)
        except asyncio.TimeoutError:
            # Check whether FFmpeg died while we were waiting.
            if self.ffmpeg_proc and self.ffmpeg_proc.returncode is not None:
                stderr_tail = ""
                if self.ffmpeg_proc.stderr:
                    try:
                        stderr_tail = (await self.ffmpeg_proc.stderr.read(4096)).decode(errors="replace").strip()
                    except Exception:
                        pass
                raise RuntimeError(
                    f"FFmpeg crash ho gaya (exit {self.ffmpeg_proc.returncode}). "
                    f"Stderr: {stderr_tail[-300:]}"
                )
            raise RuntimeError(
                "FFmpeg FIFO open timeout — FFmpeg shuru nahi hua. "
                "Check karein ki FFmpeg installed hai aur VC on hai."
            )
        self._raw_fd = fd_holder[0] if fd_holder else None

        # Non-blocking writes: a blocking FIFO write inside the event loop
        # freezes the whole bot (Telegram pings stop → the userbot gets dropped
        # from the voice chat).  Late mic frames are dropped instead.
        if self._raw_fd is not None:
            try:
                import fcntl
                flags = fcntl.fcntl(self._raw_fd, fcntl.F_GETFL)
                fcntl.fcntl(self._raw_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            except Exception as exc:
                logger.warning("raw FIFO non-blocking mode failed: %s", exc)

        self._last_pcm_at = time.monotonic()
        self._pipeline_ready.set()
        if self._pacer_task is None or self._pacer_task.done():
            self._pacer_task = asyncio.create_task(self._feed_loop())

    async def ensure_pipeline(self, rate: Optional[int] = None):
        """Start the FFmpeg pipeline once, using the browser's real sample rate."""
        async with self._pipeline_lock:
            if self.ffmpeg_proc is not None or self._closed:
                return
            if rate:
                self.input_rate = int(rate)
            await self._start_pipeline()

    @property
    def _frame_bytes(self) -> int:
        return int(self.input_rate or 48000) // (1000 // FRAME_MS) * 2

    def queue_pcm(self, data: bytes) -> None:
        """Buffer browser PCM; the feeder drains it at real-time pace."""
        if self._closed or not data:
            return
        self._buf.extend(data)
        max_bytes = self._frame_bytes * (JITTER_MAX_MS // FRAME_MS)
        if len(self._buf) > max_bytes:
            # Too far behind (network burst / reconnect flush): throw away the
            # oldest audio instead of letting latency grow without bound.
            drop = (len(self._buf) - max_bytes) & ~1
            del self._buf[:drop]
            self._dropped_bytes += drop

    async def _feed_loop(self):
        """Adaptive jitter buffer + steady 20 ms real-time pacer.

        * Prebuffer JITTER_TARGET_MS before playing and after any underrun so
          the voice plays smoothly instead of being chopped into bits.
        * If asyncio wakes up late, write the missed frames (catch-up) instead
          of silently resetting the clock — resetting starved py-tgcalls and
          was a second source of stutter.
        * If the queue grows too long, trim it gently to keep latency low.
        """
        frame = FRAME_MS / 1000.0
        next_at = time.monotonic()
        playing = False
        target_ms = self._jitter_target_ms
        trim_tick = 0
        try:
            while not self._closed:
                next_at += frame
                delay = next_at - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                elif delay < -0.2:
                    next_at = time.monotonic()  # way behind: resync
                if self._raw_fd is None or self._restarting:
                    continue
                size = self._frame_bytes
                target = size * (target_ms // FRAME_MS)
                high = size * (max(JITTER_HIGH_MS, target_ms + 300) // FRAME_MS)
                if not playing and len(self._buf) >= target:
                    playing = True
                trim_tick += 1
                if playing and len(self._buf) > high and trim_tick >= 5:
                    # v40: cutting 2 ms out of the MIDDLE of a word every
                    # 200 ms was an audible click = part of the "khar khar".
                    # Now drop one whole 20 ms frame only when it is near
                    # silence (pause between words) -> inaudible.
                    trim_tick = 0
                    try:
                        _f = array.array("h", bytes(self._buf[:size]))
                        _pk = max(max(_f), -min(_f)) if _f else 0
                    except Exception:
                        _pk = 0
                    if _pk < 900 or len(self._buf) > high * 2:
                        del self._buf[:size]
                        self._dropped_bytes += size
                if self._pending:
                    # v83: leftover bytes go FIRST, but this tick's frame still
                    # plays (old code skipped it -> buffer crept up = delay).
                    chunk, self._pending = self._pending, b""
                    if playing and len(self._buf) >= size:
                        chunk += bytes(self._buf[:size])
                        del self._buf[:size]
                elif playing and len(self._buf) >= size:
                    chunk = bytes(self._buf[:size])
                    del self._buf[:size]
                elif playing and self._buf:
                    # Underrun: play what we have with a short fade-out (no
                    # click), then re-buffer before resuming.
                    have = len(self._buf) & ~1
                    tail = bytearray(self._buf[:have])
                    del self._buf[:have]
                    n = have // 2
                    if n:
                        import array
                        a = array.array("h", bytes(tail))
                        for i in range(n):
                            a[i] = int(a[i] * (n - i) / n)
                        tail = bytearray(a.tobytes())
                    chunk = bytes(tail) + b"\x00" * (size - have)
                    playing = False
                    self._underruns += 1
                    target_ms = min(JITTER_TARGET_MAX_MS, target_ms + 60)
                    self._jitter_target_ms = target_ms
                else:
                    # A full drain is the common mobile-network underrun case.
                    # Count it too, otherwise adaptive buffering never grows.
                    if playing and not self._buf:
                        self._underruns += 1
                        target_ms = min(JITTER_TARGET_MAX_MS, target_ms + 60)
                        self._jitter_target_ms = target_ms
                    playing = False if not self._buf else playing
                    chunk = b"\x00" * size
                try:
                    written = os.write(self._raw_fd, chunk)
                    if written < len(chunk):
                        self._pending = chunk[written:]
                except BlockingIOError:
                    self._pending = chunk
                except OSError as exc:
                    if self._restarting or self._raw_fd is None:
                        continue
                    if getattr(exc, "errno", None) == errno.EPIPE:
                        self._pending = b""
                        if await self._restart_ffmpeg(max_attempts=2):
                            continue
                    logger.warning("Live mic feed write failed for user %s: %s",
                                   self.user_id, exc)
                    self._chan_log("LIVE_MIC_FEED_ERROR", {"Error": str(exc)})
                    break
                now = time.monotonic()
                if now - self._last_health_log >= 30:
                    self._last_health_log = now
                    buf_ms = len(self._buf) * FRAME_MS // max(1, size)
                    logger.info("Live mic health user=%s buf=%dms target=%dms "
                                "underruns=%d recv=%dKB restarts=%d",
                                self.user_id, buf_ms, target_ms, self._underruns,
                                self._received_bytes // 1024, self._ffmpeg_restarts)
        except asyncio.CancelledError:
            pass

    def _media_stream(self):
        from ntgcalls import MediaSource
        from pytgcalls.types.raw import AudioParameters, AudioStream, Stream
        command = shlex.join(["cat", self.proc_fifo])
        return Stream(
            microphone=AudioStream(
                MediaSource.SHELL, command, AudioParameters(48000, 2),
            ),
        )

    async def _is_admin(self, vc) -> bool:
        """Is this account an admin (its volume edits apply for everyone)?"""
        try:
            from pyrogram.enums import ChatMemberStatus
            m = await vc.client.get_chat_member(self.chat_id, "me")
            return m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
        except Exception:
            return False

    async def _unmute_self(self):
        """Make sure the streaming account is not muted inside the VC.

        Without this the stream plays but nobody hears anything when the
        account was joined muted (or muted from the Telegram UI).  In a
        non-admin group Telegram often joins new participants muted, and the
        very first unmute can land before the join is fully registered — so
        this retries with increasing delays.  A raw EditGroupCallParticipant
        call with muted=False is the last-resort force-unmute.
        """
        if _check_ghost(self, self.chat_id):
            try:
                call_input = await self.relay._call_input(self.chat_id)
                if call_input:
                    from pyrogram.raw.functions.phone import EditGroupCallParticipant
                    peer = await self.relay.client.resolve_peer(self.relay.account_id)
                    await self.relay.client.invoke(
                        EditGroupCallParticipant(call=call_input, participant=peer, muted=True)
                    )
            except Exception:
                pass
            return
        unmuted = False
        for attempt in range(5):
            for fn_name in ("unmute", "unmute_stream"):
                fn = getattr(self.relay.calls, fn_name, None)
                if fn:
                    try:
                        await fn(self.chat_id)
                        unmuted = True
                        break
                    except Exception as exc:
                        logger.debug("unmute via %s failed: %s", fn_name, exc)
            if unmuted:
                break
            await asyncio.sleep(0.5 * (attempt + 1))

        try:
            await self.relay.calls.resume(self.chat_id)
        except Exception:
            pass

        # Always send a raw unmute as well: the high-level wrapper can return
        # without raising even when Telegram leaves the participant muted.
        try:
            call_input = await self.relay._call_input(self.chat_id)
            if call_input:
                from pyrogram.raw.functions.phone import EditGroupCallParticipant
                peer = await self.relay.client.resolve_peer(
                    self.relay.account_id)
                try:
                    # unmute + 200 % boost in one call (admin chats)
                    await self.relay.client.invoke(
                        EditGroupCallParticipant(
                            call=call_input, participant=peer, muted=False,
                            volume=20000,
                        )
                    )
                except Exception as exc:
                    # non-admin: Telegram may reject the volume flag ->
                    # still force the unmute on its own
                    logger.debug("raw unmute+volume failed (%s), retrying "
                                 "unmute only", exc)
                    await self.relay.client.invoke(
                        EditGroupCallParticipant(
                            call=call_input, participant=peer, muted=False,
                        )
                    )
                unmuted = True
                logger.info("Live mic: force-unmuted via raw API in %s",
                           self.chat_id)
        except Exception as exc:
            logger.debug("raw force-unmute failed: %s", exc)

        # Telegram resets a fresh participant to 100% and the very first
        # attempt often lands before the join is registered — that is why the
        # mic sometimes sounded "normal" instead of boosted.  Retry until it
        # sticks, then keep re-applying it in the background.
        applied = False
        for delay in (0.0, 0.5, 1.2, 2.5, 4.0, 6.0):
            if delay:
                await asyncio.sleep(delay)
            try:
                if await self.relay.set_participant_volume(
                        self.chat_id, self.relay.account_id, 20000, quiet=True):
                    applied = True
                    break
            except Exception:
                continue
        if not applied:
            logger.warning("Live mic: participant volume boost not confirmed "
                           "in chat %s — retrying in background", self.chat_id)
        # Volume set by a group ADMIN applies for every listener.  The relay
        # (spare) account is usually not admin, so its self-volume only counts
        # locally.  Ask the user's own account (often admin) to set the relay
        # to 200% too, so all fighters hear the mic at double volume.
        admin_ok = False
        if getattr(self, "uvc", None) is not None and self.relay is not self.uvc:
            for delay in (0.0, 1.0, 3.0):
                if delay:
                    await asyncio.sleep(delay)
                try:
                    if await self.uvc.set_participant_volume(
                            self.chat_id, self.relay.account_id, 20000, quiet=True):
                        admin_ok = True
                        break
                except Exception:
                    continue
        elif applied:
            # Relay IS the user's own account: its volume edit is the admin one.
            admin_ok = True
        _uvc = getattr(self, "uvc", None)
        if admin_ok and not await self._is_admin(_uvc if (_uvc is not None and self.relay is not _uvc) else self.relay):
            admin_ok = False
        self._admin_boost = admin_ok
        logger.info("Live mic: admin 200%% boost %s in %s -> ceiling %.1f dBFS",
                    "CONFIRMED" if admin_ok else "not active", self.chat_id,
                    self._ceiling_db())
        try:
            await self._apply_ceiling()
        except Exception as exc:
            logger.debug("ceiling swap failed: %r", exc)
        try:
            self.relay.state(self.chat_id).live_volume = 20000
            self.relay._start_keeper(self.chat_id)
        except Exception:
            pass

        self.tg_muted = False
        if not unmuted:
            logger.warning(
                "Live mic: could not unmute account %s in chat %s — it is "
                "probably muted by a group admin, nobody will hear the stream.",
                getattr(self.relay, "account_id", "?"), self.chat_id)
        return unmuted

    async def set_tg_mute(self, muted: bool) -> bool:
        """Toggle the relay account's Telegram mic icon (mute/unmute in VC)."""
        if _check_ghost(self, self.chat_id):
            muted = True
        names = ("mute", "mute_stream") if muted else ("unmute", "unmute_stream")
        for fn_name in names:
            fn = getattr(self.relay.calls, fn_name, None)
            if fn:
                try:
                    await fn(self.chat_id)
                    self.tg_muted = muted
                    return True
                except Exception as exc:
                    logger.debug("%s failed: %s", fn_name, exc)
        return False

    @staticmethod
    def _admin_required(exc: Exception) -> bool:
        return (type(exc).__name__ == "ChatAdminRequired"
                or "CHAT_ADMIN_REQUIRED" in str(exc).upper())

    async def _vc_exists_strict(self) -> bool:
        """Like _vc_exists but 'unknown' counts as NOT running."""
        try:
            return await self.relay._call_input(self.chat_id) is not None
        except Exception:
            return False

    async def _start_vc_any(self) -> bool:
        """Start the group VC with whichever account is allowed to:
        main ID first (usually group owner/admin), then the relay."""
        for acct in (getattr(self, "uvc", None), self.relay):
            if acct is None or not hasattr(acct, "start_voice_chat"):
                continue
            try:
                if await acct.start_voice_chat(self.chat_id) and \
                        await self._vc_exists_strict():
                    return True
            except Exception as exc:
                logger.debug("start VC via %r failed: %r", acct, exc)
        return await self._vc_exists_strict()

    async def _vc_exists(self) -> bool:
        try:
            return await self.relay._call_input(self.chat_id) is not None
        except Exception:
            return True  # unknown → let py-tgcalls decide

    async def _release_stale_calls(self):
        """Make sure the streaming account is not still sitting in another VC.

        Telegram allows one group call per account, so a relay parked in the
        previously used group would keep receiving the audio — the "naya chat
        ID diya par purane GC me join ho gaya" bug.
        """
        if self.relay is None:
            return
        try:
            # VC bridge: the spare ID also sits in the private "source" VC
            # (listening to the user) — never kick it out of that one.
            keep = tuple(getattr(self, "keep_chats", ()) or ())
            if keep:
                await self.relay.release_other_chats(self.chat_id, also_keep=keep)
            else:
                await self.relay.release_other_chats(self.chat_id)
        except Exception as exc:
            logger.warning("Could not leave stale voice chats: %r", exc)

    async def _ensure_relay_peer(self):
        """The streaming account must be able to resolve (and join) the group.

        Works for non-admin groups and for groups the bot was never added to:
        the reference / invite is also attempted through the owner's own
        logged-in account, which is already a member.
        """
        from helpers.peer_guard import PeerAccessError, ensure_peer
        from helpers.logger_channel import get_bot

        client = getattr(self.relay, "client", None)
        if client is None:
            return
        inviter = None
        if not self.shared_account:
            inviter = getattr(self.uvc, "client", None)
        try:
            bot = get_bot()
        except Exception:
            bot = None
        try:
            await ensure_peer(
                client, self.chat_id, bot=bot, auto_join=True,
                inviter=inviter,
                invitee_id=getattr(self.relay, "account_id", 0),
            )
        except PeerAccessError as exc:
            if self.shared_account:
                raise RuntimeError(
                    "Aapka logged-in account is group ko access nahi kar pa raha. "
                    "Group me ek message bhejein (ya group ka @username / invite "
                    "link `.mic chat <link>` se set karein), phir `.mic on` karein."
                ) from exc
            raise RuntimeError(
                "Spare (mic) account is group ko access nahi kar pa raha. "
                "Us account ko group me add karein (non-admin group me bhi chalega), "
                "ya group ka @username / invite link `.mic chat <link>` se dein."
            ) from exc

    @staticmethod
    def _restricted_error(exc: Exception) -> bool:
        """True when Telegram refused the VC join because of a restriction."""
        text = str(exc).upper()
        name = type(exc).__name__
        return (
            name in ("ChatWriteForbidden", "ChatAdminRequired", "UserBannedInChannel",
                     "GroupcallForbidden", "ChatRestricted")
            or "CHAT_WRITE_FORBIDDEN" in text
            or "CHAT_ADMIN_REQUIRED" in text
            or "GROUPCALL_FORBIDDEN" in text
            or "USER_BANNED_IN_CHANNEL" in text
            or "CHAT_SEND_PLAIN_FORBIDDEN" in text
        )

    async def _attach_stream(self):
        from pytgcalls.types import GroupCallConfig
        stream = self._media_stream()
        # v90: browser screen / camera share from the mic page wins over the
        # bot's rendered screen.  It rides the same call as the mic audio.
        feed = getattr(self, "screen_feed", None)
        if feed is not None and feed.alive:
            from pytgcalls.types.raw import Stream
            video = feed.video_stream()
            kw = {"camera": video} if feed.kind == "camera" else {"screen": video}
            try:
                await self.relay.calls.play(
                    self.chat_id, Stream(microphone=stream.microphone, **kw),
                    GroupCallConfig(auto_start=False),
                )
                return
            except Exception as exc:
                logger.warning("live mic + browser %s refused (%r); audio only",
                               feed.kind, exc)
                self.screen_feed = None
                feed.close()
        # SS FIX: starting the live mic replaced the whole call stream with a
        # mic-only one, so an active `.ss on` screen share silently vanished.
        # Keep the fake screen attached when it is ON for this chat.
        screen = None
        try:
            st = self.relay.state(self.chat_id)
            if getattr(st, "ss_on", False):
                from config import Config
                if Config.SCREEN_SHARE_ENABLED:
                    screen = self.relay._screen_source(st)
        except Exception as exc:
            logger.debug("live mic: screen source unavailable: %r", exc)
        if screen is not None:
            from pytgcalls.types.raw import Stream
            try:
                await self.relay.calls.play(
                    self.chat_id, Stream(microphone=stream.microphone, screen=screen),
                    GroupCallConfig(auto_start=False),
                )
                return
            except Exception as exc:
                # Screen must never block the mic: retry audio-only.
                logger.warning("live mic + screen share refused (%r); audio only", exc)
                try:
                    self.relay.state(self.chat_id).ss_on = False
                except Exception:
                    pass
        await self.relay.calls.play(
            self.chat_id, stream,
            GroupCallConfig(auto_start=False),
        )

    async def _play_stream(self) -> bool:
        """Wait for real input, then attach the probe-free raw stream."""
        await self._pipeline_ready.wait()
        await self._release_stale_calls()
        await self._ensure_relay_peer()
        try:
            await asyncio.wait_for(self._first_pcm.wait(), timeout=30.0)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                "Browser se audio PCM receive nahi hua. Mic permission aur page ko foreground mein check karein."
            ) from exc

        # If the relay was parked (from .mic off), it is still in the VC with a
        # silence stream AND muted.  Clear the stale mute BEFORE attaching the
        # new live stream so the unmute lands on the already-registered
        # participant rather than racing the new join.
        for fn_name in ("unmute", "unmute_stream"):
            fn = getattr(self.relay.calls, fn_name, None)
            if fn:
                try:
                    await fn(self.chat_id)
                    break
                except Exception:
                    pass

        try:
            try:
                await self._attach_stream()
            except Exception as first:
                from helpers.peer_guard import is_peer_error

                # Peer cache went stale between resolve and join — refresh the
                # peer once and retry before giving up.
                if is_peer_error(first):
                    await self._ensure_relay_peer()
                    await self._attach_stream()
                elif (self._admin_required(first)
                      and not await self._vc_exists_strict()):
                    # ROOT FIX: VC band ho to py-tgcalls khud CreateGroupCall
                    # bhejta hai; non-admin spare ID ko Telegram
                    # CHAT_ADMIN_REQUIRED deta hai. Ye restriction nahi hai —
                    # VC ko main ID (group admin) se start karo, phir join.
                    if not await self._start_vc_any():
                        raise RuntimeError(
                            "Is group me voice chat band hai aur spare ID admin nahi "
                            "hai, isliye VC start nahi kar sakti.\n"
                            "Fix: group me Voice Chat khud start karein (ya spare ID ko "
                            "'Manage Voice Chats' admin right dein), phir mic ON karein."
                        ) from first
                    self.created_vc = True
                    await self._attach_stream()
                elif self._restricted_error(first):
                    who = ("Aapka account" if self.shared_account
                           else "Spare (mic) account")
                    raise RuntimeError(
                        f"{who} is group me restricted/muted hai, isliye Telegram "
                        "VC join allow nahi kar raha.\n"
                        "Fix: us ID ka mute/restriction hata dein, ya "
                        "Live Mic panel → 👥 Spare Mic Account → 📱 Phone se login (number + OTP) "
                        "se ek doosri (un-muted) ID lagayein."
                    ) from first
                else:
                    # Do NOT silently create/end the group voice chat by default.
                    if await self._vc_exists():
                        raise
                    if not getattr(__import__("config").Config, "LIVE_MIC_AUTO_START_VC", False):
                        raise RuntimeError(
                            "Group me voice chat band hai. Pehle group me Voice Chat "
                            "start karein, phir mic ON karein."
                        )
                    if not await self.relay.start_voice_chat(self.chat_id):
                        raise RuntimeError("Voice chat start nahi hua. Group me VC start karein.")
                    self.created_vc = True
                    await self._attach_stream()
        except Exception as exc:
            logger.error("Live mic py-tgcalls play failed (%s): %r", type(exc).__name__, exc)
            raise

        # Give py-tgcalls a moment to register the new stream with Telegram
        # before unmuting — in non-admin groups the join takes longer.
        await asyncio.sleep(0.2)
        await self._unmute_self()
        # Telegram lets a participant's volume go up to 200%: everybody in the
        # VC then hears the mic account twice as loud, on top of our chain.
        try:
            await self.relay.calls.change_volume_call(self.chat_id, 200)
        except Exception as exc:
            logger.debug("mic volume 200%% not applied: %r", exc)
        return True

    async def _notify(self, key: str, text: str):
        """Show a problem on the mic page AND as a Telegram DM (once per issue)."""
        if key in self._health_warned:
            return
        self._health_warned.add(key)
        logger.warning("Live mic health [%s] user=%s chat=%s: %s",
                       key, self.user_id, self.chat_id, text)
        try:
            if self.ws is not None and not self.ws.closed:
                await self.ws.send_str("warn:" + text)
        except Exception:
            pass
        try:
            from helpers.logger_channel import get_bot
            bot = get_bot()
            if bot is not None:
                await bot.send_message(self.user_id, "⚠️ LIVE MIC: " + text)
        except Exception as exc:
            logger.debug("health DM failed: %r", exc)
        self._chan_log("LIVE_MIC_HEALTH", {"Issue": key})

    async def _clear_issue(self, key: str):
        if key in self._health_warned:
            self._health_warned.discard(key)
            try:
                if self.ws is not None and not self.ws.closed:
                    await self.ws.send_str("ok:Mic chalu — awaaz VC me ja rahi hai")
            except Exception:
                pass

    async def _relay_participant(self):
        """The relay account's own row in the VC (None = not in the VC)."""
        from pyrogram.raw.functions.phone import GetGroupParticipants
        call = await self.relay._call_input(self.chat_id)
        if not call:
            return "no_call"
        peer = await self.relay.client.resolve_peer(self.relay.account_id)
        res = await self.relay.client.invoke(GetGroupParticipants(
            call=call, ids=[peer], sources=[], offset="", limit=20))
        for p in getattr(res, "participants", []) or []:
            if getattr(getattr(p, "peer", None), "user_id", None) == self.relay.account_id:
                return p
        return None

    async def _vc_health_loop(self):
        """record_20 ROOT FIX: the stream "ran" but the VC heard 0 %.

        The server could not see it: every stage looked healthy while the
        voice was lost BEFORE (phone gave the browser silence because the
        Telegram app held the mic) or AFTER (relay muted by an admin / not
        allowed to speak in a non-admin GC, or kicked out because the same
        ID re-joined from the phone).  Every 5 s this checks all three,
        fixes what can be fixed (re-unmute, raise hand) and tells the user
        exactly what to do.
        """
        from pyrogram.raw.functions.phone import EditGroupCallParticipant
        try:
            await asyncio.sleep(4)
            if self.shared_account:
                await self._notify("shared", (
                    "Mic usi ID se chal raha hai jisse aap phone par VC me ho. "
                    "Telegram ek ID ko ek hi jagah VC me rakhta hai — phone se VC "
                    "join/rejoin karte hi bot ki awaaz kat jaati hai. Spare Mic "
                    "Account lagayein (Live Mic panel → Spare Mic Account)."))
            not_in = 0
            while not self._closed:
                now = time.monotonic()
                # 1. Phone mic gives silence.
                if self._received_bytes > 0 and now - self._in_voice_at > 8:
                    await self._notify("mic_silent", (
                        "Phone ke mic se awaaz hi nahi aa rahi (sirf silence). "
                        "Usi phone par Telegram VC me apna mic MUTE rakhein ya "
                        "VC leave karein, screen-recorder ka mic audio band "
                        "karein, phir mic page par dobara tap karein."))
                elif now - self._in_voice_at < 2:
                    await self._clear_issue("mic_silent")
                # 2. Relay state inside the VC.
                try:
                    p = await self._relay_participant()
                except Exception as exc:
                    logger.debug("health participant check failed: %r", exc)
                    p = "error"
                if p is None:
                    not_in += 1
                    if not_in >= 2:
                        await self._notify("not_in_vc", (
                            "Mic wali ID VC me dikh hi nahi rahi (bahar ho gayi). "
                            + ("Aapne phone se usi ID se VC join kiya — isse bot "
                               "kick ho jata hai. Spare ID lagayein ya phone se VC "
                               "leave karke .mic on karein."
                               if self.shared_account else
                               "Telegram me .mic off → .mic on karein.")))
                elif p not in ("error", "no_call"):
                    not_in = 0
                    await self._clear_issue("not_in_vc")
                    if getattr(p, "muted", False):
                        if _check_ghost(self, self.chat_id):
                            pass
                        elif getattr(p, "can_self_unmute", False):
                            try:
                                await self.relay.client.invoke(EditGroupCallParticipant(
                                    call=await self.relay._call_input(self.chat_id),
                                    participant=await self.relay.client.resolve_peer(
                                        self.relay.account_id),
                                    muted=False))
                                logger.info("Live mic health: re-unmuted relay in %s",
                                            self.chat_id)
                            except Exception as exc:
                                logger.debug("health re-unmute failed: %r", exc)
                        else:
                            try:
                                await self.relay.client.invoke(EditGroupCallParticipant(
                                    call=await self.relay._call_input(self.chat_id),
                                    participant=await self.relay.client.resolve_peer(
                                        self.relay.account_id),
                                    raise_hand=True))
                            except Exception:
                                pass
                            await self._notify("admin_muted", (
                                "Is GC me admin ne mic wali ID ko MUTE kiya hai / "
                                "VC 'sirf admin bol sakte' mode me hai — Telegram "
                                "awaaz kisi ko nahi bhejta. Hand raise kar diya hai; "
                                "admin unmute kare, ya aisi spare ID lagayein jo "
                                "is VC me bol sakti ho."))
                    else:
                        await self._clear_issue("admin_muted")
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("Live mic health loop stopped: %r", exc)

    def _mark_started(self):
        if self._health_task is None or self._health_task.done():
            self._health_task = asyncio.create_task(self._vc_health_loop())
        st = self.uvc.state(self.chat_id)
        st.is_playing = True
        st.is_paused = False
        st.mic_enabled = True
        st.mic_boost_user_id = self.user_id
        st.source_name = "Live Mic"
        st.live_relay = True
        self._started = True

        logger.info("Live mic session started for user %s in chat %s",
                    self.user_id, self.chat_id)
        self._chan_log("LIVE_MIC_STARTED", {
            "Relay": "Same account" if self.shared_account else "Spare account",
            "Input rate": f"{self.input_rate} Hz",
        })

    async def start(self) -> bool:
        """Compatibility entry point for callers that already have input."""
        await self._start_pipeline()
        try:
            await self._play_stream()
            self._mark_started()
            return True
        except Exception:
            await self._cleanup_resources()
            raise

    async def apply_settings(self, changes: dict) -> bool:
        """Only the live LOUD / CRUNCH sliders change the chain.

        Other keys are ignored (chain stays the fixed best one).  A change
        swaps FFmpeg on the same processed FIFO, so the VC stream stays alive
        (a ~0.2s blip at most).  The page only sends on slider release.
        """
        # v90: only PUSH (`loud`) is live-tunable; everything else is fixed.
        live = {k: v for k, v in changes.items() if k == "loud"}
        if not live:
            return True
        if all(int(self.settings.get(k, 0) or 0) == v for k, v in live.items()):
            return True
        self.settings.update(live)
        try:
            from helpers.database import db
            await db.set_app_value(f"mic_push_{self.user_id}", str(live["loud"]))
        except Exception:
            pass
        if self._closed or self.ffmpeg_proc is None:
            return True
        saved = self._ffmpeg_restarts
        self._ffmpeg_restarts = 0
        try:
            return await self._restart_ffmpeg(max_attempts=1)
        finally:
            self._ffmpeg_restarts = saved

    async def set_mode(self, mode: str) -> str:
        """Switch loudest/phone/bass for this user; FFmpeg swaps, VC stays."""
        from helpers import live_mic_v79 as v79
        m = v79.normalize_mode(mode)
        if not m:
            raise ValueError(mode)
        changed = m != self.current_mode()
        self.mic_mode = m
        try:
            from helpers.database import db
            await db.set_app_value(f"mic_mode_{self.user_id}", m)
        except Exception:
            pass
        if changed and not self._closed and self.ffmpeg_proc is not None:
            saved = self._ffmpeg_restarts
            self._ffmpeg_restarts = 0
            try:
                await self._restart_ffmpeg(max_attempts=1)
            finally:
                self._ffmpeg_restarts = saved
        return m

    async def set_screen_feed(self, feed) -> None:
        """Attach a browser screen/camera feed to the live call stream."""
        old = self.screen_feed
        self.screen_feed = feed
        try:
            await self._attach_stream()
        finally:
            if old is not None and old is not feed:
                old.close()

    async def clear_screen_feed(self, feed=None) -> None:
        cur = self.screen_feed
        if cur is None or (feed is not None and cur is not feed):
            if feed is not None:
                feed.close()
            return
        self.screen_feed = None
        try:
            if not self._closed and self._started:
                await self._attach_stream()
        except Exception as exc:
            logger.warning("live mic: audio-only re-attach failed: %r", exc)
        finally:
            cur.close()

    async def _send(self, text: str) -> None:
        try:
            if self.ws is not None and not self.ws.closed:
                await self.ws.send_str(text)
        except Exception:
            pass

    async def _handle_studio(self, text: str) -> bool:
        """Mic page extras: mode, chat, VC members.  True = handled."""
        from helpers import web_studio
        if text.startswith("mode:"):
            try:
                m = await self.set_mode(text[5:])
                await self._send("mode:" + m)
            except ValueError:
                await self._send("mode:" + self.current_mode())
            return True
        if text.startswith("chat:"):
            try:
                await web_studio.send_chat(self, text[5:])
                await self._send("chatok")
                await self._send("chatlog:" + json.dumps(await web_studio.chat_history(self)))
            except Exception as exc:
                await self._send("chaterr:" + (str(exc) or type(exc).__name__)[:160])
            return True
        if text == "chatpull":
            try:
                await self._send("chatlog:" + json.dumps(await web_studio.chat_history(self)))
            except Exception as exc:
                await self._send("chaterr:" + (str(exc) or type(exc).__name__)[:160])
            return True
        if text == "vcpull":
            try:
                await self._send("vc:" + json.dumps(await web_studio.vc_members(self)))
            except Exception:
                await self._send("vc:[]")
            return True
        return False

    async def _restart_ffmpeg(self, max_attempts: int = 3) -> bool:
        """Relaunch FFmpeg after an unexpected exit, keeping the VC stream alive."""
        if self._closed or self._restarting:
            return False
        if self._ffmpeg_restarts >= max_attempts:
            return False
        self._ffmpeg_restarts += 1
        self._restarting = True
        try:
            logger.warning("FFmpeg died (exit %s) for user %s — restarting (%d/%d)",
                           self.ffmpeg_proc.returncode if self.ffmpeg_proc else "?",
                           self.user_id, self._ffmpeg_restarts, max_attempts)
            old_raw_fd, old_proc = self._raw_fd, self.ffmpeg_proc
            old_task, old_raw_fifo = self._stderr_task, self.raw_fifo
            self._raw_fd, self.ffmpeg_proc, self._stderr_task = None, None, None
            self._pending = b""
            self._buf = bytearray()
            if old_raw_fd is not None:
                try:
                    os.close(old_raw_fd)
                except OSError:
                    pass
            if old_task:
                old_task.cancel()
            if old_proc:
                try:
                    old_proc.kill()
                    await old_proc.wait()
                except Exception:
                    pass
            if old_raw_fifo and os.path.exists(old_raw_fifo):
                try:
                    os.unlink(old_raw_fifo)
                except OSError:
                    pass
            await self._start_pipeline(keep_proc_fifo=True)
            logger.info("FFmpeg restarted for user %s", self.user_id)
            self._chan_log("LIVE_MIC_FFMPEG_RESTART", {"Attempt": f"{self._ffmpeg_restarts}/{max_attempts}"})
            return True
        except Exception as exc:
            logger.warning("FFmpeg restart failed for user %s: %s", self.user_id, exc)
            self._chan_log("LIVE_MIC_FFMPEG_RESTART_FAILED", {"Error": str(exc)})
            return False
        finally:
            self._restarting = False

    async def run_ws_loop(self):
        """Read PCM chunks from the WebSocket and write them to the raw FIFO."""
        if not self.ws:
            return
        try:
            async for msg in self.ws:
                if self._closed:
                    break
                if (self.ffmpeg_proc and self.ffmpeg_proc.returncode is not None
                        and not self._restarting):
                    # FFmpeg can die on a transient error.  Relaunch it on the
                    # same processed FIFO (py-tgcalls keeps reading that one) so
                    # the userbot stays in the voice chat instead of the whole
                    # mic session being torn down.
                    if await self._restart_ffmpeg():
                        continue
                    logger.warning("FFmpeg died (exit %s) for user %s — stopping mic",
                                   self.ffmpeg_proc.returncode if self.ffmpeg_proc else "?",
                                   self.user_id)
                    self._chan_log("LIVE_MIC_FFMPEG_DIED", {
                        "Exit code": self.ffmpeg_proc.returncode if self.ffmpeg_proc else "?"})
                    try:
                        await self.ws.send_str("error:FFmpeg process ended")
                    except Exception:
                        pass
                    break
                if msg.type == WSMsgType.BINARY:
                    data = msg.data
                    if not data:
                        continue
                    if self.ffmpeg_proc is None:
                        await self.ensure_pipeline()
                    if self._raw_fd is None or self._restarting:
                        continue
                    try:
                        self._last_pcm_at = time.monotonic()
                        # v30: own-voice gate — other fighter's speaker bleed
                        # never reaches the loud chain (see CloseVoiceGate).
                        if self._voice_gate is None:
                            from helpers.audio_processor import CloseVoiceGate
                            self._voice_gate = CloseVoiceGate(rate=int(self.input_rate or 48000))
                        data = self._voice_gate.process(data)
                        self.queue_pcm(data)
                        self._first_pcm.set()
                        self._received_bytes += len(data)
                        now = time.monotonic()
                        try:
                            _a = array.array('h')
                            _a.frombytes(data[:len(data) & ~1])
                            if _a:
                                self._in_peak = max(max(_a), -min(_a))
                                if self._in_peak > 60:   # > -55 dBFS = real sound
                                    self._in_voice_at = now
                        except Exception:
                            pass
                        if (self._received_bytes == len(data)
                                or now - self._last_audio_log >= 10):
                            self._last_audio_log = now
                            logger.info(
                                "Live mic PCM received for user %s: %d bytes",
                                self.user_id, self._received_bytes,
                            )
                    except (OSError, BrokenPipeError):
                        if self._restarting:
                            continue
                        logger.warning("Raw FIFO write failed for user %s", self.user_id)
                        break
                elif msg.type == WSMsgType.TEXT:
                    text = msg.data or ""
                    if text == "ping":
                        try:
                            await self.ws.send_str("pong")
                        except Exception:
                            pass
                        continue
                    if text.startswith("rate:"):
                        try:
                            new_rate = int(float(text[5:]))
                            if (self.ffmpeg_proc is not None and 8000 <= new_rate <= 192000
                                    and new_rate != int(self.input_rate or 48000)):
                                # v83 ROOT FIX: reconnected page with a new
                                # AudioContext rate -> chipmunk/slow voice.
                                logger.info("Live mic rate change %s -> %s for user %s",
                                            self.input_rate, new_rate, self.user_id)
                                self.input_rate = new_rate
                                self._voice_gate = None
                                saved = self._ffmpeg_restarts
                                self._ffmpeg_restarts = 0
                                try:
                                    await self._restart_ffmpeg(max_attempts=1)
                                finally:
                                    self._ffmpeg_restarts = saved
                                continue
                            await self.ensure_pipeline(new_rate)
                        except Exception as exc:
                            logger.error("Pipeline start failed for user %s: %s",
                                         self.user_id, exc)
                            self._chan_log("LIVE_MIC_PIPELINE_FAILED", {"Error": str(exc)})
                            try:
                                await self.ws.send_str(f"error:{exc}")
                            except Exception:
                                pass
                            break
                        continue
                    if text == "stop":
                        break
                    if await self._handle_studio(text):
                        continue
                    if text.startswith("settings:"):
                        try:
                            payload = json.loads(text[len("settings:"):])
                        except Exception:
                            continue
                        changes = self.sanitize_settings(payload if isinstance(payload, dict) else {})
                        if changes:
                            ok = await self.apply_settings(changes)
                            try:
                                await self.ws.send_str(
                                    ("settings:" if ok else "settings_failed:")
                                    + json.dumps(self.public_settings()))
                            except Exception:
                                pass
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                    break
                elif msg.type == WSMsgType.ERROR:
                    logger.error("WebSocket error for user %s: %s", self.user_id, self.ws.exception())
                    break
        except Exception as exc:
            logger.error("WS loop error for user %s: %s", self.user_id, exc)
            self._chan_log("LIVE_MIC_WS_ERROR", {"Error": f"{type(exc).__name__}: {exc}"})

    async def stop(self, leave_vc: bool = False):
        """Stop the live mic relay and clean up all resources."""
        if self._closed:
            return
        self._closed = True
        logger.info("Live mic stopping user=%s chat=%s stats=%s",
                    self.user_id, self.chat_id, self._stats())
        self._chan_log("LIVE_MIC_STOPPED", {**self._stats(),
                       "Left VC": "Yes" if leave_vc else "No"})

        st = self.uvc.chats.get(self.chat_id)
        if st:
            st.mic_enabled = False
            st.mic_boost_user_id = None
            st.live_relay = False
            # Only reset is_playing/source_name if the live mic actually started.
            # If it failed during init (0 bytes), preserve the current song/stream state!
            if self._received_bytes > 0:
                st.is_playing = False
                st.source_name = "—"
                st.recent_live_mic = True

        if self.ws and not self.ws.closed:
            try:
                await self.ws.close()
            except Exception:
                pass

        task = _grace_tasks.pop(self.user_id, None)
        if task and not task.done():
            task.cancel()

        # The one-time link dies with the session, so a stale page cannot
        # silently re-open the mic later.
        try:
            from helpers.database import db
            await db.delete_app_value(f"live_mic_token_{self.user_id}")
        except Exception:
            pass

        feed, self.screen_feed = self.screen_feed, None
        if feed is not None:
            feed.close()

        await self._cleanup_resources()

        # Spare account ko VC me hi baitha rehne do (mic icon OFF, silence
        # stream) taaki wo normal user jaisa lage, bot jaisa aana-jaana nahi.
        if not self.shared_account and self.relay.calls is not None:
            if RELAY_STAY_IN_VC and not leave_vc:
                if not await park_relay(self.relay, self.chat_id):
                    logger.info("park failed, relay left VC %s", self.chat_id)
            else:
                await unpark_relay(self.relay, self.chat_id)

        # Multi-spare: extra spare sessions are not registered here — never
        # pop the user's main session when an extra one stops.
        if _sessions.get(self.user_id) is self:
            _sessions.pop(self.user_id, None)
        logger.info("Live mic session stopped for user %s", self.user_id)

    async def _cleanup_resources(self):
        """Close only the live source; keep the userbot in the group VC."""
        if self._health_task:
            self._health_task.cancel()
            self._health_task = None
        if self._pacer_task:
            self._pacer_task.cancel()
            try:
                await self._pacer_task
            except (asyncio.CancelledError, Exception):
                pass
            self._pacer_task = None

        if self._proc_keeper_fd is not None:
            try:
                os.close(self._proc_keeper_fd)
            except OSError:
                pass
            self._proc_keeper_fd = None

        if self._raw_fd is not None:
            try:
                os.close(self._raw_fd)
            except OSError:
                pass
            self._raw_fd = None

        if self._stderr_task:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):
                pass
            self._stderr_task = None

        if self.ffmpeg_proc:
            try:
                self.ffmpeg_proc.kill()
                await self.ffmpeg_proc.wait()
            except Exception:
                pass
            self.ffmpeg_proc = None

        for fifo in (self.raw_fifo, self.proc_fifo):
            if fifo and os.path.exists(fifo):
                try:
                    os.unlink(fifo)
                except OSError:
                    pass
        self.raw_fifo = None
        self.proc_fifo = None

    def update_settings(self, settings: dict):
        self.settings = dict(settings or {})


def _default_pregain() -> int:
    """Default Mic Pre-Amp (dB): env LIVE_PREGAIN_DB, else 10 dB.

    Loudness comes from the levellers + brick-wall limiter at the end of the
    chain, not from raw pre-amp gain: a huge pre-amp only clips the voice.
    """
    try:
        return int(max(0, min(36, float(os.environ.get("LIVE_PREGAIN_DB", "") or 28))))
    except (TypeError, ValueError):
        return 28


def get_session(user_id: int) -> Optional[LiveMicSession]:
    return _sessions.get(user_id)


def is_active(user_id: int) -> bool:
    return user_id in _sessions


def has_live_socket(user_id: int) -> bool:
    """True only while a browser is really connected and streaming."""
    session = _sessions.get(user_id)
    return bool(session and not session._closed
                and session.ws is not None and not session.ws.closed)


RELAY_STAY_IN_VC = os.environ.get("LIVE_MIC_RELAY_STAY", "1").strip().lower() not in {"0", "false", "no", "off"}


def _silence_stream():
    from ntgcalls import MediaSource
    from pytgcalls.types.raw import AudioParameters, AudioStream, Stream
    cmd = shlex.join([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
        "-f", "s16le", "-ac", "2", "-ar", "48000", "pipe:1",
    ])
    return Stream(microphone=AudioStream(MediaSource.SHELL, cmd, AudioParameters(48000, 2)))


async def _call_first(calls, names, chat_id) -> bool:
    for n in names:
        fn = getattr(calls, n, None)
        if fn:
            try:
                await fn(chat_id)
                return True
            except Exception as exc:
                logger.debug("%s failed: %s", n, exc)
    return False


async def park_relay(relay, chat_id: int) -> bool:
    """Spare account VC me silence ke saath, mic icon OFF karke baitha rahe."""
    try:
        from pytgcalls.types import GroupCallConfig
        await relay._peer(chat_id)
        await relay.calls.play(chat_id, _silence_stream(), GroupCallConfig(auto_start=False))
    except Exception as exc:
        logger.warning("park_relay play failed in %s: %r", chat_id, exc)
        await _call_first(relay.calls, ("leave_call", "leave_group_call"), chat_id)
        return False
    st = relay.state(chat_id)
    st.is_playing = False
    st.source_name = "Idle (spare)"
    st.recent_live_mic = True
    await _call_first(relay.calls, ("mute", "mute_stream"), chat_id)
    return True


async def unpark_relay(relay, chat_id: int) -> bool:
    ok = await _call_first(relay.calls, ("leave_call", "leave_group_call"), chat_id)
    relay.chats.pop(chat_id, None)
    return ok


def _schedule_grace_stop(session: "LiveMicSession",
                         delay: float = RECONNECT_GRACE_SECONDS) -> None:
    """Give a dropped browser time to reconnect before killing the relay."""
    async def _runner():
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if session._closed:
            return
        if session.ws is not None and not session.ws.closed:
            return  # browser came back
        logger.info("Live mic reconnect window over for user %s — stopping",
                    session.user_id)
        await session.stop()

    old = _grace_tasks.pop(session.user_id, None)
    if old and not old.done():
        old.cancel()
    _grace_tasks[session.user_id] = asyncio.create_task(_runner())


def is_active_for_chat(chat_id: int) -> bool:
    return any(session.chat_id == chat_id for session in _sessions.values())


async def create_session(user_id: int, uvc, chat_id: int, settings: dict,
                         relay=None) -> LiveMicSession:
    # Per-user lock: two tabs connecting at once must never both start an
    # FFmpeg pipeline (the loser used to leak its process + FIFOs).
    lock = _create_locks.setdefault(user_id, asyncio.Lock())
    async with lock:
        while True:
            existing = _sessions.get(user_id)
            if not existing:
                break
            try:
                await existing.stop()
            except Exception as exc:
                logger.warning("stop old live mic session failed: %r", exc)
            if _sessions.get(user_id) is existing:
                _sessions.pop(user_id, None)
        session = LiveMicSession(user_id, uvc, chat_id, settings, relay=relay)
        try:
            from helpers.database import db
            from helpers import live_mic_v79 as v79
            session.mic_mode = v79.normalize_mode(
                await db.get_app_value(f"mic_mode_{user_id}") or "") or None
            push = await db.get_app_value(f"mic_push_{user_id}")
            if push not in (None, ""):
                session.settings["loud"] = max(0, min(200, int(float(push))))
        except Exception:
            pass
        _sessions[user_id] = session
        return session


async def stop_session(user_id: int, leave_vc: bool = False) -> bool:
    """Stop the live mic. With leave_vc=True the spare ID also leaves the VC."""
    session = _sessions.get(user_id)
    if not session:
        return False
    await session.stop(leave_vc=leave_vc)
    return True


async def stop_all_for_chat(chat_id: int):
    to_stop = [s for s in _sessions.values() if s.chat_id == chat_id]
    for s in to_stop:
        await s.stop()


async def generate_token(user_id: int, chat_id: int) -> str:
    """Generate a single-use auth token and store it in the database."""
    from helpers.database import db
    secret = secrets.token_urlsafe(16)
    token = f"{user_id}:{chat_id}:{secret}"
    await db.set_app_value(f"live_mic_token_{user_id}", f"{chat_id}:{secret}")
    return token


# --- Web server ---

_app: Optional[web.Application] = None
_runner: Optional[web.AppRunner] = None


def _load_page() -> str:
    """v90 Web Studio page (mic + screen/camera share + VC chat + members)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live_mic_page.html")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


LIVE_MIC_HTML = _load_page()


# v85: public mic URL now lives in helpers/mic_url.py — every candidate is
# verified against THIS bot before a link is sent (root fix for Heroku
# "There's nothing here, yet.").  These wrappers keep the old API.
from helpers import mic_url as _mic_url

BASE_URL_DB_KEY = _mic_url.DB_LAST_GOOD


def _remember_base_url(request: web.Request) -> None:
    _mic_url.remember_request(request)


async def load_base_url() -> str:
    """Find + verify the public URL at startup (called from main)."""
    return await _mic_url.resolve()


async def _url_is_ours(base: str) -> bool:
    return (await _mic_url.probe(_mic_url.normalize(base))) == _mic_url.OK


def public_base_url() -> str:
    """Last verified base URL ('' until one is verified)."""
    return _mic_url.cached()


async def resolve_base_url(force: bool = False) -> str:
    """Verified base URL that really opens this bot's mic page."""
    return await _mic_url.resolve(force=force)


async def clear_stale_session(user_id: int) -> bool:
    """Drop a session whose browser is gone so a fresh link can be issued."""
    session = _sessions.get(user_id)
    if not session:
        return False
    if session.ws is not None and not session.ws.closed and not session._closed:
        return False
    await session.stop()
    return True


async def reset_mic_state(uvc, user_id: int) -> None:
    """Clear leftover mic flags (stale `mic_enabled` used to block `.mic on`)."""
    await clear_stale_session(user_id)
    for cid, st in list(getattr(uvc, "chats", {}).items()):
        if not st.mic_enabled:
            continue
        if st.mic_boost_user_id and st.mic_boost_user_id != user_id:
            continue
        try:
            await uvc.stop_mic_boost(cid)
        except Exception:
            pass
        st.mic_enabled = False
        st.mic_boost_user_id = None
        st.live_relay = False


async def _handle_index(request: web.Request) -> web.Response:
    _remember_base_url(request)
    return web.Response(text=LIVE_MIC_HTML, content_type="text/html",
                        headers={"Cache-Control": "no-store",
                                 "Referrer-Policy": "no-referrer",
                                 "Permissions-Policy": "microphone=(self), camera=(self), display-capture=(self)"})


async def _handle_ws(request: web.Request) -> web.WebSocketResponse:
    # heartbeat: the server pings the browser every 20 s.  Without it mobile
    # networks and the hosting proxy silently drop an "idle-looking" upload-only
    # socket after ~60 s, which is the mic cutting out mid-sentence.
    ws = web.WebSocketResponse(max_msg_size=0, heartbeat=20.0, autoping=True)
    await ws.prepare(request)

    token = request.query.get("token", "")
    if not token:
        await ws.send_str("error:token_missing")
        await ws.close()
        return ws

    parts = token.split(":")
    if len(parts) < 3:
        await ws.send_str("error:token_invalid")
        await ws.close()
        return ws

    try:
        user_id = int(parts[0])
        chat_id = int(parts[1])
    except (ValueError, IndexError):
        await ws.send_str("error:token_invalid")
        await ws.close()
        return ws

    secret = ":".join(parts[2:])

    from helpers.vc_manager import session_manager
    from helpers.database import db

    stored = await db.get_app_value(f"live_mic_token_{user_id}")
    if not stored or stored != f"{chat_id}:{secret}":
        await ws.send_str("error:token_expired_or_used — Telegram me fresh .mic on karein")
        await ws.close()
        return ws

    uvc = await session_manager.get(user_id)
    if not uvc:
        await ws.send_str("error:not_logged_in")
        await ws.close()
        return ws

    # Stream through the spare account when one is configured, so the user's
    # own account can stay in the voice chat on their phone.
    _get_relay = getattr(session_manager, "get_relay", None)
    relay = await _get_relay(user_id) if _get_relay else None

    settings = await db.get_settings(user_id)

    # A second page/tab must never tear down a relay that is already streaming.
    existing = _sessions.get(user_id)
    if existing and not existing._closed and existing.chat_id != chat_id:
        # User asked for a different group: stop the old relay completely so
        # the audio cannot keep flowing into the previous voice chat.
        logger.info("Live mic switching user %s from chat %s to %s",
                    user_id, existing.chat_id, chat_id)
        task = _grace_tasks.pop(user_id, None)
        if task and not task.done():
            task.cancel()
        try:
            await existing.stop()
        except Exception as exc:
            logger.warning("Could not stop previous live mic session: %r", exc)
        _sessions.pop(user_id, None)
        if relay is not None:
            try:
                await relay.release_other_chats(chat_id)
            except Exception:
                pass
        existing = None
    if existing and not existing._closed and existing.chat_id == chat_id:
        if existing.ws is not None and not existing.ws.closed:
            await ws.send_str(
                "error:already_active — mic dusre tab me chal raha hai. "
                "Wo tab band karein ya Telegram me .mic off karein.")
            await ws.close()
            return ws

        # Same session, browser reconnected (screen lock, network blip):
        # re-attach the socket, keep FFmpeg and the voice chat untouched.
        task = _grace_tasks.pop(user_id, None)
        if task and not task.done():
            task.cancel()
        existing.ws = ws
        existing.token_secret = secret
        logger.info("Live mic socket reconnected for user %s", user_id)
        try:
            await ws.send_str("ready")
            await ws.send_str("settings:" + json.dumps(existing.public_settings()))
            await existing.run_ws_loop()
        finally:
            if not existing._closed:
                _schedule_grace_stop(existing)
            if not ws.closed:
                await ws.close()
        return ws

    session = None
    receiver_task = None
    play_task = None
    try:
        session = await create_session(user_id, uvc, chat_id, settings, relay=relay)
        session.ws = ws
        session.token_secret = secret
        # "ready" goes out immediately so the browser can ask for the mic while
        # the user's tap is still fresh; FFmpeg starts when the browser reports
        # its real sample rate (rate:<hz>) or sends its first PCM frame.
        await ws.send_str("ready")
        await ws.send_str("settings:" + json.dumps(session.public_settings()))

        # The browser must feed PCM before PyTgCalls can attach to the live
        # source. Running both tasks concurrently removes the old startup
        # deadlock (play() probing an empty FIFO while browser awaited ready).
        receiver_task = asyncio.create_task(session.run_ws_loop())
        play_task = asyncio.create_task(session._play_stream())
        done, _ = await asyncio.wait(
            {receiver_task, play_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if play_task in done:
            error = play_task.exception()
            if error:
                raise error
            session._mark_started()
            await receiver_task
        else:
            play_task.cancel()
            await asyncio.gather(play_task, return_exceptions=True)
    except Exception as exc:
        logger.error(
            "Live mic start failed (%s): %r", type(exc).__name__, exc,
            exc_info=True,
        )
        message = str(exc) or type(exc).__name__
        try:
            await ws.send_str(f"error:{message}")
        except Exception:
            pass
    finally:
        if receiver_task and not receiver_task.done():
            receiver_task.cancel()
            await asyncio.gather(receiver_task, return_exceptions=True)
        if play_task and not play_task.done():
            play_task.cancel()
            await asyncio.gather(play_task, return_exceptions=True)
        if session and session._started and session._received_bytes:
            # Keep the relay (and the userbot's VC seat) alive for a short
            # while so a dropped browser can simply reconnect.
            _schedule_grace_stop(session)
        elif session:
            await session.stop()
        if not ws.closed:
            await ws.close()
    return ws


async def _token_owner(token: str):
    """(user_id, chat_id) for a valid live mic token, else None."""
    parts = (token or "").split(":")
    if len(parts) < 3:
        return None
    try:
        user_id, chat_id = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    from helpers.database import db
    stored = await db.get_app_value(f"live_mic_token_{user_id}")
    if not stored or stored != f"{chat_id}:{':'.join(parts[2:])}":
        return None
    return user_id, chat_id


async def _handle_screen_ws(request: web.Request) -> web.WebSocketResponse:
    """Browser screen / camera share -> VC video (needs the mic to be live)."""
    ws = web.WebSocketResponse(max_msg_size=4 * 1024 * 1024, heartbeat=20.0)
    await ws.prepare(request)
    owner = await _token_owner(request.query.get("token", ""))
    if not owner:
        await ws.send_str("error:Link expire ho gaya — Telegram me fresh .mic on karein")
        await ws.close()
        return ws
    user_id, chat_id = owner
    from helpers import web_studio
    feed = None
    session = None
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                text = msg.data or ""
                if text == "ping":
                    await ws.send_str("pong")
                elif text.startswith("cfg:"):
                    session = _sessions.get(user_id)
                    if (not session or session._closed or session.chat_id != chat_id
                            or not session._started):
                        await ws.send_str("error:Pehle Mic tab me mic ON karo, phir share karo")
                        continue
                    try:
                        cfg = json.loads(text[4:] or "{}")
                    except Exception:
                        cfg = {}
                    w, h, fps, kind = web_studio.clamp_cfg(cfg)
                    if feed is not None:
                        await session.clear_screen_feed(feed)
                    feed = web_studio.ScreenFeed(w, h, fps, kind).start()
                    try:
                        await session.set_screen_feed(feed)
                    except Exception as exc:
                        feed.close()
                        feed = None
                        await ws.send_str(f"error:VC ne video accept nahi kiya: {exc}")
                        continue
                    if session.screen_feed is not feed:
                        feed = None
                        await ws.send_str("error:VC ne video accept nahi kiya (audio chalu hai)")
                        continue
                    await ws.send_str(f"ok:{kind}:{w}x{h}@{fps}")
                    session._chan_log("LIVE_SCREEN_STARTED", {"Kind": kind, "Size": f"{w}x{h}@{fps}"})
                elif text == "stop":
                    break
            elif msg.type == WSMsgType.BINARY and feed is not None:
                await feed.push_image(msg.data)
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                break
    except Exception as exc:
        logger.warning("screen ws error for user %s: %r", user_id, exc)
    finally:
        if feed is not None:
            if session is not None:
                await session.clear_screen_feed(feed)
            else:
                feed.close()
        if not ws.closed:
            await ws.close()
    return ws


async def _handle_health(request: web.Request) -> web.Response:
    _remember_base_url(request)
    return web.Response(text=_mic_url.health_text(), content_type="text/plain",
                        headers={"Cache-Control": "no-store"})


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", _handle_index)
    app.router.add_get("/health", _handle_health)
    app.router.add_get("/ws/mic", _handle_ws)
    app.router.add_get("/ws/screen", _handle_screen_ws)
    return app


async def start_server(port: int) -> Optional[web.AppRunner]:
    global _app, _runner
    _app = create_app()
    runner = web.AppRunner(_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    _runner = runner
    try:
        from helpers import mic_tunnel
        mic_tunnel.set_port(port)
    except Exception:
        pass
    logger.info("Live mic web server ready on port %s", port)
    return runner


async def stop_server():
    global _runner
    if _runner:
        for session in list(_sessions.values()):
            await session.stop()
        await _runner.cleanup()
        _runner = None
    try:
        from helpers import mic_tunnel
        await mic_tunnel.stop()
    except Exception:
        pass
