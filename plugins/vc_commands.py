import html

import asyncio
import os

import re
from pyrogram import Client, filters
from pyrogram.errors import (SessionPasswordNeeded, PhoneCodeInvalid,
                             PhoneCodeExpired, PasswordHashInvalid)
from plugins.ui import HAS_USER, cmd_text, B, edit_screen, safe_answer, mic_text, mic_kb, cmd_prefix
from pyrogram.types import InlineKeyboardMarkup as K
from pyrogram.types import Message

from config import Config
from helpers.access_control import check_access, record_usage
from helpers.audio_processor import (
    BASS_MAX, BASS_MIN, LEVEL_MAX, LEVEL_MIN, VOLUME_MAX, VOLUME_MIN, clamp,
)
from helpers.database import db
from helpers.logger_channel import (
    get_channel, log_command, log_error, set_channel, verify_log_channel,
)
from helpers.vc_manager import AUTO_PRESET, VOL_MAX, VOL_NORMAL, session_manager

LINE = "━" * 28

LOGIN_KB = K([
    [B(" Login karein", callback_data="menu:login")],
    [B(" Tutorial", callback_data="menu:tutorial")],
])

LIMIT_KB = K([
    [B("💳 Buy Premium", callback_data="pay:menu")],
    [B("👥 Refer & Earn", callback_data="ref:menu")],
    [B("🏠 Home", callback_data="menu:home")],
])

async def _check_usage_access(msg: Message) -> bool:
    """Gate: returns True if user can use the bot, False if limit reached."""
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


def _save_gc():
    try:
        import json
        with open(_GC_FILE, "w") as f:
            json.dump({str(k): v for k, v in DEFAULT_GC.items()}, f)
    except Exception:
        pass


def now_playing_kb(cid: int, st=None) -> K:
    # Only the essentials: play controls + one "Human Mode" switch.
    paused = bool(st and st.is_paused)
    human = bool(st and (st.hand_raise or st.mic_blink))
    ss = bool(st and getattr(st, "ss_on", False))
    loop = bool(st and st.loop)
    return K([
        [B("▶ Resume" if paused else "⏸ Pause",
           callback_data=f"vc:{'resume' if paused else 'pause'}:{cid}"),
         B("⏭ Skip", callback_data=f"vc:skip:{cid}"),
         B("⏹ Stop", callback_data=f"vc:stop:{cid}")],
        [B(f"🔁 Loop: {'ON' if loop else 'OFF'}", callback_data=f"vc:loop:{cid}"),
         B(f"🖥 Screen: {'ON' if ss else 'OFF'}", callback_data=f"vc:ss:{cid}")],
        [B(f"🧑 Human Mode: {'ON' if human else 'OFF'}", callback_data=f"vc:human:{cid}")],
        [B("🎚️ Audio Controls", callback_data="menu:settings"),
         B("🔄 Refresh", callback_data=f"vc:now:{cid}")],
    ])

async def get_engine(msg: Message):
    uvc = await session_manager.get(msg.from_user.id)
    if not uvc:
        # Never raise here: in a restricted / non-admin group the bot may not be
        # allowed to post, and that must not swallow the command silently.
        await mic_notify(
            msg,
            " <b>Pehle login karein.</b>\n\n"
            "Bot ke DM mein jaakar  Login   Phone se Login, "
            "ya apna string session add karein.",
            reply_markup=LOGIN_KB,
        )
    return uvc

async def target_chat(msg: Message, arg: str = None) -> tuple:
    """Return (chat_id, join_reference) where join_reference is a username or
    invite link the logged-in account can use to join the group if needed."""
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
            ref = arg if arg.startswith("@") or "t.me/" in arg or "+" in arg else None
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
            " Voice chat sirf <b>groups</b> mein hota hai.\n"
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
    reply = msg.reply_to_message
    media = None
    if reply:
        media = (reply.audio or reply.voice or reply.video or reply.document
                 or reply.video_note)
    if not media:
        await msg.reply_text(" Kisi audio/video message ko reply karke <code>.tag</code> likhein.")
        return
    name = parts[1].strip().lower()
    ftype = "audio" if (reply.audio or reply.voice) else "video"
    await db.tag_file(msg.from_user.id, name, media.file_id, ftype, reply.caption or "")
    await msg.reply_text(f" Saved as <code>{name}</code> — ab <code>.play {name}</code>")

