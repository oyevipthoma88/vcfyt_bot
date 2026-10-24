
from enum import Enum
from urllib.parse import urlparse

from pyrogram import filters as _filters
from pyrogram.types import InlineKeyboardButton as _InlineKeyboardButton
from pyrogram.types import InlineKeyboardMarkup as K
from pyrogram.errors import RPCError, MessageNotModified

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
    # "." har jagah, "/" sirf bot DM mein, "!" band.
    # (old note) "." "/" and "!" all work in groups AND DM.  "/boost@MyBot 5" style
    # (Telegram's command menu in groups) is accepted too.  The old check
    # compared m.chat.type to the string "private", but pyrofork uses the
    # ChatType enum, so every "/" command silently failed.
    dot_re = _re.compile(r"^\." + pattern, flags)
    slash_re = _re.compile(r"^/" + pattern, flags)

    def _f(_, __, m):
        text = (getattr(m, "text", None) or getattr(m, "caption", None) or "").lstrip()
        if dot_re.match(text):
            return True  # "." har jagah
        return bool(slash_re.match(text)) and _is_private(m)  # "/" sirf bot DM

    return _filters.create(_f)


def _is_private(m) -> bool:
    chat = getattr(m, "chat", None)
    t = getattr(chat, "type", None)
    return str(getattr(t, "value", t)).lower() in ("private", "chattype.private")


def dm_cmd(commands):
    """"." command har jagah, "/" command sirf bot ke DM mein."""
    return (_filters.command(commands, prefixes=".")
            | (_filters.private & _filters.command(commands, prefixes="/")))

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
    PRIMARY = "primary"
    SUCCESS = "success"
    DANGER = "danger"

_STYLE_EMOJI = {
    "primary": "🔹",
    "success": "✅",
    "danger": "❌",
}

async def edit_screen(message, text: str, reply_markup=None, **kwargs):
    try:
        return await message.edit_text(text, reply_markup=reply_markup, **kwargs)
    except MessageNotModified:
        # Same screen (e.g. slider already at its limit) -> not an error.
        return message
    except (RPCError, TypeError, AttributeError):
        if not any(getattr(message, kind, None) for kind in
                   ("photo", "video", "animation", "document", "audio")):
            raise
        try:
            return await message.edit_caption(text, reply_markup=reply_markup)
        except MessageNotModified:
            return message
        except RPCError:
            # Send the new screen FIRST, delete the old one only on success
            # (old bug: message deleted, then send failed -> blank chat).
            sent = await message._client.send_message(
                message.chat.id, text, reply_markup=reply_markup, **kwargs)
            try:
                await message.delete()
            except Exception:
                pass
            return sent

async def safe_answer(cq, text: str = "", **kwargs):
    # Telegram rejects callback answers over 200 chars -> button looked dead.
    text = str(text or "")
    if len(text) > 200:
        text = text[:199] + "…"
    try:
        return await cq.answer(text, **kwargs)
    except RPCError as exc:
        if type(exc).__name__ != "QueryIdInvalid":
            raise
        return None

_DANGER_WORDS = ("logout", "cancel", "stop", "reset", "delete", "untag", "ban", "restart", "off")
_SUCCESS_WORDS = ("login", "addstring", "apply", "resume", "save", "send", "start", "on", "max")

def _words(text: str, callback_data: str = None) -> set:
    # Whole-word match only: the old substring check made "Controls",
    # "Mention", "Done" ... look like "on" and flipped colours randomly.
    import re as _re
    return set(_re.findall(r"[a-z]+", f"{callback_data or ''} {text}".lower()))

def _is_danger(text: str, callback_data: str = None) -> bool:
    return bool(_words(text, callback_data) & set(_DANGER_WORDS))

