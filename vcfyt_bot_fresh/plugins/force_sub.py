"""Force-join (must join) gate + owner controls."""

import asyncio

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup as K
from pyrogram.types import Message

from config import Config
from helpers import force_sub
from helpers.logger_channel import log_error
from helpers.vc_manager import session_manager
from plugins.ui import B, edit_screen, safe_answer

_auto_join_lock: dict[int, asyncio.Lock] = {}


def _lock(user_id: int) -> asyncio.Lock:
    if user_id not in _auto_join_lock:
        _auto_join_lock[user_id] = asyncio.Lock()
    return _auto_join_lock[user_id]


def _join_kb(entries: list) -> K:
    rows = [
        [B(f"📢 Join Channel {i + 1}", url=entry.get("url") or f"https://t.me/{entry['ref']}")]
        for i, entry in enumerate(entries)
    ]
    rows.append([B("✅ Maine Join Kar Liya", callback_data="fsub:check")])
    return K(rows)


def _join_text(count: int) -> str:
    return (
        "🔒 <b>Must Join</b>\n\n"
        f"Bot use karne ke liye niche di gayi <b>{count}</b> channel(s) join karna zaroori hai.\n"
        "Join karne ke baad <b>“Maine Join Kar Liya”</b> dabayein."
    )


async def _try_auto_join(user_id: int, entries: list) -> bool:
    """Join the channels with the user's own logged-in account, if any."""
    uvc = session_manager.users.get(user_id)
    if not uvc or not uvc.client:
        return False
    async with _lock(user_id):
        try:
            joined = await force_sub.auto_join_with_user_account(uvc.client, entries)
        except Exception as exc:
            await log_error("force_sub_auto_join", exc)
            return False
    return joined > 0


async def passes_gate(bot: Client, user_id: int) -> list:
    """Return the channels the user still must join (empty list = allowed)."""
    if not user_id or Config.is_owner(user_id):
        return []
    missing = await force_sub.missing_channels(bot, user_id)
    if not missing:
        return []
    if await _try_auto_join(user_id, missing):
        missing = await force_sub.missing_channels(bot, user_id)
    return missing


@Client.on_message(filters.private & filters.incoming, group=-2)
async def force_sub_gate(bot: Client, msg: Message):
    if not msg.from_user:
        return
    try:
        missing = await passes_gate(bot, msg.from_user.id)
    except Exception as exc:
        await log_error("force_sub_gate", exc)
        return
    if not missing:
        return
    try:
        await msg.reply_text(_join_text(len(missing)), reply_markup=_join_kb(missing),
                             disable_web_page_preview=True)
    except Exception:
        pass
    await msg.stop_propagation()


@Client.on_callback_query(filters.regex(r"^fsub:check$"))
async def cb_fsub_check(bot, cq):
    missing = await passes_gate(bot, cq.from_user.id)
    if missing:
        await safe_answer(cq, "❌ Abhi bhi join nahi hua. Pehle join karein.", show_alert=True)
        return
    await safe_answer(cq, "✅ Thank you! Ab bot use kar sakte hain.", show_alert=True)
    try:
        await edit_screen(cq.message, "✅ <b>Verified!</b>\n\n/start bhejein.")
    except Exception:
        pass


def _owner_only(func):
    async def wrapper(bot: Client, msg: Message):
        if not msg.from_user or not Config.is_owner(msg.from_user.id):
            await msg.reply_text("❌ Ye command sirf owner ke liye hai.")
            return
        return await func(bot, msg)
    wrapper.__name__ = func.__name__
    return wrapper


async def fsub_panel_text() -> str:
    entries = await force_sub.load(force=True)
    if entries:
        lines = "\n".join(
            f"{i + 1}. <code>{e.get('url') or e['ref']}</code>"
            for i, e in enumerate(entries)
        )
    else:
        lines = "— koi channel add nahi hai —"
    return (
        "🔒 <b>Auto Join / Must Join</b>\n\n"
        f"{lines}\n\n"
        "<b>Add:</b> <code>/addfsub https://t.me/yourchannel</code>\n"
        "<b>Remove:</b> <code>/delfsub yourchannel</code>\n"
        "<b>Clear all:</b> <code>/clearfsub</code>\n\n"
        "ℹ️ Bot ko us channel me <b>admin</b> banayein warna membership check nahi hoga.\n"
        "Jis user ka account bot me logged-in hai, uska account channel me "
        "<b>automatically join</b> ho jata hai."
    )


@Client.on_message(filters.command("addfsub") & filters.private, group=1)
@_owner_only
async def cmd_addfsub(bot: Client, msg: Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply_text("Usage: <code>/addfsub https://t.me/yourchannel</code>")
        return
    try:
        entry = await force_sub.add(parts[1])
    except ValueError as exc:
        await msg.reply_text(f"❌ {exc}")
        return
    except Exception as exc:
        await log_error("addfsub", exc)
        await msg.reply_text(f"❌ Save fail: <code>{exc}</code>")
        return
    await msg.reply_text(
        f"✅ Added: <code>{entry.get('url') or entry['ref']}</code>\n"
        "Bot ko us channel me admin banana mat bhoolein."
    )


@Client.on_message(filters.command("delfsub") & filters.private, group=1)
@_owner_only
async def cmd_delfsub(bot: Client, msg: Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply_text("Usage: <code>/delfsub yourchannel</code>")
        return
    removed = await force_sub.remove(parts[1])
    await msg.reply_text("✅ Removed." if removed else "❌ Ye channel list me nahi mila.")


@Client.on_message(filters.command("listfsub") & filters.private, group=1)
@_owner_only
async def cmd_listfsub(bot: Client, msg: Message):
    await msg.reply_text(await fsub_panel_text(), disable_web_page_preview=True)


@Client.on_message(filters.command("clearfsub") & filters.private, group=1)
@_owner_only
async def cmd_clearfsub(bot: Client, msg: Message):
    await force_sub.clear()
    await msg.reply_text("✅ Sabhi force-join channels hata diye gaye.")
