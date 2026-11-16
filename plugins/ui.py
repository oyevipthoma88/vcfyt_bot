import html as _html

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

LINE = "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄"

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


# ─── Design system ─────────────────────────────────────────────
# Every screen = card(): bold header, thin rule, body, optional tip.
RULE = "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄"

def card(icon: str, title: str, body: str, tip: str = "") -> str:
    out = f"{icon} <b>{title.upper()}</b>\n<code>{RULE}</code>\n{body.rstrip()}"
    if tip:
        out += f"\n\n<blockquote expandable>💡 {tip}</blockquote>"
    return out

def meter(label: str, value, maximum, suffix: str = "") -> str:
    return f"<code>{label:<7}</code> {_bar(value, maximum)} <b>{value}{suffix}</b>"

def chip(on: bool) -> str:
    return "🟢 <b>ON</b>" if on else "⚫ <b>OFF</b>"


def _live_mic_on() -> bool:
    try:
        from helpers.feature_flags import live_mic_on
        return live_mic_on()
    except Exception:
        return True

def home_text(name: str, logged_in: bool) -> str:
    """Short, task-first home card with the next useful action up front."""
    mic = _live_mic_on()
    greeting = f"👋 Hey <b>{_html.escape(str(name))}</b>"
    if logged_in:
        body = (
            f"{greeting}\n"
            "🟢 <b>Login active</b> — sab features khule hain\n\n"
            "<b>⚡ Quick start</b>\n"
            "① Group me voice chat start karo\n"
            + ("② <b>🎤 Live Mic</b> → group set karo → <b>MIC ON</b>\n"
               "③ Link <b>Chrome</b> me kholo aur bolo\n"
               if mic else
               "② Audio par reply karke <code>.play</code> bhejo\n"
               "③ Niche Player se control karo\n")
            + "\n▶️ Audio ke liye: audio par reply karke <code>.play</code>"
        )
        tip = ("⚔️ Fight ID (optional): Live Mic ek doosri ID se bolegi, aapka phone VC me "
               "bana rahega. Live Mic panel me milega.")
    else:
        body = (
            f"{greeting}\n"
            "🔊 Ultra-loud + clear VC bot\n\n"
            "<b>Shuru karo — 1 tap:</b>\n"
            "🔐 <b>Login</b> — apna number + OTP, bas 1 minute\n"
            "    ▶️ Player · 🎤 Live Mic · 🔗 Sync · 🎵 Library — sab unlimited\n\n"
            "⚔️ <b>Sirf Live Mic?</b> Koi doosri ID ko Fight ID bana lo"
        )
        tip = ("Login sirf aapke VC control ke liye hai. Kabhi bhi 🚪 Logout kar sakte ho.")
    return card("🎧", "Apex · VC Fight", body, tip)

def home_kb(is_owner: bool = False, logged_in: bool = False,
            active_chat_id: int = None) -> K:
    mic = _live_mic_on()
    if not logged_in:
        rows = [
            [B("🔐 Login  ·  number + OTP", callback_data="menu:login", style="success")],
            [B("⚔️ Fight ID (sirf Live Mic)", callback_data="mic:acct", style="primary"),
             B("📘 Help", callback_data="menu:tutorial", style="primary")],
        ]
    else:
        now = (f"vc:now:{active_chat_id}" if active_chat_id is not None else "vc:list:0")
        player_label = "▶️ Now Playing" if active_chat_id is not None else "▶️ Player"
        if mic or is_owner:
            mic_label = "🎤 Live Mic" if mic else "🎤 Live Mic (users OFF)"
            top = [B(mic_label, callback_data="mic:panel", style="success"),
                   B(player_label, callback_data=now, style="success")]
        else:
            top = [B(player_label, callback_data=now, style="success")]
        rows = [
            top,
            [B("🎚️ Sound", callback_data="menu:settings", style="primary"),
             B("🔗 Sync Play", callback_data="sync:menu", style="primary"),
             B("🎵 Library", callback_data="aud:menu", style="primary")],
            [B("💎 Premium", callback_data="pay:menu", style="success"),
             B("🎟️ Redeem", callback_data="pay:coupon", style="success")],
            [B("👤 Account", callback_data="menu:status", style="primary"),
             B("☰ More", callback_data="menu:more", style="primary"),
             B("📘 Help", callback_data="menu:tutorial", style="primary")],
        ]
    if is_owner:
        rows.append([B("👑 Owner Panel", callback_data="adm_back", style="success")])
    rows.extend(source_button())
    return K(rows)

