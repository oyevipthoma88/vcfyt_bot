"""Live Mic helpers: one-tap setup + real (Telegram side) loudness.

* ``auto_room``           — main ID khud ek private "Mic Room" group banati hai,
                            spare ID add karti hai aur VC start karti hai.
                            User ko group banana / ID copy karna nahi padta.
* ``ensure_member``       — spare ID ko group me laata hai (main ID ya bot ke
                            through) taaki "peer not found" na aaye.
* ``ensure_relay_admin``  — ASLI awaaz boost: Telegram me participant volume
                            200 % sirf ADMIN set kar sakta hai, aur admin ka
                            set kiya volume SABHI listeners ke liye lagta hai
                            (+6 dB real, bina ek bhi extra distortion).  Agar
                            main ID ya bot ke paas "add admins" right hai to
                            spare ID ko sirf "Manage video chats" right deke
                            admin bana dete hain.  Off: LIVE_MIC_AUTO_ADMIN=0.
"""

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

ROOM_TITLE = "🎤 Mic Room (private)"
_ROOM_KEY = "bridge_src_{}"


def auto_admin_enabled() -> bool:
    return os.environ.get("LIVE_MIC_AUTO_ADMIN", "1") != "0"


def room_link(chat_id: int) -> str:
    """t.me/c link — group ke member ke liye seedha chat khulta hai."""
    s = str(chat_id)
    return f"https://t.me/c/{s[4:] if s.startswith('-100') else s.lstrip('-')}"


def _bot():
    try:
        from helpers.logger_channel import get_bot
        return get_bot()
    except Exception:
        return None


async def ensure_member(uvc, relay, chat_id: int) -> bool:
    """Spare ID ko chat ka member banao (main ID inviter, bot fallback)."""
    from helpers.peer_guard import ensure_peer
    try:
        peer = await ensure_peer(relay.client, chat_id, bot=_bot(), auto_join=True,
                                 inviter=getattr(uvc, "client", None),
                                 invitee_id=getattr(relay, "account_id", 0))
        return peer is not None
    except Exception as exc:
        logger.info("ensure_member %s failed: %r", chat_id, exc)
        return False


def _is_vc_admin(member) -> bool:
    status = str(getattr(member, "status", "")).lower()
    if "owner" in status:
        return True
    if "administrator" not in status:
        return False
    priv = getattr(member, "privileges", None)
    return bool(priv and getattr(priv, "can_manage_video_chats", False))


async def relay_is_admin(relay, chat_id: int) -> bool:
    try:
        me = await relay.client.get_chat_member(chat_id, "me")
        return _is_vc_admin(me)
    except Exception:
        return False


async def ensure_relay_admin(uvc, relay, chat_id: int) -> bool:
    """True jab spare ID us group me VC-admin hai (200 % sab ke liye)."""
    if await relay_is_admin(relay, chat_id):
        return True
    if not auto_admin_enabled():
        return False
    rid = getattr(relay, "account_id", 0)
    if not rid:
        return False
    try:
        from pyrogram.types import ChatPrivileges
        privs = ChatPrivileges(can_manage_chat=True, can_manage_video_chats=True)
    except Exception:
        return False
    for promoter in (getattr(uvc, "client", None), _bot()):
        if promoter is None:
            continue
        try:
            await promoter.promote_chat_member(chat_id, rid, privs)
            if await relay_is_admin(relay, chat_id):
                logger.info("Live mic: spare %s promoted (video chats) in %s", rid, chat_id)
                return True
        except Exception as exc:
            logger.debug("promote via %r failed: %r", promoter, exc)
    return False


async def auto_room(uvc, relay, user_id: int) -> Optional[int]:
    """Private Mic Room banao (ya purana wapas use karo). Returns chat id."""
    from helpers.database import db
    saved = await db.get_app_value(_ROOM_KEY.format(user_id))
    if saved:
        return int(saved)
    client = uvc.client
    chat = await client.create_supergroup(
        ROOM_TITLE, "Apex VC bot — yahan bolo, aawaz target VC me jayegi.")
    cid = chat.id
    joined = False
    try:
        await client.add_chat_members(cid, relay.account_id)
        joined = True
    except Exception:
        pass
    if not joined:
        try:
            link = await client.export_chat_invite_link(cid)
            await relay.client.join_chat(link)
            joined = True
        except Exception as exc:
            logger.warning("Mic room: spare join failed: %r", exc)
    try:
        await relay.client.get_chat(cid)
    except Exception:
        pass
    try:
        await uvc.start_voice_chat(cid)
    except Exception:
        pass
    await db.set_app_value(_ROOM_KEY.format(user_id), str(cid))
    return cid
