"""
Owner-only admin commands + panel.
"""

import asyncio
import os
import sys

from pyrogram import Client, filters
from plugins.ui import B, edit_screen, safe_answer, set_source_code_url
import plugins.ui as shared_ui
from pyrogram.types import InlineKeyboardMarkup as K
from pyrogram.types import Message

from config import Config
from helpers.database import db
from helpers.logger_channel import log_broadcast, log_command
from helpers.vc_manager import session_manager

BANNED_USERS: set = set()


def owner_only(func):
    async def wrapper(bot: Client, msg: Message):
        if not msg.from_user or not Config.is_owner(msg.from_user.id):
            await msg.reply_text(" Ye command sirf owner ke liye hai.")
            return
        return await func(bot, msg)
    wrapper.__name__ = func.__name__
    return wrapper


def panel_kb() -> K:
    return K([
        [B(" Users", callback_data="adm_users"),
         B(" Stats", callback_data="adm_stats")],
        [B(" Active VCs", callback_data="adm_vcs"),
         B(" Broadcast", callback_data="adm_broadcast")],
        [B(" Add Audio", callback_data="adm_addaudio")],
        [B(" Source URL", callback_data="adm_source")],
        [B(" Restart", callback_data="adm_restart")],
        [B(" Home", callback_data="menu:home")],
    ])


async def _panel_text() -> str:
    users = await db.all_users()
    with_session = sum(1 for u in users if u.get("string_session"))
    return (
        " <b>Owner Panel</b>\n\n"
        f"├ <b>Users:</b> {len(users)}\n"
        f"├ <b>Logged in:</b> {with_session}\n"
        f"├ <b>Live engines:</b> {len(session_manager.users)}\n"
        f"└ <b>Active VCs:</b> {session_manager.active_chats()}"
    )


@Client.on_message(filters.command("owner") & filters.private)
@owner_only
async def cmd_owner_panel(bot: Client, msg: Message):
    await log_command(msg.from_user.id, msg.from_user.username, msg.chat.id, "/owner")
    await msg.reply_text(await _panel_text(), reply_markup=panel_kb())


@Client.on_callback_query(filters.regex(r"^adm_"))
async def cb_admin(bot, cq):
    if not Config.is_owner(cq.from_user.id):
        await safe_answer(cq, " Sirf owner!", show_alert=True)
        return

    action = cq.data.split("_", 1)[1]
    back = K([[B("⬅ Back", callback_data="adm_back")]])

    if action == "users":
        users = await db.all_users()
        lines = [
            f"• <code>{u['user_id']}</code> @{u.get('username') or 'none'} "
            f"{'' if u.get('string_session') else ''}"
            for u in users[:40]
        ]
        text = f" <b>Users ({len(users)})</b>\n" + "\n".join(lines)
        if len(users) > 40:
            text += f"\n…+{len(users) - 40} more"
        await edit_screen(cq.message, text, reply_markup=back)

    elif action == "stats":
        await edit_screen(cq.message, await _panel_text(), reply_markup=back)

    elif action == "vcs":
        lines = []
        for uid, uvc in session_manager.users.items():
            for cid, st in uvc.chats.items():
                lines.append(
                    f"• <code>{cid}</code> — {uvc.account_name} — "
                    f"{'playing' if st.is_playing else 'idle'}")
        await edit_screen(cq.message,
            " <b>Active VCs</b>\n" + ("\n".join(lines) or "— none —"),
            reply_markup=back)

    elif action == "addaudio":
        await edit_screen(cq.message,
            " <b>Add Bot Audio</b>\n\n"
            "Audio/video message ko reply karke:\n"
            "<code>/addaudio &lt;title&gt;</code>\n\n"
            "Save hone ke baad sab users ke <b>Bot Audios</b> section mein dikhega.",
            reply_markup=back,
        )

    elif action == "source":
        await edit_screen(cq.message,
            "🔗 <b>Source Code Button</b>\n\n"
            f"Current: <code>{shared_ui.SOURCE_CODE_URL or 'disabled'}</code>\n\n"
            "Set: <code>/setsource https://example.com/repo</code>\n"
            "Remove: <code>/clearsource</code>", reply_markup=back)

    elif action == "broadcast":
        await edit_screen(cq.message,
            " <b>Broadcast</b>\n\n"
            "<code>/broadcast &lt;message&gt;</code> ya kisi message ko reply "
            "karke <code>/broadcast</code>.\n"
            "Message database mein registered <b>sabhi users</b> ko jayega.",
            reply_markup=back)

    elif action == "restart":
        await edit_screen(cq.message, " Restarting…")
        await asyncio.sleep(1)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    elif action == "back":
        await edit_screen(cq.message, await _panel_text(), reply_markup=panel_kb())

    await safe_answer(cq)


