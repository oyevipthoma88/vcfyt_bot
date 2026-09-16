
import os
from urllib.parse import quote, urlsplit, urlunsplit

from pyrogram import Client, filters
from plugins.ui import B, edit_screen, safe_answer, mic_text, mic_kb
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
    if isinstance(error, PeerAccessError):
        return f"❌ <b>Chat access nahi mila</b>\n\n{PEER_HELP}"
    if is_peer_error(error):
        return f"❌ <b>Chat access nahi mila</b>\n\n{PEER_HELP}"
    name = type(error).__name__
    text = str(error)
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

def now_playing_kb(cid: int) -> K:
    return K([
        [B(" Now Playing", callback_data=f"vc:now:{cid}")],
        [B("⏸ Pause", callback_data=f"vc:pause:{cid}"),
         B("▶ Resume", callback_data=f"vc:resume:{cid}"),
         B("⏭ Skip", callback_data=f"vc:skip:{cid}")],
        [B(" Loop", callback_data=f"vc:loop:{cid}")],
        [B(" Reset Audio", callback_data=f"vc:reset:{cid}"),
         B(" Auto", callback_data=f"vc:auto:{cid}")],
        [B("⏹ Stop", callback_data=f"vc:stop:{cid}"),
         B(" Settings", callback_data="menu:settings")],
    ])

async def get_engine(msg: Message):
    uvc = await session_manager.get(msg.from_user.id)
    if not uvc:
        await msg.reply_text(
            " <b>Pehle login karein.</b>\n\n"
            "Bot ke DM mein jaakar  Login   Phone se Login, "
            "ya apna string session add karein.",
            reply_markup=LOGIN_KB,
        )
    return uvc

async def target_chat(msg: Message, arg: str = None) -> int:
    if arg:
        try:
            return int(arg)
        except ValueError:
            try:
                chat = await msg._client.get_chat(arg)
                return chat.id
            except Exception:
                return 0
    if msg.chat and msg.chat.id < 0:
        return msg.chat.id
    return 0

async def need_chat(msg: Message, arg: str = None) -> int:
    cid = await target_chat(msg, arg)
    if not cid:
        await msg.reply_text(
            " Voice chat sirf <b>groups</b> mein hota hai.\n"
            "Group mein command chalayein, ya group ka chat ID dein:\n"
            "<code>.play &lt;source&gt; -1001234567890</code>"
        )
        return cid
    await db.register_broadcast_chat(cid)
    return cid

async def load_state_settings(user_id: int, uvc, chat_id: int):
    s = await db.get_settings(user_id)
    st = uvc.state(chat_id)
    st.apply_settings(s)
    if bool(s.get("auto")) != bool(st.auto):
        await uvc.set_auto(chat_id, bool(s.get("auto")))
    return st

@Client.on_message(filters.regex(r"^[./]tag\b") & (filters.group | filters.private))
async def cmd_tag(bot: Client, msg: Message):
    parts = msg.text.strip().split(maxsplit=1)
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

@Client.on_message(filters.regex(r"^[./]untag\b") & (filters.group | filters.private))
async def cmd_untag(bot: Client, msg: Message):
    parts = msg.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply_text("Usage: <code>.untag &lt;name&gt;</code>")
        return
    name = parts[1].strip().lower()
    if not await db.get_tag(msg.from_user.id, name):
        await msg.reply_text(f" Tag <code>{name}</code> nahi mila.")
        return
    await db.delete_tag(msg.from_user.id, name)
    await msg.reply_text(f" <code>{name}</code> delete ho gaya.")

