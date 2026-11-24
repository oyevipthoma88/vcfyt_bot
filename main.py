import asyncio 
import logging
import os
import sys

import helpers.ffmpeg_setup  # noqa: F401  (puts ffmpeg on PATH before anything uses it)

from pyrogram import idle
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, PeerIdInvalid
from pyrogram.types import BotCommand

from config import Config
from helpers.database import db
from helpers.logger_channel import log_error, log_shutdown, log_startup, set_bot, verify_log_channel
from helpers.vc_manager import session_manager
from helpers.styled_client import StyledBotClient
from helpers.vc_sync import init_vc_sync
from plugins.ui import set_source_code_url

def _patch_story_deleted():
    """Prevent PeerIdInvalid crash from Pyrogram's StoryDeleted._parse."""
    try:
        from pyrogram.types.messages_and_media import story_deleted
        original = story_deleted.StoryDeleted._parse
        async def _safe_parse(cls, client, stories, peer):
            try:
                return await original(cls, client, stories, peer)
            except (PeerIdInvalid, Exception):
                return None
        story_deleted.StoryDeleted._parse = classmethod(_safe_parse)
    except Exception:
        pass

_patch_story_deleted()

class _FourStFormatter(logging.Formatter):
    def format(self, record):
        tag = record.name.split(".")[-1].upper()
        if record.levelno >= logging.ERROR:
            tag = f"{tag}_ERR"
        elif record.levelno == logging.WARNING:
            tag = f"{tag}_WARN"
        text = record.getMessage()
        if record.exc_info:
            text += "\n" + self.formatException(record.exc_info)
        return f"[{self.formatTime(record, '%Y-%m-%d %H:%M:%S')}] [{tag}] -> {text}"

_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(_FourStFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler])
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logging.getLogger("pytgcalls").setLevel(logging.WARNING)
logger = logging.getLogger("vcbot")

