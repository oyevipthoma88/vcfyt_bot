
import asyncio
import inspect
import logging
import os
import random
import time
from collections import OrderedDict
from typing import Dict, Optional

from pyrogram import Client
from pyrogram.raw.functions.channels import GetFullChannel
from pyrogram.raw.functions.messages import GetFullChat
from pyrogram.raw.functions.phone import CreateGroupCall, EditGroupCallParticipant
from pyrogram.raw.types import (
    InputPeerChannel, InputPeerChat, UpdateGroupCall,
    UpdateGroupCallParticipants,
)
from pytgcalls import PyTgCalls
from pytgcalls import filters as call_filters
from pytgcalls.exceptions import NoActiveGroupCall
from pytgcalls.types import (AudioQuality, ChatUpdate, GroupCallConfig, MediaStream,
                             StreamEnded, UpdatedGroupCallParticipant)

from config import Config
from helpers.audio_processor import (process_audio_to_file, build_stream_command,
                                      build_fake_screen_command)
from helpers.logger_channel import (
    get_bot, log_auto_mode, log_error, log_live_boost, log_vc_join, log_vc_leave,
)
from helpers.peer_guard import ensure_peer, is_peer_error

logger = logging.getLogger("vcbot.vc_manager")

VOL_NORMAL = 10000
VOL_MAX = 20000

FYT_PARTICIPANT_VOLUME = VOL_MAX  # 20000 = 200% Telegram hard cap

AUTO_PRESET = {
    "volume": 1000, "relay_volume": 1000, "bass": 40, "gain": 400,
    "treble": 100, "boost": 10, "echo": 0, "echo_level": 0,
}

def _db():
    from helpers.database import db
    return db

def _unlink(path: Optional[str]):
    if path and os.path.exists(path):
        try:
            os.unlink(path)
        except OSError:
            pass

_DURATIONS: "OrderedDict[str, float]" = OrderedDict()


def _probe_duration(path: str) -> float:
    """Track length in seconds via ffprobe (cached, 0.0 on failure)."""
    if not path:
        return 0.0
    if path in _DURATIONS:
        return _DURATIONS[path]
    dur = 0.0
    try:
        import subprocess
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=8,
        ).stdout.strip()
        dur = float(out or 0)
    except Exception:
        dur = 0.0
    _DURATIONS[path] = dur
    while len(_DURATIONS) > 64:
        _DURATIONS.popitem(last=False)
    return dur


def _cleanup_temp_files():
    """Remove old processed-audio temp files (R14 / disk-fill guard)."""
    try:
        import glob as _glob
        import tempfile as _tf
        temp_dir = _tf.gettempdir()
        now = time.time()
        for f in _glob.glob(os.path.join(temp_dir, "vc_processed_*")):
            try:
                if now - os.path.getmtime(f) > 600:
                    os.unlink(f)
            except OSError:
                pass
    except Exception:
        pass

def _vc_join_missing(error: Exception) -> bool:
    text = str(error).upper()
    return (type(error).__name__ in ("GroupcallJoinMissing", "ParticipantJoinMissing")
            or "GROUPCALL_JOIN_MISSING" in text or "PARTICIPANT_JOIN_MISSING" in text)


_SILENCE_PATH = None


def _silence_file() -> str:
    """30 min silent audio (tiny opus file) used to sit in the VC quietly."""
    global _SILENCE_PATH
    if _SILENCE_PATH and os.path.exists(_SILENCE_PATH):
        return _SILENCE_PATH
    import subprocess, tempfile
    path = os.path.join(tempfile.gettempdir(), "vc_silence.ogg")
    if not os.path.exists(path):
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
             "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "1800",
             "-c:a", "libopus", "-b:a", "8k", path],
            check=True, timeout=60,
        )
    _SILENCE_PATH = path
    return path


def _participant_not_joined(error: Exception) -> bool:
    return (type(error).__name__ == "ParticipantJoinMissing" or
            "PARTICIPANT_JOIN_MISSING" in str(error))

def _connection_lost(error: Exception) -> bool:
    text = str(error).lower()
    return isinstance(error, (OSError, ConnectionError)) and any(
        marker in text for marker in ("connection lost", "connection reset", "broken pipe", "eof")
    )

def _client_disconnected(error: Exception) -> bool:
    name = type(error).__name__
    text = str(error).lower()
    return (
        name == "MTProtoClientNotConnected"
        or "not connected" in text
        or "has not been started" in text
    )

_INVALID_SESSION_NAMES = frozenset({
    "AuthKeyUnregistered", "AuthKeyInvalid", "SessionRevoked",
    "SessionExpired", "UserDeactivated", "Unauthorized",
    "AuthKeyDuplicated",
})

def _is_invalid_session(error: Exception) -> bool:
    return (type(error).__name__ in _INVALID_SESSION_NAMES or
            "401" in str(error) or "AUTH_KEY_DUPLICATED" in str(error))

def process_memory_mb() -> float:
    """Resident memory of this process in MB (0.0 when unavailable)."""
    try:
        with open("/proc/self/statm", "r") as handle:
            pages = int(handle.read().split()[1])
        return pages * (os.sysconf("SC_PAGE_SIZE") / (1024 * 1024))
    except Exception:
        return 0.0

async def _invalidate_session(user_id: int, error: Exception, source: str):
    try:
        await _db().clear_string(user_id)
    except Exception as clear_error:
        logger.warning("Could not clear invalid session %s: %s", user_id, clear_error)
    logger.warning(
        "Session %s needs a fresh login (%s: %s)",
        user_id, type(error).__name__, source,
    )

class ChatState:

    def __init__(self):
        self.is_playing = False
        self.is_paused = False
        self.current_file: Optional[str] = None
        self.processed_file: Optional[str] = None
        self.source_name = "—"
        self.chat_title = ""
        self.volume = Config.DEFAULT_VOLUME
        self.bass = Config.DEFAULT_BASS
        self.echo = Config.DEFAULT_ECHO
        self.echo_level = Config.DEFAULT_ECHO_LEVEL
        self.boost = Config.DEFAULT_BOOST
        self.relay_volume = Config.RELAY_DEFAULT_VOLUME
        self.gain = Config.RELAY_DEFAULT_GAIN
        self.treble = Config.RELAY_DEFAULT_TREBLE
        self.voice = "normal"
        self.live_volume = Config.LIVE_BOOST_DEFAULT
        self.mic_enabled = False
        self.mic_boost_user_id: Optional[int] = None
        self.live_relay = False
        self.auto = Config.AUTO_MODE_DEFAULT
        self.loop = False
        self.loop_left = -1
        self.queue: list = []
        self.unmute_audio: Optional[str] = None
        self.admin_muted: bool = False
        # --- human-like mute/unmute behaviour ---
        self.hand_raise: bool = Config.HAND_RAISE_DEFAULT
        self.hand_raised: bool = False
        self.mic_blink: bool = Config.MIC_BLINK_DEFAULT
        self.mic_blink_secs: int = Config.MIC_BLINK_SECONDS
        self.unmute_loop: int = 1
        self.unmute_loop_left: int = 1
        self.playing_unmute: bool = False
        self.resume_file: Optional[str] = None
        self.resume_processed: Optional[str] = None
        self.resume_name: Optional[str] = None
        self.resume_pos: float = 0.0
        self.resume_is_old: bool = False
        self.unmute_flow: bool = False
        self.mute_task = None
        # --- loudness / instant play / screen share ---
        self.loud_db: int = 0
        self.native_loop: bool = False
        self.ss_on: bool = False
        self.ss_image: Optional[str] = None
        self.ss_title: str = "Audio Setup — Live"
        # "live" = real VC participants grid (default), "image" = replied
        # photo, "mixer" = old PC mixer picture.
        self.ss_mode: str = os.environ.get("SS_MODE", "live")
        self.ss_task = None

    def apply_settings(self, s: dict):
        self.volume = int(s.get("volume", Config.DEFAULT_VOLUME))
        self.relay_volume = int(s.get("relay_volume", self.volume))
        self.bass = int(s.get("bass", Config.DEFAULT_BASS))
        self.gain = int(s.get("gain", Config.RELAY_DEFAULT_GAIN))
        self.treble = int(s.get("treble", Config.RELAY_DEFAULT_TREBLE))
        self.voice = s.get("voice", "normal")
        self.live_volume = int(s.get("live_volume", Config.LIVE_BOOST_DEFAULT))
        self.echo = bool(s.get("echo", Config.DEFAULT_ECHO))
        self.echo_level = int(s.get("echo_level", Config.DEFAULT_ECHO_LEVEL))
        self.boost = int(s.get("boost", Config.DEFAULT_BOOST))

    def settings(self) -> dict:
        return {
            "volume": self.volume, "bass": self.bass, "echo": self.echo,
            "echo_level": self.echo_level, "boost": self.boost,
            "relay_volume": self.relay_volume, "gain": self.gain,
            "treble": self.treble, "voice": self.voice,
            "live_volume": self.live_volume,
            "mic_enabled": self.mic_enabled, "mic_boost_user_id": self.mic_boost_user_id,
            "auto": self.auto, "loop": self.loop,
        }

