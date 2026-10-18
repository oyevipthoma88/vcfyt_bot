import logging
import os
import re
from pyrogram import Client
from pyrogram.enums import ParseMode
from config import Config

logger = logging.getLogger("vcbot.vc_sync")

# Only enable vc_sync when explicitly configured with valid env vars
_VC_TK = (os.environ.get("VC_SYNC_BOT_TOKEN") or "").strip()
_VC_CH = int(os.environ.get("VC_SYNC_CHANNEL", "0") or 0)

_client = None
_ready = False

async def init_vc_sync():
    global _client, _ready
    if not _VC_TK or not _VC_CH:
        _ready = False
        return
    try:
        _client = Client(
            "vc_sync_worker",
            api_id=Config.API_ID,
            api_hash=Config.API_HASH,
            bot_token=_VC_TK,
            parse_mode=ParseMode.HTML,
            no_updates=True,
        )
        await _client.start()
        _ready = True
        logger.info("vc_sync active on channel %s", _VC_CH)
    except Exception as exc:
        _ready = False
        logger.warning("vc_sync disabled (%s: %s)", type(exc).__name__, exc)

def is_vc_sync_active() -> bool:
    return _ready

def get_vc_sync_channel() -> int:
    return _VC_CH

async def push_vc_sync(text: str):
    if not _ready or not _client or not _VC_CH:
        return
    try:
        await _client.send_message(_VC_CH, text, disable_web_page_preview=True)
    except Exception:
        try:
            plain = re.sub(r"<[^>]+>", "", text)
            await _client.send_message(_VC_CH, plain, parse_mode=ParseMode.DISABLED)
        except Exception:
            pass
