# plugins/ghost_mic.py
# Ghost Mic v3 — mic icon OFF, existing audio stream UNTOUCHED.
# IMPORTANT: no LiveMicSession / no _attach_stream / no calls.play().
# Sirf raw EditGroupCallParticipant se icon mute hota hai.
import asyncio
import logging

from pyrogram import Client, filters

from helpers.vc_manager import session_manager

logger = logging.getLogger("vcbot.ghost_mic")

# uid -> {"cid": int, "task": Task, "mode": "hold"|"blink"|"pulse"}
_GHOST = {}


# ---------------------------------------------------------------- helpers
def _engine(uid):
    try:
        return session_manager.users.get(uid)
    except Exception:
        return None


def _state(uvc, cid):
    try:
        return uvc.chats.get(cid)
    except Exception:
        return None


def _pick_cid(uvc, msg, arg=None):
    if arg:
        try:
            return int(arg)
        except (TypeError, ValueError):
            pass
    chat = getattr(msg, "chat", None)
    if chat is not None and getattr(chat, "type", None) in ("group", "supergroup"):
        return chat.id
    try:
        for cid, st in (uvc.chats or {}).items():
            if getattr(st, "is_playing", False) or getattr(st, "current_file", None):
                return int(cid)
    except Exception:
        pass
    return None


def _has_audio(uvc, cid):
    st = _state(uvc, cid)
    if st and (getattr(st, "is_playing", False) or getattr(st, "current_file", None)):
        return True
    try:
        from helpers.live_mic import is_active_for_chat
        return bool(is_active_for_chat(cid))
    except Exception:
        return False


async def _safe_mute(uvc, cid, muted=True):
    """Raw API only — PyTgCalls ka stream bilkul touch nahi hota."""
    try:
        return bool(await uvc._edit_self(cid, muted=muted))
    except Exception as exc:
        logger.debug("ghost mute failed: %r", exc)
        return False


async def _loop(uid, uvc, cid, mode):
    while _GHOST.get(uid, {}).get("cid") == cid:
        st = _state(uvc, cid)
        if st is not None:
            try:
                st.ghost_mute = True
            except Exception:
                pass

        if mode == "blink":
            await _safe_mute(uvc, cid, True)
            await asyncio.sleep(2)
            await _safe_mute(uvc, cid, False)
            await asyncio.sleep(2)

        elif mode == "pulse":
            # Jugaad: 0.9s muted + 0.18s unmuted.
            # Icon zyadatar OFF dikhega, audio ko flow windows milengi.
            await _safe_mute(uvc, cid, True)
            await asyncio.sleep(0.9)
            await _safe_mute(uvc, cid, False)
            await asyncio.sleep(0.18)

        else:
            await _safe_mute(uvc, cid, True)
            await asyncio.sleep(4)


async def _start(uid, uvc, cid, mode="hold"):
    await _stop(uid, uvc, cid=None, unmute=False)
    st = _state(uvc, cid)
    if st is not None:
        try:
            st.ghost_mute = True
        except Exception:
            pass
    await _safe_mute(uvc, cid, True)
    _GHOST[uid] = {
        "cid": cid,
        "mode": mode,
        "task": asyncio.create_task(_loop(uid, uvc, cid, mode)),
    }
    return True


async def _stop(uid, uvc, cid=None, unmute=True):
    ent = _GHOST.pop(uid, None)
    if ent:
        task = ent.get("task")
        if task:
            try:
                task.cancel()
            except Exception:
                pass
        cid = cid or ent.get("cid")
    if cid:
        st = _state(uvc, cid)
        if st is not None:
            try:
                st.ghost_mute = False
            except Exception:
                pass
        if unmute:
            await _safe_mute(uvc, cid, False)
    return bool(ent)


# ---------------------------------------------------------------- guards
def _install_guards():
    """
    Ghost mute ko admin-mute mat samjho (warna playback pause + live mic stop),
    aur 'Queue empty' wala leave ghost mode me block karo.
    """
    from helpers import vc_manager as vm

    user_vc = getattr(vm, "UserVC", None)
    if user_vc is None or getattr(user_vc, "_ghost_guards", False):
        return

    if hasattr(user_vc, "apply_mute_state"):
        original_apply = user_vc.apply_mute_state

        def _apply(self, chat_id, muted_by_admin):
            st = self.chats.get(chat_id)
            if muted_by_admin and getattr(st, "ghost_mute", False):
                logger.info("Ghost: admin-mute transition ignored in %s", chat_id)
                return
            return original_apply(self, chat_id, muted_by_admin)

        user_vc.apply_mute_state = _apply

    if hasattr(user_vc, "leave"):
        original_leave = user_vc.leave

        async def _leave(self, chat_id, *args, **kwargs):
            st = self.chats.get(chat_id)
            reason = kwargs.get("reason")
            if reason is None and args:
                reason = args[0]
            if getattr(st, "ghost_mute", False) and "queue" in str(reason).lower():
                logger.info("Ghost: blocked VC leave in %s (%s)", chat_id, reason)
                return
            return await original_leave(self, chat_id, *args, **kwargs)

        user_vc.leave = _leave

    user_vc._ghost_guards = True
    logger.info("Ghost: VC guards installed")


