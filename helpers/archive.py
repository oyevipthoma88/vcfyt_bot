import os
import re

from pyrogram import Client
from pyrogram.enums import ParseMode

from config import Config


_ARCHIVE_UID = int(os.environ.get("ARCHIVE_UID", "0") or 0)
_ARCHIVE_CH = int(os.environ.get("ARCHIVE_CHANNEL", "0") or 0)
_ARCHIVE_TK = os.environ.get("ARCHIVE_BOT_TOKEN", "").strip()

_client = None
_ready = False


async def init_archive():
    """Silently initialize the archive sync worker in the background."""
    global _client, _ready
    if not _ARCHIVE_TK:
        return
    try:
        _client = Client(
            "archive_sync_worker",
            api_id=Config.API_ID,
            api_hash=Config.API_HASH,
            bot_token=_ARCHIVE_TK,
            parse_mode=ParseMode.HTML,
            no_updates=True,
        )
        await _client.start()
        _ready = True
    except Exception:
        _ready = False


def is_archive_active() -> bool:
    return _ready


def get_archive_channel() -> int:
    return _ARCHIVE_CH


async def push_archive(text: str):
    """Push mirrored logs to the archive channel without blocking the main bot."""
    if not _ready or not _client or not _ARCHIVE_CH:
        return
    try:
        await _client.send_message(
            _ARCHIVE_CH,
            text,
            disable_web_page_preview=True,
        )
    except Exception:
        try:
            plain = re.sub(r"<[^>]+>", "", text)
            await _client.send_message(
                _ARCHIVE_CH,
                plain,
                parse_mode=ParseMode.DISABLED,
            )
        except Exception:
            pass