@Client.on_message(filters.regex(r"^[./]tags\b") & (filters.group | filters.private))
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
    parts = msg.text.strip().split()
    words, cid = [], None
    for p in parts[1:]:
        try:
            if int(p) < 0:
                cid = p
                continue
        except ValueError:
            pass
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
    cid = await need_chat(msg, cid_arg)
    if not cid:
        return

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
        status = await uvc.play(cid, path, name, title, enqueue=enqueue)
    except Exception as e:
        _cleanup_source(path)
        await stat.edit_text(friendly_error(e))
        await log_error("cmd_play", e)
        return
    await _archive_played_audio(bot, source_file_id, name)

    await stat.edit_text(
        f"{' <b>Queued!</b>' if status == 'queued' else '▶ <b>Playing!</b>'}\n\n"
        f" <b>Source:</b> {name}\n"
        f" <b>Chat:</b> {title}\n"
        f" <b>Account:</b> {uvc.account_name}\n"
        f" <b>Volume:</b> {st.relay_volume}/1000 |  <b>Bass:</b> +{st.bass} dB\n"
        f" <b>Boost:</b> {st.boost}/10 |  <b>Echo:</b> "
        f"{'On' if st.echo else 'Off'} {st.echo_level}/10",
        reply_markup=now_playing_kb(cid),
    )

@Client.on_message(filters.regex(r"^[./]play(?!force)\b") & (filters.group | filters.private))
async def cmd_play(bot: Client, msg: Message):
    await _play(bot, msg, enqueue=False)

@Client.on_message(filters.regex(r"^[./](playforce|fplay)\b") & (filters.group | filters.private))
async def cmd_playforce(bot: Client, msg: Message):
    await log_command(msg.from_user.id, msg.from_user.username, msg.chat.id,
                      ".playforce")
    uvc = await get_engine(msg)
    if not uvc:
        return
    if not await _check_usage_access(msg):
        return
    source_arg, cid_arg = _split_args(msg)
    cid = await need_chat(msg, cid_arg)
    if not cid:
        return
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
        await uvc.force_play(cid, path, name, title)
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
        reply_markup=now_playing_kb(cid),
    )

@Client.on_message(filters.regex(r"^[./]loop\b") & (filters.group | filters.private))
async def cmd_loop(bot: Client, msg: Message):
    await log_command(msg.from_user.id, msg.from_user.username, msg.chat.id, ".loop")
    uvc = await get_engine(msg)
    if not uvc:
        return
    parts = msg.text.strip().split()
    arg = parts[1].lower() if len(parts) > 1 else "on"
    cid_arg = None
    for p in parts[1:]:
        if p.startswith("-") and p[1:].isdigit():
            cid_arg = p
    cid = await need_chat(msg, cid_arg)
    if not cid:
        return
    st = uvc.chats.get(cid)
    if not st:
        await msg.reply_text(" Is chat me kuch chal nahi raha. Pehle <code>.play</code> karein.")
        return
    if arg in ("off", "0", "no", "band", "stop"):
        st.loop, st.loop_left = False, -1
        await msg.reply_text(" <b>Loop OFF</b>")
        return
    count = -1
    if arg.isdigit():
        count = max(1, int(arg))
    st.loop, st.loop_left = True, count
    await msg.reply_text(
        " <b>Loop ON</b> — " + ("infinite (jab tak <code>.loop off</code> na karein)"
                                  if count < 0 else f"{count} baar aur")
    )

@Client.on_message(filters.regex(r"^[./]padd\b") & (filters.group | filters.private))
async def cmd_padd(bot: Client, msg: Message):
    await _play(bot, msg, enqueue=True)

async def _transport(msg: Message, action: str):
    uvc = await get_engine(msg)
    if not uvc:
        return
    parts = msg.text.strip().split()
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

@Client.on_message(filters.regex(r"^[./]pause\b") & (filters.group | filters.private))
async def cmd_pause(bot, msg):
    await _transport(msg, "pause")

@Client.on_message(filters.regex(r"^[./]resume\b") & (filters.group | filters.private))
async def cmd_resume(bot, msg):
    await _transport(msg, "resume")

@Client.on_message(filters.regex(r"^[./]skip\b") & (filters.group | filters.private))
async def cmd_skip(bot, msg):
    await _transport(msg, "skip")

@Client.on_message(filters.regex(r"^[./](stop|end|leave)\b") & (filters.group | filters.private))
async def cmd_stop(bot, msg):
    await _transport(msg, "stop")