MORE_TEXT = card("☰", "More options",
                 "🔐 <b>Login</b> — aapki apni ID (sab features)\n"
                 "⚔️ <b>Fight ID</b> — Live Mic ke liye doosri ID (optional)\n"
                 "🎵 <b>Library</b> — saved audios\n"
                 "💎 <b>Premium</b> · 👥 <b>Refer & Earn</b>")

def more_kb() -> K:
    return K([
        [B("🎵 Library", callback_data="aud:menu", style="primary"),
         B("👤 My Account", callback_data="menu:status", style="primary")],
        [B("🎬 Video Guide", callback_data="tut:video", style="primary"),
         B("📘 Help", callback_data="menu:tutorial", style="primary")],
        [B("💎 Premium", callback_data="pay:menu", style="success"),
         B("🎟️ Redeem", callback_data="pay:coupon", style="success")],
        [B("👥 Refer & Earn", callback_data="ref:menu", style="success")],
        [B("⚔️ Fight ID", callback_data="mic:acct", style="primary"),
         B("🔐 Login", callback_data="menu:mainid", style="primary")],
        [B("🚪 Logout", callback_data="menu:logout", style="danger")],
        [B("🏠 Home", callback_data="menu:home", style="primary")],
    ])

def back_kb(target: str = "menu:home") -> K:
    return K([[B("⬅ Back", callback_data=target, style="primary"),
               B("🏠 Home", callback_data="menu:home", style="primary")]])

# 🔐 Login = user ki APNI Telegram ID — ek hi login, sab features.
LOGIN_INTRO = card("🔐", "Login",
    "Apni Telegram ID se login karo — <b>sab features</b> khul jayenge:\n"
    "▶️ Player · 🎤 Live Mic · 🔗 Sync Play · 🎵 Library\n\n"
    "<b>①</b> Number bhejo <i>(+91…)</i>  <b>②</b> OTP  <b>③</b> 2-step password (ho to)",
    "Session sirf aapke VC control ke liye use hota hai. Kabhi bhi 🚪 Logout kar sakte ho.")
MAIN_ID_INTRO = LOGIN_INTRO

def login_kb() -> K:
    """Login screen: ek hi login button."""
    return K([
        [B("📱 Login (number + OTP)", callback_data="login:phone", style="success")],
        [B("⚔️ Sirf Live Mic (Fight ID)", callback_data="mic:acct", style="primary"),
         B("🏠 Home", callback_data="menu:home", style="primary")],
    ])

CANCEL_KB = K([[B("✖️ Cancel", callback_data="login:cancel", style="danger")]])

def _loud_mode() -> str:
    try:
        from helpers import playback_v77 as _v77
        return _v77.get_mode()
    except Exception:
        return "loudest"

LOUD_MODE_INFO = {
    "loudest": ("📢", "Loudest", "sabse tez — phone speaker pe fight ke liye"),
    "phone": ("📱", "Phone", "tez + zyada saaf shabd"),
    "bass": ("🎧", "Bass", "heavy bass — headphones ke liye"),
}

def settings_text(s: dict) -> str:
    mode = _loud_mode()
    ico, name, desc = LOUD_MODE_INFO.get(mode, LOUD_MODE_INFO["loudest"])
    body = (
        f"{ico} <code>{'Mode':<7}</code> <b>{name}</b> · {desc}\n"
        f"🔁 <code>{'Echo':<7}</code> {chip(_g(s, 'echo', 0))}"
    )
    tip = (
        "⚡ <b>TEZ:</b> 1 tap — Loudest mode + saari settings max + sab IDs ko 200%.\n"
        "📢 <b>Loudest</b> fight ke liye · 📱 <b>Phone</b> saaf shabd · "
        "🎧 <b>Bass</b> headphones.\n"
        "💡 Badlav chalu audio par turant lagta hai."
    )
    return card("🎚️", "Audio controls", body, tip)

def settings_kb(s: dict = None) -> K:
    mode = _loud_mode()

    def mbtn(m):
        ico, name, _ = LOUD_MODE_INFO[m]
        on = m == mode
        return B(f"{'✅ ' if on else ico + ' '}{name}", callback_data=f"set:lmode:{m}",
                 style="success" if on else "primary")

    return K([
        [B("⚡ TEZ — sab MAX (1 tap)", callback_data="set:tez", style="success")],
        [mbtn("loudest"), mbtn("phone"), mbtn("bass")],
        [B(f"🔁 Echo: {'ON' if _g(s or {}, 'echo', 0) else 'OFF'}",
           callback_data="set:echo:toggle", style="primary")],
        ([B("🎤 Live Mic", callback_data="mic:panel", style="primary"),
          B("🏠 Home", callback_data="menu:home", style="primary")]
         if _live_mic_on() else
         [B("🏠 Home", callback_data="menu:home", style="primary")]),
    ])

