"""
/start, home menu, status, and the live audio-settings panel.
"""

from pyrogram import Client, filters
from pyrogram.types import Message

from config import Config
from helpers.audio_processor import (
    BASS_MAX, BASS_MIN, LEVEL_MAX, LEVEL_MIN, VOLUME_MAX, VOLUME_MIN, clamp,
)
from helpers.database import db
from helpers.logger_channel import log_command, log_error, log_new_user
from helpers.vc_manager import AUTO_PRESET, session_manager
from plugins.ui import (
    home_kb, home_text, settings_kb, settings_text, status_text,
)
from plugins.tutorial import TUTORIAL_MENU_TEXT, tutorial_kb


async def _is_logged_in(user_id: int) -> bool:
    if user_id in session_manager.users:
        return True
    data = await db.get_user(user_id)
    return bool(data and data.get("string_session"))


# ── /start ───────────────────────────────────────────────────────────────────
@Client.on_message(filters.command("start") & filters.private)
async def cmd_start(bot: Client, msg: Message):
    user = msg.from_user
    await log_command(user.id, user.username, msg.chat.id, "/start")

    existing = await db.get_user(user.id)
    await db.add_user(user.id, user.username or "", user.first_name or "")
    if not existing:
        await log_new_user(user.id, user.username, user.first_name)

    logged = await _is_logged_in(user.id)
    uvc = session_manager.users.get(user.id)
    active_chat_id = next((cid for cid, st in uvc.chats.items() if st.is_playing), None) if uvc else None
    text = home_text(user.first_name or "friend", logged)
    markup = home_kb(Config.is_owner(user.id), logged, active_chat_id)
    if Config.START_PIC:
        try:
            await msg.reply_photo(Config.START_PIC, caption=text, reply_markup=markup)
            return
        except Exception:
            # Invalid/expired Telegram file_id or inaccessible URL must not break /start.
            pass
    await msg.reply_text(text, reply_markup=markup, disable_web_page_preview=True)


@Client.on_callback_query(filters.regex(r"^menu:home$"))
async def cb_home(bot, cq):
    user = cq.from_user
    logged = await _is_logged_in(user.id)
    uvc = session_manager.users.get(user.id)
    active_chat_id = next((cid for cid, st in uvc.chats.items() if st.is_playing), None) if uvc else None
    await cq.message.edit_text(
        home_text(user.first_name or "friend", logged),
        reply_markup=home_kb(Config.is_owner(user.id), logged, active_chat_id),
        disable_web_page_preview=True,
    )
    await cq.answer()


@Client.on_callback_query(filters.regex(r"^menu:tutorial$"))
async def cb_tutorial_menu(bot, cq):
    await cq.message.edit_text(TUTORIAL_MENU_TEXT, reply_markup=tutorial_kb())
    await cq.answer()


# ── Status ───────────────────────────────────────────────────────────────────
@Client.on_message(filters.command("mystatus") & filters.private)
async def cmd_mystatus(bot: Client, msg: Message):
    await log_command(msg.from_user.id, msg.from_user.username, msg.chat.id,
                      "/mystatus")
    data = await db.get_user(msg.from_user.id)
    s = await db.get_settings(msg.from_user.id)
    uvc = session_manager.users.get(msg.from_user.id)
    active_chat_id = next((cid for cid, st in uvc.chats.items() if st.is_playing), None) if uvc else None
    await msg.reply_text(status_text(msg.from_user.id, data, uvc, s),
                         reply_markup=home_kb(
                             Config.is_owner(msg.from_user.id),
                             bool(data and data.get("string_session")), active_chat_id))


@Client.on_callback_query(filters.regex(r"^menu:status$"))
async def cb_status(bot, cq):
    uid = cq.from_user.id
    data = await db.get_user(uid)
    s = await db.get_settings(uid)
    uvc = session_manager.users.get(uid)
    from plugins.ui import back_kb
    await cq.message.edit_text(status_text(uid, data, uvc, s),
                               reply_markup=back_kb("menu:home"))
    await cq.answer()


