"""Peer resolution guard.

Pyrogram raises ChannelInvalid / PeerIdInvalid when the logged-in user account
has never "seen" a chat (in-memory sessions have no peer cache at all).
This module makes the user account resolve — and if needed auto-join — the
target chat before any voice-chat RPC is issued.
"""

import asyncio
import logging
from typing import Optional

from pyrogram import errors as _tg_errors


def _err(name: str):
    """Pyrogram version-safe error class lookup."""
    return getattr(_tg_errors, name, type(name, (Exception,), {}))


ChannelInvalid = _err("ChannelInvalid")
ChannelPrivate = _err("ChannelPrivate")
FloodWait = _err("FloodWait")
InviteHashExpired = _err("InviteHashExpired")
InviteHashInvalid = _err("InviteHashInvalid")
PeerIdInvalid = _err("PeerIdInvalid")
UserAlreadyParticipant = _err("UserAlreadyParticipant")
UserBannedInChannel = _err("UserBannedInChannel")
UsernameNotOccupied = _err("UsernameNotOccupied")

logger = logging.getLogger("vcbot.peer_guard")

_RESOLVE_ERRORS = (
    ChannelInvalid, ChannelPrivate, PeerIdInvalid, UsernameNotOccupied,
    KeyError, ValueError,
)

PEER_HELP = (
    "Aapka logged-in account is group ko access nahi kar pa raha.\n"
    "• Account ko group me add karein (ya group ka public username/invite link dein)\n"
    "• Bot ko group me admin banayein taaki wo invite link bana sake\n"
    "• Phir dobara <code>.play</code> chalayein."
)


class PeerAccessError(RuntimeError):
    """Raised when the user account cannot access the target chat at all."""


def is_peer_error(error: Exception) -> bool:
    name = type(error).__name__
    text = str(error).upper()
    return (
        name in ("ChannelInvalid", "ChannelPrivate", "PeerIdInvalid")
        or "CHANNEL_INVALID" in text
        or "PEER_ID_INVALID" in text
        or "CHANNEL_PRIVATE" in text
    )


async def _chat_reference(bot, chat_id: int) -> Optional[str]:
    """Username or fresh invite link for chat_id, using the bot account."""
    if bot is None:
        return None
    try:
        chat = await bot.get_chat(chat_id)
    except Exception as exc:
        logger.warning("bot.get_chat(%s) failed: %s", chat_id, exc)
        return None

    username = getattr(chat, "username", None)
    if username:
        return f"@{username}"

    link = getattr(chat, "invite_link", None)
    if link:
        return link

    for maker in ("create_chat_invite_link", "export_chat_invite_link"):
        try:
            result = await getattr(bot, maker)(chat_id)
            return getattr(result, "invite_link", result)
        except FloodWait as exc:
            await asyncio.sleep(int(getattr(exc, "value", 3)) + 1)
        except Exception as exc:
            logger.warning("%s(%s) failed: %s", maker, chat_id, exc)
    return None


async def _join(client, reference: str) -> bool:
    try:
        await client.join_chat(reference)
        return True
    except UserAlreadyParticipant:
        return True
    except FloodWait as exc:
        await asyncio.sleep(min(30, int(getattr(exc, "value", 5)) + 1))
        try:
            await client.join_chat(reference)
            return True
        except Exception:
            return False
    except (InviteHashExpired, InviteHashInvalid, UserBannedInChannel) as exc:
        logger.warning("join_chat(%s) rejected: %s", reference, type(exc).__name__)
        return False
    except Exception as exc:
        logger.warning("join_chat(%s) failed: %s", reference, exc)
        return False


async def ensure_peer(client, chat_id: int, bot=None, auto_join: bool = True):
    """Return a usable InputPeer for chat_id, joining the chat if required."""
    try:
        return await client.resolve_peer(chat_id)
    except _RESOLVE_ERRORS:
        pass

    # 1) Force a server-side fetch so the peer lands in the session cache.
    try:
        await client.get_chat(chat_id)
        return await client.resolve_peer(chat_id)
    except _RESOLVE_ERRORS:
        pass
    except FloodWait as exc:
        await asyncio.sleep(min(30, int(getattr(exc, "value", 5)) + 1))

    # 2) Look the chat up through dialogs (works for already-joined chats).
    try:
        async for dialog in client.get_dialogs(limit=200):
            if dialog.chat and dialog.chat.id == chat_id:
                return await client.resolve_peer(chat_id)
    except Exception:
        pass

    # 3) Auto-join using a username / invite link obtained via the bot.
    if auto_join:
        reference = await _chat_reference(bot, chat_id)
        if reference and await _join(client, reference):
            await asyncio.sleep(1.0)
            try:
                return await client.resolve_peer(chat_id)
            except _RESOLVE_ERRORS:
                try:
                    await client.get_chat(chat_id)
                    return await client.resolve_peer(chat_id)
                except _RESOLVE_ERRORS:
                    pass

    raise PeerAccessError(PEER_HELP)
