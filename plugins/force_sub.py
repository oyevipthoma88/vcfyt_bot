"""Force-join (must join) gate + owner controls."""

from pyrogram import Client, filters
from plugins.ui import dm_cmd
from pyrogram.types import InlineKeyboardMarkup as K
from pyrogram.types import Message

from config import Config
from helpers import force_sub
from helpers.logger_channel import log_error
from plugins.ui import HAS_USER, cmd_text, B, edit_screen, safe_answer

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


async def passes_gate(bot: Client, user_id: int) -> list:
    """Return channels not verified as joined; errors never fail open."""
    if not user_id or Config.is_owner(user_id):
        return []
    return await force_sub.missing_channels(bot, user_id)


@Client.on_message(filters.private & filters.incoming, group=-2)
async def force_sub_gate(bot: Client, msg: Message):
    if not msg.from_user:
        return
    try:
        missing = await passes_gate(bot, msg.from_user.id)
    except Exception as exc:
        await log_error("force_sub_gate", exc)
        missing = await force_sub.load(force=True)
        if not missing:
            return
    if not missing:
        return
    try:
        from helpers.database import db
        u = msg.from_user
        if not await db.get_user(u.id):
            await db.add_user(u.id, u.username or "", u.first_name or "")
    except Exception as exc:
        await log_error("force_sub_register", exc)
    try:
        await msg.reply_text(_join_text(len(missing)), reply_markup=_join_kb(missing),
                             disable_web_page_preview=True)
    except Exception:
        pass
    await msg.stop_propagation()


@Client.on_callback_query(filters.regex(r"^fsub:check$"))
async def cb_fsub_check(bot, cq):
    try:
        missing = await passes_gate(bot, cq.from_user.id)
    except Exception as exc:
        await log_error("force_sub_check", exc)
        missing = await force_sub.load(force=True)
    if missing:
        # Unverified targets remain blocked; owner can use /listfsub and
        # /delfsub or /clearfsub to repair a bad channel configuration.
        await safe_answer(cq, "❌ Join verify nahi ho paya. Channel join karein; issue rahe to owner se kahen.", show_alert=True)
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
        "🔒 <b>Must Join verification</b>\n\n"
        f"{lines}\n\n"
        "<b>Add:</b> <code>/addfsub https://t.me/yourchannel</code>\n"
        "<b>Remove:</b> <code>.delfsub yourchannel</code>\n"
        "<b>Clear all:</b> <code>.clearfsub</code>\n\n"
        "ℹ️ Membership verify karne ke liye bot ko channel me add karke admin banayein.\n"
        "Users khud Join button dabayenge; bot kisi account ko auto-join nahi karta.\n"
        "Agar channel verify na ho, owner <code>/delfsub</code> ya <code>/clearfsub</code> se repair kar sakta hai."
    )


@Client.on_message(HAS_USER & dm_cmd("addfsub") & filters.private, group=1)
@_owner_only
async def cmd_addfsub(bot: Client, msg: Message):
    parts = cmd_text(msg).split(maxsplit=1)
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
        "Bot ko channel me admin banayein; setup ko verify kiye bina users ko allow nahi kiya jayega."
    )


@Client.on_message(HAS_USER & dm_cmd("delfsub") & filters.private, group=1)
@_owner_only
async def cmd_delfsub(bot: Client, msg: Message):
    parts = cmd_text(msg).split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply_text("Usage: <code>.delfsub yourchannel</code>")
        return
    removed = await force_sub.remove(parts[1])
    await msg.reply_text("✅ Removed." if removed else "❌ Ye channel list me nahi mila.")


@Client.on_message(HAS_USER & dm_cmd("listfsub") & filters.private, group=1)
@_owner_only
async def cmd_listfsub(bot: Client, msg: Message):
    await msg.reply_text(await fsub_panel_text(), disable_web_page_preview=True)


@Client.on_message(HAS_USER & dm_cmd("clearfsub") & filters.private, group=1)
@_owner_only
async def cmd_clearfsub(bot: Client, msg: Message):
    await force_sub.clear()
    await msg.reply_text("✅ Sabhi force-join channels hata diye gaye.")
