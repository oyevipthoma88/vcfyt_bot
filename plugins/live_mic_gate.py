"""Live Mic master switch: owner OFF karde to users ko Live Mic na dikhe, na chale.

Runs in group=-5 (before login gate / real handlers). Owner is never blocked.
"""
import re

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup as K

from config import Config
from helpers.feature_flags import live_mic_on, refresh_flags
from plugins.ui import B, edit_screen, safe_answer

_CB = re.compile(r"^(mic:|gm:|tut:livemic$)")
# .micblink belongs to Human mode (admin mute/unmute), not Live Mic.
_CMD = re.compile(r"^[./!](mic|micaccount|ghostmic)(@\w+)?\b", re.I)

OFF_TEXT = (
    "🎤 <b>Live Mic abhi band hai</b>\n"
    "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
    "Owner ne Live Mic temporarily <b>OFF</b> kiya hai.\n"
    "Player, Sound aur Library pehle jaise chalte rahenge."
)
OFF_KB = K([[B("🏠 Home", callback_data="menu:home", style="primary")]])


def _cb(_, __, cq):
    return bool(cq.data and _CB.match(cq.data))


def _msg(_, __, m):
    return bool(_CMD.match((m.text or m.caption or "").strip()))


@Client.on_callback_query(filters.create(_cb), group=-5)
async def live_mic_cb_gate(bot, cq):
    if not cq.from_user or Config.is_owner(cq.from_user.id):
        return
    if await refresh_flags():
        return
    await safe_answer(cq, "🎤 Live Mic abhi OFF hai", show_alert=False)
    try:
        await edit_screen(cq.message, OFF_TEXT, reply_markup=OFF_KB)
    except Exception:
        pass
    cq.stop_propagation()


@Client.on_message(filters.create(_msg) & (filters.group | filters.private), group=-5)
async def live_mic_msg_gate(bot, msg):
    if not msg.from_user or Config.is_owner(msg.from_user.id):
        return
    if await refresh_flags():
        return
    try:
        await msg.reply_text(OFF_TEXT)
    except Exception:
        pass
    msg.stop_propagation()


__all__ = ["live_mic_on"]
