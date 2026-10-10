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
import json
import logging
import os
import secrets
import shlex
import tempfile
import time
from typing import Dict, Optional

from aiohttp import web, WSMsgType

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
JITTER_MAX_MS = 800
# Playout starts only once this much audio is queued, and after an underrun
# we re-buffer back to it.  Without this every tiny network hiccup inserted
# 20 ms holes of silence mid-word = the choppy "atak atak" voice.
JITTER_TARGET_MS = 120
# Each underrun grows the prebuffer (adaptive) so a user on a bad network
# stutters once, not every few seconds.
JITTER_TARGET_MAX_MS = 400
# Beyond this, latency is creeping up: drop 1 sample per ~100 (inaudible).
JITTER_HIGH_MS = 360


# Loudness comes from the levelling + limiter stages, not from piling on
# pre-amp and turbo; the old 22 dB pregain + 4 dB turbo only squashed the
# voice flat before it ever reached the voice chat.
FIXED_BEST = {"volume": 1000, "gain": 400, "boost": 10, "bass": 0,
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
        }

    @staticmethod
    def sanitize_settings(data: dict) -> dict:
        limits = {
            "volume": (0, 2000), "bass": (0, 100), "treble": (0, 120),
            "gain": (0, 400), "boost": (0, 10), "echo_level": (0, 10),
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
                                     crunch=int(self.settings.get("crunch", 0) or 0))

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
        return _env("LIVE_MIC_OPEN_CEILING_DB", -1.0)

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
                fcntl.fcntl(self._proc_keeper_fd, 1031, 8192)  # F_SETPIPE_SZ
            except Exception:
                pass

        ffmpeg_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-y",
            # Keep probe minimal but non-zero — probesize=16 / analyzeduration=0
            # made FFmpeg's internal analyser overflow (ERANGE) and die instantly,
            # which then caused the 15 s FIFO-open timeout.
            "-fflags", "+nobuffer", "-flags", "low_delay",
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
                if playing and len(self._buf) > high and trim_tick >= 10:
                    # Trim latency very gently: only a few samples every
                    # 200 ms (inaudible) instead of whole 20 ms frames.
                    trim_tick = 0
                    cut = (size // 10) & ~1
                    del self._buf[:cut]
                    self._dropped_bytes += cut
                if self._pending:
                    chunk, self._pending = self._pending, b""
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
                        if getattr(p, "can_self_unmute", False):
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
        live = {k: v for k, v in changes.items() if k in ("loud", "crunch")}
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
                            await self.ensure_pipeline(int(float(text[5:])))
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

        await self._cleanup_resources()

        # Spare account ko VC me hi baitha rehne do (mic icon OFF, silence
        # stream) taaki wo normal user jaisa lage, bot jaisa aana-jaana nahi.
        if not self.shared_account and self.relay.calls is not None:
            if RELAY_STAY_IN_VC and not leave_vc:
                if not await park_relay(self.relay, self.chat_id):
                    logger.info("park failed, relay left VC %s", self.chat_id)
            else:
                await unpark_relay(self.relay, self.chat_id)

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
        return 10


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


LIVE_MIC_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<meta name="theme-color" content="#04050b">
<title>Apex VC Fyt Bot - Live Mic</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@700;900&family=Rajdhani:wght@500;600;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
:root{--bg:#04050b;--line:rgba(255,255,255,.08);--muted:#8b90a3;--acc:#22e57a;--acc2:#38bdf8;--hot:#ff3d5a;--amber:#ffb020;--lvl:0}
html,body{height:100%}
body{font-family:'Rajdhani',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);
color:#fff;min-height:100vh;overflow-x:hidden;position:relative}
/* animated backdrop */
.bg{position:fixed;inset:0;z-index:0;overflow:hidden;pointer-events:none}
.blob{position:absolute;border-radius:50%;filter:blur(70px);opacity:.55;animation:float 14s ease-in-out infinite}
.b1{width:420px;height:420px;background:#0ea5e9;top:-140px;left:-120px}
.b2{width:380px;height:380px;background:#16a34a;bottom:-120px;right:-120px;animation-delay:-5s}
.b3{width:260px;height:260px;background:#db2777;top:40%;left:60%;opacity:.3;animation-delay:-9s}
@keyframes float{0%,100%{transform:translate(0,0) scale(1)}50%{transform:translate(40px,30px) scale(1.12)}}
.grid{position:absolute;inset:0;background-image:linear-gradient(rgba(255,255,255,.035) 1px,transparent 1px),
linear-gradient(90deg,rgba(255,255,255,.035) 1px,transparent 1px);background-size:34px 34px;
mask-image:radial-gradient(circle at 50% 40%,#000 20%,transparent 75%);-webkit-mask-image:radial-gradient(circle at 50% 40%,#000 20%,transparent 75%)}
.star{position:absolute;width:3px;height:3px;border-radius:50%;background:#fff;opacity:0;animation:twinkle 4s infinite}
@keyframes twinkle{0%,100%{opacity:0}50%{opacity:.8}}
.wrap{position:relative;z-index:1;max-width:460px;margin:0 auto;padding:22px 16px 40px;display:flex;flex-direction:column;align-items:center;min-height:100vh}
header{text-align:center}
.badge{display:inline-flex;align-items:center;gap:6px;font-size:.72rem;letter-spacing:2px;text-transform:uppercase;
padding:5px 12px;border-radius:99px;border:1px solid var(--line);background:rgba(255,255,255,.04);color:var(--muted)}
.badge .dot{width:7px;height:7px;border-radius:50%;background:var(--muted)}
body.live .badge{color:#ff8fa0;border-color:rgba(255,61,90,.5);background:rgba(255,61,90,.08)}
body.live .badge .dot{background:var(--hot);box-shadow:0 0 10px var(--hot);animation:blink 1s infinite}
@keyframes blink{50%{opacity:.25}}
h1{font-family:'Orbitron',sans-serif;font-size:1.7rem;font-weight:900;letter-spacing:3px;margin-top:12px;
background:linear-gradient(90deg,#38bdf8,#22e57a,#d9f99d,#38bdf8);background-size:300% 100%;
-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;color:transparent;
text-transform:uppercase;animation:shine 6s linear infinite}
@keyframes shine{to{background-position:300% 0}}
.subtitle{color:var(--muted);font-size:.9rem;margin-top:4px;letter-spacing:1px}
/* mic orb */
.stage{position:relative;width:290px;height:290px;margin:26px 0 8px;display:grid;place-items:center}
.ring{position:absolute;inset:0;border-radius:50%;border:2px solid rgba(56,189,248,.25)}
.ring.r2{inset:22px;border-color:rgba(34,229,122,.22)}
.ring.r3{inset:44px;border-style:dashed;border-color:rgba(255,255,255,.12);animation:spin 18s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.pulse{position:absolute;inset:60px;border-radius:50%;border:2px solid var(--acc);opacity:0}
body.live .pulse{animation:pulse 2.2s ease-out infinite}
body.live .pulse.p2{animation-delay:.7s}body.live .pulse.p3{animation-delay:1.4s}
@keyframes pulse{0%{transform:scale(1);opacity:.8}100%{transform:scale(2.1);opacity:0}}
canvas#viz{position:absolute;inset:0;width:100%;height:100%}
.orb{position:relative;width:150px;height:150px;border-radius:50%;border:none;cursor:pointer;display:grid;place-items:center;
background:radial-gradient(circle at 35% 30%,#3b4256,#141824 70%);box-shadow:0 0 0 6px rgba(255,255,255,.04),0 20px 60px rgba(0,0,0,.6),inset 0 2px 8px rgba(255,255,255,.12);
transition:transform .15s,box-shadow .3s,background .3s;transform:scale(calc(1 + var(--lvl)*.12))}
.orb:active{transform:scale(.94)}
.orb svg{width:62px;height:62px;fill:#c7cbd6;transition:fill .3s}
body.live .orb{background:radial-gradient(circle at 35% 30%,#4ade80,#0e9f55 70%);
box-shadow:0 0 0 6px rgba(34,229,122,.18),0 0 calc(40px + var(--lvl)*80px) rgba(34,229,122,.7),inset 0 2px 8px rgba(255,255,255,.3)}
body.live .orb svg{fill:#03140a}
body.busy .orb{background:radial-gradient(circle at 35% 30%,#fcd34d,#d97706 70%);animation:breathe 1.2s ease-in-out infinite}
body.busy .orb svg{fill:#1c1204}
@keyframes breathe{50%{box-shadow:0 0 0 6px rgba(255,176,32,.2),0 0 60px rgba(255,176,32,.6)}}
.tap{font-family:'Orbitron',sans-serif;font-size:.8rem;letter-spacing:3px;color:var(--muted);text-transform:uppercase;margin-top:2px}
.status{margin-top:16px;padding:11px 18px;border-radius:14px;font-size:1rem;font-weight:700;text-align:center;letter-spacing:.5px;width:100%}
.status.off{background:rgba(255,255,255,.03);color:var(--muted);border:1px solid var(--line)}
.status.on{background:rgba(34,229,122,.1);color:#4ade80;border:1px solid rgba(34,229,122,.6);box-shadow:0 0 24px rgba(34,229,122,.25)}
.status.err{background:rgba(255,61,90,.1);color:#fb7185;border:1px solid rgba(255,61,90,.6)}
.status.connecting{background:rgba(255,176,32,.1);color:#fcd34d;border:1px solid rgba(255,176,32,.6)}
.eq{display:flex;gap:4px;align-items:flex-end;height:34px;margin-top:16px}
.eq i{display:block;width:6px;border-radius:3px;background:linear-gradient(#a3e635,#22e57a);height:4px;transition:height .08s}
.feat{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;width:100%;margin-top:22px}
.f{background:rgba(255,255,255,.04);border:1px solid var(--line);border-radius:16px;padding:12px 6px;text-align:center;backdrop-filter:blur(10px)}
.f b{display:block;font-family:'Orbitron',sans-serif;font-size:.78rem;letter-spacing:1px;margin-top:6px}
.f small{color:var(--muted);font-size:.72rem}
.f .ic{font-size:1.4rem}
.btn{width:100%;padding:14px;border-radius:14px;font-size:.95rem;font-weight:700;cursor:pointer;margin-top:12px;
background:rgba(255,255,255,.04);color:#dfe3ea;border:1px solid var(--line);font-family:'Rajdhani',sans-serif}
.btn:active{transform:scale(.97)}
.help{display:none;background:#1b1206;border:1px solid #b45309;color:#fcd34d;border-radius:14px;padding:12px;margin-top:12px;
font-size:.86rem;line-height:1.65;text-align:left;width:100%}
.help.show{display:block}.help b{color:#fff}.help ol{margin:6px 0 0 18px}.help li{margin:3px 0}
details{width:100%;margin-top:14px;background:rgba(255,255,255,.03);border:1px solid var(--line);border-radius:14px;padding:12px 14px}
summary{cursor:pointer;font-weight:700;letter-spacing:1px;color:#dfe3ea}
details p{color:var(--muted);font-size:.88rem;line-height:1.7;margin-top:8px}details p b{color:#fff}
.save{font-size:.8rem;color:var(--acc);min-height:16px;margin-top:8px;text-align:center}
.ctl{width:100%;margin-top:16px;background:rgba(255,255,255,.04);border:1px solid var(--line);border-radius:16px;padding:14px;backdrop-filter:blur(10px)}
.ctl .row{display:flex;justify-content:space-between;align-items:center;font-size:.86rem;letter-spacing:1px;color:#dfe3ea;margin-top:10px}
.ctl .row:first-child{margin-top:0}
.ctl .row b{font-family:'Orbitron',sans-serif;color:var(--acc);font-size:.86rem}
.ctl input[type=range]{-webkit-appearance:none;appearance:none;width:100%;height:6px;border-radius:99px;margin-top:8px;
background:linear-gradient(90deg,var(--acc2),var(--acc));outline:none}
.ctl input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:22px;height:22px;border-radius:50%;background:#fff;
box-shadow:0 2px 8px rgba(0,0,0,.6);cursor:pointer}
.ctl input[type=range]::-moz-range-thumb{width:22px;height:22px;border:none;border-radius:50%;background:#fff;cursor:pointer}
.ctl small{display:block;color:var(--muted);font-size:.74rem;margin-top:10px;line-height:1.5}
footer{margin-top:auto;padding-top:22px;color:#4b5064;font-size:.72rem;letter-spacing:2px;text-transform:uppercase}
</style>
</head>
<body>
<div class="bg"><div class="grid"></div><div class="blob b1"></div><div class="blob b2"></div><div class="blob b3"></div><div id="stars"></div></div>
<div class="wrap">
<header>
  <span class="badge"><span class="dot"></span><span id="badgeTxt">Offline</span></span>
  <h1>Apex VC Fyt</h1>
  <p class="subtitle">Live Mic &bull; Extreme Loud &bull; Crystal Clear</p>
</header>

<div class="stage">
  <canvas id="viz" width="580" height="580"></canvas>
  <div class="ring"></div><div class="ring r2"></div><div class="ring r3"></div>
  <div class="pulse"></div><div class="pulse p2"></div><div class="pulse p3"></div>
  <button id="toggleBtn" class="orb" onclick="toggleMic()" aria-label="Mic ON / OFF">
    <svg viewBox="0 0 24 24"><path d="M12 14a3 3 0 0 0 3-3V5a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.92V21h2v-3.08A7 7 0 0 0 19 11h-2z"/></svg>
  </button>
</div>
<div class="tap" id="tapTxt">Tap to go live</div>
<div class="eq" id="eq"></div>
<div id="status" class="status off">Mic OFF</div>
<div class="save" id="saveNote"></div>


<div class="ctl" id="liveCtl">
  <div class="row"><span>&#128266; VOLUME BOOST</span><b id="loudV">0</b></div>
  <input type="range" id="loudR" min="0" max="200" step="5" value="120">
  <div class="row"><span>&#128165; FATNA (DISTORTION)</span><b id="crunchV">0</b></div>
  <input type="range" id="crunchR" min="0" max="200" step="5" value="0">
  <small>Default 0 = best saaf awaaz. Fight me saamne wala tez ho to <b>VOLUME BOOST</b> badhayein; aur zyada tez + phati awaaz chahiye to <b>FATNA</b> bhi badhayein. <b>100 se upar = OVERDRIVE</b>: sabse zyada tez, awaaz fat sakti hai. Slider chhodte hi live lagta hai.</small>
</div>

<button id="retryBtn" class="btn" style="display:none" onclick="retryMic()">Permission dene ke baad — Dobara try karein</button>
<div id="permHelp" class="help"></div>

<div class="feat">
  <div class="f"><div class="ic">&#128266;</div><b>MAX</b><small>Extreme loud</small></div>
  <div class="f"><div class="ic">&#10022;</div><b>HD</b><small>Crystal clear</small></div>
  <div class="f"><div class="ic">&#128263;</div><b>ZERO</b><small>No khar-khar</small></div>
</div>

<button id="chromeBtn" class="btn" onclick="openInChrome()">Chrome me kholein (link copy ho jayega)</button>

<details>
  <summary>Kaise use karein</summary>
  <p>
  1. Group me pehle <b>Voice Chat start</b> karein.<br>
  2. Telegram me <b>.mic on</b> bhejein aur link kholein.<br>
  3. Beech wala <b>mic button</b> dabayein aur permission <b>Allow</b> karein.<br>
  4. Telegram app ka apna mic <b>mute</b> rakhein — awaaz isi page se jaati hai.<br>
  5. Awaaz kam lage to neeche <b>VOLUME BOOST</b> / <b>FATNA</b> slider badhayein.<br>
  6. Mic na chale to <b>Chrome me kholein</b> dabayein.<br>
  7. Rokne ke liye mic button dobara dabayein ya <b>.mic off</b> bhejein.
  </p>
</details>
<footer>Apex VC Fyt Bot &bull; Live</footer>
</div>

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
// LIVE controls: VOLUME BOOST + FATNA (distortion). Sent on release only.
const loudR=document.getElementById('loudR'), crunchR=document.getElementById('crunchR');
const loudV=document.getElementById('loudV'), crunchV=document.getElementById('crunchV');
function syncCtl(st){
  if(st && st.loud!=null){loudR.value=st.loud;loudV.textContent=st.loud;}
  if(st && st.crunch!=null){crunchR.value=st.crunch;crunchV.textContent=st.crunch;}
}
function sendCtl(){
  const p={loud:+loudR.value,crunch:+crunchR.value};
  if(ws && ws.readyState===1 && sendReady){try{ws.send('settings:'+JSON.stringify(p));}catch(e){}}
}
loudR.addEventListener('input',()=>{loudV.textContent=loudR.value;});
crunchR.addEventListener('input',()=>{crunchV.textContent=crunchR.value;});
loudR.addEventListener('change',sendCtl);
crunchR.addEventListener('change',sendCtl);

// decoration: stars + eq bars
(function(){const s=document.getElementById('stars');for(let i=0;i<40;i++){const d=document.createElement('i');d.className='star';
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
    const g=vctx.createLinearGradient(x1,y1,x2,y2);g.addColorStop(0,'#22e57a');g.addColorStop(1,v>.7?'#ff3d5a':'#38bdf8');
    vctx.strokeStyle=g;vctx.lineWidth=6;vctx.lineCap='round';vctx.beginPath();vctx.moveTo(x1,y1);vctx.lineTo(x2,y2);vctx.stroke();
  }
}
drawViz(null);

function setStatus(t,c){statusEl.textContent=t;statusEl.className='status '+c;
  document.body.classList.toggle('live',c==='on');document.body.classList.toggle('busy',c==='connecting');
  badgeEl.textContent=c==='on'?'Live':(c==='connecting'?'Connecting':'Offline');}

function closeSocket(sendStop){
  if(window._wsPingInterval){clearInterval(window._wsPingInterval);window._wsPingInterval=null;}
  const oldWs=ws; ws=null; sendReady=false;
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
  setStatus(message,cls); tapEl.textContent='Tap to go live'; btnEl.disabled=false;
}
const helpEl=document.getElementById('permHelp');
const retryEl=document.getElementById('retryBtn');
const IS_ANDROID=/Android/i.test(navigator.userAgent);
const IN_TELEGRAM=/Telegram/i.test(navigator.userAgent)||!!window.TelegramWebview||!!window.TelegramWebviewProxy;

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
            // Do not send settings automatically on connect.  The server-side
            // settings path rebuilds FFmpeg; doing that after first audio used
            // to create an audible gap and could make the relay look dead on
            // slower phones. Explicit preset/reset actions still send changes.
        } else if (ev.data.startsWith('settings:')) {
            try { const st=JSON.parse(ev.data.slice(9)); syncCtl(st); } catch(e){}
        } else if (ev.data.startsWith('settings_failed:')) {
            /* ignored */
        } else if (ev.data.startsWith('warn:')) {
            setStatus(ev.data.slice(5), 'err');      // keep streaming
        } else if (ev.data.startsWith('ok:')) {
            setStatus(ev.data.slice(3), 'on');
        } else if (ev.data.startsWith('error:')) {
            socket._fatal = true;
            resetAudioState(ev.data.slice(6), 'err');
        }
    };

    socket.onerror = () => {};

    socket.onclose = () => {
        if (attemptId!==myAttempt || socket!==ws || socket._fatal) return;
        if (window._wsPingInterval) { clearInterval(window._wsPingInterval); window._wsPingInterval=null; }
        sendReady = false;
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
        audioCtx = new (window.AudioContext||window.webkitAudioContext)();
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
            const hp = audioCtx.createBiquadFilter(); hp.type='highpass'; hp.frequency.value=80; hp.Q.value=0.707;
            const pre = audioCtx.createGain(); pre.gain.value = 14.0; // +23 dB: phone mic (AGC off) is very quiet; limiter below stops clipping
            const lim = audioCtx.createDynamicsCompressor();
            lim.threshold.value=-3; lim.knee.value=0; lim.ratio.value=20;
            lim.attack.value=0.002; lim.release.value=0.06;
            source.connect(hp); hp.connect(pre); pre.connect(lim);
            nPre = pre; nComp = lim;
            source = lim;
        } catch(e){ nComp=nPre=null; }
    } catch (e) {
        const friendly = micErrorText(e);
        resetAudioState(friendly, 'err');
        return;
    }

    captureRate = Math.round(audioCtx.sampleRate || 48000);

    function useScriptProcessor(){
        if (scriptNode || !audioCtx || !source) return;
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
            workletNode.port.onmessage = (e) => push(e.data, myAttempt);
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
    function updateMeter() {
        if (!isOn || !analyser) return;
        analyser.getByteFrequencyData(data);
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


# Public base URL learned from real incoming requests.  Heroku app domains now
# carry a random suffix, so the guessed "<app>.herokuapp.com" URL is often wrong
# ("There's nothing here, yet." / "No such app").  As soon as anyone opens the
# app on its real domain, we remember it and use it for every future mic link.
_detected_base_url: str = ""


BASE_URL_DB_KEY = "live_mic_base_url"


async def _persist_base_url(base: str) -> None:
    try:
        from helpers.database import db
        await db.set_app_value(BASE_URL_DB_KEY, base)
    except Exception as exc:
        logger.debug("base url persist failed: %s", exc)


def _remember_base_url(request: web.Request) -> None:
    global _detected_base_url
    host = (request.headers.get("X-Forwarded-Host")
            or request.headers.get("Host") or "").split(",")[0].strip()
    if not host or host.startswith(("localhost", "127.0.0.1", "0.0.0.0")):
        return
    proto = (request.headers.get("X-Forwarded-Proto") or "https").split(",")[0].strip() or "https"
    base = f"{proto}://{host}".rstrip("/")
    if base != _detected_base_url:
        _detected_base_url = base
        logger.info("Live mic public base URL detected: %s", base)
        # Dyno restarts wipe the in-memory value and Heroku domains carry a
        # random suffix, so remember the real host across restarts — otherwise
        # `.mic on` cannot build a link until someone opens the page again.
        try:
            asyncio.get_running_loop().create_task(_persist_base_url(base))
        except RuntimeError:
            pass


async def load_base_url() -> str:
    """Restore the last known public URL at startup (called from main)."""
    global _detected_base_url
    if _detected_base_url:
        return _detected_base_url
    try:
        from helpers.database import db
        stored = (await db.get_app_value(BASE_URL_DB_KEY) or "").strip()
    except Exception:
        stored = ""
    from config import Config
    # An explicit env override always wins over a stale stored host.
    override = (Config._LIVE_MIC_OVERRIDE or "").strip()
    if override:
        return ""
    if stored.startswith("http"):
        _detected_base_url = stored.rstrip("/")
        logger.info("Live mic public base URL restored: %s", _detected_base_url)
        return _detected_base_url

    # Last resort: the "<app>.herokuapp.com" guess.  Modern Heroku domains have
    # a random suffix, so that guess usually opens "There's nothing here, yet."
    # Verify it once; a dead URL is worse than no link at all.
    guess = (Config.LIVE_MIC_BASE_URL or "").strip().rstrip("/")
    if guess and await _url_is_ours(guess):
        _detected_base_url = guess
        await _persist_base_url(guess)
        logger.info("Live mic public base URL verified: %s", guess)
    elif guess:
        logger.warning(
            "Guessed live mic URL %s is not reachable — set LIVE_MIC_BASE_URL "
            "to the app's real https URL.", guess)
    return _detected_base_url


async def _url_is_ours(base: str) -> bool:
    """True when <base>/health is served by this bot."""
    try:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.get(f"{base}/health") as resp:
                if resp.status != 200:
                    return False
                text = await resp.text()
                return "vcfyt" in text.lower()
    except Exception as exc:
        logger.debug("base url probe failed for %s: %s", base, exc)
        return False


def public_base_url() -> str:
    """Base URL used to build live mic links (detected host wins)."""
    from config import Config
    return _detected_base_url or Config.LIVE_MIC_BASE_URL or ""


async def resolve_base_url() -> str:
    """Same as public_base_url(), but also consults the stored host."""
    return public_base_url() or await load_base_url() or ""


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
    return web.Response(text=LIVE_MIC_HTML, content_type="text/html")


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


async def _handle_health(request: web.Request) -> web.Response:
    _remember_base_url(request)
    return web.Response(text="vcfyt bot is running\n", content_type="text/plain")


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
    logger.info("Live mic web server ready on port %s", port)
    return runner


async def stop_server():
    global _runner
    if _runner:
        for session in list(_sessions.values()):
            await session.stop()
        await _runner.cleanup()
        _runner = None