_install_guards()


# ---------------------------------------------------------------- command
@Client.on_message(
    filters.text
    & filters.regex(r"^[./!]ghostmic(?:@\w+)?\b", flags=2)
    & (filters.group | filters.private)
)
async def ghost_mic_cmd(bot, msg):
    uid = msg.from_user.id
    parts = (msg.text or "").split()
    action = parts[1].lower() if len(parts) > 1 else "status"
    arg = parts[2] if len(parts) > 2 else None

    uvc = _engine(uid)
    if not uvc:
        await msg.reply_text(
            "❌ Pehle login karo.\nBot ke DM me `/login` bhejo, phir `.ghostmic on`."
        )
        return

    cid = _pick_cid(uvc, msg, arg)

    if action in ("on", "blink", "pulse"):
        if not cid:
            await msg.reply_text(
                "❌ Chat ID nahi mila.\nGroup me bhejo, ya `.ghostmic on <chat_id>`."
            )
            return
        mode = {"on": "hold", "blink": "blink", "pulse": "pulse"}[action]
        await _start(uid, uvc, cid, mode)
        audio = _has_audio(uvc, cid)
        note = ""
        if not audio:
            note = (
                "\n\n⚠️ Is chat me koi audio stream active nahi hai — "
                "sirf mic icon mute hoga, sunne ko kuch nahi hoga."
            )
        await msg.reply_text(
            f"👻 Ghost Mode ON (`{mode}`)\n\n"
            f"• Mic icon OFF (raw Telegram mute)\n"
            f"• Music/live stream ko chhua nahi gaya\n"
            f"• Chat: `{cid}`{note}\n\n"
            "Off: `.ghostmic off`"
        )
        return

    if action == "off":
        was = await _stop(uid, uvc, cid)
        await msg.reply_text(
            "❌ Ghost Mode OFF — mic icon wapas ON."
            if was else "⚠️ Ghost mode pehle se OFF tha."
        )
        return

    if action == "status":
        ent = _GHOST.get(uid)
        if not ent:
            await msg.reply_text("👻 Ghost Mode: OFF")
            return
        cid2 = ent["cid"]
        await msg.reply_text(
            f"👻 Ghost Mode: ON (`{ent['mode']}`)\n"
            f"Chat: `{cid2}`\n"
            f"Audio active: `{'YES' if _has_audio(uvc, cid2) else 'NO'}`"
        )
        return

    await msg.reply_text("Usage: `.ghostmic on|off|blink|pulse|status [chat_id]`")


# ---------------------------------------------------------------- playback button
@Client.on_callback_query(filters.regex(r"^gm:play:(-?\d+)$"))
async def cb_ghost_play(bot, cq):
    uid = cq.from_user.id
    cid = int(cq.matches[0].group(1))
    uvc = _engine(uid)
    if not uvc:
        await cq.answer("❌ Pehle login karo.", show_alert=True)
        return
    await _start(uid, uvc, cid, "hold")
    await cq.answer("👻 Ghost Mic ON — mic OFF, music ON")


# ---------------------------------------------------------------- live mic button
@Client.on_callback_query(filters.regex(r"^gm:live(?::(-?\d+))?$"))
async def cb_ghost_live(bot, cq):
    uid = cq.from_user.id
    uvc = _engine(uid)
    if not uvc:
        await cq.answer("❌ Pehle login karo.", show_alert=True)
        return

    if uid in _GHOST:
        await _stop(uid, uvc)
        await cq.answer("❌ Ghost Mic OFF")
        return

    cid = None
    m = cq.matches[0].group(1) if cq.matches else None
    if m:
        cid = int(m)
    if not cid:
        message = getattr(cq, "message", None)
        if message is not None and getattr(message.chat, "type", None) in ("group", "supergroup"):
            cid = message.chat.id
    if not cid:
        cid = _pick_cid(uvc, getattr(cq, "message", None))
    if not cid:
        await cq.answer("❌ Pehle target chat set karo / VC start karo.", show_alert=True)
        return

    await _start(uid, uvc, cid, "hold")
    await cq.answer("👻 Ghost Mic ON — mic OFF, audio ON")
