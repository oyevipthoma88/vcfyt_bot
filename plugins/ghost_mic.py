# plugins/ghost_mic.py
# Ghost Mic v4 — mic icon steady OFF, audio stream untouched.
# No LiveMicSession / no calls.play(). Sirf raw EditGroupCallParticipant.
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
    """Ghost mode me mute/unmute/blink/leave — sab ignore karo."""
    from helpers import vc_manager as vm

    user_vc = getattr(vm, "UserVC", None)
    if user_vc is None or getattr(user_vc, "_ghost_guards", False):
        return

    def _ghost_active(self, chat_id):
        st = self.chats.get(chat_id)
        return bool(st and getattr(st, "ghost_mute", False))

    if hasattr(user_vc, "apply_mute_state"):
        original_apply = user_vc.apply_mute_state

        def _apply(self, chat_id, muted_by_admin):
            if _ghost_active(self, chat_id):
                logger.info("Ghost: transition ignored in %s", chat_id)
                return
            return original_apply(self, chat_id, muted_by_admin)

        user_vc.apply_mute_state = _apply

    if hasattr(user_vc, "handle_admin_mute"):
        original_mute = user_vc.handle_admin_mute

        async def _hm(self, chat_id):
            if _ghost_active(self, chat_id):
                return
            return await original_mute(self, chat_id)

        user_vc.handle_admin_mute = _hm

    if hasattr(user_vc, "handle_admin_unmute"):
        original_unmute = user_vc.handle_admin_unmute

        async def _hu(self, chat_id):
            if _ghost_active(self, chat_id):
                return
            return await original_unmute(self, chat_id)

        user_vc.handle_admin_unmute = _hu

    if hasattr(user_vc, "_mic_blink"):
        original_blink = user_vc._mic_blink

        async def _blink(self, chat_id, seconds):
            if _ghost_active(self, chat_id):
                return
            return await original_blink(self, chat_id, seconds)

        user_vc._mic_blink = _blink

    if hasattr(user_vc, "leave"):
        original_leave = user_vc.leave

        async def _leave(self, chat_id, *args, **kwargs):
            st = self.chats.get(chat_id)
            reason = kwargs.get("reason")
            if reason is None and args:
                reason = args[0]
            if getattr(st, "ghost_mute", False) and "queue" in str(reason).lower():
                logger.info("Ghost: blocked leave in %s", chat_id)
                return
            return await original_leave(self, chat_id, *args, **kwargs)

        user_vc.leave = _leave

    user_vc._ghost_guards = True
    logger.info("Ghost: guards installed (mute/unmute/blink/leave)")


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
        note = "" if audio else (
            "\n\n⚠️ Is chat me koi audio active nahi hai — "
            "sirf mic icon mute hoga, sunne ko kuch nahi hoga."
        )
        await msg.reply_text(
            f"👻 Ghost Mode ON (`{mode}`)\n\n"
            f"• Mic icon steady OFF (raw Telegram mute)\n"
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
