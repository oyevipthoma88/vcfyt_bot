"""Live Mic = Web Studio (browser mic + screen/camera share + VC chat).

`.mic on` -> private Studio link.  Chrome me kholo, 🎙 dabao, bolo.
Sound: same 3 modes as audio playback (loudest | phone | bass), 0 dBFS.
"""
import html as _html

from pyrogram.types import InlineKeyboardMarkup as K

from helpers.database import db
from helpers.vc_manager import session_manager
from plugins.ui import B

HELP = (
    "🎤 <b>Live Mic · Web Studio</b>\n\n"
    "<b>Start (3 step)</b>\n"
    "1. <code>.mic chat -100xxx</code> — group set karo (ek baar)\n"
    "2. <code>.mic on</code> — private Studio link aayega\n"
    "3. Link <b>Chrome</b> me kholo → 🎙 dabao → bolo\n\n"
    "<b>Studio page par</b>\n"
    "🎙 <b>Mic</b> — Loudest / Phone / Bass mode, PUSH slider\n"
    "🖥 <b>Screen</b> — screen share (PC) ya 📷 camera (phone) VC me\n"
    "💬 <b>Chat</b> — group messages padho + bhejo\n"
    "👥 <b>VC</b> — kaun VC me hai, kaun bol raha hai\n\n"
    "<b>Commands</b>\n"
    "<code>.mic off</code> · <code>.mic leave</code> · <code>.mic status</code>\n"
    "<code>.micmode loudest|phone|bass</code>\n"
    "<code>.mic url https://app.herokuapp.com</code> — (owner) link address\n\n"
    "<b>Sabse tez awaaz</b>: 🎧 earphone lagao · mic 3–5 cm · Telegram app me apna mic mute · "
    "kisi ID ko group admin banao (200% sabke liye).")


def _url_line(base: str) -> str:
    if base:
        return f"✅ <code>{_html.escape(base)}</code>"
    return "❌ address verify nahi hua — <code>.mic url</code> bhejo"


def _no_url_text() -> str:
    from helpers import mic_url
    tried = mic_url.explain_failure()
    return ("❌ <b>Mic page ka sahi address nahi mila</b>\n"
            "Isliye link nahi bheja (warna Heroku \"There's nothing here, yet\" khulta).\n\n"
            + (f"<b>Check kiye:</b>\n{tried}\n\n" if tried else "")
            + "<b>Fix (sirf 1 baar):</b>\n"
            "1. Heroku Dashboard → apni app → <b>Open app</b> dabao.\n"
            "2. Jo address khule (jaise <code>https://my-app-1a2b3c4d5e6f.herokuapp.com</code>) copy karo.\n"
            "3. Bot DM me bhejo: <code>.mic url https://my-app-1a2b3c4d5e6f.herokuapp.com</code>\n\n"
            "<i>Ya Heroku Config Vars me <code>LIVE_MIC_BASE_URL</code> sahi karo / hata do. "
            "Auto-detect ke liye: <code>heroku labs:enable runtime-dyno-metadata -a APP</code></i>")


async def _set_url(msg, uid, args):
    from config import Config
    from helpers import live_mic, mic_url
    from plugins.vc_commands import mic_notify
    if not args:
        base = await live_mic.resolve_base_url(force=True)
        await mic_notify(msg, ("🌐 <b>Mic page address</b>\n\n"
                               + (f"✅ Working: <code>{_html.escape(base)}</code>" if base else _no_url_text())
                               + "\n\nBadalne ke liye: <code>.mic url https://your-app.herokuapp.com</code>\n"
                               "Auto par wapas: <code>.mic url reset</code>"),
                         disable_web_page_preview=True)
        return
    if not Config.is_owner(uid):
        await mic_notify(msg, "⛔ Sirf bot owner mic address badal sakta hai.")
        return
    ok, info = await mic_url.set_override(args[0])
    if ok and not info:
        base = await live_mic.resolve_base_url(force=True)
        await mic_notify(msg, "♻️ Mic address auto-detect par wapas.\n"
                         + (f"✅ <code>{_html.escape(base)}</code>" if base else _no_url_text()),
                         disable_web_page_preview=True)
    elif ok:
        await mic_notify(msg, f"✅ <b>Mic address save + verify ho gaya</b>\n<code>{_html.escape(info)}</code>\n\n"
                         "Ab <code>.mic on</code> karo — link sahi khulega.",
                         disable_web_page_preview=True)
    elif info == "invalid":
        await mic_notify(msg, "❌ Ye address sahi nahi lag raha. Example:\n"
                         "<code>.mic url https://my-app-1a2b3c4d5e6f.herokuapp.com</code>")
    else:
        await mic_notify(msg, "❌ Ye address is bot ko nahi khol raha ("
                         + ("Heroku 'nothing here' / doosri app" if info == "foreign" else "khula hi nahi")
                         + ").\nHeroku Dashboard → <b>Open app</b> wala exact address bhejo.",
                         disable_web_page_preview=True)


