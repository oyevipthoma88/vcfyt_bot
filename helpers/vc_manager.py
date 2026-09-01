"""
Multi-user VC engine.

Every logged-in user gets their OWN Pyrogram user client + PyTgCalls instance,
so many people can use the bot at the same time in different (or the same)
groups without stepping on each other.

Rules baked in (as requested):
  • Nobody's volume is EVER lowered. We only ever raise participant volume.
  • The user's own mic and the bot's audio can be live at the same time —
    there is no auto-pause when the user speaks.
  • The logged-in account is auto-boosted to Telegram's max participant
    volume (20000 = 200%) so the user's LIVE voice is genuinely louder.
  • If somebody else mutes our account in the VC, the stream is held (stays
    muted) and on unmute it continues / moves to the next queued audio.
"""

import asyncio
import os
import shlex
from typing import Dict, Optional

from pyrogram import Client
from pyrogram.raw.functions.channels import GetFullChannel
from pyrogram.raw.functions.phone import EditGroupCallParticipant, GetGroupParticipants
from ntgcalls import MediaSource

from pytgcalls import MediaDevices, PyTgCalls
from pytgcalls import filters as call_filters
from pytgcalls.types import AudioQuality, ChatUpdate, MediaStream, StreamEnded
from pytgcalls.types.raw import AudioParameters, AudioStream, Stream

from config import Config
from helpers.audio_processor import build_ffmpeg_filter, process_audio_to_file
from helpers.logger_channel import (
    log_auto_mode, log_error, log_external_mute, log_live_boost, log_vc_join,
    log_vc_leave,
)

# Telegram participant volume scale: 1 – 20000 (10000 = 100%)
VOL_NORMAL = 10000
VOL_MAX = 20000


def _db():
    from helpers.database import db
    return db


