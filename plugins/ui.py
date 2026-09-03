"""
Shared UI — every screen, keyboard and text lives here so the bot has one
consistent, professional look. Everything is button-driven.
"""

from enum import Enum

from pyrogram.types import InlineKeyboardButton as _InlineKeyboardButton
from pyrogram.types import InlineKeyboardMarkup as K
from pyrogram.errors import RPCError

from config import Config

GEN = Config.SESSION_BOT_LINK
GEN_NAME = f"@{Config.SESSION_BOT_USERNAME}"

LINE = "━━━━━━━━━━━━━━━━━━━━"


class ButtonStyle(str, Enum):
    """Bot API semantic styles; Pyrofork falls back when MTProto lacks them."""

    PRIMARY = "primary"
    SUCCESS = "success"
    DANGER = "danger"


_STYLE_FALLBACK_LABEL = {
    "primary": "[PRIMARY]",
    "success": "[SUCCESS]",
    "danger": "[DANGER]",
}


async def edit_screen(message, text: str, reply_markup=None, **kwargs):
    """Edit a text screen, falling back to a caption for /start photos."""
    try:
        return await message.edit_text(text, reply_markup=reply_markup, **kwargs)
    except (RPCError, TypeError, AttributeError):
        if not any(getattr(message, kind, None) for kind in
                   ("photo", "video", "animation", "document", "audio")):
            raise
        return await message.edit_caption(text, reply_markup=reply_markup)


async def safe_answer(cq, text: str = "", **kwargs):
    """Answer a callback without crashing when Telegram already expired it."""
    try:
        return await cq.answer(text, **kwargs)
    except RPCError as exc:
        if type(exc).__name__ != "QueryIdInvalid":
            raise
        return None


# Bot API style is not available in every Pyrogram/Pyrofork build. Keep the
# semantic style at this single boundary and fall back to a plain button so
# unsupported versions never break keyboard construction or callback routing.
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
    """Build a semantic button, compatible with old and new client builds."""
    params = dict(kwargs)
    if callback_data is not None:
        params["callback_data"] = callback_data

    if style is None:
        if _is_danger(text, callback_data):
            style = "danger"
        elif _is_success(text, callback_data):
            style = "success"

    # Custom button emoji IDs belong to the Bot API representation and are not
    # supported by Pyrofork's MTProto keyboard type. Deliberately discard the
    # numeric ID instead of leaking it into the constructor and crashing.
    del icon_custom_emoji_id
    if isinstance(style, ButtonStyle):
        style = style.value
    if style:
        try:
            return _InlineKeyboardButton(text, style=style, **params)
        except (TypeError, ValueError):
            # Older Pyrogram/Pyrofork does not expose Bot API button styles.
            # Preserve the semantic meaning visibly instead of silently
            # returning an indistinguishable normal button.
            text = f"{_STYLE_FALLBACK_LABEL[style]} {text}"
    return _InlineKeyboardButton(text, **params)


# ── Home ─────────────────────────────────────────────────────────────────────
def home_text(name: str, logged_in: bool) -> str:
    status = " Logged in" if logged_in else " Not logged in"
    return (
        f" <b>Welcome, {name}!</b>\n\n"
        f" <b>VC Audio Studio Bot</b> — voice chat mein high-power audio,\n"
        f"live mic boost, bass, echo aur queue. Sab kuch buttons se.\n\n"
        f"{LINE}\n"
        f"<b>Status:</b> {status}\n"
        f"{LINE}\n\n"
        f" <b>Login</b> — apna account bot se connect karein (phone + OTP)\n"
        f" <b>Add String</b> — already string session hai? Yahan paste karein\n"
        f" <b>Tutorial</b> — har feature ka step-by-step guide\n"
        f" <b>Audio Settings</b> — volume / bass / echo / boost live control\n\n"
        f" Multi-user: har user apne account se, ek saath use kar sakta hai."
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
        [
            B(" VC Commands", callback_data="tut:play"),
            B(" Audio Library", callback_data="aud:menu"),
        ],
        [
            B(" My Status", callback_data="menu:status"),
            B(" Help / FAQ", callback_data="tut:faq"),
        ],
        [B(f" Session Generator — {GEN_NAME}", url=GEN)],
    ]
    if active_chat_id is not None:
        rows.insert(1, [B(" Now Playing", callback_data=f"vc:now:{active_chat_id}")])
    if logged_in:
        rows.insert(1 if active_chat_id is None else 2,
                    [B(" Logout", callback_data="menu:logout")])
    if is_owner:
        rows.append([B(" Owner Panel", callback_data="adm_back")])
    return K(rows)


def back_kb(target: str = "menu:home") -> K:
    return K([[B("⬅ Back", callback_data=target), B(" Home", callback_data="menu:home")]])


# ── Login screens ────────────────────────────────────────────────────────────
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
    return K([
        [B(" Phone se Login", callback_data="login:phone")],
        [B(" String Session daalein", callback_data="menu:addstring")],
        [B(f" Generator — {GEN_NAME}", url=GEN)],
        [B(" Home", callback_data="menu:home")],
    ])


CANCEL_KB = K([[B(" Cancel", callback_data="login:cancel")]])


