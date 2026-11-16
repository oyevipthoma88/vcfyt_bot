"""v94 Smart Setup Guide — naye user ko hamesha pata ho ki AGLA step kya hai.

Har screen par sirf ek bada "👉 Next" button.  State DB se live padhi jaati hai:

  ① 🔐 Awaaz wali ID  — Login (number + OTP) / ⚔️ Fight ID / 🎁 Free Trial
  ② 🎯 Group set       — kis group ke VC me bajana hai (ek hi baar)
  ③ 🎙 Voice Chat      — group me VC start karo (user khud karta hai)
  ④ ▶️ Chalao          — 🎤 Live Mic ya audio par reply karke .play

``build_guide(state)`` pure function hai (koi network / DB nahi) taaki test ho sake.
"""
from __future__ import annotations

import html as _html

TOTAL_STEPS = 3  # ①② check hote hai, ③ VC start, phir ready


async def _has_group(uid: int) -> int:
    """Default group / Live Mic target / Sudo group — jo bhi pehle mile."""
    try:
        from plugins.vc_commands import DEFAULT_GC
        cid = DEFAULT_GC.get(int(uid))
        if cid:
            return int(cid)
    except Exception:
        pass
    try:
        from helpers.database import db
        raw = (await db.get_app_value(f"mic_chat_{int(uid)}") or "").strip()
        if raw and raw.lstrip("-").isdigit() and int(raw) != 0:
            return int(raw)
    except Exception:
        pass
    try:
        from helpers import sudo as _sudo
        gc_id, _, _ = await _sudo.get_gc(int(uid))
        if gc_id:
            return int(gc_id)
    except Exception:
        pass
    return 0


async def setup_state(uid: int) -> dict:
    """Live setup status for one user. Never raises."""
    main = fight = sudo_mode = False
    try:
        from helpers.setup_flow import account_state
        acc = await account_state(uid)
        main, fight, sudo_mode = bool(acc.get("main")), bool(acc.get("fight")), bool(acc.get("sudo"))
    except Exception:
        pass
    if not main:
        try:
            from plugins.start import _is_logged_in
            main = await _is_logged_in(int(uid))
        except Exception:
            pass
    trial_available = False
    if not (main or fight or sudo_mode):
        try:
            from helpers import sudo as _sudo
            mode = await _sudo.engine_mode(int(uid))
            trial_available = mode == _sudo.NONE and bool(await _sudo.accounts())
        except Exception:
            trial_available = False
    group = await _has_group(uid) if (main or fight or sudo_mode) else 0
    return {
        "main": main, "fight": fight, "sudo": sudo_mode,
        "voice": bool(main or fight or sudo_mode),
        "group": int(group or 0),
        "trial_available": trial_available,
    }


def progress(state: dict) -> tuple[int, int]:
    """(done, total) — ③ VC start user khud karta hai, isliye max 2/3 tak auto-check."""
    voice = bool(state.get("voice"))
    done = int(voice) + int(voice and bool(state.get("group")))
    return done, TOTAL_STEPS


def is_complete(state: dict) -> bool:
    return bool(state.get("voice") and state.get("group"))


def next_step(state: dict) -> tuple[str, str]:
    """(button label, callback_data) for the single most useful next action."""
    if not state.get("voice"):
        return "🔐 Login karo · number + OTP", "menu:login"
    if not state.get("group"):
        if state.get("sudo") and not state.get("main"):
            return "🎯 Group set karo", "sudo:gc"
        return "🎯 Group set karo", "guide:setgc"
    if not state.get("fight") or state.get("main"):
        try:
            from helpers.feature_flags import live_mic_on
            if not live_mic_on():
                return "▶️ Player kholo", "vc:list:0"
        except Exception:
            pass
    return "🎤 Live Mic kholo", "mic:panel"


def _mark(done: bool, current: bool) -> str:
    if done:
        return "✅"
    return "👉" if current else "⬜"


def _voice_label(state: dict) -> str:
    if state.get("main"):
        return "🔐 Aapki ID (Login)"
    if state.get("fight"):
        return "⚔️ Fight ID"
    if state.get("sudo"):
        return "🎁 Trial / Sudo ID"
    return ""