def _is_success(text: str, callback_data: str = None) -> bool:
    return bool(_words(text, callback_data) & set(_SUCCESS_WORDS))

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

    if isinstance(style, ButtonStyle):
        style = style.value
    if icon_custom_emoji_id is None:
        from helpers.custom_emoji import button_icon
        icon_custom_emoji_id = button_icon(text)
    if style:
        try:
            display_text = text
            if text[:1].isascii() and not text.startswith(tuple(_STYLE_EMOJI.values())):
                display_text = f"{_STYLE_EMOJI.get(style, '')} {text}".strip()
            button = _InlineKeyboardButton(display_text, style=style, **params)
            if not icon_custom_emoji_id:
                try:
                    button.text = display_text
                except (AttributeError, TypeError):
                    pass
            if icon_custom_emoji_id:
                button._bot_api_icon = icon_custom_emoji_id
                # Premium icon buttons are sent through the Bot API, which
                # only knows the colour from _bot_api_style.  Without this
                # every button with a premium emoji lost its colour.
                button._bot_api_style = style
            return button
        except (TypeError, ValueError):

            pass
    emoji = _STYLE_EMOJI.get(style, "")
    # Text pehle se emoji se shuru ho (ya premium icon ho) to prefix mat
    # chipkao — warna "✅ 🎤" double emoji aata tha aur premium icon toot-ta tha.
    if text[:1] and not text[:1].isascii():
        emoji = ""
    if emoji and not text.startswith(tuple(_STYLE_EMOJI.values())):
        text = f"{emoji} {text}"
    button = _InlineKeyboardButton(text, **params)
    if style:

        button._bot_api_style = style
    if icon_custom_emoji_id:
        button._bot_api_icon = icon_custom_emoji_id
    return button

def _g(s: dict, key: str, default=0):
    """Settings may be missing keys (old DB rows) -> never KeyError."""
    try:
        return (s or {}).get(key, default)
    except AttributeError:
        return default


def _bar(value, maximum, width: int = 10) -> str:
    try:
        n = max(0, min(width, round(float(value) / float(maximum) * width)))
    except (TypeError, ValueError, ZeroDivisionError):
        n = 0
    return "▰" * n + "▱" * (width - n)


def home_text(name: str, logged_in: bool) -> str:
    if logged_in:
        status = "🟢 <b>Ready</b> — account connected"
        steps = (
            "<b>⚡ 3 steps me fight shuru</b>\n"
            "<b>1.</b> Group me <code>.setgc</code> likho <i>(ek baar)</i>\n"
            "<b>2.</b> Voice chat start karo\n"
            "<b>3.</b> 🎤 <b>Live Mic</b> ya audio reply karke <code>.play</code>"
        )
    else:
        status = "🔴 <b>Not connected</b>"
        steps = (
            "<b>⚡ 2 minute setup</b>\n"
            "<b>1.</b> 🔐 <b>Start Setup</b> dabao\n"
            "<b>2.</b> Phone number bhejo <i>(+91…)</i>\n"
            "<b>3.</b> Telegram ka OTP bhejo — done ✅"
        )
    return (
        "🎧 <b>APEX VC FIGHT BOT</b>\n"
        f"<blockquote>👋 Hey <b>{name}</b>!\n"
        "🔊 Loud & clear VC audio • 🎤 Live mic</blockquote>\n"
        f"{status}\n\n"
        f"{steps}\n\n"
        "<i>❓ Kuch samajh na aaye to 📘 Help dabao.</i>"
    )

def home_kb(is_owner: bool = False, logged_in: bool = False,
            active_chat_id: int = None) -> K:
    if not logged_in:
        rows = [
            [B("🔐 Start Setup", callback_data="menu:login", style="success")],
            [B("🎬 Video Guide", callback_data="tut:video", style="primary"),
             B("📘 Help", callback_data="menu:tutorial", style="primary")],
        ]
    else:
        now = (f"vc:now:{active_chat_id}" if active_chat_id is not None else "vc:list:0")
        rows = [
            [B("🎤 Live Mic", callback_data="mic:panel", style="success"),
             B("▶️ Player", callback_data=now, style="primary")],
            [B("🎚️ Sound", callback_data="menu:settings", style="primary"),
             B("🔗 Sync Play", callback_data="sync:menu", style="primary")],
            [B("☰ More", callback_data="menu:more", style="primary"),
             B("📘 Help", callback_data="menu:tutorial", style="primary")],
        ]
    if is_owner:
        rows.append([B("👑 Owner Panel", callback_data="adm_back", style="success")])
    rows.extend(source_button())
    return K(rows)

