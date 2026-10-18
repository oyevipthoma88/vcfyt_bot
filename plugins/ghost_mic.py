# plugins/ghost_mic.py
# Ghost Mic v6 — ChatState.ghost_mute patch + mic_blink force-off + _self_unmute block.
import asyncio
import logging

from pyrogram import Client, filters

from helpers.vc_manager import session_manager

logger = logging.getLogger("vcbot.ghost_mic")

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


def _is_ghost_chat(cid):
    if cid is None:
        return False
    for ent in _GHOST.values():
        if ent.get("cid") == cid:
            return True
    return False


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
                st.mic_blink = False   # v6: blink hamesha off ghost me
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
            await asyncio.sleep(30)
            await _safe_mute(uvc, cid, True)


async def _start(uid, uvc, cid, mode="hold"):
    await _stop(uid, uvc, cid=None, unmute=False)

    st = _state(uvc, cid)
    if st is not None:
        try:
            st.ghost_mute = True
            st.mic_blink = False   # v6: blink band
        except Exception:
            pass

    _GHOST[uid] = {"cid": cid, "mode": mode, "task": None}
    await _safe_mute(uvc, cid, True)
    _GHOST[uid]["task"] = asyncio.create_task(_loop(uid, uvc, cid, mode))
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


# ---------------------------------------------------------------- HARD GUARDS
def _install_guards():
    from helpers import vc_manager as vm

    user_vc = getattr(vm, "UserVC", None)
    chat_state = getattr(vm, "ChatState", None)
    if user_vc is None:
        return

    # ---- 0) ChatState.ghost_mute attribute add ----
    if chat_state is not None and not getattr(chat_state, "_ghost_v6", False):
        _orig_init = chat_state.__init__

        def _init_v6(self, *args, **kwargs):
            _orig_init(self, *args, **kwargs)
            self.ghost_mute = False
            self.mic_blink = False  # v6 default: blink off

        chat_state.__init__ = _init_v6
        chat_state._ghost_v6 = True

    if getattr(user_vc, "_ghost_v6_guards", False):
        return

    def _active(self, chat_id):
        st = self.chats.get(chat_id) if hasattr(self, "chats") else None
        if not st:
            return False
        if getattr(st, "ghost_mute", False):
            return True
        return _is_ghost_chat(chat_id)

    # ---- 1) _edit_self force-mute ----
    if hasattr(user_vc, "_edit_self"):
        original_edit = user_vc._edit_self

        async def _edit_self_v6(self, chat_id, *args, **kwargs):
            if _active(self, chat_id):
                if args:
                    args = (True,) + args[1:]
                else:
                    kwargs["muted"] = True
            return await original_edit(self, chat_id, *args, **kwargs)

        user_vc._edit_self = _edit_self_v6

    # ---- 2) apply_mute_state dono directions block ----
    if hasattr(user_vc, "apply_mute_state"):
        orig_apply = user_vc.apply_mute_state

        def _apply_v6(self, chat_id, muted_by_admin):
            if _active(self, chat_id):
                logger.info("Ghost v6: apply_mute_state blocked %s", chat_id)
                return
            return orig_apply(self, chat_id, muted_by_admin)

        user_vc.apply_mute_state = _apply_v6

    # ---- 3) handle_admin_unmute block (mic_blink yahin se aata hai) ----
    if hasattr(user_vc, "handle_admin_unmute"):
        orig_hu = user_vc.handle_admin_unmute

        async def _hu_v6(self, chat_id):
            if _active(self, chat_id):
                logger.info("Ghost v6: handle_admin_unmute blocked %s", chat_id)
                return
            return await orig_hu(self, chat_id)

        user_vc.handle_admin_unmute = _hu_v6

    # ---- 4) handle_admin_mute block ----
    if hasattr(user_vc, "handle_admin_mute"):
        orig_hm = user_vc.handle_admin_mute

        async def _hm_v6(self, chat_id):
            if _active(self, chat_id):
                return
            return await orig_hm(self, chat_id)

        user_vc.handle_admin_mute = _hm_v6

    # ---- 5) _mic_blink block ----
    if hasattr(user_vc, "_mic_blink"):
        orig_blink = user_vc._mic_blink

        async def _blink_v6(self, chat_id, *args, **kwargs):
            if _active(self, chat_id):
                logger.info("Ghost v6: _mic_blink blocked %s", chat_id)
                return
            return await orig_blink(self, chat_id, *args, **kwargs)

        user_vc._mic_blink = _blink_v6

    # ---- 6) _self_unmute block ----
    if hasattr(user_vc, "_self_unmute"):
        orig_unmute = user_vc._self_unmute

        async def _su_v6(self, chat_id):
            if _active(self, chat_id):
                logger.info("Ghost v6: _self_unmute blocked %s", chat_id)
                return
            return await orig_unmute(self, chat_id)

        user_vc._self_unmute = _su_v6

    # ---- 7) LiveMicSession.set_tg_mute force-mute ----
    try:
        from helpers.live_mic import LiveMicSession
        if not getattr(LiveMicSession, "_ghost_v6_guards", False):
            original_set_tg = LiveMicSession.set_tg_mute

            async def _set_tg_v6(self, muted):
                cid = getattr(self, "chat_id", None) or getattr(self, "cid", None)
                if cid is not None and _is_ghost_chat(cid):
                    muted = True
                return await original_set_tg(self, muted)

            LiveMicSession.set_tg_mute = _set_tg_v6
            LiveMicSession._ghost_v6_guards = True
    except Exception as exc:
        logger.warning("Ghost v6: LiveMicSession guard failed: %s", exc)

    # ---- 8) leave guard ----
    if hasattr(user_vc, "leave"):
        orig_leave = user_vc.leave

        async def _leave_v6(self, chat_id, *args, **kwargs):
            st = self.chats.get(chat_id) if hasattr(self, "chats") else None
            reason = kwargs.get("reason")
            if reason is None and args:
                reason = args[0]
            if getattr(st, "ghost_mute", False) and "queue" in str(reason).lower():
                logger.info("Ghost v6: blocked leave %s", chat_id)
                return
            return await orig_leave(self, chat_id, *args, **kwargs)

        user_vc.leave = _leave_v6

    user_vc._ghost_v6_guards = True
    logger.info("Ghost v6: HARD guards installed (ChatState + mute/unmute/blink/leave)")


_install_guards()


# ---------------------------------------------------------------- commands
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
            "\n\n⚠️ Is chat me koi audio active nahi hai — sirf icon mute hoga."
        )
        await msg.reply_text(
            f"👻 Ghost Mode ON (`{mode}`)\n\n"
            f"• Mic icon **steady OFF** — blink + admin-unmute dono blocked\n"
            f"• Music/live stream chalta rahega\n"
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
