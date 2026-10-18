# plugins/ghost_mic.py
# Ghost Mic — mic icon OFF, audio ON (Live Mic + Playback dono ke liye)
import asyncio
import logging
import re

from pyrogram import Client, filters
from helpers.vc_manager import session_manager

logger = logging.getLogger("vcbot.ghost_mic")

_ghost_live = {}       # uid -> LiveMicSession
_ghost_playback = {}   # uid -> bool

GHOST_CMD = filters.regex(
    r"^[./!]ghostmic(?:@\w+)?\b",
    flags=re.IGNORECASE,
)


# ---------------- engine ----------------
async def _get_engine(uid: int):
    uvc = session_manager.users.get(uid)
    if uvc:
        return uvc
    try:
        return await session_manager.get(uid)
    except Exception:
        return None


def _pick_chat(uvc, msg, arg=None):
    if arg:
        try:
            return int(arg)
        except (TypeError, ValueError):
            pass

    chat = getattr(msg, "chat", None)
    if chat and getattr(chat, "type", None) in ("group", "supergroup"):
        return chat.id

    for cid, st in (getattr(uvc, "chats", {}) or {}).items():
        if getattr(st, "mic_enabled", False) or getattr(st, "is_playing", False):
            return int(cid)

    for attr in ("default_gc", "default_chat", "last_chat"):
        value = getattr(uvc, attr, None)
        if value:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    return None


# ---------------- live relay ----------------
async def _start_live(uid: int, uvc, cid: int):
    from helpers.live_mic import LiveMicSession

    relay = LiveMicSession(uid, uvc, cid, settings={})

    for fn_name in ("_attach_stream", "_ensure_relay_peer", "_release_stale_calls"):
        fn = getattr(relay, fn_name, None)
        if not fn:
            continue
        try:
            await fn()
        except Exception as exc:
            logger.warning("ghost live %s failed: %s", fn_name, exc)

    await uvc._edit_self(cid, muted=True)

    _ghost_live[uid] = relay
    _ghost_playback[uid] = True

    # LiveMicSession background me auto-unmute karta hai, isliye mute ko
    # kuch der tak re-assert karte hain — icon OFF hi rahe.
    async def _hold_mute():
        for delay in (0.5, 1.5, 3.0, 6.0, 12.0, 20.0):
            await asyncio.sleep(delay)
            if uid not in _ghost_live:
                return
            try:
                await uvc._edit_self(cid, muted=True)
            except Exception:
                pass

    relay._ghost_hold_task = asyncio.create_task(_hold_mute())
    return relay


# ---------------- .ghostmic command ----------------
@Client.on_message(
    filters.text
    & GHOST_CMD
    & (filters.group | filters.private)
)
async def ghost_mic_cmd(bot, msg):
    uid = msg.from_user.id
    parts = (msg.text or "").split()
    action = parts[1].lower() if len(parts) > 1 else "status"
    arg = parts[2] if len(parts) > 2 else None

    uvc = await _get_engine(uid)
    if not uvc:
        await msg.reply_text(
            "❌ Pehle login karo.\nBot ke DM me `/login` bhejo, phir `.ghostmic on` karo."
        )
        return

    cid = _pick_chat(uvc, msg, arg)

    if action == "on":
        if uid in _ghost_live:
            await msg.reply_text("⚠️ Ghost mode pehle se ON hai. Band karne ke liye `.ghostmic off`.")
            return
        if not cid:
            await msg.reply_text(
                "❌ Chat ID nahi mila.\nGroup me `.ghostmic on` bhejo, "
                "ya DM me `.ghostmic on <chat_id>` use karo."
            )
            return
        try:
            await _start_live(uid, uvc, cid)
            await msg.reply_text(
                "✅ **Ghost Mode ON**\n\n"
                "• Mic icon OFF (strictly muted)\n"
                "• Live mic audio relay se ja raha hai\n"
                "• `.play` par bhi mic OFF rahega\n"
                f"• Chat: `{cid}`\n\n"
                "Band: `.ghostmic off`"
            )
        except Exception as exc:
            logger.exception("ghost on failed")
            await msg.reply_text(f"❌ Ghost ON fail: {exc}")
        return

    if action == "off":
        relay = _ghost_live.pop(uid, None)
        _ghost_playback.pop(uid, None)

        if relay:
            task = getattr(relay, "_ghost_hold_task", None)
            if task:
                task.cancel()
            try:
                await relay.set_tg_mute(False)
            except Exception:
                pass

        if cid:
            try:
                await uvc._edit_self(cid, muted=False)
            except Exception:
                pass

        await msg.reply_text("❌ **Ghost Mode OFF** — mic icon wapas ON.")
        return

    if action == "blink":
        relay = _ghost_live.get(uid)
        if not relay:
            await msg.reply_text("⚠️ Pehle `.ghostmic on` karo, phir `.ghostmic blink`.")
            return

        await msg.reply_text("✨ **Blink ON** — mic icon on/off hoga, audio continuous rahega.")

        async def _blink():
            while uid in _ghost_live:
                try:
                    await uvc._edit_self(cid, muted=True)
                    await asyncio.sleep(2)
                    await uvc._edit_self(cid, muted=False)
                    await asyncio.sleep(2)
                except Exception:
                    break

        old = getattr(relay, "_ghost_blink_task", None)
        if old and not old.done():
            old.cancel()
        relay._ghost_blink_task = asyncio.create_task(_blink())
        return

    if action == "status":
        live = uid in _ghost_live
        play = bool(_ghost_playback.get(uid))
        await msg.reply_text(
            f"👻 **Ghost Status**\n\n"
            f"• Live relay: `{'ON' if live else 'OFF'}`\n"
            f"• Playback mute: `{'ON' if play else 'OFF'}`\n"
            f"• Chat: `{cid or '—'}`\n\n"
            "Commands: `.ghostmic on|off|blink|status [chat_id]`"
        )
        return

    await msg.reply_text("Usage: `.ghostmic on|off|blink|status [chat_id]`")


