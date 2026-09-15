
from enum import Enum
from urllib.parse import urlparse

from pyrogram.types import InlineKeyboardButton as _InlineKeyboardButton
from pyrogram.types import InlineKeyboardMarkup as K
from pyrogram.errors import RPCError

from config import Config

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
    return [[B(" Source Code", url=SOURCE_CODE_URL)]] if SOURCE_CODE_URL else []

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
    status = "✅ Logged in" if logged_in else "⚠️ Not logged in"
    return (
        f"👋 <b>Welcome, {name}!</b>\n\n"
        f"<blockquote>🎧 <b>Apex VC Fyt Bot</b>\n"
        f"🔊 High-power voice-chat audio • live mic boost\n"
        f"🎚️ Bass • echo • boost • queue controls</blockquote>\n"
        f"{LINE}\n"
        f"<b>Status:</b> {status}\n"
        f"{LINE}\n\n"
        f"🔐 <b>Login</b> — apna account bot se connect karein (phone + OTP)\n"
        f"🧾 <b>Add String</b> — already string session hai? Yahan paste karein\n"
        f"📘 <b>Tutorial</b> — har feature ka step-by-step guide\n"
        f"🎚️ <b>Audio Settings</b> — volume / bass / echo / boost live control\n"
        f"💳 <b>Premium</b> — unlimited uses, no daily limit\n"
        f"👥 <b>Refer & Earn</b> — dosto ko bulao, free premium pao\n\n"
        f"👥 <b>Multi-user:</b> har user apne account se, ek saath use kar sakta hai."
    )

def home_kb(is_owner: bool = False, logged_in: bool = False,
            active_chat_id: int = None) -> K:
    rows = [
        [
            B(" Login" if not logged_in else " Re-Login", callback_data="menu:login"),
            B(" Add String", callback_data="menu:addstring"),
        ],
        [
            B(" Tutorial", callback_data="menu:tutorial"),
            B(" Audio Settings", callback_data="menu:settings"),
        ],
        [B(" Now Playing", callback_data="vc:list:0")],
        [
            B(" VC Commands", callback_data="tut:play"),
            B(" Audio Library", callback_data="aud:menu"),
        ],
        [B("🎤 Live Mic", callback_data="mic:panel")],
        [
            B(" My Status", callback_data="menu:status"),
            B(" Help / FAQ", callback_data="tut:faq"),
        ],
        [B("💳 Premium", callback_data="pay:menu")],
        [B("👥 Refer & Earn", callback_data="ref:menu")],
        [B("🎟️ Redeem Coupon", callback_data="pay:coupon")],
        [B(f" Session Generator — {GEN_NAME}", url=GEN)],
    ]
    rows.extend(source_button())
    if active_chat_id is not None:
        rows[2] = [B(" Now Playing", callback_data=f"vc:now:{active_chat_id}")]
    if logged_in:
        rows.insert(1 if active_chat_id is None else 2,
                    [B(" Logout", callback_data="menu:logout")])
    if is_owner:
        rows.append([B(" Owner Panel", callback_data="adm_back")])
    return K(rows)

def back_kb(target: str = "menu:home") -> K:
    return K([[B("⬅ Back", callback_data=target), B(" Home", callback_data="menu:home")]])

LOGIN_INTRO = (
    " <b>Login — apna account connect karein</b>\n\n"
    f"{LINE}\n"
    "<b>Do tarike hain:</b>\n"
    f"{LINE}\n\n"
    "<b>1 Phone Login (asaan)</b>\n"
    "• Phone number bhejein (country code ke saath)\n"
    "• Telegram jo OTP bhejega wo bot ko dein\n"
    "• 2-step password ho to wo bhi\n"
    "• Bot khud aapka string session bana lega\n\n"
    "<b>2 String Session (already hai)</b>\n"
    f"• {GEN_NAME} se session generate karein\n"
    "• Yahan paste karein\n\n"
    " Session sirf aapke VC control ke liye use hota hai."
)

def login_kb() -> K:
    rows = [
        [B(" Phone se Login", callback_data="login:phone")],
        [B(" String Session daalein", callback_data="menu:addstring")],
        [B(f" Generator — {GEN_NAME}", url=GEN)],
        [B(" Home", callback_data="menu:home")],
    ]
    rows.extend(source_button())
    return K(rows)

CANCEL_KB = K([[B(" Cancel", callback_data="login:cancel")]])

