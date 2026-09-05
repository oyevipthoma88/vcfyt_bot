
import asyncio
import logging
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
    build_ffmpeg_filter, build_live_mic_filter, process_audio_to_file, shell_quote,
)
from helpers.logger_channel import (
    log_auto_mode, log_error, log_live_boost, log_vc_join, log_vc_leave,
)

logger = logging.getLogger("vcbot.vc_manager")

VOL_NORMAL = 10000
VOL_MAX = 20000

FYT_PARTICIPANT_VOLUME = VOL_MAX

AUTO_PRESET = {
    "volume": 1000, "relay_volume": 1000, "bass": 30, "gain": 150,
    "treble": 80, "boost": 10, "echo": 0, "echo_level": 0,
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
    return (type(error).__name__ == "ParticipantJoinMissing" or
            "PARTICIPANT_JOIN_MISSING" in str(error))

def _connection_lost(error: Exception) -> bool:
    text = str(error).lower()
    return isinstance(error, (OSError, ConnectionError)) and any(
        marker in text for marker in ("connection lost", "connection reset", "broken pipe", "eof")
    )

_INVALID_SESSION_NAMES = frozenset({
    "AuthKeyUnregistered", "AuthKeyInvalid", "SessionRevoked",
    "SessionExpired", "UserDeactivated", "Unauthorized",
    "AuthKeyDuplicated",
})

def _is_invalid_session(error: Exception) -> bool:
    return (type(error).__name__ in _INVALID_SESSION_NAMES or
            "401" in str(error) or "AUTH_KEY_DUPLICATED" in str(error))

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
        self.mic_device = Config.MIC_DEVICE
        self.auto = Config.AUTO_MODE_DEFAULT
        self.loop = False
        self.loop_left = -1
        self.queue: list = []

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
            "mic_enabled": self.mic_enabled, "mic_device": self.mic_device,
            "auto": self.auto, "loop": self.loop,
        }

class UserVC:

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

        self._stopped_chats: set[int] = set()
        self._lock = asyncio.Lock()
        self._reconnect_lock = asyncio.Lock()
        self.live_volume = Config.LIVE_BOOST_DEFAULT

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

    def state(self, chat_id: int) -> ChatState:
        if chat_id not in self.chats:
            self.chats[chat_id] = ChatState()
            self.chats[chat_id].live_volume = self.live_volume
        return self.chats[chat_id]

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

    async def _keeper_loop(self, chat_id: int):
        interval = max(5, Config.KEEPER_INTERVAL)
        while True:
            try:
                st = self.chats.get(chat_id)
                if not st or (not st.auto and not Config.AUTO_LIVE_BOOST):
                    return
                await self.set_participant_volume(
                    chat_id, self.account_id, FYT_PARTICIPANT_VOLUME, quiet=True
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

    async def play_microphone(self, chat_id: int, device_hint: str = "") -> str:
        if Config.MIC_RELAY_ENABLED:
            device = None
            title = "Android Chrome Live Relay"
            input_args = [
                "-f", "s16le", "-ar", "48000", "-ac", "1",
                "-i", Config.MIC_RELAY_FIFO,
            ]
        else:
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
            title = device.title
            input_args = ["-f", Config.MIC_INPUT_FORMAT, "-i", device.metadata]
        st = self.state(chat_id)
        mic_filter = build_live_mic_filter() if Config.MIC_DSP else "anull"
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            *input_args,
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
        st.mic_device = Config.MIC_RELAY_FIFO if Config.MIC_RELAY_ENABLED else device.metadata
        st.is_playing = True
        st.is_paused = False
        st.source_name = f" {title}"
        await self.set_participant_volume(
            chat_id, self.account_id, FYT_PARTICIPANT_VOLUME, quiet=True
        )
        return title

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
            if not _connection_lost(error):
                raise
            logger.warning("Telegram connection lost during playback; reconnecting once")
            await self._reconnect_client()
            await self._stream(chat_id, path, source_name)

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
            await self._stream_with_reconnect(chat_id, path, source_name)
            return "playing"

    async def force_play(self, chat_id: int, path: str, source_name: str = "audio",
                         chat_title: str = "") -> str:
        self._check_group(chat_id)
        async with self._lock:
            self._stopped_chats.discard(chat_id)
            st = self.state(chat_id)
            if chat_title:
                st.chat_title = chat_title
            st.queue.clear()
            st.is_paused = False
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

    async def reapply(self, chat_id: int) -> bool:
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

class SessionManager:

    def __init__(self):
        self.users: Dict[int, UserVC] = {}
        self._locks: Dict[int, asyncio.Lock] = {}

    def _lock(self, user_id: int) -> asyncio.Lock:
        if user_id not in self._locks:
            self._locks[user_id] = asyncio.Lock()
        return self._locks[user_id]

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
            return uvc

    async def get(self, user_id: int) -> Optional[UserVC]:
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
            if _is_invalid_session(e):
                await _invalidate_session(user_id, e, "on-demand start")
            else:
                await log_error(f"session_start_{user_id}", e)
            return None

    async def remove(self, user_id: int):
        uvc = self.users.pop(user_id, None)
        if uvc:
            await uvc.stop()

    async def restore_all(self) -> int:
        try:
            users = await _db().all_users()
        except Exception as e:
            await log_error("restore_all_db", e)
            return 0
        started = 0
        for u in users:
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

    def active_chats(self) -> int:
        return sum(len(u.chats) for u in self.users.values())

session_manager = SessionManager()
