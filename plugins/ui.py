
from enum import Enum
from urllib.parse import urlparse

from pyrogram import filters as _filters
from pyrogram.types import InlineKeyboardButton as _InlineKeyboardButton
from pyrogram.types import InlineKeyboardMarkup as K
from pyrogram.errors import RPCError

from config import Config

# Anonymous admins / channel-sender messages have no `from_user`; every command
# handler dereferences msg.from_user.id, so those messages used to blow up in the
# dispatcher and the command looked "dead".  Filter them out up front.
HAS_USER = _filters.create(lambda _, __, m: bool(getattr(m, "from_user", None)))

# Command prefix rule:  "." works everywhere (groups + DM).
# "/" works ONLY in bot DM (private chat).  In groups, "/" is a Telegram bot
# command trigger that every bot competes for, so we reserve it for DM use.
import re as _re

def cmd_prefix(pattern: str, flags=0):
    """Build a filter that enforces the prefix rule.

    pattern must start with the command word (no prefix), e.g. r"play(?!force)\\b".
    The filter matches:
      - "." + pattern  in any chat (group or private)
      - "/" + pattern  only in private chats
    """
    # "." "/" and "!" all work in groups AND DM.  "/boost@MyBot 5" style
    # (Telegram's command menu in groups) is accepted too.  The old check
    # compared m.chat.type to the string "private", but pyrofork uses the
    # ChatType enum, so every "/" command silently failed.
    any_re = _re.compile(r"^[./!]" + pattern, flags)

    def _f(_, __, m):
        text = (getattr(m, "text", None) or getattr(m, "caption", None) or "").lstrip()
        return bool(any_re.match(text))

    return _filters.create(_f)

def cmd_text(m) -> str:
    """Command text of a message.

    A command sent as a media caption (e.g. an audio with caption ".play song")
    matched the handler but `msg.text` was None, so `msg.text.strip()` raised
    AttributeError and the command silently died.
    """
    return (getattr(m, "text", None) or getattr(m, "caption", None) or "").strip()


GEN = Config.SESSION_BOT_LINK
GEN_NAME = f"@{Config.SESSION_BOT_USERNAME}"
SOURCE_CODE_URL = Config.SOURCE_CODE_URL

def set_source_code_url(url: str) -> str:
    global SOURCE_CODE_URL
    value = (url or "").strip()
    if value and urlparse(value).scheme not in {"http", "https"}:
        raise ValueError("URL must start with http:// or https://")
    SOURCE_CODE_URL = value
    return SOURCE_CODE_URL

def source_button():
    return [[B("💻 Source Code", url=SOURCE_CODE_URL)]] if SOURCE_CODE_URL else []

LINE = "━━━━━━━━━━━━━━━━━━━━"

class ButtonStyle(str, Enum):

    SUCCESS = "success"

_STYLE_EMOJI = {
    "primary": "🔹",
    "success": "✅",
    "danger": "❌",
}

async def edit_screen(message, text: str, reply_markup=None, **kwargs):
    try:
        return await message.edit_text(text, reply_markup=reply_markup, **kwargs)
    except (RPCError, TypeError, AttributeError):
        if not any(getattr(message, kind, None) for kind in
                   ("photo", "video", "animation", "document", "audio")):
            raise
        try:
            return await message.edit_caption(text, reply_markup=reply_markup)
        except RPCError:
            try:
                await message.delete()
            except Exception:
                pass
            return await message._client.send_message(
                message.chat.id, text, reply_markup=reply_markup, **kwargs)

async def safe_answer(cq, text: str = "", **kwargs):
    try:
        return await cq.answer(text, **kwargs)
    except RPCError as exc:
        if type(exc).__name__ != "QueryIdInvalid":
            raise
        return None

_DANGER_WORDS = ("logout", "cancel", "stop", "reset", "delete", "untag", "ban", "restart", "off")
_SUCCESS_WORDS = ("login", "addstring", "apply", "resume", "save", "send", "start", "on", "max")

def _is_danger(text: str, callback_data: str = None) -> bool:
    haystack = f"{callback_data or ''} {text}".lower()
    return any(w in haystack for w in _DANGER_WORDS)