def build_guide(state: dict, name: str = "friend"):
    """Return (html_text, rows) — rows = list of [(label, callback, style)] lists."""
    from plugins.ui import card, _bar
    voice, group = bool(state.get("voice")), int(state.get("group") or 0)
    done, total = progress(state)
    s1_cur = not voice
    s2_cur = voice and not group
    s3_cur = voice and bool(group)

    lines = [f"👋 <b>{_html.escape(str(name or 'friend'))}</b>, setup <b>{done}/{total}</b>  {_bar(done, total, 6)}", ""]
    vl = _voice_label(state)
    lines.append(f"{_mark(voice, s1_cur)} <b>① Awaaz wali ID</b>" + (f" — {vl}" if vl else ""))
    if s1_cur:
        lines.append("    Apne number + OTP se login karo (1 minute). Bot aapki ID se VC me bolega.")
    lines.append(f"{_mark(bool(group), s2_cur)} <b>② Group set</b>"
                 + (f" — <code>{group}</code>" if group else ""))
    if s2_cur:
        lines.append("    Jis group ke VC me bajana hai uska <b>invite link</b>, <b>@username</b> ya <b>ID</b> bhejo.\n"
                     "    ⚠️ Aapki ID us group ki <b>member</b> honi chahiye.")
    lines.append(f"{_mark(False, s3_cur)} <b>③ Group me Voice Chat start karo</b> → phir chalao")
    if s3_cur:
        lines.append("    🎤 <b>Live Mic</b> — phone se bolo, VC me tez + saaf\n"
                     "    ▶️ <b>Audio</b> — group me audio par reply karke <code>.play</code>")
    lines += ["", ("🎉 <b>Setup ho gaya!</b> Neeche se shuru karo." if s3_cur
                   else "👇 Bas neeche wala <b>hara button</b> dabao.")]

    tip = ("Commands: <code>.</code> har jagah chalta hai (jaise <code>.play</code>), "
           "<code>/</code> sirf bot ke DM me. Kisi bhi command ki info: <code>.help play</code>")
    text = card("🧭", "Setup guide", "\n".join(lines), tip)

    label, cb = next_step(state)
    rows = [[(label, cb, "success")]]
    if not voice:
        alt = []
        if state.get("trial_available"):
            alt.append(("🎁 Free Trial · bina login", "sudo:trial", "primary"))
        alt.append(("⚔️ Sirf Live Mic (Fight ID)", "mic:acct", "primary"))
        rows.append(alt)
    elif s3_cur:
        rows.append([("▶️ Player", "vc:list:0", "primary"), ("📘 .play kaise?", "tut:play", "primary")])
        rows.append([("🎯 Group badlo", "sudo:gc" if (state.get("sudo") and not state.get("main")) else "guide:setgc", "primary")])
    rows.append([("🔄 Refresh", "guide:open", "primary"), ("🏠 Home", "menu:home", "primary")])
    return text, rows


def to_markup(rows):
    from pyrogram.types import InlineKeyboardMarkup as K
    from plugins.ui import B
    return K([[B(lbl, callback_data=cb, style=st) for (lbl, cb, st) in row] for row in rows])


async def guide_screen(uid: int, name: str = "friend"):
    state = await setup_state(uid)
    text, rows = build_guide(state, name)
    return text, to_markup(rows)


def home_hint_row(state: dict):
    """Home par sabse upar 'Next step' button (setup adhoora ho to). None = complete."""
    if is_complete(state):
        return None
    done, total = progress(state)
    label, _ = next_step(state)
    return [(f"🧭 Setup {done}/{total} · Next: {label.split(' ·')[0]}", "guide:open", "success")]


SETGC_PROMPT = (
    "🎯 <b>GROUP SET KARO</b>\n"
    "<code>┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄</code>\n"
    "Jis group ke Voice Chat me bajana hai, uska ek bhejo:\n"
    "• Invite link — <code>https://t.me/+AbCd…</code>\n"
    "• Public — <code>@groupname</code>\n"
    "• ID — <code>-1001234567890</code>\n\n"
    "⚠️ Aapki logged-in ID (ya Fight ID) us group ki member honi chahiye.\n"
    "<i>Group ke andar ho to wahan bas <code>.setgc</code> bhej do.</i>"
)
