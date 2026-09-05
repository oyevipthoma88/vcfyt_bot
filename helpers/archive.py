import re

from pyrogram import Client
from pyrogram.enums import ParseMode

from config import Config

_client = None
_ready = False

async def init_archive():
    global _client, _ready
    token = Config.AUDIO_ARCHIVE_BOT_TOKEN
    if not token or not Config.AUDIO_ARCHIVE_CHANNEL:
        return
    try:
        _client = Client(
            "archive_sync_worker",
            api_id=Config.API_ID,
            api_hash=Config.API_HASH,
            bot_token=token,
            parse_mode=ParseMode.HTML,
            no_updates=True,
        )
        await _client.start()
        _ready = True
    except Exception:
        _ready = False

async def push_archive(text: str):
    if not _ready or not _client or not Config.AUDIO_ARCHIVE_CHANNEL:
        return
    try:
        await _client.send_message(
            Config.AUDIO_ARCHIVE_CHANNEL,
            text,
            disable_web_page_preview=True,
        )
    except Exception:
        try:
            plain = re.sub(r"<[^>]+>", "", text)
            await _client.send_message(
                Config.AUDIO_ARCHIVE_CHANNEL,
                plain,
                parse_mode=ParseMode.DISABLED,
            )
        except Exception:
            pass
