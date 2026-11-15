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
        from helpers import live_mic_v79 as _mic_audio
        self.audio_mode = _mic_audio.normalize_mode(
            self.settings.pop("live_mic_mode", None) or _mic_audio.get_mode(), "loudest")

        self.ws: Optional[web.WebSocketResponse] = None
        self.raw_fifo: Optional[str] = None
        self.proc_fifo: Optional[str] = None
        self._raw_fd: Optional[int] = None
        self.ffmpeg_proc: Optional[asyncio.subprocess.Process] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._closed = False
        self._stop_task: Optional[asyncio.Task] = None
        self._leave_vc_requested = False
        self._intentional_stop = False
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
            "mode": self.audio_mode,
        }

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
        return build_live_mic_filter(self._pipeline_ceiling,
                                     loud=int(self.settings.get("loud") or 120),
                                     crunch=int(self.settings.get("crunch", 0) or 0),
                                     mode=self.audio_mode)

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
        # v88: LOUDEST ignores this headroom inside live_mic_v79.limiter_db
        # (it cancelled the 200 % boost = "sab max par bhi normal").
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
        # v88: LOUD + CRUNCH (Extra Tez) sliders drive the chain again.
        live = {k: int(changes[k]) for k in ("loud", "crunch") if k in changes}
        if not live:
            return True
        if all(int(self.settings.get(k, 0) or 0) == v for k, v in live.items()):
            return True
        self.settings.update(live)
        if self._closed or self.ffmpeg_proc is None:
            return True
        saved = self._ffmpeg_restarts
        self._ffmpeg_restarts = 0
        try:
            return await self._restart_ffmpeg(max_attempts=1)
        finally:
            self._ffmpeg_restarts = saved

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
                        self._intentional_stop = True
                        break
                    if text.startswith("control:"):
                        try:
                            payload = json.loads(text[len("control:"):])
                        except (TypeError, ValueError):
                            payload = None
                        await self._handle_web_control(payload)
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

    async def stop(self, leave_vc: bool = False, revoke_token: bool = True):
        """Stop once; concurrent OFF/LEAVE calls share cleanup and LEAVE escalates."""
        if leave_vc:
            self._leave_vc_requested = True
        if self._stop_task is None:
            if self._closed:
                # A partially failed earlier cleanup must not make LEAVE a no-op.
                if leave_vc and not self.shared_account:
                    await unpark_relay(self.relay, self.chat_id)
                return
            self._closed = True
            self._stop_task = asyncio.create_task(self._stop_impl(revoke_token))
        try:
            await asyncio.shield(self._stop_task)
        finally:
            # A LEAVE arriving after the stop task has already parked the relay
            # still wins; never let the earlier OFF decision strand it in VC.
            if leave_vc and not self.shared_account:
                await unpark_relay(self.relay, self.chat_id)

    async def _stop_impl(self, revoke_token: bool):
        logger.info("Live mic stopping user=%s chat=%s stats=%s",
                    self.user_id, self.chat_id, self._stats())
        self._chan_log("LIVE_MIC_STOPPED", {**self._stats(),
                       "Left VC": "Yes" if self._leave_vc_requested else "No"})

        # Clear both sides of a spare-account relay. Previously the browser
        # session used to clear only the owner's state, leaving the relay UI stale.
        for owner in {id(self.uvc): self.uvc, id(self.relay): self.relay}.values():
            st = getattr(owner, "chats", {}).get(self.chat_id)
            if st:
                st.mic_enabled = False
                st.mic_boost_user_id = None
                st.live_relay = False
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
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()

        # Burn only the token that created this session. A late cleanup from an
        # old socket must never invalidate a newer .mic on link.
        if revoke_token and self.token_secret:
            try:
                from helpers.database import db
                key = f"live_mic_token_{self.user_id}"
                if await db.get_app_value(key) == f"{self.chat_id}:{self.token_secret}":
                    await db.delete_app_value(key)
            except Exception:
                pass

        await self._cleanup_resources()

        # An explicit LEAVE must leave the selected VC even when the user's
        # own account is also the relay. Previously the shared-account path
        # skipped this branch, so the browser stopped but the account stayed.
        if self.relay.calls is not None:
            if self._leave_vc_requested:
                await unpark_relay(self.relay, self.chat_id)
            elif not self.shared_account and RELAY_STAY_IN_VC:
                if not await park_relay(self.relay, self.chat_id):
                    logger.info("park failed, relay left VC %s", self.chat_id)

        if _sessions.get(self.user_id) is self:
            _sessions.pop(self.user_id, None)
        logger.info("Live mic session stopped for user %s", self.user_id)

    async def _handle_web_control(self, payload):
        """Authenticated controls carried on this session's token-validated socket."""
        if not isinstance(payload, dict) or self._closed or not self.ws or self.ws.closed:
            return
        action = str(payload.get("action", ""))[:20]
        result = {"action": action, "ok": False, "message": "Action not available"}
        try:
            if action == "hand":
                state = self.relay.state(self.chat_id)
                raised = not bool(getattr(state, "hand_raised", False))
                fn = getattr(self.relay, "raise_hand", None)
                ok = bool(fn and await fn(self.chat_id, raised))
                result.update(ok=ok, on=bool(getattr(state, "hand_raised", False)),
                              message=("Hand raised" if raised else "Hand lowered") if ok
                              else "Could not change hand raise")
            elif action == "screen":
                state = self.relay.state(self.chat_id)
                busy = getattr(self.relay, "ss_busy", None)
                if busy and busy(self.chat_id):
                    result["message"] = "Screen share is switching; try again shortly"
                else:
                    want = not bool(getattr(state, "ss_on", False))
                    fn = getattr(self.relay, "set_screen_share", None)
                    ok = bool(fn and await fn(self.chat_id, want, image_path="" if want else None))
                    result.update(ok=ok, on=bool(getattr(state, "ss_on", False)),
                                  message=("Screen share ON" if want else "Screen share OFF")
                                  if ok else (getattr(state, "ss_error", "") or "Screen share failed"))
            elif action == "mode":
                from helpers import live_mic_v79 as v79
                mode = v79.normalize_mode(payload.get("mode", ""))
                if mode is None:
                    result["message"] = "Choose one of the available sound modes"
                elif mode == self.audio_mode:
                    result.update(ok=True, mode=mode, message=f"{mode.title()} mode already active")
                else:
                    previous = self.audio_mode
                    self.audio_mode = mode
                    if self.ffmpeg_proc is not None:
                        saved = self._ffmpeg_restarts
                        self._ffmpeg_restarts = 0
                        try:
                            ok = await self._restart_ffmpeg(max_attempts=1)
                            if not ok:
                                # Roll back the selected mode and try to restore
                                # the previous filter before reporting the result.
                                self.audio_mode = previous
                                self._ffmpeg_restarts = 0
                                restored = await self._restart_ffmpeg(max_attempts=1)
                                if not restored:
                                    result["message"] = "Mode switch failed; prior audio pipeline could not be restored"
                        finally:
                            self._ffmpeg_restarts = saved
                    else:
                        ok = True
                    if not ok:
                        result.update(mode=previous,
                                      message="Mode switch failed; previous mode restored where possible")
                    else:
                        result.update(ok=True, mode=mode, message=f"{mode.title()} mode active")
            elif action == "chat":
                text = payload.get("text", "")
                text = "".join(ch for ch in str(text) if ch >= " " or ch in "\n\t").strip()
                if not text:
                    result["message"] = "Type a message first"
                elif len(text) > 400:
                    result["message"] = "Keep VC chat under 400 characters"
                else:
                    await self.relay.send_vc_message(self.chat_id, text)
                    result.update(ok=True, message="Sent to VC chat")
        except Exception as exc:
            result["message"] = str(exc)[:180] or type(exc).__name__
        try:
            await self.ws.send_str("control:" + json.dumps(result))
        except Exception:
            pass

    async def send_relay_profile(self):
        """Tell the page WHICH account fights in the VC: name, @user, id, DP, group."""
        info = {"name": "", "username": "", "id": 0, "photo": "", "chat": "",
                "shared": bool(self.shared_account)}
        try:
            client = self.relay.client
            me = await client.get_me()
            info["id"] = int(me.id)
            info["name"] = " ".join(x for x in (me.first_name, me.last_name) if x) or "Relay ID"
            info["username"] = me.username or ""
            photo = getattr(me, "photo", None)
            if photo is not None:
                import base64
                try:
                    buf = await client.download_media(photo.small_file_id, in_memory=True)
                    raw = bytes(buf.getbuffer()) if hasattr(buf, "getbuffer") else b""
                    if raw and len(raw) < 400_000:
                        info["photo"] = "data:image/jpeg;base64," + base64.b64encode(raw).decode()
                except Exception as exc:
                    logger.debug("relay photo download failed: %r", exc)
            try:
                chat = await client.get_chat(self.chat_id)
                info["chat"] = getattr(chat, "title", "") or ""
            except Exception:
                pass
        except Exception as exc:
            logger.debug("relay profile failed: %r", exc)
        try:
            if self.ws is not None and not self.ws.closed:
                await self.ws.send_str("relay:" + json.dumps(info))
        except Exception:
            pass

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
        await session.stop(revoke_token=False)

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
        _sessions[user_id] = session
        return session


