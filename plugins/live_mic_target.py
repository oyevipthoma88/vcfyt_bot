"""Shared target-group routing for Live Mic, .setgc and synchronized playback.

This module intentionally contains no private-source/VC-bridge behavior.
"""
import asyncio
import re
import time

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup as K

from helpers.database import db
from helpers.vc_manager import session_manager
from plugins.ui import HAS_USER, cmd_text, B

_AWAIT_TTL = 300
_await_chat: dict[int, tuple[str, float]] = {}


async def _verify_numeric_group(msg, ref: str, uid: int) -> int:
    from helpers.peer_guard import chat_id_variants
    clients = [getattr(msg, "_client", None)]
    try:
        uvc = await session_manager.get(uid)
        if uvc and getattr(uvc, "client", None):
            clients.append(uvc.client)
    except Exception:
        pass
    try:
        relay = await session_manager.get_relay(uid)
        if relay and getattr(relay, "client", None):
            clients.append(relay.client)
    except Exception:
        pass
    try:
        for relay in await session_manager.extra_relays(uid, exclude_ids=()):
            if getattr(relay, "client", None):
                clients.append(relay.client)
    except Exception:
        pass
    clients = [client for client in clients if client is not None]
    variants = chat_id_variants(ref)

    async def probe():
        for candidate in variants:
            for client in clients:
                try:
                    chat = await client.get_chat(candidate)
                    cid = getattr(chat, "id", None)
                    if cid and int(cid) < 0:
                        return int(cid)
                except Exception:
                    continue
        return 0

    found = await probe()
    if found:
        return found
    for client in clients[1:]:
        try:
            async for _ in client.get_dialogs(limit=300):
                pass
        except Exception:
            pass
    return await probe()


def _join_key(ref: str) -> str:
    ref = (ref or "").strip()
    match = re.search(r"t\.me/(?!\+|joinchat/)([A-Za-z0-9_]{4,})", ref)
    if match:
        return match.group(1)
    return ref[1:] if ref.startswith("@") else ref


async def resolve_group(msg, ref: str, uid: int, kind: str = "tgt"):
    """Resolve a target by ID, public reference, or invite using user relays."""
    ref = (ref or "").strip()
    if re.fullmatch(r"-?\d+", ref):
        return await _verify_numeric_group(msg, ref, uid), None
    from plugins.vc_commands import target_chat
    cid, join_ref = await target_chat(msg, ref)
    if cid:
        return cid, join_ref or ref
    clients = []
    try:
        relay = await session_manager.get_relay(uid)
        if relay and getattr(relay, "client", None):
            clients.append(relay.client)
    except Exception:
        pass
    try:
        user = await session_manager.get(uid)
        if user and getattr(user, "client", None):
            clients.insert(0, user.client)
    except Exception:
        pass
    key = _join_key(ref)
    invite = re.search(r"(?:joinchat/|\+)([A-Za-z0-9_-]{6,})", ref)
    for client in (clients if invite else []):
        try:
            from pyrogram import raw, utils
            result = await client.invoke(raw.functions.messages.CheckChatInvite(hash=invite.group(1)))
            chat = getattr(result, "chat", None)
            if chat is not None:
                if isinstance(chat, raw.types.Channel):
                    return utils.get_channel_id(chat.id), ref
                return -chat.id, ref
        except Exception:
            pass
    for client in clients:
        for method in ("join_chat", "get_chat"):
            try:
                chat = await getattr(client, method)(key)
                if getattr(chat, "id", None):
                    return chat.id, ref
            except Exception as exc:
                if "ALREADY" in str(exc).upper():
                    try:
                        chat = await client.get_chat(key)
                        if getattr(chat, "id", None):
                            return chat.id, ref
                    except Exception:
                        pass
    return 0, ref


async def ensure_in_target(client, target: int, ref=None) -> None:
    if not client:
        return
    try:
        await client.get_chat(target)
        return
    except Exception:
        pass
    if ref:
        try:
            await client.join_chat(_join_key(ref))
        except Exception:
            pass
        try:
            await client.get_chat(_join_key(ref))
        except Exception:
            pass
    try:
        async for _ in client.get_dialogs(limit=200):
            pass
    except Exception:
        pass


async def save_target(uid: int, cid: int, ref=None) -> None:
    await db.set_app_value(f"mic_chat_{uid}", str(cid))
    await db.set_app_value(f"mic_chat_ref_{uid}", ref or "")
    await db.set_app_value(f"mic_chat_ref_for_{uid}", str(cid) if ref else "")