@Client.on_message(filters.command("setsource") & filters.private)
@owner_only
async def cmd_setsource(bot: Client, msg: Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply_text("🔗 Usage: <code>/setsource https://example.com/repo</code>")
        return
    try:
        set_source_code_url(parts[1])
    except ValueError as exc:
        await msg.reply_text(f"❌ Invalid URL: <code>{exc}</code>")
        return
    await msg.reply_text("✅ Source Code button updated. /start dobara bhejein.")


@Client.on_message(filters.command("clearsource") & filters.private)
@owner_only
async def cmd_clearsource(bot: Client, msg: Message):
    set_source_code_url("")
    await msg.reply_text("✅ Source Code button removed. /start dobara bhejein.")


@Client.on_message(filters.command("broadcast") & filters.private)
@owner_only
async def cmd_broadcast(bot: Client, msg: Message):
    parts = msg.text.split(maxsplit=1)
    text_to_send = parts[1] if len(parts) > 1 else None
    reply_src = msg.reply_to_message
    if not text_to_send and not reply_src:
        await msg.reply_text("Usage: <code>/broadcast &lt;message&gt;</code> "
                             "ya kisi message ko reply karein.")
        return

    users = await db.all_users()
    recipients = {
        int(user["user_id"])
        for user in users
        if user.get("user_id") is not None
    }
    # Include every group currently tracked by a logged-in VC engine too.
    # A single set prevents duplicate sends when an owner is also registered.
    recipients.update(await db.all_broadcast_chats())
    recipients.update(
        int(chat_id)
        for uvc in session_manager.users.values()
        for chat_id in uvc.chats
        if int(chat_id) < 0
    )
    recipients = sorted(recipients)
    status = await msg.reply_text(
        f" Sending broadcast to {len(recipients)} user/group chat(s)…"
    )
    success = 0
    for user_id in recipients:
        try:
            if reply_src:
                await reply_src.forward(user_id)
            else:
                await bot.send_message(user_id, text_to_send)
            success += 1
        except Exception:
            # A blocked bot, deleted account, or invalid chat must not stop
            # delivery to the remaining registered users.
            pass
        await asyncio.sleep(0.05)

    await status.edit_text(
        f" <b>Broadcast done</b>\n├ Sent: {success}/{len(recipients)}\n"
        f"└ Failed: {len(recipients) - success}\n"
        "└ Target: all registered users + tracked VC groups")
    await log_broadcast(msg.from_user.id, len(recipients), success)


@Client.on_message(filters.command("users") & filters.private)
@owner_only
async def cmd_users(bot: Client, msg: Message):
    users = await db.all_users()
    with_session = sum(1 for u in users if u.get("string_session"))
    lines = [
        f"• <code>{u['user_id']}</code> @{u.get('username') or 'none'} "
        f"{'' if u.get('string_session') else ''}"
        for u in users[:40]
    ]
    text = (f" <b>Users ({len(users)} total, {with_session} logged in)</b>\n"
            + "\n".join(lines))
    if len(users) > 40:
        text += f"\n…+{len(users) - 40} more"
    await msg.reply_text(text)


@Client.on_message(filters.command("stats") & filters.private)
@owner_only
async def cmd_stats(bot: Client, msg: Message):
    await msg.reply_text(await _panel_text(), reply_markup=panel_kb())


@Client.on_message(filters.command("ban") & filters.private)
@owner_only
async def cmd_ban(bot: Client, msg: Message):
    parts = msg.text.split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await msg.reply_text("Usage: <code>/ban &lt;user_id&gt;</code>")
        return
    uid = int(parts[1])
    BANNED_USERS.add(uid)
    await session_manager.remove(uid)
    await msg.reply_text(f" <code>{uid}</code> banned.")


@Client.on_message(filters.command("unban") & filters.private)
@owner_only
async def cmd_unban(bot: Client, msg: Message):
    parts = msg.text.split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await msg.reply_text("Usage: <code>/unban &lt;user_id&gt;</code>")
        return
    BANNED_USERS.discard(int(parts[1]))
    await msg.reply_text(f" <code>{parts[1]}</code> unbanned.")


@Client.on_message(filters.command("restart") & filters.private)
@owner_only
async def cmd_restart(bot: Client, msg: Message):
    await msg.reply_text(" Restarting…")
    await asyncio.sleep(1)
    os.execv(sys.executable, [sys.executable] + sys.argv)


@Client.on_message(filters.incoming, group=-1)
async def ban_filter(bot: Client, msg: Message):
    if msg.from_user and msg.from_user.id in BANNED_USERS:
        await msg.stop_propagation()
