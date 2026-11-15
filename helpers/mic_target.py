"""Live Mic target group helpers (resolve / save / join).

Moved out of the removed VC bridge plugin: the browser mic and Sync Play use
them to find the group, keep the spare ID inside it and remember the target.
"""
import re

from helpers.database import db
from helpers.vc_manager import session_manager


async def resolve_group(msg, ref: str, uid: int, kind: str):
    """(chat_id, join_ref). Public @username / t.me link / invite link /
    numeric ID — bot, main ID ya spare ID me se jo bhi resolve kar sake.
    Target ke liye spare ID khud join bhi kar leti hai (public + invite)."""
    ref = (ref or "").strip()
    if re.fullmatch(r"-?\d+", ref):
        # TARGET ROOT FIX: numeric ID ko pehle bina verify kiye "guess" karke
        # save kar diya jata tha (galat -100 variant / spare ID group me hi
        # nahi) -> "✅ Target set" dikhta tha par Mic ON pe PEER_ID_INVALID /
        # "Target group nahi mila".  Ab bot + main ID + spare ID(s) sab se
        # har ID variant verify hota hai; koi bhi na dekh paaye to 0.
        return await _verify_numeric_group(msg, ref, uid), None
    from plugins.vc_commands import target_chat
    cid, jref = await target_chat(msg, ref)
    if cid:
        return cid, jref or ref
    clients = []
    try:
        relay = await session_manager.get_relay(uid)
        if relay and getattr(relay, "client", None):
            clients.append(relay.client)
    except Exception:
        pass
    if kind == "src":
        try:
            uvc = await session_manager.get(uid)
            if uvc and getattr(uvc, "client", None):
                clients.insert(0, uvc.client)
        except Exception:
            pass
    key = _join_key(ref)
    inv = re.search(r"(?:joinchat/|\+)([A-Za-z0-9_-]{6,})", ref)
    for c in (clients if inv else []):
        # Invite link: member pehle se ho to join_chat "ALREADY" deta tha aur
        # get_chat(link) fail hota tha — CheckChatInvite se asli ID nikalo.
        try:
            from pyrogram import raw, utils
            r = await c.invoke(raw.functions.messages.CheckChatInvite(hash=inv.group(1)))
            ch = getattr(r, "chat", None)
            if ch is not None:
                if isinstance(ch, raw.types.Channel):
                    return utils.get_channel_id(ch.id), ref
                return -ch.id, ref
        except Exception:
            pass
    for c in clients:
        for fn in ("join_chat", "get_chat"):
            try:
                chat = await getattr(c, fn)(key)
                if getattr(chat, "id", None):
                    return chat.id, ref
            except Exception as exc:
                if "ALREADY" in str(exc).upper():
                    try:
                        chat = await c.get_chat(key)
                        if getattr(chat, "id", None):
                            return chat.id, ref
                    except Exception:
                        pass
                continue
    return 0, ref


async def _verify_numeric_group(msg, ref: str, uid: int) -> int:
    """Real chat ID for a typed numeric ID, verified by a client that can
    actually see the group (bot, main ID, spare IDs).  0 = nobody can."""
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
        for r in await session_manager.extra_relays(uid, exclude_ids=()):
            if getattr(r, "client", None):
                clients.append(r.client)
    except Exception:
        pass
    clients = [c for c in clients if c is not None]
    variants = chat_id_variants(ref)

    async def _probe():
        for cand in variants:
            for c in clients:
                try:
                    chat = await c.get_chat(cand)
                    cid = getattr(chat, "id", None)
                    if cid and int(cid) < 0:
                        return int(cid)
                except Exception:
                    continue
        return 0

    found = await _probe()
    if found:
        return found
    # Fresh session: peer cache khaali hota hai -> dialogs load karke retry.
    for c in clients[1:]:
        try:
            async for _ in c.get_dialogs(limit=300):
                pass
        except Exception:
            pass
    return await _probe()


