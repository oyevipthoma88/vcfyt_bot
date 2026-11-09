"""v81 Live Mic = WEB BROWSER MIC (VC Bridge hata diya).

`.mic on` -> private link.  Phone Chrome me kholo, 🎙 dabao, bolo.
Browser: echo-cancel / noise-suppression / auto-gain OFF (phone awaaz nahi
dabata) + FIGHT pre-chain (presence +6 dB @2.7 kHz, 12 dB pre-amp, 6:1
comp, brick-wall limiter).  Server: live_mic_v79 LOUDEST chain -> VC.
"""
import html as _html

from pyrogram.types import InlineKeyboardMarkup as K

from helpers.database import db
from helpers.vc_manager import session_manager
from plugins.ui import B

HELP = (
    "🎤 <b>Live Mic (Web Browser) — ULTRA LOUD</b>\n\n"
    "1. <code>.mic chat -100xxx</code> — target group set karo (ek baar)\n"
    "2. <code>.mic on</code> — private link milega\n"
    "3. Link <b>Chrome</b> me kholo (Telegram ke andar wala browser nahi) → 🎙 dabao → bolo\n\n"
    "• <code>.mic off</code> — band  •  <code>.mic leave</code> — band + VC se bahar\n"
    "• <code>.mic status</code>  •  <code>.micmode loudest|clean</code>\n\n"
    "<b>Sabse tez awaaz ke liye:</b>\n"
    "• <b>Earphone/headset</b> lagao — VC ki awaaz mic me wapas nahi aati, boost full lagta hai\n"
    "• Phone mic muh se 3–5 cm door rakho\n"
    "• Page ko screen par khula rakho (lock mat karo)\n"
    "• Kisi ID ko group admin banao → 200% sabke liye\n"
    "<i>Echo aaye (speaker par) to link ke end me <code>&aec=1</code> jodo. "
    "Phati lage to <code>&boost=clean</code>.</i>")


async def _target(msg, uid):
    raw = await db.get_app_value(f"mic_chat_{uid}")
    if raw:
        return int(raw)
    if msg.chat and msg.chat.id < 0:
        try:
            from plugins.vc_bridge import save_target
            await save_target(uid, msg.chat.id)
        except Exception:
            pass
        return msg.chat.id
    return 0


async def run_web_mic(msg, action: str, args=()):
    from helpers import live_mic
    from plugins.vc_commands import mic_notify, get_engine
    uid = msg.from_user.id

    if action in {"off", "leave"}:
        ok = await live_mic.stop_session(uid, leave_vc=(action == "leave"))
        await db.set_app_value(f"live_mic_token_{uid}", "")  # burn link
        uvc = await session_manager.get(uid)
        if uvc:
            try:
                await live_mic.reset_mic_state(uvc, uid)
            except Exception:
                pass
        await mic_notify(msg, ("⏹ <b>Live Mic OFF</b>" + (" — VC se bahar" if action == "leave" else ""))
                         if ok else "Live Mic chal nahi raha tha.")
        return

    if action == "status":
        s = live_mic.get_session(uid)
        tgt = await db.get_app_value(f"mic_chat_{uid}")
        live = live_mic.has_live_socket(uid)
        await mic_notify(msg, "🎤 <b>Live Mic status</b>\n\n"
                         f"Status: {'🟢 LIVE' if live else ('🟡 link khula, mic band' if s else '⚪ OFF')}\n"
                         f"🎯 Target: <code>{tgt or '— set nahi'}</code>")
        return

    if action != "on":
        await mic_notify(msg, HELP, disable_web_page_preview=True)
        return

    uvc = await get_engine(msg)
    if not uvc:
        return
    cid = await _target(msg, uid)
    if not cid:
        await mic_notify(msg, "🎯 Pehle target group set karo: <code>.mic chat -100xxxxxxxx</code>")
        return
    base = await live_mic.resolve_base_url()
    if not base:
        await mic_notify(msg, "❌ Web link ka address nahi mila. Hosting me env "
                         "<code>LIVE_MIC_BASE_URL=https://your-app-url</code> set karo.")
        return
    # Playing ID (spare if set, else main) ko group me pahunchao.
    try:
        from plugins.vc_bridge import ensure_in_target, target_ref
        ref, _ = await target_ref(uid, cid)
        relay = await session_manager.get_relay(uid)
        eng = relay or uvc
        await ensure_in_target(getattr(eng, "client", None), int(cid), ref)
    except Exception:
        pass
    token = await live_mic.generate_token(uid, cid)
    url = f"{base.rstrip('/')}/?token={token}"
    kb = K([[B("🎙 MIC KHOLO (Chrome)", url=url)],
            [B("⏹ Mic OFF", callback_data="mic:off")]])
    await mic_notify(msg,
                     "🎤 <b>Live Mic ready — ULTRA LOUD</b>\n\n"
                     f"🎯 Group: <code>{cid}</code>\n\n"
                     "👇 Button dabao → <b>Chrome</b> me khulega → 🎙 dabao → bolo.\n"
                     "🎧 <b>Earphone lagao</b> = sabse tez + saaf.\n\n"
                     f"<i>Link private hai, kisi ko mat bhejna.</i>\n<code>{_html.escape(url)}</code>",
                     reply_markup=kb, disable_web_page_preview=True)


async def web_mic_panel(uid: int):
    """v82: ek saaf Live Mic control panel (web browser mic)."""
    from helpers import live_mic
    s = live_mic.get_session(uid)
    live = live_mic.has_live_socket(uid)
    tgt = await db.get_app_value(f"mic_chat_{uid}")
    try:
        from helpers import live_mic_v79 as v79
        mm = v79.get_mode()
    except Exception:
        mm = "loudest"
    state = "🟢 <b>LIVE</b> — awaaz ja rahi hai" if live else (
        "🟡 Link khula, mic band" if s else "⚪ OFF")
    text = ("🎤 <b>LIVE MIC — Web Browser</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Status : {state}\n"
            f"🎯 Group : <code>{tgt or 'set nahi'}</code>\n"
            f"🔊 Server mode : <b>{str(mm).upper()}</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "🎧 Earphone + mic 3–5 cm = sabse tez\n"
            "📊 Page par MIC LEVEL green rakho")
    rows = []
    if s or live:
        rows.append([B("⏹ Mic OFF", callback_data="mic:off", style="danger"),
                     B("🚪 OFF + Leave", callback_data="mic:wleave", style="danger")])
    else:
        rows.append([B("🎙 MIC ON — link do", callback_data="mic:won", style="success")])
    rows.append([B("⚡ Mic LOUDEST", callback_data="mic:wmode:loudest",
                   style="success" if mm == "loudest" else "primary"),
                 B("✨ Mic CLEAN", callback_data="mic:wmode:clean",
                   style="success" if mm == "clean" else "primary")])
    rows.append([B("🔄 Refresh", callback_data="mic:panel", style="primary"),
                 B("❓ Help", callback_data="mic:whelp", style="primary")])
    rows.append([B("🌉 Old VC Bridge", callback_data="mic:bridge", style="primary"),
                 B("🏠 Home", callback_data="menu:home", style="primary")])
    return text, K(rows)