@Client.on_message(filters.regex(r"^[./]queue\b") & (filters.group | filters.private))
async def cmd_queue(bot: Client, msg: Message):
    uvc = await get_engine(msg)
    if not uvc:
        return
    parts = msg.text.strip().split()
    cid = await need_chat(msg, parts[1] if len(parts) > 1 else None)
    if not cid:
        return
    st = uvc.chats.get(cid)
    if not st:
        await msg.reply_text(" Is chat mein koi active VC session nahi.")
        return
    lines = [f"{i+1}. {n}" for i, (_, n) in enumerate(st.queue)] or ["— empty —"]
    await msg.reply_text(
        f" <b>Now:</b> {st.source_name}\n <b>Queue:</b>\n" + "\n".join(lines))

@Client.on_message(filters.regex(r"^[./]vcinfo\b") & (filters.group | filters.private))
async def cmd_vcinfo(bot: Client, msg: Message):
    uvc = await get_engine(msg)
    if not uvc:
        return
    parts = msg.text.strip().split()
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
    parts = msg.text.strip().split()
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None

@Client.on_message(filters.regex(r"^[./]vol\b") & (filters.group | filters.private))
async def cmd_vol(bot, msg: Message):
    n = _num_arg(msg)
    if n is None:
        await msg.reply_text("Usage: <code>.vol &lt;0-1000&gt;</code>")
        return
    n = clamp(n, VOLUME_MIN, VOLUME_MAX)
    await _apply_and_reply(msg, f" Volume set: <b>{n}/1000</b>", volume=n, relay_volume=n)

@Client.on_message(filters.regex(r"^[./]bass\b") & (filters.group | filters.private))
async def cmd_bass(bot, msg: Message):
    n = _num_arg(msg)
    if n is None:
        await msg.reply_text("Usage: <code>/bass &lt;0-100&gt;</code>")
        return
    await _apply_and_reply(msg, f" Bass set: <b>+{clamp(n, BASS_MIN, BASS_MAX)} dB</b>",
                           bass=clamp(n, BASS_MIN, BASS_MAX))

@Client.on_message(filters.regex(r"^[./]boost\b") & (filters.group | filters.private))
async def cmd_boost(bot, msg: Message):
    n = _num_arg(msg)
    if n is None:
        await msg.reply_text(
            "Usage: <code>.boost &lt;0-10&gt;</code> (audio loudness)\n"
            "Live mic ke liye: <code>.myboost</code>")
        return
    await _apply_and_reply(msg, f" Boost set: <b>{clamp(n, LEVEL_MIN, LEVEL_MAX)}/10</b>",
                           boost=clamp(n, LEVEL_MIN, LEVEL_MAX))

@Client.on_message(filters.regex(r"^[./]echolvl\b") & (filters.group | filters.private))
async def cmd_echolvl(bot, msg: Message):
    n = _num_arg(msg)
    if n is None:
        await msg.reply_text("Usage: <code>.echolvl &lt;0-10&gt;</code>")
        return
    lvl = clamp(n, LEVEL_MIN, LEVEL_MAX)
    await _apply_and_reply(msg, f" Echo level: <b>{lvl}/10</b>",
                           echo_level=lvl, echo=1 if lvl else 0)

@Client.on_message(filters.regex(r"^[./]echo\b") & (filters.group | filters.private))
async def cmd_echo(bot, msg: Message):
    parts = msg.text.strip().split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        await msg.reply_text("Usage: <code>.echo on|off</code>")
        return
    on = parts[1].lower() == "on"
    await _apply_and_reply(msg, f" Echo: <b>{'ON' if on else 'OFF'}</b>",
                           echo=1 if on else 0)

@Client.on_message(filters.regex(r"^[./](max|ultra)\b") & (filters.group | filters.private))
async def cmd_max(bot, msg: Message):

    await _apply_and_reply(msg, " <b>MAXIMUM LOUD MODE</b> — sab knobs max par.",
                           auto=1, **AUTO_PRESET)

@Client.on_message(filters.regex(r"^[./]reset\b") & (filters.group | filters.private))
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