ADDSTRING_TEXT = (
    " <b>String Session Add</b>\n\n"
    "Bas apna Pyrogram string session <b>seedha yahan bhej dein</b> "
    "(ya <code>/addstring &lt;session&gt;</code>).\n\n"
    f"Session nahi hai? {GEN_NAME} se banayein \n\n"
    " Message bhejte hi bot use delete kar deta hai — safe hai."
)


def addstring_kb() -> K:
    return K([
        [B(f" Generate — {GEN_NAME}", url=GEN)],
        [B(" Ya phone se login karein", callback_data="login:phone")],
        [B(" Home", callback_data="menu:home")],
    ])


# ── Audio settings panel ─────────────────────────────────────────────────────
def settings_text(s: dict) -> str:
    bars = lambda n: "█" * n + "░" * (10 - n)          # noqa: E731
    return (
        " <b>Audio Settings</b> (aapke account ke liye)\n\n"
        f"{LINE}\n"
        f" <b>AUTO mode:</b> {'ON  (real max preset)' if s.get('auto') else 'OFF'}\n"
        f" <b>Playback Volume:</b> <code>{s.get('relay_volume', Config.RELAY_DEFAULT_VOLUME)}/1000</code>\n"
        f" <b>Gain:</b> <code>{s.get('gain', Config.RELAY_DEFAULT_GAIN)}/150</code>\n"
        f" <b>Bass:</b> <code>+{s['bass']} dB</code>  (0 – 100)\n"
        f" <b>Treble:</b> <code>{s.get('treble', Config.RELAY_DEFAULT_TREBLE)}/100</code>\n"
        f" <b>Voice:</b> <code>{s.get('voice', 'normal')}</code>\n"
        f" <b>Boost:</b> <code>{bars(int(s['boost']))} {s['boost']}/10</code>\n"
        f" <b>Echo:</b> {'ON' if s['echo'] else 'OFF'} "
        f"<code>{bars(int(s['echo_level']))} {s['echo_level']}/10</code>\n"
        f"{LINE}\n\n"
        "Buttons se ghata/badha sakte hain — audio par real FFmpeg controls apply hote hain. "
        "<b>Apply Live</b> se turant lag jayega."
    )


def settings_kb() -> K:
    return K([
        [B(" AUTO ON/OFF (sab automatic)", callback_data="set:auto:toggle")],
        [B(" −25", callback_data="set:relay:-25"),
         B("Volume", callback_data="set:noop"),
         B("+25 ", callback_data="set:relay:25")],
        [B("−100", callback_data="set:relay:-100"),
         B("Reset", callback_data="set:reset"),
         B("+100", callback_data="set:relay:100")],
        [B("−250", callback_data="set:relay:-250"),
         B(" MAX 1000", callback_data="set:relay:1000"),
         B("+250", callback_data="set:relay:250")],
        [B(" Bass −5", callback_data="set:bass:-5"),
         B(" Bass", callback_data="set:noop"),
         B("Bass +5 ", callback_data="set:bass:5")],
        [B(" Boost −1", callback_data="set:boost:-1"),
         B(" Boost", callback_data="set:noop"),
         B("Boost +1 ", callback_data="set:boost:1")],
        [B(" Echo −1", callback_data="set:echolvl:-1"),
         B(" Echo On/Off", callback_data="set:echo:toggle"),
         B("Echo +1 ", callback_data="set:echolvl:1")],
        [B(" Apply Live", callback_data="set:apply"),
         B(" MAX", callback_data="set:max")],
        [B(" Home", callback_data="menu:home")],
    ])


# ── Status ───────────────────────────────────────────────────────────────────
def status_text(user_id: int, data: dict, uvc, s: dict) -> str:
    logged = " Active" if uvc else (" Saved (idle)" if data and data.get("string_session") else " Not logged in")
    acc = (
        f"├ <b>Connected Acc:</b> {uvc.account_name} "
        f"(@{uvc.account_username or 'none'})\n"
        f"├ <b>Acc ID:</b> <code>{uvc.account_id}</code>\n"
        f"├ <b>Active VCs:</b> {len(uvc.chats)}\n"
        if uvc else ""
    )
    return (
        " <b>Your Account</b>\n\n"
        f"├ <b>ID:</b> <code>{user_id}</code>\n"
        f"├ <b>Name:</b> {(data or {}).get('first_name', '—')}\n"
        f"├ <b>Username:</b> @{(data or {}).get('username') or 'none'}\n"
        f"├ <b>Login:</b> {logged}\n"
        f"{acc}"
        f"├ <b>Volume:</b> <code>{s.get('relay_volume', Config.RELAY_DEFAULT_VOLUME)}/1000</code>\n"
        f"├ <b>Gain:</b> {s.get('gain', Config.RELAY_DEFAULT_GAIN)} | <b>Bass:</b> +{s['bass']} | <b>Treble:</b> {s.get('treble', Config.RELAY_DEFAULT_TREBLE)}\n"
        f"├ <b>Voice:</b> {s.get('voice', 'normal')}\n"
        f"├ <b>Boost:</b> {s['boost']}/10 | <b>Echo:</b> "
        f"{'On' if s['echo'] else 'Off'} {s['echo_level']}/10\n"
        f"└ <b>Joined:</b> {(data or {}).get('joined_at', '—')}"
    )