@Client.on_message(HAS_USER & cmd_prefix(r"untag\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_untag(bot: Client, msg: Message):
    parts = cmd_text(msg).split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply_text("Usage: <code>.untag &lt;name&gt;</code>")
        return
    name = parts[1].strip().lower()
    if not await db.get_tag(msg.from_user.id, name):
        await msg.reply_text(f" Tag <code>{name}</code> nahi mila.")
        return
    await db.delete_tag(msg.from_user.id, name)
    await msg.reply_text(f" <code>{name}</code> delete ho gaya.")

@Client.on_message(HAS_USER & cmd_prefix(r"tags\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_tags(bot: Client, msg: Message):
    tags = await db.list_tags(msg.from_user.id)
    if not tags:
        await msg.reply_text(" Koi tag nahi. <code>.tag &lt;name&gt;</code> se save karein.")
        return
    lines = [f"• <code>{t['tag_name']}</code> — {t['file_type']}" for t in tags]
    await msg.reply_text(" <b>Your Tags</b>\n" + "\n".join(lines))

async def resolve_source(bot: Client, msg: Message, arg: str):
    reply = msg.reply_to_message
    if reply:
        media = (reply.audio or reply.voice or reply.video or reply.document
                 or reply.video_note)
        if media:
            source_file_id = media.file_id
            stat = await msg.reply_text("⬇ Media download ho raha hai…")
            try:
                archived = await db.get_archived_audio(source_file_id)
                path = await bot.download_media(archived["archive_file_id"] if archived else source_file_id)
            except Exception as e:
                await stat.edit_text(f" Download fail: <code>{e}</code>")
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
                await stat.edit_text(f" Download fail: <code>{e}</code>")
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
        try:
            if int(p) < 0:
                cid = p
                continue
        except ValueError:
            pass
        if p.startswith("@") or "t.me/+" in p or "t.me/joinchat/" in p:
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
    await db.register_broadcast_chat(cid)

    path, name, source_file_id = await resolve_source(bot, msg, source_arg)
    if not path or not os.path.exists(path):
        return

    try:
        chat = await bot.get_chat(cid)
        title = chat.title or str(cid)
    except Exception:
        title = str(cid)

    await db.register_broadcast_chat(cid, title)
    st = await load_state_settings(msg.from_user.id, uvc, cid)
    stat = await msg.reply_text(" Audio process ho raha hai…")
    try:
        status = await uvc.play(cid, path, name, title, enqueue=enqueue, join_ref=join_ref)
    except Exception as e:
        _cleanup_source(path)
        await stat.edit_text(friendly_error(e))
        await log_error("cmd_play", e)
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

    await stat.edit_text(
        f"{' <b>Queued!</b>' if status == 'queued' else '▶ <b>Playing!</b>'}\n\n"
        f" <b>Source:</b> {name}\n"
        f" <b>Chat:</b> {title}\n"
        + (f" <b>Queue position:</b> {len(uvc.chats[cid].queue) if uvc.chats.get(cid) else 0}\n" if status == 'queued' else '')
        + f" <b>Account:</b> {uvc.account_name}\n"
        f" <b>Volume:</b> {st.relay_volume}/1000 |  <b>Bass:</b> +{st.bass} dB\n"
        f" <b>Boost:</b> {st.boost}/10 |  <b>Echo:</b> "
        f"{'On' if st.echo else 'Off'} {st.echo_level}/10",
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
    await db.register_broadcast_chat(cid)
    path, name, source_file_id = await resolve_source(bot, msg, source_arg)
    if not path or not os.path.exists(path):
        return
    try:
        chat = await bot.get_chat(cid)
        title = chat.title or str(cid)
    except Exception:
        title = str(cid)

    st = await load_state_settings(msg.from_user.id, uvc, cid)
    stat = await msg.reply_text(" <b>FORCE PLAY</b> — process ho raha hai…")
    try:
        await uvc.force_play(cid, path, name, title, join_ref=join_ref)
    except Exception as e:
        _cleanup_source(path)
        await stat.edit_text(friendly_error(e))
        await log_error("cmd_playforce", e)
        return
    await _archive_played_audio(bot, source_file_id, name)
    await stat.edit_text(
        f" <b>Force playing!</b>\n\n"
        f" <b>Source:</b> {name}\n"
        f" <b>Chat:</b> {title}\n"
        f" <b>Volume:</b> {st.relay_volume}/1000 |  <b>Bass:</b> +{st.bass} dB\n"
        f" <b>Boost:</b> {st.boost}/10 |  <b>Echo:</b> "
        f"{'On' if st.echo else 'Off'} {st.echo_level}/10",
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
        if (p.startswith("-") and p[1:].isdigit()) or p.startswith("@") \
                or p.startswith("http"):
            cid_arg = p
        else:
            words.append(p)
    arg = words[0].lower() if words else "on"
    cid = await need_chat(msg, cid_arg)
    if not cid:
        return
    if arg in ("off", "0", "no", "band", "stop"):
        uvc.set_loop(cid, False)
        await msg.reply_text(" <b>Loop OFF</b>")
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
                "Pehle bot ko DM me <code>/start</code> karein."
            )
        except Exception:
            pass
    return None


@Client.on_message(HAS_USER & cmd_prefix(r"padd\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_padd(bot: Client, msg: Message):
    await _play(bot, msg, enqueue=True)

@Client.on_message(HAS_USER & cmd_prefix(r"(playmute|unmuteaudio)\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_unmuteaudio(bot: Client, msg: Message):
    """Set an audio to play when bot is unmuted after admin mute."""
    uvc = await get_engine(msg)
    if not uvc:
        return
    reply = msg.reply_to_message
    media = None
    if reply:
        media = (reply.audio or reply.voice or reply.video or reply.document
                 or reply.video_note)
    if not media:
        await msg.reply_text(
            " Kisi audio/video message ko reply karke <code>.unmuteaudio</code> likhein.\n"
            "Jab koi admin bot ko unmute karega, ye audio pehle bajega, phir normal queue."
        )
        return
    source_arg, cid_arg = _split_args(msg)
    cid = await need_chat(msg, cid_arg)
    if not cid:
        return
    st = uvc.state(cid)
    stat = await msg.reply_text("⬇ Unmute audio download ho raha hai…")
    try:
        path = await bot.download_media(media.file_id)
    except Exception as e:
        await stat.edit_text(f" Download fail: <code>{e}</code>")
        return
    await stat.delete()
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
    loops = 1
    for tok in cmd_text(msg).split()[1:]:
        low = tok.lower()
        if low in ("loop", "inf", "infinite", "always"):
            loops = -1
        elif low.isdigit():
            loops = max(1, int(low))
    st.unmute_loop = loops
    st.unmute_loop_left = loops
    uvc.save_mute_prefs(cid)
    loop_txt = "infinite loop" if loops < 0 else f"{loops}x"
    await msg.reply_text(
        f" <b>Unmute audio set!</b> ({loop_txt})\n\n"
        f"Jab is chat me koi admin bot ko unmute karega: pehle "
        f"{st.mic_blink_secs}s mic on/off (<code>.micblink</code>), phir ye audio "
        f"{loop_txt} bajega, aur uske baad purani recording / queue wapas chalu."
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
        await msg.reply_text(" Hand raise ho gaya." if ok else
                             " Hand raise nahi hua (VC me nahi ya permission nahi).")
        return
    elif arg:
        await msg.reply_text(" Use: <code>.handraise on|off|now</code>")
        return
    await msg.reply_text(
        f"✋ <b>Hand raise:</b> <code>{'ON' if st.hand_raise else 'OFF'}</code>\n"
        f"Admin mute karte hi bot Telegram ke rule ke hisaab se hand raise karega."
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
        await msg.reply_text(" Use: <code>.micblink on|off|&lt;seconds&gt;</code>")
        return
    await msg.reply_text(
        f" <b>Mic blink:</b> <code>{'ON' if st.mic_blink else 'OFF'}</code> — "
        f"<code>{st.mic_blink_secs}s</code>\n"
        f"Unmute hone par pehle sirf mic on/off hoga, recording turant nahi bajegi."
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
        await msg.reply_text(" Use: <code>.loud 0-18</code>")
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
    if arg not in ("on", "off", ""):
        await msg.reply_text(" Use: <code>.ss on|off</code> (image ke liye kisi "
                             "photo ko reply karein)")
        return
    if not arg:
        await msg.reply_text(
            f"️ <b>Screen share:</b> <code>{'ON' if st.ss_on else 'OFF'}</code> — "
            f"{Config.SS_WIDTH}x{Config.SS_HEIGHT}@{Config.SS_FPS}fps"
        )
        return
    image = None
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
                image = None
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
        "️ <b>Screen share ON</b> — bot VC me ek fake mic/mixer setup panel "
        "share kar raha hai (PC-style video)."
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
            await msg.reply_text(" Is VC mein bot ka active session nahi hai.")
            return
        try:
            await uvc.leave(cid, reason="Manual stop")
        except Exception as exc:
            await log_error("transport_stop", exc)
            await msg.reply_text(f" Stop fail hua: <code>{exc}</code>")
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
        await msg.reply_text(" Is chat mein koi active VC session nahi.")
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
    await msg.reply_text(f" <b>Queue cleared</b> — {count} track(s) hata diye." if count
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
        await msg.reply_text(" Number dein, jaise <code>.qremove 2</code>")
        return
    cid = await need_chat(msg, parts[2] if len(parts) > 2 else None)
    if not cid:
        return
    ok = uvc.queue_remove(cid, idx)
    await msg.reply_text(f" Track {idx+1} hata diya." if ok
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
    await msg.reply_text(" Queue shuffle ho gayi!" if ok
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
        await msg.reply_text(" Koi active VC session nahi.")
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
        f" Vol <code>{s['volume']}/1000</code> |  Gain <code>{s['gain']}/200</code> | "
        f" Bass <code>{s['bass']}</code> |  Boost <code>{s['boost']}/10</code> |  Echo "
        f"<code>{'On' if s['echo'] else 'Off'} {s['echo_level']}/10</code>\n"
        + (f" {applied} live VC par apply hua." if applied else
           " Saved — agli play par lagega."),
        reply_markup=K([[B(" Settings Panel", callback_data="menu:settings")]]),
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
        await msg.reply_text("Usage: <code>.vol &lt;0-1000&gt;</code>")
        return
    n = clamp(n, VOLUME_MIN, VOLUME_MAX)
    await _apply_and_reply(msg, f" Volume set: <b>{n}/1000</b>", volume=n, relay_volume=n)

@Client.on_message(HAS_USER & cmd_prefix(r"bass\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_bass(bot, msg: Message):
    n = _num_arg(msg)
    if n is None:
        await msg.reply_text("Usage: <code>/bass &lt;0-100&gt;</code>")
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
        if v < 0:
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

@Client.on_callback_query(filters.regex(r"^vc:"))
async def cb_vc(bot, cq):
    _, action, cid = cq.data.split(":")
    cid = int(cid)
    uvc = session_manager.users.get(cq.from_user.id)
    if not uvc:
        await safe_answer(cq, "Pehle login karein.", show_alert=True)
        return
    if action == "list":
        active = next((chat_id for chat_id, state in uvc.chats.items()
                       if state.is_playing), None)
        if active is None:
            await safe_answer(cq, "Kuch play nahi ho raha", show_alert=True)
            return
        cid = active
        action = "now"
    if action == "now":
        st = uvc.chats.get(cid)
        if not st or not st.is_playing:
            await safe_answer(cq, "Kuch play nahi ho raha", show_alert=True)
            return
        await edit_screen(cq.message,
            f"🎧 <b>NOW PLAYING</b>\n\n"
            f"🎵 {st.source_name}\n"
            f"{'⏸ Paused' if st.is_paused else '▶ Playing'} · "
            f"🔊 {st.relay_volume}/1000 · 💥 {st.boost}/10\n\n"
            "<i>Human Mode = hand raise + mic on/off blink, taaki account "
            "asli insaan jaisa lage.</i>",
            reply_markup=now_playing_kb(cid, st),
        )
        await safe_answer(cq, "Now Playing updated")
    elif action == "reset":
        await safe_answer(cq, " Reset apply ho raha hai…")
        from plugins.start import DEFAULT_SETTINGS, apply_settings_live
        await db.save_settings(cq.from_user.id, **DEFAULT_SETTINGS)
        await apply_settings_live(cq.from_user.id)
    elif action == "auto":
        await safe_answer(cq, " Auto apply ho raha hai…")
        from plugins.start import apply_settings_live
        s = await db.get_settings(cq.from_user.id)
        on = not bool(s.get("auto"))
        await db.save_settings(cq.from_user.id, auto=1 if on else 0,
                               **(AUTO_PRESET if on else {}))
        await apply_settings_live(cq.from_user.id)
    elif action == "human":
        st = uvc.state(cid)
        on = not (st.hand_raise or st.mic_blink)
        st.hand_raise = on
        st.mic_blink = on
        uvc.save_mute_prefs(cid)
        if on:
            try:
                await uvc.raise_hand(cid, True)
            except Exception:
                pass
        await safe_answer(cq, "🧑 Human Mode " + ("ON" if on else "OFF"))
    elif action == "ss":
        st = uvc.state(cid)
        want = not bool(getattr(st, "ss_on", False))
        ok = await uvc.set_screen_share(cid, want)
        await safe_answer(cq, ("🖥 Screen share " + ("ON" if want else "OFF")) if ok
                          else "Screen share nahi chal paya (VC me hai?)", show_alert=not ok)
    elif action == "pause":
        await safe_answer(cq, "⏸ Paused" if await uvc.pause(cid) else "Kuch chal nahi raha")
    elif action == "resume":
        await safe_answer(cq, "▶ Resumed" if await uvc.resume(cid) else "Paused nahi tha")
    elif action == "skip":
        await uvc.skip(cid)
        await safe_answer(cq, "⏭ Skipped")
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
            await safe_answer(cq, f" Mic blink {'ON' if st.mic_blink else 'OFF'} ({st.mic_blink_secs}s)")
        elif action == "mbnow":
            await safe_answer(cq, f" Blinking {st.mic_blink_secs}s…")
            asyncio.create_task(uvc._mic_blink(cid, st.mic_blink_secs))
        elif action == "pm":
            await safe_answer(cq, "PlayMute set hai ✅" if st.unmute_audio else
                              "Audio ko reply karke .playmute likhein", show_alert=True)
        elif action == "pmclr":
            st.unmute_audio = None
            uvc.save_mute_prefs(cid)
            await safe_answer(cq, " PlayMute cleared")
        elif action == "pmtest":
            ok = await uvc.play_unmute_audio(cid)
            await safe_answer(cq, " PlayMute chal raha" if ok else "PlayMute audio set nahi hai", show_alert=not ok)
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
            await safe_answer(cq, " Loop " + ("ON" if st.loop else "OFF"), show_alert=True)

AUTO_KB = K([[B(" Settings Panel", callback_data="menu:settings")]])

@Client.on_callback_query(filters.regex(r"^mic:"))
async def cb_mic(bot: Client, cq):
    from plugins.start import apply_settings_live

    uid = cq.from_user.id
    uvc = session_manager.users.get(uid)
    if not uvc:
        await safe_answer(cq, " Pehle login karein.", show_alert=True)
        return

    _, action, *rest = cq.data.split(":")

    if action == "noop":
        await safe_answer(cq)
        return

    if action == "panel":
        s = await db.get_settings(uid)
        active_cid = next((cid for cid, st in uvc.chats.items()
                           if st.mic_enabled), None)
        from helpers import vc_bridge
        mic_on = active_cid is not None or vc_bridge.get_bridge(uid) is not None
        title = (f"User {uvc.chats[active_cid].mic_boost_user_id}"
                 if mic_on and uvc.chats[active_cid].mic_boost_user_id else "")
        await edit_screen(cq.message, mic_text(s, mic_on, title),
                          reply_markup=mic_kb(mic_on, logged_in=True))
        await safe_answer(cq)
        return

    if action == "acct":
        sub = rest[0] if rest else ""
        if sub == "phone":
            await _spare_cancel(uid)
            _mic_login[uid] = {"stage": "phone"}
            await safe_answer(cq)
        elif sub == "string":
            await _spare_cancel(uid)
            _mic_login[uid] = {"stage": "string"}
            await safe_answer(cq)
        elif sub == "cancel":
            await _spare_cancel(uid)
            await safe_answer(cq, "Cancelled")
        elif sub == "remove":
            await _spare_cancel(uid)
            await _spare_remove(uid)
            await safe_answer(cq, "Spare account hata diya")
        else:
            await safe_answer(cq)
        text, kb = await spare_screen(uid)
        await edit_screen(cq.message, text, reply_markup=kb)
        return

    s = await db.get_settings(uid)
    active_cid = next((cid for cid, st in uvc.chats.items()
                       if st.mic_enabled), None)

    if action == "on":
        from plugins.vc_bridge import HELP as BRIDGE_HELP
        if not await session_manager.assistant_string(uid):
            text, kb = await spare_screen(uid)
            await edit_screen(cq.message, text, reply_markup=kb)
            await safe_answer(cq, "Pehle Spare ID login karein")
            return
        await edit_screen(cq.message,
            BRIDGE_HELP + "\n\n👉 Setup ke baad bot DM me <code>.mic on</code> bhejein.",
            reply_markup=K([[B("⬅ Back", callback_data="mic:panel")]]))
        await safe_answer(cq)
        return

    if action == "off":
        from helpers import vc_bridge
        from helpers.live_mic import reset_mic_state
        ok = await vc_bridge.stop_bridge(uid)
        try:
            await reset_mic_state(uvc, uid)
        except Exception:
            pass
        s = await db.get_settings(uid)
        await edit_screen(cq.message, mic_text(s, False, ""),
                          reply_markup=mic_kb(False, logged_in=True))
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
            reply_markup=mic_kb(active_cid is not None, logged_in=True))
        await safe_answer(cq, f"Mic gain: {s['live_volume']}/20000")
        return

    if action == "gain":
        delta = int(rest[0]) if rest else 0
        s["gain"] = max(0, min(400,
            int(s.get("gain", Config.RELAY_DEFAULT_GAIN)) + delta))
        await db.save_settings(uid, gain=s["gain"])
        await apply_settings_live(uid)
        await edit_screen(cq.message, mic_text(s, active_cid is not None,
            (f"User {uvc.chats[active_cid].mic_boost_user_id}"
             if active_cid and uvc.chats[active_cid].mic_boost_user_id else "")),
            reply_markup=mic_kb(active_cid is not None, logged_in=True))
        await safe_answer(cq, f"Gain: {s['gain']}/200")
        return

    if action == "bass":
        delta = int(rest[0]) if rest else 0
        s["bass"] = clamp(s["bass"] + delta, BASS_MIN, BASS_MAX)
        await db.save_settings(uid, bass=s["bass"])
        await apply_settings_live(uid)
        await edit_screen(cq.message, mic_text(s, active_cid is not None,
            (f"User {uvc.chats[active_cid].mic_boost_user_id}"
             if active_cid and uvc.chats[active_cid].mic_boost_user_id else "")),
            reply_markup=mic_kb(active_cid is not None, logged_in=True))
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
            reply_markup=mic_kb(active_cid is not None, logged_in=True))
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
            reply_markup=mic_kb(active_cid is not None, logged_in=True))
        await safe_answer(cq, f"Echo level: {s['echo_level']}/10")
        return

    if action == "echo":
        s["echo"] = 0 if s["echo"] else 1
        await db.save_settings(uid, echo=s["echo"])
        await apply_settings_live(uid)
        await edit_screen(cq.message, mic_text(s, active_cid is not None,
            (f"User {uvc.chats[active_cid].mic_boost_user_id}"
             if active_cid and uvc.chats[active_cid].mic_boost_user_id else "")),
            reply_markup=mic_kb(active_cid is not None, logged_in=True))
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
            reply_markup=mic_kb(active_cid is not None, logged_in=True))
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
            reply_markup=mic_kb(active_cid is not None, logged_in=True))
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
        await msg.reply_text(" <b>AUTO MODE OFF</b> — manual control wapas.",
                             reply_markup=AUTO_KB)
        return

    await msg.reply_text(
        " <b>AUTO MODE ON</b> — ab sab automatic hai \n\n"
        f" Volume <code>{VOLUME_MAX}/1000</code> (max)\n"
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
        await msg.reply_text(f" <b>Log channel kaam nahi kar raha</b>\n\n{problem}")
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
        set_channel(int(parts[1]))
    except ValueError:
        await msg.reply_text(" Channel ID number honi chahiye (<code>-100…</code>).")
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
        await msg.reply_text("Usage: <code>/volume &lt;0-1000&gt;</code>")
        return
    n = max(0, min(VOLUME_MAX, n))
    await _apply_and_reply(msg, f" Playback volume: <b>{n}/1000</b>", relay_volume=n, volume=n)

@Client.on_message(HAS_USER & cmd_prefix(r"gain\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_gain(bot: Client, msg: Message):
    n = _relay_num_arg(msg)
    if n is None:
        await msg.reply_text("Usage: <code>/gain &lt;0-200&gt;</code>")
        return
    n = max(0, min(400, n))
    await _apply_and_reply(msg, f" Gain: <b>{n}/400</b>", gain=n)

@Client.on_message(HAS_USER & cmd_prefix(r"treble\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_treble(bot: Client, msg: Message):
    n = _relay_num_arg(msg)
    if n is None:
        await msg.reply_text("Usage: <code>/treble &lt;0-100&gt;</code>")
        return
    n = max(0, min(100, n))
    await _apply_and_reply(msg, f" Treble: <b>{n}/100</b>", treble=n)

@Client.on_message(HAS_USER & cmd_prefix(r"voice\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_voice(bot: Client, msg: Message):
    parts = cmd_text(msg).split()
    if len(parts) < 2 or parts[1].lower() not in {"female", "male", "normal"}:
        await msg.reply_text(
            "Usage: <code>/voice female|male|normal</code>\n\n"
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
        f"├ Volume: <code>{s.get('relay_volume', Config.RELAY_DEFAULT_VOLUME)}/1000</code>\n"
        f"├ Gain: <code>{s.get('gain', Config.RELAY_DEFAULT_GAIN)}/200</code>\n"
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
    "🎤 <b>Spare Mic Account</b>\n\n"
    "Live mic ke liye <b>2 account</b> chahiye:\n"
    "├ aapka account — VC me baitha rahega\n"
    "└ spare account — isse aawaz VC me jayegi\n\n"
    "Telegram ek account ko ek hi VC me rehne deta hai, isliye spare ke bina "
    "aap khud VC se bahar fek diye jate hain.\n\n"
)


def spare_setup_kb(pending: bool = False) -> K:
    if pending:
        return K([
            [B("❌ Cancel", callback_data="mic:acct:cancel")],
            [B("⬅ Back", callback_data="mic:panel")],
        ])
    return K([
        [B("📱 Phone se login (easy)", callback_data="mic:acct:phone")],
        [B("🔑 Session string se", callback_data="mic:acct:string")],
        [B("⬅ Back", callback_data="mic:panel")],
    ])


def spare_ready_kb() -> K:
    return K([
        [B("♻ Dusra account lagayein", callback_data="mic:acct:phone")],
        [B("🗑 Spare account hatayein", callback_data="mic:acct:remove")],
        [B("⬅ Back", callback_data="mic:panel")],
    ])


async def spare_screen(uid: int):
    """(text, keyboard) for the spare-account screen."""
    from plugins.ui import GEN_NAME
    st = _mic_login.get(uid)
    if st:
        stage = st.get("stage")
        if stage == "phone":
            return (SPARE_WHY +
                    "📱 <b>Spare account ka number bhejein</b> — country code ke saath, "
                    "jaise <code>+919876543210</code>.\n"
                    "Bas yahin normal message me likh dein.\n\n"
                    "<i>Aapka message turant delete ho jayega.</i>",
                    spare_setup_kb(True))
        if stage == "code":
            return (SPARE_WHY +
                    "📩 <b>OTP</b> us account ke Telegram par bhej diya hai.\n"
                    "Ab OTP yahin bhej dein, jaise <code>12345</code>.\n\n"
                    "<i>Message auto-delete ho jayega.</i>",
                    spare_setup_kb(True))
        if stage == "string":
            return (SPARE_WHY +
                    "🔑 Spare account ka pura <b>Pyrogram session string</b> bhejein.\n"
                    f"String {GEN_NAME} se bana sakte hain — ya aasan tarika: "
                    "Cancel karke <b>📱 Phone se login</b> use karein.",
                    spare_setup_kb(True))
        return (SPARE_WHY +
                "🔐 Is account par <b>2-Step Verification</b> ON hai — uska password "
                "yahin bhejein.\n\n<i>Message auto-delete ho jayega.</i>",
                spare_setup_kb(True))

    current = await session_manager.assistant_string(uid)
    if not current:
        return (SPARE_WHY +
                "Niche <b>📱 Phone se login</b> dabayein — sirf number aur OTP lagega. "
                "Koi session string dhoondhne ki zaroorat nahi.",
                spare_setup_kb())
    relay = await session_manager.get_relay(uid)
    if not relay:
        return (SPARE_WHY +
                "⚠️ Spare account set hai par uska login fail ho raha hai. "
                "Phone se dobara login karein ya hata dein.",
                spare_ready_kb())
    return (
        "🎤 <b>Spare Mic Account</b> — ✅ ready\n\n"
        f"├ Name: <code>{relay.account_name}</code>\n"
        f"└ ID: <code>{relay.account_id}</code>\n\n"
        "Live mic isi account se VC me jayega aur aap VC me hi rahenge.\n"
        "<i>Dhyan: is spare account ko us group me add karein.</i>",
        spare_ready_kb())


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


async def _spare_save(uid: int, session_str: str, account_id: int = None):
    """Validate + save a spare session. Returns (ok, message)."""
    session_str = (session_str or "").strip()
    if account_id is not None and account_id == uid:
        return False, ("❌ Ye aapka hi account hai. Spare mic account <b>alag</b> hona "
                       "chahiye, warna wahi purani problem rahegi.")
    own = await db.get_user(uid)
    if own and (own.get("string_session") or "").strip() == session_str:
        return False, ("❌ Ye wahi account hai jisse aap login hain. Spare mic account "
                       "<b>alag</b> account ka hona chahiye.")
    await db.set_app_value(f"assistant_session_{uid}", session_str)
    await session_manager.drop_relay(uid)
    relay = await session_manager.get_relay(uid)
    if not relay:
        await db.delete_app_value(f"assistant_session_{uid}")
        return False, "❌ Account save hua par connect nahi ho paya. Dobara try karein."
    if relay.account_id == uid:
        await _spare_remove(uid)
        return False, "❌ Ye aapka hi account hai. Spare mic account alag hona chahiye."
    return True, ("✅ <b>Spare mic account set!</b>\n\n"
                  f"├ Name: <code>{relay.account_name}</code>\n"
                  f"└ ID: <code>{relay.account_id}</code>\n\n"
                  "Ab 🎤 <b>Mic ON</b> par aawaz is account se VC me jayegi aur aapka "
                  "account VC me hi rahega.\n\n"
                  "<i>Zaroori: is spare account ko us group me add karein.</i>")


async def _spare_send_code(uid: int, phone: str):
    if not Config.API_ID or not Config.API_HASH:
        return False, "❌ Bot me API_ID/API_HASH set nahi hai — owner se bolein."
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
    _mic_login[uid] = {"client": client, "phone": phone,
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
            ok, message = await _spare_save(uid, text)
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
    await _spare_cancel(uid)
    return await _spare_save(uid, session_str, account_id=me.id)


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
    kb = spare_ready_kb() if done else spare_setup_kb(uid in _mic_login)
    await bot.send_message(msg.chat.id, reply, reply_markup=kb,
                           disable_web_page_preview=True)


@Client.on_message(HAS_USER & cmd_prefix(r"micaccount\b", flags=re.IGNORECASE) & filters.private)
async def cmd_micaccount(bot: Client, msg: Message):
    """Spare account setup — number/OTP se, ya session string se."""
    uid = msg.from_user.id
    parts = cmd_text(msg).split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""

    if arg.lower() in {"off", "remove", "clear", "delete"}:
        await _spare_cancel(uid)
        await _spare_remove(uid)
        await msg.reply_text(
            "🗑 <b>Spare mic account hata diya.</b>\n\n"
            "Ab live mic aapke apne account se jayega — dhyan rahe, us waqt "
            "aapka account phone wali VC se bahar ho jayega.")
        return

    if arg.lower() == "cancel":
        await _spare_cancel(uid)
        await msg.reply_text("❎ Mic account login cancel kar diya.")
        return

    if arg and arg.lower() != "status":
        try:
            await msg.delete()
        except Exception:
            pass
        reply, done = await _spare_handle_text(uid, arg)
        kb = spare_ready_kb() if done else spare_setup_kb(uid in _mic_login)
        await bot.send_message(msg.chat.id, reply, reply_markup=kb,
                               disable_web_page_preview=True)
        return

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
            "🪑 <b>Spare ID in VC</b>\n\n"
            "• <code>.spare join</code> — spare ID VC me baith jaye (mic OFF)\n"
            "• <code>.spare join -100xxx</code> — kisi aur group me\n"
            "• <code>.spare mute</code> / <code>.spare unmute</code> — TG mic icon\n"
            "• <code>.spare leave</code> — spare ID VC se nikal jaye\n"
            "   (live mic chalu ho tab bhi chalega — mic band + VC exit)\n\n"
            "<code>.mic off</code> karne par bhi spare ID VC me baithi rehti hai.")
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
        await msg.reply_text(("🔇 Spare mic icon OFF" if action == "mute" else "🎙 Spare mic icon ON") if ok
                             else "❌ Spare ID VC me nahi hai. Pehle <code>.spare join</code>.")
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
            "👋 <b>Spare ID VC se nikal gayi</b> aur live mic band ho gaya: "
            f"<code>{cid}</code>")
        return
    if is_active(uid):
        await msg.reply_text("Live mic chal raha hai — spare ID pehle se VC me hai. Pehle <code>.mic off</code>.")
        return
    if action in {"join", "sit"}:
        ok = await park_relay(relay, cid)
        await msg.reply_text(
            f"🪑 Spare ID VC me baith gayi (mic OFF): <code>{cid}</code>" if ok else
            "❌ Join nahi hua. Check: group me VC chalu hai aur spare ID group ki member hai.")
    else:
        await unpark_relay(relay, cid)
        await msg.reply_text(f"👋 Spare ID VC se nikal gayi: <code>{cid}</code>")


@Client.on_message(HAS_USER & cmd_prefix(r"mic\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_mic(bot: Client, msg: Message):
    """Live Mic = VC Bridge (browser mic page aur direct live hata diye)."""
    parts = cmd_text(msg).split()
    action = parts[1].lower() if len(parts) > 1 else "help"

    if action in {"chat", "setchat", "target"}:
        if not await get_engine(msg):
            return
        # Save a default group so `.mic on` works from bot DM — nothing is
        # ever typed in the group, other members won't know.
        if len(parts) < 3:
            saved = (await db.get_app_value(f"mic_chat_{msg.from_user.id}"))
            await mic_notify(msg, 
                "🎯 <b>Mic target group</b>: "
                + (f"<code>{saved}</code>" if saved else "set nahi hai") +
                "\n\nSet karne ke liye (bot DM me): <code>.mic chat -100xxxxxxxx</code>\n"
                "Username / invite link bhi chalega.")
            return
        cid, _ = await target_chat(msg, parts[2])
        if not cid:
            await mic_notify(msg, "❌ Group nahi mila. Chat ID / @username / invite link check karein.")
            return
        await db.set_app_value(f"mic_chat_{msg.from_user.id}", str(cid))
        await mic_notify(msg, 
            f"✅ Mic target set: <code>{cid}</code>\n\nAb bot DM me sirf "
            "<code>.mic on</code> bhejo — aawaz isi group ki VC me jayegi.")
        return

    from plugins.vc_bridge import run_bridge
    alias = {"start": "on", "stop": "off", "exit": "leave", "out": "leave",
             "vcleave": "leave", "devices": "help", "list": "help",
             "source": "src", "power": "loud", "boost": "loud"}
    action = alias.get(action, action)
    if action not in {"on", "off", "leave", "status", "src", "help", "loud"}:
        action = "help"
    await run_bridge(msg, ["bridge", action, *parts[2:]])


@Client.on_message(HAS_USER & cmd_prefix(r"(setgc|unsetgc|gc)\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_setgc(bot: Client, msg: Message):
    """Set a default group once; every command then targets it automatically."""
    uid = msg.from_user.id
    parts = cmd_text(msg).split()
    name = parts[0].lstrip("./!").lower()
    if name == "unsetgc":
        DEFAULT_GC.pop(uid, None)
        _save_gc()
        await msg.reply_text(" Default group hata diya.")
        return
    if name == "gc" or (name == "setgc" and len(parts) == 1 and not (msg.chat and msg.chat.id < 0)):
        cur = DEFAULT_GC.get(uid)
        await msg.reply_text(
            f" <b>Default group:</b> <code>{cur or 'none'}</code>\n"
            "Set: group me <code>.setgc</code> ya DM me <code>.setgc -100xxxx / @username / link</code>")
        return
    arg = parts[1] if len(parts) > 1 else None
    DEFAULT_GC.pop(uid, None)  # so target_chat resolves the given/current chat
    cid, _ = await target_chat(msg, arg)
    if not cid:
        await msg.reply_text(" Group resolve nahi hua. Chat ID / @username / invite link dein.")
        return
    DEFAULT_GC[uid] = cid
    _save_gc()
    await msg.reply_text(
        f"✅ <b>Default group set:</b> <code>{cid}</code>\n"
        "Ab saari commands (play, handraise, micblink, playmute…) bina chat ID ke isi group pe lagengi.\n"
        "Hatane ke liye: <code>.unsetgc</code>")


# ----------------------------------------------------------------------
# VC CHAT + EMOJI REACTIONS — logged-in ID voice chat ke chat panel me
# message / floating emoji reaction bhejti hai.
# ----------------------------------------------------------------------
_VC_REACT_DEFAULT = "❤️"
_VC_REACT_MAX = 10


def _split_chat_arg(parts):
    """Last token may be a chat id / @username / invite link."""
    if parts and (parts[-1].lstrip("-").isdigit() and parts[-1].startswith("-")
                  or parts[-1].startswith("@") or "t.me/" in parts[-1]):
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