ADDSTRING_TEXT = (
    " <b>String Session Add</b>\n\n"
    "Bas apna Pyrogram string session <b>seedha yahan bhej dein</b> "
    "(ya <code>/addstring &lt;session&gt;</code>).\n\n"
    f"Session nahi hai? {GEN_NAME} se banayein \n\n"
    " Message bhejte hi bot use delete kar deta hai — safe hai."
)

def addstring_kb() -> K:
    rows = [
        [B(f" Generate — {GEN_NAME}", url=GEN)],
        [B(" Ya phone se login karein", callback_data="login:phone")],
        [B(" Home", callback_data="menu:home")],
    ]
    rows.extend(source_button())
    return K(rows)

def settings_text(s: dict) -> str:
    bars = lambda n: "█" * n + "░" * (10 - n)
    auto_status = "ON  (real max preset)" if s.get('auto') else 'OFF'
    return (
        "🎚️ <b>Audio Settings</b> (aapke account ke liye)\n\n"
        f"{LINE}\n"
        f"⚡ <b>AUTO mode:</b> {auto_status}\n"
        f"🔊 <b>Playback Volume:</b> <code>{s.get('relay_volume', Config.RELAY_DEFAULT_VOLUME)}/1000</code>\n"
        f"📈 <b>Gain:</b> <code>{s.get('gain', Config.RELAY_DEFAULT_GAIN)}/200</code>\n"
        f"🎵 <b>Bass:</b> <code>+{s['bass']} dB</code>  (0 – 100)\n"
        f"🎶 <b>Treble:</b> <code>{s.get('treble', Config.RELAY_DEFAULT_TREBLE)}/100</code>\n"
        f"🗣️ <b>Voice:</b> <code>{s.get('voice', 'normal')}</code>\n"
        f"💥 <b>Boost:</b> <code>{bars(int(s['boost']))} {s['boost']}/10</code>\n"
        f"🔊 <b>Echo:</b> {'ON' if s['echo'] else 'OFF'} "
        f"<code>{bars(int(s['echo_level']))} {s['echo_level']}/10</code>\n"
        f"{LINE}\n\n"
        "Buttons se ghata/badha sakte hain — audio par real FFmpeg controls apply hote hain.\n"
        "<b>OVERDRIVE</b> = maximum loud/distorted preset.\n"
        "<b>Apply Live</b> = turant chal rahe track par lag jayega."
    )

def settings_kb() -> K:
    return K([
        [B("⚡ AUTO ON/OFF (sab automatic)", callback_data="set:auto:toggle")],
        [B("🔊 −25", callback_data="set:relay:-25"),
         B("Volume", callback_data="set:noop"),
         B("+25 🔊", callback_data="set:relay:25")],
        [B("−100", callback_data="set:relay:-100"),
         B("🔄 Reset", callback_data="set:reset"),
         B("+100", callback_data="set:relay:100")],
        [B("−250", callback_data="set:relay:-250"),
         B(" MAX 1000", callback_data="set:relay:1000"),
         B("+250", callback_data="set:relay:250")],
        [B("📈 Gain −25", callback_data="set:gain:-25"),
         B("Gain", callback_data="set:noop"),
         B("Gain +25 📈", callback_data="set:gain:25")],
        [B("Gain −75", callback_data="set:gain:-75"),
         B("🔥 MAX GAIN 200", callback_data="set:gain:200"),
         B("Gain +75", callback_data="set:gain:75")],
        [B("🎵 Bass −5", callback_data="set:bass:-5"),
         B("Bass", callback_data="set:noop"),
         B("Bass +5 🎵", callback_data="set:bass:5")],
        [B("💥 Boost −1", callback_data="set:boost:-1"),
         B("Boost", callback_data="set:noop"),
         B("Boost +1 💥", callback_data="set:boost:1")],
        [B("🔊 Echo −1", callback_data="set:echolvl:-1"),
         B("Echo On/Off", callback_data="set:echo:toggle"),
         B("Echo +1 🔊", callback_data="set:echolvl:1")],
        [B("✅ Apply Live", callback_data="set:apply"),
         B("🔥 OVERDRIVE", callback_data="set:max")],
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

def mic_kb(mic_on: bool = False, logged_in: bool = False,
           relay_url: str = "") -> K:
    if not logged_in:
        return K([
            [B(" Login karein", callback_data="menu:login")],
            [B("⬅ Home", callback_data="menu:home")],
        ])
    rows = [
        [B("🎤 Mic ON" if not mic_on else "⏹ Mic OFF",
           callback_data="mic:off" if mic_on else "mic:on")],
    ]
    if mic_on and relay_url:
        rows.append([B("📱 Open Mic Page (Chrome)", url=relay_url)])
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