def more_kb() -> K:
    return K([
        [B("🎵 Library", callback_data="aud:menu", style="primary"),
         B("👤 My Account", callback_data="menu:status", style="primary")],
        [B("🎬 Video Guide", callback_data="tut:video", style="primary"),
         B("📘 Help", callback_data="menu:tutorial", style="primary")],
        [B("💎 Premium", callback_data="pay:menu", style="primary"),
         B("👥 Refer & Earn", callback_data="ref:menu", style="primary")],
        [B("🔁 Re-Login", callback_data="menu:login", style="primary"),
         B("🚪 Logout", callback_data="menu:logout", style="danger")],
        [B("🏠 Home", callback_data="menu:home", style="primary")],
    ])

def back_kb(target: str = "menu:home") -> K:
    return K([[B("⬅ Back", callback_data=target, style="primary"),
               B("🏠 Home", callback_data="menu:home", style="primary")]])

LOGIN_INTRO = (
    "🔐 <b>Account connect karein</b>\n\n"
    "<b>1.</b> Phone number bhejo <i>(country code ke saath, jaise +91…)</i>\n"
    "<b>2.</b> Telegram ka OTP bot ko bhejo\n"
    "<b>3.</b> 2-step password ho to wo bhi\n\n"
    "<blockquote>🛡️ Session sirf aapke VC control ke liye use hota hai.</blockquote>"
)

def login_kb() -> K:
    rows = [
        [B("📱 Phone se Login", callback_data="login:phone", style="success")],
        [B("👥 Multiple ID Login", callback_data="mic:acct")],
        [B("🏠 Home", callback_data="menu:home", style="primary")],
    ]
    rows.extend(source_button())
    return K(rows)

CANCEL_KB = K([[B("✖️ Cancel", callback_data="login:cancel", style="danger")]])

def settings_text(s: dict) -> str:
    vol = int(_g(s, "relay_volume", Config.RELAY_DEFAULT_VOLUME) or 0)
    gain = int(_g(s, "gain", Config.RELAY_DEFAULT_GAIN) or 0)
    bass = int(_g(s, "bass", Config.DEFAULT_BASS) or 0)
    boost = int(_g(s, "boost", Config.DEFAULT_BOOST) or 0)
    echo = "🟢 ON" if _g(s, "echo", 0) else "⚪ OFF"
    return (
        "🎚️ <b>Sound Controls</b>\n\n"
        f"🔊 Volume  <code>{_bar(vol, 3000)}</code> <b>{vol}</b>\n"
        f"📈 Gain    <code>{_bar(gain, 500)}</code> <b>{gain}</b>\n"
        f"🎵 Bass    <code>{_bar(bass, 100)}</code> <b>{bass}</b>\n"
        f"💥 Boost   <code>{_bar(boost, 10)}</code> <b>{boost}</b>\n"
        f"🔁 Echo    {echo}\n\n"
        "<blockquote>💡 Sabse tez + saaf: <b>🔥 MAX LOUD</b> dabao.\n"
        "Badlav chal rahe audio pe khud lag jaata hai.</blockquote>"
    )

def settings_kb() -> K:
    return K([
        [B("🔥 MAX LOUD (1 tap)", callback_data="set:max", style="success")],
        [B("🔉 Vol −", callback_data="set:relay:-250", style="primary"),
         B("🔊 Vol +", callback_data="set:relay:250", style="primary")],
        [B("📉 Gain −", callback_data="set:gain:-50", style="primary"),
         B("📈 Gain +", callback_data="set:gain:50", style="primary")],
        [B("🎵 Bass −", callback_data="set:bass:-5", style="primary"),
         B("🎵 Bass +", callback_data="set:bass:5", style="primary")],
        [B("💥 Boost −", callback_data="set:boost:-1", style="primary"),
         B("💥 Boost +", callback_data="set:boost:1", style="primary")],
        [B("🔁 Echo", callback_data="set:echo:toggle", style="primary"),
         B("♻️ Reset", callback_data="set:reset", style="danger")],
        [B("🎤 Live Mic", callback_data="mic:panel", style="primary"),
         B("🏠 Home", callback_data="menu:home", style="primary")],
    ])