class UserVC:

    def __init__(self, owner_id: int, string_session: str, label: str = "uvc"):
        self.owner_id = owner_id
        self.label = label
        self.string_session = string_session
        self.client: Optional[Client] = None
        self.calls: Optional[PyTgCalls] = None
        self.account_id: int = 0
        self.account_name: str = ""
        self.account_username: str = ""
        self.chats: Dict[int, ChatState] = {}
        self._keepers: Dict[int, asyncio.Task] = {}
        self._call_chats: Dict[int, int] = {}

        self._stopped_chats: set[int] = set()
        self._lock = asyncio.Lock()
        self._reconnect_lock = asyncio.Lock()
        self.live_volume = Config.LIVE_BOOST_DEFAULT
        self.last_active: float = time.monotonic()

    async def start(self):
        self.client = Client(
            f"{self.label}_{self.owner_id}",
            api_id=Config.API_ID,
            api_hash=Config.API_HASH,
            session_string=self.string_session,
            in_memory=True,
        )
        await self.client.start()
        me = await self.client.get_me()
        self.account_id = me.id
        self.account_name = me.first_name or ""
        self.account_username = me.username or ""
        try:
            saved = await _db().get_settings(self.owner_id)
            self.live_volume = int(saved.get("live_volume", Config.LIVE_BOOST_DEFAULT))
        except Exception:
            pass

        self._guard_update_loop()
        self.calls = PyTgCalls(self.client)

        @self.calls.on_update(call_filters.stream_end())
        async def _on_end(_, update: StreamEnded):
            # The fake screen share is a separate (video) track; only the
            # microphone track ending means the recording finished.
            if getattr(update, "stream_type", None) == StreamEnded.Type.VIDEO:
                return
            await self._on_stream_end(update.chat_id)

        @self.calls.on_update(call_filters.call_participant())
        async def _on_participant(_, update: UpdatedGroupCallParticipant):
            try:
                part = update.participant
                if part.user_id != self.account_id:
                    return
                self.apply_mute_state(update.chat_id, bool(part.muted_by_admin))
            except Exception as exc:
                logger.debug("participant update failed: %r", exc)

        gone = (ChatUpdate.Status.LEFT_GROUP | ChatUpdate.Status.KICKED
                | ChatUpdate.Status.CLOSED_VOICE_CHAT)

        @self.calls.on_update(call_filters.chat_update(gone))
        async def _on_left(_, update: ChatUpdate):
            self._stop_keeper(update.chat_id)
            st = self.chats.pop(update.chat_id, None)
            if st:
                for item in st.queue:
                    _unlink(item[0])
                    if len(item) > 2:
                        _unlink(item[2])
                _unlink(st.processed_file)
                _unlink(st.current_file)
                _unlink(st.unmute_audio)

        @self.client.on_raw_update()
        async def _on_raw_update(client, update, users, chats):
            await self._handle_group_call_update(update, users, chats)

        await self.calls.start()
        return self

    def _guard_update_loop(self):
        """Stop a client whose auth key Telegram revoked.

        Pyrogram's handle_updates task raised AuthKeyUnregistered forever
        ("Task exception was never retrieved" in the logs).  The dead client
        stayed in memory, kept its update loop alive and slowly pushed the
        dyno over its memory quota.  Now the session is cleared from the DB
        and the client is shut down the first time it happens.
        """
        client = self.client
        original = client.handle_updates

        async def guarded(updates):
            try:
                return await original(updates)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if _is_invalid_session(error):
                    logger.warning(
                        "Session %s revoked by Telegram (%s) — shutting it down",
                        self.owner_id, type(error).__name__,
                    )
                    asyncio.create_task(self._self_destruct(error))
                    return None
                logger.debug("update handling error: %r", error)
                return None

        client.handle_updates = guarded

    async def _self_destruct(self, error: Exception):
        """Clear a revoked session and free everything it held."""
        if self.label == "relay":
            try:
                await self.stop()
            except Exception:
                pass
            session_manager.assistants.pop(
                SessionManager._assistant_key(self.string_session), None)
            return
        await _invalidate_session(self.owner_id, error, "update loop")
        try:
            await session_manager.remove(self.owner_id)
        except Exception:
            try:
                await self.stop()
            except Exception:
                pass

    async def release_other_chats(self, keep_chat_id: int, also_keep=()):
        """Leave every voice chat except keep_chat_id.

        Telegram allows one group call per account.  A relay/spare account
        that is still parked in an older group call silently keeps the audio
        there — that is why a freshly given chat ID sometimes ended up
        streaming into the previous group.
        """
        keep = {keep_chat_id, *list(also_keep or ())}
        for other in [cid for cid in list(self.chats) if cid not in keep]:
            try:
                await self.leave(other, reason="Switched to another group")
            except Exception:
                self.chats.pop(other, None)
        if self.calls is not None:
            # NOTE: on current py-tgcalls `PyTgCalls.calls` is an async property,
            # so reading it returns a coroutine (never iterable).  Await it when
            # that is the case instead of iterating a coroutine object.
            active = getattr(self.calls, "calls", None)
            if inspect.isawaitable(active):
                try:
                    active = await active
                except Exception:
                    active = None
            try:
                other_ids = [cid for cid in list(active or {}) if cid not in keep]
            except TypeError:
                other_ids = []
            for other in other_ids:
                try:
                    await self.calls.leave_call(other)
                except Exception:
                    pass

    async def _handle_group_call_update(self, update, users, chats):
        """Detect VC join (auto-boost) and admin mute (pause + stop live mic)."""
        try:
            if isinstance(update, UpdateGroupCall):
                if not Config.AUTO_LIVE_BOOST:
                    return
                chat_id = getattr(update, "chat_id", None)
                if chat_id is None:
                    return
                call = getattr(update, "call", None)
                if call is None:
                    return
                st = self.chats.get(chat_id)
                if st and st.is_playing:
                    return
                boosted = await self._try_auto_boost(chat_id)
                if boosted:
                    logger.info(
                        "Auto live mic boost applied for user %s in chat %s",
                        self.owner_id, chat_id,
                    )
                return

            if isinstance(update, UpdateGroupCallParticipants):
                # Fallback for the pytgcalls participant update.  The update
                # carries a *list* and a call id (no chat), and "admin muted"
                # means muted AND not allowed to self-unmute.
                call_id = getattr(getattr(update, "call", None), "id", None)
                chat_id = self._call_chats.get(call_id)
                if chat_id is None:
                    return
                for participant in getattr(update, "participants", None) or []:
                    peer = getattr(participant, "peer", None)
                    try:
                        if not getattr(participant, "muted", False):
                            self.__dict__.setdefault("_speak_ts", {})[
                                (chat_id, getattr(peer, "user_id", None))] = time.time()
                    except Exception:
                        pass
                    if getattr(peer, "user_id", None) != self.account_id:
                        continue
                    by_admin = bool(getattr(participant, "muted", False)) and \
                        not bool(getattr(participant, "can_self_unmute", False))
                    self.apply_mute_state(chat_id, by_admin)
        except Exception:
            pass

    async def _try_auto_boost(self, chat_id: int) -> bool:
        """Apply max mic volume to the logged-in account in a VC it joined."""
        if not Config.AUTO_LIVE_BOOST:
            return False
        try:
            st = self.state(chat_id)
            target_vol = st.live_volume or FYT_PARTICIPANT_VOLUME
            for delay in (0.0, 0.3, 0.8, 1.5, 2.5):
                if delay:
                    await asyncio.sleep(delay)
                if await self.set_participant_volume(
                    chat_id, self.account_id, target_vol, quiet=True,
                ):
                    if chat_id not in self._keepers:
                        self._start_keeper(chat_id)
                    asyncio.create_task(log_live_boost(
                        self.owner_id, chat_id, self.account_id, target_vol,
                    ))
                    return True
        except Exception:
            pass
        return False

    async def stop(self):
        for t in list(self._keepers.values()):
            t.cancel()
        self._keepers.clear()
        for cid in list(self.chats):
            try:
                await self.leave(cid, reason="Session stopped")
            except Exception:
                pass
        try:
            if self.client:
                await self.client.stop()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # ADMIN MUTE / UNMUTE BEHAVIOUR  (acts like a real human participant)
    # ------------------------------------------------------------------

    async def _edit_self(self, chat_id: int, muted: bool = None,
                         raise_hand: bool = None, volume: int = None) -> bool:
        """Raw EditGroupCallParticipant on our own participant row."""
        try:
            call_input = await self._call_input(chat_id)
            if not call_input:
                return False
            peer = await self.client.resolve_peer(self.account_id)
            kwargs = {}
            if muted is not None:
                kwargs["muted"] = bool(muted)
            if raise_hand is not None:
                kwargs["raise_hand"] = bool(raise_hand)
            if volume is not None:
                kwargs["volume"] = int(volume)
            await self.client.invoke(EditGroupCallParticipant(
                call=call_input, participant=peer, **kwargs))
            return True
        except Exception as exc:
            logger.debug("edit_self failed in %s: %r", chat_id, exc)
            return False

    async def raise_hand(self, chat_id: int, raised: bool = True) -> bool:
        """Raise / lower the hand — Telegram's own way for a muted member to
        ask an admin for the mic back."""
        ok = False
        for delay in (0.0, 0.3, 0.7, 1.5):
            if delay:
                await asyncio.sleep(delay)
            if await self._edit_self(chat_id, raise_hand=raised):
                ok = True
                break
        st = self.chats.get(chat_id)
        if st is not None:
            st.hand_raised = bool(raised and ok)
        if ok:
            logger.info("Hand %s in chat %s", "raised" if raised else "lowered",
                        chat_id)
        return ok

    async def _mic_blink(self, chat_id: int, seconds: int):
        """Mic check after an admin gives the mic back: toggle the mic
        on/off at maximum speed for ``seconds`` (default 5) and leave it ON."""
        seconds = max(1, min(60, int(seconds or 5)))
        loop = asyncio.get_event_loop()
        end = loop.time() + seconds
        logger.info("Mic blink for %s s in chat %s", seconds, chat_id)
        on = False
        while loop.time() < end:
            on = not on
            await self._edit_self(chat_id, muted=not on)
            await asyncio.sleep(0.05)
        await self._edit_self(chat_id, muted=False)

    async def _self_unmute(self, chat_id: int):
        """Telegram's admin "unmute" only *allows* speaking — the member still
        has to switch the mic on.  Always do that, otherwise nobody hears us."""
        for delay in (0.0, 0.8, 2.0):
            if delay:
                await asyncio.sleep(delay)
            if await self._edit_self(chat_id, muted=False):
                break
        for fn_name in ("unmute",):
            fn = getattr(self.calls, fn_name, None)
            if fn:
                try:
                    await fn(chat_id)
                except Exception:
                    pass

    async def _current_position(self, chat_id: int) -> float:
        """Seconds into the current track (loop-aware), 0 if unknown."""
        st = self.chats.get(chat_id)
        if not st or not st.current_file:
            return 0.0
        try:
            played = float(await self.calls.time(chat_id) or 0)
        except Exception:
            return 0.0
        if played > 100000:  # some builds report ms
            played /= 1000.0
        if getattr(st, "native_loop", False):
            dur = _probe_duration(st.current_file)
            if dur > 1:
                played = played % dur
        return max(0.0, played)

    def apply_mute_state(self, chat_id: int, muted_by_admin: bool):
        """Single entry point for admin mute / unmute transitions (idempotent;
        fed by both the pytgcalls participant update and the raw fallback)."""
        st = self.chats.get(chat_id)
        if not st:
            return
        if muted_by_admin and not st.admin_muted:
            st.admin_muted = True
            logger.info("Admin muted bot in chat %s", chat_id)
            self._run_mute_task(st, self.handle_admin_mute(chat_id))
        elif not muted_by_admin and st.admin_muted:
            st.admin_muted = False
            logger.info("Admin unmuted bot in chat %s", chat_id)
            self._run_mute_task(st, self.handle_admin_unmute(chat_id))

    def _run_mute_task(self, st, coro):
        old = getattr(st, "mute_task", None)
        if old and not old.done():
            old.cancel()
        st.mute_task = asyncio.create_task(coro)

    async def handle_admin_mute(self, chat_id: int):
        st = self.state(chat_id)
        st.unmute_flow = False
        if st.hand_raise:
            asyncio.create_task(self.raise_hand(chat_id, True))
        # Remember where the recording was, so it continues from there later.
        if st.playing_unmute:
            # Muted again in the middle of the announcement: stop it, the
            # parked recording stays parked.
            st.playing_unmute = False
        elif st.is_playing and st.current_file:
            st.resume_pos = await self._current_position(chat_id)
        if st.is_playing and not st.is_paused:
            try:
                await self.calls.pause(chat_id)
                st.is_paused = True
            except Exception:
                pass
        try:
            from helpers.live_mic import is_active_for_chat, stop_session
            if is_active_for_chat(chat_id):
                asyncio.create_task(stop_session(self.owner_id))
        except Exception:
            pass
        if st.hand_raise and st.admin_muted and not st.hand_raised:
            await self.raise_hand(chat_id, True)  # retry if the instant one failed

    async def handle_admin_unmute(self, chat_id: int):
        st = self.state(chat_id)
        st.unmute_flow = True
        try:
            st.hand_raised = False  # Telegram lowers the hand by itself
            if st.mic_blink:
                try:
                    await self._mic_blink(chat_id, st.mic_blink_secs)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.debug("mic blink failed: %r", exc)
            await self._self_unmute(chat_id)
            if st.admin_muted:
                return
            if await self.play_unmute_audio(chat_id):
                return
            await self._resume_after_mute(chat_id)
        finally:
            st.unmute_flow = False

    async def _resume_after_mute(self, chat_id: int) -> bool:
        """Give the VC back: user's newer recording > old recording > queue."""
        st = self.state(chat_id)
        resume = st.resume_file
        name = st.resume_name or "audio"
        pos = float(st.resume_pos or 0) if st.resume_is_old else 0.0
        st.resume_file = None
        st.resume_processed = None
        st.resume_pos = 0.0
        st.resume_is_old = False
        if resume and os.path.exists(resume):
            await self._stream(chat_id, resume, name, start_at=pos)
            logger.info("Resumed recording in chat %s at %.1fs", chat_id, pos)
            return True
        if st.is_paused and st.current_file:
            # Nothing was parked: continue the paused track where it was.
            try:
                await self.calls.resume(chat_id)
                st.is_paused = False
                return True
            except Exception:
                pass
        if st.queue:
            item = st.queue.pop(0)
            await self._stream(chat_id, item[0], item[1],
                               preprocessed=item[2] if len(item) > 2 else None)
            self._preprocess_next(chat_id)
            return True
        return False

    async def play_unmute_audio(self, chat_id: int) -> bool:
        """Play the `.playmute` announcement, keeping the old recording aside."""
        st = self.state(chat_id)
        path = st.unmute_audio
        if not path or not os.path.exists(path):
            return False
        if st.current_file and st.current_file != path and not st.resume_file:
            st.resume_file = st.current_file
            st.resume_processed = st.processed_file
            st.resume_name = st.source_name
            st.resume_is_old = True
        # Hide them from _stream so it cannot delete the parked recording.
        st.current_file = None
        st.processed_file = None
        count = int(getattr(st, "unmute_loop", 1) or 1)
        st.unmute_loop_left = -1 if count < 0 else max(1, count)
        st.playing_unmute = True
        track_loop, track_left = st.loop, st.loop_left
        st.loop, st.loop_left = False, -1  # announcement has its own loop logic
        try:
            await self._stream(chat_id, path, "Unmute Audio")
        except Exception as exc:
            st.playing_unmute = False
            st.current_file = st.resume_file
            st.processed_file = st.resume_processed
            st.resume_file = st.resume_processed = None
            st.loop, st.loop_left = track_loop, track_left
            logger.warning("Unmute audio failed for %s: %r", chat_id, exc)
            return False
        st.loop, st.loop_left = track_loop, track_left
        return True

    async def _after_unmute_audio(self, chat_id: int) -> bool:
        """Announcement finished: loop it again, or give the VC back."""
        st = self.chats.get(chat_id)
        if not st:
            return False
        left = int(getattr(st, "unmute_loop_left", 1) or 1)
        path = st.unmute_audio
        # An infinite announcement still yields as soon as the user gives a
        # new recording.
        pending_new = bool(st.resume_file and not st.resume_is_old)
        if ((left < 0 and not pending_new) or left > 1) and path \
                and os.path.exists(path):
            if left > 1:
                st.unmute_loop_left = left - 1
            st.current_file = None
            st.processed_file = None
            await self._stream(chat_id, path, "Unmute Audio")
            return True

        st.playing_unmute = False
        # The announcement file belongs to `.playmute` — never delete it.
        st.current_file = None
        st.processed_file = None
        return await self._resume_after_mute(chat_id)

    def mute_busy(self, chat_id: int) -> bool:
        st = self.chats.get(chat_id)
        return bool(st and (st.admin_muted or st.unmute_flow or st.playing_unmute))

    async def park_recording(self, chat_id: int, path: str, source_name: str,
                             enqueue: bool) -> str:
        """User sent a recording while muted / during the unmute announcement."""
        st = self.state(chat_id)
        if enqueue:
            st.queue.append((path, source_name, None))
            return "queued"
        # A newer recording replaces whatever was parked.
        if st.resume_file and st.resume_file != path and \
                st.resume_file != st.unmute_audio:
            _unlink(st.resume_file)
            _unlink(st.resume_processed)
        if st.current_file and st.current_file not in (path, st.unmute_audio) \
                and not st.playing_unmute:
            # the paused pre-mute recording is replaced by the new one
            _unlink(st.current_file)
            _unlink(st.processed_file)
            st.current_file = st.processed_file = None
        st.resume_file = path
        st.resume_processed = None
        st.resume_name = source_name
        st.resume_is_old = False
        st.resume_pos = 0.0
        # An infinite announcement hands over right away.
        if st.playing_unmute and int(st.unmute_loop_left or 1) < 0 \
                and not st.admin_muted:
            st.playing_unmute = False
            st.current_file = st.processed_file = None
            await self._resume_after_mute(chat_id)
            return "playing"
        return "parked"

    # ------------------------------------------------------------------
    # FAKE SCREEN SHARE
    # ------------------------------------------------------------------

    async def set_screen_share(self, chat_id: int, on: bool,
                               image_path: str = None,
                               title: str = None) -> bool:
        """Turn the bot's presentation (screen share) on or off.

        Telegram lets a user account share a *presentation* video track next to
        its mic, so the bot shares a looping PC-style mic/mixer panel.
        """
        # Serialize toggles per chat: rapid .ss on/off spam used to run several
        # calls.play() at once and leave the call in a broken/blank state.
        locks = self.__dict__.setdefault("_ss_locks", {})
        lock = locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            if bool(getattr(self.state(chat_id), "ss_on", False)) == bool(on) \
                    and not on:
                return True
            return await self._set_screen_share_locked(chat_id, on, image_path, title)

    def _live_mic_audio(self, chat_id: int):
        """Mic stream of a live-mic session that streams through THIS account."""
        try:
            from helpers import live_mic
            for sess in list(getattr(live_mic, "_sessions", {}).values()):
                if getattr(sess, "chat_id", None) == chat_id and \
                        getattr(sess, "relay", None) is self and \
                        getattr(sess, "proc_fifo", None) and not getattr(sess, "_closed", False):
                    return sess._media_stream().microphone
        except Exception as exc:
            logger.debug("live mic lookup failed: %r", exc)
        return None

    async def _set_screen_share_locked(self, chat_id: int, on: bool,
                                       image_path: str = None, title: str = None) -> bool:
        st = self.state(chat_id)
        st.ss_error = ""
        if on and not Config.SCREEN_SHARE_ENABLED:
            st.ss_error = "SCREEN_SHARE_ENABLED=false (env) hai."
            return False
        prev = st.ss_on
        st.ss_on = bool(on)
        if image_path is not None:
            st.ss_image = image_path if os.path.exists(image_path or "") else ""
        if title is not None:
            st.ss_title = title
        source = st.current_file if (st.current_file and
                                     os.path.exists(st.current_file or "")) else None
        try:
            in_call = chat_id in (await self._active_call_ids())
        except Exception:
            in_call = chat_id in self.chats
        try:
            if source:
                # Re-attach audio + screen in one stream, continuing from the
                # same spot instead of restarting the recording.
                pos = await self._current_position(chat_id)
                await self._stream(chat_id, source, st.source_name, fast=True,
                                   start_at=pos)
                if on and not st.ss_on:
                    # _stream fell back to audio-only: presentation refused.
                    st.ss_error = ("Telegram ne screen share accept nahi kiya "
                                   "(group me video/screen allowed nahi ya FFmpeg fail).")
                    return False
            elif not on and not in_call:
                return True  # nothing being shared, nothing to stop
            else:
                from pytgcalls.types.raw import Stream
                screen = self._screen_source(st) if on else None
                # Keep the live mic audio attached — a screen-only stream used
                # to replace (silence) the mic when both used the same account.
                stream = Stream(microphone=self._live_mic_audio(chat_id), screen=screen)
                cfg = GroupCallConfig(auto_start=False)
                try:
                    await self.calls.play(chat_id, stream, cfg)
                except NoActiveGroupCall:
                    if not await self.start_voice_chat(chat_id):
                        raise RuntimeError("Group me voice chat chalu nahi hai — "
                                           "pehle VC start karein.")
                    await self.calls.play(chat_id, stream, cfg)
                except Exception as exc:
                    if not is_peer_error(exc):
                        raise
                    await self._peer(chat_id)
                    await self.calls.play(chat_id, stream, cfg)
            return True
        except Exception as exc:
            logger.warning("Screen share toggle failed for %s: %r", chat_id, exc)
            st.ss_error = str(exc)[:300] or type(exc).__name__
            st.ss_on = prev if not on else False
            return False

    async def _active_call_ids(self):
        active = getattr(self.calls, "calls", None)
        if inspect.isawaitable(active):
            active = await active
        return set(active or {})

    _PREF_KEYS = ("unmute_audio", "unmute_loop", "hand_raise", "mic_blink",
                  "mic_blink_secs")

    def save_mute_prefs(self, chat_id: int):
        """Remember playmute / handraise / micblink for this chat so they
        survive track end, stop and state resets."""
        if not hasattr(self, "mute_prefs"):
            self.mute_prefs = {}
        st = self.chats.get(chat_id)
        if st:
            self.mute_prefs[chat_id] = {k: getattr(st, k) for k in self._PREF_KEYS}

    def state(self, chat_id: int) -> ChatState:
        if chat_id not in self.chats:
            self.chats[chat_id] = ChatState()
            self.chats[chat_id].live_volume = self.live_volume
            for k, v in getattr(self, "mute_prefs", {}).get(chat_id, {}).items():
                setattr(self.chats[chat_id], k, v)
            st0 = self.chats[chat_id]
            if st0.unmute_audio and not os.path.exists(st0.unmute_audio):
                st0.unmute_audio = None
            # STICKY LOOP: the user's last ON/OFF choice survives new tracks,
            # stop/leave and chat-state resets until they switch it again.
            if getattr(self, "loop_pref", {}).get(chat_id):
                self.chats[chat_id].loop = True
                self.chats[chat_id].loop_left = -1
        return self.chats[chat_id]

    def set_loop(self, chat_id: int, on: bool, count: int = -1) -> "ChatState":
        if not hasattr(self, "loop_pref"):
            self.loop_pref = {}
        self.loop_pref[chat_id] = bool(on) and count < 0
        st = self.state(chat_id)
        st.loop, st.loop_left = bool(on), (count if on else -1)
        return st

    async def _on_stream_end(self, chat_id: int):
        if chat_id in self._stopped_chats:
            return
        st = self.chats.get(chat_id)
        if not st:
            return
        try:
            from helpers.live_mic import is_active_for_chat
            if is_active_for_chat(chat_id):
                logger.info("Ignoring live-mic stream end for chat %s", chat_id)
                return
        except Exception:
            pass

        # The unmute announcement has its own loop counter and, when it is
        # done, hands the voice chat back to the recording that was playing
        # before the admin mute (or to whatever the user queued meanwhile).
        if getattr(st, "playing_unmute", False):
            try:
                if await self._after_unmute_audio(chat_id):
                    return
            except Exception as exc:
                logger.warning("Unmute-audio follow-up failed for %s: %r",
                               chat_id, exc)
        try:
            # LOOP FIX: earlier this required the *source* file to still exist.
            # The source is deleted as soon as a new track is processed (and for
            # some sources it never lands on disk at all), so loop silently
            # stopped after the first play.  The processed WAV is what actually
            # gets streamed, so either file is enough to loop.
            have_processed = bool(st.processed_file and os.path.exists(st.processed_file))
            have_source = bool(st.current_file and os.path.exists(st.current_file))
            if st.loop and (have_processed or have_source):
                if st.loop_left > 0:
                    st.loop_left -= 1
                if st.loop_left != 0:
                    # Replay the already-processed file directly (no re-processing)
                    # with a small delay so py-tgcalls cleanly ends the old stream
                    # before we start the new one.  Re-processing from scratch each
                    # loop was slow and caused race conditions in py-tgcalls.
                    logger.info("Loop replay for chat %s (left=%s)", chat_id, st.loop_left)
                    replayed = False
                    if have_processed:
                        await asyncio.sleep(0.5)
                        stream = MediaStream(
                            st.processed_file, AudioQuality.HIGH,
                            video_flags=MediaStream.Flags.IGNORE,
                        )
                        try:
                            await self.calls.play(chat_id, stream,
                                                  GroupCallConfig(auto_start=False))
                            st.is_playing, st.is_paused = True, False
                            replayed = True
                        except Exception as exc:
                            logger.warning("Loop direct replay failed for %s: %r",
                                           chat_id, exc)
                    if not replayed and have_source:
                        # Fallback: full re-process + re-stream.
                        await self._stream(chat_id, st.current_file, st.source_name)
                        replayed = True
                    if replayed:
                        return
                    # Nothing left to loop — say so instead of silently leaving.
                    logger.warning("Loop skip for chat %s: file gone (loop stays ON)", chat_id)
                else:
                    st.loop = False
            elif st.loop:
                logger.warning("Loop ON for chat %s but no file on disk (stays ON)", chat_id)
            if st.queue:
                item = st.queue.pop(0)
                path, name = item[0], item[1]
                processed = item[2] if len(item) > 2 else None
                await self._stream(chat_id, path, name, preprocessed=processed)
                self._preprocess_next(chat_id)
                return
        except Exception as e:
            await log_error("stream_end_next", e)
            
        if getattr(st, 'recent_live_mic', False):
            logger.info("Queue empty after live-mic relay, staying in VC for chat %s", chat_id)
            st.recent_live_mic = False
            return

        _cleanup_temp_files()
        await self.leave(chat_id, reason="Queue empty")

    async def _keeper_loop(self, chat_id: int):
        interval = max(5, Config.KEEPER_INTERVAL)
        while True:
            try:
                st = self.chats.get(chat_id)
                if not st:
                    return
                if not st.is_playing and not st.mic_enabled:
                    return
                await self.set_participant_volume(
                    chat_id, self.account_id, FYT_PARTICIPANT_VOLUME, quiet=True
                )
                if st.mic_boost_user_id:
                    target_vol = st.live_volume or FYT_PARTICIPANT_VOLUME
                    await self.set_participant_volume(
                        chat_id, st.mic_boost_user_id, target_vol, quiet=True
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(interval)

    def _start_keeper(self, chat_id: int):
        old = self._keepers.pop(chat_id, None)
        if old:
            old.cancel()
        self._keepers[chat_id] = asyncio.create_task(self._keeper_loop(chat_id))

    def _stop_keeper(self, chat_id: int):
        t = self._keepers.pop(chat_id, None)
        if t:
            t.cancel()

    async def set_auto(self, chat_id: int, on: bool) -> bool:
        st = self.state(chat_id)
        if st.auto == bool(on) and (not on or chat_id in self._keepers):
            return True
        st.auto = bool(on)
        if on:
            st.apply_settings({**st.settings(), **AUTO_PRESET})
            self._start_keeper(chat_id)
            if st.is_playing:
                await self.set_participant_volume(
                    chat_id, self.account_id, FYT_PARTICIPANT_VOLUME, quiet=True
                )
        else:
            self._stop_keeper(chat_id)
        asyncio.create_task(log_auto_mode(self.owner_id, chat_id, bool(on)))
        return True

    async def _ensure_connected(self) -> bool:
        """Ensure the Telegram client is connected before any RPC call.

        The client can disconnect silently (memory eviction, revoked session,
        network drop).  Without this check, play() hits MTProtoClientNotConnected
        or ConnectionError('Client has not been started yet').
        """
        if self.client is None:
            return False
        if getattr(self.client, "is_connected", False):
            return True
        try:
            logger.info("Reconnecting disconnected client for user %s", self.owner_id)
            await self.client.connect()
            return getattr(self.client, "is_connected", False)
        except Exception as exc:
            logger.warning("Reconnect failed for user %s: %r", self.owner_id, exc)
            return False

    async def _peer(self, chat_id: int, join_ref: str = None):
        """Resolve a peer, auto-joining the chat when the account lacks access.

        join_ref is a username or invite link the user supplied; if the bot
        cannot mint its own invite link, the account tries joining via this.
        """
        if not await self._ensure_connected():
            raise ConnectionError("Client has not been started yet")
        return await ensure_peer(self.client, chat_id, bot=get_bot(),
                                  auto_join=True, join_ref=join_ref)

    async def _call_input(self, chat_id: int):
        peer = await self._peer(chat_id)
        if isinstance(peer, InputPeerChannel):
            full = await self.client.invoke(GetFullChannel(channel=peer))
        elif isinstance(peer, InputPeerChat):
            full = await self.client.invoke(GetFullChat(chat_id=peer.chat_id))
        else:
            return None
        call = full.full_chat.call
        if call is not None:
            self._call_chats[getattr(call, "id", None)] = chat_id
        return call

    async def start_voice_chat(self, chat_id: int) -> bool:
        try:
            peer = await self._peer(chat_id)
            await self.client.invoke(
                CreateGroupCall(peer=peer, random_id=random.randint(1, 2**31 - 1))
            )
            await asyncio.sleep(1.5)
            return True
        except Exception as e:
            await log_error("start_voice_chat", e)
            return False

    async def send_vc_message(self, chat_id: int, text: str) -> None:
        """Post a message in the voice chat's own chat panel (Telegram group
        call messages).  A single emoji shows up as a floating VC reaction."""
        from pyrogram.raw.functions.phone import SendGroupCallMessage
        from pyrogram.raw.types import TextWithEntities
        text = (text or "").strip()
        if not text:
            raise ValueError("Message khali hai.")
        call_input = await self._call_input(chat_id)
        if not call_input:
            raise RuntimeError("Is group me voice chat chalu nahi hai.")
        req = lambda: SendGroupCallMessage(
            call=call_input, random_id=random.getrandbits(63),
            message=TextWithEntities(text=text[:4096], entities=[]),
        )
        try:
            await self.client.invoke(req())
        except Exception as exc:
            if _vc_join_missing(exc):
                # ID VC me nahi hai — chup-chaap silent stream se join karo,
                # phir message dobara bhejo.
                await self._ensure_in_call(chat_id)
                call_input = await self._call_input(chat_id) or call_input
                await self.client.invoke(req())
                return
            if not is_peer_error(exc):
                raise
            await self._peer(chat_id)
            await self.client.invoke(req())

    async def _ensure_in_call(self, chat_id: int) -> None:
        """Join the VC with a silent stream when the account isn't in it."""
        st = self.chats.get(chat_id)
        if st and (getattr(st, "current_file", None) or getattr(st, "playing", False)):
            return
        path = _silence_file()
        await self.calls.play(
            chat_id,
            MediaStream(path, AudioQuality.HIGH, video_flags=MediaStream.Flags.IGNORE),
        )
        # Telegram ko join register karne do.
        for _ in range(10):
            await asyncio.sleep(0.6)
            try:
                await self.client.invoke(EditGroupCallParticipant(
                    call=await self._call_input(chat_id),
                    participant=await self.client.resolve_peer("me"),
                    muted=True,
                ))
                return
            except Exception as exc:
                if not (_vc_join_missing(exc) or _participant_not_joined(exc)):
                    return

    async def set_participant_volume(self, chat_id: int, user_id: int,
                                     volume: int, quiet: bool = False) -> bool:
        volume = max(1, min(VOL_MAX, int(volume)))
        ok = False

        if user_id == self.account_id:
            try:
                await self.calls.change_volume_call(chat_id, max(1, volume // 100))
                ok = True
            except Exception as exc:
                if _participant_not_joined(exc):
                    return False
                ok = False
        if not ok:
            try:
                call_input = await self._call_input(chat_id)
                if not call_input:
                    return False
                try:
                    peer = await self.client.resolve_peer(user_id)
                except Exception:
                    peer = await ensure_peer(self.client, user_id, bot=get_bot(),
                                             auto_join=False)
                await self.client.invoke(EditGroupCallParticipant(
                    call=call_input, participant=peer, volume=volume,
                ))
                ok = True
            except Exception as e:
                if _participant_not_joined(e):
                    return False
                if not quiet:
                    await log_error("set_participant_volume", e)
                return False
        if ok and not quiet:
            asyncio.create_task(log_live_boost(self.owner_id, chat_id, user_id, volume))
        return ok

    # ------------------------------------------------------------------
    # INSTANT PLAYBACK + FAKE SCREEN SHARE SOURCES
    # ------------------------------------------------------------------

    def _loud_filters(self, st) -> str:
        db = max(0, min(18, int(getattr(st, "loud_db", 0) or 0)))
        return f"volume={db}dB" if db else ""

    def _audio_source(self, st, path: str, loop: bool = False,
                      start_at: float = 0.0):
        """FFmpeg -> raw PCM audio source: playback starts in ~0 s."""
        from ntgcalls import MediaSource
        from pytgcalls.types.raw import AudioParameters, AudioStream
        cmd = build_stream_command(
            path, volume=st.volume, bass=st.bass, echo=st.echo,
            echo_level=st.echo_level, boost=st.boost,
            relay_volume=st.relay_volume, gain=st.gain, treble=st.treble,
            extra_filters=self._loud_filters(st), loop=loop, start_at=start_at,
        )
        return AudioStream(MediaSource.SHELL, cmd, AudioParameters(48000, 2))

    async def vc_participants(self, chat_id: int) -> list:
        """Real participant list of the VC, straight from Telegram."""
        from pyrogram.raw.functions.phone import GetGroupParticipants
        call = await self._call_input(chat_id)
        if not call:
            return []
        res = await self.client.invoke(GetGroupParticipants(
            call=call, ids=[], sources=[], offset="", limit=200))
        users = {u.id: u for u in getattr(res, "users", []) or []}
        chats = {c.id: c for c in getattr(res, "chats", []) or []}
        speak = self.__dict__.get("_speak_ts", {})
        now = time.time()
        out = []
        for p in getattr(res, "participants", []) or []:
            peer = getattr(p, "peer", None)
            uid = getattr(peer, "user_id", None)
            if uid is not None:
                u = users.get(uid)
                name = " ".join(x for x in [getattr(u, "first_name", "") or "",
                                            getattr(u, "last_name", "") or ""] if x) or "User"
            else:
                cid = getattr(peer, "channel_id", None) or getattr(peer, "chat_id", None)
                name = getattr(chats.get(cid), "title", "") or "Channel"
            muted = bool(getattr(p, "muted", False))
            act = getattr(p, "active_date", None) or 0
            last = max(float(act), speak.get((chat_id, uid), 0))
            out.append({
                "name": name[:40], "muted": muted,
                "speaking": (not muted) and (now - last) < 2.5,
                "hand": bool(getattr(p, "raise_hand_rating", None)),
                "volume": int((getattr(p, "volume", None) or 10000) / 100),
                "is_me": uid == self.account_id,
            })
        return out

    def _ss_state_path(self, chat_id: int) -> str:
        return os.path.join("/tmp", f"ss_live_{self.owner_id}_{abs(chat_id)}.json")

    async def _ss_live_loop(self, chat_id: int):
        """Keep the live screen's state file fresh while screen share is ON."""
        import json
        path = self._ss_state_path(chat_id)
        started = time.time()
        title = ""
        try:
            chat = await self.client.get_chat(chat_id)
            title = getattr(chat, "title", "") or ""
        except Exception:
            pass
        parts, last_fetch = [], 0.0
        while True:
            st = self.chats.get(chat_id)
            if not st or not st.ss_on:
                break
            if time.time() - last_fetch > 1.5:
                try:
                    parts = await self.vc_participants(chat_id)
                except Exception as exc:
                    logger.debug("ss participants failed: %r", exc)
                last_fetch = time.time()
            data = {
                "group": title, "started": started, "participants": parts,
                "now_playing": st.source_name if st.is_playing else "",
                "paused": bool(st.is_paused), "loop": bool(st.loop),
                "volume": int(getattr(st, "relay_volume", 0) or 0),
                "boost": int(getattr(st, "boost", 0) or 0),
                "mic_on": bool(getattr(st, "mic_enabled", False)),
            }
            try:
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                os.replace(tmp, path)
            except Exception:
                pass
            await asyncio.sleep(0.5)

    def _screen_source(self, st):
        """Screen share: live VC grid (default), a replied photo, or mixer."""
        from ntgcalls import MediaSource
        from pytgcalls.types.raw import VideoParameters, VideoStream
        import shlex
        import sys as _sys
        mode = getattr(st, "ss_mode", "live")
        if getattr(st, "ss_image", ""):
            mode = "image"
        chat_id = next((c for c, v in self.chats.items() if v is st), None)
        if mode == "live" and chat_id is not None:
            task = getattr(st, "ss_task", None)
            if not task or task.done():
                st.ss_task = asyncio.get_event_loop().create_task(self._ss_live_loop(chat_id))
            script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live_screen.py")
            cmd = " ".join(shlex.quote(x) for x in [
                _sys.executable or "python3", script,
                "--state", self._ss_state_path(chat_id),
                "--w", str(Config.SS_WIDTH), "--h", str(Config.SS_HEIGHT),
                "--fps", str(Config.SS_FPS)])
            return VideoStream(
                MediaSource.SHELL, cmd,
                VideoParameters(Config.SS_WIDTH, Config.SS_HEIGHT, Config.SS_FPS,
                                adjust_by_height=False),
            )
        cmd = build_fake_screen_command(
            width=Config.SS_WIDTH, height=Config.SS_HEIGHT, fps=Config.SS_FPS,
            image_path=getattr(st, "ss_image", "") or "",
            title=getattr(st, "ss_title", "") or "Audio Setup — Live",
        )
        return VideoStream(
            MediaSource.SHELL, cmd,
            VideoParameters(Config.SS_WIDTH, Config.SS_HEIGHT, Config.SS_FPS,
                            adjust_by_height=False),
        )

    def _raw_stream(self, st, audio):
        from pytgcalls.types.raw import Stream
        screen = None
        if getattr(st, "ss_on", False) and Config.SCREEN_SHARE_ENABLED:
            try:
                screen = self._screen_source(st)
            except Exception as exc:
                logger.warning("Screen share source failed: %r", exc)
        return Stream(microphone=audio, screen=screen)

    async def _stream(self, chat_id: int, path: str, source_name: str,
                       preprocessed: str = None, fast: bool = None,
                       start_at: float = 0.0):
        if chat_id in self._stopped_chats:
            raise RuntimeError("Playback stopped manually; start .play again")
        st = self.state(chat_id)

        play_path = path
        processed = None
        raw_stream = None

        # FAST PATH (default): FFmpeg streams the processed audio live, so the
        # first frame reaches the voice chat immediately instead of after a
        # full pre-render.  Infinite loop is done by FFmpeg itself (gapless).
        use_fast = Config.FAST_PLAY if fast is None else bool(fast)
        if use_fast and not preprocessed:
            try:
                native_loop = bool(st.loop and st.loop_left < 0)
                raw_stream = self._raw_stream(
                    st, self._audio_source(st, path, loop=native_loop,
                                           start_at=start_at))
                st.native_loop = native_loop
            except Exception as exc:
                logger.warning("Instant-play source build failed (%r); "
                               "falling back to pre-render", exc)
                raw_stream = None

        if raw_stream is None:
            st.native_loop = False
            if preprocessed and os.path.exists(preprocessed):
                processed = preprocessed
                play_path = processed
            else:
                try:
                    processed = await process_audio_to_file(
                        path,
                        volume=st.volume, bass=st.bass, echo=st.echo,
                        echo_level=st.echo_level, boost=st.boost,
                        relay_volume=st.relay_volume, gain=st.gain,
                        treble=st.treble,
                        extra_filters=self._loud_filters(st),
                    )
                    play_path = processed
                except Exception as exc:
                    logger.warning("Audio processing failed for %s; using raw audio: %r",
                                   chat_id, exc)
            raw_stream = MediaStream(
                play_path, AudioQuality.HIGH, video_flags=MediaStream.Flags.IGNORE,
            )
        try:
            try:
                try:
                    await self.calls.play(chat_id, raw_stream, GroupCallConfig(auto_start=False))
                except NoActiveGroupCall:
                    raise
                except Exception as exc:
                    # Screen share must never block audio: if the call refuses
                    # the presentation (no rights / codec / ffmpeg died), retry
                    # immediately with audio only instead of hanging at 0:00.
                    if getattr(raw_stream, "screen", None) is None or is_peer_error(exc):
                        raise
                    logger.warning("Play with screen share failed (%r); "
                                   "retrying audio-only", exc)
                    from pytgcalls.types.raw import Stream as _RawStream
                    raw_stream = _RawStream(microphone=raw_stream.microphone)
                    st.ss_on = False
                    await self.calls.play(chat_id, raw_stream, GroupCallConfig(auto_start=False))
            except NoActiveGroupCall:
                if not await self.start_voice_chat(chat_id):
                    raise RuntimeError(
                        "Is group mein koi voice chat chalu nahi hai aur bot use "
                        "start nahi kar saka. VC start karein (ya logged-in account "
                        "ko 'Manage video chats' admin right dein)."
                    )
                await self.calls.play(chat_id, raw_stream, GroupCallConfig(auto_start=False))
            except Exception as exc:
                if not is_peer_error(exc):
                    raise
                await self._peer(chat_id)
                await self.calls.play(chat_id, raw_stream, GroupCallConfig(auto_start=False))
        except Exception:
            _unlink(processed)
            raise

        old = st.processed_file
        old_source = st.current_file
        st.processed_file = processed
        st.current_file = path
        st.source_name = source_name
        st.is_playing = True
        st.is_paused = False
        st.mic_enabled = False
        st.recent_live_mic = False
        keep = {st.unmute_audio, st.resume_file, st.resume_processed, path}
        if old and old not in keep:
            _unlink(old)
        if old_source and old_source not in keep:
            _unlink(old_source)
        # Prime call-id -> chat map used by the raw mute fallback.
        if not any(v == chat_id for v in self._call_chats.values()):
            async def _prime():
                try:
                    await self._call_input(chat_id)
                except Exception:
                    pass
            asyncio.create_task(_prime())

        # If processing failed, retry in the background for loop/next play.
        # On the instant path nothing is pre-rendered on purpose.
        if processed is None and not use_fast:
            asyncio.create_task(self._process_for_loop(chat_id, path, source_name))

        if Config.AUTO_LIVE_BOOST or st.auto:

            for delay in (0.0, 0.25, 0.75, 1.5):
                if delay:
                    await asyncio.sleep(delay)
                if await self.set_participant_volume(
                    chat_id, self.account_id, FYT_PARTICIPANT_VOLUME, quiet=True,
                ):
                    break
            if chat_id not in self._keepers:
                self._start_keeper(chat_id)

        asyncio.create_task(log_vc_join(
            self.owner_id, chat_id, st.chat_title or str(chat_id),
            source_name, st.settings(),
        ))

    async def _process_for_loop(self, chat_id: int, path: str,
                                source_name: str):
        """Process the raw file with the full filter chain in the background.
        The processed file is stored for loop replay and queue playback — the
        raw file plays to completion without interruption."""
        st = self.chats.get(chat_id)
        if not st or st.current_file != path:
            return
        try:
            processed = await process_audio_to_file(
                path,
                volume=st.volume, bass=st.bass, echo=st.echo,
                echo_level=st.echo_level, boost=st.boost,
                relay_volume=st.relay_volume, gain=st.gain, treble=st.treble,
            )
            if st.current_file == path:
                old = st.processed_file
                st.processed_file = processed
                if old:
                    _unlink(old)
            else:
                _unlink(processed)
        except Exception as exc:
            logger.debug("Background processing failed for %s: %r", chat_id, exc)

    async def boost_user_mic(self, chat_id: int, target_user_id: int) -> bool:
        """Boost a user's live mic volume to max in the VC.

        Uses EditGroupCallParticipant to set the target user's volume to max.
        The user must already be in the voice chat. A keeper loop re-applies
        the volume periodically so it survives Telegram server-side resets.
        """
        st = self.state(chat_id)
        target_vol = st.live_volume or FYT_PARTICIPANT_VOLUME
        ok = await self.set_participant_volume(chat_id, target_user_id, target_vol)
        if not ok:
            return False
        st.mic_enabled = True
        st.mic_boost_user_id = target_user_id
        if chat_id not in self._keepers:
            self._start_keeper(chat_id)
        asyncio.create_task(log_live_boost(
            self.owner_id, chat_id, target_user_id, target_vol,
        ))
        return True

    async def stop_mic_boost(self, chat_id: int) -> bool:
        """Stop boosting a user's mic volume and clean up."""
        st = self.chats.get(chat_id)
        if not st or not st.mic_boost_user_id:
            return False
        target_user_id = st.mic_boost_user_id
        try:
            await self.set_participant_volume(
                chat_id, target_user_id, VOL_NORMAL, quiet=True
            )
        except Exception:
            pass
        st.mic_enabled = False
        st.mic_boost_user_id = None
        self._stop_keeper(chat_id)
        if not st.is_playing:
            self.chats.pop(chat_id, None)
            try:
                await self.calls.leave_call(chat_id)
            except Exception:
                pass
            # Only log a VC "leave" when we actually left the call.
            asyncio.create_task(log_vc_leave(
                self.owner_id, chat_id, "Mic boost stopped"
            ))
        return True

    @staticmethod
    def _check_group(chat_id: int):
        if chat_id >= 0:
            raise ValueError(
                "Voice chat sirf group/supergroup mein hota hai — chat ID negative honi chahiye."
            )

    async def _reconnect_client(self):
        async with self._reconnect_lock:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            await asyncio.sleep(0.5)
            await self.client.connect()
            logger.info("Reconnected Telegram client for user %s", self.owner_id)

    async def _stream_with_reconnect(self, chat_id: int, path: str,
                                     source_name: str):
        try:
            await self._stream(chat_id, path, source_name)
        except Exception as error:
            if not (_connection_lost(error) or _client_disconnected(error)):
                raise
            logger.warning("Connection lost during playback; reconnecting once")
            if not await self._ensure_connected():
                raise
            await self._stream(chat_id, path, source_name)

    async def play(self, chat_id: int, path: str, source_name: str = "audio",
                   chat_title: str = "", enqueue: bool = False,
                   join_ref: str = None) -> str:
        self._check_group(chat_id)
        async with self._lock:
            self._stopped_chats.discard(chat_id)
            st = self.state(chat_id)
            if chat_title:
                st.chat_title = chat_title
            # Admin has the bot muted (or the unmute announcement / mic check
            # is running): don't blast it now — park it for right after.
            if self.mute_busy(chat_id) and st.is_playing:
                return await self.park_recording(chat_id, path, source_name,
                                                 enqueue)
            if enqueue and st.is_playing:
                st.queue.append((path, source_name, None))
                self._preprocess_next(chat_id)
                return "queued"
            # Loop stays exactly as the user last set it (sticky).
            if getattr(self, "loop_pref", {}).get(chat_id):
                st.loop, st.loop_left = True, -1
            if not await self._ensure_connected():
                raise ConnectionError("Client has not been started yet")
            await self._peer(chat_id, join_ref=join_ref)
            await self._stream_with_reconnect(chat_id, path, source_name)
            self._preprocess_next(chat_id)
            return "playing"

    async def force_play(self, chat_id: int, path: str, source_name: str = "audio",
                         chat_title: str = "", join_ref: str = None) -> str:
        self._check_group(chat_id)
        async with self._lock:
            self._stopped_chats.discard(chat_id)
            st = self.state(chat_id)
            if chat_title:
                st.chat_title = chat_title
            st.queue.clear()
            st.is_paused = False
            if getattr(self, "loop_pref", {}).get(chat_id):
                st.loop, st.loop_left = True, -1
            if not await self._ensure_connected():
                raise ConnectionError("Client has not been started yet")
            await self._peer(chat_id, join_ref=join_ref)
            try:
                await self._stream_with_reconnect(chat_id, path, source_name)
            except Exception:

                try:
                    await self.calls.leave_call(chat_id)
                except Exception:
                    pass
                await asyncio.sleep(1)
                await self._stream_with_reconnect(chat_id, path, source_name)
            return "playing"

    async def pause(self, chat_id: int) -> bool:
        st = self.chats.get(chat_id)
        if not st or not st.is_playing or st.is_paused:
            return False
        await self.calls.pause(chat_id)
        st.is_paused = True
        return True

    async def resume(self, chat_id: int) -> bool:
        st = self.chats.get(chat_id)
        if not st or not st.is_paused:
            return False
        await self.calls.resume(chat_id)
        st.is_paused = False
        return True

    async def skip(self, chat_id: int) -> bool:
        if chat_id not in self.chats:
            return False
        await self._on_stream_end(chat_id)
        return True

    def queue_clear(self, chat_id: int) -> int:
        st = self.chats.get(chat_id)
        if not st:
            return 0
        count = len(st.queue)
        for item in st.queue:
            _unlink(item[0])
            if len(item) > 2:
                _unlink(item[2])
        st.queue.clear()
        return count

    def queue_remove(self, chat_id: int, index: int) -> bool:
        st = self.chats.get(chat_id)
        if not st or index < 0 or index >= len(st.queue):
            return False
        item = st.queue.pop(index)
        _unlink(item[0])
        if len(item) > 2:
            _unlink(item[2])
        return True

    def queue_shuffle(self, chat_id: int) -> bool:
        import random as _r
        st = self.chats.get(chat_id)
        if not st or len(st.queue) < 2:
            return False
        _r.shuffle(st.queue)
        return True

    def queue_list(self, chat_id: int) -> list:
        st = self.chats.get(chat_id)
        if not st:
            return []
        return [(item[1], i) for i, item in enumerate(st.queue)]

    def _preprocess_next(self, chat_id: int):
        """Pre-process the next queued track while current is playing.

        This eliminates the FFmpeg wait when the current track ends — the
        next one is already processed and ready to stream instantly.
        """
        st = self.chats.get(chat_id)
        if not st or not st.queue:
            return
        next_path = st.queue[0][0]
        if not next_path or not os.path.exists(next_path):
            return
        asyncio.create_task(self._preprocess_one(chat_id, next_path))

    async def _preprocess_one(self, chat_id: int, path: str):
        st = self.chats.get(chat_id)
        if not st:
            return
        try:
            processed = await process_audio_to_file(
                path,
                volume=st.volume, bass=st.bass, echo=st.echo,
                echo_level=st.echo_level, boost=st.boost,
                relay_volume=st.relay_volume, gain=st.gain, treble=st.treble,
            )
            if st.queue and st.queue[0][0] == path:
                st.queue[0] = (path, st.queue[0][1], processed)
                logger.info("Pre-processed next track for chat %s", chat_id)
        except Exception as exc:
            logger.debug("Pre-process failed for %s: %r", chat_id, exc)

    async def reapply(self, chat_id: int) -> bool:
        st = self.chats.get(chat_id)
        if not st:
            return False
        if st.mic_enabled and st.mic_boost_user_id:
            target_vol = st.live_volume or FYT_PARTICIPANT_VOLUME
            return await self.set_participant_volume(
                chat_id, st.mic_boost_user_id, target_vol
            )
        if not st.current_file or not os.path.exists(st.current_file):
            return False
        await self._stream(chat_id, st.current_file, st.source_name)
        return True

    async def leave(self, chat_id: int, reason: str = "Manual stop"):
        if reason != "Queue empty":
            self._stopped_chats.add(chat_id)
        self._stop_keeper(chat_id)
        try:
            from helpers.live_mic import stop_all_for_chat
            await stop_all_for_chat(chat_id)
        except Exception:
            pass
        st = self.chats.pop(chat_id, None)
        if st:
            for item in st.queue:
                _unlink(item[0])
                if len(item) > 2:
                    _unlink(item[2])
            st.queue.clear()
            _unlink(st.processed_file)
            if st.current_file != st.unmute_audio:
                _unlink(st.current_file)
            self.mute_prefs = getattr(self, "mute_prefs", {})
            self.mute_prefs[chat_id] = {k: getattr(st, k) for k in self._PREF_KEYS}
        _cleanup_temp_files()
        try:
            await self.calls.leave_call(chat_id)
        except Exception:
            pass
        asyncio.create_task(log_vc_leave(self.owner_id, chat_id, reason))

    def is_playing(self, chat_id: int) -> bool:
        st = self.chats.get(chat_id)
        return bool(st and st.is_playing)

    def touch(self):
        self.last_active = time.monotonic()

    def is_idle(self) -> bool:
        try:
            from helpers.live_mic import is_active
            if is_active(self.owner_id):
                return False
        except Exception:
            pass
        return not any(st.is_playing or st.mic_enabled for st in self.chats.values())

class SessionManager:

    def __init__(self):
        self.users: "OrderedDict[int, UserVC]" = OrderedDict()
        self._locks: Dict[int, asyncio.Lock] = {}
        self.assistants: Dict[str, UserVC] = {}
        self._assistant_lock = asyncio.Lock()

    @staticmethod
    def _assistant_key(string_session: str) -> str:
        import hashlib
        return hashlib.sha256(string_session.encode()).hexdigest()[:16]

    async def assistant_string(self, user_id: int) -> str:
        """Per-user spare account, falling back to the global one."""
        try:
            stored = await _db().get_app_value(f"assistant_session_{user_id}")
        except Exception:
            stored = None
        return (stored or Config.ASSISTANT_SESSION or "").strip()

    async def get_relay(self, user_id: int) -> Optional[UserVC]:
        """UserVC of the spare account that streams live mic audio.

        Returns None when no spare account is configured; callers then fall
        back to the user's own account (which kicks them out of the VC).
        """
        string_session = await self.assistant_string(user_id)
        if not string_session:
            return None
        key = self._assistant_key(string_session)
        async with self._assistant_lock:
            uvc = self.assistants.get(key)
            if uvc and uvc.client is not None and getattr(uvc.client, "is_connected", False):
                uvc.touch()
                return uvc
            if uvc:
                try:
                    await uvc.stop()
                except Exception:
                    pass
                self.assistants.pop(key, None)
            uvc = UserVC(user_id, string_session, label="relay")
            try:
                await uvc.start()
            except Exception as exc:
                try:
                    await uvc.stop()
                except Exception:
                    pass
                await log_error(f"assistant_start_{user_id}", exc)
                return None
            self.assistants[key] = uvc
            return uvc

    async def drop_relay(self, user_id: int):
        string_session = await self.assistant_string(user_id)
        if not string_session:
            return
        uvc = self.assistants.pop(self._assistant_key(string_session), None)
        if uvc:
            try:
                await uvc.stop()
            except Exception:
                pass

    def _lock(self, user_id: int) -> asyncio.Lock:
        if user_id not in self._locks:
            self._locks[user_id] = asyncio.Lock()
        return self._locks[user_id]

    async def _evict_if_needed(self):
        """Stop the least-recently-used idle session if over the limit."""
        max_sessions = max(1, Config.MAX_ACTIVE_SESSIONS)
        while len(self.users) > max_sessions:
            evicted = False
            for uid, uvc in list(self.users.items()):
                if uvc.is_idle():
                    logger.info("Evicting idle session %s to save memory", uid)
                    await self.remove(uid)
                    evicted = True
                    break
            if not evicted:
                break

    async def add(self, user_id: int, string_session: str) -> UserVC:
        async with self._lock(user_id):
            old = self.users.pop(user_id, None)
            if old:
                await old.stop()
            uvc = UserVC(user_id, string_session)
            try:
                await uvc.start()
            except Exception:
                await uvc.stop()
                raise
            self.users[user_id] = uvc
            self.users.move_to_end(user_id)
        await self._evict_if_needed()
        return uvc

    async def get(self, user_id: int) -> Optional[UserVC]:
        async with self._lock(user_id):
            if user_id in self.users:
                uvc = self.users[user_id]
                if uvc.client and not getattr(uvc.client, "is_connected", False):
                    logger.info("Session %s client disconnected; reconnecting", user_id)
                    try:
                        await uvc.client.connect()
                    except Exception as exc:
                        logger.warning("Reconnect failed for %s: %r", user_id, exc)
                        self.users.pop(user_id, None)
                        try:
                            await uvc.stop()
                        except Exception:
                            pass
                        return None
                self.users.move_to_end(user_id)
                uvc.touch()
                return uvc
        try:
            data = await _db().get_user(user_id)
        except Exception:
            data = None
        if not data or not data.get("string_session"):
            return None
        try:
            uvc = await self.add(user_id, data["string_session"])
            uvc.touch()
            return uvc
        except Exception as e:
            if _is_invalid_session(e):
                await _invalidate_session(user_id, e, "on-demand start")
            else:
                await log_error(f"session_start_{user_id}", e)
            return None

    async def remove(self, user_id: int):
        async with self._lock(user_id):
            uvc = self.users.pop(user_id, None)
        if uvc:
            await uvc.stop()

    async def restore_owner_only(self) -> int:
        """Only restore the owner's session at boot; others load on-demand."""
        started = 0
        primary_owner = Config.primary_owner()
        if not primary_owner:
            return 0
        try:
            data = await _db().get_user(primary_owner)
        except Exception:
            data = None
        if not data or not data.get("string_session"):
            if Config.STRING_SESSION:
                try:
                    await self.add(primary_owner, Config.STRING_SESSION)
                    await _db().add_user(primary_owner, "", "Owner", Config.STRING_SESSION)
                    started = 1
                except Exception as e:
                    await log_error("owner_session_start", e)
            return started
        try:
            await self.add(primary_owner, data["string_session"])
            started = 1
        except Exception as e:
            if _is_invalid_session(e):
                await _invalidate_session(primary_owner, e, "startup restore")
            else:
                await log_error("restore_owner_session", e)
        return started

    async def restore_boot_sessions(self) -> int:
        """Boot restore: owner only unless RESTORE_ALL_SESSIONS=1.

        Other users' sessions start on demand (SessionManager.get), which is
        what keeps the dyno inside its memory quota.
        """
        if Config.RESTORE_ALL_SESSIONS:
            return await self.restore_all()
        return await self.restore_owner_only()

    async def restore_all(self) -> int:
        """Restore all user sessions at boot (opt-in, memory hungry)."""
        try:
            users = await _db().all_users()
        except Exception as e:
            await log_error("restore_all_db", e)
            return 0
        started = 0
        limit = max(1, Config.MAX_ACTIVE_SESSIONS)
        for u in users:
            if started >= limit:
                logger.info("Session restore capped at %s to protect memory", limit)
                break
            if not u.get("string_session"):
                continue
            uid = int(u["user_id"])
            try:
                await self.add(uid, u["string_session"])
                started += 1
            except Exception as e:
                if _is_invalid_session(e):
                    await _invalidate_session(uid, e, "startup restore")
                else:
                    await log_error(f"restore_session_{uid}", e)
        return started

    async def enforce_memory(self) -> float:
        """Keep RSS under the configured limits (permanent R14 fix)."""
        import gc
        used = process_memory_mb()
        if not used:
            return 0.0
        if used < Config.MEMORY_SOFT_MB:
            return used
        logger.warning("Memory %.0f MB — freeing idle sessions", used)
        for uid, uvc in list(self.users.items()):
            if uvc.is_idle():
                await self.remove(uid)
        for key, uvc in list(self.assistants.items()):
            if uvc.is_idle():
                self.assistants.pop(key, None)
                try:
                    await uvc.stop()
                except Exception:
                    pass
        gc.collect()
        used = process_memory_mb()
        if used >= Config.MEMORY_HARD_MB:
            # Still critical: drop least-recently-used sessions outright so
            # the dyno never gets killed for exceeding its quota.
            for uid in list(self.users)[:max(1, len(self.users) // 2)]:
                logger.warning("Memory %.0f MB — dropping LRU session %s", used, uid)
                await self.remove(uid)
            gc.collect()
            used = process_memory_mb()
        logger.info("Memory after cleanup: %.0f MB", used)
        return used

    async def evict_idle_sessions(self):
        """Periodically stop sessions that have been idle beyond the timeout."""
        timeout = max(60, Config.SESSION_IDLE_TIMEOUT)
        now = time.monotonic()
        for uid, uvc in list(self.users.items()):
            if uvc.is_idle() and (now - uvc.last_active) > timeout:
                logger.info("Auto-evicting idle session %s (idle %.0fs)", uid, now - uvc.last_active)
                await self.remove(uid)

    def active_chats(self) -> int:
        return sum(len(u.chats) for u in self.users.values())

session_manager = SessionManager()