def _is_success(text: str, callback_data: str = None) -> bool:
    haystack = f"{callback_data or ''} {text}".lower()
    return any(w in haystack for w in _SUCCESS_WORDS)

def B(text: str, callback_data: str = None, style: str = None,
      icon_custom_emoji_id=None, **kwargs):
    params = dict(kwargs)
    if callback_data is not None:
        params["callback_data"] = callback_data

    if style is None:
        if _is_danger(text, callback_data):
            style = "danger"
        elif _is_success(text, callback_data):
            style = "success"
        else:

            style = "primary"

    del icon_custom_emoji_id
    if isinstance(style, ButtonStyle):
        style = style.value
    if style:
        try:
            return _InlineKeyboardButton(text, style=style, **params)
        except (TypeError, ValueError):

            pass
    emoji = _STYLE_EMOJI.get(style, "")
    if emoji and not text.startswith(tuple(_STYLE_EMOJI.values())):
        text = f"{emoji} {text}"
    button = _InlineKeyboardButton(text, **params)
    if style:

        button._bot_api_style = style
    return button

def home_text(name: str, logged_in: bool) -> str:
    status = "✅ Account connected" if logged_in else "⚠️ Account connected nahi hai"
    if logged_in:
        steps = (
            "<b>Ab kya karein?</b>\n"
            "1️⃣ Bot ko apne group me add karo\n"
            "2️⃣ Group me <code>.setgc</code> likho (sirf ek baar)\n"
            "3️⃣ Group ki voice chat start karo\n"
            "4️⃣ Kisi audio ko reply karke <code>.play</code> likho\n"
            "5️⃣ <b>Now Playing</b> button se sab control karo"
        )
    else:
        steps = (
            "<b>Shuru kaise karein? (2 min)</b>\n"
            "1️⃣ Neeche <b>🔐 Login</b> dabao\n"
            "2️⃣ Apna phone number bhejo (jaise +91…)\n"
            "3️⃣ Telegram ka OTP bot ko bhejo\n"
            "4️⃣ Done! Fir group me <code>.setgc</code> aur <code>.play</code>"
        )
    return (
        f"🎧 <b>APEX VC FYT BOT</b>\n"
        f"<blockquote>👋 Welcome, <b>{name}</b>!\n"
        f"Loud VC audio • Live mic boost • VC chat</blockquote>\n"
        f"<b>Status:</b> {status}\n"
        f"{LINE}\n"
        f"{steps}\n"
        f"{LINE}\n"
        f"❓ Samajh na aaye to <b>📘 Tutorial</b> dabao."
    )

def home_kb(is_owner: bool = False, logged_in: bool = False,
            active_chat_id: int = None) -> K:
    now = (f"vc:now:{active_chat_id}" if active_chat_id is not None else "vc:list:0")
    rows = []
    if not logged_in:
        rows.append([B("🔐 Login — Start here", callback_data="menu:login", style="success")])
    rows += [
        [B("▶️ Now Playing", callback_data=now, style="success"),
         B("🎤 Live Mic", callback_data="mic:panel", style="success")],
        [B("🎚️ Audio Controls", callback_data="menu:settings", style="primary"),
         B("🎵 Library", callback_data="aud:menu", style="primary")],
        [B("👤 My Status", callback_data="menu:status", style="primary"),
         B("📘 How to use", callback_data="menu:tutorial", style="primary")],
        [B("💎 Premium", callback_data="pay:menu", style="primary"),
         B("👥 Refer & Earn", callback_data="ref:menu", style="primary")],
    ]
    if logged_in:
        rows.append([B("🔁 Re-Login", callback_data="menu:login", style="primary"),
                     B("🚪 Logout", callback_data="menu:logout", style="danger")])
    if is_owner:
        rows.append([B("👑 Owner Panel", callback_data="adm_back", style="success")])
    rows.extend(source_button())
    return K(rows)

def back_kb(target: str = "menu:home") -> K:
    return K([[B("⬅ Back", callback_data=target, style="primary"),
               B("🏠 Home", callback_data="menu:home", style="primary")]])

