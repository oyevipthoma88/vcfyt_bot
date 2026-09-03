"""Persistent personal and owner-shared audio library commands."""

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup as K, Message

from config import Config
from pyrogram.types import InlineKeyboardButton as B
from helpers.database import db
from helpers.logger_channel import log_command, log_error
from helpers.vc_manager import session_manager


def library_kb() -> K:
    return K([
        [B("🎵 Available Audio", callback_data="aud:my"),
         B("👑 Bot Audios", callback_data="aud:bot")],
        [B("➕ Save My Audio", callback_data="aud:help")],
        [B("🏠 Home", callback_data="menu:home")],
    ])


def _media(reply):
    if not reply:
        return None
    return (reply.audio or reply.voice or reply.video or reply.document
            or reply.video_note)


def _active_chat(uvc, message=None):
    if not uvc:
        return None
    # A library opened inside a group already identifies the intended target.
    # The session may not have a state entry yet when the user's account joined
    # the VC manually, so do not require a previous bot playback in that chat.
    if message and message.chat and message.chat.id < 0:
        return message.chat.id
    return next(
        (cid for cid, state in uvc.chats.items() if state.is_playing),
        next(iter(uvc.chats), None),
    )


def _item_kb(item: dict, can_delete: bool = False) -> K:
    audio_id = str(item["audio_id"])
    rows = [[B("▶️ Play in active VC", callback_data=f"aud:play:{audio_id}")]]
    if can_delete:
        rows.append([B("🗑️ Delete", callback_data=f"aud:del:{audio_id}")])
    return K(rows)


async def _show_items(cq, items: list, heading: str, user_id: int):
    if not items:
        await cq.message.edit_text(
            f"{heading}\n\n📭 Abhi koi audio saved nahi hai.",
            reply_markup=library_kb(),
        )
        return
    await cq.message.edit_text(
        f"{heading}\n\n{len(items)} audio saved hai. Neeche har item ka alag control hai.",
        reply_markup=library_kb(),
    )
    for item in items:
        title = item.get("title") or "Untitled audio"
        kind = item.get("file_type", "audio")
        await cq.message.reply_text(
            f"🎵 <b>{title}</b>\n"
            f"├ Type: <code>{kind}</code>\n"
            f"└ ID: <code>{str(item['audio_id'])[:8]}</code>",
            reply_markup=_item_kb(
                item, int(item.get("owner_id", 0)) == int(user_id)
            ),
        )


@Client.on_message(filters.regex(r"^[./](audio|audios|myaudio)\b") &
                   (filters.group | filters.private))
async def cmd_audio_library(bot: Client, msg: Message):
    await log_command(msg.from_user.id, msg.from_user.username, msg.chat.id, ".audio")
    parts = msg.text.strip().split(maxsplit=1)
    if len(parts) == 1:
        await msg.reply_text(
            "🎧 <b>Audio Library</b>\n\n"
            "Apne saved audio aur owner ke shared Bot Audios browse karein.",
            reply_markup=library_kb(),
        )
        return
    title = parts[1].strip()
    await _save_reply_audio(msg, msg.from_user.id, title, "my")


@Client.on_message(filters.regex(r"^[./]saveaudio\b") &
                   (filters.group | filters.private))
async def cmd_save_audio(bot: Client, msg: Message):
    parts = msg.text.strip().split(maxsplit=1)
    title = parts[1].strip() if len(parts) > 1 else ""
    await _save_reply_audio(msg, msg.from_user.id, title, "my")


@Client.on_message(filters.regex(r"^[./]addaudio\b") & filters.private)
async def cmd_add_owner_audio(bot: Client, msg: Message):
    if not Config.is_owner(msg.from_user.id):
        await msg.reply_text("⛔ Ye command sirf owner ke liye hai.")
        return
    parts = msg.text.strip().split(maxsplit=1)
    title = parts[1].strip() if len(parts) > 1 else ""
    await _save_reply_audio(msg, msg.from_user.id, title, "owner")