async def stop_session(user_id: int, leave_vc: bool = False) -> bool:
    """Stop a live mic; LEAVE also repairs the target VC if the session is stale."""
    session = _sessions.get(user_id)
    if session:
        await session.stop(leave_vc=leave_vc)
        return True
    if not leave_vc:
        return False

    # A worker restart or failed websocket cleanup can leave the Telegram
    # account in its target VC after the in-memory session has disappeared.
    # The explicit LEAVE command is the recovery path for that stale state.
    try:
        from helpers.database import db
        from helpers.vc_manager import session_manager
        raw_chat_id = await db.get_app_value(f"mic_chat_{user_id}")
        chat_id = int(raw_chat_id) if raw_chat_id else 0
        relay = await session_manager.get_relay(user_id)
        if relay is None:
            relay = await session_manager.get(user_id)
        if not chat_id or relay is None or getattr(relay, "calls", None) is None:
            return False
        return await unpark_relay(relay, chat_id)
    except Exception as exc:
        logger.warning("Stale live-mic LEAVE cleanup failed for user %s: %r",
                       user_id, exc)
        return False


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


LIVE_MIC_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
<meta name="theme-color" content="#07080c">
<meta name="robots" content="noindex,nofollow">
<meta name="referrer" content="no-referrer">
<title>Live Mic - Apex VC Fyt</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Unbounded:wght@600;800&family=Manrope:wght@500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
:root{
 --bg:#07080c;--card:rgba(255,255,255,.035);--card2:rgba(255,255,255,.07);
 --line:rgba(255,255,255,.08);--line2:rgba(255,255,255,.15);
 --text:#f4f5f8;--muted:#8d92a6;--dim:#50556a;
 --acc:#c6ff3d;--acc-ink:#0c1400;--acc-soft:rgba(198,255,61,.12);
 --live:#ff3b4f;--live-soft:rgba(255,59,79,.13);
 --amber:#ffb627;--amber-soft:rgba(255,182,39,.13);--cyan:#3de0ff;
 --display:'Unbounded',system-ui,sans-serif;
 --body:'Manrope',system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;
 --mono:'JetBrains Mono',ui-monospace,Menlo,monospace;
 --lvl:0}