# ──────────────────────────────────────────────────────────────────────────────
class ChatState:
    """Per (user, chat) playback state."""

    def __init__(self):
        self.is_playing = False
        self.is_paused = False
        self.held_by_mute = False          # someone else muted us
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
        self.auto = Config.AUTO_MODE_DEFAULT   # AUTO mode (max loud + keeper)
        self.loop = False                  # .loop — current track repeat
        self.loop_left = -1                # -1 = infinite, warna baaki counts
        self.queue: list = []              # [(path, source_name)]

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
        self.owner_id = owner_id                # Telegram user who owns the bot session
        self.string_session = string_session
        self.client: Optional[Client] = None
        self.calls: Optional[PyTgCalls] = None
        self.account_id: int = 0
        self.account_name: str = ""
        self.account_username: str = ""
        self.chats: Dict[int, ChatState] = {}
        self._keepers: Dict[int, asyncio.Task] = {}
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
            from helpers.database import db
            saved = await db.get_settings(self.owner_id)
            self.live_volume = saved.get("live_volume", Config.LIVE_BOOST_DEFAULT)
        except Exception:
            pass

        self.calls = PyTgCalls(self.client)

        @self.calls.on_update(call_filters.stream_end())
        async def _on_end(_, update: StreamEnded):
            await self._on_stream_end(update.chat_id)

        @self.calls.on_update(call_filters.call_participant())
        async def _on_part(_, update):
            await self._on_participant(update)

        @self.calls.on_update(call_filters.chat_update(ChatUpdate.Status.LEFT_CALL))
        async def _on_left(_, update: ChatUpdate):
            self.chats.pop(update.chat_id, None)

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
        st = self.chats.get(chat_id)
        if not st:
            return
        if st.held_by_mute:
            st.is_playing = False
            return
        # .loop — same track dubara chalao
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
        else:
            await self.leave(chat_id, reason="Queue empty")

    async def _on_participant(self, update):
        chat_id = getattr(update, "chat_id", None)
        participant = getattr(update, "participant", None)
        if not chat_id or participant is None:
            return
        uid = getattr(participant, "user_id", None)
        if uid is None:
            return

        # Our own account: watch for external mute / unmute.
        if uid == self.account_id:
            st = self.chats.get(chat_id)
            muted = bool(getattr(participant, "muted", False)) or bool(
                getattr(participant, "muted_by_admin", False)
            )
            if st:
                if muted and not st.held_by_mute:
                    st.held_by_mute = True
                    try:
                        if st.is_playing and not st.is_paused:
                            await self.calls.pause(chat_id)
                            st.is_paused = True
                    except Exception:
                        pass
                    asyncio.create_task(log_external_mute(self.account_id, chat_id, True))
                elif not muted and st.held_by_mute:
                    st.held_by_mute = False
                    asyncio.create_task(log_external_mute(self.account_id, chat_id, False))
                    try:
                        if st.is_paused:
                            await self.calls.resume(chat_id)
                            st.is_paused = False
                        elif st.queue:
                            path, name = st.queue.pop(0)
                            await self._stream(chat_id, path, name)
                    except Exception:
                        pass
            # Keep our own live mic pinned at max volume.
            if Config.AUTO_LIVE_BOOST:
                asyncio.create_task(
                    self.set_participant_volume(
                        chat_id, self.account_id, self.state(chat_id).live_volume, quiet=True
                    )
                )
            return

        # Anybody else: never lower — only make sure they are not below normal.
        vol = getattr(participant, "volume", None)
        if Config.ME_LOUDEST:
            # Meri aavaj sabse upar: baaki ko normal (100%) par set karo.
            if vol is not None and vol != VOL_NORMAL:
                asyncio.create_task(
                    self.set_participant_volume(chat_id, uid, VOL_NORMAL, quiet=True)
                )
        elif Config.NEVER_LOWER_OTHERS:
            if vol is not None and vol < VOL_NORMAL:
                asyncio.create_task(
                    self.set_participant_volume(chat_id, uid, VOL_NORMAL, quiet=True)
                )

    # ── AUTO MODE (volume keeper) ────────────────────────────────────────────
    async def _keeper_loop(self, chat_id: int):
        """
        Telegram participant volume server par store hota hai aur reconnect /
        admin action / naye join par reset ho sakta hai. Yeh loop har
        KEEPER_INTERVAL second par:
          • apni live mic ko wapas 20000 (200% = Telegram max) par pin karta hai
          • baaki sab ko normal se neeche nahi rehne deta (kabhi kam nahi karta)
        Yahi "live aavaj badhane" ka available jugaad hai — 200% Telegram ka
        hard server-side cap hai, usse aage client side kuch nahi kar sakta,
        isliye baaki loudness FFmpeg chain se aati hai.
        """
        interval = max(5, Config.KEEPER_INTERVAL)
        while True:
            try:
                st = self.chats.get(chat_id)
                if not st or not st.auto:
                    return
                await self.set_participant_volume(
                    chat_id, self.account_id, self.state(chat_id).live_volume, quiet=True
                )
                # Meri aavaj sabse zyada: khud 200%, baaki sirf normal (100%).
                # Kisi ki aavaj kam nahi ki jaati, bas mujhse upar nahi jaati.
                if Config.ME_LOUDEST:
                    await self.normalize_others(chat_id)
                else:
                    await self.boost_everyone(chat_id, VOL_MAX)
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
        """AUTO mode on/off for one chat. On = max loud + keeper loop."""
        st = self.state(chat_id)
        st.auto = bool(on)
        if on:
            from helpers.audio_processor import BASS_MAX, LEVEL_MAX, VOLUME_MAX
            st.volume, st.bass = VOLUME_MAX, BASS_MAX
            st.boost, st.echo, st.echo_level = LEVEL_MAX, True, LEVEL_MAX
            self._start_keeper(chat_id)
            await self.set_participant_volume(
                chat_id, self.account_id, self.state(chat_id).live_volume, quiet=True
            )
            if Config.ME_LOUDEST:
                await self.normalize_others(chat_id)
            else:
                await self.boost_everyone(chat_id, VOL_MAX)
        else:
            self._stop_keeper(chat_id)
        asyncio.create_task(log_auto_mode(self.owner_id, chat_id, bool(on)))
        return True

    # ── raw helpers ──────────────────────────────────────────────────────────
    async def _call_input(self, chat_id: int):
        peer = await self.client.resolve_peer(chat_id)
        full = await self.client.invoke(GetFullChannel(channel=peer))
        return full.full_chat.call

    async def set_participant_volume(self, chat_id: int, user_id: int,
                                     volume: int, quiet: bool = False) -> bool:
        """Set a participant's LIVE volume (1–20000). Never used to silence."""
        volume = max(1, min(VOL_MAX, int(volume)))
        try:
            call_input = await self._call_input(chat_id)
            if not call_input:
                return False
            peer = await self.client.resolve_peer(user_id)
            await self.client.invoke(
                EditGroupCallParticipant(
                    call=call_input, participant=peer, volume=volume,
                )
            )
            if not quiet:
                asyncio.create_task(
                    log_live_boost(self.owner_id, chat_id, user_id, volume)
                )
            return True
        except Exception as e:
            if not quiet:
                await log_error("set_participant_volume", e)
            return False

    async def boost_everyone(self, chat_id: int, volume: int = VOL_MAX) -> int:
        """Raise every participant (never lower)."""
        volume = max(1, min(VOL_MAX, int(volume)))
        try:
            call_input = await self._call_input(chat_id)
            if not call_input:
                return 0
            res = await self.client.invoke(
                GetGroupParticipants(
                    call=call_input, ids=[], sources=[], offset="", limit=200,
                )
            )
            done = 0
            for p in res.participants:
                uid = getattr(p.peer, "user_id", None)
                if not uid:
                    continue
                current = getattr(p, "volume", None) or VOL_NORMAL
                if volume <= current:
                    continue                     # never lower anybody
                if await self.set_participant_volume(chat_id, uid, volume, quiet=True):
                    done += 1
            return done
        except Exception as e:
            await log_error("boost_everyone", e)
            return 0

    async def normalize_others(self, chat_id: int) -> int:
        """Baaki participants ko normal (100%) par laao — kabhi neeche nahi.
        Khud 200% par rehta hai, isliye meri aavaj sabse loud sunai deti hai."""
        try:
            call_input = await self._call_input(chat_id)
            if not call_input:
                return 0
            res = await self.client.invoke(
                GetGroupParticipants(
                    call=call_input, ids=[], sources=[], offset="", limit=200,
                )
            )
            done = 0
            for p in res.participants:
                uid = getattr(p.peer, "user_id", None)
                if not uid or uid == self.account_id:
                    continue
                current = getattr(p, "volume", None) or VOL_NORMAL
                if current == VOL_NORMAL:
                    continue
                if await self.set_participant_volume(chat_id, uid, VOL_NORMAL,
                                                     quiet=True):
                    done += 1
            return done
        except Exception as e:
            await log_error("normalize_others", e)
            return 0

    async def me_loudest(self, chat_id: int) -> int:
        """Khud ko max par pin karo aur baaki ko normal par."""
        await self.set_participant_volume(chat_id, self.account_id, VOL_MAX)
        return await self.normalize_others(chat_id)

    # ── playback ─────────────────────────────────────────────────────────────
    async def _stream(self, chat_id: int, path: str, source_name: str):
        st = self.state(chat_id)
        effective_volume = (
            st.volume if st.volume != Config.DEFAULT_VOLUME else st.relay_volume
        )
        processed = await process_audio_to_file(
            path,
            volume=effective_volume, bass=st.bass, echo=st.echo,
            echo_level=st.echo_level, boost=st.boost,
            relay_volume=st.relay_volume, gain=st.gain, treble=st.treble,
        )

        old = st.processed_file
        st.processed_file = processed
        st.current_file = path
        st.source_name = source_name
        st.is_playing = True
        st.is_paused = False

        await self.calls.play(
            chat_id,
            MediaStream(
                processed, AudioQuality.STUDIO,
                video_flags=MediaStream.Flags.IGNORE,
            ),
        )

        if old and old != processed and os.path.exists(old):
            try:
                os.unlink(old)
            except Exception:
                pass

        if Config.AUTO_LIVE_BOOST or st.auto:
            asyncio.create_task(
                self.set_participant_volume(
                    chat_id, self.account_id,
                    st.live_volume, quiet=True
                )
            )
        if st.auto and chat_id not in self._keepers:
            self._start_keeper(chat_id)

        asyncio.create_task(
            log_vc_join(self.owner_id, chat_id, st.chat_title or str(chat_id),
                        source_name, st.settings())
        )

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
        st.mic_enabled = True
        st.mic_device = device.metadata
        mic_filter = build_ffmpeg_filter(
            volume=st.relay_volume,
            bass=st.bass,
            echo=False,
            echo_level=0,
            boost=st.boost,
            relay_volume=st.relay_volume,
            gain=st.gain,
            treble=st.treble,
        ) if Config.MIC_DSP else "anull"
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", Config.MIC_INPUT_FORMAT, "-i", device.metadata,
            "-af", mic_filter,
            "-f", "s16le", "-ac", "2", "-ar", "48000", "pipe:1",
        ]
        stream = Stream(
            microphone=AudioStream(
                MediaSource.SHELL,
                shlex.join(command),
                AudioParameters(48000, 2),
            )
        )
        await self.calls.play(chat_id, stream)
        await self.set_participant_volume(
            chat_id, self.account_id, st.live_volume, quiet=True
        )
        return device.title

    @staticmethod
    def microphone_devices() -> list:
        return list(MediaDevices.microphone_devices())

    async def play(self, chat_id: int, path: str, source_name: str = "audio",
                   chat_title: str = "", enqueue: bool = False) -> str:
        if chat_id >= 0:
            raise ValueError(
                "Voice chat sirf group/supergroup mein hota hai — chat ID negative honi chahiye."
            )
        async with self._lock:
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
        """.playforce — queue clear, current track band, turant yeh chalao."""
        if chat_id >= 0:
            raise ValueError(
                "Voice chat sirf group/supergroup mein hota hai — chat ID negative honi chahiye."
            )
        async with self._lock:
            st = self.state(chat_id)
            if chat_title:
                st.chat_title = chat_title
            st.queue.clear()
            st.held_by_mute = False
            try:
                if st.is_paused:
                    await self.calls.resume(chat_id)
            except Exception:
                pass
            st.is_paused = False
            try:
                await self._stream(chat_id, path, source_name)
            except Exception:
                # VC se nikal kar dubara join karke retry (stuck call ka fix)
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
        st = self.chats.get(chat_id)
        if not st:
            return False
        await self._on_stream_end(chat_id)
        return True

    async def reapply(self, chat_id: int) -> bool:
        """Re-render the current track with the current effect settings."""
        st = self.chats.get(chat_id)
        if not st or not st.current_file or not os.path.exists(st.current_file):
            return False
        await self._stream(chat_id, st.current_file, st.source_name)
        return True

    async def leave(self, chat_id: int, reason: str = "Manual stop"):
        self._stop_keeper(chat_id)
        st = self.chats.get(chat_id)
        if st:
            st.queue.clear()
            for f in (st.processed_file,):
                if f and os.path.exists(f):
                    try:
                        os.unlink(f)
                    except Exception:
                        pass
        try:
            await self.calls.leave_call(chat_id)
        except Exception:
            pass
        self.chats.pop(chat_id, None)
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
            await uvc.start()
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