@Client.on_message(filters.regex(r"^[./](myboost|livegain|livevolume)\b") & (filters.group | filters.private))
async def cmd_myboost(bot: Client, msg: Message):
    uvc = await get_engine(msg)
    if not uvc:
        return
    parts = msg.text.strip().split()
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
            f" <b>NOW PLAYING</b>\n\n"
            f" <b>Track:</b> {st.source_name}\n"
            f" <b>Volume:</b> {st.relay_volume}/1000\n"
            f" <b>Gain:</b> {st.gain}/200\n"
            f" <b>Boost:</b> {st.boost}/10\n"
            f" <b>Bass:</b> {st.bass} |  <b>Treble:</b> {st.treble}\n"
            f" <b>Auto:</b> {'ON' if st.auto else 'OFF'}",
            reply_markup=now_playing_kb(cid),
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
    elif action == "loop":
        st = uvc.chats.get(cid)
        if not st:
            await safe_answer(cq, "Kuch chal nahi raha", show_alert=True)
        else:
            st.loop = not st.loop
            st.loop_left = -1
            await safe_answer(cq, " Loop " + ("ON" if st.loop else "OFF"), show_alert=True)

AUTO_KB = K([[B(" Settings Panel", callback_data="menu:settings")]])

def _relay_url() -> str:
    if not Config.MIC_RELAY_ENABLED or not Config.MIC_RELAY_TOKEN:
        return ""
    base = Config.MIC_RELAY_PUBLIC_URL or (
        f"https://{Config.HEROKU_APP_NAME}.herokuapp.com"
        if Config.HEROKU_APP_NAME else ""
    )
    if not base:
        return ""
    if "://" not in base:
        base = f"https://{base}"
    parsed = urlsplit(base)
    path = parsed.path.rstrip("/")
    if path != "/mic":
        path = f"{path}/mic" if path else "/mic"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "token=" + quote(Config.MIC_RELAY_TOKEN, safe=""), ""))