async def _target(msg, uid):
    raw = await db.get_app_value(f"mic_chat_{uid}")
    if raw:
        return int(raw)
    if msg.chat and msg.chat.id < 0:
        try:
            from helpers.mic_target import save_target
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
                         f"🎯 Target: <code>{tgt or '— set nahi'}</code>\n"
                         f"🌐 Mic page: {_url_line(await live_mic.resolve_base_url())}",
                         disable_web_page_preview=True)
        return

    if action == "url":
        await _set_url(msg, uid, list(args))
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
        # v85 ROOT FIX: pehle yahan bina check kiye purana / galat address
        # chala jata tha -> Heroku "There's nothing here, yet."  Ab sirf
        # verified address bhejte hain; nahi mila to saaf batate hain.
        await mic_notify(msg, _no_url_text(), disable_web_page_preview=True)
        return
    # Playing ID (spare if set, else main) ko group me pahunchao.
    try:
        from helpers.mic_target import ensure_in_target, target_ref
        ref, _ = await target_ref(uid, cid)
        relay = await session_manager.get_relay(uid)
        eng = relay or uvc
        await ensure_in_target(getattr(eng, "client", None), int(cid), ref)
    except Exception:
        pass
    token = await live_mic.generate_token(uid, cid)
    url = f"{base.rstrip('/')}/?token={token}"
    mode = await user_mode(uid)
    ico, name, _ = MODE_INFO.get(mode, MODE_INFO["loudest"])
    kb = K([[B("🎙 STUDIO KHOLO (Chrome)", url=url)],
            [B("⏹ Mic OFF", callback_data="mic:off", style="danger"),
             B("🎛 Panel", callback_data="mic:panel", style="primary")]])
    await mic_notify(msg,
                     "🎤 <b>Live Mic ready</b>\n\n"
                     f"🎯 Group <code>{cid}</code>  ·  {ico} <b>{name}</b> · 0 dBFS\n\n"
                     "👇 Button dabao → <b>Chrome</b> me khulega → 🎙 dabao → bolo.\n"
                     "Page par 🖥 Screen share · 💬 Chat · 👥 VC bhi hai.\n\n"
                     f"<i>Link private hai, kisi ko mat bhejna.</i>\n<code>{_html.escape(url)}</code>",
                     reply_markup=kb, disable_web_page_preview=True)


async def user_mode(uid: int) -> str:
    from helpers import live_mic_v79 as v79
    try:
        return v79.normalize_mode(await db.get_app_value(f"mic_mode_{uid}") or "") or v79.get_mode()
    except Exception:
        return "loudest"


async def set_user_mode(uid: int, mode: str) -> str:
    """Per-user live mic mode; a running mic switches at once (VC stays)."""
    from helpers import live_mic, live_mic_v79 as v79
    m = v79.normalize_mode(mode)
    if not m:
        raise ValueError(mode)
    s = live_mic.get_session(uid)
    if s is not None and not s._closed:
        await s.set_mode(m)
    else:
        await db.set_app_value(f"mic_mode_{uid}", m)
    return m


MODE_INFO = {
    "loudest": ("⚡", "Loudest", "fight · sabse tez"),
    "phone": ("📱", "Phone", "tez + saaf shabd"),
    "bass": ("🔊", "Bass", "damdar · heavy"),
}


async def web_mic_panel(uid: int):
    """v90: ek saaf Live Mic panel (Web Studio)."""
    from helpers import live_mic
    s = live_mic.get_session(uid)
    live = live_mic.has_live_socket(uid)
    tgt = await db.get_app_value(f"mic_chat_{uid}")
    mm = await user_mode(uid)
    ico, name, desc = MODE_INFO.get(mm, MODE_INFO["loudest"])
    sharing = bool(s and getattr(s, "screen_feed", None) is not None)
    state = ("🟢 <b>LIVE</b> — awaaz VC me ja rahi hai" + (" · 🖥 share ON" if sharing else "")
             if live else ("🟡 Link khula hai, mic band" if s else "⚪ OFF"))
    text = ("🎤 <b>LIVE MIC  ·  Web Studio</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Status  {state}\n"
            f"🎯 Group  <code>{tgt or 'set nahi'}</code>\n"
            f"{ico} Mode  <b>{name}</b> · {desc} · 0 dBFS\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Studio page: 🎙 Mic · 🖥 Screen/Camera · 💬 Chat · 👥 VC\n"
            "<i>🎧 Earphone + mic 3–5 cm = sabse tez + saaf</i>")

    def mbtn(m):
        i, n, _ = MODE_INFO[m]
        return B(("✅ " if m == mm else i + " ") + n, callback_data=f"mic:wmode:{m}",
                 style="success" if m == mm else "primary")

    rows = []
    if s or live:
        rows.append([B("⏹ Mic OFF", callback_data="mic:off", style="danger"),
                     B("🚪 OFF + Leave VC", callback_data="mic:wleave", style="danger")])
    else:
        rows.append([B("🎙 MIC ON — Studio link", callback_data="mic:won", style="success")])
    rows.append([mbtn("loudest"), mbtn("phone"), mbtn("bass")])
    rows.append([B("🎯 Group", callback_data="mic:wtgt", style="primary"),
                 B("👥 Spare ID", callback_data="mic:acct", style="primary"),
                 B("❓ Help", callback_data="mic:whelp", style="primary")])
    rows.append([B("🔄 Refresh", callback_data="mic:panel", style="primary"),
                 B("🏠 Home", callback_data="menu:home", style="primary")])
    return text, K(rows)