async def target_ref(uid: int, cid) -> tuple:
    from helpers.peer_guard import same_chat
    ref = await db.get_app_value(f"mic_chat_ref_{uid}") or ""
    if not ref:
        return None, False
    owner = await db.get_app_value(f"mic_chat_ref_for_{uid}") or ""
    if owner:
        return (ref, True) if same_chat(owner, cid) else (None, False)
    return ref, False


async def retarget_live(uid: int, cid: int, ref=None) -> str:
    from helpers import live_mic
    from helpers.peer_guard import same_chat
    session = live_mic.get_session(uid)
    if session is None or same_chat(session.chat_id, cid):
        return ""
    await live_mic.stop_session(uid)
    return ("\n\n🔁 Naya target save ho gaya. Purana live stream safety ke liye stop kiya; "
            "naye group ke liye <code>.mic on</code> se naya link kholna.")


async def set_target(msg, uid: int, ref_text: str) -> tuple:
    cid, join_ref = await resolve_group(msg, ref_text, uid, "tgt")
    if not cid:
        return 0, ("❌ Group verify nahi hua. Numeric ID tabhi chalega jab bolne wali ID (Login / Fight ID) us group me ho; "
                   "warna invite link ya @username bhejo.")
    await save_target(uid, cid, join_ref)
    note = await retarget_live(uid, cid, join_ref)
    return cid, f"✅ Target group set: <code>{cid}</code>" + note


def request_target_input(uid: int, *, start_after: bool = False, surface: str = "chat") -> None:
    mode = "tgt_start" if start_after else ("tgt_web" if surface == "web" else
                                             "tgt_chat" if surface == "chat" else
                                             "tgt_setgc" if surface == "setgc" else
                                             "tgt_guide" if surface == "guide" else "tgt")
    try:
        from plugins.vc_commands import cancel_pending_fight_prompt
        cancel_pending_fight_prompt(int(uid))
    except Exception:
        pass
    _await_chat[int(uid)] = (mode, time.time())


@Client.on_message(HAS_USER & filters.private & filters.text
                   & ~filters.regex(r"^[/.!]"), group=4)
async def live_mic_target_listener(bot, msg):
    uid = msg.from_user.id
    entry = _await_chat.get(uid)
    if not entry:
        return
    mode, since = entry
    if time.time() - since > _AWAIT_TTL:
        _await_chat.pop(uid, None)
        await bot.send_message(msg.chat.id,
                               "⌛ Group ID prompt expire ho gaya. Dobara <code>/guide</code> ya <code>.setgc</code> bhejo.")
        return
    ref = (cmd_text(msg).strip().split()[0] if cmd_text(msg).strip() else "")
    try:
        await msg.delete()
    except Exception:
        pass
    cid, join_ref = await resolve_group(msg, ref, uid) if ref else (0, None)
    if not cid:
        await bot.send_message(msg.chat.id,
            "❌ Group verify nahi hua. Apni ID / Fight ID ko group me add karo, ya invite link/@username bhejo.",
            reply_markup=K([[B("✖ Cancel", callback_data="mic:wcancel")]]))
        return
    _await_chat.pop(uid, None)
    await save_target(uid, cid, join_ref)
    note = await retarget_live(uid, cid, join_ref)
    if mode in ("tgt_setgc", "tgt_guide"):
        from plugins.vc_commands import DEFAULT_GC, _save_gc
        DEFAULT_GC[uid] = cid
        _save_gc()
    if mode == "tgt_guide":
        # v94: Setup Guide se aaya -> wapas guide par, agla step dikhao.
        from helpers.onboarding import guide_screen
        text, kb = await guide_screen(uid, msg.from_user.first_name or "friend")
        await bot.send_message(msg.chat.id, f"✅ Group set: <code>{cid}</code>{note}\n\n" + text,
                               reply_markup=kb, disable_web_page_preview=True)
        return
    if mode == "tgt_start":
        await bot.send_message(msg.chat.id, f"✅ Live Mic target set: <code>{cid}</code>{note}")
        from plugins.web_mic import run_web_mic
        await run_web_mic(msg, "on")
        return
    from plugins.web_mic import web_mic_panel
    panel_text, keyboard = await web_mic_panel(uid)
    heading = "✅ Default group + Live Mic target saved" if mode == "tgt_setgc" else "✅ Live Mic target saved"
    await bot.send_message(msg.chat.id, f"{heading}: <code>{cid}</code>{note}\n\n" + panel_text,
                           reply_markup=keyboard)
