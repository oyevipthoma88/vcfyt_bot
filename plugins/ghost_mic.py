# plugins/ghost_mic.py
# Ghost Mic v10 — mic icon steady OFF (no blink at all), audio keeps playing.
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


def is_ghost_chat(cid) -> bool:
    if cid is None:
        return False
    try:
        from helpers.peer_guard import chat_id_variants
        cids = set(chat_id_variants(cid) or [int(cid)])
    except Exception:
        try:
            cids = {int(cid)}
        except Exception:
            cids = {cid}

    for ent in _GHOST.values():
        c = ent.get("cid")
        if c is not None:
            if c in cids:
                return True
            try:
                c_int = int(c)
                if c_int in cids:
                    return True
                from helpers.peer_guard import chat_id_variants
                if any(v in cids for v in chat_id_variants(c)):
                    return True
            except Exception:
                pass

    try:
        for uvc in getattr(session_manager, "users", {}).values():
            chats = getattr(uvc, "chats", {}) or {}
            for target_cid in cids:
                st = chats.get(target_cid)
                if st and getattr(st, "ghost_mute", False):
                    return True
    except Exception:
        pass

    return False


_is_ghost_chat = is_ghost_chat


def _pick_cid(uvc, msg, arg=None):
    if arg:
        try:
            return int(arg)
        except (TypeError, ValueError):
            pass
    chat = getattr(msg, "chat", None)
    # pyrogram v2 ChatType is an Enum (ChatType.SUPERGROUP), never the plain
    # string "supergroup" -> old check always failed and ghost mode often
    # landed on the WRONG chat (mic kept blinking in the real one).
    ctype = str(getattr(getattr(chat, "type", None), "value", getattr(chat, "type", "")) or "").lower()
    if chat is not None and (ctype in ("group", "supergroup") or (getattr(chat, "id", 0) or 0) < 0):
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


# InputGroupCall ids of every ghost chat (filled on .ghostmic on).  The
# pyrogram-level guard below uses it to force muted=True on ANY self edit /
# join request for that call — whichever code path (pytgcalls, live mic,
# health checker, bridge, volume keeper) sends it.
_GHOST_CALLS = {}  # call_id -> chat_id


def _user_engines(uid, uvc):
    """Main ID + every spare / relay ID of this user that may be streaming."""
    out, seen = [], set()

    def _add(e):
        if e is None or id(e) in seen:
            return
        seen.add(id(e))
        out.append(e)

    _add(uvc)
    try:
        for e in (getattr(session_manager, "assistants", {}) or {}).values():
            if getattr(e, "owner_id", getattr(e, "user_id", None)) == uid:
                _add(e)
    except Exception:
        pass
    try:
        from helpers import live_mic
        sess = live_mic._sessions.get(uid)
        if sess is not None:
            _add(getattr(sess, "relay", None))
    except Exception:
        pass
    try:
        from helpers import vc_bridge
        b = vc_bridge._bridges.get(uid)
        if b is not None:
            _add(getattr(b, "relay", None))
            for x in list(getattr(b, "extras", []) or []):
                _add(getattr(x, "relay", None))
    except Exception:
        pass
    try:
        from plugins.sync_play import sync_engines
        for e in sync_engines(uid):
            _add(e)
    except Exception:
        pass
    return out


async def _remember_call(eng, cid):
    try:
        call = await eng._call_input(cid)
        if call is not None and getattr(call, "id", None):
            _GHOST_CALLS[int(call.id)] = cid
    except Exception:
        pass


async def _safe_mute(uvc, cid, muted=True, uid=None):
    engines = _user_engines(uid, uvc) if uid is not None else [uvc]
    ok = False
    for eng in engines:
        if muted:
            await _remember_call(eng, cid)
        try:
            ok = bool(await eng._edit_self(cid, muted=muted)) or ok
        except Exception as exc:
            logger.debug("ghost mute failed: %r", exc)
    return ok