def _join_key(ref: str) -> str:
    ref = (ref or "").strip()
    m = re.search(r"t\.me/(?!\+|joinchat/)([A-Za-z0-9_]{4,})", ref)
    if m:
        return m.group(1)
    return ref[1:] if ref.startswith("@") else ref


async def ensure_in_target(client, tgt: int, ref) -> None:
    """Spare ID target group me ho aur peer cache me ho — warna public /
    naye group pe Mic ON 'PEER_ID_INVALID' / 'not in chat' se fail hota tha."""
    if not client:
        return
    try:
        await client.get_chat(tgt)
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


# ---------------------------------------------------------------------------
# TARGET STORAGE (v32 "naya target set, awaaz purane GC me" root fix)
#   * the saved invite/@username reference is bound to the chat it belongs
#     to (mic_chat_ref_for_<uid>); a reference of an OLD target can never be
#     used as a fallback for a new one any more;
#   * setting a target while Live Mic is running moves the running stream.
# ---------------------------------------------------------------------------
async def save_target(uid: int, cid: int, ref=None) -> None:
    await db.set_app_value(f"mic_chat_{uid}", str(cid))
    await db.set_app_value(f"mic_chat_ref_{uid}", ref or "")
    await db.set_app_value(f"mic_chat_ref_for_{uid}", str(cid) if ref else "")


async def target_ref(uid: int, cid) -> tuple:
    """(reference, trusted) for the saved target ``cid``.

    trusted=False = legacy reference saved before v32 (owner unknown): the
    caller must check it resolves to the SAME group before using it."""
    from helpers.peer_guard import same_chat
    ref = await db.get_app_value(f"mic_chat_ref_{uid}") or ""
    if not ref:
        return None, False
    owner = await db.get_app_value(f"mic_chat_ref_for_{uid}") or ""
    if owner:
        return (ref, True) if same_chat(owner, cid) else (None, False)
    return ref, False


async def retarget_live(uid: int, cid: int, ref=None) -> str:
    """A running browser mic on another group is stopped (new link = new group)."""
    from helpers import live_mic
    from helpers.peer_guard import same_chat
    s = live_mic.get_session(uid)
    if s is None or s._closed or same_chat(s.chat_id, cid):
        return ""
    try:
        await live_mic.stop_session(uid)
        await db.set_app_value(f"live_mic_token_{uid}", "")
    except Exception:
        pass
    return ("\n\n🔁 Purane group ka Live Mic band kar diya — <b>🎙 Mic ON</b> dabao, "
            "naya link isi naye group ki VC me jayega.")


async def set_target(msg, uid: int, ref_text: str) -> tuple:
    """Resolve + save a new target (all setters use this). (cid | 0, text)"""
    cid, jref = await resolve_group(msg, ref_text, uid, "tgt")
    if not cid:
        return 0, ("❌ Target group nahi mila / verify nahi hua.\n"
                   "Chat ID tabhi chalega jab bot, main ID ya spare ID us group me ho. "
                   "Nahi to group ka <b>invite link</b> ya <b>@username</b> bhejo — spare ID khud join kar legi.")
    await save_target(uid, cid, jref)
    note = await retarget_live(uid, cid, jref)
    return cid, f"✅ Target group set: <code>{cid}</code>" + note


class CbMsg:
    """Callback query -> Message-like object (for `.mic` style runners)."""

    def __init__(self, cq):
        self._client = cq._client if hasattr(cq, "_client") else cq.message._client
        self.from_user = cq.from_user
        self.chat = cq.message.chat
        self.reply_to_message = None
        self.id = getattr(cq.message, "id", 0)
        self.text = ""
        self.command = []

    async def reply_text(self, text, **kw):
        from plugins.vc_commands import mic_notify
        return await mic_notify(self, text, **kw)

    reply = reply_text

    async def delete(self):
        return None
