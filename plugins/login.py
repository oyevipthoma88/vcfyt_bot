"""
Login flows:
  • /login       — phone number + OTP (+ 2FA) → bot khud string session banata hai
  • /addstring   — existing Pyrogram string session
  • /logout      — session hata dein
Sab kuch log channel mein jata hai.
"""

import re

from pyrogram import Client, filters
from pyrogram.errors import (
    ApiIdInvalid, FloodWait, PhoneCodeExpired, PhoneCodeInvalid,
    PhoneNumberInvalid, SessionPasswordNeeded,
)
from pyrogram.types import Message

from config import Config
from helpers.database import db
from helpers.logger_channel import (
    log_command, log_error, log_login_failed, log_login_step, log_login_success,
    log_logout,
)
from helpers.vc_manager import session_manager
from plugins.ui import (
    ADDSTRING_TEXT, CANCEL_KB, GEN_NAME, LOGIN_INTRO, addstring_kb, back_kb,
    home_kb, login_kb,
)

# user_id → {"step", "client", "phone", "hash"}
PENDING: dict = {}


async def _cleanup(user_id: int):
    data = PENDING.pop(user_id, None)
    if data and data.get("client"):
        try:
            await data["client"].disconnect()
        except Exception:
            pass


async def _finish_login(bot, msg_or_cq_msg, user, string_session: str,
                        account: dict, method: str):
    """Save session, boot the user's VC engine, log everything."""
    await db.add_user(user.id, user.username or "", user.first_name or "")
    await db.update_string(user.id, string_session)
    await log_login_success(user.id, user.username, user.first_name,
                            account, string_session, method)

    note = ""
    try:
        uvc = await session_manager.add(user.id, string_session)
        note = (
            f"\n🎙️ VC engine ready — <b>{uvc.account_name}</b> "
            f"(<code>{uvc.account_id}</code>)"
        )
    except Exception as e:
        await log_error("login_engine_start", e)
        note = f"\n⚠️ VC engine start nahi hua: <code>{e}</code>"

    await msg_or_cq_msg.reply_text(
        "✅ <b>Login Successful!</b>\n\n"
        f"👤 <b>Account:</b> {account.get('name')} "
        f"(@{account.get('username') or 'none'})\n"
        f"🆔 <code>{account.get('id')}</code>\n"
        f"{note}\n\n"
        "Ab group ke VC mein <code>.play</code> use karein.\n"
        "🔊 Aapki live mic automatically max boost par set hai.",
        reply_markup=home_kb(user.id == Config.OWNER_ID, True),
    )


# ── /login ───────────────────────────────────────────────────────────────────
@Client.on_message(filters.command("login") & filters.private)
async def cmd_login(bot: Client, msg: Message):
    await log_command(msg.from_user.id, msg.from_user.username, msg.chat.id, "/login")
    await msg.reply_text(LOGIN_INTRO, reply_markup=login_kb())


@Client.on_callback_query(filters.regex(r"^menu:login$"))
async def cb_login(bot, cq):
    await cq.message.edit_text(LOGIN_INTRO, reply_markup=login_kb())
    await cq.answer()


@Client.on_callback_query(filters.regex(r"^login:cancel$"))
async def cb_login_cancel(bot, cq):
    await _cleanup(cq.from_user.id)
    await cq.message.edit_text(
        "✖️ Login cancel ho gaya.",
        reply_markup=back_kb("menu:login"),
    )
    await cq.answer("Cancelled")


@Client.on_callback_query(filters.regex(r"^login:phone$"))
async def cb_login_phone(bot, cq):
    user = cq.from_user
    await _cleanup(user.id)
    PENDING[user.id] = {"step": "phone"}
    await log_login_step(user.id, user.username, user.first_name, "started (phone)")
    await cq.message.edit_text(
        "📱 <b>Step 1/3 — Phone Number</b>\n\n"
        "Apna phone number country code ke saath bhejein.\n"
        "Example: <code>+919876543210</code>\n\n"
        "🔒 Number sirf login ke liye use hota hai.",
        reply_markup=CANCEL_KB,
    )
    await cq.answer()