# ── Audio settings panel ─────────────────────────────────────────────────────
@Client.on_message(filters.command("settings") & filters.private)
async def cmd_settings(bot: Client, msg: Message):
    s = await db.get_settings(msg.from_user.id)
    await msg.reply_text(settings_text(s), reply_markup=settings_kb())


@Client.on_callback_query(filters.regex(r"^menu:settings$"))
async def cb_settings(bot, cq):
    s = await db.get_settings(cq.from_user.id)
    await cq.message.edit_text(settings_text(s), reply_markup=settings_kb())
    await cq.answer()


DEFAULT_SETTINGS = {
    "volume": Config.DEFAULT_VOLUME, "relay_volume": Config.RELAY_DEFAULT_VOLUME,
    "bass": Config.DEFAULT_BASS, "gain": Config.RELAY_DEFAULT_GAIN,
    "treble": Config.RELAY_DEFAULT_TREBLE, "voice": "normal",
    "echo": 1 if Config.DEFAULT_ECHO else 0,
    "echo_level": Config.DEFAULT_ECHO_LEVEL, "boost": Config.DEFAULT_BOOST,
    "auto": 0,
}


async def apply_settings_live(user_id: int) -> int:
    """Push the user's saved settings onto every VC they are streaming in."""
    uvc = session_manager.users.get(user_id)
    if not uvc:
        return 0
    s = await db.get_settings(user_id)
    applied = 0
    for chat_id, st in list(uvc.chats.items()):
        st.apply_settings(s)
        if bool(s.get("auto")) != bool(st.auto):
            await uvc.set_auto(chat_id, bool(s.get("auto")))
        try:
            if st.is_playing and await uvc.reapply(chat_id):
                applied += 1
        except Exception as e:
            await log_error("apply_settings_live", e)
    return applied


@Client.on_callback_query(filters.regex(r"^set:"))
async def cb_settings_change(bot, cq):
    uid = cq.from_user.id
    _, action, *rest = cq.data.split(":")
    s = await db.get_settings(uid)

    if action == "noop":
        await cq.answer()
        return

    if action in ("vol", "relay"):
        # Practical playback control: 0–1000. Keep legacy volume synced so
        # old settings and the new panel always point to the same real gain.
        s["relay_volume"] = clamp(
            s.get("relay_volume", Config.RELAY_DEFAULT_VOLUME) + int(rest[0]),
            VOLUME_MIN, VOLUME_MAX,
        )
        s["volume"] = s["relay_volume"]
    elif action == "bass":
        s["bass"] = clamp(s["bass"] + int(rest[0]), BASS_MIN, BASS_MAX)
    elif action == "boost":
        s["boost"] = clamp(s["boost"] + int(rest[0]), LEVEL_MIN, LEVEL_MAX)
    elif action == "echolvl":
        s["echo_level"] = clamp(s["echo_level"] + int(rest[0]), LEVEL_MIN, LEVEL_MAX)
        s["echo"] = 1 if s["echo_level"] > 0 else s["echo"]
    elif action == "echo":
        s["echo"] = 0 if s["echo"] else 1
    elif action == "auto":
        s["auto"] = 0 if s.get("auto") else 1
        if s["auto"]:
            # AUTO = maximum real gain, but voice-safe EQ (not muddy bass).
            s.update(AUTO_PRESET)
    elif action == "reset":
        s.update(DEFAULT_SETTINGS)
    elif action == "max":
        s.update(AUTO_PRESET, auto=1)
    elif action == "apply":
        n = await apply_settings_live(uid)
        await cq.answer(f"⚡ {n} VC par apply ho gaya" if n
                        else "Koi active VC nahi", show_alert=not n)
        return

    await db.save_settings(uid, **s)
    await apply_settings_live(uid)
    try:
        await cq.message.edit_text(settings_text(s), reply_markup=settings_kb())
    except Exception:
        pass
    await cq.answer("Saved ✅")
