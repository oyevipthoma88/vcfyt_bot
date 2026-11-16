import html

import asyncio
import os

import re
from pyrogram import Client, filters
from pyrogram.errors import (SessionPasswordNeeded, PhoneCodeInvalid,
                             PhoneCodeExpired, PasswordHashInvalid)
from plugins.ui import now_playing_text
from plugins.ui import card, meter, HAS_USER, cmd_text, B, edit_screen, safe_answer, mic_text, mic_kb, cmd_prefix
from pyrogram.types import InlineKeyboardMarkup as K
from pyrogram.types import Message

from config import Config
from helpers.access_control import check_access, record_usage
from helpers.audio_processor import (
    BASS_MAX, BASS_MIN, GAIN_MAX, LEVEL_MAX, LEVEL_MIN, VOLUME_MAX, VOLUME_MIN, clamp,
)
from helpers.database import db
from helpers.logger_channel import (
    get_channel, log_command, log_error, set_channel, verify_log_channel,
)
from helpers.vc_manager import AUTO_PRESET, VOL_MAX, VOL_NORMAL, session_manager

LINE = "━" * 28

LOGIN_KB = K([
    [B("🔐 Login", callback_data="menu:login", style="success")],
    [B("📘 Help", callback_data="menu:tutorial", style="primary")],
])

LIMIT_KB = K([
    [B("💳 Buy Premium", callback_data="pay:menu")],
    [B("👥 Refer & Earn", callback_data="ref:menu")],
    [B("🏠 Home", callback_data="menu:home")],
])

async def _check_usage_access(msg: Message) -> bool:
    """Gate: returns True if user can use the bot, False if limit reached."""
    from helpers import sudo as _sudo
    mode = await _sudo.engine_mode(msg.from_user.id, fresh=True)
    if mode in _sudo.SUDO_ENGINE_MODES or mode == _sudo.NONE:
        # Bina login (Sudo ID): verified group + trial/premium/sudo rules
        from plugins.sudo import gate_sudo_use
        return await gate_sudo_use(msg)
    access = await check_access(msg.from_user.id)
    if access["allowed"]:
        await record_usage(msg.from_user.id)
        return True
    usage = access["usage_today"]
    limit = access["limit"]
    await msg.reply_text(
        f"⚠️ <b>Daily Limit Reached!</b>\n\n"
        f"Aapne aaj <b>{usage}/{limit}</b> uses kar liye hain.\n\n"
        f"🔄 <b>Options:</b>\n"
        f"• Premium lein — unlimited uses\n"
        f"• Dosto ko refer karein — free premium hours\n\n"
        f"<i> Kal limit reset ho jayegi.</i>",
        reply_markup=LIMIT_KB,
    )
    return False

def friendly_error(error: Exception) -> str:
    """Human readable Hindi/Hinglish message for common playback failures."""
    from helpers.peer_guard import PEER_HELP, PeerAccessError, is_peer_error
    name = type(error).__name__
    text = str(error)
    if name == "MTProtoClientNotConnected" or "not connected" in text.lower():
        return ("❌ <b>Connection lost</b>\n\n"
                "Telegram session disconnect ho gaya. Bot reconnect kar raha hai — "
                "ek do second ruk kar dobara <code>.play</code> karein.")
    if "has not been started" in text.lower():
        return ("❌ <b>Session not ready</b>\n\n"
                "Account abhi connect nahi hua. Kuch second ruk kar "
                "dobara <code>.play</code> karein.")
    if isinstance(error, PeerAccessError):
        return f"❌ <b>Chat access nahi mila</b>\n\n{PEER_HELP}"
    if is_peer_error(error):
        return f"❌ <b>Chat access nahi mila</b>\n\n{PEER_HELP}"
    if name in ("ChatAdminRequired", "ChatWriteForbidden") or "ADMIN" in text.upper():
        return ("❌ <b>Permission missing</b>\n\nLogged-in account ko group me "
                "<b>Manage video chats</b> admin right dein, phir dobara try karein.")
    if "GROUPCALL_FORBIDDEN" in text.upper():
        return ("❌ Voice chat band ho gayi ya account ko VC join karne ki "
                "permission nahi hai. VC dobara start karke try karein.")
    if name == "FloodWait" or "FLOOD_WAIT" in text.upper():
        return ("⏳ Telegram ne thoda rate limit laga diya hai. Kuch second ruk kar "
                "dobara <code>.play</code> karein.")
    if name == "FFmpegError" or "not installed" in text.lower():
        return ("❌ <b>Audio engine start nahi hua</b>\n\n"
                "Server par ffmpeg/ffprobe nahi mila. Bot ko ek baar restart karein — "
                "startup par yeh apne aap install ho jaata hai.")
    if name in ("NoAudioSourceFound",):
        return "❌ Is file mein audio nahi mila — koi aur audio/video bhejein."
    if "FFmpeg failed" in text:
        return "❌ Audio process nahi ho paaya — dusri file try karein."
    return f"❌ Error: <code>{text[:400]}</code>"

def _cleanup_source(path: str):
    if path and os.path.exists(path):
        try:
            os.unlink(path)
        except OSError:
            pass

def _cached_file_id(message):
    media = (getattr(message, "audio", None) or getattr(message, "voice", None)
             or getattr(message, "video", None) or getattr(message, "document", None)
             or getattr(message, "video_note", None))
    return getattr(media, "file_id", None) if media else None

async def _archive_played_audio(bot: Client, source_file_id: str, title: str):
    if not source_file_id or not Config.AUDIO_ARCHIVE_CHANNEL:
        return
    if await db.get_archived_audio(source_file_id):
        return
    try:
        archived = await bot.send_cached_media(
            Config.AUDIO_ARCHIVE_CHANNEL, source_file_id,
            caption=f"🎧 <b>{title}</b>\n🗃️ VC Fyt audio archive",
        )
        archive_file_id = _cached_file_id(archived)
        if archive_file_id:
            await db.save_archived_audio(
                source_file_id, archive_file_id, title,
                "audio" if getattr(archived, "audio", None) or getattr(archived, "voice", None) else "video",
            )
    except Exception as exc:
        await log_error("archive_played_audio", exc)

_GC_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "default_gc.json")
_PLAYMUTE_DIR = os.path.join(os.path.dirname(_GC_FILE), "playmute_audio")


def _load_gc() -> dict:
    try:
        import json
        with open(_GC_FILE) as f:
            return {int(k): int(v) for k, v in json.load(f).items()}
    except Exception:
        return {}


DEFAULT_GC: dict = _load_gc()


_GC_DB_KEY = "default_gc_map"


def _gc_dump() -> str:
    import json
    return json.dumps({str(k): int(v) for k, v in DEFAULT_GC.items() if v})


def _gc_parse(raw) -> dict:
    import json
    try:
        data = json.loads(raw or "{}")
        return {int(k): int(v) for k, v in data.items() if int(v)}
    except Exception:
        return {}


def _save_gc():
    """v94: file + DB dono.  Heroku ka disk har restart par saaf hota hai, isliye
    sirf default_gc.json me save karne se sabka default group restart par mit jaata tha."""
    payload = _gc_dump()
    try:
        with open(_GC_FILE, "w") as f:
            f.write(payload)
    except Exception:
        pass
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    async def _persist():
        try:
            await db.set_app_value(_GC_DB_KEY, payload)
        except Exception:
            pass
    loop.create_task(_persist())


async def load_gc_from_db() -> int:
    """Startup par DB se default groups wapas laao (file me jo hai wo bhi rahega)."""
    try:
        saved = _gc_parse(await db.get_app_value(_GC_DB_KEY))
    except Exception:
        return 0
    for uid, cid in saved.items():
        DEFAULT_GC.setdefault(uid, cid)
    return len(saved)


def now_playing_kb(cid: int, st=None) -> K:
    # v81: sabse zaroori upar — controls, phir TEZ, phir extras.
    paused = bool(st and st.is_paused)
    human = bool(st and st.hand_raise)
    ss = bool(st and getattr(st, "ss_on", False))
    loop = bool(st and st.loop)
    def onoff(v):
        return "success" if v else "danger"
    try:
        from plugins.ui import _loud_mode, LOUD_MODE_INFO
        ico, mname, _ = LOUD_MODE_INFO.get(_loud_mode(), LOUD_MODE_INFO["loudest"])
    except Exception:
        ico, mname = "📢", "Loudest"
    return K([
        [B("▶ Resume" if paused else "⏸ Pause",
           callback_data=f"vc:{'resume' if paused else 'pause'}:{cid}",
           style="success" if paused else "primary"),
         B("⏭ Skip", callback_data=f"vc:skip:{cid}", style="primary"),
         B("⏹ Stop", callback_data=f"vc:stop:{cid}", style="danger")],
        [B("⚡ TEZ — sab MAX", callback_data=f"vc:tez:{cid}", style="success"),
         B(f"{ico} Mode: {mname}", callback_data=f"vc:lmode:{cid}", style="primary")],
        [B(f"🔁 Loop {'ON' if loop else 'OFF'}", callback_data=f"vc:loop:{cid}", style=onoff(loop)),
         B(f"🧑 Human {'ON' if human else 'OFF'}", callback_data=f"vc:human:{cid}", style=onoff(human)),
         B(f"🖥 Screen {'ON' if ss else 'OFF'}", callback_data=f"vc:ss:{cid}", style=onoff(ss))],
        [B("👻 Ghost Mic", callback_data=f"gm:play:{cid}", style="primary"),
         B("💬 Chat + React", callback_data=f"vct:panel:{cid}", style="primary")],
        [B("🎚️ Sound", callback_data="menu:settings", style="primary"),
         B("🔄 Refresh", callback_data=f"vc:now:{cid}", style="primary"),
         B("🏠 Home", callback_data="menu:home", style="primary")],
    ])

async def _cb_engine(uid: int):
    """Saved login ko bhi start karta hai — pehle sirf in-memory session
    dekha jaata tha, isliye restart ke baad Live Mic 'login karo' bolta tha."""
    uvc = session_manager.users.get(uid)
    if uvc:
        return uvc
    try:
        uvc = await session_manager.get(uid)
    except Exception:
        uvc = None
    if not uvc:
        try:
            if Config.is_owner(uid) and Config.STRING_SESSION:
                uvc = await session_manager.add(uid, Config.STRING_SESSION)
        except Exception:
            uvc = None
    return uvc

async def get_engine(msg: Message):
    uid = msg.from_user.id
    uvc = await session_manager.get(uid)
    if not uvc:
        from plugins.start import _is_logged_in
        if await _is_logged_in(uid):
            # Logged-in user: session sirf start nahi hua (network / flood) —
            # ek baar retry, aur "login karo" mat bolo.
            import asyncio
            await asyncio.sleep(1.5)
            uvc = await session_manager.get(uid)
            if not uvc:
                try:
                    from config import Config
                    if Config.is_owner(uid) and Config.STRING_SESSION:
                        uvc = await session_manager.add(uid, Config.STRING_SESSION)
                except Exception:
                    uvc = None
            if not uvc:
                await mic_notify(
                    msg,
                    "⚠️ <b>Aapka account connect ho raha hai…</b>\n\n"
                    "Login saved hai, account start ho raha hai — thodi der me dobara try karo.\n"
                    "Baar-baar aaye to 🔁 Re-Login karo.",
                    reply_markup=LOGIN_KB,
                )
            return uvc
        from plugins.sudo import no_engine_notice
        await no_engine_notice(msg)
    return uvc

_CHAT_ID_RE = re.compile(r"-?\d{7,}")


def is_chat_id_token(p) -> bool:
    """Chat ID with OR without the -100 prefix (1234567890, -1234567890,
    -1001234567890).  7+ digits so volume/numbers (<=5 digits) never match."""
    return bool(p) and bool(_CHAT_ID_RE.fullmatch(str(p).strip()))


def is_chat_ref(p) -> bool:
    p = str(p or "")
    return is_chat_id_token(p) or p.startswith("@") or "t.me/" in p or p.startswith("http")


async def target_chat(msg: Message, arg: str = None) -> tuple:
    """Return (chat_id, join_reference) where join_reference is a username or
    invite link the logged-in account can use to join the group if needed."""
    uid0 = msg.from_user.id if getattr(msg, "from_user", None) else 0
    if uid0:
        # Sudo mode: sirf wahi group jo user ne link bhej kar verify kiya.
        # Shared Sudo ID kisi aur ke group ko control na kare.
        from helpers import sudo as _sudo
        if await _sudo.is_sudo_mode(uid0):
            gc_id, gc_ref, _ = await _sudo.get_gc(uid0)
            return (gc_id, gc_ref or None) if gc_id else (0, None)
    if arg:
        try:
            int(arg)
            is_num = True
        except ValueError:
            is_num = False
        if is_num:
            from helpers.peer_guard import chat_id_variants, safe_get_chat
            resolvers = [msg._client]
            try:
                uvc = await session_manager.get(msg.from_user.id)
                if uvc and getattr(uvc, "client", None):
                    resolvers.insert(0, uvc.client)
            except Exception:
                pass
            chat = await safe_get_chat(resolvers, arg)
            if chat:
                return chat.id, None
            variants = chat_id_variants(arg)
            return (variants[0] if variants else int(arg)), None
        else:
            arg = arg.strip()
            ref = arg if arg.startswith("@") or "t.me/" in arg or "+" in arg else None
            m = re.search(r"t\.me/(?!\+|joinchat/)([A-Za-z0-9_]{4,})", arg)
            if m:
                # Public link https://t.me/name -> "name" (get_chat URL nahi leta)
                arg = m.group(1)
            elif arg.startswith("@"):
                arg = arg[1:]
            # Bot first; if the bot was never added to that group (or it is a
            # private group), fall back to the user's own logged-in account —
            # that one IS a member, so it can resolve the chat.
            resolvers = [msg._client]
            try:
                uvc = await session_manager.get(msg.from_user.id)
                if uvc and getattr(uvc, "client", None):
                    resolvers.append(uvc.client)
            except Exception:
                pass
            for client in resolvers:
                try:
                    chat = await client.get_chat(arg)
                    if chat and chat.id:
                        return chat.id, ref
                except Exception:
                    continue
            return 0, ref
    uid = msg.from_user.id if msg.from_user else 0
    if uid in DEFAULT_GC:
        return DEFAULT_GC[uid], None
    if msg.chat and msg.chat.id < 0:
        return msg.chat.id, None
    return 0, None

async def mic_notify(msg: Message, text: str, **kwargs):
    """Send a mic reply without ever letting a group restriction abort the flow.

    In groups the answer (and the private mic link) goes to the user's DM, so
    it also works when the user's ID is muted in the group or the bot is not
    an admin there.  Any send failure is swallowed — the relay must still run.
    """
    bot = msg._client
    uid = msg.from_user.id if msg.from_user else 0
    targets = []
    if msg.chat and msg.chat.id < 0 and uid:
        targets = [uid, msg.chat.id]
    elif msg.chat:
        targets = [msg.chat.id]
    else:
        targets = [uid] if uid else []
    for chat_id in targets:
        try:
            return await bot.send_message(chat_id, text, **kwargs)
        except Exception:
            continue
    return None

async def need_chat(msg: Message, arg: str = None) -> int:
    cid, _ = await target_chat(msg, arg)
    if not cid:
        await mic_notify(
            msg,
            "ℹ️ Voice chat sirf <b>groups</b> mein hota hai.\n"
            "Group mein command chalayein, ya group ka chat ID / username / invite link dein:\n"
            "<code>.play &lt;source&gt; -1001234567890</code>\n"
            "<code>.play &lt;source&gt; @groupusername</code>\n"
            "<code>.play &lt;source&gt; https://t.me/+invitehash</code>"
        )
        return cid
    try:
        await db.register_broadcast_chat(cid)
    except Exception:
        pass
    return cid

