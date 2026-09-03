"""
Multi-user VC engine.

Every logged-in user gets their OWN Pyrogram user client + PyTgCalls instance,
so many people can use the bot at the same time in different (or the same)
groups without stepping on each other.

Rules:
  • Nobody's volume is ever lowered. We only raise our own participant volume.
  • The user's own mic and the bot's audio can be live at the same time.
  • On playback and reconnect, the bot sets the logged-in account to
    Telegram's participant-volume maximum (20000 = 200%).
  • If the group has no running voice chat, the bot tries to start one
    (works when the logged-in account is an admin with "manage video chats").
"""

import asyncio
import os
import random
from typing import Dict, Optional

from ntgcalls import MediaSource
from pyrogram import Client
from pyrogram.raw.functions.channels import GetFullChannel
from pyrogram.raw.functions.messages import GetFullChat
from pyrogram.raw.functions.phone import CreateGroupCall, EditGroupCallParticipant
from pyrogram.raw.types import InputPeerChannel, InputPeerChat
from pytgcalls import MediaDevices, PyTgCalls
from pytgcalls import filters as call_filters
from pytgcalls.exceptions import NoActiveGroupCall
from pytgcalls.types import AudioQuality, ChatUpdate, MediaStream, StreamEnded
from pytgcalls.types.raw import AudioParameters, AudioStream, Stream

from config import Config
from helpers.audio_processor import (
    build_ffmpeg_filter, process_audio_to_file, shell_quote,
)
from helpers.logger_channel import (
    log_auto_mode, log_error, log_live_boost, log_vc_join, log_vc_leave,
)

# Telegram participant volume scale: 1 – 20000 (10000 = 100%)
VOL_NORMAL = 10000
VOL_MAX = 20000

AUTO_PRESET = {
    "volume": 1000, "relay_volume": 1000, "bass": 20, "gain": 150,
    "treble": 75, "boost": 10, "echo": 0, "echo_level": 0,
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


def _participant_not_joined(error: Exception) -> bool:
    """Telegram raises this when our account is not yet a VC participant."""
    return (type(error).__name__ == "ParticipantJoinMissing" or
            "PARTICIPANT_JOIN_MISSING" in str(error))


# ──────────────────────────────────────────────────────────────────────────────
class ChatState:
    """Per (user, chat) playback state."""

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
        self.mic_device = Config.MIC_DEVICE
        self.auto = Config.AUTO_MODE_DEFAULT
        self.loop = False
        self.loop_left = -1                # -1 = infinite
        self.queue: list = []              # [(path, source_name)]

    def apply_settings(self, s: dict):
        """Copy a saved settings dict (from the DB) onto this state."""
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
            "mic_enabled": self.mic_enabled, "mic_device": self.mic_device,
            "auto": self.auto, "loop": self.loop,
        }