def mic_text(s: dict, mic_on: bool = False, mic_title: str = "",
             live_vol: int = None) -> str:
    lv = live_vol if live_vol is not None else _g(s, "live_volume", Config.LIVE_BOOST_DEFAULT)
    try:
        lv = int(lv)
    except (TypeError, ValueError):
        lv = 0
    pct = round(lv / 100)
    status_line = "🔴 <b>LIVE</b> — aapki awaaz VC me ja rahi hai" if mic_on else "⚪ <b>Mic OFF</b>"
    device_line = f"📱 Input: <b>{mic_title}</b>\n" if mic_title else ""
    boost = int(_g(s, "boost", Config.DEFAULT_BOOST) or 0)
    return (
        "🎤 <b>Live Mic</b>\n\n"
        f"{status_line}\n"
        f"{device_line}\n"
        f"🔊 Mic volume <code>{_bar(lv, 20000)}</code> <b>{pct}%</b>\n"
        f"💥 Boost      <code>{_bar(boost, 10)}</code> <b>{boost}</b>\n"
        f"🔁 Echo       {'🟢 ON' if _g(s, 'echo', 0) else '⚪ OFF'}\n\n"
        "<blockquote><b>1.</b> 🎤 <b>Mic ON</b> dabao\n"
        "<b>2.</b> Aur tez chahiye? <b>⚡ MAX ALL</b>\n"
        "<b>3.</b> Kuch badla? <b>✅ Apply</b></blockquote>"
    )

def mic_kb(mic_on: bool = False, logged_in: bool = False) -> K:
    if not logged_in:
        return K([
            [B("🔐 Login karein", callback_data="menu:login", style="success")],
            [B("🏠 Home", callback_data="menu:home", style="primary")],
        ])
    return K([
        [B("⏹ Mic OFF" if mic_on else "🎤 Mic ON",
           callback_data="mic:off" if mic_on else "mic:on",
           style="danger" if mic_on else "success")],
        [B("⚡ MAX ALL (sabse loud)", callback_data="mic:max", style="success")],
        [B("🔉 Mic −", callback_data="mic:vol:-2000", style="primary"),
         B("🔥 200%", callback_data="mic:vol:20000", style="success"),
         B("🔊 Mic +", callback_data="mic:vol:2000", style="primary")],
        [B("💥 Boost −", callback_data="mic:boost:-1", style="primary"),
         B("💥 Boost +", callback_data="mic:boost:1", style="primary")],
        [B("🎵 Bass −", callback_data="mic:bass:-5", style="primary"),
         B("🎵 Bass +", callback_data="mic:bass:5", style="primary")],
        [B("🔁 Echo", callback_data="mic:echo:toggle", style="primary"),
         B("✅ Apply", callback_data="mic:apply", style="success")],
        [B("👥 Spare ID", callback_data="mic:acct", style="primary"),
         B("💬 Spare Chat", callback_data="mic:chat", style="primary")],
        [B("🏠 Home", callback_data="menu:home", style="primary")],
    ])

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
        f"├ <b>Volume:</b> <code>{_g(s, 'relay_volume', Config.RELAY_DEFAULT_VOLUME)}/3000</code>\n"
        f"├ <b>Gain:</b> {_g(s, 'gain', Config.RELAY_DEFAULT_GAIN)} | <b>Bass:</b> +{_g(s, 'bass')} | <b>Treble:</b> {_g(s, 'treble', Config.RELAY_DEFAULT_TREBLE)}\n"
        f"├ <b>Voice:</b> {_g(s, 'voice', 'normal')}\n"
        f"├ <b>Boost:</b> {_g(s, 'boost')}/10 | <b>Echo:</b> "
        f"{'On' if _g(s, 'echo') else 'Off'} {_g(s, 'echo_level')}/10\n"
        f"└ <b>Joined:</b> {(data or {}).get('joined_at', '—')}\n"
        f"{LINE}"
    )
