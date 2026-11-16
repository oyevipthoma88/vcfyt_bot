"""Login gate: jab tak user login nahi karta, control buttons kaam nahi karte.

Runs in group=-1 (before the real callback handlers).  For a user without a
saved/active session, every control callback (player, mic, audio, library,
status, bridge) shows a friendly "pehle login karo" screen instead.
"""
import re

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup as K

from config import Config
from plugins.ui import B, LINE, edit_screen, safe_answer

_GATED = re.compile(r"^(vc:|vct:|mic:|set:|aud:|menu:settings$|menu:status$)")


def _gated(_, __, cq):
    return bool(cq.data and _GATED.match(cq.data))


GATE_TEXT = (
    "🔒 <b>Pehle Login karo</b>\n"
    f"{LINE}\n"
    "Ye control sirf login ke baad khulta hai.\n\n"
    "1️⃣ <b>🔐 Login</b> dabao\n"
    "2️⃣ Phone number bhejo (+91…)\n"
    "3️⃣ OTP bhejo — bas ho gaya!\n"
    f"{LINE}\n"
    "Login ke baad Player, Live Mic, Audio aur Library sab unlock ho jayenge."
)

GATE_KB = K([
    [B("🔐 Login — Start here", callback_data="menu:login", style="success")],
    [B("📘 How to use", callback_data="menu:tutorial", style="primary"),
     B("🏠 Home", callback_data="menu:home", style="primary")],
])


@Client.on_callback_query(filters.create(_gated), group=-1)
async def login_gate(bot, cq):
    user = cq.from_user
    if not user or Config.is_owner(user.id):
        return
    from plugins.start import _is_logged_in
    if await _is_logged_in(user.id):
        return
    from helpers import sudo as _sudo
    if await _sudo.is_sudo_mode(user.id):
        # Sudo user sirf apne verified group ke controls chala sakta hai.
        ids = re.findall(r"-\d{7,}", cq.data or "")
        gc_id, _, _ = await _sudo.get_gc(user.id)
        if not ids or all(int(x) == gc_id for x in ids):
            return
        await safe_answer(cq, "🔒 Ye aapka verified group nahi hai", show_alert=True)
        cq.stop_propagation()
        return
    # Live Mic sirf Spare ID se bhi chalta hai — main login ke bina.
    if re.match(r"^mic:", cq.data or ""):
        return
    await safe_answer(cq, "🔒 Login ya Free Trial chahiye", show_alert=False)
    try:
        # Purana login screen + Free Trial option — user khud choose kare.
        mode = await _sudo.engine_mode(user.id)
        if mode == _sudo.TRIAL_OVER:
            from plugins.sudo import paywall_screen
            text, kb = await paywall_screen(user.id)
        else:
            from plugins.sudo import home_override
            text, kb = await home_override(
                user.id, user.first_name or "friend",
                notice="🔒 <b>Ye control Login ke baad (ya Free Trial me) khulta hai.</b>")
        await edit_screen(cq.message, text, reply_markup=kb)
    except Exception:
        try:
            await edit_screen(cq.message, GATE_TEXT, reply_markup=GATE_KB)
        except Exception:
            pass
    cq.stop_propagation()
