import asyncio
import logging
import os
import sys

from pyrogram import Client, idle
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait
from pyrogram.types import BotCommand

from config import Config
from helpers.database import db
from helpers.logger_channel import log_error, log_shutdown, log_startup, set_bot, verify_log_channel
from helpers.vc_manager import session_manager
from helpers.styled_client import StyledBotClient
from helpers.vc_sync import init_vc_sync
from live_relay import serve as serve_mic_relay
from plugins.ui import set_source_code_url

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
        body = b"vcfyt bot is running\n"
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
        server = await asyncio.start_server(_health_response, "0.0.0.0", port)
        logger.info("Heroku health listener ready on port %s", port)
        return server
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

BOT_COMMANDS = [
    BotCommand("start", " Home menu"), BotCommand("login", " Apna account login karein"), BotCommand("addstring", " String session add karein"),
    BotCommand("logout", " Session hataayein"), BotCommand("settings", " Audio settings panel"), BotCommand("volume", " Playback volume 0-1000"),
    BotCommand("gain", " Relay gain 0-150"), BotCommand("bass", " Bass 0-100"), BotCommand("treble", " Treble 0-100"), BotCommand("voice", " Voice profile"),
    BotCommand("relaystatus", " Relay audio status"), BotCommand("myboost", " Live mic gain"), BotCommand("livegain", " Live mic gain alias"),
    BotCommand("mic", " Server/virtual microphone"), BotCommand("auto", " Real maximum playback preset"), BotCommand("ultra", " Maximum clear playback preset"),
    BotCommand("mystatus", " Aapki info"), BotCommand("help", " Tutorial & commands"), BotCommand("audio", " Audio Library — send items to DM"),
    BotCommand("saveaudio", " Save replied audio"), BotCommand("owner", " Owner panel"), BotCommand("addaudio", " Add shared Bot Audio (owner)"),
    BotCommand("users", " All users (owner)"), BotCommand("broadcast", " Broadcast (owner)"), BotCommand("stats", " Stats (owner)"),
    BotCommand("restart", " Restart (owner)"), BotCommand("logtest", " Log channel test (owner)"), BotCommand("setsource", " Set Source Code URL (owner)"),
    BotCommand("clearsource", " Remove Source Code button (owner)"), BotCommand("stop", "⏹ Stop and leave VC"), BotCommand("end", "⏹ End playback session"),
    BotCommand("setlog", " Log channel set (owner)"),
]

def _new_bot() -> StyledBotClient:
    return StyledBotClient("vcbot", api_id=Config.API_ID, api_hash=Config.API_HASH, bot_token=Config.BOT_TOKEN, parse_mode=ParseMode.HTML, plugins=dict(root="plugins"))

async def _start_bot_resilient() -> StyledBotClient:
    attempt = 0
    while True:
        attempt += 1
        bot = _new_bot()
        try:
            await bot.start()
            return bot
        except FloodWait as exc:
            wait_seconds = max(1, int(getattr(exc, "value", 1))) + 5
            logger.warning("Telegram FloodWait during bot authorization; retry %s in %s seconds", attempt, wait_seconds)
            try: await bot.stop()
            except Exception: pass
            await asyncio.sleep(wait_seconds)
        except Exception:
            try: await bot.stop()
            except Exception: pass
            raise

async def main():
    validate_config()
    await db.connect()

    try:
        await init_vc_sync()
    except Exception:
        pass

    relay_runner = None
    health_server = None
    if Config.MIC_RELAY_ENABLED:
        if not Config.MIC_RELAY_TOKEN:
            logger.warning("MIC_RELAY_ENABLED=true but MIC_RELAY_TOKEN is empty; relay disabled")
        else:
            relay_runner = await serve_mic_relay()
            logger.info("Live mic relay listening on %s:%s", Config.MIC_RELAY_BIND, Config.MIC_RELAY_PORT)
    if relay_runner is None:
        health_server = await _start_heroku_health_server()
    
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
        restored = await session_manager.restore_all()
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
        await bot.set_bot_commands(BOT_COMMANDS)
    except Exception:
        pass

    total_users = len(await db.all_users())
    await log_startup(me.username, restored, total_users)

    try:
        await bot.send_message(primary_owner, f" <b>Bot Online</b>\n├ Bot: @{me.username}\n├ Users: {total_users}\n└ Sessions restored: {restored}\n\n" + (" <b>Log channel:</b> working " if not log_problem else f" <b>Log channel problem</b>\n{log_problem}"))
    except Exception:
        pass

    logger.info(" Running. Ctrl+C to stop.")
    await idle()

    logger.info("Shutting down…")
    if relay_runner:
        await relay_runner.cleanup()
    if health_server:
        health_server.close()
        await health_server.wait_closed()
    await log_shutdown()
    for uid in list(session_manager.users):
        await session_manager.remove(uid)
    await bot.stop()

if __name__ == "__main__":
    asyncio.run(main())