LOGIN_INTRO = (
    " <b>Login — apna account connect karein</b>\n\n"
    f"{LINE}\n"
    "<b>Phone Login</b>\n"
    "• Phone number bhejein (country code ke saath)\n"
    "• Telegram jo OTP bhejega wo bot ko dein\n"
    "• 2-step password ho to wo bhi\n"
    "• Bot khud aapka session bana lega\n\n"
    " Session sirf aapke VC control ke liye use hota hai."
)

def login_kb() -> K:
    rows = [
        [B(" Phone se Login", callback_data="login:phone")],
        [B(" Home", callback_data="menu:home")],
    ]
    rows.extend(source_button())
    return K(rows)

CANCEL_KB = K([[B(" Cancel", callback_data="login:cancel")]])

def settings_text(s: dict) -> str:
    bars = lambda n: "█" * n + "░" * (10 - n)
    vol = int(s.get('relay_volume', Config.RELAY_DEFAULT_VOLUME))
    return (
        "🎚️ <b>Audio Controls</b>\n"
        f"{LINE}\n"
        f"🔊 <b>Volume:</b> <code>{bars(min(10, vol // 100))} {vol}/1000</code>\n"
        f"🎵 <b>Bass:</b> <code>{s['bass']}/100</code>\n"
        f"💥 <b>Boost:</b> <code>{bars(int(s['boost']))} {s['boost']}/10</code>\n"
        f"🔁 <b>Echo:</b> {'ON' if s['echo'] else 'OFF'}\n"
        f"{LINE}\n"
        "Button dabao → <b>✅ Apply</b> dabao, chal raha gaana turant update.\n"
        "Ya group me likho: <code>/boost 10</code>, <code>/echo on</code>, <code>/vol 1000</code>"
    )

def settings_kb() -> K:
    # Simple panel: only the controls people actually use.
    return K([
        [B("🔉 Volume −", callback_data="set:relay:-100"),
         B("Volume + 🔊", callback_data="set:relay:100")],
        [B("🎵 Bass −", callback_data="set:bass:-5"),
         B("Bass + 🎵", callback_data="set:bass:5")],
        [B("💥 Boost −", callback_data="set:boost:-1"),
         B("Boost + 💥", callback_data="set:boost:1")],
        [B("🔁 Echo On/Off", callback_data="set:echo:toggle")],
        [B("🔥 MAX LOUD", callback_data="set:max"),
         B("🔄 Reset", callback_data="set:reset")],
        [B("✅ Apply to playing song", callback_data="set:apply")],
        [B("🏠 Home", callback_data="menu:home")],
    ])

def mic_text(s: dict, mic_on: bool = False, mic_title: str = "",
             live_vol: int = None) -> str:
    bars = lambda n: "█" * n + "░" * (10 - n)
    lv = live_vol if live_vol is not None else s.get("live_volume", Config.LIVE_BOOST_DEFAULT)
    pct = round(int(lv) / 200)
    status_line = "🔴 LIVE (mic ON)" if mic_on else "⚪ Mic OFF"
    device_line = f"📱 <b>Input:</b> {mic_title}\n" if mic_title else ""
    return (
        "🎤 <b>Live Mic Control</b>\n\n"
        f"{LINE}\n"
        f"📡 <b>Status:</b> {status_line}\n"
        f"{device_line}"
        f"🔊 <b>Mic Gain:</b> <code>{lv}/20000</code> "
        f"({pct}% — Telegram participant volume)\n"
        f"📈 <b>Audio Gain:</b> <code>{s.get('gain', Config.RELAY_DEFAULT_GAIN)}/200</code>\n"
        f"🎵 <b>Bass:</b> <code>+{s['bass']} dB</code>\n"
        f"💥 <b>Boost:</b> <code>{bars(int(s['boost']))} {s['boost']}/10</code>\n"
        f"🔊 <b>Echo:</b> {'ON' if s['echo'] else 'OFF'} "
        f"<code>{bars(int(s['echo_level']))} {s['echo_level']}/10</code>\n"
        f"{LINE}\n\n"
        "<b>Mic ON</b> = live aawaz VC mein max boost ke saath jayegi.\n"
        "<b>Max Boost</b> = mic gain 20000 (Telegram hard cap).\n"
        "<b>Apply</b> = turant chal rahe mic par settings lag jayengi."
    )