async def load_state_settings(user_id: int, uvc, chat_id: int):
    s = await db.get_settings(user_id)
    st = uvc.state(chat_id)
    st.apply_settings(s)
    if bool(s.get("auto")) != bool(st.auto):
        await uvc.set_auto(chat_id, bool(s.get("auto")))
    return st

@Client.on_message(HAS_USER & cmd_prefix(r"tag\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_tag(bot: Client, msg: Message):
    parts = cmd_text(msg).split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply_text("Usage: <code>.tag &lt;name&gt;</code> (audio ko reply karke)")
        return
    media, dl = await _find_reply_media(bot, msg)
    if media is not None and dl is not bot:
        media = None  # file_id seen only by the user account: bot can't replay it
    if not media:
        await msg.reply_text("ℹ️ Kisi audio/video message ko reply karke <code>.tag</code> likhein.")
        return
    name = parts[1].strip().lower()
    reply = msg.reply_to_message
    ftype = "audio" if type(media).__name__ in ("Audio", "Voice") else "video"
    caption = (getattr(reply, "caption", None) or "") if reply else ""
    await db.tag_file(msg.from_user.id, name, media.file_id, ftype, caption)
    await msg.reply_text(f"✅ Saved as <code>{name}</code> — ab <code>.play {name}</code>")

@Client.on_message(HAS_USER & cmd_prefix(r"untag\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_untag(bot: Client, msg: Message):
    parts = cmd_text(msg).split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply_text("Usage: <code>.untag &lt;name&gt;</code>")
        return
    name = parts[1].strip().lower()
    if not await db.get_tag(msg.from_user.id, name):
        await msg.reply_text(f"❌ Tag <code>{name}</code> nahi mila.")
        return
    await db.delete_tag(msg.from_user.id, name)
    await msg.reply_text(f"✅ <code>{name}</code> delete ho gaya.")

@Client.on_message(HAS_USER & cmd_prefix(r"tags\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_tags(bot: Client, msg: Message):
    tags = await db.list_tags(msg.from_user.id)
    if not tags:
        await msg.reply_text("❌ Koi tag nahi. <code>.tag &lt;name&gt;</code> se save karein.")
        return
    lines = [f"• <code>{t['tag_name']}</code> — {t['file_type']}" for t in tags]
    await msg.reply_text("ℹ️ <b>Your Tags</b>\n" + "\n".join(lines))

def _media_of(m):
    if not m:
        return None
    return (getattr(m, "audio", None) or getattr(m, "voice", None)
            or getattr(m, "video", None) or getattr(m, "video_note", None)
            or getattr(m, "animation", None) or getattr(m, "document", None))


async def _find_reply_media(bot: Client, msg: Message):
    """ROOT FIX for '.play' randomly showing Usage:
    * reply object missing (forum topics / old msg / not cached) -> fetch by id
    * audio sent WITH '.play' as caption (no reply)
    * reply is to a text that itself replies to the audio
    Returns (media, downloader_client)."""
    if _media_of(msg):
        return _media_of(msg), bot
    reply = msg.reply_to_message
    rid = getattr(msg, "reply_to_message_id", None) or getattr(reply, "id", None)
    if (reply is None or not _media_of(reply)) and rid:
        try:
            reply = await bot.get_messages(msg.chat.id, rid) or reply
        except Exception:
            pass
    if _media_of(reply):
        return _media_of(reply), bot
    inner = getattr(reply, "reply_to_message", None) if reply else None
    if _media_of(inner):
        return _media_of(inner), bot
    # Last resort: the user's own logged-in account can see the message
    # even when the bot can't (bot privacy / not admin).
    if rid and msg.from_user:
        try:
            uvc = await session_manager.get(msg.from_user.id)
            uc = getattr(uvc, "client", None) if uvc else None
            if uc:
                m = await uc.get_messages(msg.chat.id, rid)
                if _media_of(m):
                    return _media_of(m), uc
        except Exception:
            pass
    return None, bot


async def resolve_source(bot: Client, msg: Message, arg: str):
    media, dl = await _find_reply_media(bot, msg)
    if media:
        if media:
            source_file_id = media.file_id
            stat = await msg.reply_text("⬇ Media download ho raha hai…")
            try:
                archived = await db.get_archived_audio(source_file_id) if dl is bot else None
                path = await dl.download_media(archived["archive_file_id"] if archived else source_file_id)
            except Exception as e:
                await stat.edit_text(f"❌ Download fail: <code>{e}</code>")
                await log_error("resolve_source_reply", e)
                return None, None, None
            await stat.delete()
            return path, getattr(media, "file_name", None) or "Reply media", source_file_id

    if arg:

        tag = await db.get_tag(msg.from_user.id, arg.split()[0].lower())
        if tag:
            source_file_id = tag["file_id"]
            stat = await msg.reply_text("⬇ Tagged file download ho rahi hai…")
            try:
                archived = await db.get_archived_audio(source_file_id)
                path = await bot.download_media(archived["archive_file_id"] if archived else source_file_id)
            except Exception as e:
                await stat.edit_text(f"❌ Download fail: <code>{e}</code>")
                return None, None, None
            await stat.delete()
            return path, arg, source_file_id
        await msg.reply_text(
            " Sirf Telegram audio/video reply ya saved tag use karein. "
            "External links aur search playback supported nahi hai."
        )
        return None, None, None

    await msg.reply_text(
        "<b>Usage</b>\n"
        "• audio/video reply + <code>.play</code>\n"
        "• audio/video reply + <code>.play &lt;chat_id&gt;</code>\n"
        "• <code>.play &lt;tag&gt;</code>\n"
        "• <code>.play &lt;tag&gt; &lt;group_chat_id&gt;</code>"
    )
    return None, None, None

def _split_args(msg: Message):
    parts = cmd_text(msg).split()
    words, cid = [], None
    for p in parts[1:]:
        if is_chat_id_token(p):
            cid = p
            continue
        if p.startswith("@") or "t.me/" in p:
            cid = p
            continue
        words.append(p)
    return (" ".join(words) or None), cid

async def _play(bot: Client, msg: Message, enqueue: bool):
    await log_command(msg.from_user.id, msg.from_user.username, msg.chat.id,
                      ".padd" if enqueue else ".play")
    uvc = await get_engine(msg)
    if not uvc:
        return
    if not await _check_usage_access(msg):
        return
    source_arg, cid_arg = _split_args(msg)
    cid, join_ref = await target_chat(msg, cid_arg)
    if not cid:
        await msg.reply_text(
            " Voice chat sirf <b>groups</b> mein hota hai.\n"
            "Group mein command chalayein, ya group ka chat ID / username / invite link dein:\n"
            "<code>.play &lt;source&gt; -1001234567890</code>\n"
            "<code>.play &lt;source&gt; @groupusername</code>\n"
            "<code>.play &lt;source&gt; https://t.me/+invitehash</code>"
        )
        return
    try:
        await db.register_broadcast_chat(cid)
    except Exception:
        pass

    path, name, source_file_id = await resolve_source(bot, msg, source_arg)
    if not path or not os.path.exists(path):
        return

    try:
        chat = await bot.get_chat(cid)
        title = chat.title or str(cid)
    except Exception:
        title = str(cid)

    try:
        await db.register_broadcast_chat(cid, title)
    except Exception:
        pass
    st = await load_state_settings(msg.from_user.id, uvc, cid)
    stat = await msg.reply_text("ℹ️ Audio process ho raha hai…")
    try:
        status = await uvc.play(cid, path, name, title, enqueue=enqueue, join_ref=join_ref)
    except Exception as e:
        _cleanup_source(path)
        await stat.edit_text(friendly_error(e))
        await log_error("cmd_play", e)
        from plugins.sudo import refund_if_trial
        await refund_if_trial(msg.from_user.id)
        return
    await _archive_played_audio(bot, source_file_id, name)
    if status == "parked":
        await stat.edit_text(
            "⏳ <b>Ready — unmute ke baad bajega</b>\n\n"
            f" <b>Source:</b> {name}\n"
            f" <b>Chat:</b> {title}\n"
            "Admin ne bot ko mute kiya hai (ya unmute audio chal raha hai). "
            "Unmute + mic check + <code>.playmute</code> audio ke turant baad "
            "yahi recording chalegi (purani recording ki jagah)."
        )
        return

    _q = len(uvc.chats[cid].queue) if (status == 'queued' and uvc.chats.get(cid)) else 0
    await stat.edit_text(
        now_playing_text(uvc.chats.get(cid) or st, "queued" if status == "queued" else "playing",
                         title, getattr(uvc, "account_name", ""), _q),
        reply_markup=now_playing_kb(cid, uvc.chats.get(cid)),
    )

@Client.on_message(HAS_USER & cmd_prefix(r"play(?!force)\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_play(bot: Client, msg: Message):
    await _play(bot, msg, enqueue=False)

@Client.on_message(HAS_USER & cmd_prefix(r"(playforce|fplay)\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_playforce(bot: Client, msg: Message):
    await log_command(msg.from_user.id, msg.from_user.username, msg.chat.id,
                      ".playforce")
    uvc = await get_engine(msg)
    if not uvc:
        return
    if not await _check_usage_access(msg):
        return
    source_arg, cid_arg = _split_args(msg)
    cid, join_ref = await target_chat(msg, cid_arg)
    if not cid:
        await msg.reply_text(
            " Voice chat sirf <b>groups</b> mein hota hai.\n"
            "Group mein command chalayein, ya group ka chat ID / username / invite link dein:\n"
            "<code>.play &lt;source&gt; -1001234567890</code>\n"
            "<code>.play &lt;source&gt; @groupusername</code>\n"
            "<code>.play &lt;source&gt; https://t.me/+invitehash</code>"
        )
        return
    try:
        await db.register_broadcast_chat(cid)
    except Exception:
        pass
    path, name, source_file_id = await resolve_source(bot, msg, source_arg)
    if not path or not os.path.exists(path):
        return
    try:
        chat = await bot.get_chat(cid)
        title = chat.title or str(cid)
    except Exception:
        title = str(cid)

    st = await load_state_settings(msg.from_user.id, uvc, cid)
    stat = await msg.reply_text("ℹ️ <b>FORCE PLAY</b> — process ho raha hai…")
    try:
        await uvc.force_play(cid, path, name, title, join_ref=join_ref)
    except Exception as e:
        _cleanup_source(path)
        await stat.edit_text(friendly_error(e))
        await log_error("cmd_playforce", e)
        from plugins.sudo import refund_if_trial
        await refund_if_trial(msg.from_user.id)
        return
    await _archive_played_audio(bot, source_file_id, name)
    await stat.edit_text(
        now_playing_text(uvc.chats.get(cid) or st, "force", title, getattr(uvc, "account_name", "")),
        reply_markup=now_playing_kb(cid, uvc.chats.get(cid)),
    )

@Client.on_message(HAS_USER & cmd_prefix(r"loop\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_loop(bot: Client, msg: Message):
    await log_command(msg.from_user.id, msg.from_user.username, msg.chat.id, ".loop")
    uvc = await get_engine(msg)
    if not uvc:
        return
    parts = cmd_text(msg).split()
    cid_arg = None
    words = []
    for p in parts[1:]:
        # A chat id / @username / invite link is a target, not the on/off word.
        if is_chat_ref(p):
            cid_arg = p
        else:
            words.append(p)
    arg = words[0].lower() if words else "on"
    cid = await need_chat(msg, cid_arg)
    if not cid:
        return
    if arg in ("off", "0", "no", "band", "stop"):
        uvc.set_loop(cid, False)
        await msg.reply_text("ℹ️ <b>Loop OFF</b>")
        return
    count = -1
    if arg.isdigit():
        count = max(1, int(arg))
    uvc.set_loop(cid, True, count)
    await msg.reply_text(
        " <b>Loop ON</b> — " + ("infinite (jab tak <code>.loop off</code> na karein)"
                                  if count < 0 else f"{count} baar aur")
    )

async def dm_only(msg: Message, text: str, **kwargs):
    """Send strictly to the user's DM (never into the group)."""
    bot = msg._client
    uid = msg.from_user.id if msg.from_user else 0
    if not uid:
        return None
    try:
        return await bot.send_message(uid, text, **kwargs)
    except Exception:
        if msg.chat and msg.chat.id > 0:
            try:
                return await msg.reply_text(text, **kwargs)
            except Exception:
                return None
        try:
            await msg.reply_text(
                " Iska jawab aapke DM me bheja jaata hai. "
                "Pehle bot ko DM me <code>.start</code> karein."
            )
        except Exception:
            pass
    return None


@Client.on_message(HAS_USER & cmd_prefix(r"padd\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_padd(bot: Client, msg: Message):
    await _play(bot, msg, enqueue=True)

def _parse_unmute_loops(argument: str) -> int:
    """Parse only the PlayMute options, never a destination chat ID."""
    loops = 1
    for token in (argument or "").split():
        low = token.lower()
        if low in {"loop", "inf", "infinite", "always"}:
            return -1
        if low.isdigit():
            loops = max(1, min(100, int(low)))
    return loops


@Client.on_message(HAS_USER & cmd_prefix(r"(playmute|unmuteaudio|pmclr)\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_unmuteaudio(bot: Client, msg: Message):
    """Set an audio to play when bot is unmuted after admin mute."""
    uvc = await get_engine(msg)
    if not uvc:
        return
    # ROOT FIX: these two lines (and the clear branch) had been pasted into
    # `.tag` by mistake, so `.playmute` crashed with NameError every time.
    source_arg, cid_arg = _split_args(msg)
    is_clear = (cmd_text(msg).split()[0].lstrip("./!").lower().split("@", 1)[0] == "pmclr"
                or (source_arg or "").lower() == "clear")
    if is_clear:
        cid = await need_chat(msg, cid_arg)
        if not cid:
            return
        uvc.clear_playmute(cid)
        await msg.reply_text("✅ PlayMute audio aur saved archive reference clear kar diya.")
        return
    # Same robust lookup as `.play`: reply object missing (forum topic /
    # uncached), audio sent with `.playmute` as caption, reply-to-a-reply,
    # or only the logged-in account can see the message.
    media, dl = await _find_reply_media(bot, msg)
    if not media:
        await msg.reply_text(
            "🎧 Kisi audio/voice/video message ko <b>reply</b> karke "
            "<code>.playmute</code> likhein.\n"
            "Admin unmute karega → 10-15 sec ruk ke pehle ye audio poora bajega, "
            "phir main audio shuru se.\n"
            "Repeat: <code>.playmute 3</code> · hamesha: <code>.playmute loop</code> · "
            "hatao: <code>.pmclr</code>"
        )
        return
    cid = await need_chat(msg, cid_arg)
    if not cid:
        return
    st = uvc.state(cid)
    stat = await msg.reply_text("⬇ PlayMute audio download ho raha hai…")
    try:
        path = await dl.download_media(media.file_id)
    except Exception as e:
        await stat.edit_text(f"❌ Download fail: <code>{html.escape(str(e))[:300]}</code>")
        return
    if not path or not os.path.exists(path):
        await stat.edit_text("❌ Download fail: file nahi mili. Dobara try karein.")
        return
    try:
        await stat.delete()
    except Exception:
        pass
    archive_file_id = None
    # A Telegram file_id survives local temp cleanup/deploys. Keep this
    # announcement in the configured private archive channel when available.
    if Config.AUDIO_ARCHIVE_CHANNEL and dl is bot:
        try:
            archived = await bot.send_cached_media(
                Config.AUDIO_ARCHIVE_CHANNEL, media.file_id,
                caption=f"PlayMute · owner {uvc.owner_id} · chat {cid}")
            archived_media = _cached_file_id(archived)
            if archived_media:
                archive_file_id = archived_media
        except Exception as exc:
            await log_error("playmute_archive", exc)
    if not archive_file_id and dl is bot:
        # The bot's own file_id stays downloadable for the bot forever, so the
        # PlayMute audio survives restarts even without an archive channel.
        archive_file_id = media.file_id
    try:
        import shutil
        os.makedirs(_PLAYMUTE_DIR, exist_ok=True)
        keep = os.path.join(_PLAYMUTE_DIR, f"{uvc.owner_id}_{abs(cid)}{os.path.splitext(path)[1] or '.mp3'}")
        if st.unmute_audio and os.path.exists(st.unmute_audio) and st.unmute_audio != keep:
            _cleanup_source(st.unmute_audio)
        shutil.move(path, keep)
        path = keep
    except Exception as exc:
        await log_error("playmute_store", exc)
    st.unmute_audio = path
    st.unmute_file_id = archive_file_id
    loops = _parse_unmute_loops(source_arg)
    st.unmute_loop = loops
    st.unmute_loop_left = loops
    uvc.save_mute_prefs(cid)
    loop_txt = "♾ loop" if loops < 0 else f"{loops}x"
    await msg.reply_text(
        f"✅ <b>PlayMute audio set!</b> ({loop_txt})\n\n"
        "Admin is ID ko mute kare to:\n"
        "1️⃣ 2-3 sec me ✋ hand raise\n"
        "2️⃣ Unmute hote hi mic ON (bina blink)\n"
        f"3️⃣ 10-15 sec baad ye audio poora ({loop_txt})\n"
        "4️⃣ Phir main audio <b>shuru se</b>"
        + ("\n\n✅ Restart ke baad bhi yaad rahega." if archive_file_id else
           "\n\n⚠️ Restart ke baad dobara set karna padega.")
    )


@Client.on_message(HAS_USER & cmd_prefix(r"handraise\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_handraise(bot: Client, msg: Message):
    """Toggle Telegram's own 'raise hand' when an admin mutes the bot."""
    uvc = await get_engine(msg)
    if not uvc:
        return
    parts = cmd_text(msg).split()
    arg = parts[1].lower() if len(parts) > 1 else ""
    cid = await need_chat(msg, parts[2] if len(parts) > 2 else None)
    if not cid:
        return
    st = uvc.state(cid)
    if arg in ("on", "off"):
        st.hand_raise = (arg == "on")
        uvc.save_mute_prefs(cid)
    elif arg == "now":
        ok = await uvc.raise_hand(cid, True)
        await msg.reply_text("✋ Hand raise ho gaya." if ok else
                             "❌ Hand raise nahi hua (VC me nahi ya permission nahi).")
        return
    elif arg:
        await msg.reply_text("✅ Use: <code>.handraise on|off|now</code>")
        return
    await msg.reply_text(
        f"✋ <b>Hand raise:</b> <code>{'ON' if st.hand_raise else 'OFF'}</code>\n"
        "Admin mute kare to 2-3 sec baad hand raise; unmute ke 10-15 sec baad "
        "PlayMute audio (agar set hai) aur phir main audio shuru se."
    )


@Client.on_message(HAS_USER & cmd_prefix(r"micblink\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_micblink(bot: Client, msg: Message):
    """Toggle / set the silent mic on-off window after an admin unmute."""
    uvc = await get_engine(msg)
    if not uvc:
        return
    parts = cmd_text(msg).split()
    arg = parts[1].lower() if len(parts) > 1 else ""
    cid = await need_chat(msg, parts[2] if len(parts) > 2 else None)
    if not cid:
        return
    st = uvc.state(cid)
    if arg in ("on", "off"):
        st.mic_blink = (arg == "on")
        uvc.save_mute_prefs(cid)
    elif arg.isdigit():
        st.mic_blink_secs = max(1, min(60, int(arg)))
        st.mic_blink = True
        uvc.save_mute_prefs(cid)
    elif arg:
        await msg.reply_text("✅ Use: <code>.micblink on|off|&lt;seconds&gt;</code>")
        return
    await msg.reply_text(
        f"🎙 <b>Mic blink:</b> <code>{'ON' if st.mic_blink else 'OFF'}</code> — "
        f"<code>{st.mic_blink_secs}s</code>\n"
        + ("Unmute hone par pehle mic on/off blink hoga." if st.mic_blink else
           "Unmute hone par mic seedha ON rahega (blink nahi) — human jaisa.")
    )


@Client.on_message(HAS_USER & cmd_prefix(r"loud\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_loud(bot: Client, msg: Message):
    """Extra loudness on top of volume/boost (0-18 dB)."""
    uvc = await get_engine(msg)
    if not uvc:
        return
    parts = cmd_text(msg).split()
    arg = parts[1] if len(parts) > 1 else ""
    cid = await need_chat(msg, parts[2] if len(parts) > 2 else None)
    if not cid:
        return
    st = uvc.state(cid)
    if arg.lstrip("+-").isdigit():
        st.loud_db = max(0, min(18, int(arg.lstrip("+"))))
        if st.is_playing and st.current_file and os.path.exists(st.current_file):
            try:
                pos = await uvc._current_position(cid)
                await uvc._stream(cid, st.current_file, st.source_name,
                                  start_at=pos)
            except Exception:
                pass
    elif arg:
        await msg.reply_text("ℹ️ Use: <code>.loud 0-18</code>")
        return
    await msg.reply_text(
        f" <b>Extra loud:</b> <code>+{st.loud_db} dB</code> "
        f"(volume <code>{st.volume}</code>, boost <code>{st.boost}</code>)"
    )


@Client.on_message(HAS_USER & cmd_prefix(r"ss\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_ss(bot: Client, msg: Message):
    """Fake screen share: bot shares a PC-style mic/mixer panel in the VC."""
    uvc = await get_engine(msg)
    if not uvc:
        return
    parts = cmd_text(msg).split()
    arg = parts[1].lower() if len(parts) > 1 else ""
    cid = await need_chat(msg, parts[2] if len(parts) > 2 else None)
    if not cid:
        return
    st = uvc.state(cid)
    if arg not in ("on", "off", "", "live", "mixer"):
        await msg.reply_text("✅ Use: <code>.ss on</code> | <code>.ss off</code>\n"
                             "Photo ko reply karke <code>.ss on</code> = wahi photo share.")
        return
    if arg in ("live", "mixer"):
        # Live participants screen removed — both map to the mixer SS.
        st.ss_mode = "mixer"
        arg = "on"
        if st.ss_on:
            await uvc.set_screen_share(cid, False)
    if not arg:
        await msg.reply_text(
            f"️ <b>Screen share:</b> <code>{'ON' if st.ss_on else 'OFF'}</code> — "
            "1280x720 HD"
        )
        return
    image = "" if arg == "on" else None
    reply = msg.reply_to_message
    if arg == "on" and reply:
        media = reply.photo
        doc = reply.document
        if not media and doc and (doc.mime_type or "").startswith("image/"):
            media = doc
        if media:
            try:
                image = await bot.download_media(media.file_id)
            except Exception:
                image = ""
    stat = await msg.reply_text("️ Screen share set ho raha hai…")
    ok = await uvc.set_screen_share(cid, arg == "on", image_path=image)
    if not ok:
        reason = getattr(st, "ss_error", "") or "unknown"
        await stat.edit_text(
            " Screen share set nahi hua.\n"
            f"Reason: <code>{html.escape(reason)}</code>\n"
            "Check: group me VC chalu ho, ID muted na ho, aur group me video allowed ho."
        )
        return
    await stat.edit_text(
        ("🖥 <b>Screen share ON</b> — " + (
            "aapki photo share ho rahi hai." if image else
            "PC mixer screen share ho raha hai (HD, bina blur)."))
        if arg == "on" else "️ <b>Screen share OFF.</b>"
    )


async def _transport(msg: Message, action: str):
    uvc = await get_engine(msg)
    if not uvc:
        return
    parts = cmd_text(msg).split()
    cid = await need_chat(msg, parts[1] if len(parts) > 1 else None)
    if not cid:
        return
    if action == "pause":
        ok = await uvc.pause(cid)
        await msg.reply_text("⏸ Paused." if ok else " Kuch play nahi ho raha.")
    elif action == "resume":
        ok = await uvc.resume(cid)
        await msg.reply_text("▶ Resumed." if ok else " Pause nahi tha.")
    elif action == "skip":
        ok = await uvc.skip(cid)
        await msg.reply_text("⏭ Skipped." if ok else " Active VC nahi.")
    elif action == "stop":
        if cid not in uvc.chats:
            await msg.reply_text("❌ Is VC mein bot ka active session nahi hai.")
            return
        try:
            await uvc.leave(cid, reason="Manual stop")
        except Exception as exc:
            await log_error("transport_stop", exc)
            await msg.reply_text(f"❌ Stop fail hua: <code>{exc}</code>")
            return
        await msg.reply_text("⏹ <b>Stopped</b> — bot ne VC playback session chhod diya.")

@Client.on_message(HAS_USER & cmd_prefix(r"pause\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_pause(bot, msg):
    await _transport(msg, "pause")

@Client.on_message(HAS_USER & cmd_prefix(r"resume\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_resume(bot, msg):
    await _transport(msg, "resume")

@Client.on_message(HAS_USER & cmd_prefix(r"skip\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_skip(bot, msg):
    await _transport(msg, "skip")

@Client.on_message(HAS_USER & cmd_prefix(r"(stop|end|leave)\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_stop(bot, msg):
    await _transport(msg, "stop")

@Client.on_message(HAS_USER & cmd_prefix(r"queue\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_queue(bot: Client, msg: Message):
    uvc = await get_engine(msg)
    if not uvc:
        return
    parts = cmd_text(msg).split()
    cid = await need_chat(msg, parts[1] if len(parts) > 1 else None)
    if not cid:
        return
    st = uvc.chats.get(cid)
    if not st:
        await msg.reply_text("❌ Is chat mein koi active VC session nahi.")
        return
    items = uvc.queue_list(cid)
    lines = [f"{i+1}. {n}" for n, i in items] or ["— empty —"]
    await msg.reply_text(
        f" <b>Now:</b> {st.source_name}\n <b>Queue ({len(items)}):</b>\n" + "\n".join(lines))

@Client.on_message(HAS_USER & cmd_prefix(r"qclear\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_qclear(bot: Client, msg: Message):
    uvc = await get_engine(msg)
    if not uvc:
        return
    parts = cmd_text(msg).split()
    cid = await need_chat(msg, parts[1] if len(parts) > 1 else None)
    if not cid:
        return
    count = uvc.queue_clear(cid)
    await msg.reply_text(f"✅ <b>Queue cleared</b> — {count} track(s) hata diye." if count
                        else " Queue pehle hi khaali tha.")

@Client.on_message(HAS_USER & cmd_prefix(r"qremove\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_qremove(bot: Client, msg: Message):
    uvc = await get_engine(msg)
    if not uvc:
        return
    parts = cmd_text(msg).split()
    if len(parts) < 2:
        await msg.reply_text("Usage: <code>.qremove &lt;number&gt;</code> (queue position)")
        return
    try:
        idx = int(parts[1]) - 1
    except ValueError:
        await msg.reply_text("ℹ️ Number dein, jaise <code>.qremove 2</code>")
        return
    cid = await need_chat(msg, parts[2] if len(parts) > 2 else None)
    if not cid:
        return
    ok = uvc.queue_remove(cid, idx)
    await msg.reply_text(f"❌ Track {idx+1} hata diya." if ok
                        else " Track nahi mila — queue position check karein (.queue)")

@Client.on_message(HAS_USER & cmd_prefix(r"qshuffle\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_qshuffle(bot: Client, msg: Message):
    uvc = await get_engine(msg)
    if not uvc:
        return
    parts = cmd_text(msg).split()
    cid = await need_chat(msg, parts[1] if len(parts) > 1 else None)
    if not cid:
        return
    ok = uvc.queue_shuffle(cid)
    await msg.reply_text("ℹ️ Queue shuffle ho gayi!" if ok
                        else " Queue mein 2+ tracks nahi hai.")

@Client.on_message(HAS_USER & cmd_prefix(r"vcinfo\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_vcinfo(bot: Client, msg: Message):
    uvc = await get_engine(msg)
    if not uvc:
        return
    parts = cmd_text(msg).split()
    cid = await need_chat(msg, parts[1] if len(parts) > 1 else None)
    if not cid:
        return
    st = uvc.chats.get(cid)
    if not st:
        await msg.reply_text("❌ Koi active VC session nahi.")
        return
    state = "⏸ Paused" if st.is_paused else "▶ Playing"
    await msg.reply_text(
        f" <b>VC Info</b> — <code>{cid}</code>\n\n"
        f"├ <b>Account:</b> {uvc.account_name} (<code>{uvc.account_id}</code>)\n"
        f"├ <b>Status:</b> {state}\n"
        f"├ <b>Now:</b> {st.source_name}\n"
        f"├ <b>Volume:</b> {st.volume}x\n"
        f"├ <b>Bass:</b> +{st.bass} dB\n"
        f"├ <b>Boost:</b> {st.boost}/10\n"
        f"├ <b>Echo:</b> {'On' if st.echo else 'Off'} ({st.echo_level}/10)\n"
        f"└ <b>Queue:</b> {len(st.queue)}"
    )

async def _apply_and_reply(msg: Message, label: str, **changes):
    from plugins.start import apply_settings_live
    uid = msg.from_user.id
    await db.save_settings(uid, **changes)
    s = await db.get_settings(uid)
    applied = await apply_settings_live(uid)
    await msg.reply_text(
        f"{label}\n\n"
        f" Vol <code>{s['volume']}/3000</code> | Gain <code>{s['gain']}/500</code>\n"
        f" 📻 Play <code>{float(s.get('play_drive_db', 10.0)):+.1f} dB</code> | 🎙️ Master <code>{float(s.get('master_drive_db', 12.0)):+.1f} dB</code>\n"
        f" 🛡️ Ceil <code>{float(s.get('play_ceiling_db', -0.3)):.1f} dB</code> | Bass <code>{s['bass']}</code> | Boost <code>{s['boost']}/10</code>\n"
        + (f" {applied} live VC par apply hua." if applied else
           " Saved — agli play/mic par lagega."),
        reply_markup=K([[B("🔹 Settings Panel", callback_data="menu:settings")]]),
    )

def _num_arg(msg: Message):
    parts = cmd_text(msg).split()
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None

@Client.on_message(HAS_USER & cmd_prefix(r"vol\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_vol(bot, msg: Message):
    n = _num_arg(msg)
    if n is None:
        await msg.reply_text("Usage: <code>.vol &lt;0-3000&gt;</code>\n1000 = clean max, 1001-3000 = OVERDRIVE (sach me aur tez, presence boost)")
        return
    n = clamp(n, VOLUME_MIN, VOLUME_MAX)
    await _apply_and_reply(msg, f" Volume set: <b>{n}/3000</b>" + (" — OVERDRIVE" if n > 1000 else ""), volume=n, relay_volume=n)

@Client.on_message(HAS_USER & cmd_prefix(r"bass\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_bass(bot, msg: Message):
    n = _num_arg(msg)
    if n is None:
        await msg.reply_text("Usage: <code>.bass &lt;0-100&gt;</code>")
        return
    await _apply_and_reply(msg, f" Bass set: <b>+{clamp(n, BASS_MIN, BASS_MAX)} dB</b>",
                           bass=clamp(n, BASS_MIN, BASS_MAX))

@Client.on_message(HAS_USER & cmd_prefix(r"boost\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_boost(bot, msg: Message):
    n = _num_arg(msg)
    if n is None:
        await msg.reply_text(
            "Usage: <code>.boost &lt;0-10&gt;</code> (audio loudness)\n"
            "Live mic ke liye: <code>.myboost</code>")
        return
    await _apply_and_reply(msg, f" Boost set: <b>{clamp(n, LEVEL_MIN, LEVEL_MAX)}/10</b>",
                           boost=clamp(n, LEVEL_MIN, LEVEL_MAX))

@Client.on_message(HAS_USER & cmd_prefix(r"echolvl\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_echolvl(bot, msg: Message):
    n = _num_arg(msg)
    if n is None:
        await msg.reply_text("Usage: <code>.echolvl &lt;0-10&gt;</code>")
        return
    lvl = clamp(n, LEVEL_MIN, LEVEL_MAX)
    await _apply_and_reply(msg, f" Echo level: <b>{lvl}/10</b>",
                           echo_level=lvl, echo=1 if lvl else 0)

@Client.on_message(HAS_USER & cmd_prefix(r"echo\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_echo(bot, msg: Message):
    parts = cmd_text(msg).split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        await msg.reply_text("Usage: <code>.echo on|off</code>")
        return
    on = parts[1].lower() == "on"
    await _apply_and_reply(msg, f" Echo: <b>{'ON' if on else 'OFF'}</b>",
                           echo=1 if on else 0)

@Client.on_message(HAS_USER & cmd_prefix(r"(max|ultra)\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_max(bot, msg: Message):

    await _apply_and_reply(msg, " <b>MAXIMUM LOUD MODE</b> — sab knobs max par.",
                           auto=1, **AUTO_PRESET)

@Client.on_message(HAS_USER & cmd_prefix(r"reset\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_reset(bot, msg: Message):
    await _apply_and_reply(msg, " <b>Defaults restored</b>",
                           volume=Config.DEFAULT_VOLUME,
                           relay_volume=Config.RELAY_DEFAULT_VOLUME,
                           gain=Config.RELAY_DEFAULT_GAIN,
                           bass=Config.DEFAULT_BASS,
                           treble=Config.RELAY_DEFAULT_TREBLE,
                           voice="normal", boost=Config.DEFAULT_BOOST,
                           echo=1 if Config.DEFAULT_ECHO else 0,
                           echo_level=Config.DEFAULT_ECHO_LEVEL, auto=0)

@Client.on_message(HAS_USER & cmd_prefix(r"(myboost|livegain|livevolume)\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_myboost(bot: Client, msg: Message):
    uvc = await get_engine(msg)
    if not uvc:
        return
    parts = cmd_text(msg).split()
    vol = None
    cid_arg = None
    for p in parts[1:]:
        try:
            v = int(p)
        except ValueError:
            continue
        if is_chat_id_token(p):
            cid_arg = p
        else:
            vol = max(VOL_NORMAL, min(VOL_MAX, v))
    cid = await need_chat(msg, cid_arg)
    if not cid:
        return
    st = uvc.state(cid)
    if vol is None:
        vol = st.live_volume
    st.live_volume = vol
    await db.save_settings(msg.from_user.id, live_volume=vol)
    ok = await uvc.set_participant_volume(cid, uvc.account_id, vol)
    await msg.reply_text(
        f" <b>Live mic boost {'lag gaya' if ok else 'fail'}</b>\n"
        f" <code>{uvc.account_id}</code> → {vol} ({round(vol/100)}%)\n"
        "Ab is session ke VC join/reconnect par bhi ye live gain re-apply hoga.\n\n"
        + ("Ab VC mein bolte hi aapki aavaj max loud jayegi."
           if ok else "VC on hai? Aapka account VC mein hai? Check karein.")
    )

IDLE_TEXT = (
    "🔇 <b>Abhi koi audio play nahi ho raha</b>\n┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
    "<b>▶️ Audio kaise chalayein:</b>\n"
    "1️⃣ Group ka VC (voice chat) on karo\n"
    "2️⃣ Group me kisi audio/video ko reply karke <code>.play</code> likho\n"
    "   ya DM se: <code>.play -100xxxxxxxxxx</code> (audio reply karke)\n"
    "3️⃣ Bas! Gaana VC me bajega — phir yahan ▶️ Player dabao\n\n"
    "💡 Saved audio: <code>.tag naam</code> se save, <code>.play naam</code> se chalao"
)
IDLE_KB = K([[B("🎵 Audio Library", callback_data="aud:menu", style="primary"),
              B("🏠 Home", callback_data="menu:home", style="primary")]])


async def _show_idle(cq):
    await edit_screen(cq.message, IDLE_TEXT, reply_markup=IDLE_KB)
    await safe_answer(cq, "Abhi kuch play nahi ho raha")


@Client.on_callback_query(filters.regex(r"^vc:"))
async def cb_vc(bot, cq):
    _, action, cid = cq.data.split(":")
    cid = int(cid)
    uvc = await _cb_engine(cq.from_user.id)
    if not uvc:
        if action in ("list", "now"):
            await _show_idle(cq)
        else:
            await safe_answer(cq, "⚠️ Account connect nahi hua — 🔁 Re-Login karo.", show_alert=True)
        return
    if action == "list":
        active = next((chat_id for chat_id, state in uvc.chats.items()
                       if state.is_playing), None)
        if active is None:
            await _show_idle(cq)
            return
        cid = active
        action = "now"
    if action == "now":
        st = uvc.chats.get(cid)
        if not st or not st.is_playing:
            await _show_idle(cq)
            return
        await edit_screen(cq.message,
            now_playing_text(st, "playing", getattr(st, "chat_title", "") or "",
                             getattr(uvc, "account_name", "")),
            reply_markup=now_playing_kb(cid, st),
        )
        await safe_answer(cq, "Now Playing updated")
    elif action in ("tez", "lmode"):
        st = uvc.chats.get(cid)
        if action == "tez":
            await safe_answer(cq, "⚡ TEZ lag raha hai…")
            from plugins.loud_max import do_maxloud
            try:
                lines = await do_maxloud(cq.from_user.id)
            except Exception as e:
                lines = [f"⚠️ {e}"]
        else:
            from helpers import playback_v77 as _v77
            order = ["loudest", "phone", "bass"]
            cur = _v77.get_mode()
            nxt = order[(order.index(cur) + 1) % 3] if cur in order else "loudest"
            _v77.set_mode(nxt)
            await db.set_app_value("play_loud_mode", nxt)
            from plugins.start import apply_settings_live
            await apply_settings_live(cq.from_user.id)
            await safe_answer(cq, f"Mode: {nxt}")
            lines = []
        if st and st.is_playing:
            txt = now_playing_text(st, "playing", getattr(st, "chat_title", "") or "",
                                   getattr(uvc, "account_name", ""))
            if lines:
                txt += "\n\n<blockquote expandable>" + "\n".join(lines) + "</blockquote>"
            try:
                await edit_screen(cq.message, txt, reply_markup=now_playing_kb(cid, st))
            except Exception:
                pass
    elif action == "reset":
        await safe_answer(cq, "✅ Reset apply ho raha hai…")
        from plugins.start import DEFAULT_SETTINGS, apply_settings_live
        await db.save_settings(cq.from_user.id, **DEFAULT_SETTINGS)
        await apply_settings_live(cq.from_user.id)
    elif action == "auto":
        await safe_answer(cq, "✅ Auto apply ho raha hai…")
        from plugins.start import apply_settings_live
        s = await db.get_settings(cq.from_user.id)
        on = not bool(s.get("auto"))
        await db.save_settings(cq.from_user.id, auto=1 if on else 0,
                               **(AUTO_PRESET if on else {}))
        await apply_settings_live(cq.from_user.id)
    elif action == "human":
        # Human mode = hand raise 2-3 s after an admin mute + steady mic and
        # a 10-15 s pause after the unmute.  It never blinks the mic and
        # never raises the hand while nobody muted us.
        st = uvc.state(cid)
        on = not st.hand_raise
        st.hand_raise = on
        st.mic_blink = False
        uvc.save_mute_prefs(cid)
        await safe_answer(cq, "🧑 Human Mode " + ("ON" if on else "OFF"))
    elif action == "ss":
        if getattr(uvc, "ss_busy", None) and uvc.ss_busy(cid):
            await safe_answer(cq, "⏳ Screen share switch ho raha hai… 2 sec ruko.")
            return
        st = uvc.state(cid)
        pres = getattr(getattr(uvc, "calls", None), "_presentations", None)
        actual = (cid in pres) if pres is not None else bool(getattr(st, "ss_on", False))
        want = not actual
        try:
            ok = await uvc.set_screen_share(cid, want, image_path="" if want else None)
        except Exception as exc:
            st.ss_error = str(exc)
            ok = False
        err = getattr(st, "ss_error", "") or ""
        if "default_launcher" in err or "No such file" in err:
            err = "Screen program start nahi hua. Bot update karke restart karein."
        elif "[system" in err or "/__w/" in err:
            err = err.split("[system")[0].strip() or "Telegram ne screen share mana kiya."
        await safe_answer(cq, ("🖥 Screen share " + ("ON" if want else "OFF")) if ok
                          else ("Screen share nahi chal paya: " + (err or "VC me hai?"))[:190],
                          show_alert=not ok)
    elif action == "pause":
        await safe_answer(cq, "⏸ Paused" if await uvc.pause(cid) else "Kuch chal nahi raha")
    elif action == "resume":
        await safe_answer(cq, "▶ Resumed" if await uvc.resume(cid) else "Paused nahi tha")
    elif action == "skip":
        await safe_answer(cq, "⏭ Skipped" if await uvc.skip(cid) else "Kuch chal nahi raha")
    elif action == "stop":
        await uvc.leave(cid, reason="Stopped from button")
        await safe_answer(cq, "⏹ Stopped")
    elif action in ("hr", "hrnow", "mb", "mbnow", "pm", "pmclr", "pmtest"):
        st = uvc.state(cid)
        if action == "hr":
            st.hand_raise = not st.hand_raise
            uvc.save_mute_prefs(cid)
            await safe_answer(cq, "✋ Hand raise " + ("ON" if st.hand_raise else "OFF"))
        elif action == "hrnow":
            ok = await uvc.raise_hand(cid, True)
            await safe_answer(cq, "✋ Hand raised" if ok else "Hand raise fail (VC me nahi?)", show_alert=not ok)
        elif action == "mb":
            st.mic_blink = not st.mic_blink
            uvc.save_mute_prefs(cid)
            await safe_answer(cq, f"✅ Mic blink {'ON' if st.mic_blink else 'OFF'} ({st.mic_blink_secs}s)")
        elif action == "mbnow":
            await safe_answer(cq, f"ℹ️ Blinking {st.mic_blink_secs}s…")
            asyncio.create_task(uvc._mic_blink(cid, st.mic_blink_secs))
        elif action == "pm":
            await safe_answer(cq, "PlayMute set hai ✅" if st.unmute_audio else
                              "Audio ko reply karke .playmute likhein", show_alert=True)
        elif action == "pmclr":
            uvc.clear_playmute(cid)
            await safe_answer(cq, "✅ PlayMute cleared")
        elif action == "pmtest":
            ok = await uvc.play_unmute_audio(cid)
            await safe_answer(cq, "▶️ PlayMute chal raha" if ok else "PlayMute audio set nahi hai", show_alert=not ok)
        try:
            await cq.message.edit_reply_markup(now_playing_kb(cid, uvc.chats.get(cid)))
        except Exception:
            pass
    if action in ("human", "ss", "pause", "resume", "loop"):
        async def _refresh():
            await asyncio.sleep(0.3)
            try:
                await cq.message.edit_reply_markup(now_playing_kb(cid, uvc.chats.get(cid)))
            except Exception:
                pass
        asyncio.create_task(_refresh())
    if action == "loop":
        cur = uvc.chats.get(cid)
        on = not (cur.loop if cur else getattr(uvc, "loop_pref", {}).get(cid, False))
        st = uvc.set_loop(cid, on)
        if True:
            await safe_answer(cq, "✅ Loop " + ("ON" if st.loop else "OFF"), show_alert=True)

AUTO_KB = K([[B("🔹 Settings Panel", callback_data="menu:settings")]])

def _mic_mode(name):
    """Live mic mode from a button/command (old clean/warm names still work)."""
    from helpers import live_mic_v79
    return live_mic_v79.normalize_mode(name)


def _mic_ss_target(uid, uvc, active_cid):
    """Return the relay and target VC used by this user's browser Live Mic."""
    try:
        from helpers import live_mic
        session = live_mic.get_session(uid)
        if session is not None and not getattr(session, "_closed", False):
            return session.relay, session.chat_id
    except Exception:
        pass
    if uvc is not None and active_cid is not None:
        return uvc, active_cid
    return None, None


def _mic_ss_on(uid, uvc, active_cid) -> bool:
    relay, cid = _mic_ss_target(uid, uvc, active_cid)
    if relay is None or cid is None:
        return False
    try:
        return bool(getattr(relay.state(cid), "ss_on", False))
    except Exception:
        return False


@Client.on_callback_query(filters.regex(r"^mic:"))
async def cb_mic(bot: Client, cq):
    from plugins.start import apply_settings_live

    uid = cq.from_user.id
    _, action, *rest = cq.data.split(":")

    # FIGHT-ID-ONLY MODE: Main ID ke bina bhi Live Mic chalta hai — user ki
    # apni ⚔️ Fight ID hi listener + speaker dono banti hai.  Fight ID wale
    # (bina main ID) user ke liye owner ki Sudo ID beech me nahi aati.
    from helpers.setup_flow import has_fight_id, has_main_id
    uvc = None
    if await has_main_id(uid) or not await has_fight_id(uid):
        uvc = await _cb_engine(uid)
    if not uvc:
        uvc = await session_manager.get_relay(uid)
    if not uvc and action not in {"acct", "panel", "noop", "wtarget", "wcancel", "whelp", "wsetup", "won"}:
        await edit_screen(cq.message, NEED_ACCOUNT_TEXT, reply_markup=need_account_kb())
        await safe_answer(cq, "Pehle Login ya Fight ID")
        return

    if action == "noop":
        await safe_answer(cq)
        return

    if action in {"panel", "won", "wleave", "whelp", "wmode", "wtarget", "wcancel", "wsetup"}:
        # Browser Live Mic panel.
        from plugins.web_mic import web_mic_panel, run_web_mic, HELP
        class _M:  # callback ko message jaisa banao
            pass
        m = _M(); m.from_user = cq.from_user; m.chat = cq.message.chat; m._client = bot; m.id = cq.message.id; m.text = ""; m.command = []
        m.reply_text = cq.message.reply_text; m.reply = cq.message.reply_text
        if action == "whelp":
            await edit_screen(cq.message, HELP, reply_markup=K([[B("⬅ Back", callback_data="mic:panel")]]))
            await safe_answer(cq)
            return
        if action == "wsetup":
            text, kb = await web_mic_panel(uid)
            await edit_screen(cq.message, text, reply_markup=kb)
            await safe_answer(cq)
            return
        if action == "wtarget":
            from plugins.live_mic_target import request_target_input
            request_target_input(uid, surface="web")
            await edit_screen(cq.message,
                "🎯 <b>Set Live Mic target</b>\n\n"
                "Send a group ID, <code>@username</code> or invite link here.\n"
                "I'll verify and save it before updating this panel.\n"
                "<i>Prompt expires in 5 minutes. You can also use <code>.mic cancel</code>.</i>",
                reply_markup=K([[B("✖ Cancel", callback_data="mic:wcancel", style="danger")],
                                [B("⬅ Back", callback_data="mic:panel")]]))
            await safe_answer(cq, "Target ID bhejo")
            return
        if action == "wcancel":
            from plugins.live_mic_target import _await_chat
            _await_chat.pop(uid, None)
            await safe_answer(cq, "Target prompt cancelled")
        elif action == "won":
            await run_web_mic(m, "on")
            await safe_answer(cq, "Link bhej diya 👇")
        elif action == "wleave":
            await run_web_mic(m, "leave")
            await safe_answer(cq, "Mic OFF + VC se bahar")
        elif action == "wmode" and rest and _mic_mode(rest[0]):
            from helpers import live_mic
            want = _mic_mode(rest[0])
            session = live_mic.get_session(uid)
            if session is None or session.ws is None or session.ws.closed:
                await safe_answer(cq, "Pehle browser mic connect karein", show_alert=True)
            else:
                await session._handle_web_control({"action": "mode", "mode": want})
                if session.audio_mode == want:
                    await safe_answer(cq, f"Mic mode: {want.upper()}")
                else:
                    await safe_answer(cq, "Mode switch nahi hua; previous mode active hai", show_alert=True)
        else:
            await safe_answer(cq)
        text, kb = await web_mic_panel(uid)
        await edit_screen(cq.message, text, reply_markup=kb)
        return

    if action == "chat":
        sub = rest[0] if rest else ""
        if sub == "off":
            _spare_chat.pop(uid, None)
            await safe_answer(cq, "Fight chat mode OFF")
        elif sub == "on":
            cid = await _spare_chat_target(uid)
            if not cid:
                await safe_answer(cq, "Pehle group me .mic on karein ya .setgc lagayein",
                                  show_alert=True)
            else:
                _spare_chat[uid] = cid
                await safe_answer(cq, "Fight chat mode ON")
        else:
            await safe_answer(cq)
        text, kb = await spare_chat_screen(uid)
        await edit_screen(cq.message, text, reply_markup=kb)
        return

    if action == "acct":
        sub = rest[0] if rest else ""
        if sub == "":
            # Fight ID nahi hai to seedha number step (extra button tap nahi).
            await start_fight_setup(uid)
            await safe_answer(cq)
        elif sub == "phone":
            _clear_other_prompts(uid)
            await _spare_cancel(uid)
            _mic_login[uid] = {"stage": "phone"}
            await safe_answer(cq)
        elif sub == "string":
            await _spare_cancel(uid)
            _clear_other_prompts(uid)
            _mic_login[uid] = {"stage": "string"}
            await safe_answer(cq)
        elif sub == "cancel":
            await _spare_cancel(uid)
            await safe_answer(cq, "Cancelled")
        elif sub == "remove":
            await _spare_cancel(uid)
            await _spare_remove(uid)
            await safe_answer(cq, "Fight ID hata di")
        elif sub == "add":
            from helpers.access_control import can_multi_spare
            if not await session_manager.assistant_string(uid):
                await safe_answer(cq, "Pehle ek Fight ID jodo", show_alert=True)
            elif not await can_multi_spare(uid):
                await safe_answer(cq, "🔒 Multi Fight ID sirf Premium users ke liye hai. "
                                  "Premium lo ya owner se baat karo.", show_alert=True)
            else:
                used = set(await session_manager.spare_slots(uid))
                free = next((n for n in range(2, session_manager.MAX_SPARES + 1)
                             if n not in used), None)
                if free is None:
                    await safe_answer(cq, f"Max {session_manager.MAX_SPARES} Fight ID ho gayi",
                                      show_alert=True)
                else:
                    await _spare_cancel(uid)
                    _clear_other_prompts(uid)
                    _mic_login[uid] = {"stage": "phone", "slot": free}
                    await safe_answer(cq, f"Fight ID #{free}: number ya session string bhejo")
        elif sub == "rm" and len(rest) > 1 and rest[1].isdigit() and int(rest[1]) > 1:
            slot = int(rest[1])
            await session_manager.drop_relay(uid, slot)
            await db.delete_app_value(session_manager.spare_key(uid, slot))
            await safe_answer(cq, f"Fight ID #{slot} hata di")
        else:
            await safe_answer(cq)
        text, kb = await spare_screen(uid)
        await edit_screen(cq.message, text, reply_markup=kb)
        return

    s = await db.get_settings(uid)
    active_cid = next((cid for cid, st in uvc.chats.items()
                       if st.mic_enabled), None)

    if action == "ss":
        # Live Mic me bhi Live Screen (audio panel jaisa) — screen share us ID
        # par jo VC me bol rahi hai (active relay, warna main ID).
        relay, cid = _mic_ss_target(uid, uvc, active_cid)
        if relay is None or cid is None:
            await safe_answer(cq, "Pehle 🎤 Mic ON karo — phir Live Screen chalega.",
                              show_alert=True)
            return
        if getattr(relay, "ss_busy", None) and relay.ss_busy(cid):
            await safe_answer(cq, "⏳ Screen share switch ho raha hai… 2 sec ruko.")
            return
        st = relay.state(cid)
        pres = getattr(getattr(relay, "calls", None), "_presentations", None)
        actual = (cid in pres) if pres is not None else bool(getattr(st, "ss_on", False))
        want = not actual
        try:
            ok = await relay.set_screen_share(cid, want, image_path="" if want else None)
        except Exception as exc:
            st.ss_error = str(exc)[:200]
            ok = False
        err = getattr(st, "ss_error", "") or ""
        await safe_answer(cq, ("🖥 Live Screen " + ("ON" if want else "OFF")) if ok
                          else ("Live Screen nahi chal paya: " + (err or "VC me hai?"))[:190],
                          show_alert=not ok)
        try:
            await edit_screen(cq.message, mic_text(s, active_cid is not None or relay is not uvc, ""),
                              reply_markup=mic_kb(active_cid is not None or relay is not uvc,
                                                  logged_in=True,
                                                  ss=_mic_ss_on(uid, uvc, active_cid)))
        except Exception:
            pass
        return

    if action == "on":
        from helpers.setup_flow import account_state
        acc = await account_state(uid)
        if not acc["ready"] and not await session_manager.assistant_string(uid):
            await edit_screen(cq.message, NEED_ACCOUNT_TEXT, reply_markup=need_account_kb())
            await safe_answer(cq, "Pehle Login ya Fight ID")
            return
        # Browser Live Mic link.
        class _MicCallbackMessage:
            def __init__(self, query):
                self.from_user = query.from_user
                self.chat = query.message.chat
                self._client = query._client if hasattr(query, "_client") else query.message._client
                self.id = query.message.id
                self.text = ""
                self.command = []
            async def reply_text(self, text, **kwargs):
                return await mic_notify(self, text, **kwargs)
            async def reply(self, text, **kwargs):
                return await self.reply_text(text, **kwargs)
        from plugins.web_mic import run_web_mic
        await safe_answer(cq, "Mic link aa raha hai…")
        await run_web_mic(_MicCallbackMessage(cq), "on")
        return

    if action == "off":
        from helpers.live_mic import reset_mic_state, stop_session
        ok = await stop_session(uid)
        await db.set_app_value(f"live_mic_token_{uid}", "")
        try:
            await reset_mic_state(uvc, uid)
        except Exception:
            pass
        from plugins.web_mic import web_mic_panel
        text, kb = await web_mic_panel(uid)
        await edit_screen(cq.message, text, reply_markup=kb)
        await safe_answer(cq, "⏹ Live Mic OFF" if ok else "Live Mic chal nahi raha tha")
        return

    if action == "vol":
        delta = int(rest[0]) if rest else 0
        if delta == 20000:
            s["live_volume"] = 20000
        else:
            s["live_volume"] = max(200, min(20000,
                int(s.get("live_volume", Config.LIVE_BOOST_DEFAULT)) + delta))
        await db.save_settings(uid, live_volume=s["live_volume"])
        if active_cid:
            st = uvc.chats.get(active_cid)
            target_uid = st.mic_boost_user_id if st and st.mic_boost_user_id else uid
            await uvc.set_participant_volume(active_cid, target_uid,
                                             s["live_volume"], quiet=True)
        await edit_screen(cq.message, mic_text(s, active_cid is not None,
            (f"User {uvc.chats[active_cid].mic_boost_user_id}"
             if active_cid and uvc.chats[active_cid].mic_boost_user_id else "")),
            reply_markup=mic_kb(active_cid is not None, logged_in=True,
                              ss=_mic_ss_on(uid, uvc, active_cid)))
        await safe_answer(cq, f"Mic gain: {s['live_volume']}/20000")
        return

    if action == "gain":
        delta = int(rest[0]) if rest else 0
        s["gain"] = max(0, min(GAIN_MAX,
            int(s.get("gain", Config.RELAY_DEFAULT_GAIN)) + delta))
        await db.save_settings(uid, gain=s["gain"])
        await apply_settings_live(uid)
        await edit_screen(cq.message, mic_text(s, active_cid is not None,
            (f"User {uvc.chats[active_cid].mic_boost_user_id}"
             if active_cid and uvc.chats[active_cid].mic_boost_user_id else "")),
            reply_markup=mic_kb(active_cid is not None, logged_in=True,
                              ss=_mic_ss_on(uid, uvc, active_cid)))
        await safe_answer(cq, f"Gain: {s['gain']}/500")
        return

    if action == "bass":
        delta = int(rest[0]) if rest else 0
        s["bass"] = clamp(s["bass"] + delta, BASS_MIN, BASS_MAX)
        await db.save_settings(uid, bass=s["bass"])
        await apply_settings_live(uid)
        await edit_screen(cq.message, mic_text(s, active_cid is not None,
            (f"User {uvc.chats[active_cid].mic_boost_user_id}"
             if active_cid and uvc.chats[active_cid].mic_boost_user_id else "")),
            reply_markup=mic_kb(active_cid is not None, logged_in=True,
                              ss=_mic_ss_on(uid, uvc, active_cid)))
        await safe_answer(cq, f"Bass: +{s['bass']} dB")
        return

    if action == "boost":
        delta = int(rest[0]) if rest else 0
        s["boost"] = clamp(s["boost"] + delta, LEVEL_MIN, LEVEL_MAX)
        await db.save_settings(uid, boost=s["boost"])
        await apply_settings_live(uid)
        await edit_screen(cq.message, mic_text(s, active_cid is not None,
            (f"User {uvc.chats[active_cid].mic_boost_user_id}"
             if active_cid and uvc.chats[active_cid].mic_boost_user_id else "")),
            reply_markup=mic_kb(active_cid is not None, logged_in=True,
                              ss=_mic_ss_on(uid, uvc, active_cid)))
        await safe_answer(cq, f"Boost: {s['boost']}/10")
        return

    if action == "echolvl":
        delta = int(rest[0]) if rest else 0
        s["echo_level"] = clamp(s["echo_level"] + delta, LEVEL_MIN, LEVEL_MAX)
        s["echo"] = 1 if s["echo_level"] > 0 else s["echo"]
        await db.save_settings(uid, echo_level=s["echo_level"], echo=s["echo"])
        await apply_settings_live(uid)
        await edit_screen(cq.message, mic_text(s, active_cid is not None,
            (f"User {uvc.chats[active_cid].mic_boost_user_id}"
             if active_cid and uvc.chats[active_cid].mic_boost_user_id else "")),
            reply_markup=mic_kb(active_cid is not None, logged_in=True,
                              ss=_mic_ss_on(uid, uvc, active_cid)))
        await safe_answer(cq, f"Echo level: {s['echo_level']}/10")
        return

    if action == "echo":
        s["echo"] = 0 if s["echo"] else 1
        await db.save_settings(uid, echo=s["echo"])
        await apply_settings_live(uid)
        await edit_screen(cq.message, mic_text(s, active_cid is not None,
            (f"User {uvc.chats[active_cid].mic_boost_user_id}"
             if active_cid and uvc.chats[active_cid].mic_boost_user_id else "")),
            reply_markup=mic_kb(active_cid is not None, logged_in=True,
                              ss=_mic_ss_on(uid, uvc, active_cid)))
        await safe_answer(cq, f"Echo: {'ON' if s['echo'] else 'OFF'}")
        return

    if action == "max":
        s.update(AUTO_PRESET, auto=1, live_volume=20000)
        await db.save_settings(uid, **s)
        await apply_settings_live(uid)
        if active_cid:
            st = uvc.chats.get(active_cid)
            target_uid = st.mic_boost_user_id if st and st.mic_boost_user_id else uid
            await uvc.set_participant_volume(active_cid, target_uid,
                                             20000, quiet=True)
        await edit_screen(cq.message, mic_text(s, active_cid is not None,
            (f"User {uvc.chats[active_cid].mic_boost_user_id}"
             if active_cid and uvc.chats[active_cid].mic_boost_user_id else "")),
            reply_markup=mic_kb(active_cid is not None, logged_in=True,
                              ss=_mic_ss_on(uid, uvc, active_cid)))
        await safe_answer(cq, "⚡ MAX ALL — sab max par!")
        return

    if action == "apply":
        await apply_settings_live(uid)
        if active_cid:
            st = uvc.chats.get(active_cid)
            if st and st.mic_enabled and st.mic_boost_user_id:
                target_vol = s.get("live_volume", Config.LIVE_BOOST_DEFAULT)
                await uvc.set_participant_volume(active_cid, st.mic_boost_user_id,
                                                 target_vol, quiet=True)
        await edit_screen(cq.message, mic_text(s, active_cid is not None,
            (f"User {uvc.chats[active_cid].mic_boost_user_id}"
             if active_cid and uvc.chats[active_cid].mic_boost_user_id else "")),
            reply_markup=mic_kb(active_cid is not None, logged_in=True,
                              ss=_mic_ss_on(uid, uvc, active_cid)))
        await safe_answer(cq, "✅ Mic par apply ho gaya!")
        return

    await safe_answer(cq)

@Client.on_message(HAS_USER & cmd_prefix(r"auto\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_auto(bot: Client, msg: Message):
    from plugins.start import apply_settings_live
    await log_command(msg.from_user.id, msg.from_user.username, msg.chat.id, ".auto")
    parts = cmd_text(msg).split()
    on = not any(p.lower() in ("off", "0", "no", "band") for p in parts[1:])

    uvc = await get_engine(msg)
    if not uvc:
        return

    await db.save_settings(msg.from_user.id, auto=1 if on else 0,
                           **(AUTO_PRESET if on else {}))
    applied = await apply_settings_live(msg.from_user.id)

    if not on:
        await msg.reply_text("ℹ️ <b>AUTO MODE OFF</b> — manual control wapas.",
                             reply_markup=AUTO_KB)
        return

    await msg.reply_text(
        " <b>AUTO MODE ON</b> — ab sab automatic hai \n\n"
        f" Volume <code>{VOLUME_MAX}/3000</code> (max)\n"
        f" Bass <code>+{AUTO_PRESET['bass']}</code> (voice-safe max)\n"
        f" Treble <code>{AUTO_PRESET['treble']}/100</code> + Gain <code>200/200</code>\n"
        f" Boost <code>{LEVEL_MAX}/10</code> (max)\n"
        f" Echo <code>OFF</code> (clear voice)\n"
        f" Live mic <code>{VOL_MAX}</code> (200% — Telegram max)\n"
        f" Volume keeper: har {Config.KEEPER_INTERVAL}s par volume wapas "
        f"max par pin ho jayega (reset/reconnect ke baad bhi)\n\n"
        + (f" {applied} live VC par turant apply ho gaya." if applied else
           " Save ho gaya — <code>.play</code> karte hi khud lag jayega.")
        + "\n\n<i>Note: 200% Telegram ka server-side hard cap hai; usse aage "
          "loudness FFmpeg chain (dynaudnorm + compressor + volume + limiter) "
          "se aati hai, jo AUTO me poori max par hai.</i>",
        reply_markup=AUTO_KB,
    )

@Client.on_message(HAS_USER & cmd_prefix(r"logtest\b", flags=re.IGNORECASE) & filters.private)
async def cmd_logtest(bot: Client, msg: Message):
    if not Config.is_owner(msg.from_user.id):
        return
    problem = await verify_log_channel()
    if problem:
        await msg.reply_text(f"❌ <b>Log channel kaam nahi kar raha</b>\n\n{problem}")
    else:
        await msg.reply_text(
            f" <b>Log channel OK</b> — test message bhej diya.\n"
            f"Channel: <code>{get_channel()}</code>"
        )

@Client.on_message(HAS_USER & cmd_prefix(r"setlog\b", flags=re.IGNORECASE) & filters.private)
async def cmd_setlog(bot: Client, msg: Message):
    if not Config.is_owner(msg.from_user.id):
        return
    parts = cmd_text(msg).split()
    if len(parts) < 2:
        await msg.reply_text(
            f"Usage: <code>.setlog -100xxxxxxxxxx</code>\n"
            f"Abhi: <code>{get_channel()}</code>"
        )
        return
    try:
        from helpers.peer_guard import chat_id_variants
        set_channel(chat_id_variants(parts[1])[0])
    except (ValueError, IndexError):
        await msg.reply_text("ℹ️ Channel ID number honi chahiye (-100 ke saath ya bina).")
        return
    problem = await verify_log_channel()
    await msg.reply_text(
        f"{' ' + problem if problem else ' Log channel set + verified'}\n"
        f"Channel: <code>{get_channel()}</code>"
    )

def _relay_num_arg(msg: Message):
    parts = cmd_text(msg).split()
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None

@Client.on_message(HAS_USER & cmd_prefix(r"volume\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_volume(bot: Client, msg: Message):
    n = _relay_num_arg(msg)
    if n is None:
        await msg.reply_text("Usage: <code>.volume &lt;0-3000&gt;</code>")
        return
    n = max(0, min(VOLUME_MAX, n))
    await _apply_and_reply(msg, f" Playback volume: <b>{n}/3000</b>", relay_volume=n, volume=n)

@Client.on_message(HAS_USER & cmd_prefix(r"gain\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_gain(bot: Client, msg: Message):
    n = _relay_num_arg(msg)
    if n is None:
        await msg.reply_text("Usage: <code>.gain &lt;0-500&gt;</code>")
        return
    n = max(0, min(GAIN_MAX, n))
    await _apply_and_reply(msg, f" Gain: <b>{n}/500</b>", gain=n)

@Client.on_message(HAS_USER & cmd_prefix(r"treble\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_treble(bot: Client, msg: Message):
    n = _relay_num_arg(msg)
    if n is None:
        await msg.reply_text("Usage: <code>.treble &lt;0-100&gt;</code>")
        return
    n = max(0, min(100, n))
    await _apply_and_reply(msg, f" Treble: <b>{n}/100</b>", treble=n)

@Client.on_message(HAS_USER & cmd_prefix(r"voice\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_voice(bot: Client, msg: Message):
    parts = cmd_text(msg).split()
    if len(parts) < 2 or parts[1].lower() not in {"female", "male", "normal"}:
        await msg.reply_text(
            "Usage: <code>.voice female|male|normal</code>\n\n"
            "female: sharp/bright | male: heavy/bassy | normal: balanced"
        )
        return
    profile = parts[1].lower()
    values = {
        "female": {"bass": 5, "treble": 70},
        "male": {"bass": 60, "treble": 15},
        "normal": {"bass": Config.RELAY_DEFAULT_BASS, "treble": Config.RELAY_DEFAULT_TREBLE},
    }[profile]
    await _apply_and_reply(
        msg,
        f" Voice profile: <b>{profile}</b>",
        voice=profile, bass=values["bass"], treble=values["treble"],
    )

@Client.on_message(HAS_USER & cmd_prefix(r"relaystatus\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_relaystatus(bot: Client, msg: Message):
    s = await db.get_settings(msg.from_user.id)
    await msg.reply_text(
        " <b>VC Audio Relay Settings</b>\n\n"
        f"├ Volume: <code>{s.get('relay_volume', Config.RELAY_DEFAULT_VOLUME)}/3000</code>\n"
        f"├ Gain: <code>{s.get('gain', Config.RELAY_DEFAULT_GAIN)}/500</code>\n"
        f"├ Bass: <code>{s.get('bass', Config.RELAY_DEFAULT_BASS)}/100</code>\n"
        f"├ Treble: <code>{s.get('treble', Config.RELAY_DEFAULT_TREBLE)}/100</code>\n"
        f"├ Live mic: <code>{s.get('live_volume', Config.LIVE_BOOST_DEFAULT)}/20000</code>\n"
        f"└ Voice: <code>{s.get('voice', 'normal')}</code>"
    )

# ───────────────────────────── Spare mic account ─────────────────────────────
# Live mic ke liye 2 account chahiye: ek aapka (jo VC me baitha rehta hai) aur
# ek spare, jisse aawaz VC me jati hai.  Pehle sirf "session string" maanga
# jata tha aur user ko pata hi nahi hota wo string kahan se laaye.  Ab pura
# kaam button se hota hai:  Live Mic → Spare Account → Phone se login → OTP.
#
# Pending logins: uid -> {client, phone, hash, stage}
_mic_login: dict = {}

SPARE_WHY = (
    "⚔️ <b>FIGHT ID</b>  <i>(optional)</i>\n"
    "Fight ID = ek <b>doosri</b> Telegram ID jo Live Mic me aapki awaaz bolegi —\n"
    "aapka apna phone VC me bana rahega.\n"
    "• Isko apne group me add rakho\n"
    "• Galti se apni ID daal di? Koi baat nahi — wo seedha 🔐 Login ban jayegi\n\n"
)


def spare_setup_kb(pending: bool = False) -> K:
    if pending:
        return K([
            [B("❌ Cancel", callback_data="mic:acct:cancel", style="danger")],
            [B("🏠 Home", callback_data="menu:home", style="primary")],
        ])
    return K([
        [B("📱 Fight ID jodo (number + OTP)", callback_data="mic:acct:phone", style="success")],
        [B("🔑 Session string se", callback_data="mic:acct:string", style="primary")],
        [B("🏠 Home", callback_data="menu:home", style="primary")],
    ])


def spare_ready_kb(extra_slots=()) -> K:
    rows = [[B("🎙 Live Mic shuru karo  ·  Step 2", callback_data="mic:panel", style="success")],
            [B("➕ Aur Fight ID (Multi)", callback_data="mic:acct:add", style="primary")]]
    for slot in extra_slots:
        rows.append([B(f"🗑 Fight ID #{slot} hatao", callback_data=f"mic:acct:rm:{slot}",
                       style="danger")])
    rows += [
        [B("♻ Fight ID badlo", callback_data="mic:acct:phone", style="primary"),
         B("🗑 Fight ID hatao", callback_data="mic:acct:remove", style="danger")],
        [B("🔐 Login", callback_data="menu:login", style="primary"),
         B("🏠 Home", callback_data="menu:home", style="primary")],
    ]
    return K(rows)


async def _multi_spare_lines(uid: int):
    """(text, extra_slots) — extra Fight IDs ki list for the Fight ID screen."""
    from helpers.access_control import can_multi_spare, multi_spare_premium_only
    slots = [x for x in await session_manager.spare_slots(uid) if x > 1]
    lines = []
    for slot in slots:
        r = await session_manager.get_relay(uid, slot)
        lines.append(f"├ #{slot}: <code>{r.account_name}</code> (<code>{r.account_id}</code>)"
                     if r else f"├ #{slot}: ⚠️ login fail — hata ke dobara lagao")
    allowed = await can_multi_spare(uid)
    lock = ("🔓 Free" if not await multi_spare_premium_only()
            else ("💎 Premium ✅" if allowed else "🔒 Sirf Premium"))
    text = (f"\n\n👥 <b>Multi Fight ID</b> ({lock}) — {1 + len(slots)}/"
            f"{session_manager.MAX_SPARES} ID ek saath bolengi\n"
            + ("\n".join(lines) if lines else "├ koi extra Fight ID nahi") +
            "\n└ Sab Fight ID target group me add honi chahiye.")
    return text, slots


async def spare_screen(uid: int):
    """(text, keyboard) for the Fight ID screen."""
    st = _mic_login.get(uid)
    if st:
        stage = st.get("stage")
        slot = int(st.get("slot", 1) or 1)
        which = "Fight ID" if slot <= 1 else f"Extra Fight ID #{slot}"
        if stage == "phone":
            return (SPARE_WHY +
                    f"📱 <b>{which} ka number bhejo</b> — country code ke saath\n"
                    "Jaise: <code>+919876543210</code>\n\n"
                    "<i>Session string hai? Wo bhi yahin paste kar sakte ho. "
                    "Aapka message turant delete ho jayega.</i>",
                    spare_setup_kb(True))
        if stage == "code":
            return (SPARE_WHY +
                    f"📩 <b>OTP bheja gaya</b> — {which} ke Telegram par.\n"
                    "OTP yahin bhejo, jaise <code>12345</code>.\n\n"
                    "<i>Message auto-delete ho jayega.</i>",
                    spare_setup_kb(True))
        if stage == "string":
            return (SPARE_WHY +
                    f"🔑 {which} ka pura <b>Pyrogram session string</b> paste karo.\n"
                    "String nahi hai? Cancel karke <b>📱 number se</b> jodo — wo aasan hai.",
                    spare_setup_kb(True))
        return (SPARE_WHY +
                "🔐 Is ID par <b>2-Step Verification</b> ON hai — uska password "
                "yahin bhejo.\n\n<i>Message auto-delete ho jayega.</i>",
                spare_setup_kb(True))

    current = await session_manager.assistant_string(uid)
    if not current:
        return (SPARE_WHY +
                "Niche <b>📱 Fight ID jodo</b> dabao — sirf number aur OTP lagega.",
                spare_setup_kb())
    relay = await session_manager.get_relay(uid)
    if not relay:
        return (SPARE_WHY +
                "⚠️ Fight ID set hai par connect nahi ho rahi. "
                "♻ Dobara jodo ya 🗑 hatao.",
                spare_ready_kb())
    multi_text, extra_slots = await _multi_spare_lines(uid)
    return (
        "⚔️ <b>FIGHT ID</b> — ✅ ready\n\n"
        f"├ Name: <code>{relay.account_name}</code>\n"
        f"└ ID: <code>{relay.account_id}</code>\n\n"
        "<b>Agla step 👇</b> 🎙 Live Mic shuru karo → group batao → link kholo.\n"
        "<i>Fight ID ko us group me add rakho.</i>" + multi_text,
        spare_ready_kb(extra_slots))


def _clear_other_prompts(uid: int) -> None:
    """Fight ID ka number/OTP kisi aur prompt (target group, main login,
    trial group link) me na chala jaye."""
    try:
        from plugins.live_mic_target import _await_chat
        _await_chat.pop(uid, None)
    except Exception:
        pass
    try:
        from plugins.login import CONVERSATION
        CONVERSATION.pop(uid, None)
    except Exception:
        pass
    try:
        from plugins.sudo import clear_await
        clear_await(uid)
    except Exception:
        pass


async def start_fight_setup(uid: int) -> None:
    """Fight ID nahi hai to flow seedha 'number bhejo' step se shuru (1 tap kam)."""
    from helpers.setup_flow import has_fight_id
    if uid in _mic_login or await session_manager.assistant_string(uid):
        return
    if await has_fight_id(uid):
        return
    _clear_other_prompts(uid)
    _mic_login[uid] = {"stage": "phone"}


async def _after_save_kb(uid: int) -> K:
    """Save ke baad: Fight ID judi -> Fight ID screen, warna (Login ban gaya) Home."""
    from helpers.setup_flow import has_fight_id
    if await has_fight_id(uid):
        return spare_ready_kb()
    from plugins.ui import home_kb
    return home_kb(Config.is_owner(uid), True)


def cancel_pending_fight_prompt(uid: int) -> None:
    """Number step (abhi koi OTP client nahi) pending ho to hata do."""
    st = _mic_login.get(uid)
    if st and not st.get("client") and st.get("stage") in {"phone", "string"}:
        _mic_login.pop(uid, None)


async def get_mic_engine(msg):
    """Live Mic ke liye engine: Main ID ho to wahi, warna user ki ⚔️ Fight ID.

    Main ID zaroori nahi — Fight ID + private browser link kaafi hai.
    """
    from helpers.setup_flow import has_fight_id, has_main_id
    from helpers import sudo as _sudo
    uid = msg.from_user.id
    if await has_main_id(uid):
        return await get_engine(msg)
    relay = None
    try:
        relay = await session_manager.get_relay(uid)
    except Exception:
        relay = None
    if relay is not None and await has_fight_id(uid):
        return relay
    if await _sudo.is_sudo_mode(uid):
        return await get_engine(msg)
    if relay is not None:
        return relay
    await mic_notify(msg, NEED_ACCOUNT_TEXT, reply_markup=need_account_kb())
    return None


NEED_ACCOUNT_TEXT = (
    "🔐 <b>Live Mic ke liye ek ID chahiye</b>\n\n"
    "• <b>🔐 Login</b> — apni ID (number + OTP) · sab features\n"
    "• <b>⚔️ Fight ID</b> — koi doosri ID · sirf Live Mic\n\n"
    "Dono me bas 1 minute lagta hai."
)


def need_account_kb() -> K:
    return K([[B("🔐 Login", callback_data="menu:login", style="success"),
               B("⚔️ Fight ID", callback_data="mic:acct", style="primary")],
              [B("🏠 Home", callback_data="menu:home", style="primary")]])


async def _spare_cancel(uid: int):
    st = _mic_login.pop(uid, None)
    client = st.get("client") if st else None
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass


async def _spare_remove(uid: int):
    await db.delete_app_value(f"assistant_session_{uid}")
    await session_manager.drop_relay(uid)


async def _spare_save(uid: int, session_str: str, account_id: int = None,
                      slot: int = 1):
    """Validate + save a spare session. Returns (ok, message)."""
    session_str = (session_str or "").strip()
    if slot > 1:
        return await _spare_save_extra(uid, session_str, account_id, slot)
    own = await db.get_user(uid)
    if own and (own.get("string_session") or "").strip() == session_str:
        return False, ("ℹ️ Aap isi ID se already 🔐 logged in ho — Live Mic isi se chal jayega.\n"
                       "⚔️ Fight ID ke liye koi <b>doosri</b> ID chahiye (optional).")
    if account_id is not None and account_id == uid:
        # User ne apni hi ID daal di: OTP barbaad mat karo, isko Login bana do.
        return await promote_to_login(uid, session_str)
    await db.set_app_value(f"assistant_session_{uid}", session_str)
    await session_manager.drop_relay(uid)
    relay = await session_manager.get_relay(uid)
    if not relay:
        await db.delete_app_value(f"assistant_session_{uid}")
        return False, "❌ Account save hua par connect nahi ho paya. Dobara try karein."
    if relay.account_id == uid:
        await _spare_remove(uid)
        return await promote_to_login(uid, session_str)
    return True, ("✅ <b>Fight ID jud gayi!</b>\n\n"
                  f"├ Name: <code>{relay.account_name}</code>\n"
                  f"└ ID: <code>{relay.account_id}</code>\n\n"
                  "<b>Agla step 👇</b> 🎙 Live Mic shuru karo → group batao → "
                  "link Chrome me kholo aur bolo.\n\n"
                  "<i>Zaroori: Fight ID ko us group me add rakho.</i>")


async def promote_to_login(uid: int, session_str: str):
    """Fight ID flow me user ki APNI ID aayi -> use 🔐 Login (main) bana do."""
    try:
        await db.update_string(uid, session_str)
        try:
            from helpers import sudo as _sudo
            _sudo._cache_clear(uid)
        except Exception:
            pass
        try:
            await session_manager.add(uid, session_str)
        except Exception as e:
            await log_error("promote_to_login", e)
    except Exception as e:
        await log_error("promote_to_login_save", e)
        return False, f"❌ Login save nahi hua: <code>{str(e)[:120]}</code>"
    return True, ("✅ <b>Login ho gaya!</b>\n\n"
                  "Ye aapki apni ID thi, isliye isse <b>🔐 Login</b> kar diya — "
                  "OTP barbaad nahi hua.\n\n"
                  "🎤 Live Mic + ▶️ Player ab ready.\n"
                  "<i>⚔️ Fight ID optional hai — chaho to baad me koi doosri ID jodo.</i>")


async def _spare_save_extra(uid: int, session_str: str, account_id, slot: int):
    """Multi-spare slot (2..MAX) save — main ID / dusre spare se alag ho."""
    if account_id is not None and account_id == uid:
        return False, "❌ Ye aapki main ID hai. Extra Fight ID alag ID honi chahiye."
    own = await db.get_user(uid)
    if own and (own.get("string_session") or "").strip() == session_str:
        return False, "❌ Ye wahi account hai jisse aap login hain."
    for other in await session_manager.spare_slots(uid):
        if other != slot and await session_manager.assistant_string(uid, other) == session_str:
            return False, f"❌ Ye ID pehle se Fight ID #{other} me lagi hai."
    key = session_manager.spare_key(uid, slot)
    await db.set_app_value(key, session_str)
    relay = await session_manager.get_relay(uid, slot)
    if not relay:
        await db.delete_app_value(key)
        return False, "❌ Account connect nahi ho paya. Dobara try karein."
    used = set()
    for other in await session_manager.spare_slots(uid):
        if other != slot:
            r = await session_manager.get_relay(uid, other)
            if r:
                used.add(r.account_id)
    if relay.account_id == uid or relay.account_id in used:
        await session_manager.drop_relay(uid, slot)
        await db.delete_app_value(key)
        return False, "❌ Ye ID aapki main ID hai ya pehle se Fight ID me lagi hai — alag ID lagao."
    return True, (f"✅ <b>Extra Fight ID #{slot} jud gayi!</b>\n\n"
                  f"├ Name: <code>{relay.account_name}</code>\n"
                  f"└ ID: <code>{relay.account_id}</code>\n\n"
                  "Agli baar 🟢 <b>Mic ON</b> par ye ID bhi pehli Fight ID ke saath "
                  "<b>ek saath</b> target VC me bolegi.\n"
                  "<i>Is ID ko target group me add karna zaroori hai.</i>")


async def _spare_send_code(uid: int, phone: str):
    if not Config.API_ID or not Config.API_HASH:
        return False, "❌ Bot me API_ID/API_HASH set nahi hai — owner se bolein."
    slot = int((_mic_login.get(uid) or {}).get("slot", 1))
    await _spare_cancel(uid)
    client = Client(name=f"miclogin_{uid}", api_id=Config.API_ID,
                    api_hash=Config.API_HASH, in_memory=True, no_updates=True)
    try:
        await client.connect()
        sent = await client.send_code(phone)
    except Exception as e:
        try:
            await client.disconnect()
        except Exception:
            pass
        return False, (f"❌ OTP bhejna fail: <code>{e}</code>\n"
                       "Number country code ke saath bhejein, jaise "
                       "<code>+919876543210</code>.")
    _mic_login[uid] = {"client": client, "phone": phone, "slot": slot,
                       "hash": sent.phone_code_hash, "stage": "code"}
    return True, ("📩 <b>OTP bhej diya</b> us number ke Telegram par.\n\n"
                  "Ab OTP yahin bhej dein, jaise <code>12345</code>.\n"
                  "<i>Message auto-delete ho jayega.</i>")


async def _spare_handle_text(uid: int, text: str):
    """Drive the pending spare login with a plain message. Returns (reply, done)."""
    text = (text or "").strip()
    st = _mic_login.get(uid)
    stage = st.get("stage") if st else "phone"

    # A pasted session string works at any "what do I send?" stage.
    if stage in {"phone", "string"}:
        if len(text) > 60 and " " not in text:
            ok, message = await _spare_save(uid, text, slot=int((st or {}).get("slot", 1)))
            if ok:
                await _spare_cancel(uid)
            return message, ok
        phone = re.sub(r"[\s\-()]", "", text)
        if not re.fullmatch(r"\+?\d{10,15}", phone):
            return ("❌ Number samajh nahi aaya. Country code ke saath bhejein, jaise "
                    "<code>+919876543210</code>."), False
        if not phone.startswith("+"):
            phone = "+" + phone
        _, message = await _spare_send_code(uid, phone)
        return message, False

    client = st["client"]
    try:
        if stage == "code":
            code = re.sub(r"\D", "", text)
            if not code:
                return "❌ OTP me sirf digits bhejein, jaise <code>12345</code>.", False
            try:
                await client.sign_in(st["phone"], st["hash"], code)
            except SessionPasswordNeeded:
                st["stage"] = "password"
                return ("🔐 Is account par <b>2-Step Verification</b> ON hai.\n"
                        "Ab uska password yahin bhej dein."), False
        else:
            await client.check_password(text)
    except (PhoneCodeInvalid, PhoneCodeExpired):
        return ("❌ OTP galat ya expire ho gaya. Naya OTP lene ke liye number dobara "
                "bhejein, ya Cancel karein."), False
    except PasswordHashInvalid:
        return "❌ Password galat hai. Dobara bhejein.", False
    except Exception as e:
        await _spare_cancel(uid)
        return f"❌ Login fail: <code>{e}</code>", False

    try:
        session_str = await client.export_session_string()
        me = await client.get_me()
    except Exception as e:
        await _spare_cancel(uid)
        return f"❌ Session banane me error: <code>{e}</code>", False
    slot = int(st.get("slot", 1))
    await _spare_cancel(uid)
    return await _spare_save(uid, session_str, account_id=me.id, slot=slot)


@Client.on_message(HAS_USER & filters.private & filters.text
                   & ~filters.regex(r"^[/.!]"), group=3)
async def spare_login_listener(bot: Client, msg: Message):
    """Plain messages drive the spare login — user ko command yaad rakhne ki
    zaroorat nahi, Live Mic button se flow start hota hai."""
    uid = msg.from_user.id
    if uid not in _mic_login:
        return
    try:
        await msg.delete()
    except Exception:
        pass
    reply, done = await _spare_handle_text(uid, cmd_text(msg))
    kb = (await _after_save_kb(uid)) if done else spare_setup_kb(uid in _mic_login)
    await bot.send_message(msg.chat.id, reply, reply_markup=kb,
                           disable_web_page_preview=True)


@Client.on_message(HAS_USER & cmd_prefix(r"(micaccount|fightid)\b", flags=re.IGNORECASE) & filters.private)
async def cmd_micaccount(bot: Client, msg: Message):
    """Spare account setup — number/OTP se, ya session string se."""
    uid = msg.from_user.id
    parts = cmd_text(msg).split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""

    if arg.lower() in {"off", "remove", "clear", "delete"}:
        await _spare_cancel(uid)
        await _spare_remove(uid)
        await msg.reply_text(
            "🗑 <b>Fight ID hata di.</b>\n\n"
            "Live Mic ke liye nayi ⚔️ Fight ID jodo: <code>.fightid</code>")
        return

    if arg.lower() == "cancel":
        await _spare_cancel(uid)
        await msg.reply_text("❎ Fight ID jodna cancel kar diya.")
        return

    if arg and arg.lower() != "status":
        try:
            await msg.delete()
        except Exception:
            pass
        reply, done = await _spare_handle_text(uid, arg)
        kb = (await _after_save_kb(uid)) if done else spare_setup_kb(uid in _mic_login)
        await bot.send_message(msg.chat.id, reply, reply_markup=kb,
                               disable_web_page_preview=True)
        return

    await start_fight_setup(uid)
    text, kb = await spare_screen(uid)
    await msg.reply_text(text, reply_markup=kb, disable_web_page_preview=True)


@Client.on_message(HAS_USER & cmd_prefix(r"spare\b", flags=re.IGNORECASE) & filters.private)
async def cmd_spare(bot: Client, msg: Message):
    """Spare ID ko VC me baithana / uthana — bot DM se, group me kuch nahi."""
    from helpers.live_mic import park_relay, unpark_relay, is_active
    parts = cmd_text(msg).split()
    action = parts[1].lower() if len(parts) > 1 else "help"
    if action not in {"join", "sit", "leave", "left", "mute", "unmute"}:
        await msg.reply_text(
            "🪑 <b>Fight ID in VC</b>\n\n"
            "• <code>.spare join</code> — Fight ID VC me baith jaye (mic OFF)\n"
            "• <code>.spare join -100xxx</code> — kisi aur group me\n"
            "• <code>.spare mute</code> / <code>.spare unmute</code> — TG mic icon\n"
            "• <code>.spare leave</code> — Fight ID VC se nikal jaye\n"
            "   (live mic chalu ho tab bhi chalega — mic band + VC exit)\n\n"
            "<code>.mic off</code> karne par bhi Fight ID VC me baithi rehti hai.")
        return
    uid = msg.from_user.id
    relay = await session_manager.get_relay(uid)
    if not relay:
        text, kb = await spare_screen(uid)
        await msg.reply_text(text, reply_markup=kb, disable_web_page_preview=True)
        return
    raw = parts[2] if len(parts) > 2 else (await db.get_app_value(f"mic_chat_{uid}"))
    if not raw:
        await msg.reply_text("🎯 Group batao: <code>.spare join -100xxx</code> ya pehle <code>.mic chat -100xxx</code>.")
        return
    cid, _ = await target_chat(msg, str(raw))
    if not cid:
        await msg.reply_text("❌ Group nahi mila.")
        return
    if action in {"mute", "unmute"}:
        names = ("mute", "mute_stream") if action == "mute" else ("unmute", "unmute_stream")
        ok = False
        for n in names:
            fn = getattr(relay.calls, n, None)
            if fn:
                try:
                    await fn(cid); ok = True; break
                except Exception:
                    pass
        await msg.reply_text(("🔇 Fight ID mic icon OFF" if action == "mute" else "🎙 Fight ID mic icon ON") if ok
                             else "❌ Fight ID VC me nahi hai. Pehle <code>.spare join</code>.")
        return
    if action in {"leave", "left"} and is_active(uid):
        # Force exit: live mic band karo AUR spare ID ko VC se bahar nikalo.
        from helpers.live_mic import stop_session, reset_mic_state
        await stop_session(uid, leave_vc=True)
        uvc = await get_engine(msg)
        if uvc:
            try:
                await reset_mic_state(uvc, uid)
            except Exception:
                pass
        await unpark_relay(relay, cid)
        await msg.reply_text(
            "👋 <b>Fight ID VC se nikal gayi</b> aur live mic band ho gaya: "
            f"<code>{cid}</code>")
        return
    if is_active(msg.from_user.id):
        await msg.reply_text("Live mic chal raha hai — Fight ID pehle se VC me hai. Pehle <code>.mic off</code>.")
        return
    if action in {"join", "sit"}:
        ok = await park_relay(relay, cid)
        await msg.reply_text(
            f"🪑 Fight ID VC me baith gayi (mic OFF): <code>{cid}</code>" if ok else
            "❌ Join nahi hua. Check: group me VC chalu hai aur Fight ID group ki member hai.")
    else:
        await unpark_relay(relay, cid)
        await msg.reply_text(f"👋 Fight ID VC se nikal gayi: <code>{cid}</code>")


@Client.on_message(HAS_USER & cmd_prefix(r"(target|settarget|tgt)\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_target(bot: Client, msg: Message):
    """.target <id/@user/link> — shortcut for .mic chat."""
    parts = cmd_text(msg).split()
    msg.text = " ".join([".mic", "chat", *parts[1:]])
    await cmd_mic(bot, msg)


@Client.on_message(HAS_USER & cmd_prefix(r"mic\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_mic(bot: Client, msg: Message):
    """Live Mic = Web Browser Mic (v81, VC Bridge hata diya)."""
    parts = cmd_text(msg).split()
    action = parts[1].lower() if len(parts) > 1 else "help"

    if action in {"chat", "setchat", "target", "tgt"}:
        # In a group, bare `.mic chat` selects this group. In DM, prompt for
        # a numeric ID so the user doesn't need to memorize a command syntax.
        if len(parts) < 3 and msg.chat and msg.chat.id < 0:
            parts = parts[:2] + [str(msg.chat.id)]
        if len(parts) < 3:
            saved = await db.get_app_value(f"mic_chat_{msg.from_user.id}")
            from plugins.live_mic_target import request_target_input
            request_target_input(msg.from_user.id, surface="chat")
            await mic_notify(msg,
                "🎯 <b>Live Mic target group</b>: "
                + (f"<code>{saved}</code>\n\n" if saved else "not set\n\n")
                + "Is private chat mein group ID, <code>@username</code> ya invite link bhejo.\n"
                + "<i>5 minute tak active. Cancel: <code>.mic cancel</code></i>")
            return
        from plugins.live_mic_target import set_target
        cid, text = await set_target(msg, msg.from_user.id, parts[2])
        if cid and "🔁" not in text and "⚠️" not in text:
            text += ("\n\nAb bot DM me sirf <code>.mic on</code> bhejo — aawaz isi "
                     "group ki VC me jayegi.")
        await mic_notify(msg, text)
        return

    alias = {"start": "on", "stop": "off", "exit": "leave", "out": "leave",
             "vcleave": "leave", "devices": "help", "list": "help"}
    action = alias.get(action, action)
    from plugins.web_mic import run_web_mic
    if action in {"link", "seturl", "domain"}:
        action = "url"
    await run_web_mic(msg, action if action in {"on", "off", "leave", "status", "url", "cancel"} else "help", parts[2:])


@Client.on_message(HAS_USER & cmd_prefix(r"(setgc|unsetgc|gc)\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_setgc(bot: Client, msg: Message):
    """Set a default group once; every command then targets it automatically."""
    uid = msg.from_user.id
    from helpers import sudo as _sudo
    if await _sudo.is_sudo_mode(uid):
        from plugins.sudo import sudo_setgc
        await sudo_setgc(msg)
        return
    parts = cmd_text(msg).split()
    name = parts[0].lstrip("./!").lower()
    if name == "unsetgc":
        DEFAULT_GC.pop(uid, None)
        _save_gc()
        await msg.reply_text("✅ Default group hata diya.")
        return
    if name == "gc" or (name == "setgc" and len(parts) == 1 and not (msg.chat and msg.chat.id < 0)):
        cur = DEFAULT_GC.get(uid)
        if name == "setgc" and msg.chat and str(getattr(msg.chat.type, "value", msg.chat.type)).lower() == "private":
            from plugins.live_mic_target import request_target_input
            request_target_input(uid, surface="setgc")
            await msg.reply_text(
                f"🎯 <b>Default + Live Mic group:</b> <code>{cur or 'not set'}</code>\n\n"
                "Ab isi DM me group ka numeric chat ID bhejo, jaise <code>-1001234567890</code>.\n"
                "Group verify hoga, phir default group aur Live Mic dono set honge.\n"
                "Cancel: <code>.mic cancel</code> · Prompt 5 minute tak active rahega.")
            return
        await msg.reply_text(
            f"🎯 <b>Default + Live Mic group:</b> <code>{cur or 'not set'}</code>\n"
            "Set karne ke liye: target group me <code>.setgc</code> ya DM me <code>.setgc -100xxxx</code> bhejo.")
        return
    arg = parts[1] if len(parts) > 1 else None
    # Resolve explicitly before changing either setting. Bare `.setgc` in a
    # group means this group; a failed validation must preserve the old target.
    target_ref_text = arg or (str(msg.chat.id) if msg.chat and msg.chat.id < 0 else None)
    cid, _ = await target_chat(msg, target_ref_text)
    if not cid:
        await msg.reply_text("❌ Group resolve nahi hua. Chat ID / @username / invite link dein.")
        return
    # Keep the Live Mic's dedicated target aligned with .setgc as well. Use
    # the same verified resolver as .mic chat so an unverified guessed ID is
    # never shown as a successful microphone destination.
    from plugins.live_mic_target import set_target
    target_ref_text = target_ref_text or str(cid)
    mic_cid, mic_text = await set_target(msg, uid, target_ref_text)
    if not mic_cid:
        await msg.reply_text(mic_text)
        return
    cid = mic_cid
    DEFAULT_GC[uid] = cid
    _save_gc()
    await msg.reply_text(
        f"✅ <b>Default group + Live Mic target set:</b> <code>{cid}</code>\n"
        "Ab <code>.mic on</code> ya dusri default-target commands DM se bhi isi group ko use karengi.\n"
        "Hatane ke liye <code>.unsetgc</code> default commands se unset karega; mic target <code>.mic chat</code> se badlo.")


# ----------------------------------------------------------------------
# VC CHAT + EMOJI REACTIONS — logged-in ID voice chat ke chat panel me
# message / floating emoji reaction bhejti hai.
# ----------------------------------------------------------------------
_VC_REACT_DEFAULT = "❤️"
_VC_REACT_MAX = 10


def _split_chat_arg(parts):
    """Last token may be a chat id / @username / invite link."""
    if parts and is_chat_ref(parts[-1]):
        return parts[:-1], parts[-1]
    return parts, None


async def _vc_send(msg: Message, uvc, cid: int, text: str) -> bool:
    try:
        await uvc.send_vc_message(cid, text)
        return True
    except Exception as exc:
        err = str(exc)
        if "GROUPCALL_MESSAGES_DISABLED" in err or "CHAT_SEND" in err or "FORBIDDEN" in err:
            hint = "Is VC me chat band hai ya aapki ID ko message bhejne ki permission nahi."
        elif "GROUPCALL_JOIN_MISSING" in err or "PARTICIPANT" in err:
            hint = "ID auto-join nahi ho payi. VC chalu hai aur ID group me hai, ye check karke dobara try karein."
        else:
            hint = "VC chalu hai aur ID group me muted nahi hai, ye check karein."
        await mic_notify(msg, f"❌ VC chat nahi gaya.\n<code>{html.escape(err[:300])}</code>\n{hint}")
        return False


@Client.on_message(HAS_USER & cmd_prefix(r"(vcchat|vcmsg|vcm)\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_vcchat(bot: Client, msg: Message):
    """.vcchat <text> [-100chat] — VC chat panel me message."""
    uvc = await get_engine(msg)
    if not uvc:
        return
    raw = cmd_text(msg).split(maxsplit=1)
    body = raw[1] if len(raw) > 1 else ""
    if not body and msg.reply_to_message:
        body = msg.reply_to_message.text or msg.reply_to_message.caption or ""
    words, chat_arg = _split_chat_arg(body.split(" "))
    text = " ".join(words).strip()
    if not text:
        await mic_notify(msg, "💬 <b>VC Chat</b>\n<code>.vcchat Hello sab log</code>\n"
                              "<code>.vcchat hii -100xxxx</code> (DM se)\n"
                              "Kisi message ko reply karke <code>.vcchat</code> = wahi text VC me.\n"
                              "Reaction: <code>.vcreact 🔥 5</code>")
        return
    cid = await need_chat(msg, chat_arg)
    if not cid:
        return
    if msg.chat and msg.chat.id < 0:
        try:
            await msg.delete()
        except Exception:
            pass
    if await _vc_send(msg, uvc, cid, text):
        if not (msg.chat and msg.chat.id < 0):
            await msg.reply_text("✅ VC chat me bhej diya.")


@Client.on_message(HAS_USER & cmd_prefix(r"(vcreact|vcr|vcemoji)\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_vcreact(bot: Client, msg: Message):
    """.vcreact [emoji ...] [count] [-100chat] — floating VC reactions."""
    uvc = await get_engine(msg)
    if not uvc:
        return
    words, chat_arg = _split_chat_arg(cmd_text(msg).split()[1:])
    count = 1
    if words and words[-1].isdigit():
        count = max(1, min(_VC_REACT_MAX, int(words[-1])))
        words = words[:-1]
    emojis = words or [_VC_REACT_DEFAULT]
    cid = await need_chat(msg, chat_arg)
    if not cid:
        return
    if msg.chat and msg.chat.id < 0:
        try:
            await msg.delete()
        except Exception:
            pass
    sent = 0
    for i in range(count):
        if not await _vc_send(msg, uvc, cid, emojis[i % len(emojis)]):
            break
        sent += 1
        if i + 1 < count:
            await asyncio.sleep(0.7)  # Telegram flood-limit se bachne ke liye
    if sent and not (msg.chat and msg.chat.id < 0):
        await msg.reply_text(f"✅ {sent} reaction VC me bhej diye.")


# ───────────────────────────── Spare ID chat ─────────────────────────────
# Fight ke time message spare account se group me bhejna:
#   • Mic panel → "💬 Spare ID se Chat bhejo" → ON → ab bot DM me jo bhi
#     text likhoge wo spare ID se group me chala jayega (aur tumhara msg
#     DM se delete ho jayega).
#   • Ya seedha: .say <text>   (group me likho to tumhara msg delete hoke
#     spare ID se jayega; DM me likho to default/mic wale group me).
#   • Reply ke saath .say karoge to spare ID usi msg ko reply karegi.

_spare_chat: dict = {}   # uid -> target chat id


async def _spare_chat_target(uid: int) -> int:
    if uid in DEFAULT_GC:
        return DEFAULT_GC[uid]
    try:
        uvc = await session_manager.get(uid)
        if uvc:
            for cid, st in uvc.chats.items():
                if getattr(st, "mic_enabled", False):
                    return cid
            for cid, st in uvc.chats.items():
                if getattr(st, "is_playing", False):
                    return cid
    except Exception:
        pass
    return 0


async def spare_chat_screen(uid: int):
    on = uid in _spare_chat
    cid = _spare_chat.get(uid) or await _spare_chat_target(uid)
    text = (
        "💬 <b>Fight ID se Chat</b>\n\n"
        f"Status: <b>{'🟢 ON' if on else '🔴 OFF'}</b>\n"
        f"Group: <code>{cid or 'set nahi'}</code>\n\n"
        "ON karne ke baad is DM me jo bhi likhoge, wo <b>Fight ID</b> "
        "se group me jayega.\n\n"
        "Shortcut: <code>.say teri aawaz nahi aa rahi</code>\n"
        "Kisi msg ko reply karke <code>.say ...</code> = Fight ID us msg ko reply karegi."
    )
    kb = K([
        [B("🔴 Chat mode OFF" if on else "🟢 Chat mode ON",
           callback_data="mic:chat:off" if on else "mic:chat:on")],
        [B("⬅ Back", callback_data="mic:panel")],
    ])
    return text, kb


async def _send_via_spare(uid: int, cid: int, text: str, reply_to: int = None):
    relay = await session_manager.get_relay(uid)
    if not relay or not getattr(relay, "client", None):
        return "❌ Fight ID judi nahi hai. Home → ⚔️ Fight ID jodo."
    try:
        kwargs = {"disable_web_page_preview": True}
        if reply_to:
            kwargs["reply_to_message_id"] = reply_to
        await relay.client.send_message(cid, text, **kwargs)
        return None
    except Exception as e:
        return f"❌ Fight ID se msg nahi gaya: <code>{html.escape(str(e))}</code>\n" \
               "(Fight ID group me member honi chahiye aur muted nahi.)"


@Client.on_message(HAS_USER & cmd_prefix(r"say\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_say(bot: Client, msg: Message):
    uid = msg.from_user.id
    parts = cmd_text(msg).split(maxsplit=1)
    text = parts[1].strip() if len(parts) > 1 else ""
    if not text:
        t, kb = await spare_chat_screen(uid)
        await msg.reply_text(t, reply_markup=kb)
        return
    if msg.chat and msg.chat.id < 0:
        cid = msg.chat.id
    else:
        cid = _spare_chat.get(uid) or await _spare_chat_target(uid)
    if not cid:
        await msg.reply_text("❌ Group nahi mila. Group me <code>.say</code> likho ya pehle mic ON karo.")
        return
    reply_to = None
    if msg.reply_to_message and msg.chat and msg.chat.id == cid:
        reply_to = msg.reply_to_message.id
    try:
        await msg.delete()
    except Exception:
        pass
    err = await _send_via_spare(uid, cid, text, reply_to)
    if err:
        await mic_notify(msg, err)


# group=6 (was 4): Pyrogram runs only ONE handler per group, and the Live
# Live Mic target listener also sits in group 4 with the
# same filter -> one of them silently swallowed the other's messages.
@Client.on_message(HAS_USER & filters.private & filters.text
                   & ~filters.regex(r"^[/.!]"), group=6)
async def spare_chat_listener(bot: Client, msg: Message):
    uid = msg.from_user.id
    if uid not in _spare_chat or uid in _mic_login:
        return
    err = await _send_via_spare(uid, _spare_chat[uid], msg.text)
    if err:
        await msg.reply_text(err)
        return
    try:
        await msg.delete()
    except Exception:
        pass


@Client.on_message(HAS_USER & cmd_prefix(r"(playdb|pldb)\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_playdb(bot: Client, msg: Message):
    parts = cmd_text(msg).split()
    s = await db.get_settings(msg.from_user.id)
    if len(parts) < 2:
        cur = float(s.get("play_drive_db", 10.0) or 10.0)
        await msg.reply_text(
            f"📻 <b>Play Drive dB:</b> <code>{cur:+.1f} dB</code>\n\n"
            "💡 <i>Play/Tag audio & songs ki boost power. Range: 0 to 24 dB.</i>\n"
            "Usage: <code>.playdb 14</code> ya <code>.playdb +2</code> ya <code>.playdb -2</code>",
            reply_markup=K([[B("🎚️ Sound Controls", callback_data="menu:settings")]])
        )
        return
    raw = parts[1].strip()
    try:
        if raw.startswith(("+", "-")):
            val = float(s.get("play_drive_db", 10.0) or 10.0) + float(raw)
        else:
            val = float(raw)
    except ValueError:
        await msg.reply_text("❌ Sahi number dalein, jaise: <code>.playdb 14</code> ya <code>.playdb +2</code>")
        return
    val = round(max(0.0, min(24.0, val)), 1)
    os.environ["PLAY_DRIVE_DB"] = str(val)
    await _apply_and_reply(
        msg,
        f"📻 <b>Play Drive set: {val:+.1f} dB</b>\n"
        "<i>(Played audio & tagged voice boost updated)</i>",
        play_drive_db=val
    )

@Client.on_message(HAS_USER & cmd_prefix(r"(masterdb|mstrdb)\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_masterdb(bot: Client, msg: Message):
    parts = cmd_text(msg).split()
    s = await db.get_settings(msg.from_user.id)
    if len(parts) < 2:
        cur = float(s.get("master_drive_db", 12.0) or 12.0)
        await msg.reply_text(
            f"🎙️ <b>Master Drive dB:</b> <code>{cur:+.1f} dB</code>\n\n"
            "💡 <i>Playback aur Live Mic ki power. Range: 0 to 24 dB.</i>\n"
            "Usage: <code>.masterdb 16</code> ya <code>.masterdb +2</code> ya <code>.masterdb -2</code>",
            reply_markup=K([[B("🎚️ Sound Controls", callback_data="menu:settings")]])
        )
        return
    raw = parts[1].strip()
    try:
        if raw.startswith(("+", "-")):
            val = float(s.get("master_drive_db", 12.0) or 12.0) + float(raw)
        else:
            val = float(raw)
    except ValueError:
        await msg.reply_text("❌ Sahi number dalein, jaise: <code>.masterdb 14</code> ya <code>.masterdb +2</code>")
        return
    val = round(max(0.0, min(24.0, val)), 1)
    os.environ["MASTER_DRIVE_DB"] = str(val)
    await _apply_and_reply(
        msg,
        f"🎙️ <b>Master Drive set: {val:+.1f} dB</b>\n"
        "<i>(Playback aur Live Mic power updated)</i>",
        master_drive_db=val
    )

@Client.on_message(HAS_USER & cmd_prefix(r"(ceiling|ceilingdb|ceildb)\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_ceiling(bot: Client, msg: Message):
    parts = cmd_text(msg).split()
    s = await db.get_settings(msg.from_user.id)
    if len(parts) < 2:
        cur = float(s.get("play_ceiling_db", -0.3) or -0.3)
        await msg.reply_text(
            f"🛡️ <b>Limiter Ceiling:</b> <code>{cur:.1f} dBFS</code>\n\n"
            "• <code>-0.3</code> = 🔥 MAX (Default - sabse tagdi aawaz)\n"
            "• <code>-1.5</code> = ⚡ Loud (thoda kam crunch)\n"
            "• <code>-3.0</code> = 🛡️ Soft (clean, lekin normal jaisa lagega)\n\n"
            "Usage: <code>.ceiling -0.3</code> ya <code>.ceiling -1.5</code>",
            reply_markup=K([[B("🎚️ Sound Controls", callback_data="menu:settings")]])
        )
        return
    raw = parts[1].strip()
    try:
        val = float(raw)
    except ValueError:
        await msg.reply_text("❌ Sahi number dalein, jaise: <code>.ceiling -2.5</code>")
        return
    val = round(max(-6.0, min(-0.1, val)), 1)
    os.environ["PLAY_CEILING_DB"] = str(val)
    os.environ["MASTER_CEILING_DB"] = str(val)
    await _apply_and_reply(
        msg,
        f"🛡️ <b>Limiter Ceiling set: {val:.1f} dBFS</b>\n"
        "<i>(Output ceiling updated)</i>",
        play_ceiling_db=val
    )

@Client.on_message(HAS_USER & cmd_prefix(r"(sound|controls)\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_sound_shortcut(bot: Client, msg: Message):
    from plugins.ui import settings_kb, settings_text
    s = await db.get_settings(msg.from_user.id)
    await msg.reply_text(settings_text(s), reply_markup=settings_kb(s))
