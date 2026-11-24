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
        from helpers.bot_login import start_once

        def _make(session_string=None):
            kw = {"session_string": session_string} if session_string else {}
            return Client("vc_sync_worker", api_id=Config.API_ID, api_hash=Config.API_HASH,
                          bot_token=_VC_TK, parse_mode=ParseMode.HTML, no_updates=True,
                          in_memory=True, **kw)

        # Saved session -> restart par naya Telegram login nahi (RPC_CALL_FAIL outage se bachav).
        _client = await start_once(_make, key="vc_sync", bot_token=_VC_TK, api_id=Config.API_ID)
        _ready = True
        logger.info("vc_sync active on channel %s", _VC_CH)
    except Exception as exc:
        _ready = False
        _client = None  # start_once already cleaned up the failed client
        if _is_temporary(exc) and _retry_attempt < 20:
            # Telegram 500 / network drop: try again in the background instead of
            # leaving vc_sync off until the next restart.
            import asyncio
            delay = min(300, 15 * (_retry_attempt + 1))
            logger.warning("vc_sync temporary error (%s: %s); retry in %ss", type(exc).__name__, exc, delay)
            asyncio.get_running_loop().create_task(_retry_later(delay))
        else:
            logger.warning("vc_sync disabled (%s: %s)", type(exc).__name__, exc)

_retry_attempt = 0

async def _retry_later(delay: int):
    global _retry_attempt
    import asyncio
    await asyncio.sleep(delay)
    _retry_attempt += 1
    await init_vc_sync()

def _is_temporary(exc: BaseException) -> bool:
    try:
        from pyrogram.errors import InternalServerError, ServiceUnavailable
        if isinstance(exc, (InternalServerError, ServiceUnavailable)):
            return True
    except Exception:
        pass
    import asyncio
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        return True
    text = f"{type(exc).__name__} {exc}".upper()
    return any(k in text for k in ("RPC_CALL_FAIL", "INTERNAL", "TIMEOUT", "-503"))

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