def mic_kb(mic_on: bool = False, logged_in: bool = False) -> K:
    if not logged_in:
        return K([
            [B(" Login karein", callback_data="menu:login")],
            [B("⬅ Home", callback_data="menu:home")],
        ])
    rows = [
        [B("🎤 Mic ON" if not mic_on else "⏹ Mic OFF",
           callback_data="mic:off" if mic_on else "mic:on")],
        [B("👥 Spare Mic Account (2nd ID)", callback_data="mic:acct")],
    ]
    rows.extend([
        [B("🔊 Mic Gain −2000", callback_data="mic:vol:-2000"),
         B("Mic Gain", callback_data="mic:noop"),
         B("Mic Gain +2000 🔊", callback_data="mic:vol:2000")],
        [B("−5000", callback_data="mic:vol:-5000"),
         B("🔥 MAX BOOST", callback_data="mic:vol:20000"),
         B("+5000", callback_data="mic:vol:5000")],
        [B("📈 Audio Gain −25", callback_data="mic:gain:-25"),
         B("Gain", callback_data="mic:noop"),
         B("Audio Gain +25 📈", callback_data="mic:gain:25")],
        [B("🎵 Bass −5", callback_data="mic:bass:-5"),
         B("Bass", callback_data="mic:noop"),
         B("Bass +5 🎵", callback_data="mic:bass:5")],
        [B("💥 Boost −1", callback_data="mic:boost:-1"),
         B("Boost", callback_data="mic:noop"),
         B("Boost +1 💥", callback_data="mic:boost:1")],
        [B("🔊 Echo −1", callback_data="mic:echolvl:-1"),
         B("Echo On/Off", callback_data="mic:echo:toggle"),
         B("Echo +1 🔊", callback_data="mic:echolvl:1")],
        [B("✅ Apply to Mic", callback_data="mic:apply"),
         B("⚡ MAX ALL", callback_data="mic:max")],
        [B("⬅ Home", callback_data="menu:home")],
    ])
    return K(rows)

def status_text(user_id: int, data: dict, uvc, s: dict, premium: dict = None) -> str:
    logged = "✅ Active" if uvc else ("💾 Saved (idle)" if data and data.get("string_session") else "❌ Not logged in")
    acc = (
        f"├ <b>Connected Acc:</b> {uvc.account_name} "
        f"(@{uvc.account_username or 'none'})\n"
        f"├ <b>Acc ID:</b> <code>{uvc.account_id}</code>\n"
        f"├ <b>Active VCs:</b> {len(uvc.chats)}\n"
        if uvc else ""
    )
    if premium and premium.get("active"):
        prem_line = f"├ <b>Premium:</b> ✅ Active ({premium.get('plan', '—')})\n"
        prem_line += f"├ <b>Until:</b> <code>{premium.get('until', '—')}</code>\n"
    else:
        prem_line = "├ <b>Premium:</b> ❌ Free plan\n"
    return (
        "👤 <b>Your Account</b>\n\n"
        f"{LINE}\n"
        f"├ <b>ID:</b> <code>{user_id}</code>\n"
        f"├ <b>Name:</b> {(data or {}).get('first_name', '—')}\n"
        f"├ <b>Username:</b> @{(data or {}).get('username') or 'none'}\n"
        f"├ <b>Login:</b> {logged}\n"
        f"{acc}{prem_line}"
        f"├ <b>Volume:</b> <code>{s.get('relay_volume', Config.RELAY_DEFAULT_VOLUME)}/1000</code>\n"
        f"├ <b>Gain:</b> {s.get('gain', Config.RELAY_DEFAULT_GAIN)} | <b>Bass:</b> +{s['bass']} | <b>Treble:</b> {s.get('treble', Config.RELAY_DEFAULT_TREBLE)}\n"
        f"├ <b>Voice:</b> {s.get('voice', 'normal')}\n"
        f"├ <b>Boost:</b> {s['boost']}/10 | <b>Echo:</b> "
        f"{'On' if s['echo'] else 'Off'} {s['echo_level']}/10\n"
        f"└ <b>Joined:</b> {(data or {}).get('joined_at', '—')}\n"
        f"{LINE}"
    )