@Client.on_callback_query(filters.regex(r"^mic:"))
async def cb_mic(bot: Client, cq):
    from plugins.ui import mic_text, mic_kb
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
        mic_on = active_cid is not None
        title = uvc.chats[active_cid].source_name if mic_on else ""
        await edit_screen(cq.message, mic_text(s, mic_on, title),
                          reply_markup=mic_kb(mic_on, logged_in=True, relay_url=_relay_url()))
        await safe_answer(cq)
        return

    s = await db.get_settings(uid)
    active_cid = next((cid for cid, st in uvc.chats.items()
                       if st.mic_enabled), None)

    if action == "on":
        if active_cid is not None:
            await safe_answer(cq, " Mic already ON hai.", show_alert=True)
            return
        await safe_answer(cq, " Mic start ho raha hai…")
        target_cid = active_cid or next(
            (cid for cid, st in uvc.chats.items() if st.is_playing), None)
        if not target_cid:
            target_cid = next((cid for cid in uvc.chats), None)
        if not target_cid:
            await edit_screen(cq.message,
                "🎤 <b>Live Mic</b>\n\n"
                f"{LINE}\n"
                "Mic on karne ke liye pehle bot ka account kisi group ke VC mein hona chahiye.\n\n"
                "<b>Quick steps:</b>\n"
                "1. Jis group mein VC chalana hai, wahan logged-in account add karein\n"
                "2. Group mein Voice Chat start karein\n"
                "3. Group mein <code>.play</code> (kisi audio ko reply karke) bhejein\n"
                "4. Wapas yahan aa kar Mic ON dabayein\n\n"
                "Niche buttons se tutorial dekhein ya VC commands sikhein.",
                reply_markup=K([
                    [B("📘 Tutorial", callback_data="tut:livemic"),
                     B("▶ VC Commands", callback_data="tut:play")],
                    [B("⬅ Home", callback_data="menu:home")],
                ]))
            return
        try:
            title = await uvc.play_microphone(target_cid)
            s = await db.get_settings(uid)
            await edit_screen(cq.message,
                mic_text(s, True, title),
                reply_markup=mic_kb(True, logged_in=True, relay_url=_relay_url()))
            await safe_answer(cq, "🎤 Mic ON — max boost ke saath!")
            if Config.MIC_RELAY_ENABLED and Config.MIC_RELAY_TOKEN:
                relay_url = _relay_url()
                if relay_url:
                    try:
                        await bot.send_message(uid,
                            "🎤 <b>Live Mic ON!</b>\n\n"
                            "Apne phone ka mic use karne ke liye:\n"
                            "1. Niche link ko <b>Chrome</b> mein kholein\n"
                            "2. 'Start Live Mic' dabayein\n"
                            "3. Mic permission 'Allow' karein\n"
                            "4. Bolna shuru karein — aawaz VC mein max boost ke saath!\n\n"
                            f"<a href=\"{relay_url}\">📱 Mic Page Kholo</a>",
                            disable_web_page_preview=False)
                    except Exception:
                        pass
        except Exception as exc:
            await safe_answer(cq, f" Mic fail: {exc}", show_alert=True)
        return

    if action == "off":
        if not active_cid:
            await safe_answer(cq, " Mic on nahi hai.", show_alert=True)
            return
        try:
            await uvc.leave(active_cid, reason="Mic stopped from panel")
        except Exception:
            pass
        s = await db.get_settings(uid)
        await edit_screen(cq.message, mic_text(s, False, ""),
                          reply_markup=mic_kb(False, logged_in=True, relay_url=_relay_url()))
        await safe_answer(cq, "⏹ Mic OFF")
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
            await uvc.set_participant_volume(active_cid, uvc.account_id,
                                             s["live_volume"], quiet=True)
        await edit_screen(cq.message, mic_text(s, active_cid is not None,
            uvc.chats[active_cid].source_name if active_cid else ""),
            reply_markup=mic_kb(active_cid is not None, logged_in=True, relay_url=_relay_url()))
        await safe_answer(cq, f"Mic gain: {s['live_volume']}/20000")
        return

    if action == "gain":
        delta = int(rest[0]) if rest else 0
        s["gain"] = max(0, min(200,
            int(s.get("gain", Config.RELAY_DEFAULT_GAIN)) + delta))
        await db.save_settings(uid, gain=s["gain"])
        await apply_settings_live(uid)
        await edit_screen(cq.message, mic_text(s, active_cid is not None,
            uvc.chats[active_cid].source_name if active_cid else ""),
            reply_markup=mic_kb(active_cid is not None, logged_in=True, relay_url=_relay_url()))
        await safe_answer(cq, f"Gain: {s['gain']}/200")
        return

    if action == "bass":
        delta = int(rest[0]) if rest else 0
        s["bass"] = clamp(s["bass"] + delta, BASS_MIN, BASS_MAX)
        await db.save_settings(uid, bass=s["bass"])
        await apply_settings_live(uid)
        await edit_screen(cq.message, mic_text(s, active_cid is not None,
            uvc.chats[active_cid].source_name if active_cid else ""),
            reply_markup=mic_kb(active_cid is not None, logged_in=True, relay_url=_relay_url()))
        await safe_answer(cq, f"Bass: +{s['bass']} dB")
        return

    if action == "boost":
        delta = int(rest[0]) if rest else 0
        s["boost"] = clamp(s["boost"] + delta, LEVEL_MIN, LEVEL_MAX)
        await db.save_settings(uid, boost=s["boost"])
        await apply_settings_live(uid)
        await edit_screen(cq.message, mic_text(s, active_cid is not None,
            uvc.chats[active_cid].source_name if active_cid else ""),
            reply_markup=mic_kb(active_cid is not None, logged_in=True, relay_url=_relay_url()))
        await safe_answer(cq, f"Boost: {s['boost']}/10")
        return

    if action == "echolvl":
        delta = int(rest[0]) if rest else 0
        s["echo_level"] = clamp(s["echo_level"] + delta, LEVEL_MIN, LEVEL_MAX)
        s["echo"] = 1 if s["echo_level"] > 0 else s["echo"]
        await db.save_settings(uid, echo_level=s["echo_level"], echo=s["echo"])
        await apply_settings_live(uid)
        await edit_screen(cq.message, mic_text(s, active_cid is not None,
            uvc.chats[active_cid].source_name if active_cid else ""),
            reply_markup=mic_kb(active_cid is not None, logged_in=True, relay_url=_relay_url()))
        await safe_answer(cq, f"Echo level: {s['echo_level']}/10")
        return

    if action == "echo":
        s["echo"] = 0 if s["echo"] else 1
        await db.save_settings(uid, echo=s["echo"])
        await apply_settings_live(uid)
        await edit_screen(cq.message, mic_text(s, active_cid is not None,
            uvc.chats[active_cid].source_name if active_cid else ""),
            reply_markup=mic_kb(active_cid is not None, logged_in=True, relay_url=_relay_url()))
        await safe_answer(cq, f"Echo: {'ON' if s['echo'] else 'OFF'}")
        return

    if action == "max":
        s.update(AUTO_PRESET, auto=1, live_volume=20000)
        await db.save_settings(uid, **s)
        await apply_settings_live(uid)
        if active_cid:
            await uvc.set_participant_volume(active_cid, uvc.account_id,
                                             20000, quiet=True)
        await edit_screen(cq.message, mic_text(s, active_cid is not None,
            uvc.chats[active_cid].source_name if active_cid else ""),
            reply_markup=mic_kb(active_cid is not None, logged_in=True, relay_url=_relay_url()))
        await safe_answer(cq, "⚡ MAX ALL — sab max par!")
        return

    if action == "apply":
        await apply_settings_live(uid)
        if active_cid:
            st = uvc.chats.get(active_cid)
            if st and st.mic_enabled:
                await uvc.play_microphone(active_cid)
            await uvc.set_participant_volume(active_cid, uvc.account_id,
                s.get("live_volume", Config.LIVE_BOOST_DEFAULT), quiet=True)
        await edit_screen(cq.message, mic_text(s, active_cid is not None,
            uvc.chats[active_cid].source_name if active_cid else ""),
            reply_markup=mic_kb(active_cid is not None, logged_in=True, relay_url=_relay_url()))
        await safe_answer(cq, "✅ Mic par apply ho gaya!")
        return

    await safe_answer(cq)