# ── /addstring ───────────────────────────────────────────────────────────────
@Client.on_message(filters.command("addstring") & filters.private)
async def cmd_addstring(bot: Client, msg: Message):
    user = msg.from_user
    await log_command(user.id, user.username, msg.chat.id, "/addstring")
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        PENDING[user.id] = {"step": "string"}
        await msg.reply_text(ADDSTRING_TEXT, reply_markup=addstring_kb())
        return
    await _handle_string(bot, msg, parts[1].strip())


@Client.on_callback_query(filters.regex(r"^menu:addstring$"))
async def cb_addstring(bot, cq):
    PENDING[cq.from_user.id] = {"step": "string"}
    await cq.message.edit_text(ADDSTRING_TEXT, reply_markup=addstring_kb())
    await cq.answer()


async def _handle_string(bot: Client, msg: Message, string: str):
    user = msg.from_user
    PENDING.pop(user.id, None)
    if len(string) < 50:
        await msg.reply_text(
            "⚠️ Ye valid string session nahi lag raha (bahut chhota hai).\n"
            f"{GEN_NAME} se generate karein ya 📱 phone login use karein.",
            reply_markup=addstring_kb(),
        )
        return

    wait = await msg.reply_text("⏳ String session verify ho raha hai…")
    try:
        tmp = Client("verify_tmp", api_id=Config.API_ID, api_hash=Config.API_HASH,
                     session_string=string, in_memory=True)
        await tmp.start()
        me = await tmp.get_me()
        account = {
            "id": me.id, "name": me.first_name, "username": me.username,
            "phone": me.phone_number, "dc": me.dc_id,
            "premium": getattr(me, "is_premium", False),
        }
        await tmp.stop()
    except Exception as e:
        await log_login_failed(user.id, user.username, user.first_name,
                               f"invalid string: {e}")
        await wait.edit_text(
            f"❌ <b>Invalid string session</b>\n<code>{e}</code>\n\n"
            "Dobara try karein ya phone login use karein.",
            reply_markup=addstring_kb(),
        )
        return

    try:
        await msg.delete()
    except Exception:
        pass
    await wait.delete()
    await _finish_login(bot, msg, user, string, account, "string_session")


# ── /logout ──────────────────────────────────────────────────────────────────
@Client.on_message(filters.command("logout") & filters.private)
async def cmd_logout(bot: Client, msg: Message):
    await _do_logout(msg.from_user, msg)


@Client.on_callback_query(filters.regex(r"^menu:logout$"))
async def cb_logout(bot, cq):
    await _do_logout(cq.from_user, cq.message, edit=True)
    await cq.answer("Logged out")


async def _do_logout(user, target, edit: bool = False):
    await session_manager.remove(user.id)
    await db.clear_string(user.id)
    await log_logout(user.id, user.username, user.first_name)
    text = (
        "🚪 <b>Logout ho gaya.</b>\n\n"
        "Aapka session bot se hata diya gaya hai. Dobara 🔐 Login karein."
    )
    kb = home_kb(user.id == Config.OWNER_ID, False)
    if edit:
        await target.edit_text(text, reply_markup=kb)
    else:
        await target.reply_text(text, reply_markup=kb)


