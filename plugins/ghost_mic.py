# plugins/ghost_mic.py — Ghost Mic: mic icon OFF, audio ON
import asyncio, logging
from pyrogram import Client, filters
from pyrogram.types import Message
from config import Config
from helpers.vc_manager import session_manager

logger = logging.getLogger("vcbot.ghost_mic")
_ghost_live = {}          # uid -> LiveMicSession
_ghost_playback = {}      # uid -> bool (playback ke liye auto-mute)

# ---------- .ghostmic on/off/blink ----------
@Client.on_message(filters.group & filters.regex(r"\.ghostmic\s+(on|off|blink)$"))
async def ghost_mic_cmd(bot, msg: Message):
    uid, cid, arg = msg.from_user.id, msg.chat.id, msg.matches[0].group(1).lower()
    uvc = session_manager.users.get(uid)
    if not uvc:
        await msg.reply_text("❌ Pehle login karo. Bot ke DM me /login bhejo.")
        return

    if arg == "on":
        if uid in _ghost_live:
            await msg.reply_text("⚠️ Ghost mode pehle se ON hai.")
            return
        from helpers.live_mic import LiveMicSession
        relay = LiveMicSession(uid, uvc, cid, settings={})
        if hasattr(relay, "_attach_stream"): await relay._attach_stream()
        if hasattr(relay, "_ensure_relay_peer"): await relay._ensure_relay_peer()
        if hasattr(relay, "_release_stale_calls"): await relay._release_stale_calls()
        await relay.set_tg_mute(True)
        _ghost_live[uid] = relay
        _ghost_playback[uid] = True
        await msg.reply_text(
            "✅ Ghost Mode ON\n• Mic icon OFF (strictly muted)\n"
            "• Live mic audio relay se ja raha hai\n• Playback par bhi mic OFF rahega\n"
            "Band: .ghostmic off"
        )
        return

    if arg == "off":
        relay = _ghost_live.pop(uid, None)
        _ghost_playback.pop(uid, None)
        try:
            if relay: await relay.set_tg_mute(False)
        except Exception: pass
        try: await uvc._edit_self(cid, muted=False)
        except Exception: pass
        await msg.reply_text("❌ Ghost Mode OFF. Mic icon ON.")
        return

    if arg == "blink":
        relay = _ghost_live.get(uid)
        if not relay:
            await msg.reply_text("⚠️ Pehle .ghostmic on karo.")
            return
        await msg.reply_text("✨ Blink mode ON. Mic icon blink, audio continuous.")
        async def _blink():
            while uid in _ghost_live:
                try:
                    await relay.set_tg_mute(True); await asyncio.sleep(2)
                    await relay.set_tg_mute(False); await asyncio.sleep(2)
                except Exception: break
        if not hasattr(relay, "_blink_task") or relay._blink_task.done():
            relay._blink_task = asyncio.create_task(_blink())
        return

# ---------- Playback card button: gm:play:<cid> ----------
@Client.on_callback_query(filters.regex(r"^gm:play:(-?\d+)$"))
async def cb_ghost_play(bot, cq):
    uid = cq.from_user.id
    cid = int(cq.matches[0].group(1))
    uvc = session_manager.users.get(uid)
    if not uvc:
        await cq.answer("❌ Pehle login karo.", show_alert=True)
        return
    _ghost_playback[uid] = True
    try:
        await uvc._edit_self(cid, muted=True)
        await cq.answer("👻 Ghost Mic ON — mic OFF, audio ON")
    except Exception as e:
        await cq.answer(f"❌ {e}", show_alert=True)

# ---------- Live Mic card button: gm:live ----------
@Client.on_callback_query(filters.regex(r"^gm:live$"))
async def cb_ghost_live(bot, cq):
    uid = cq.from_user.id
    # agar already ON hai to OFF kar do (toggle)
    if uid in _ghost_live:
        relay = _ghost_live.pop(uid, None)
        _ghost_playback.pop(uid, None)
        try:
            if relay: await relay.set_tg_mute(False)
        except Exception: pass
        await cq.answer("❌ Ghost Mic OFF — mic icon wapas ON")
        return
    uvc = session_manager.users.get(uid)
    if not uvc:
        await cq.answer("❌ Pehle login karo.", show_alert=True)
        return
    # live mic card se chat id nikaalo (current VC)
    cid = None
    for c, st in uvc.chats.items():
        if getattr(st, "mic_enabled", False) or st.is_playing:
            cid = c; break
    if not cid:
        await cq.answer("❌ Pehle VC start karo.", show_alert=True)
        return
    from helpers.live_mic import LiveMicSession
    relay = LiveMicSession(uid, uvc, cid, settings={})
    if hasattr(relay, "_attach_stream"): await relay._attach_stream()
    if hasattr(relay, "_ensure_relay_peer"): await relay._ensure_relay_peer()
    if hasattr(relay, "_release_stale_calls"): await relay._release_stale_calls()
    await relay.set_tg_mute(True)
    _ghost_live[uid] = relay
    _ghost_playback[uid] = True
    await cq.answer("👻 Ghost Mic ON — mic OFF, audio ON")
