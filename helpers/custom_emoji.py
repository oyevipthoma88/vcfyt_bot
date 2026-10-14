"""Telegram Premium custom emoji IDs for inline buttons.

Saare IDs Telegram se verify kiye gaye (getCustomEmojiStickers / premium packs).
Jab icon lagta hai to button text ka normal emoji hata diya jaata hai, taaki
sirf premium emoji dikhe (pehle normal + premium dono aate the).
Telegram icon reject kare to bot_api_styles original text wapas laga deta hai.
"""

ICONS = {
    "⏭": "5348125953090403204",
    "⏰": "5983192938602041487",
    "⏹": "5411225014148014586",
    "▶": "5348125953090403204",
    "♻": "5375338737028841420",
    "⚔": "5408935401442267103",
    "⚙": "5341715473882955310",
    "⚡": "5888578476973232531",
    "✂": "5237808360882977239",
    "✅": "5206607081334906820",
    "✍": "5172506385329422865",
    "❌": "5210952531676504517",
    "❓": "5436113877181941026",
    "➕": "5397916757333654639",
    "⬅": "5877536313623711363",
    "🌊": "5206421491503088521",
    "🌱": "5449885771420934013",
    "🎙": "5888760051010637563",
    "🎚": "5341715473882955310",
    "🎛": "5341715473882955310",
    "🎟": "5377599075237502153",
    "🎤": "5429413328768224565",
    "🎬": "5375464961822695044",
    "🎯": "5310278924616356636",
    "🎵": "5206421491503088521",
    "🏠": "5877506824378257176",
    "🏷": "5895293752829808957",
    "👑": "5217822164362739968",
    "👤": "5373012449597335010",
    "👥": "5942877472163892475",
    "💎": "5429153655045507054",
    "💥": "5276032951342088188",
    "💬": "5886378723573240274",
    "💳": "5429153655045507054",
    "💻": "5431376038628171216",
    "📉": "5246762912428603768",
    "📊": "5231200819986047254",
    "📋": "5231200819986047254",
    "📢": "5889016933004612635",
    "📥": "5443127283898405358",
    "📨": "5406631276042002796",
    "📱": "6037193420680729470",
    "🔁": "5375338737028841420",
    "🔄": "5375338737028841420",
    "🔇": "5388632425314140043",
    "🔉": "5388632425314140043",
    "🔊": "5388632425314140043",
    "🔐": "5886505193180239900",
    "🔑": "5330115548900501467",
    "🔒": "5983353389990285649",
    "🔓": "5886505193180239900",
    "🔗": "5778351117733729026",
    "🔥": "5170202955713872686",
    "🔴": "5411225014148014586",
    "🖥": "5282843764451195532",
    "🗃": "5443127283898405358",
    "🗑": "5445267414562389170",
    "🚀": "5445284980978621387",
    "🚪": "5877536313623711363",
    "🛡": "5251203410396458957",
    "🟢": "5416081784641168838",
    "🤖": "5983534401386976295",
    "🧑": "5373012449597335010",
    # Aliases (sirf verified IDs reuse kiye)
    "⏸": "5411225014148014586",
    "🛑": "5411225014148014586",
    "☢": "5276032951342088188",
    "🎧": "5206421491503088521",
    "🎶": "5206421491503088521",
    "📘": "5406631276042002796",
    "⚠": "5436113877181941026",
    "☰": "5231200819986047254",
    "✨": "5429153655045507054",
}

_VS = "\ufe0f"


def _split(text: str):
    """(emoji, rest) agar text kisi known emoji se start ho."""
    t = (text or "").lstrip()
    for emoji in sorted(ICONS, key=len, reverse=True):
        for form in (emoji + _VS, emoji):
            if t.startswith(form):
                return emoji, t[len(form):].lstrip(_VS).lstrip()
    return None, text


def button_icon(text: str):
    emoji, _ = _split(text)
    return ICONS.get(emoji) if emoji else None


def strip_icon_emoji(text: str) -> str:
    """Button text bina leading emoji ke (icon_custom_emoji_id use hone par)."""
    emoji, rest = _split(text)
    return rest if emoji and rest else text


# ---------------------------------------------------------------------------
# PREMIUM TEXT EMOJI: messages, status panels and notifications.
# Har known emoji ko <emoji id="..."> tag me wrap karte hain (Pyrogram HTML).
# Telegram jahan custom emoji allow na kare, wahan wahi normal emoji dikhta hai
# (safe fallback). PREMIUM_TEXT_EMOJI=0 se band kar sakte hain.
# ---------------------------------------------------------------------------
import os as _os
import re as _re

_TEXT_RE = _re.compile(
    "(" + "|".join(_re.escape(e) for e in sorted(ICONS, key=len, reverse=True)) + ")\ufe0f?"
)
_SKIP_RE = _re.compile(r"(?is)<(code|pre|emoji|a)\b.*?</\1>|`[^`]*`|<[^>]+>")


def premium_text(value):
    """Wrap known emojis in message text with premium custom-emoji tags."""
    if not isinstance(value, str) or not value:
        return value
    if _os.environ.get("PREMIUM_TEXT_EMOJI", "1") == "0":
        return value

    def _wrap(chunk: str) -> str:
        return _TEXT_RE.sub(lambda m: f'<emoji id="{ICONS[m.group(1)]}">{m.group(1)}</emoji>', chunk)

    out, pos = [], 0
    for m in _SKIP_RE.finditer(value):
        out.append(_wrap(value[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_wrap(value[pos:]))
    return "".join(out)