def _calm_state(st):
    if st is None:
        return
    try:
        st.ghost_mute = True
        st.mic_blink = False
        st.mic_blink_secs = 0
    except Exception:
        pass


async def _loop(uid, uvc, cid, mode):
    """Ghost v10: mic icon STEADY OFF.

    Old blink/pulse modes toggled the mic every 0.2-2 s and even the hold
    mode re-sent a mute edit every 5 s.  Every edit makes Telegram clients
    re-render the participant row = visible flicker / "blink".  Now we only
    send a mute when Telegram reports the mic actually came ON (raw update
    handler below) plus a slow safety re-check.  All modes = steady OFF.
    """
    while _GHOST.get(uid, {}).get("cid") == cid:
        for eng in _user_engines(uid, uvc):
            _calm_state(_state(eng, cid))
            _install_raw_watch(eng)
        await asyncio.sleep(20)
        if _GHOST.get(uid, {}).get("cid") != cid:
            break
        await _safe_mute(uvc, cid, True, uid)


# ---- instant re-mute: react to Telegram's own participant updates ----
_WATCHED = set()


def _install_raw_watch(eng):
    client = getattr(eng, "client", None)
    if client is None or id(client) in _WATCHED:
        return
    try:
        from pyrogram.handlers import RawUpdateHandler
        from pyrogram.raw.types import UpdateGroupCallParticipants
    except Exception:
        return

    async def _on_raw(c, update, users, chats):
        if not _GHOST_CALLS or not isinstance(update, UpdateGroupCallParticipants):
            return
        try:
            call_id = int(getattr(update.call, "id", 0) or 0)
        except Exception:
            return
        cid = _GHOST_CALLS.get(call_id)
        if cid is None:
            return
        for p in getattr(update, "participants", []) or []:
            if getattr(p, "is_self", False) and not getattr(p, "muted", True) \
                    and not getattr(p, "left", False):
                try:
                    await eng._edit_self(cid, muted=True)
                except Exception:
                    pass

    try:
        client.add_handler(RawUpdateHandler(_on_raw), group=-77)
        _WATCHED.add(id(client))
    except Exception as exc:
        logger.debug("ghost raw watch failed: %r", exc)


async def _start(uid, uvc, cid, mode="hold"):
    await _stop(uid, uvc, cid=None, unmute=False)

    st = _state(uvc, cid)
    if st is not None:
        try:
            st.ghost_mute = True
            st.mic_blink = False
            st.mic_blink_secs = 0
        except Exception:
            pass

    mode = "hold"  # blink/pulse removed: mic hamesha steady OFF
    _GHOST[uid] = {"cid": cid, "mode": mode, "task": None}
    for eng in _user_engines(uid, uvc):
        _calm_state(_state(eng, cid))
        _install_raw_watch(eng)
    await _safe_mute(uvc, cid, True, uid)
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
        # Clear the flag on EVERY engine (main + spare/relay/sync IDs).  Only
        # the main ID was cleared before, so is_ghost_chat() stayed True via
        # the relay's state and the mic was stuck muted after ".ghostmic off".
        for eng in (_user_engines(uid, uvc) if uid is not None else [uvc]):
            st = _state(eng, cid)
            if st is not None:
                try:
                    st.ghost_mute = False
                except Exception:
                    pass
        if not any(e.get("cid") == cid for e in _GHOST.values()):
            for k in [k for k, v in _GHOST_CALLS.items() if v == cid]:
                _GHOST_CALLS.pop(k, None)
        if unmute:
            await _safe_mute(uvc, cid, False, uid)
    return bool(ent)