def mic_text(s: dict, mic_on: bool = False, mic_title: str = "",
             live_vol: int = None) -> str:
    lv = live_vol if live_vol is not None else _g(s, "live_volume", Config.LIVE_BOOST_DEFAULT)
    try:
        lv = int(lv)
    except (TypeError, ValueError):
        lv = 0
    pct = round(lv / 100)
    status_line = ("🔴 <b>LIVE</b> · aapki awaaz VC me ja rahi hai" if mic_on
                   else "⚫ <b>Mic OFF</b> · start karne ke liye 🎤 dabao")
    device_line = f"📱 Input: <b>{mic_title}</b>\n" if mic_title else ""
    boost = int(_g(s, "boost", Config.DEFAULT_BOOST) or 0)
    master_db = float(_g(s, "master_drive_db", 12.0) or 12.0)
    return card("🎤", "Live mic",
        f"{status_line}\n{device_line}\n"
        f"🔊 {meter('Mic', pct, 200, '%')}\n"
        f"🎙️ <code>{'Master dB':<9}</code> <b>{master_db:+.1f} dB</b> (Mic/Bridge power)\n"
        f"💥 {meter('Boost', boost, 10)}\n"
        f"🔁 <code>{'Echo':<7}</code> {chip(_g(s, 'echo', 0))}",
        "💡 <i>Tip: Agar live aawaz dabi lage to Master dB badhao!</i>\n"
        "<b>①</b> 🎤 Mic ON  <b>②</b> ⚡ MAX ALL  <b>③</b> kuch badla? ✅ Apply")

def mic_kb(mic_on: bool = False, logged_in: bool = False, ss: bool = False) -> K:
    if not logged_in:
        return K([
            [B("🔐 Login", callback_data="menu:login", style="success"),
             B("⚔️ Fight ID", callback_data="mic:acct", style="primary")],
            [B("🏠 Home", callback_data="menu:home", style="primary")],
        ])
    return K([
        [B("⏹ Mic OFF" if mic_on else "🎤 Mic ON",
           callback_data="mic:off" if mic_on else "mic:on",
           style="danger" if mic_on else "success")],
        [B(f"🖥 Live Screen: {'ON' if ss else 'OFF'}", callback_data="mic:ss",
           style="success" if ss else "danger")],
        [B("⚡ MAX ALL (sabse loud)", callback_data="mic:max", style="success")],
        [B("🎙️ Master dB −", callback_data="set:masterdb:-2", style="primary"),
         B("🎙️ Master dB +", callback_data="set:masterdb:2", style="primary")],
        [B("🔉 Mic −", callback_data="mic:vol:-2000", style="primary"),
         B("🔥 200%", callback_data="mic:vol:20000", style="success"),
         B("🔊 Mic +", callback_data="mic:vol:2000", style="primary")],
        [B("💥 Boost −", callback_data="mic:boost:-1", style="primary"),
         B("💥 Boost +", callback_data="mic:boost:1", style="primary")],
        [B("🎵 Bass −", callback_data="mic:bass:-5", style="primary"),
         B("🎵 Bass +", callback_data="mic:bass:5", style="primary")],
        [B("🔁 Echo", callback_data="mic:echo:toggle", style="primary"),
         B("✅ Apply", callback_data="mic:apply", style="success")],
        [B("⚔️ Fight ID", callback_data="mic:acct", style="primary"),
         B("💬 Fight Chat", callback_data="mic:chat", style="primary")],
        [B("🏠 Home", callback_data="menu:home", style="primary")],
    ])

