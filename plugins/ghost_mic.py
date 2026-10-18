# plugins/ghost_mic.py
import asyncio
import functools
import logging
from pyrogram import Client, filters
from pyrogram.types import Message

from config import Config  # noqa
from helpers.vc_manager import session_manager

logger = logging.getLogger("vcbot.ghost_mic")

_ghost_live = {}
_ghost_playback = {}
_play_patched = False


def _patch_play():
    global _play_patched
    if _play_patched:
        return
    try:
        from helpers.vc_manager import UserVC
        if getattr(UserVC, "_ghost_patched", False):
            _play_patched = True
            return
        _orig = UserVC.play
        @functools.wraps(_orig)
        async def _patched(self, *args, **kwargs):
            result = await _orig(self, *args, **kwargs)
            try:
                uid = getattr(self, "user_id", None) or getattr(self, "uid", None)
                if uid and uid in _ghost_playback:
                    cid = kwargs.get("chat_id") or kwargs.get("cid")
                    if cid is None and args:
                        cid = args[0]
                    if cid:
                        await self._edit_self(cid, muted=True)
                        logger.info(f"Ghost play mute applied uid={uid} cid={cid}")
            except Exception as e:
                logger.debug(f"Ghost play mute skip: {e}")
            return result
        UserVC.play = _patched
        UserVC._ghost_patched = True
        _play_patched = True
        logger.info("Ghost mic: UserVC.play patched")
    except Exception as e:
        logger.warning(f"Ghost mic: play patch failed: {e}")


try:
    _patch_play()
except Exception as e:
    logger.warning(f"Ghost mic: patch init failed: {e}")


@Client.on_message(filters.group & filters.regex(r"\.ghostmic\s+(on|off|blink)$"))
async def ghost_mic_cmd(bot, msg: Message):
    user_id = msg.from_user.id
    chat_id = msg.chat.id
    arg = msg.matches[0].group(1).lower()

    uvc = session_manager.users.get(user_id)
    if not uvc:
        await msg.reply_text("❌ Pehle login karo. Bot ke DM me /login bhejo.")
        return

    if arg == "on":
        if user_id in _ghost_live:
            await msg.reply_text("⚠️ Ghost mode pehle se ON hai.")
            return
        try:
            from helpers.live_mic import LiveMicSession
            relay = LiveMicSession(user_id, uvc, chat_id, settings={})
            if hasattr(relay, "_attach_stream"):
                await relay._attach_stream()
            if hasattr(relay, "_ensure_relay_peer"):
                await relay._ensure_relay_peer()
            if hasattr(relay, "_release_stale_calls"):
                await relay._release_stale_calls()
            await relay.set_tg_mute(True)
            _ghost_live[user_id] = relay
            _ghost_playback[user_id] = True
            await msg.reply_text(
                "✅ Ghost Mode ON\n"
                "• Mic icon OFF (strictly muted)\n"
                "• Live mic audio relay se ja raha hai\n"
                "• Playback par bhi mic OFF rahega\n"
                "Band: .ghostmic off"
            )
        except Exception as e:
            logger.exception("ghost on failed")
            await msg.reply_text(f"❌ Ghost ON fail: {e}")
        return

    if arg == "off":
        relay = _ghost_live.pop(user_id, None)
        _ghost_playback.pop(user_id, None)
        try:
            if relay:
                await relay.set_tg_mute(False)
        except Exception:
            pass
        try:
            await uvc._edit_self(chat_id, muted=False)
        except Exception:
            pass
        await msg.reply_text("❌ Ghost Mode OFF. Mic icon ON.")
        return

    if arg == "blink":
        relay = _ghost_live.get(user_id)
        if not relay:
            await msg.reply_text("⚠️ Pehle .ghostmic on karo.")
            return
        await msg.reply_text("✨ Blink mode ON. Mic icon blink, audio continuous.")
        async def _blink():
            while user_id in _ghost_live:
                try:
                    await relay.set_tg_mute(True)
                    await asyncio.sleep(2)
                    await relay.set_tg_mute(False)
                    await asyncio.sleep(2)
                except Exception:
                    break
        if not hasattr(relay, "_blink_task") or relay._blink_task.done():
            relay._blink_task = asyncio.create_task(_blink())
        return
