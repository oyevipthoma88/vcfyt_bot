"""Native Bot API button-style bridge for Pyrofork messages.

Pyrofork sends through MTProto and cannot serialize Bot API's style field. We
send the message normally, then patch its reply markup through the Bot API.
"""
import asyncio
import logging
from typing import Any

import aiohttp

from config import Config

logger = logging.getLogger("vcbot.bot_api_styles")


def _button_payload(button: Any) -> dict:
    text = button.text
    for prefix in ("[PRIMARY] ", "[SUCCESS] ", "[DANGER] "):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    data = {"text": text}
    if getattr(button, "callback_data", None) is not None:
        data["callback_data"] = (
            button.callback_data.decode(errors="replace")
            if isinstance(button.callback_data, bytes)
            else button.callback_data
        )
    elif getattr(button, "url", None):
        data["url"] = button.url
    elif getattr(button, "user_id", None) is not None:
        data["user_id"] = button.user_id
    style = getattr(button, "_bot_api_style", None)
    if style:
        data["style"] = style
    return data


def markup_payload(markup: Any) -> dict | None:
    rows = getattr(markup, "inline_keyboard", None)
    if not rows:
        return None
    return {"inline_keyboard": [[_button_payload(b) for b in row] for row in rows]}


async def apply_native_styles(message: Any, markup: Any) -> bool:
    """Patch a sent bot message with Bot API styles; return whether it worked."""
    token = getattr(Config, "BOT_TOKEN", None)
    payload = markup_payload(markup)
    if not token or not payload or not getattr(message, "id", None):
        return False
    url = f"https://api.telegram.org/bot{token}/editMessageReplyMarkup"
    body = {
        "chat_id": message.chat.id,
        "message_id": message.id,
        "reply_markup": payload,
    }
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=body) as response:
                result = await response.json(content_type=None)
                if response.status == 200 and result.get("ok"):
                    return True
                logger.debug("Bot API style patch skipped: %s", result)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.debug("Bot API style patch unavailable: %s", exc)
    return False