async def _health_response(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        await reader.read(4096)
        from helpers.mic_url import health_text
        body = health_text().encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\nConnection: close\r\n" + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

async def _start_heroku_health_server():
    raw_port = os.getenv("PORT")
    if not raw_port:
        return None
    try:
        port = int(raw_port)
        from helpers.live_mic import start_server as start_live_mic_server
        await start_live_mic_server(port)
        logger.info("Live mic + health server ready on port %s", port)
        return True
    except (OSError, ValueError) as exc:
        logger.error("Could not bind Heroku PORT=%s: %s", raw_port, exc)
        return None

def validate_config():
    required = {"API_ID": Config.API_ID, "API_HASH": Config.API_HASH, "BOT_TOKEN": Config.BOT_TOKEN, "OWNER_ID/OWNER_IDS": Config.primary_owner()}
    missing = [k for k, v in required.items() if not v]
    if missing:
        logger.error(f"Missing required env vars: {', '.join(missing)}")
        sys.exit(1)
    logger.info(f"Log channel: {Config.LOG_CHANNEL}")

# v94: "/" menu me 60+ commands (owner wale bhi) sabko dikhte the -> naye user confuse.
# Ab users ko sirf zaroori commands, owner ko unke DM me poori list.
USER_COMMANDS = [
    BotCommand("start", "🏠 Home menu"),
    BotCommand("guide", "🧭 Setup guide — agla step kya hai"),
    BotCommand("login", "🔐 Login (number + OTP)"),
    BotCommand("help", "📘 Tutorial & commands"),
    BotCommand("mystatus", "👤 Aapka account / plan"),
    BotCommand("settings", "🎚️ Sound settings"),
    BotCommand("fightid", "⚔️ Fight ID (optional, Live Mic)"),
    BotCommand("audio", "🎵 Audio Library"),
    BotCommand("sudo", "🎁 Free Trial / Sudo (bina login)"),
    BotCommand("plans", "💎 Premium plans"),
    BotCommand("redeem", "🎟️ Coupon redeem"),
    BotCommand("referral", "👥 Refer & earn"),
    BotCommand("logout", "🚪 Logout"),
]

OWNER_COMMANDS = [
    BotCommand("owner", "👑 Owner panel"), BotCommand("users", "👥 All users"),
    BotCommand("stats", "📊 Stats"), BotCommand("broadcast", "📢 Broadcast"),
    BotCommand("restart", "♻️ Restart"), BotCommand("addaudio", "🎵 Shared audio add"),
    BotCommand("cleandb", "🧹 DB clean"), BotCommand("logtest", "🧪 Log channel test"),
    BotCommand("setlog", "📡 Log channel set"), BotCommand("setsource", "💻 Source URL set"),
    BotCommand("clearsource", "💻 Source button hatao"),
    BotCommand("addfsub", "🔒 Must-join add"), BotCommand("delfsub", "🔒 Must-join remove"),
    BotCommand("listfsub", "🔒 Must-join list"), BotCommand("clearfsub", "🔒 Must-join clear"),
    BotCommand("setprice", "💳 Plan price"), BotCommand("grant", "💳 Premium do"),
    BotCommand("revoke", "💳 Premium hatao"), BotCommand("pending", "📋 Pending payments"),
    BotCommand("addcoupon", "🎟️ Coupon banao"), BotCommand("delcoupon", "🎟️ Coupon delete"),
    BotCommand("listcoupons", "🎟️ Coupons list"), BotCommand("setqr", "💳 QR set"),
    BotCommand("clearqr", "💳 QR hatao"), BotCommand("vcjoin", "🎛 VC Joiner panel"),
    BotCommand("setvc", "🎯 Target VC add"), BotCommand("delvc", "🗑 Target VC remove"),
    BotCommand("addsudo", "👑 Sudo do: id [days]"), BotCommand("rmsudo", "👑 Sudo hatao"),
    BotCommand("sudolist", "👑 Sudo users"), BotCommand("resettrial", "🎁 Trial reset"),
]

# Purane imports ke liye (kuch plugins/tests BOT_COMMANDS padhte the).
BOT_COMMANDS = USER_COMMANDS + OWNER_COMMANDS

def _new_bot(session_string: str | None = None) -> StyledBotClient:
    # Hamesha in-memory: Heroku par file session har restart par mit jaati thi,
    # isliye har boot par naya Telegram login lagta tha. Ab login ke baad session
    # MongoDB me save hota hai (helpers/bot_login.py) aur yahan session_string se aata hai.
    kw = {"session_string": session_string} if session_string else {}
    return StyledBotClient("vcbot", api_id=Config.API_ID, api_hash=Config.API_HASH, bot_token=Config.BOT_TOKEN,
                           parse_mode=ParseMode.HTML, plugins=dict(root="plugins"), in_memory=True, **kw)


async def _check_bot_token() -> str:
    """Bot API (HTTPS) se token check: 'ok', 'invalid', 'busy' (Telegram 429/5xx) ya 'unknown'."""
    token = (Config.BOT_TOKEN or "").strip()
    if not token or ":" not in token:
        return "invalid"
    try:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=45)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.get(f"https://api.telegram.org/bot{token}/getMe") as resp:
                if resp.status in (401, 404):
                    return "invalid"
                if resp.status == 429 or resp.status >= 500:
                    return "busy"
                data = await resp.json(content_type=None)
                return "ok" if data.get("ok") else "unknown"
    except Exception:
        return "unknown"


async def _start_bot_resilient() -> StyledBotClient:
    from helpers.bot_login import start_forever
    return await start_forever(_new_bot, key="main", bot_token=Config.BOT_TOKEN, api_id=Config.API_ID,
                               token_check=_check_bot_token)


def _is_transient_start_error(exc: BaseException) -> bool:
    from helpers.bot_login import is_transient_error
    return is_transient_error(exc)

def _install_loop_guard():
    """Never let a background task die with an unretrieved exception.

    Pyrogram's update loop used to raise AuthKeyUnregistered over and over;
    those orphaned tasks piled up in memory and filled the logs.
    """
    loop = asyncio.get_running_loop()

    def handler(_loop, context):
        error = context.get("exception")
        message = context.get("message", "")
        if error is not None:
            logger.warning("Background task error (%s): %s",
                           type(error).__name__, error)
        else:
            logger.warning("Background task issue: %s", message)

    loop.set_exception_handler(handler)

async def _memory_watchdog():
    """Permanent guard against Heroku R14 (memory quota exceeded)."""
    interval = max(15, Config.MEMORY_CHECK_INTERVAL)
    while True:
        await asyncio.sleep(interval)
        try:
            used = await session_manager.enforce_memory()
            if used and used >= Config.MEMORY_HARD_MB:
                logger.error("Memory still high after cleanup: %.0f MB", used)
        except Exception as exc:
            logger.warning("memory watchdog error: %r", exc)