async def _save_reply_audio(msg: Message, owner_id: int, title: str, mode: str):
    reply = msg.reply_to_message
    media = _media(reply)
    if not media:
        usage = ".addaudio <title>" if mode == "owner" else ".saveaudio <title>"
        await msg.reply_text(
            f"⚠️ Audio/video message ko reply karke <code>{usage}</code> bhejein."
        )
        return
    title = (title or getattr(media, "file_name", None) or
             getattr(reply, "caption", None) or "Untitled audio").strip()
    file_type = "audio" if (reply.audio or reply.voice) else "video"
    try:
        audio_id = await db.add_audio(
            owner_id, title, media.file_id, file_type, reply.caption or ""
        )
    except Exception as exc:
        await log_error("save_audio", exc)
        await msg.reply_text(f"❌ Audio save nahi hua: <code>{exc}</code>")
        return
    scope = "Bot Audios mein public" if mode == "owner" else "My Audio mein private"
    await msg.reply_text(
        f"✅ <b>{title}</b> save ho gaya.\n"
        f"📚 {scope}\n"
        f"🆔 <code>{audio_id}</code>",
        reply_markup=library_kb(),
    )


@Client.on_callback_query(filters.regex(r"^aud:"))
async def cb_audio_library(bot, cq):
    parts = cq.data.split(":", 2)
    action = parts[1]
    uid = cq.from_user.id
    if action == "menu":
        await cq.message.edit_text(
            "🎧 <b>Audio Library</b>\n\n"
            "Available Audio mein apne saved tracks aur owner ke shared tracks milenge.",
            reply_markup=library_kb(),
        )
        await cq.answer()
        return
    if action == "help":
        await cq.answer("Reply to audio/video, then .saveaudio <title> bhejein", show_alert=True)
        return
    if action == "my":
        owner = Config.primary_owner()
        await _show_items(
            cq, await db.list_available_audio(uid, owner),
            "🎵 <b>My Audio + Bot Audios</b>", uid,
        )
        await cq.answer()
        return
    if action == "bot":
        owner = Config.primary_owner()
        await _show_items(
            cq, await db.list_bot_audio(owner), "👑 <b>Bot Audios</b>", uid
        )
        await cq.answer()
        return
    if len(parts) < 3:
        await cq.answer("Invalid audio action", show_alert=True)
        return
    audio_id = parts[2]
    item = await db.get_audio(audio_id)
    if not item:
        await cq.answer("Audio nahi mila", show_alert=True)
        return
    if action == "del":
        if int(item.get("owner_id", 0)) != uid:
            await cq.answer("Sirf apna audio delete kar sakte hain", show_alert=True)
            return
        deleted = await db.delete_audio(uid, audio_id)
        await cq.answer("Deleted" if deleted else "Audio nahi mila", show_alert=True)
        try:
            await cq.message.edit_text("🗑️ Audio delete ho gaya.", reply_markup=library_kb())
        except Exception:
            pass
        return
    if action == "play":
        uvc = await session_manager.get(uid)
        cid = _active_chat(uvc, cq.message)
        if not uvc or cid is None:
            await cq.answer(
                "Library ko group ke andar kholkar Play dabayein, "
                "ya pehle .play se is group ko select karein.",
                show_alert=True,
            )
            return
        try:
            path = await bot.download_media(item["file_id"])
            if not path:
                raise RuntimeError("Telegram media download empty path returned")
            from plugins.vc_commands import load_state_settings
            await load_state_settings(uid, uvc, cid)
            await uvc.play(cid, path, item.get("title", "Saved audio"), enqueue=False)
            await cq.answer("▶️ Active VC mein play ho raha hai")
        except Exception as exc:
            await log_error("play_saved_audio", exc)
            await cq.answer("Play fail hua—VC aur login check karein", show_alert=True)