# ---------------- Playback card button: gm:play:<cid> ----------------
@Client.on_callback_query(filters.regex(r"^gm:play:(-?\d+)$"))
async def cb_ghost_play(bot, cq):
    uid = cq.from_user.id
    cid = int(cq.matches[0].group(1))

    uvc = await _get_engine(uid)
    if not uvc:
        await cq.answer("❌ Pehle login karo.", show_alert=True)
        return

    _ghost_playback[uid] = True
    try:
        ok = await uvc._edit_self(cid, muted=True)
        await cq.answer("👻 Ghost Mic ON — mic OFF, audio ON" if ok else "⚠️ Mute apply nahi hua", show_alert=not ok)
    except Exception as exc:
        await cq.answer(f"❌ {exc}", show_alert=True)


# ---------------- Live Mic card button: gm:live ----------------
@Client.on_callback_query(filters.regex(r"^gm:live(?::(-?\d+))?$"))
async def cb_ghost_live(bot, cq):
    uid = cq.from_user.id

    # Toggle OFF
    if uid in _ghost_live:
        relay = _ghost_live.pop(uid, None)
        _ghost_playback.pop(uid, None)
        if relay:
            task = getattr(relay, "_ghost_hold_task", None)
            if task:
                task.cancel()
            try:
                await relay.set_tg_mute(False)
            except Exception:
                pass
        await cq.answer("❌ Ghost Mic OFF — mic icon wapas ON")
        return

    uvc = await _get_engine(uid)
    if not uvc:
        await cq.answer("❌ Pehle login karo.", show_alert=True)
        return

    cid = None
    m = cq.matches[0].group(1) if cq.matches else None
    if m:
        try:
            cid = int(m)
        except ValueError:
            cid = None

    if not cid:
        for chat_id, st in (getattr(uvc, "chats", {}) or {}).items():
            if getattr(st, "mic_enabled", False) or getattr(st, "is_playing", False):
                cid = int(chat_id)
                break

    if not cid:
        await cq.answer("❌ Pehle VC start karo / `.mic on` karo.", show_alert=True)
        return

    try:
        await _start_live(uid, uvc, cid)
        await cq.answer("👻 Ghost Mic ON — mic OFF, audio ON")
    except Exception as exc:
        logger.exception("ghost live button failed")
        await cq.answer(f"❌ {exc}", show_alert=True)


# ---------------- Playback ke baad mute enforce ----------------
def _patch_play():
    try:
        from helpers.vc_manager import UserVC

        if getattr(UserVC, "_ghost_play_patched", False):
            return

        original = UserVC.play

        async def _wrapped(self, *args, **kwargs):
            result = await original(self, *args, **kwargs)
            try:
                uid = getattr(self, "owner_id", None)
                if uid in _ghost_playback:
                    cid = kwargs.get("chat_id") or kwargs.get("cid")
                    if cid is None:
                        for value in args:
                            if isinstance(value, int):
                                cid = value
                                break
                    if cid:
                        await self._edit_self(int(cid), muted=True)
            except Exception:
                pass
            return result

        UserVC.play = _wrapped
        UserVC._ghost_play_patched = True
        logger.info("Ghost mic: UserVC.play patched")
    except Exception as exc:
        logger.warning("Ghost mic: play patch failed: %s", exc)


_patch_play()