async def main():
    _install_loop_guard()
    validate_config()
    await db.connect()
    try:  # v94: default groups DB se (Heroku restart par mit-te the)
        from plugins.vc_commands import load_gc_from_db
        await load_gc_from_db()
    except Exception as exc:
        logger.warning("Could not load default groups: %r", exc)
    try:  # v63: .playdb/.masterdb/.ceiling survive restarts/redeploys
        from helpers.playback_v63 import load_saved_db_settings
        _owner = Config.primary_owner()
        if _owner:
            load_saved_db_settings(await db.get_settings(_owner))
    except Exception as exc:
        logger.warning("Could not load saved dB settings: %r", exc)

    # Heroku 60s me PORT bind na ho to dyno R10 se crash hota hai — isliye health
    # server sabse pehle, aur vc_sync ka Telegram login background me.
    health_server = await _start_heroku_health_server()

    async def _vc_sync_bg():
        try:
            await init_vc_sync()
        except Exception:
            pass
    globals()['_vc_sync_task'] = asyncio.get_running_loop().create_task(_vc_sync_bg())
    
    try:
        # v85: self-check runs in the background so startup is never delayed.
        from helpers.live_mic import load_base_url
        globals()['_mic_url_task'] = asyncio.get_running_loop().create_task(load_base_url())
    except Exception:
        pass
    try:
        from helpers.live_mic_v79 import load_mode as _load_mic_mode
        await _load_mic_mode()
    except Exception:
        pass

    stored_source = await db.get_app_value("source_code_url")
    if stored_source is not None:
        set_source_code_url(stored_source)

    bot = await _start_bot_resilient()
    me = await bot.get_me()
    logger.info(f"Bot started: @{me.username}")
    set_bot(bot)

    log_problem = await verify_log_channel()
    if log_problem:
        logger.error("LOG CHANNEL PROBLEM: %s", log_problem)
    else:
        logger.info("Log channel verified OK")

    restored = 0
    try:
        restored = await session_manager.restore_boot_sessions()
    except Exception as e:
        await log_error("restore_all", e)
    logger.info(f"Restored {restored} user session(s)")

    primary_owner = Config.primary_owner()
    if Config.STRING_SESSION and primary_owner not in session_manager.users:
        try:
            await session_manager.add(primary_owner, Config.STRING_SESSION)
            await db.add_user(primary_owner, "", "Owner", Config.STRING_SESSION)
            restored += 1
        except Exception as e:
            await log_error("owner_env_session", e)

    try:
        from pyrogram.types import BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats
        await bot.set_bot_commands(USER_COMMANDS, scope=BotCommandScopeAllPrivateChats())  # "/" menu sirf DM
        await bot.delete_bot_commands(scope=BotCommandScopeAllGroupChats())
    except Exception as exc:
        logger.warning("Could not set user command menu: %r", exc)
    try:
        from pyrogram.types import BotCommandScopeChat
        for oid in Config.OWNER_IDS:
            try:
                await bot.set_bot_commands(USER_COMMANDS + OWNER_COMMANDS, scope=BotCommandScopeChat(oid))
            except Exception:
                pass  # owner ne abhi bot start nahi kiya
    except Exception:
        pass

    total_users = len(await db.all_users())
    await log_startup(me.username, restored, total_users)

    try:
        db_warn = ("" if getattr(db, "_use_mongo", False) else
                   "\n\n⚠️ <b>MONGO_URI set nahi hai</b> — Heroku restart par sabke login mit jayenge "
                   "aur users ko dobara login karna padega. Config Vars me MONGO_URI daalo.")
        await bot.send_message(primary_owner, f" <b>Bot Online</b>\n├ Bot: @{me.username}\n├ Users: {total_users}\n└ Sessions restored: {restored}\n\n" + (" <b>Log channel:</b> working " if not log_problem else f" <b>Log channel problem</b>\n{log_problem}") + db_warn)
    except Exception:
        pass

    logger.info(" Running. Ctrl+C to stop.")

    async def _idle_evictor():
        interval = max(30, Config.SESSION_EVICT_INTERVAL)
        while True:
            await asyncio.sleep(interval)
            try:
                await session_manager.evict_idle_sessions()
            except Exception:
                pass

    try:
        from helpers.auto_core import start_background as start_auto_core
        start_auto_core()
    except Exception as e:
        logger.warning("auto core start failed: %r", e)

    evictor_task = asyncio.create_task(_idle_evictor())
    memory_task = asyncio.create_task(_memory_watchdog())

    await idle()

    evictor_task.cancel()
    memory_task.cancel()
    for task in (evictor_task, memory_task):
        try:
            await task
        except asyncio.CancelledError:
            pass

    logger.info("Shutting down…")
    if health_server:
        from helpers.live_mic import stop_server as stop_live_mic_server
        await stop_live_mic_server()
    await log_shutdown()
    for uid in list(session_manager.users):
        await session_manager.remove(uid)
    await bot.stop()

if __name__ == "__main__":
    asyncio.run(main())
