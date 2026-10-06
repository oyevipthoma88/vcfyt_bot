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
ChatIdInvalid = _err("ChatIdInvalid")
UserAlreadyParticipant = _err("UserAlreadyParticipant")
UserBannedInChannel = _err("UserBannedInChannel")
UsernameNotOccupied = _err("UsernameNotOccupied")

logger = logging.getLogger("vcbot.peer_guard")

_RESOLVE_ERRORS = (
    ChannelInvalid, ChannelPrivate, PeerIdInvalid, UsernameNotOccupied,
    ChatIdInvalid, KeyError, ValueError,
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
        name in ("ChannelInvalid", "ChannelPrivate", "PeerIdInvalid", "ChatIdInvalid")
        or "CHAT_ID_INVALID" in text
        or "CHANNEL_INVALID" in text
        or "PEER_ID_INVALID" in text
        or "CHANNEL_PRIVATE" in text
    )


async def _chat_reference(source, chat_id: int) -> Optional[str]:
    """Username or fresh invite link for chat_id.

    `source` can be the bot client OR any logged-in user client that is already
    a member of the chat.  Using a member account is what makes this work in
    groups where the bot was never added / is not an admin.
    """
    if source is None:
        return None
    try:
        chat = await source.get_chat(chat_id)
    except Exception as exc:
        logger.warning("get_chat(%s) failed: %s", chat_id, exc)
        return None

    username = getattr(chat, "username", None)
    if username:
        return f"@{username}"

    link = getattr(chat, "invite_link", None)
    if link:
        return link

    for maker in ("create_chat_invite_link", "export_chat_invite_link"):
        try:
            result = await getattr(source, maker)(chat_id)
            return getattr(result, "invite_link", result)
        except FloodWait as exc:
            await asyncio.sleep(int(getattr(exc, "value", 3)) + 1)
        except Exception as exc:
            logger.warning("%s(%s) failed: %s", maker, chat_id, exc)
    return None


async def _add_member(inviter, chat_id: int, user_id: int) -> bool:
    """Add user_id to chat_id using an account that is already a member.

    Normal (non-admin) members can add users when the group allows it, so this
    is the main path for "bot group me nahi hai / admin nahi hai" cases.
    """
    if inviter is None or not user_id:
        return False
    try:
        await inviter.add_chat_members(chat_id, user_id)
        return True
    except UserAlreadyParticipant:
        return True
    except FloodWait as exc:
        await asyncio.sleep(min(30, int(getattr(exc, "value", 5)) + 1))
        try:
            await inviter.add_chat_members(chat_id, user_id)
            return True
        except Exception:
            return False
    except Exception as exc:
        logger.warning("add_chat_members(%s, %s) failed: %s", chat_id, user_id, exc)
        return False


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


async def _resolve(client, chat_id: int):
    """resolve_peer with one server-side refresh, or None."""
    try:
        return await client.resolve_peer(chat_id)
    except _RESOLVE_ERRORS:
        pass
    try:
        await client.get_chat(chat_id)
        return await client.resolve_peer(chat_id)
    except _RESOLVE_ERRORS:
        return None
    except Exception:
        return None


async def ensure_peer(client, chat_id: int, bot=None, auto_join: bool = True,
                      join_ref: str = None, inviter=None, invitee_id: int = 0):
    """Return a usable InputPeer for chat_id, joining the chat if required.

    join_ref    caller-supplied username / invite link.
    inviter     a logged-in user client that is already a member of the chat
                (e.g. the owner's own account).  Used to fetch a reference and,
                as a last resort, to add `invitee_id` (the spare account) to
                the group.  This is what makes non-admin groups — and groups
                the bot was never added to — work.
    invitee_id  user id of `client`'s account, for the add-member fallback.
    """
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

    # 3) Auto-join using a username / invite link.  References are collected
    #    from the caller, then the bot, then a member account (inviter) —
    #    the member account also works when the bot is absent or not admin.
    if auto_join:
        references = []
        if join_ref:
            references.append(join_ref)
        for source in (bot, inviter):
            reference = await _chat_reference(source, chat_id)
            if reference and reference not in references:
                references.append(reference)

        for reference in references:
            if await _join(client, reference):
                await asyncio.sleep(1.0)
                peer = await _resolve(client, chat_id)
                if peer is not None:
                    return peer

        # 4) Last resort: a member account adds us directly.  No admin rights
        #    needed as long as the group allows members to add users.
        if await _add_member(inviter, chat_id, invitee_id):
            await asyncio.sleep(1.5)
            peer = await _resolve(client, chat_id)
            if peer is not None:
                return peer

    raise PeerAccessError(PEER_HELP)


def chat_id_variants(raw) -> list:
    """Possible real IDs for what the user typed.

    Users often paste a supergroup ID without the -100 prefix (e.g. 1234567890
    or -1234567890).  Pyrogram then treats it as an old basic group and calls
    messages.GetChats, which fails with [400 CHAT_ID_INVALID].
    """
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        return []
    s = str(abs(n))
    out = []
    if s.startswith("100") and len(s) >= 13:
        out.append(-int(s))
    else:
        if n < 0:
            out.append(n)
        out.append(-int("100" + s))
        if n > 0:
            out.append(-n)
    seen = []
    for v in out:
        if v not in seen:
            seen.append(v)
    return seen


async def safe_get_chat(clients, raw):
    """get_chat over several clients and ID variants; never raises."""
    candidates = chat_id_variants(raw) or [raw]
    for cand in candidates:
        for client in clients:
            if client is None:
                continue
            try:
                chat = await client.get_chat(cand)
                if chat and chat.id:
                    return chat
            except Exception as exc:
                logger.debug("get_chat(%s) failed: %s", cand, exc)
    return None