html,body{min-height:100%}
body{font-family:var(--body);background:var(--bg);color:var(--text);overflow-x:hidden;-webkit-font-smoothing:antialiased}
button{font-family:inherit}
/* backdrop */
.bg{position:fixed;inset:0;z-index:0;pointer-events:none;overflow:hidden;
 background:radial-gradient(120% 60% at 50% -12%,#171c2a 0%,transparent 62%),radial-gradient(90% 50% at 50% 112%,#121c06 0%,transparent 62%),var(--bg)}
.grid{position:absolute;inset:0;background-image:radial-gradient(rgba(255,255,255,.075) 1px,transparent 1.2px);background-size:22px 22px;
 mask-image:radial-gradient(ellipse at 50% 28%,#000 8%,transparent 68%);-webkit-mask-image:radial-gradient(ellipse at 50% 28%,#000 8%,transparent 68%)}
.ambient-mic-blur{position:absolute;top:30%;left:50%;width:560px;height:560px;border-radius:50%;transform:translate(-50%,-50%);
 background:radial-gradient(circle,rgba(198,255,61,.11),rgba(61,224,255,.06) 42%,transparent 70%);
 filter:blur(60px);-webkit-filter:blur(60px);animation:amb 8s ease-in-out infinite alternate;transition:background .6s}
body.live .ambient-mic-blur{background:radial-gradient(circle,rgba(255,59,79,.2),rgba(198,255,61,.09) 46%,transparent 72%)}
@keyframes amb{from{opacity:.6;transform:translate(-50%,-50%) scale(.9)}to{opacity:1;transform:translate(-50%,-50%) scale(1.08)}}
.star{position:absolute;width:2px;height:2px;border-radius:50%;background:#fff;opacity:0;animation:tw 5s infinite}
@keyframes tw{50%{opacity:.5}}
.wrap{position:relative;z-index:1;max-width:440px;margin:0 auto;display:flex;flex-direction:column;gap:14px;min-height:100vh;
 padding:calc(14px + env(safe-area-inset-top)) 16px calc(26px + env(safe-area-inset-bottom))}
/* top bar */
.top{display:flex;align-items:center;justify-content:space-between;gap:10px}
.brand{display:flex;align-items:center;gap:10px;min-width:0}
.logo{flex:none;width:36px;height:36px;border-radius:11px;display:grid;place-items:center;background:var(--acc);color:var(--acc-ink);
 box-shadow:0 0 0 1px rgba(255,255,255,.12),0 8px 24px rgba(198,255,61,.22)}
.logo svg{width:18px;height:18px;fill:currentColor}
.brand b{display:block;font:800 .8rem var(--display);letter-spacing:.14em;text-transform:uppercase;white-space:nowrap}
.brand small{display:block;color:var(--muted);font-size:.72rem;font-weight:600;letter-spacing:.04em}
.badge{flex:none;display:inline-flex;align-items:center;gap:8px;height:30px;padding:0 12px;border-radius:99px;border:1px solid var(--line2);
 background:var(--card);font:700 .66rem/1 var(--mono);letter-spacing:.16em;text-transform:uppercase;color:var(--muted);transition:all .3s}
.badge .dot{width:7px;height:7px;border-radius:50%;background:var(--dim)}
body.busy .badge{color:var(--amber);border-color:rgba(255,182,39,.45);background:var(--amber-soft)}
body.busy .badge .dot{background:var(--amber);animation:blink .8s infinite}
body.live .badge{color:#fff;border-color:var(--live);background:var(--live);box-shadow:0 0 22px rgba(255,59,79,.45)}
body.live .badge .dot{background:#fff;animation:blink 1s infinite}
@keyframes blink{50%{opacity:.2}}
.tgbar{display:none;align-items:center;gap:10px;padding:12px 12px 12px 14px;border-radius:16px;background:var(--amber-soft);
 border:1px solid rgba(255,182,39,.45);color:var(--amber);font-size:.84rem;font-weight:700;line-height:1.35}
.tgbar.show{display:flex}.tgbar span{flex:1}
.tgbar button{flex:none;border:0;border-radius:11px;padding:9px 12px;background:var(--amber);color:#1a1000;font-weight:800;font-size:.8rem;cursor:pointer}
/* stage */
.hero{position:relative;border-radius:30px;border:1px solid var(--line);padding:16px 14px 16px;display:flex;flex-direction:column;align-items:center;overflow:hidden;
 background:linear-gradient(180deg,rgba(255,255,255,.045),rgba(255,255,255,.012))}
.hero-top{width:100%;display:flex;justify-content:space-between;align-items:center;font:700 .68rem var(--mono);letter-spacing:.16em;color:var(--dim);text-transform:uppercase}
.timer{font:700 .8rem var(--mono);letter-spacing:.18em;color:var(--dim);transition:color .3s}
body.live .timer{color:var(--live)}
.stage{position:relative;width:min(300px,76vw);aspect-ratio:1/1;display:grid;place-items:center;margin:4px 0 2px}
.mic-blur-bg{position:absolute;inset:20%;border-radius:50%;pointer-events:none;background:radial-gradient(circle,rgba(198,255,61,.22),transparent 70%);
 filter:blur(30px);-webkit-filter:blur(30px);transform:scale(calc(1 + var(--lvl)*.55));transition:transform .12s,background .5s}
body.live .mic-blur-bg{background:radial-gradient(circle,rgba(255,59,79,.5),rgba(198,255,61,.1) 60%,transparent 76%)}
canvas#viz{position:absolute;inset:0;width:100%;height:100%}
.ring{position:absolute;inset:3%;border-radius:50%;
 background:repeating-conic-gradient(rgba(255,255,255,.22) 0 .9deg,transparent .9deg 7.5deg);
 mask:radial-gradient(closest-side,transparent 93%,#000 94%,#000 99%,transparent 100%);
 -webkit-mask:radial-gradient(closest-side,transparent 93%,#000 94%,#000 99%,transparent 100%)}
.ring.r2{inset:21%;background:none;border:1px dashed rgba(255,255,255,.1);mask:none;-webkit-mask:none;animation:spin 36s linear infinite}
.ring.r3{inset:12%;background:none;border:1px solid rgba(255,255,255,.05);mask:none;-webkit-mask:none}
@keyframes spin{to{transform:rotate(360deg)}}
.pulse{position:absolute;inset:28%;border-radius:50%;border:2px solid var(--live);opacity:0;pointer-events:none}
body.live .pulse{animation:pulse 2.4s ease-out infinite}
body.live .pulse.p2{animation-delay:.8s}body.live .pulse.p3{animation-delay:1.6s}
@keyframes pulse{0%{transform:scale(1);opacity:.65}100%{transform:scale(1.85);opacity:0}}
.orb{position:relative;width:44%;aspect-ratio:1/1;border-radius:50%;border:none;cursor:pointer;display:grid;place-items:center;color:var(--text);
 background:radial-gradient(circle at 50% 28%,#2c3140,#12151d 72%);
 box-shadow:inset 0 1px 0 rgba(255,255,255,.16),inset 0 -12px 26px rgba(0,0,0,.55),0 0 0 9px rgba(255,255,255,.035),0 26px 60px rgba(0,0,0,.7);
 transition:transform .15s,background .35s,box-shadow .35s;transform:scale(calc(1 + var(--lvl)*.08))}
.orb:active{transform:scale(.93)}
.orb:focus-visible{outline:2px solid var(--acc);outline-offset:6px}
.orb-glow{position:absolute;inset:-12px;border-radius:50%;pointer-events:none;opacity:0;transition:opacity .4s;
 background:conic-gradient(from 0deg,transparent 0 55%,rgba(198,255,61,.75) 80%,transparent 100%);
 mask:radial-gradient(closest-side,transparent 86%,#000 88%,#000 100%);-webkit-mask:radial-gradient(closest-side,transparent 86%,#000 88%,#000 100%)}
body:not(.live):not(.busy) .orb-glow{opacity:1;animation:spin 3.2s linear infinite}
body.live .orb-glow{opacity:1;background:radial-gradient(closest-side,transparent 80%,rgba(255,255,255,.55));animation:none}
.orb svg{position:relative;width:36%;height:36%;fill:currentColor}
body.live .orb{background:radial-gradient(circle at 50% 28%,#ff7381,#d8122a 72%);color:#fff;
 box-shadow:inset 0 1px 0 rgba(255,255,255,.4),0 0 0 9px rgba(255,59,79,.16),0 0 calc(30px + var(--lvl)*90px) rgba(255,59,79,.62)}
body.busy .orb{background:radial-gradient(circle at 50% 28%,#ffd677,#e08700 72%);color:#1a1000;animation:breathe 1.2s ease-in-out infinite}
@keyframes breathe{50%{box-shadow:0 0 0 12px rgba(255,182,39,.16),0 0 60px rgba(255,182,39,.5)}}
.tap{font:800 .7rem var(--display);letter-spacing:.26em;text-transform:uppercase;color:var(--muted);margin-top:4px}
body.live .tap{color:#ff8b97}
.status{margin-top:14px;width:100%;padding:12px 14px;border-radius:16px;font-weight:700;font-size:.93rem;text-align:center;line-height:1.4;transition:all .25s}
.status.off{background:var(--card);color:var(--muted);border:1px solid var(--line)}
.status.on{background:var(--acc-soft);color:var(--acc);border:1px solid rgba(198,255,61,.42)}
.status.err{background:var(--live-soft);color:#ff8d99;border:1px solid rgba(255,59,79,.5)}
.status.connecting{background:var(--amber-soft);color:var(--amber);border:1px solid rgba(255,182,39,.45)}
.save{font-size:.78rem;font-weight:700;color:var(--acc);min-height:14px;text-align:center;margin-top:6px}
/* cards */
.card{border-radius:22px;border:1px solid var(--line);background:var(--card);padding:16px;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px)}
.card-h{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:12px}
.card-h h3{font:800 .64rem var(--display);letter-spacing:.22em;text-transform:uppercase;color:var(--muted)}
.card-h .val{font:700 1.15rem var(--mono);color:var(--acc)}
.vu{position:relative;height:20px;border-radius:7px;background:rgba(255,255,255,.05);overflow:hidden}
.vu i{position:absolute;left:0;top:0;bottom:0;width:0;transition:width .06s;
 background:linear-gradient(90deg,var(--acc) 0%,var(--acc) 62%,var(--amber) 80%,var(--live) 100%);background-size:min(406px,calc(100vw - 66px)) 100%}
.vu::after{content:"";position:absolute;inset:0;background:repeating-linear-gradient(90deg,transparent 0 8px,rgba(7,8,12,.9) 8px 11px)}
.vurow{display:flex;justify-content:space-between;gap:10px;margin-top:9px;font:700 .8rem var(--mono)}
#vuDb{color:var(--text)}#vuHint{color:var(--muted);text-align:right}
#vuHint.q{color:var(--amber)}#vuHint.g{color:var(--acc)}#vuHint.c{color:var(--live)}
.eq{display:flex;gap:3px;align-items:flex-end;height:30px;margin-top:12px}
.eq i{flex:1;display:block;border-radius:2px;height:4px;background:linear-gradient(var(--cyan),var(--acc));opacity:.85;transition:height .08s}
input[type=range]{-webkit-appearance:none;appearance:none;width:100%;height:8px;border-radius:99px;outline:none;margin:6px 0 2px;
 background:linear-gradient(90deg,var(--acc) 0 var(--fill,43%),rgba(255,255,255,.09) var(--fill,43%) 100%)}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:28px;height:28px;border-radius:50%;background:#fff;cursor:pointer;
 box-shadow:0 0 0 5px rgba(198,255,61,.22),0 4px 12px rgba(0,0,0,.6)}
input[type=range]::-moz-range-thumb{width:28px;height:28px;border:none;border-radius:50%;background:#fff;cursor:pointer;box-shadow:0 0 0 5px rgba(198,255,61,.22)}
.ticks{display:flex;justify-content:space-between;margin-top:8px;font:600 .66rem var(--mono);color:var(--dim)}
.note{margin-top:12px;font-size:.8rem;line-height:1.55;color:var(--muted)}
.note b{color:var(--text)}.note .g{color:var(--acc)}.note .r{color:var(--live)}
.seg-label{font-size:.72rem;font-weight:700;color:var(--muted);letter-spacing:.04em;margin:0 0 8px}
.seg-label+.seg{margin-bottom:14px}.seg:last-child{margin-bottom:0}
.seg{display:grid;grid-template-columns:1fr 1fr;gap:5px;padding:5px;border-radius:17px;background:rgba(0,0,0,.38);border:1px solid var(--line)}
.chip{border:0;border-radius:13px;padding:11px 6px;background:transparent;color:var(--muted);font-weight:800;font-size:.92rem;cursor:pointer;
 display:flex;flex-direction:column;align-items:center;gap:2px;transition:background .2s,color .2s,box-shadow .2s}
.chip small{font-weight:600;font-size:.7rem;color:var(--dim)}
.chip.act{background:var(--card2);color:var(--text);box-shadow:inset 0 0 0 1px var(--line2),0 6px 16px rgba(0,0,0,.35)}
.chip.act small{color:var(--acc)}
.feat{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.f{border-radius:16px;border:1px solid var(--line);background:var(--card);padding:12px 6px;text-align:center}
.f b{display:block;font:700 .9rem var(--mono);color:var(--text)}
.f small{display:block;margin-top:2px;color:var(--muted);font-size:.68rem;font-weight:600}
.btn{width:100%;padding:14px;border-radius:16px;border:1px solid var(--line2);background:var(--card);color:var(--text);font-weight:800;font-size:.9rem;cursor:pointer;
 display:flex;align-items:center;justify-content:center;gap:8px;transition:transform .12s}
.btn:active{transform:scale(.97)}
.btn:disabled{opacity:.45;cursor:not-allowed;transform:none}
.web-controls{display:grid;grid-template-columns:1fr 1fr;gap:9px}
.web-controls .btn{min-height:48px;padding:10px 8px;font-size:.8rem}
.web-chat{display:flex;gap:8px;margin-top:10px}
.web-chat input{flex:1;min-width:0;border:1px solid var(--line2);border-radius:13px;padding:12px;background:rgba(0,0,0,.35);color:var(--text);font:600 .86rem var(--body);outline:none}
.web-chat input:focus{border-color:var(--acc)}
.web-chat .btn{width:auto;padding:0 16px;min-height:44px}
.control-note{margin-top:9px;color:var(--muted);font-size:.74rem;line-height:1.45;text-align:center}
.btn.warn{margin-top:12px;background:var(--amber);border-color:transparent;color:#1a1000}
.help{display:none;width:100%;margin-top:12px;padding:13px 14px;border-radius:16px;background:rgba(255,182,39,.07);border:1px solid rgba(255,182,39,.4);
 color:#ffd98a;font-size:.84rem;line-height:1.65;text-align:left}
.help.show{display:block}.help b{color:#fff}.help ol{margin:6px 0 0 18px}.help li{margin:3px 0}
.help code{font-family:var(--mono);font-size:.8rem;color:#fff}
details{border-radius:20px;border:1px solid var(--line);background:var(--card);padding:14px 16px}
summary{cursor:pointer;list-style:none;display:flex;justify-content:space-between;align-items:center;font-weight:800;font-size:.92rem}
summary::-webkit-details-marker{display:none}
summary::after{content:"+";font:700 1.1rem var(--mono);color:var(--muted);transition:transform .2s}
details[open] summary::after{transform:rotate(45deg)}
.steps{list-style:none;counter-reset:s;margin-top:12px;display:flex;flex-direction:column;gap:10px}
.steps li{counter-increment:s;display:flex;gap:10px;font-size:.86rem;line-height:1.5;color:var(--muted)}
.steps li::before{content:counter(s);flex:none;width:22px;height:22px;border-radius:7px;display:grid;place-items:center;background:var(--card2);color:var(--acc);font:700 .72rem var(--mono)}
.steps b{color:var(--text)}
footer{margin-top:auto;padding-top:8px;text-align:center;color:var(--dim);font:700 .64rem var(--mono);letter-spacing:.2em;text-transform:uppercase}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style>
</head>
<body>
<div class="bg"><div class="grid"></div><div class="ambient-mic-blur"></div><div id="stars"></div></div>
<main class="wrap">
<header class="top">
  <div class="brand">
    <div class="logo"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 14a3 3 0 0 0 3-3V5a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.92V21h2v-3.08A7 7 0 0 0 19 11h-2z"/></svg></div>
    <div><b>Apex VC Fyt</b><small>Live Mic Studio</small></div>
  </div>
  <span class="badge"><span class="dot"></span><span id="badgeTxt">Offline</span></span>
</header>

<div id="tgBar" class="tgbar"><span>Telegram ka browser mic block karta hai. Chrome me kholein.</span><button onclick="openInChrome()">Chrome</button></div>

<section class="hero">
  <div class="hero-top"><span>VC Relay</span><span class="timer" id="liveTimer">00:00</span></div>
  <div class="stage">
    <div class="mic-blur-bg"></div>
    <canvas id="viz" width="580" height="580"></canvas>
    <div class="ring"></div><div class="ring r2"></div><div class="ring r3"></div>
    <div class="pulse"></div><div class="pulse p2"></div><div class="pulse p3"></div>
    <button id="toggleBtn" class="orb" onclick="toggleMic()" aria-label="Mic ON / OFF">
      <div class="orb-glow"></div>
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 14a3 3 0 0 0 3-3V5a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.92V21h2v-3.08A7 7 0 0 0 19 11h-2z"/></svg>
    </button>
  </div>
  <div class="tap" id="tapTxt">Tap to go live</div>
  <div id="status" class="status off" role="status" aria-live="polite">Mic OFF</div>
  <div class="save" id="saveNote"></div>
  <button id="retryBtn" class="btn warn" style="display:none" onclick="retryMic()">Permission dene ke baad - Dobara try karein</button>
  <div id="permHelp" class="help"></div>
</section>

<section class="card" id="relayCard">
  <div class="card-h"><h3>VC me Fight ID</h3><span class="val" id="relayTag" style="font-size:.75rem">--</span></div>
  <div style="display:flex;align-items:center;gap:14px">
    <div id="relayDp" style="width:58px;height:58px;border-radius:50%;flex:none;background:var(--card2) center/cover no-repeat;border:2px solid var(--acc);display:flex;align-items:center;justify-content:center;font-family:var(--display);font-size:1.3rem;color:var(--acc)">?</div>
    <div style="min-width:0">
      <div id="relayName" style="font-weight:800;font-size:1.05rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">Connect hone par dikhega</div>
      <div id="relayUser" style="color:var(--muted);font-size:.85rem;font-family:var(--mono)"></div>
      <div id="relayChat" style="color:var(--dim);font-size:.8rem;margin-top:2px"></div>
    </div>
  </div>
</section>

<section class="card">
  <div class="card-h"><h3>Extra Tez</h3><b id="crunchVal" class="val">0%</b></div>
  <input type="range" id="tezR" min="0" max="200" step="10" value="0" oninput="paintTez(this.value)" onchange="sendTez(this.value)" aria-label="Extra loudness">
  <p class="note">Aur tez chahiye to badhao (thodi phategi). Chhodne par 1 sec me lagta hai.</p>
</section>

<section class="card">
  <div class="card-h"><h3>Mic Level</h3><span id="vuDb" class="val">-- dB</span></div>
  <div class="vu"><i id="vuBar"></i></div>
  <div class="vurow"><span>-50</span><span id="vuHint">Mic band hai</span></div>
  <div class="eq" id="eq"></div>
</section>

<section class="card">
  <div class="card-h"><h3>Mic Boost</h3><b id="boostVal" class="val">200%</b></div>
  <input type="range" id="boostR" min="0.5" max="4" step="0.25" value="2" oninput="setBoost(this.value)" aria-label="Mic boost">
  <div class="ticks"><span>50%</span><span>100%</span><span>200%</span><span>300%</span><span>400%</span></div>
  <p class="note">Level <b class="g">GREEN</b> rakho. <b class="r">RED / CLIP</b> = boost kam. Dheemi = boost zyada. Mic ON rehte hi turant lagta hai.</p>
</section>

<section class="card">
  <div class="card-h"><h3>Live VC Controls</h3><span class="val" style="font-size:.75rem">LIVE</span></div>
  <div class="web-controls">
    <button id="handBtn" class="btn" onclick="sendControl('hand')" disabled>✋ Raise hand</button>
    <button id="screenBtn" class="btn" onclick="sendControl('screen')" disabled>🖥 Live screen</button>
    <button class="btn" onclick="sendControl('chat',{text:'❤️'})" disabled>❤️ React</button>
    <button class="btn" onclick="sendControl('chat',{text:'👏'})" disabled>👏 React</button>
  </div>
  <form class="web-chat" onsubmit="sendChat(event)">
    <input id="vcChatInput" maxlength="400" autocomplete="off" placeholder="Message in live VC chat" aria-label="Message in live VC chat" disabled>
    <button id="chatSendBtn" class="btn" type="submit" disabled>Send</button>
  </form>
  <p id="controlNote" class="control-note" role="status" aria-live="polite">Connect mic to use VC controls.</p>
</section>

<section class="card">
  <div class="card-h"><h3>Awaaz Mode</h3></div>
  <p class="seg-label">Sound</p>
  <div class="seg">
    <button class="chip" id="mFight" onclick="setSoundMode('loudest')">&#9889; LOUDEST<small>Phone speaker max</small></button>
    <button class="chip" id="mPhone" onclick="setSoundMode('phone')">&#128241; PHONE<small>Tez + saaf</small></button>
    <button class="chip" id="mBass" onclick="setSoundMode('bass')">&#128266; BASS<small>Tez + bhari</small></button>
  </div>
  <p class="seg-label">Output</p>
  <div class="seg">
    <button class="chip" id="mEar" onclick="setMode('aec','')">&#127911; Earphone<small>Max loud</small></button>
    <button class="chip" id="mSpk" onclick="setMode('aec','1')">&#128266; Speaker<small>Echo band</small></button>
  </div>
</section>

<div class="feat">
  <div class="f"><b>48k</b><small>Native rate</small></div>
  <div class="f"><b>20ms</b><small>Low latency</small></div>
  <div class="f"><b>SAFE</b><small>Peak limited</small></div>
</div>

<button id="chromeBtn" class="btn" onclick="openInChrome()">Chrome me kholein (link copy ho jayega)</button>

<details>
  <summary>Kaise use karein</summary>
  <ol class="steps">
    <li><span>Group me pehle <b>Voice Chat start</b> karein.</span></li>
    <li><span>Telegram me <b>.mic on</b> bhejein aur link <b>Chrome</b> me kholein.</span></li>
    <li><span>Beech wala <b>mic button</b> dabayein aur permission <b>Allow</b> karein.</span></li>
    <li><span>Telegram app ka apna mic <b>mute</b> rakhein - awaaz isi page se jaati hai.</span></li>
    <li><span><b>MIC LEVEL</b> green rahe to best. Red = phat rahi, <b>CLEAN</b> dabao.</span></li>
    <li><span>Rokne ke liye mic button dobara dabayein ya <b>.mic off</b> bhejein.</span></li>
  </ol>
</details>
<footer>Private link &bull; Share mat karna</footer>
</main>

<script>
let ws=null, audioCtx=null, mediaStream=null, source=null, workletNode=null,
    scriptNode=null, analyser=null, silentSink=null;
let isOn=false, readyTimer=null, firstFrameTimer=null, retryTimer=null;
// No controls: the phone sends a clean signal, the server makes it loud+clear.
let nComp=null, nPre=null;
const CTL_VERSION = 25;
let attemptId=0, firstFrameSeen=false, pending=[], sendReady=false,
    retries=0, captureRate=48000;
const MAX_RETRIES=600;          // ~30+ min of retrying instead of giving up
let lastFrameAt=0, wakeLock=null, watchdogTimer=null, lastReadyAt=0;
const statusEl=document.getElementById('status');
const btnEl=document.getElementById('toggleBtn');
const noteEl=document.getElementById('saveNote');
const tapEl=document.getElementById('tapTxt');
const badgeEl=document.getElementById('badgeTxt');
const TOKEN=new URLSearchParams(location.search).get('token');
try{localStorage.removeItem('vcfyt_ctl');}catch(e){}
try{localStorage.removeItem('vcfyt_mic');}catch(e){}
// v41: loudness FIXED in code — sliders hidden, nothing sent.
['loudR','crunchR'].forEach(function(id){var e=document.getElementById(id);
  if(e){var c=e.closest('.ctl,.row,label,div')||e;c.style.display='none';}});
function syncCtl(st){if(st&&typeof st.crunch==='number'){const r=document.getElementById('tezR');if(r){r.value=st.crunch;paintTez(st.crunch);}}}
function paintTez(v){const b=document.getElementById('crunchVal');if(b)b.textContent=Math.round(v/2)+'%';const r=document.getElementById('tezR');if(r)r.style.setProperty('--fill',(v/2)+'%');}
function sendTez(v){try{localStorage.setItem('vcfyt_tez',String(v));if(ws&&ws.readyState===1)ws.send('settings:'+JSON.stringify({crunch:parseInt(v,10)||0,loud:200}));}catch(e){}}
function showRelay(r){try{const n=document.getElementById('relayName'),u=document.getElementById('relayUser'),c=document.getElementById('relayChat'),d=document.getElementById('relayDp'),t=document.getElementById('relayTag');
  n.textContent=r.name||'Relay ID';u.textContent=(r.username?'@'+r.username+' · ':'')+'ID '+(r.id||'?');c.textContent=r.chat?('Group: '+r.chat):'';
  t.textContent=r.shared?'Aapki ID':'Spare ID';
  if(r.photo){d.style.backgroundImage='url("'+r.photo+'")';d.textContent='';}else{d.style.backgroundImage='';d.textContent=(r.name||'?').trim().charAt(0).toUpperCase();}}catch(e){}}
// v83 MIC BOOST: phone par hi live badlo (server restart nahi, gap nahi).
function micBoost(){let v=2.0;try{const x=parseFloat(localStorage.getItem('vcfyt_boost'));if(x>=0.5&&x<=4)v=x;}catch(e){}return v;}
function setBoost(v){v=Math.max(0.5,Math.min(4,parseFloat(v)||2));try{localStorage.setItem('vcfyt_boost',String(v));}catch(e){}
  const b=document.getElementById('boostVal');if(b)b.textContent=Math.round(v*100)+'%';
  const rr=document.getElementById('boostR');if(rr)rr.style.setProperty('--fill',((v-0.5)/3.5*100).toFixed(1)+'%');
  if(nPre&&audioCtx){try{nPre.gain.setTargetAtTime(v,audioCtx.currentTime,0.05);}catch(e){nPre.gain.value=v;}}}
function initBoost(){const r=document.getElementById('boostR');if(r){r.value=micBoost();setBoost(r.value);}}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',initBoost);else initBoost();
function sendCtl(){}
const controlEls=[...document.querySelectorAll('.web-controls button'),document.getElementById('vcChatInput'),document.getElementById('chatSendBtn')];
function enableControls(on){controlEls.forEach(el=>{if(el)el.disabled=!on;});}
function sendControl(action,extra={}){
  if(!ws||ws.readyState!==1||!sendReady){controlNote('Reconnect to use VC controls.');return false;}
  try{ws.send('control:'+JSON.stringify({action,...extra}));return true;}catch(e){controlNote('Could not send VC control.');return false;}
}
function sendChat(ev){ev.preventDefault();const input=document.getElementById('vcChatInput');const text=(input.value||'').trim();if(!text)return;
  if(sendControl('chat',{text}))input.value='';}
function controlNote(text){const el=document.getElementById('controlNote');if(el)el.textContent=text;}

// decoration: stars + eq bars
(function(){const s=document.getElementById('stars');for(let i=0;i<28;i++){const d=document.createElement('i');d.className='star';
d.style.left=Math.random()*100+'%';d.style.top=Math.random()*100+'%';d.style.animationDelay=(Math.random()*4)+'s';s.appendChild(d);}
const eq=document.getElementById('eq');for(let i=0;i<24;i++)eq.appendChild(document.createElement('i'));})();
const eqBars=[...document.querySelectorAll('#eq i')];
const viz=document.getElementById('viz'), vctx=viz.getContext('2d');
function drawViz(data){
  const W=viz.width,H=viz.height,cx=W/2,cy=H/2;vctx.clearRect(0,0,W,H);
  const n=64,r0=160;
  for(let i=0;i<n;i++){
    const v=data?data[i%data.length]/255:0;const a=i/n*Math.PI*2;const len=6+v*110;
    const x1=cx+Math.cos(a)*r0,y1=cy+Math.sin(a)*r0,x2=cx+Math.cos(a)*(r0+len),y2=cy+Math.sin(a)*(r0+len);
    const g=vctx.createLinearGradient(x1,y1,x2,y2);g.addColorStop(0,'rgba(198,255,61,.95)');g.addColorStop(1,v>.7?'#ff3b4f':'#3de0ff');
    vctx.strokeStyle=g;vctx.lineWidth=5;vctx.lineCap='round';vctx.beginPath();vctx.moveTo(x1,y1);vctx.lineTo(x2,y2);vctx.stroke();
  }
}
drawViz(null);

let liveSince=0, liveTick=null;
function paintTimer(){const el=document.getElementById('liveTimer');if(!el)return;
  const s=liveSince?Math.floor((Date.now()-liveSince)/1000):0;
  el.textContent=String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0');}
function stopTimer(){liveSince=0;if(liveTick){clearInterval(liveTick);liveTick=null;}paintTimer();}
function setStatus(t,c){statusEl.textContent=t;statusEl.className='status '+c;
  document.body.classList.toggle('live',c==='on');document.body.classList.toggle('busy',c==='connecting');
  badgeEl.textContent=c==='on'?'On Air':(c==='connecting'?'Connecting':'Offline');
  if(c==='on'&&!liveSince){liveSince=Date.now();clearInterval(liveTick);liveTick=setInterval(paintTimer,1000);paintTimer();
    try{if(navigator.vibrate)navigator.vibrate(18);}catch(e){}}}

function closeSocket(sendStop){
  if(window._wsPingInterval){clearInterval(window._wsPingInterval);window._wsPingInterval=null;}
  const oldWs=ws; ws=null; sendReady=false;
  enableControls(false);
  if(oldWs){
    oldWs.onclose=null; oldWs.onerror=null; oldWs.onmessage=null; oldWs.onopen=null;
    try{if(sendStop&&oldWs.readyState===1)oldWs.send('stop');}catch(e){}
    try{oldWs.close();}catch(e){}
  }
}

function resetAudioState(message='Mic OFF', cls='off', sendStop=true){
  attemptId++;
  if(typeof watchdogTimer!=='undefined'&&watchdogTimer){clearInterval(watchdogTimer);watchdogTimer=null;}
  try{releaseWakeLock();}catch(e){}
  clearTimeout(readyTimer); clearTimeout(firstFrameTimer); clearTimeout(retryTimer);
  readyTimer=firstFrameTimer=retryTimer=null;
  closeSocket(sendStop);
  if(workletNode){try{workletNode.disconnect();}catch(e){} try{workletNode.port.onmessage=null;}catch(e){} workletNode=null;}
  if(scriptNode){try{scriptNode.disconnect();}catch(e){} scriptNode.onaudioprocess=null; scriptNode=null;}
  if(silentSink){try{silentSink.disconnect();}catch(e){} silentSink=null;}
  if(analyser){try{analyser.disconnect();}catch(e){} analyser=null;}
  if(source){try{source.disconnect();}catch(e){} source=null;}
  nComp=nPre=null;
  if(mediaStream){mediaStream.getTracks().forEach(t=>{try{t.stop();}catch(e){}});mediaStream=null;}
  if(audioCtx){try{audioCtx.close();}catch(e){} audioCtx=null;}
  isOn=false; firstFrameSeen=false; pending=[]; retries=0;
  document.documentElement.style.setProperty('--lvl',0); eqBars.forEach(b=>b.style.height='4px'); drawViz(null);
  stopTimer(); setStatus(message,cls); tapEl.textContent='Tap to go live'; btnEl.disabled=false;
}
const helpEl=document.getElementById('permHelp');
const retryEl=document.getElementById('retryBtn');
const IS_ANDROID=/Android/i.test(navigator.userAgent);
const IN_TELEGRAM=/Telegram/i.test(navigator.userAgent)||!!window.TelegramWebview||!!window.TelegramWebviewProxy;
if(IN_TELEGRAM){const tb=document.getElementById('tgBar');if(tb)tb.classList.add('show');}

function showHelp(html){helpEl.innerHTML=html;helpEl.classList.add('show');retryEl.style.display='block';}
function hideHelp(){helpEl.classList.remove('show');helpEl.innerHTML='';retryEl.style.display='none';}

const DENIED_HELP=
  '<b>Mic permission block ho gayi hai.</b> Browser dobara nahi poochega — manually allow karein:'
  +'<ol>'
  +'<li>Address bar me <b>lock / (i) icon</b> dabayein.</li>'
  +'<li><b>Permissions</b> (ya "Site settings") kholein.</li>'
  +'<li><b>Microphone</b> par <b>Allow</b> chunein.</li>'
  +'<li>Page <b>reload</b> karein, phir mic button dabayein.</li>'
  +'</ol>'
  +'Phone Settings: <b>Apps → (browser) → Permissions → Microphone → Allow</b>.';

const TG_HELP=
  '<b>Telegram ka andar wala browser mic block karta hai.</b><br>'
  +'<b>"Chrome me kholein"</b> dabayein aur wahan <b>Allow</b> karein.';

function micErrorText(e){
  const name=(e&&e.name)||'';
  const msg=(e&&e.message)||String(e||'');
  if(name==='NotAllowedError'||name==='SecurityError'||/denied|dismiss/i.test(msg)){
    showHelp(DENIED_HELP+(IN_TELEGRAM?'<br><br>'+TG_HELP:''));
    return 'Mic permission block hai — niche steps follow karein';
  }
  if(name==='NotFoundError'||name==='OverconstrainedError'){
    showHelp('<b>Mic mila nahi.</b> Headset nikaal kar dobara try karein, ya doosre browser me kholein.');
    return 'Mic device nahi mila';
  }
  if(name==='NotReadableError'||name==='AbortError'){
    showHelp('<b>Mic doosri app use kar rahi hai.</b> Call/recorder band karein, VC me apna mic <b>mute</b> rakhein, phir try karein.');
    return 'Mic busy hai (doosri app use kar rahi hai)';
  }
  if(/timeout/i.test(msg)){
    showHelp('<b>Permission popup ka reply nahi mila.</b> Page reload karke <b>Allow</b> dabayein.'+(IN_TELEGRAM?'<br><br>'+TG_HELP:''));
    return 'Permission timeout — dobara try karein';
  }
  showHelp('<b>Mic start nahi hua:</b> '+msg+'<br>Page reload karein ya Chrome me kholein.');
  return 'Mic error: '+msg;
}

function retryMic(){hideHelp();toggleMic();}
function friendlyErr(code){
  const c=String(code||'');
  if(c.indexOf('not_logged_in')===0) return 'Bot me aapka login nahi hai - Telegram me /login karein, phir .mic on';
  if(c.indexOf('token_missing')===0||c.indexOf('token_invalid')===0) return 'Link adhoora hai - Telegram me fresh .mic on karein';
  if(c.indexOf('token_expired')===0) return 'Link purana ho gaya - Telegram me fresh .mic on karein';
  if(c.indexOf('already_active')===0) return 'Mic doosre tab me chal raha hai - wo tab band karein';
  return c;
}

function openInChrome(){
  const url=location.href;
  try{navigator.clipboard.writeText(url);}catch(e){}
  try{
    const ta=document.createElement('textarea');ta.value=url;document.body.appendChild(ta);
    ta.select();document.execCommand('copy');document.body.removeChild(ta);
  }catch(e){}
  noteEl.textContent='Link copy ho gaya';
  setTimeout(()=>{noteEl.textContent='';},2000);
  if(IS_ANDROID){
    const bare=url.replace(/^https?:[/][/]/,'');
    location.href='intent://'+bare+'#Intent;scheme=https;package=com.android.chrome;end';
  }else{
    window.open(url,'_blank');
  }
}

async function checkPermissionUpfront(){
  if(!window.isSecureContext){
    setStatus('Ye page https par kholein — warna mic allowed nahi hoga','err');
    showHelp('<b>Secure (https) page zaroori hai.</b> Telegram me <code>.mic on</code> se mila link hi kholein.');
    return;
  }
  if(!navigator.mediaDevices||!navigator.mediaDevices.getUserMedia){
    setStatus('Is browser me mic support nahi hai','err');
    showHelp('<b>Is browser me mic capture support nahi hai.</b> Link <b>Chrome</b> me kholein.');
    return;
  }
  try{
    if(navigator.permissions&&navigator.permissions.query){
      const st=await navigator.permissions.query({name:'microphone'});
      if(st.state==='denied'){
        setStatus('Mic permission pehle se blocked hai','err');
        showHelp(DENIED_HELP+(IN_TELEGRAM?'<br><br>'+TG_HELP:''));
      }
      st.onchange=()=>{ if(st.state!=='denied'&&!isOn){hideHelp();setStatus('Mic OFF','off');} };
    }
  }catch(e){}
}

(function init(){
  if(!TOKEN) setStatus('Token missing — Telegram me .mic on karein','err');
  else checkPermissionUpfront();
})();

const PCM_WORKLET = `
class PCMP extends AudioWorkletProcessor {
    constructor(){
        super();
        // Emit one 20 ms packet at the device's actual sample rate.  The old
        // fixed 1024 samples was ~21.3 ms at 48 kHz while the server paced 20
        // ms frames, creating continuous jitter/latency drift.
        this.size = Math.max(128, Math.round(sampleRate * 0.02));
        this.buf = new Int16Array(this.size);
        this.n = 0;
    }
    process(inputs) {
        const input = inputs[0];
        if (!input || !input[0]) return true;
        const ch0 = input[0];
        for (let i = 0; i < ch0.length; i++) {
            let s = ch0[i];
            s = s > 1 ? 1 : (s < -1 ? -1 : s);
            this.buf[this.n++] = Math.round(s < 0 ? s * 32768 : s * 32767);
            if (this.n >= this.size) {
                const out = this.buf.buffer;
                this.port.postMessage(out, [out]);
                this.buf = new Int16Array(this.size);
                this.n = 0;
            }
        }
        return true;
    }
}
registerProcessor('pcm-processor', PCMP);
`;

function push(buf, myAttempt){
    if (attemptId!==myAttempt) return;
    firstFrameSeen = true;
    lastFrameAt = Date.now();
    trackMicLevel(buf);
    if (firstFrameTimer) { clearTimeout(firstFrameTimer); firstFrameTimer=null; }
    if (!ws || ws.readyState!==1 || !sendReady) {
        // Buffer ~1s while the socket (re)connects, then drop the oldest.
        pending.push(buf);
        if (pending.length > 25) pending.shift();
        return;
    }
    try { ws.send(buf); } catch(e){}
}

function connectSocket(myAttempt){
    if (attemptId!==myAttempt) return;
    const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    const socket = new WebSocket(proto + location.host + '/ws/mic?token=' + encodeURIComponent(TOKEN));
    ws = socket;
    sendReady = false;
    socket.binaryType = 'arraybuffer';

    clearTimeout(readyTimer);
    readyTimer = setTimeout(()=>{
        if (attemptId===myAttempt && !sendReady && ws===socket) {
            try{socket.close();}catch(e){}
        }
    }, 20000);

    socket.onopen = () => {
        if (window._wsPingInterval) clearInterval(window._wsPingInterval);
        window._wsPingInterval = setInterval(() => {
            if (ws && ws.readyState === 1) { try { ws.send('ping'); } catch(e){} }
            else clearInterval(window._wsPingInterval);
        }, 15000);
    };

    socket.onmessage = (ev) => {
        if (typeof ev.data !== 'string') return;
        if (ev.data === 'pong') return;
        if (ev.data === 'ready') {
            clearTimeout(readyTimer); readyTimer=null;
            if (attemptId!==myAttempt || socket!==ws) return;
            retries = 0;
            // Only the capture rate goes out on connect.  Sending settings here
            // restarted FFmpeg one second into every session, which is what
            // knocked the bot out of the voice chat.
            try { socket.send('rate:' + captureRate); } catch(e){}
            sendReady = true;
            lastReadyAt = Date.now();
            while (pending.length) { try { socket.send(pending.shift()); } catch(e){ break; } }
            setStatus('LIVE - Mic ON', 'on');
            enableControls(true);
            try{const preferred=normMode(localStorage.getItem('vcfyt_sound_mode'));if(preferred&&preferred!==activeSoundMode)activeSoundMode=preferred;sendControl('mode',{mode:activeSoundMode});}catch(e){}
            controlNote('Hand raise, live screen, VC chat and reactions are ready.');
            try{const tz=parseInt(localStorage.getItem('vcfyt_tez')||'0',10);if(tz>0)setTimeout(()=>sendTez(tz),1500);}catch(e){}
            // Do not send settings automatically on connect.  The server-side
            // settings path rebuilds FFmpeg; doing that after first audio used
            // to create an audible gap and could make the relay look dead on
            // slower phones. Explicit preset/reset actions still send changes.
        } else if (ev.data.startsWith('settings:')) {
            try { const st=JSON.parse(ev.data.slice(9)); syncCtl(st); } catch(e){}
        } else if (ev.data.startsWith('relay:')) {
            try { showRelay(JSON.parse(ev.data.slice(6))); } catch(e){}
        } else if (ev.data.startsWith('settings_failed:')) {
            /* ignored */
        } else if (ev.data.startsWith('control:')) {
            try{
              const r=JSON.parse(ev.data.slice(8));controlNote(r.message||'Done');if(r.action==='mode'&&r.mode){paintModes(r.mode);try{localStorage.setItem('vcfyt_sound_mode',r.mode);}catch(e){}}
              if(r.action==='hand'&&r.ok){const b=document.getElementById('handBtn');b.textContent=r.on?'✋ Lower hand':'✋ Raise hand';}
              if(r.action==='screen'&&r.ok){const b=document.getElementById('screenBtn');b.textContent=r.on?'🖥 Stop live screen':'🖥 Live screen';}
            }catch(e){}
        } else if (ev.data.startsWith('warn:')) {
            setStatus(ev.data.slice(5), 'err');      // keep streaming
        } else if (ev.data.startsWith('ok:')) {
            setStatus(ev.data.slice(3), 'on');
        } else if (ev.data.startsWith('error:')) {
            socket._fatal = true;
            resetAudioState(friendlyErr(ev.data.slice(6)), 'err');
        }
    };

    socket.onerror = () => {};

    socket.onclose = () => {
        if (attemptId!==myAttempt || socket!==ws || socket._fatal) return;
        if (window._wsPingInterval) { clearInterval(window._wsPingInterval); window._wsPingInterval=null; }
        sendReady = false;
        enableControls(false);
        if (!isOn) return;
        // A socket that stayed up for a while is a healthy one: don't let
        // earlier blips count towards the give-up limit.
        if (lastReadyAt && (Date.now()-lastReadyAt) > 30000) retries = 0;
        if (retries >= MAX_RETRIES) {
            resetAudioState('Connection tut gaya — Telegram me fresh .mic on karein','err',false);
            return;
        }
        retries++;
        setStatus('Dobara jud rahe hain… ('+retries+')','connecting');
        clearTimeout(retryTimer);
        // Fast first retries (most drops are a 1-2 s blip), then back off.
        retryTimer = setTimeout(()=>connectSocket(myAttempt),
                                Math.min(3000, 250 + 250*retries));
    };
}

const USE_AEC = new URLSearchParams(location.search).get("aec") === "1";
let activeSoundMode='loudest';
function normMode(m){m=String(m||'').toLowerCase();m=({clean:'phone',warm:'bass',fight:'loudest'})[m]||m;return ['loudest','phone','bass'].includes(m)?m:'';}
try{const savedMode=normMode(localStorage.getItem('vcfyt_sound_mode'));if(savedMode)activeSoundMode=savedMode;}catch(e){}
function setSoundMode(mode){
  mode=normMode(mode);if(!mode)return;
  activeSoundMode=mode;try{localStorage.setItem('vcfyt_sound_mode',mode);}catch(e){}paintModes(mode);
  if(!isOn||!sendReady){controlNote(mode.toUpperCase()+' selected — mic connect hone par apply hoga.');return;}
  if(sendControl('mode',{mode}))controlNote('Switching to '+mode.toUpperCase()+'…');
}
function setMode(k,v){const u=new URL(location.href);if(v)u.searchParams.set(k,v);else u.searchParams.delete(k);
 if(u.href===location.href)return; if(isOn&&!confirm('Output device badalne ke liye page reload hoga. Mic dobara tap karna hoga.'))return; location.href=u.href;}
function paintModes(mode){
 const t=(id,on)=>{const e=document.getElementById(id);if(e)e.classList.toggle('act',on);};
 mode=normMode(mode);if(mode)activeSoundMode=mode;
 t('mFight',activeSoundMode==='loudest');t('mPhone',activeSoundMode==='phone');t('mBass',activeSoundMode==='bass');
 const q=new URLSearchParams(location.search),a=q.get('aec')==='1';t('mEar',!a);t('mSpk',a);
}
let vuPeak=0;
function vuUpdate(td){let pk=0,ss=0;for(let i=0;i<td.length;i++){const x=Math.abs(td[i]-128)/128;if(x>pk)pk=x;ss+=x*x;}
 const rms=Math.sqrt(ss/td.length),db=rms>0?20*Math.log10(rms):-90;vuPeak=Math.max(pk,vuPeak*0.92);
 const w=Math.max(0,Math.min(100,(db+50)*2));const b=document.getElementById('vuBar');if(b)b.style.width=w+'%';
 const d=document.getElementById('vuDb');if(d)d.textContent=(db<-89?'--':db.toFixed(0))+' dB';
 const h=document.getElementById('vuHint');if(!h)return;
 if(vuPeak>0.985){h.textContent='CLIP — thoda door / CLEAN';h.className='c';}
 else if(db<-32){h.textContent='Dheemi — mic paas lao';h.className='q';}
 else{h.textContent='PERFECT — full tez';h.className='g';}}
document.addEventListener('DOMContentLoaded',()=>paintModes());setTimeout(()=>paintModes(),0);
const FIGHT = true; // Capture stays stable; selectable sound profiles run on the server.
// SILENT-MIC DETECTOR: Android gives the browser pure silence while another
// app (Telegram in the VC, a call) holds the microphone.  Warn the user
// instead of streaming silence for the whole fight.
let micPeak = 0, silentSince = 0, silentWarned = false;
function trackMicLevel(buf){
    try{
        const v = new Int16Array(buf); let pk = 0;
        for (let i=0;i<v.length;i+=8){ const a = v[i]<0?-v[i]:v[i]; if(a>pk) pk=a; }
        micPeak = pk;
        const now = Date.now();
        if (pk < 40) {                    // < -58 dBFS = no mic signal at all
            if (!silentSince) silentSince = now;
            if (!silentWarned && now - silentSince > 6000) {
                silentWarned = true;
                setStatus('Mic se awaaz nahi aa rahi! Telegram app me VC se mic MUTE/leave karein (Telegram mic pakad leta hai), phir yahan dobara tap karein','err');
            }
        } else {
            if (silentWarned) setStatus('Mic chalu — awaaz ja rahi hai','on');
            silentSince = 0; silentWarned = false;
        }
    }catch(e){}
}

async function requestWakeLock(){
    // Screen-off is the #1 reason the mic "ruk jata hai" mid-stream.
    try{
        if ('wakeLock' in navigator && !wakeLock) {
            wakeLock = await navigator.wakeLock.request('screen');
            wakeLock.addEventListener('release', ()=>{ wakeLock=null; });
        }
    }catch(e){ wakeLock=null; }
}
function releaseWakeLock(){ try{ if(wakeLock) wakeLock.release(); }catch(e){} wakeLock=null; }

function keepContextAlive(){
    // A silent oscillator keeps the AudioContext from being suspended when the
    // page goes to the background on Android/Chrome.
    try{
        if(!audioCtx || !silentSink) return;
        const osc=audioCtx.createOscillator();
        const g=audioCtx.createGain(); g.gain.value=0.00001;
        osc.frequency.value=30; osc.connect(g); g.connect(silentSink);
        osc.start();
    }catch(e){}
}

function startWatchdog(myAttempt){
    clearInterval(watchdogTimer);
    watchdogTimer = setInterval(()=>{
        if (attemptId!==myAttempt || !isOn) { clearInterval(watchdogTimer); watchdogTimer=null; return; }
        // 1. AudioContext suspended (screen lock / background) -> resume.
        if (audioCtx && audioCtx.state !== 'running') { audioCtx.resume().catch(()=>{}); }
        // 2. Mic track died or got muted by the OS (incoming call, other app).
        try{
            const t = mediaStream && mediaStream.getAudioTracks()[0];
            if (t && (t.readyState === 'ended')) {
                resetAudioState('Mic band ho gaya — dobara tap karein','err');
                return;
            }
        }catch(e){}
        // 3. Capture graph stopped producing frames -> fall back / restart it.
        if (firstFrameSeen && lastFrameAt && (Date.now()-lastFrameAt) > 4000) {
            if (audioCtx) audioCtx.resume().catch(()=>{});
            try{ if(!scriptNode) useScriptProcessorRef && useScriptProcessorRef(); }catch(e){}
            lastFrameAt = Date.now();
        }
        // 4. Socket gone and no retry pending -> reconnect now.
        if ((!ws || ws.readyState===3) && !retryTimer) {
            setStatus('Dobara jud rahe hain…','connecting');
            connectSocket(myAttempt);
        }
        // 5. Wake lock lost after a tab switch -> take it again.
        if (!wakeLock && !document.hidden) requestWakeLock();
    }, 2000);
}
let useScriptProcessorRef=null;

async function toggleMic() {
    if (isOn) { stopMic(); return; }
    const myAttempt=++attemptId;
    btnEl.disabled = true;
    hideHelp();
    setStatus('Mic permission maang rahe hain...', 'connecting');

    if (!TOKEN) { setStatus('Token missing', 'err'); btnEl.disabled=false; return; }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        btnEl.disabled=false;
        setStatus(micErrorText({name:'NotSupported',message:'Is browser me mic support nahi hai'}),'err');
        return;
    }

    // Mic + AudioContext are created right here, inside the tap handler —
    // waiting for the socket first made some Android browsers refuse the mic.
    pending=[]; retries=0; firstFrameSeen=false;
    try {
        const AC = window.AudioContext||window.webkitAudioContext;
        // v83: 48 kHz = Telegram ka native rate (no resample = saaf awaaz).
        try { audioCtx = new AC({ sampleRate: 48000, latencyHint: 'interactive' }); }
        catch(e) { audioCtx = new AC(); }
        try { await audioCtx.resume(); } catch(e){}
        // Strict constraints (sampleRate/sampleSize) make some Android phones
        // throw OverconstrainedError, which looked like "permission denied".
        // Try the tuned request first, then a plain one.
        const ask = (constraints) => Promise.race([
            navigator.mediaDevices.getUserMedia(constraints),
            new Promise((_,reject)=>setTimeout(()=>reject(new Error('Microphone permission timeout')),25000))
        ]);
        try {
            mediaStream = await ask({
                // Echo-cancel ON (stops the VC sound from the speaker feeding
                // back into the mic).  Phone noise-suppression and auto-gain
                // OFF: they muffle the voice and pump the level up and down;
                // the server does clean denoise + levelling instead.
                // record_20 ROOT FIX: echo-cancel is now OFF by default.
                // In a fight the VC never stops talking; phone AEC treats
                // that as permanent "double talk" and ducks/cancels YOUR
                // voice (others came through, our voice ~0%).  The server
                // gate + limiter handle feedback.  Add ?aec=1 to the link
                // only if you use the phone speaker and hear echo.
                audio: { echoCancellation: USE_AEC, noiseSuppression: false,
                         autoGainControl: false, channelCount: 1 },
            });
        } catch (firstErr) {
            const n = firstErr && firstErr.name;
            if (n === 'NotAllowedError' || n === 'SecurityError' || /timeout/i.test(String(firstErr&&firstErr.message))) throw firstErr;
            mediaStream = await ask({ audio: true });
        }
        if (attemptId!==myAttempt) return;
        try { await audioCtx.resume(); } catch(e){}
        source = audioCtx.createMediaStreamSource(mediaStream);
        // CLEAN CAPTURE (root fix).  The old phone chain was x10 pre-amp ->
        // 2 compressors (each with automatic make-up gain) -> x6 post gain,
        // which hard-clipped the voice into a square wave ON THE PHONE —
        // loud-ish but torn and unclear, and no server chain can repair it.
        // Now: rumble cut -> +6 dB -> transparent peak limiter.  Nothing
        // ever reaches the int16 clip; all loudness is made server-side.
        try {
            // v81 FIGHT pre-chain (?boost=clean = old +6 dB only).
            // rumble cut 150 Hz -> body cut 300 Hz -> presence +6 dB @2.7 kHz
            // -> 12 dB pre-amp -> 6:1 comp (dheemi awaaz upar) -> brick-wall
            // limiter at -1 dBFS.  Phone ka AGC/NS/AEC OFF hai, isliye full
            // raw mic yahi se tez + saaf hoke server tak jata hai.
            const hp = audioCtx.createBiquadFilter(); hp.type='highpass'; hp.frequency.value=FIGHT?150:80; hp.Q.value=0.707;
            const mud = audioCtx.createBiquadFilter(); mud.type='peaking'; mud.frequency.value=300; mud.Q.value=1; mud.gain.value=FIGHT?-3:0;
            const pres = audioCtx.createBiquadFilter(); pres.type='peaking'; pres.frequency.value=2700; pres.Q.value=1; pres.gain.value=FIGHT?6:0;
            const pre = audioCtx.createGain(); pre.gain.value = micBoost();
            const comp = audioCtx.createDynamicsCompressor();
            comp.threshold.value=FIGHT?-22:0; comp.knee.value=6; comp.ratio.value=FIGHT?6:1;
            comp.attack.value=0.003; comp.release.value=0.12;
            const lim = audioCtx.createDynamicsCompressor();
            lim.threshold.value=-1; lim.knee.value=0; lim.ratio.value=20;
            lim.attack.value=0.001; lim.release.value=0.05;
            // v83: soft-clip guard - DynamicsCompressor brick-wall nahi hota,
            // peaks int16 me hard-clip hote the (phone par hi 'khar khar').
            const sc = audioCtx.createWaveShaper();
            const N=2048, curve=new Float32Array(N);
            for(let i=0;i<N;i++){const x=i/(N-1)*2-1; curve[i]=Math.tanh(1.25*x)/Math.tanh(1.25)*0.95;}
            sc.curve=curve; sc.oversample='4x';
            source.connect(hp); hp.connect(mud); mud.connect(pres); pres.connect(pre);
            pre.connect(comp); comp.connect(lim); lim.connect(sc);
            nPre = pre; nComp = lim;
            source = sc;
        } catch(e){ nComp=nPre=null; }
    } catch (e) {
        const friendly = micErrorText(e);
        resetAudioState(friendly, 'err');
        return;
    }

    captureRate = Math.round(audioCtx.sampleRate || 48000);

    function useScriptProcessor(){
        if (scriptNode || !audioCtx || !source) return;
        // v83 ROOT FIX: worklet + ScriptProcessor dono chal rahe the =
        // har frame DO baar server ko jaata tha (atak/garbled, latency).
        // Fallback lene se pehle worklet ko poori tarah hatao.
        if (workletNode) { try{ workletNode.port.onmessage=null; workletNode.disconnect(); }catch(e){} workletNode=null; }
        scriptNode = audioCtx.createScriptProcessor(2048, 1, 1);
        scriptNode.onaudioprocess = (event) => {
            const input = event.inputBuffer.getChannelData(0);
            const out = new ArrayBuffer(input.length * 2);
            const view = new DataView(out);
            for (let i = 0; i < input.length; i++) {
                const v = Math.max(-1, Math.min(1, input[i]));
                view.setInt16(i * 2, v * 32767, true);
            }
            push(out, myAttempt);
        };
        source.connect(scriptNode);
        scriptNode.connect(silentSink);
    }

    silentSink = audioCtx.createGain();
    silentSink.gain.value = 0;
    silentSink.connect(audioCtx.destination);

    let worklet = false;
    if (audioCtx.audioWorklet && typeof AudioWorkletNode !== 'undefined') {
        try {
            await audioCtx.audioWorklet.addModule(
                URL.createObjectURL(new Blob([PCM_WORKLET], { type: 'application/javascript' })));
            workletNode = new AudioWorkletNode(audioCtx, 'pcm-processor');
            workletNode.port.onmessage = (e) => { if (!scriptNode) push(e.data, myAttempt); };
            source.connect(workletNode);
            workletNode.connect(silentSink);
            worklet = true;
        } catch (err) { console.warn('AudioWorkletNode unavailable; using ScriptProcessor', err); }
    }
    useScriptProcessorRef = useScriptProcessor;
    if (!worklet) useScriptProcessor();
    keepContextAlive();
    setTimeout(()=>{ if (attemptId===myAttempt && !firstFrameSeen) useScriptProcessor(); }, 1200);

    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 256;
    source.connect(analyser);
    const data = new Uint8Array(analyser.frequencyBinCount);
    const tdata = new Uint8Array(analyser.fftSize);
    function updateMeter() {
        if (!isOn || !analyser) return;
        analyser.getByteFrequencyData(data);
        try{ analyser.getByteTimeDomainData(tdata); vuUpdate(tdata);}catch(e){}
        let sum = 0;
        for (let i = 0; i < data.length; i++) sum += data[i];
        const lvl = Math.min(1, (sum / data.length) / 70);
        document.documentElement.style.setProperty('--lvl', lvl.toFixed(3));
        for (let i = 0; i < eqBars.length; i++) eqBars[i].style.height = (4 + (data[i*2+2]||0)/255*30) + 'px';
        drawViz(data);
        requestAnimationFrame(updateMeter);
    }

    isOn = true;
    lastFrameAt = Date.now();
    hideHelp();
    requestWakeLock();
    startWatchdog(myAttempt);
    updateMeter();
    setStatus('Mic ready — server se jud rahe hain...', 'connecting');
    tapEl.textContent = 'Tap to stop';
    btnEl.disabled = false;

    firstFrameTimer = setTimeout(()=>{
        if (attemptId===myAttempt && !firstFrameSeen)
            resetAudioState('Browser audio frames nahi bhej raha — page foreground mein rakhein','err');
    }, 20000);

    connectSocket(myAttempt);
}

// Screen lock / tab switch suspends the AudioContext on many phones.
function revive(){
    if (!isOn) return;
    if (audioCtx && audioCtx.state !== 'running') audioCtx.resume().catch(()=>{});
    if (!document.hidden) requestWakeLock();
    if ((!ws || ws.readyState===3) && !retryTimer) connectSocket(attemptId);
}
document.addEventListener('visibilitychange', revive);
window.addEventListener('pageshow', revive);
window.addEventListener('focus', revive);
window.addEventListener('online', revive);

function stopMic() {
    hideHelp();
    clearInterval(watchdogTimer); watchdogTimer=null;
    releaseWakeLock();
    resetAudioState('Mic OFF', 'off');
}
</script>
</body>
</html>"""


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
    """Clear owner and spare-relay mic flags left by a stale session."""
    await clear_stale_session(user_id)
    engines = [uvc]
    try:
        from helpers.vc_manager import session_manager
        relay = await session_manager.get_relay(user_id)
        if relay is not None and relay is not uvc:
            engines.append(relay)
    except Exception:
        pass

    seen = set()
    for engine in engines:
        for cid, st in list(getattr(engine, "chats", {}).items()):
            if id(st) in seen:
                continue
            seen.add(id(st))
            if not (getattr(st, "mic_enabled", False)
                    or getattr(st, "live_relay", False)):
                continue
            boost_user = getattr(st, "mic_boost_user_id", None)
            if boost_user and boost_user != user_id:
                continue
            if getattr(st, "mic_enabled", False):
                try:
                    await engine.stop_mic_boost(cid)
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
                                 "Permissions-Policy": "microphone=(self)"})


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
        existing._intentional_stop = False
        logger.info("Live mic socket reconnected for user %s", user_id)
        try:
            await ws.send_str("ready")
            await ws.send_str("settings:" + json.dumps(existing.public_settings()))
            asyncio.create_task(existing.send_relay_profile())
            await existing.run_ws_loop()
        finally:
            if existing._intentional_stop:
                await existing.stop(revoke_token=False)
            elif not existing._closed:
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
        session._intentional_stop = False
        # "ready" goes out immediately so the browser can ask for the mic while
        # the user's tap is still fresh; FFmpeg starts when the browser reports
        # its real sample rate (rate:<hz>) or sends its first PCM frame.
        await ws.send_str("ready")
        await ws.send_str("settings:" + json.dumps(session.public_settings()))
        asyncio.create_task(session.send_relay_profile())

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
        if session and session._intentional_stop:
            # Browser OFF is a clean pause, not revocation: the same private link
            # can start a fresh session when the user taps ON again.
            await session.stop(revoke_token=False)
        elif session and session._started and session._received_bytes:
            # A dropped page gets a reconnect window; after cleanup the same
            # private link remains valid unless Telegram explicitly revoked it.
            _schedule_grace_stop(session)
        elif session:
            await session.stop(revoke_token=False)
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