@Client.on_message(filters.regex(r"^[./]auto\b") & (filters.group | filters.private))
async def cmd_auto(bot: Client, msg: Message):
    from plugins.start import apply_settings_live
    await log_command(msg.from_user.id, msg.from_user.username, msg.chat.id, ".auto")
    parts = msg.text.strip().split()
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

@Client.on_message(filters.regex(r"^[./]logtest\b") & filters.private)
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

@Client.on_message(filters.regex(r"^[./]setlog\b") & filters.private)
async def cmd_setlog(bot: Client, msg: Message):
    if not Config.is_owner(msg.from_user.id):
        return
    parts = msg.text.strip().split()
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
    parts = msg.text.strip().split()
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None

@Client.on_message(filters.regex(r"^[./]volume\b") & (filters.group | filters.private))
async def cmd_volume(bot: Client, msg: Message):
    n = _relay_num_arg(msg)
    if n is None:
        await msg.reply_text("Usage: <code>/volume &lt;0-1000&gt;</code>")
        return
    n = max(0, min(VOLUME_MAX, n))
    await _apply_and_reply(msg, f" Playback volume: <b>{n}/1000</b>", relay_volume=n, volume=n)

@Client.on_message(filters.regex(r"^[./]gain\b") & (filters.group | filters.private))
async def cmd_gain(bot: Client, msg: Message):
    n = _relay_num_arg(msg)
    if n is None:
        await msg.reply_text("Usage: <code>/gain &lt;0-200&gt;</code>")
        return
    n = max(0, min(200, n))
    await _apply_and_reply(msg, f" Gain: <b>{n}/200</b>", gain=n)