# ---------------------------------------------------------------- HARD GUARDS
def _install_guards():
    from helpers import vc_manager as vm

    user_vc = getattr(vm, "UserVC", None)
    chat_state = getattr(vm, "ChatState", None)
    if user_vc is None:
        return

    # ---- 0) ChatState.ghost_mute attribute add ----
    if chat_state is not None and not getattr(chat_state, "_ghost_v7", False):
        _orig_init = chat_state.__init__

        def _init_v7(self, *args, **kwargs):
            _orig_init(self, *args, **kwargs)
            self.ghost_mute = False
            self.mic_blink = False

        chat_state.__init__ = _init_v7
        chat_state._ghost_v7 = True

    if getattr(user_vc, "_ghost_v7_guards", False):
        return

    def _active(self, chat_id):
        if is_ghost_chat(chat_id):
            return True
        st = self.chats.get(chat_id) if hasattr(self, "chats") else None
        return bool(st and getattr(st, "ghost_mute", False))

    # ---- 0b) state() guard — ghost chat me mic_blink hamesha False ----
    if hasattr(user_vc, "state"):
        orig_state = user_vc.state

        def _state_v7(self, chat_id):
            st = orig_state(self, chat_id)
            if is_ghost_chat(chat_id):
                try:
                    st.ghost_mute = True
                    st.mic_blink = False
                    st.mic_blink_secs = 0
                except Exception:
                    pass
            return st

        user_vc.state = _state_v7

    # ---- 1) _edit_self force-mute in ghost mode ----
    if hasattr(user_vc, "_edit_self"):
        original_edit = user_vc._edit_self

        async def _edit_self_v7(self, chat_id, *args, **kwargs):
            if _active(self, chat_id):
                if args:
                    args = (True,) + args[1:]
                else:
                    kwargs["muted"] = True
            return await original_edit(self, chat_id, *args, **kwargs)

        user_vc._edit_self = _edit_self_v7

    # ---- 2) set_participant_volume guard (prevent pytgcalls unmute) ----
    if hasattr(user_vc, "set_participant_volume"):
        orig_spv = user_vc.set_participant_volume

        async def _spv_v7(self, chat_id, user_id, volume, quiet=False):
            if _active(self, chat_id) and user_id == self.account_id:
                try:
                    call_input = await self._call_input(chat_id)
                    if call_input:
                        from pyrogram.raw.functions.phone import EditGroupCallParticipant
                        peer = await self.client.resolve_peer(user_id)
                        await self.client.invoke(EditGroupCallParticipant(
                            call=call_input, participant=peer, muted=True, volume=int(volume),
                        ))
                        return True
                except Exception:
                    return False
            return await orig_spv(self, chat_id, user_id, volume, quiet)

        user_vc.set_participant_volume = _spv_v7

    # ---- 3) apply_mute_state blocked in ghost mode ----
    if hasattr(user_vc, "apply_mute_state"):
        orig_apply = user_vc.apply_mute_state

        def _apply_v7(self, chat_id, muted_by_admin):
            if _active(self, chat_id):
                logger.info("Ghost v7: apply_mute_state blocked %s", chat_id)
                return
            return orig_apply(self, chat_id, muted_by_admin)

        user_vc.apply_mute_state = _apply_v7

    # ---- 4) handle_admin_unmute blocked in ghost mode ----
    if hasattr(user_vc, "handle_admin_unmute"):
        orig_hu = user_vc.handle_admin_unmute

        async def _hu_v7(self, chat_id):
            if _active(self, chat_id):
                logger.info("Ghost v7: handle_admin_unmute blocked %s", chat_id)
                return
            return await orig_hu(self, chat_id)

        user_vc.handle_admin_unmute = _hu_v7

    # ---- 5) handle_admin_mute blocked in ghost mode ----
    if hasattr(user_vc, "handle_admin_mute"):
        orig_hm = user_vc.handle_admin_mute

        async def _hm_v7(self, chat_id):
            if _active(self, chat_id):
                return
            return await orig_hm(self, chat_id)

        user_vc.handle_admin_mute = _hm_v7

    # ---- 6) _mic_blink blocked in ghost mode ----
    if hasattr(user_vc, "_mic_blink"):
        orig_blink = user_vc._mic_blink

        async def _blink_v7(self, chat_id, *args, **kwargs):
            if _active(self, chat_id):
                logger.info("Ghost v7: _mic_blink blocked %s", chat_id)
                return
            return await orig_blink(self, chat_id, *args, **kwargs)

        user_vc._mic_blink = _blink_v7

    # ---- 7) _self_unmute blocked in ghost mode ----
    if hasattr(user_vc, "_self_unmute"):
        orig_unmute = user_vc._self_unmute

        async def _su_v7(self, chat_id):
            if _active(self, chat_id):
                logger.info("Ghost v7: _self_unmute blocked %s", chat_id)
                return
            return await orig_unmute(self, chat_id)

        user_vc._self_unmute = _su_v7

    # ---- 8) LiveMicSession guards ----
    try:
        from helpers.live_mic import LiveMicSession
        if not getattr(LiveMicSession, "_ghost_v7_guards", False):
            orig_set_tg = LiveMicSession.set_tg_mute

            async def _set_tg_v7(self, muted):
                cid = getattr(self, "chat_id", None) or getattr(self, "cid", None)
                if cid is not None and is_ghost_chat(cid):
                    muted = True
                return await orig_set_tg(self, muted)

            LiveMicSession.set_tg_mute = _set_tg_v7

            orig_unmute_self = LiveMicSession._unmute_self

            async def _unmute_self_v7(self):
                cid = getattr(self, "chat_id", None) or getattr(self, "cid", None)
                if cid is not None and is_ghost_chat(cid):
                    try:
                        call_input = await self.relay._call_input(cid)
                        if call_input:
                            from pyrogram.raw.functions.phone import EditGroupCallParticipant
                            peer = await self.relay.client.resolve_peer(self.relay.account_id)
                            await self.relay.client.invoke(
                                EditGroupCallParticipant(call=call_input, participant=peer, muted=True)
                            )
                    except Exception:
                        pass
                    return
                return await orig_unmute_self(self)

            LiveMicSession._unmute_self = _unmute_self_v7
            LiveMicSession._ghost_v7_guards = True
    except Exception as exc:
        logger.warning("Ghost v7: LiveMicSession guard failed: %s", exc)

    # ---- 9) PyTgCalls client guards (ROOT FIX for "mic off but blink on") ----
    # PyrogramClient OVERRIDES change_volume / set_call_status / join, so the
    # old patch on the BridgedClient base class never ran.  pytgcalls kept
    # sending muted=False on every volume change (live mic 200 %), every
    # stream state change (play / pause / next track) and every join, and
    # ghost mode re-muted up to 10 s later = the visible blink.  Patch the
    # concrete class and force muted=True at the single request choke point.
    try:
        import contextvars
        from pyrogram.raw.functions.phone import (
            EditGroupCallParticipant as _EGCP, JoinGroupCall as _JGC,
        )
        try:
            from pytgcalls.mtproto.pyrogram_client import PyrogramClient as _PC
        except Exception:
            from pytgcalls.mtproto.pyrogram_client import BridgedClient as _PC
        _ghost_ctx = contextvars.ContextVar("ghost_ctx", default=False)

        if not getattr(_PC, "_ghost_v8_guards", False):
            orig_invoke = _PC._invoke

            async def _invoke_v8(self, request, *a, **kw):
                if _ghost_ctx.get() and isinstance(request, (_EGCP, _JGC)):
                    try:
                        request.muted = True
                    except Exception:
                        pass
                return await orig_invoke(self, request, *a, **kw)

            _PC._invoke = _invoke_v8

            def _wrap(name):
                orig = getattr(_PC, name, None)
                if orig is None:
                    return

                async def _guarded(self, chat_id, *a, **kw):
                    if is_ghost_chat(chat_id):
                        tok = _ghost_ctx.set(True)
                        try:
                            return await orig(self, chat_id, *a, **kw)
                        finally:
                            _ghost_ctx.reset(tok)
                    return await orig(self, chat_id, *a, **kw)

                setattr(_PC, name, _guarded)

            for _n in ("change_volume", "set_call_status", "join_group_call"):
                _wrap(_n)
            _PC._ghost_v8_guards = True
    except Exception as exc:
        logger.warning("Ghost v8: PyTgCalls client patch failed: %s", exc)

    # ---- 9b) PYROGRAM CHOKE POINT (blink ROOT FIX) ----
    # Many paths call relay.client.invoke(EditGroupCallParticipant(muted=False))
    # directly (live-mic force-unmute, health re-unmute, bridge volume keeper,
    # pytgcalls join).  Each one flipped the icon ON for a moment until the
    # ghost loop muted it again = blink.  Every raw request goes through
    # pyrogram.Client.invoke, so force muted=True there for self-edits/joins
    # on a ghost call.
    try:
        import pyrogram
        from pyrogram.raw.functions.phone import (
            EditGroupCallParticipant as _E2, JoinGroupCall as _J2,
        )
        from pyrogram.raw.types import InputPeerSelf as _Self
        if not getattr(pyrogram.Client, "_ghost_v9_invoke", False):
            _orig_client_invoke = pyrogram.Client.invoke

            async def _client_invoke_v9(self, query, *a, **kw):
                try:
                    if isinstance(query, (_E2, _J2)) and _GHOST_CALLS:
                        call_id = int(getattr(getattr(query, "call", None), "id", 0) or 0)
                        if call_id in _GHOST_CALLS:
                            if isinstance(query, _J2):
                                query.muted = True
                            else:
                                part = getattr(query, "participant", None)
                                me = getattr(getattr(self, "me", None), "id", None)
                                is_self = isinstance(part, _Self) or (
                                    me is not None and getattr(part, "user_id", None) == me)
                                if is_self and query.muted is not None:
                                    query.muted = True
                except Exception:
                    pass
                return await _orig_client_invoke(self, query, *a, **kw)

            pyrogram.Client.invoke = _client_invoke_v9
            pyrogram.Client._ghost_v9_invoke = True
    except Exception as exc:
        logger.warning("Ghost v9: pyrogram invoke guard failed: %s", exc)

    # ---- 10) leave guard ----
    if hasattr(user_vc, "leave"):
        orig_leave = user_vc.leave

        async def _leave_v7(self, chat_id, *args, **kwargs):
            st = self.chats.get(chat_id) if hasattr(self, "chats") else None
            reason = kwargs.get("reason")
            if reason is None and args:
                reason = args[0]
            if getattr(st, "ghost_mute", False) and "queue" in str(reason).lower():
                logger.info("Ghost v7: blocked leave %s", chat_id)
                return
            return await orig_leave(self, chat_id, *args, **kwargs)

        user_vc.leave = _leave_v7

    user_vc._ghost_v7_guards = True
    logger.info("Ghost v7: HARD guards installed (ChatState + mute/unmute/blink/volume/leave)")


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
    if not arg:
        # Sync play chal raha ho to ghost TARGET GC par lage, private group par nahi.
        try:
            from plugins.sync_play import _ACTIVE
            ent = _ACTIVE.get(uid)
            if ent and ent.get("cid"):
                cid = ent["cid"]
        except Exception:
            pass

    if action in ("on", "blink", "pulse"):
        if not cid:
            await msg.reply_text(
                "❌ Chat ID nahi mila.\nGroup me bhejo, ya `.ghostmic on <chat_id>`."
            )
            return
        mode = "hold"
        await _start(uid, uvc, cid, mode)
        audio = _has_audio(uvc, cid)
        note = "" if audio else (
            "\n\n⚠️ Is chat me koi audio active nahi hai — sirf icon mute hoga."
        )
        await msg.reply_text(
            f"👻 Ghost Mode ON (`{mode}`)\n\n"
            f"• Mic icon **steady OFF** (muted) — blink off rahega\n"
            f"• Voice / music audio VC me chalta rahega\n"
            f"• Chat: `{cid}`{note}\n\n"
            "Off: `.ghostmic off`"
        )
        return

    if action == "off":
        was = await _stop(uid, uvc, cid)
        await msg.reply_text(
            "❌ Ghost Mode OFF — mic icon wapas normal."
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