# ──────────────────────────────────────────────────────────────────────────────
class UserVC:
    """One logged-in Telegram account: user client + pytgcalls + chat states."""

    def __init__(self, owner_id: int, string_session: str):
        self.owner_id = owner_id
        self.string_session = string_session
        self.client: Optional[Client] = None
        self.calls: Optional[PyTgCalls] = None
        self.account_id: int = 0
        self.account_name: str = ""
        self.account_username: str = ""
        self.chats: Dict[int, ChatState] = {}
        self._keepers: Dict[int, asyncio.Task] = {}
        # Explicit .stop creates a tombstone so late stream-end callbacks or
        # keeper tasks cannot immediately recreate the VC state.
        self._stopped_chats: set[int] = set()
        self._lock = asyncio.Lock()
        self.live_volume = Config.LIVE_BOOST_DEFAULT

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def start(self):
        self.client = Client(
            f"uvc_{self.owner_id}",
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

        self.calls = PyTgCalls(self.client)

        @self.calls.on_update(call_filters.stream_end())
        async def _on_end(_, update: StreamEnded):
            await self._on_stream_end(update.chat_id)

        gone = (ChatUpdate.Status.LEFT_GROUP | ChatUpdate.Status.KICKED
                | ChatUpdate.Status.CLOSED_VOICE_CHAT)

        @self.calls.on_update(call_filters.chat_update(gone))
        async def _on_left(_, update: ChatUpdate):
            self._stop_keeper(update.chat_id)
            st = self.chats.pop(update.chat_id, None)
            if st:
                for queued_path, _ in st.queue:
                    _unlink(queued_path)
                _unlink(st.processed_file)
                _unlink(st.current_file)

        await self.calls.start()
        return self

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

    # ── state helper ─────────────────────────────────────────────────────────
    def state(self, chat_id: int) -> ChatState:
        if chat_id not in self.chats:
            self.chats[chat_id] = ChatState()
            self.chats[chat_id].live_volume = self.live_volume
        return self.chats[chat_id]

    # ── events ───────────────────────────────────────────────────────────────
    async def _on_stream_end(self, chat_id: int):
        if chat_id in self._stopped_chats:
            return
        st = self.chats.get(chat_id)
        if not st:
            return
        try:
            if st.loop and st.current_file and os.path.exists(st.current_file):
                if st.loop_left > 0:
                    st.loop_left -= 1
                if st.loop_left != 0:
                    await self._stream(chat_id, st.current_file, st.source_name)
                    return
                st.loop = False
            if st.queue:
                path, name = st.queue.pop(0)
                await self._stream(chat_id, path, name)
                return
        except Exception as e:
            await log_error("stream_end_next", e)
        await self.leave(chat_id, reason="Queue empty")

    # ── AUTO MODE (volume keeper) ────────────────────────────────────────────
    async def _keeper_loop(self, chat_id: int):
        """Re-pin our participant volume every KEEPER_INTERVAL seconds.

        Telegram stores participant volume server-side; it can reset after a
        reconnect or an admin action. 200% is the server-side cap — all
        loudness beyond that comes from the FFmpeg chain.
        """
        interval = max(5, Config.KEEPER_INTERVAL)
        while True:
            try:
                st = self.chats.get(chat_id)
                if not st or not st.auto:
                    return
                await self.set_participant_volume(
                    chat_id, self.account_id, st.live_volume, quiet=True
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
        """AUTO mode on/off for one chat. On = max preset + keeper loop."""
        st = self.state(chat_id)
        if st.auto == bool(on) and (not on or chat_id in self._keepers):
            return True
        st.auto = bool(on)
        if on:
            st.apply_settings({**st.settings(), **AUTO_PRESET})
            self._start_keeper(chat_id)
            if st.is_playing:
                await self.set_participant_volume(
                    chat_id, self.account_id, st.live_volume, quiet=True
                )
        else:
            self._stop_keeper(chat_id)
        asyncio.create_task(log_auto_mode(self.owner_id, chat_id, bool(on)))
        return True

    # ── raw helpers ──────────────────────────────────────────────────────────
    async def _call_input(self, chat_id: int):
        peer = await self.client.resolve_peer(chat_id)
        if isinstance(peer, InputPeerChannel):
            full = await self.client.invoke(GetFullChannel(channel=peer))
        elif isinstance(peer, InputPeerChat):
            full = await self.client.invoke(GetFullChat(chat_id=peer.chat_id))
        else:
            return None
        return full.full_chat.call

    async def start_voice_chat(self, chat_id: int) -> bool:
        """Create a group call if none is running (needs admin rights)."""
        try:
            peer = await self.client.resolve_peer(chat_id)
            await self.client.invoke(
                CreateGroupCall(peer=peer, random_id=random.randint(1, 2**31 - 1))
            )
            await asyncio.sleep(1.5)
            return True
        except Exception as e:
            await log_error("start_voice_chat", e)
            return False

    async def set_participant_volume(self, chat_id: int, user_id: int,
                                     volume: int, quiet: bool = False) -> bool:
        """Set a participant's LIVE volume (1–20000). Never used to silence."""
        volume = max(1, min(VOL_MAX, int(volume)))
        ok = False
        # Our own participant: PyTgCalls has a first-class API for it.
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
                peer = await self.client.resolve_peer(user_id)
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

    # ── playback ─────────────────────────────────────────────────────────────
    async def _stream(self, chat_id: int, path: str, source_name: str):
        if chat_id in self._stopped_chats:
            raise RuntimeError("Playback stopped manually; start .play again")
        st = self.state(chat_id)
        processed = await process_audio_to_file(
            path,
            volume=st.volume, bass=st.bass, echo=st.echo,
            echo_level=st.echo_level, boost=st.boost,
            relay_volume=st.relay_volume, gain=st.gain, treble=st.treble,
        )
        # process_audio_to_file renders 48 kHz stereo. HIGH is also 48 kHz;
        # STUDIO expects 96 kHz and forces an extra conversion that can cause
        # dropouts, choppy audio, and lower perceived loudness in the VC.
        stream = MediaStream(
            processed, AudioQuality.HIGH, video_flags=MediaStream.Flags.IGNORE,
        )
        try:
            try:
                await self.calls.play(chat_id, stream)
            except NoActiveGroupCall:
                if not await self.start_voice_chat(chat_id):
                    raise RuntimeError(
                        "Is group mein koi voice chat chalu nahi hai aur bot use "
                        "start nahi kar saka. VC start karein (ya logged-in account "
                        "ko 'Manage video chats' admin right dein)."
                    )
                await self.calls.play(chat_id, stream)
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
        if old and old != processed:
            _unlink(old)
        if old_source and old_source != path:
            _unlink(old_source)

        if Config.AUTO_LIVE_BOOST or st.auto:
            asyncio.create_task(self.set_participant_volume(
                chat_id, self.account_id, st.live_volume, quiet=True,
            ))
        if st.auto and chat_id not in self._keepers:
            self._start_keeper(chat_id)

        asyncio.create_task(log_vc_join(
            self.owner_id, chat_id, st.chat_title or str(chat_id),
            source_name, st.settings(),
        ))

    async def play_microphone(self, chat_id: int, device_hint: str = "") -> str:
        """Publish a server/VM microphone or virtual input device to the VC.

        The input must exist on the machine running this userbot. Telegram does
        not expose a remote phone microphone to a bot session.
        """
        devices = list(MediaDevices.microphone_devices())
        if not devices:
            raise RuntimeError(
                "Server par koi microphone/virtual input device nahi mila. "
                "ALSA/PulseAudio virtual mic configure karein."
            )
        wanted = (device_hint or Config.MIC_DEVICE).strip().lower()
        device = next(
            (d for d in devices if wanted and (
                wanted in d.title.lower() or wanted in d.metadata.lower()
            )),
            devices[0],
        )
        st = self.state(chat_id)
        mic_filter = build_ffmpeg_filter(
            volume=st.volume, bass=st.bass, echo=False, echo_level=0,
            boost=st.boost, relay_volume=st.relay_volume, gain=st.gain,
            treble=st.treble,
        ) if Config.MIC_DSP else "anull"
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", Config.MIC_INPUT_FORMAT, "-i", device.metadata,
            "-af", mic_filter,
            "-f", "s16le", "-ac", "2", "-ar", "48000", "pipe:1",
        ]
        stream = Stream(microphone=AudioStream(
            MediaSource.SHELL, shell_quote(command), AudioParameters(48000, 2),
        ))
        try:
            await self.calls.play(chat_id, stream)
        except NoActiveGroupCall:
            if not await self.start_voice_chat(chat_id):
                raise RuntimeError(
                    "Is group mein koi voice chat chalu nahi hai aur bot use "
                    "start nahi kar saka. VC start karein ya manage-video-chats "
                    "admin right dein."
                )
            await self.calls.play(chat_id, stream)
        st.mic_enabled = True
        st.mic_device = device.metadata
        st.is_playing = True
        st.is_paused = False
        st.source_name = f"🎙️ {device.title}"
        await self.set_participant_volume(
            chat_id, self.account_id, st.live_volume, quiet=True
        )
        return device.title

    @staticmethod
    def microphone_devices() -> list:
        try:
            return list(MediaDevices.microphone_devices())
        except Exception:
            return []

    @staticmethod
    def _check_group(chat_id: int):
        if chat_id >= 0:
            raise ValueError(
                "Voice chat sirf group/supergroup mein hota hai — chat ID negative honi chahiye."
            )

    async def play(self, chat_id: int, path: str, source_name: str = "audio",
                   chat_title: str = "", enqueue: bool = False) -> str:
        self._check_group(chat_id)
        async with self._lock:
            self._stopped_chats.discard(chat_id)
            st = self.state(chat_id)
            if chat_title:
                st.chat_title = chat_title
            if enqueue and st.is_playing:
                st.queue.append((path, source_name))
                return "queued"
            await self._stream(chat_id, path, source_name)
            return "playing"

    async def force_play(self, chat_id: int, path: str, source_name: str = "audio",
                         chat_title: str = "") -> str:
        """.playforce — clear queue, drop the current track, play this now."""
        self._check_group(chat_id)
        async with self._lock:
            self._stopped_chats.discard(chat_id)
            st = self.state(chat_id)
            if chat_title:
                st.chat_title = chat_title
            st.queue.clear()
            st.is_paused = False
            try:
                await self._stream(chat_id, path, source_name)
            except Exception:
                # Leave and rejoin once (fixes a stuck call), then retry.
                try:
                    await self.calls.leave_call(chat_id)
                except Exception:
                    pass
                await asyncio.sleep(1)
                await self._stream(chat_id, path, source_name)
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

    async def reapply(self, chat_id: int) -> bool:
        """Re-render the current track with the current effect settings."""
        st = self.chats.get(chat_id)
        if not st or st.mic_enabled or not st.current_file or not os.path.exists(st.current_file):
            return False
        await self._stream(chat_id, st.current_file, st.source_name)
        return True

    async def leave(self, chat_id: int, reason: str = "Manual stop"):
        if reason != "Queue empty":
            self._stopped_chats.add(chat_id)
        self._stop_keeper(chat_id)
        st = self.chats.pop(chat_id, None)
        if st:
            for queued_path, _ in st.queue:
                _unlink(queued_path)
            st.queue.clear()
            _unlink(st.processed_file)
            _unlink(st.current_file)
        try:
            await self.calls.leave_call(chat_id)
        except Exception:
            pass
        asyncio.create_task(log_vc_leave(self.owner_id, chat_id, reason))

    def is_playing(self, chat_id: int) -> bool:
        st = self.chats.get(chat_id)
        return bool(st and st.is_playing)


# ──────────────────────────────────────────────────────────────────────────────
class SessionManager:
    """Registry of all logged-in users' VC engines."""

    def __init__(self):
        self.users: Dict[int, UserVC] = {}
        self._locks: Dict[int, asyncio.Lock] = {}

    def _lock(self, user_id: int) -> asyncio.Lock:
        if user_id not in self._locks:
            self._locks[user_id] = asyncio.Lock()
        return self._locks[user_id]

    async def add(self, user_id: int, string_session: str) -> UserVC:
        """Start (or restart) a user's engine with a string session."""
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
            return uvc

    async def get(self, user_id: int) -> Optional[UserVC]:
        """Return the running engine, starting it from the DB when needed."""
        if user_id in self.users:
            return self.users[user_id]
        try:
            data = await _db().get_user(user_id)
        except Exception:
            data = None
        if not data or not data.get("string_session"):
            return None
        try:
            return await self.add(user_id, data["string_session"])
        except Exception as e:
            await log_error(f"session_start_{user_id}", e)
            return None

    async def remove(self, user_id: int):
        uvc = self.users.pop(user_id, None)
        if uvc:
            await uvc.stop()

    async def restore_all(self) -> int:
        """Start every saved session at boot. Returns how many came up."""
        try:
            users = await _db().all_users()
        except Exception as e:
            await log_error("restore_all_db", e)
            return 0
        started = 0
        for u in users:
            if not u.get("string_session"):
                continue
            try:
                await self.add(int(u["user_id"]), u["string_session"])
                started += 1
            except Exception as e:
                await log_error(f"restore_session_{u.get('user_id')}", e)
        return started

    def active_chats(self) -> int:
        return sum(len(u.chats) for u in self.users.values())


session_manager = SessionManager()