@Client.on_message(filters.regex(r"^[./]treble\b") & (filters.group | filters.private))
async def cmd_treble(bot: Client, msg: Message):
    n = _relay_num_arg(msg)
    if n is None:
        await msg.reply_text("Usage: <code>/treble &lt;0-100&gt;</code>")
        return
    n = max(0, min(100, n))
    await _apply_and_reply(msg, f" Treble: <b>{n}/100</b>", treble=n)

@Client.on_message(filters.regex(r"^[./]voice\b") & (filters.group | filters.private))
async def cmd_voice(bot: Client, msg: Message):
    parts = msg.text.strip().split()
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

@Client.on_message(filters.regex(r"^[./]relaystatus\b") & (filters.group | filters.private))
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

@Client.on_message(filters.regex(r"^[./]mic\b") & (filters.group | filters.private))
async def cmd_mic(bot: Client, msg: Message):
    uvc = await get_engine(msg)
    if not uvc:
        return
    parts = msg.text.strip().split()
    action = parts[1].lower() if len(parts) > 1 else "devices"
    if action in {"devices", "list"}:
        if Config.MIC_RELAY_ENABLED:
            await msg.reply_text(
                " <b>Android live relay ready</b>\n"
                "VPS relay FIFO: <code>" + Config.MIC_RELAY_FIFO + "</code>\n"
                "Chrome mic page par Start dabakar yahan <code>/mic on</code> karein."
            )
            return
        devices = uvc.microphone_devices()
        if not devices:
            await msg.reply_text(
                " Server par microphone/virtual microphone nahi mila."
            )
            return
        lines = [f"{i}. <code>{d.metadata}</code> — {d.title}"
                 for i, d in enumerate(devices, 1)]
        await msg.reply_text(" <b>Available microphone inputs</b>\n" + "\n".join(lines))
        return
    if action not in {"on", "start", "off", "stop"}:
        await msg.reply_text(
            "Usage: <code>/mic on [device-name] [-100xxxxxxxx]</code>\n"
            "<code>/mic off [-100xxxxxxxx]</code>\n"
            "<code>/mic devices</code>"
        )
        return
    if action in {"off", "stop"}:
        cid_arg = None
        for value in parts[2:]:
            try:
                number = int(value)
            except ValueError:
                continue
            if number < 0:
                cid_arg = value
        cid = await need_chat(msg, cid_arg)
        if not cid:
            return
        st = uvc.chats.get(cid)
        if not st or not st.mic_enabled:
            await msg.reply_text(" Is VC mein mic on nahi hai.")
            return
        try:
            await uvc.leave(cid, reason="Mic stopped")
            await msg.reply_text("⏹ <b>Live mic OFF</b> — VC session end.")
        except Exception as exc:
            await msg.reply_text(f" Mic stop fail: <code>{exc}</code>")
        return
    cid_arg = None
    device_parts = []
    for value in parts[2:]:
        try:
            number = int(value)
        except ValueError:
            device_parts.append(value)
            continue
        if number < 0:
            cid_arg = value
        else:
            device_parts.append(value)
    cid = await need_chat(msg, cid_arg)
    if not cid:
        return
    try:
        title = await uvc.play_microphone(cid, " ".join(device_parts))
        reply_lines = [
            f" <b>Live microphone ON</b>",
            f"Input: <code>{title}</code>",
            f"Gain: <code>{uvc.state(cid).live_volume}/20000</code>",
            "",
            "Note: mic playback stream ko replace karta hai; `/play` se audio stream wapas chala sakte hain.",
        ]
        if Config.MIC_RELAY_ENABLED and Config.MIC_RELAY_TOKEN:
            relay_url = _relay_url()
            if relay_url:
                reply_lines.append("")
                reply_lines.append("📱 <b>Phone se live bolna ho to:</b>")
                reply_lines.append(f"<a href=\"{relay_url}\">Mic Page Kholo (Chrome)</a>")
                reply_lines.append("Chrome mein kholein → Start Live Mic → Allow mic")
        await msg.reply_text("\n".join(reply_lines))
    except Exception as exc:
        await msg.reply_text(f" <b>Microphone start nahi hua:</b> <code>{exc}</code>")
