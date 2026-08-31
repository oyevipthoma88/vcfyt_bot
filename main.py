"""
Main entry point — VC Audio Studio Bot.

  1. Connect DB
  2. Start the bot client (HTML parse mode everywhere)
  3. Wire the log channel
  4. Restore every saved user session (multi-user VC engines)
  5. Register the command menu and idle
"""

import asyncio
import logging
import sys

from pyrogram import Client, idle
from pyrogram.enums import ParseMode
from pyrogram.types import BotCommand

from config import Config
from helpers.database import db
from helpers.logger_channel import (
    log_error, log_shutdown, log_startup, set_bot, verify_log_channel,
)
from helpers.vc_manager import session_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("vcbot")


def validate_config():
    required = {
        "API_ID": Config.API_ID,
        "API_HASH": Config.API_HASH,
        "BOT_TOKEN": Config.BOT_TOKEN,
        "OWNER_ID/OWNER_IDS": Config.primary_owner(),
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        logger.error(f"Missing required env vars: {', '.join(missing)}")
        sys.exit(1)
    logger.info(f"Log channel: {Config.LOG_CHANNEL}")


BOT_COMMANDS = [
    BotCommand("start", "🏠 Home menu"),
    BotCommand("login", "🔐 Apna account login karein"),
    BotCommand("addstring", "🔑 String session add karein"),
    BotCommand("logout", "🚪 Session hataayein"),
    BotCommand("settings", "🎚️ Audio settings panel"),
    BotCommand("volume", "🎚️ Relay volume 0-400"),
    BotCommand("gain", "📈 Relay gain 0-150"),
    BotCommand("bass", "🎸 Bass 0-100"),
    BotCommand("treble", "✨ Treble 0-100"),
    BotCommand("voice", "🎤 Voice profile"),
    BotCommand("relaystatus", "📊 Relay audio status"),
    BotCommand("auto", "🤖 AUTO mode — sab automatic, max aavaj"),
    BotCommand("ultra", "🔥 Ultra loud (max volume/bass/boost/echo)"),
    BotCommand("mystatus", "👤 Aapki info"),
    BotCommand("help", "📖 Tutorial & commands"),
    BotCommand("owner", "👑 Owner panel"),
    BotCommand("users", "👥 All users (owner)"),
    BotCommand("broadcast", "📢 Broadcast (owner)"),
    BotCommand("stats", "📊 Stats (owner)"),
    BotCommand("restart", "♻️ Restart (owner)"),
    BotCommand("logtest", "🧪 Log channel test (owner)"),
    BotCommand("setlog", "🪵 Log channel set (owner)"),
]


async def main():
    validate_config()
    await db.connect()

    bot = Client(
        "vcbot",
        api_id=Config.API_ID,
        api_hash=Config.API_HASH,
        bot_token=Config.BOT_TOKEN,
        parse_mode=ParseMode.HTML,
        plugins=dict(root="plugins"),
    )
    await bot.start()
    me = await bot.get_me()
    logger.info(f"Bot started: @{me.username}")

    set_bot(bot)

    # Verify the log channel up-front. Earlier this failed silently, which is
    # why no logs ever arrived. Now the reason is printed AND sent to the owner.
    log_problem = await verify_log_channel()
    if log_problem:
        logger.error("LOG CHANNEL PROBLEM: %s", log_problem)
    else:
        logger.info("Log channel verified OK")

    # Restore all saved user sessions so logins survive restarts.
    restored = 0
    try:
        restored = await session_manager.restore_all()
    except Exception as e:
        await log_error("restore_all", e)
    logger.info(f"Restored {restored} user session(s)")

    # Owner's env STRING_SESSION (optional) — treated like a normal login.
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
        await bot.send_message(
            primary_owner,
            f"✅ <b>Bot Online</b>\n"
            f"├ Bot: @{me.username}\n"
            f"├ Users: {total_users}\n"
            f"└ Sessions restored: {restored}\n\n"
            + ("🟢 <b>Log channel:</b> working ✅"
               if not log_problem else
               f"❌ <b>Log channel problem</b>\n{log_problem}"),
        )
    except Exception:
        pass

    logger.info("✅ Running. Ctrl+C to stop.")
    await idle()

    logger.info("Shutting down…")
    await log_shutdown()
    for uid in list(session_manager.users):
        await session_manager.remove(uid)
    await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