def status_text(user_id: int, data: dict, uvc, s: dict, premium: dict = None) -> str:
    data = data or {}
    logged = ("🟢 Active" if uvc else
              ("💾 Saved (idle)" if data.get("string_session") else "🔴 Not logged in"))
    acc = (
        f"├ <b>VC Account:</b> {_html.escape(str(uvc.account_name or '—'))} "
        f"(@{_html.escape(str(uvc.account_username or 'none'))})\n"
        f"├ <b>Acc ID:</b> <code>{uvc.account_id}</code>\n"
        f"└ <b>Active VCs:</b> {len(uvc.chats)}\n"
        if uvc else "└ <i>Koi active VC session nahi</i>\n"
    )
    from helpers.access_control import format_until, plan_label, time_left
    if premium and premium.get("active"):
        if premium.get("plan") == "owner":
            prem = "👑 <b>Owner</b> · unlimited"
        else:
            left = time_left(premium.get("until"))
            prem = (f"💎 <b>Premium</b> · {plan_label(premium.get('plan'))}\n"
                    f"   till <b>{format_until(premium.get('until'))}</b>"
                    + (f" · ⏳ {left}" if left and left != "expired" else ""))
    else:
        prem = "🆓 <b>Free plan</b> · upgrade ke liye 💎 Premium"
    esc = _html.escape
    joined = format_until(data.get("joined_at")) if data.get("joined_at") else "—"
    return card("👤", "Your account",
        "<b>Profile</b>\n"
        f"├ <b>ID:</b> <code>{user_id}</code>\n"
        f"├ <b>Name:</b> {esc(str(data.get('first_name') or '—'))}\n"
        f"├ <b>Username:</b> @{esc(str(data.get('username') or 'none'))}\n"
        f"└ <b>Joined:</b> {joined}\n\n"
        "<b>Session</b>\n"
        f"├ <b>Login:</b> {logged}\n"
        f"{acc}\n"
        "<b>Sound</b>\n"
        f"├ <b>Volume:</b> <code>{_g(s, 'relay_volume', Config.RELAY_DEFAULT_VOLUME)}/3000</code>\n"
        f"├ <b>Gain:</b> {_g(s, 'gain', Config.RELAY_DEFAULT_GAIN)} · <b>Bass:</b> +{_g(s, 'bass')} · "
        f"<b>Treble:</b> {_g(s, 'treble', Config.RELAY_DEFAULT_TREBLE)}\n"
        f"├ <b>Voice:</b> {_g(s, 'voice', 'normal')}\n"
        f"└ <b>Boost:</b> {_g(s, 'boost')}/10 · <b>Echo:</b> "
        f"{'On' if _g(s, 'echo') else 'Off'} {_g(s, 'echo_level')}/10\n\n"
        f"{prem}")


# ─── v81 Now Playing card ──────────────────────────────────────
def now_playing_text(st, status: str = "playing", chat_title: str = "",
                     account: str = "", queue_pos: int = 0) -> str:
    """Ek hi sundar card — .play, .playforce aur 🔄 Refresh teeno yahi dikhate hai."""
    name = getattr(st, "source_name", "") or "Audio"
    paused = bool(getattr(st, "is_paused", False))
    head = {"queued": ("📥", "Queued"), "force": ("⚡", "Force playing")}.get(
        status, ("⏸", "Paused") if paused else ("🎧", "Now playing"))
    mode = _loud_mode()
    ico, mname, mdesc = LOUD_MODE_INFO.get(mode, LOUD_MODE_INFO["loudest"])
    state = ("⏸ <b>Paused</b>" if paused else "🔴 <b>LIVE</b> · VC me baj raha hai")
    if status == "queued":
        state = f"📥 <b>Queue #{queue_pos}</b> · line me hai"
    flags = []
    if getattr(st, "loop", False):
        flags.append("🔁 Loop")
    if getattr(st, "hand_raise", False):
        flags.append("🧑 Human")
    if getattr(st, "ss_on", False):
        flags.append("🖥 Screen")
    if getattr(st, "echo", False):
        flags.append("🌊 Echo")
    body = f"🎵 <b>{name}</b>\n{state}\n"
    if chat_title:
        body += f"💬 <code>{'Group':<7}</code> <b>{chat_title}</b>\n"
    if account:
        body += f"👤 <code>{'ID':<7}</code> <b>{account}</b>\n"
    body += (f"\n{ico} <code>{'Mode':<7}</code> <b>{mname}</b> · {mdesc}\n"
             f"🔊 {meter('Volume', getattr(st, 'relay_volume', 0) or 0, 3000)}\n"
             f"💥 {meter('Boost', getattr(st, 'boost', 0), 10)}")
    if flags:
        body += "\n" + "  ·  ".join(flags)
    return card(head[0], head[1], body,
                "⚡ <b>TEZ</b> = 1 tap me sab MAX + sab IDs 200%. "
                "200% sabko tabhi sunta hai jab playing ID group admin ho.")
