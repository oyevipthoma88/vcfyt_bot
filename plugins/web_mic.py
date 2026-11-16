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
    "🎤 <b>Live Mic · Browser Studio</b>\n\n"
    "0. 🔐 <code>/login</code> karo — ya sirf Live Mic ke liye ⚔️ <code>.fightid</code>\n"
    "1. Group me <code>.setgc</code>, ya DM me <code>.mic chat</code> / <code>.setgc</code>\n"
    "2. <code>.mic on</code> — target na ho to bot chat ID pooch lega\n"
    "3. Link <b>Chrome</b> me kholo (Telegram ke andar wala browser nahi) → 🎙 dabao → bolo\n\n"
    "• <code>.mic chat -100xxx</code> — target badlo  •  <code>.mic cancel</code> — ID prompt roko\n"
    "• <code>.mic off</code> — band  •  <code>.mic leave</code> — band + VC se bahar\n"
    "• <code>.mic status</code>  •  <code>.micmode loudest|phone|bass</code>\n"
    "• <code>.mic url https://your-app.herokuapp.com</code> — (owner) mic link ka address set karo\n\n"
    "<b>Sabse tez + saaf awaaz ke liye:</b>\n"
    "• Page par <b>MIC BOOST</b> slider — MIC LEVEL green rakho\n"
    "• <b>Earphone/headset</b> lagao — VC ki awaaz mic me wapas nahi aati, boost full lagta hai\n"
    "• Phone mic muh se 3–5 cm door rakho\n"
    "• Page ko screen par khula rakho (lock mat karo)\n"
    "• Kisi ID ko group admin banao → 200% sabke liye\n"
    "<i>Echo aaye (speaker par) to link ke end me <code>&aec=1</code> jodo. "
    "Phati lage to page par ✨ CLEAN dabao ya MIC BOOST kam karo.</i>")


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
    """Use only an explicitly saved Live Mic target.

    Never silently infer the destination from the chat where `.mic on` was
    sent: an explicit target prevents sending audio into the wrong VC.
    """
    raw = await db.get_app_value(f"mic_chat_{uid}")
    try:
        return int(raw) if raw else 0
    except (TypeError, ValueError):
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

    if action == "cancel":
        from plugins.live_mic_target import _await_chat
        _await_chat.pop(uid, None)
        await mic_notify(msg, "❎ Live Mic target prompt cancel kar diya.")
        return

    if action != "on":
        await mic_notify(msg, HELP, disable_web_page_preview=True)
        return

    from plugins.vc_commands import get_mic_engine
    from helpers.setup_flow import has_fight_id
    # Main ID zaroori nahi: Fight ID ho to wahi VC me bolti hai.
    uvc = await get_mic_engine(msg)
    if not uvc:
        return
    from helpers import sudo as _sudo
    sudo_trial = False
    own_fight = await has_fight_id(uid)
    if not own_fight and await _sudo.is_sudo_mode(uid):
        # Bina login: verified group zaroori + trial ho to 1 use katega.
        from plugins.sudo import gate_sudo_use
        if not await gate_sudo_use(msg):
            return
        sudo_trial = await _sudo.engine_mode(uid) in (_sudo.TRIAL, _sudo.TRIAL_OVER)
    cid = await _target(msg, uid)
    if not cid:
        from plugins.live_mic_target import request_target_input
        request_target_input(uid, start_after=True)
        await mic_notify(
            msg,
            "🎯 <b>Step 2: Group batao</b>\n\n"
            "Jis group me VC chal rahi hai uska <b>invite link</b>, <code>@username</code> "
            "ya group ID yahin bhejo.\n"
            "Group verify hote hi mic link aa jayega.\n"
            "<i>Prompt 5 minute active rahega; cancel: <code>.mic cancel</code></i>",
        )
        return
    base = await live_mic.resolve_base_url()
    if not base:
        # v85 ROOT FIX: pehle yahan bina check kiye purana / galat address
        # chala jata tha -> Heroku "There's nothing here, yet."  Ab sirf
        # verified address bhejte hain; nahi mila to saaf batate hain.
        await mic_notify(msg, _no_url_text(), disable_web_page_preview=True)
        if sudo_trial:
            await _sudo.refund_trial(uid)
        return
    # Bolne wali ID (Fight ID, warna main) ko group me pahunchao.
    try:
        from plugins.live_mic_target import ensure_in_target, target_ref
        ref, _ = await target_ref(uid, cid)
        relay = await session_manager.get_relay(uid)
        eng = relay or uvc
        await ensure_in_target(getattr(eng, "client", None), int(cid), ref)
    except Exception:
        pass
    speaker_note = ("⚔️ Aapki awaaz <b>Fight ID</b> se VC me jayegi — aapka phone VC me bana rahega.\n"
                    if await has_fight_id(uid) or await session_manager.assistant_string(uid) else
                    "🔐 Aapki awaaz <b>aapki ID</b> se VC me jayegi "
                    "(phone wala VC chhod dega — chaho to ⚔️ Fight ID jodo).\n")
    token = await live_mic.generate_token(uid, cid)
    url = f"{base.rstrip('/')}/?token={token}"
    kb = K([[B("🎙 MIC KHOLO (Chrome)", url=url)],
            [B("⏹ Mic OFF", callback_data="mic:off")]])
    await mic_notify(msg,
                     "🎤 <b>Step 3: Live Mic ready — ULTRA LOUD</b>\n\n"
                     f"🎯 Group: <code>{cid}</code>\n\n"
                     "👇 Button dabao → <b>Chrome</b> me khulega → 🎙 dabao → bolo.\n"
                     f"{speaker_note}"
                     "🎧 <b>Earphone lagao</b> = sabse tez + saaf.\n\n"
                     f"<i>Link private hai, kisi ko mat bhejna.</i>\n<code>{_html.escape(url)}</code>",
                     reply_markup=kb, disable_web_page_preview=True)


