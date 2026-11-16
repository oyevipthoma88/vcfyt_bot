"""v94 Smart Setup Guide — /guide, /setup, 🧭 button.

Naye user ko ek hi screen par dikhta hai: kya ho gaya ✅, abhi kya karna hai 👉,
aur sirf ek hara button jo seedha us step par le jaata hai.
"""
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup as K, Message

from helpers.onboarding import SETGC_PROMPT, guide_screen, setup_state
from plugins.ui import B, HAS_USER, dm_cmd, edit_screen, safe_answer


@Client.on_message(HAS_USER & dm_cmd(["guide", "setup"]) & filters.private)
async def cmd_guide(bot: Client, msg: Message):
    text, kb = await guide_screen(msg.from_user.id, msg.from_user.first_name or "friend")
    await msg.reply_text(text, reply_markup=kb, disable_web_page_preview=True)


@Client.on_callback_query(filters.regex(r"^guide:open$"))
async def cb_guide(bot, cq):
    text, kb = await guide_screen(cq.from_user.id, cq.from_user.first_name or "friend")
    await edit_screen(cq.message, text, reply_markup=kb, disable_web_page_preview=True)
    await safe_answer(cq)


@Client.on_callback_query(filters.regex(r"^guide:setgc$"))
async def cb_guide_setgc(bot, cq):
    uid = cq.from_user.id
    state = await setup_state(uid)
    if not state["voice"]:
        await safe_answer(cq, "Pehle step ① — 🔐 Login karo", show_alert=True)
        text, kb = await guide_screen(uid, cq.from_user.first_name or "friend")
        await edit_screen(cq.message, text, reply_markup=kb)
        return
    from plugins.live_mic_target import request_target_input
    request_target_input(uid, surface="guide")
    await edit_screen(cq.message, SETGC_PROMPT, reply_markup=K([
        [B("✖️ Cancel", callback_data="guide:cancel", style="danger")],
    ]), disable_web_page_preview=True)
    await safe_answer(cq, "Group ka link / @username / ID bhejo")


@Client.on_callback_query(filters.regex(r"^guide:cancel$"))
async def cb_guide_cancel(bot, cq):
    try:
        from plugins.live_mic_target import _await_chat
        _await_chat.pop(cq.from_user.id, None)
    except Exception:
        pass
    text, kb = await guide_screen(cq.from_user.id, cq.from_user.first_name or "friend")
    await edit_screen(cq.message, text, reply_markup=kb)
    await safe_answer(cq, "Cancelled")