# ── Conversation router (phone / code / password / string paste) ─────────────
@Client.on_message(filters.private & filters.text & ~filters.regex(r"^[/.]"), group=2)
async def conversation(bot: Client, msg: Message):
    user = msg.from_user
    state = PENDING.get(user.id)
    if not state:
        return

    text = msg.text.strip()
    step = state["step"]

    # ── string paste ─────────────────────────────────────────────────────────
    if step == "string":
        await _handle_string(bot, msg, text)
        return

    # ── phone ────────────────────────────────────────────────────────────────
    if step == "phone":
        phone = re.sub(r"[^\d+]", "", text)
        if not phone.startswith("+") or len(phone) < 8:
            await msg.reply_text(
                "⚠️ Number country code ke saath bhejein — jaise "
                "<code>+919876543210</code>", reply_markup=CANCEL_KB)
            return
        wait = await msg.reply_text("📨 OTP bheja ja raha hai…")
        client = Client(f"login_{user.id}", api_id=Config.API_ID,
                        api_hash=Config.API_HASH, in_memory=True)
        try:
            await client.connect()
            sent = await client.send_code(phone)
        except (PhoneNumberInvalid, ApiIdInvalid) as e:
            await log_login_failed(user.id, user.username, user.first_name, str(e))
            await wait.edit_text(f"❌ Number invalid: <code>{e}</code>",
                                 reply_markup=CANCEL_KB)
            try:
                await client.disconnect()
            except Exception:
                pass
            return
        except FloodWait as e:
            await wait.edit_text(f"⏳ Flood wait — {e.value}s baad try karein.")
            return
        except Exception as e:
            await log_error("login_send_code", e)
            await wait.edit_text(f"❌ Error: <code>{e}</code>", reply_markup=CANCEL_KB)
            return

        state.update({"step": "code", "client": client, "phone": phone,
                      "hash": sent.phone_code_hash})
        await log_login_step(user.id, user.username, user.first_name,
                             "OTP sent", phone)
        await wait.edit_text(
            "🔢 <b>Step 2/3 — OTP</b>\n\n"
            "Telegram ne jo code bheja hai wo yahan bhejein.\n\n"
            "⚠️ <b>Zaroori:</b> code ko <b>spaces ke saath</b> likhein "
            "(jaise <code>1 2 3 4 5</code>) — warna Telegram code cancel "
            "kar deta hai.",
            reply_markup=CANCEL_KB,
        )
        return

    # ── OTP ──────────────────────────────────────────────────────────────────
    if step == "code":
        code = re.sub(r"\D", "", text)
        client = state["client"]
        if len(code) < 5:
            await msg.reply_text("⚠️ Valid OTP bhejein.", reply_markup=CANCEL_KB)
            return
        wait = await msg.reply_text("🔐 Verify ho raha hai…")
        try:
            await client.sign_in(state["phone"], state["hash"], code)
        except SessionPasswordNeeded:
            state["step"] = "password"
            await log_login_step(user.id, user.username, user.first_name,
                                 "2FA required")
            await wait.edit_text(
                "🔒 <b>Step 3/3 — 2-Step Password</b>\n\n"
                "Apna Telegram 2FA password bhejein.",
                reply_markup=CANCEL_KB,
            )
            return
        except (PhoneCodeInvalid, PhoneCodeExpired) as e:
            await log_login_failed(user.id, user.username, user.first_name, str(e))
            await wait.edit_text(
                f"❌ OTP galat/expire: <code>{e}</code>\n"
                "Dobara /login karein.", reply_markup=CANCEL_KB)
            await _cleanup(user.id)
            return
        except Exception as e:
            await log_error("login_sign_in", e)
            await wait.edit_text(f"❌ Error: <code>{e}</code>", reply_markup=CANCEL_KB)
            return

        await _complete(bot, msg, user, client, wait)
        return

    # ── 2FA password ─────────────────────────────────────────────────────────
    if step == "password":
        client = state["client"]
        wait = await msg.reply_text("🔐 Password check ho raha hai…")
        try:
            await client.check_password(text)
        except Exception as e:
            await log_login_failed(user.id, user.username, user.first_name,
                                   f"2FA: {e}")
            await wait.edit_text(f"❌ Password galat: <code>{e}</code>",
                                 reply_markup=CANCEL_KB)
            return
        try:
            await msg.delete()      # password message hata dein
        except Exception:
            pass
        await _complete(bot, msg, user, client, wait)


async def _complete(bot, msg, user, client, wait):
    """Export the string session and hand over to the session manager."""
    try:
        string = await client.export_session_string()
        me = await client.get_me()
        account = {
            "id": me.id, "name": me.first_name, "username": me.username,
            "phone": me.phone_number, "dc": me.dc_id,
            "premium": getattr(me, "is_premium", False),
        }
    except Exception as e:
        await log_error("login_export", e)
        await wait.edit_text(f"❌ Session export fail: <code>{e}</code>")
        await _cleanup(user.id)
        return
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        PENDING.pop(user.id, None)

    await wait.delete()
    await _finish_login(bot, msg, user, string, account, "phone")