async def web_mic_panel(uid: int):
    """v93 Live Mic panel — ① ID (Login / Fight ID / Trial) ② Group ③ MIC ON.

    Login wale user se Fight ID kabhi zaroori nahi maangi jaati."""
    from helpers import live_mic
    from helpers.setup_flow import account_state, speaker_label
    s = live_mic.get_session(uid)
    live = live_mic.has_live_socket(uid)
    tgt = await db.get_app_value(f"mic_chat_{uid}")
    acc = await account_state(uid)
    fight = acc["fight"] or bool(await session_manager.assistant_string(uid))
    ready = acc["ready"] or fight
    who = speaker_label(acc["main"], fight, acc["sudo"])
    try:
        from helpers import live_mic_v79 as v79
        mm = s.audio_mode if s else v79.get_mode()
    except Exception:
        mm = "loudest"
    state = "🟢 <b>LIVE</b> — awaaz ja rahi hai" if live else (
        "🟡 Link khula, mic band" if s else "⚪ OFF")
    if ready:
        hint = ("<i>💡 ⚔️ Fight ID (optional) jodo to aapka phone VC me bana rahega.</i>"
                if acc["main"] and not fight else
                "<i>Link kisi ko mat bhejna.</i>")
    else:
        hint = "<i>🔐 Login karo (sab features) — ya sirf Live Mic ke liye ⚔️ Fight ID jodo.</i>"
    text = ("🎙️ <b>APEX LIVE MIC</b>\n"
            "<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>\n"
            f"{'✅' if ready else '①'} <b>ID</b>        "
            f"{who if ready else '— Login ya Fight ID'}\n"
            f"{'✅' if tgt else '②'} <b>Group</b>     "
            f"{f'<code>{_html.escape(str(tgt))}</code>' if tgt else '— set nahi'}\n"
            f"{'🟢' if live else '③'} <b>Mic</b>       {state}\n"
            f"🔊 <b>Sound</b>     {dict(loudest='⚡ LOUDEST', phone='📱 PHONE · saaf', bass='🔊 BASS · bhari').get(mm, '⚡ LOUDEST')}\n"
            "<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>\n"
            "Link <b>Chrome</b> me kholo · 🎧 earphone = kam echo\n"
            f"{hint}")
    rows = []
    if not ready:
        rows.append([B("🔐 Login", callback_data="menu:login", style="success"),
                     B("⚔️ Fight ID", callback_data="mic:acct", style="primary")])
    if s or live:
        rows.append([B("⏹ Mic OFF", callback_data="mic:off", style="danger"),
                     B("🚪 OFF + Leave", callback_data="mic:wleave", style="danger")])
    elif ready:
        rows.append([B("🎙 MIC ON — link do", callback_data="mic:won", style="success")])
    tgt_row = [B("🎯 Group badlo" if tgt else "🎯 Group set karo", callback_data="mic:wtarget",
                 style="primary")]
    if ready:
        tgt_row.append(B("⚔️ Fight ID" if fight else "⚔️ Fight ID (optional)",
                         callback_data="mic:acct", style="primary"))
    rows.append(tgt_row)
    rows.append([B("⚡ LOUDEST", callback_data="mic:wmode:loudest",
                   style="success" if mm == "loudest" else "primary"),
                 B("📱 PHONE", callback_data="mic:wmode:phone",
                   style="success" if mm == "phone" else "primary"),
                 B("🔊 BASS", callback_data="mic:wmode:bass",
                   style="success" if mm == "bass" else "primary")])
    rows.append([B("🔄 Refresh", callback_data="mic:wsetup", style="primary"),
                 B("📘 Guide", callback_data="tut:livemic", style="primary"),
                 B("❓ Help", callback_data="mic:whelp", style="primary")])
    rows.append([B("🏠 Home", callback_data="menu:home", style="primary")])
    return text, K(rows)
