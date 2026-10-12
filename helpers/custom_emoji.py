"""Telegram Premium custom emoji IDs for inline buttons (public catalog).

Button text hamesha normal emoji rakhta hai; ID sirf enhancement hai. Telegram
reject kare to bot_api_styles plain keyboard pe fallback karta hai."""

ICONS = {
    "🎤": "5429413328768224565", "🔐": "5886505193180239900",
    "▶️": "5348125953090403204", "🏠": "5877506824378257176",
    "⚙️": "5341715473882955310", "✅": "5206607081334906820",
    "💎": "5429153655045507054", "📥": "5443127283898405358",
    "🎯": "5310278924616356636", "👥": "5942877472163892475",
    "🔊": "5388632425314140043", "🎵": "5206421491503088521",
    "⬅️": "5877536313623711363",
}


def button_icon(text: str):
    for emoji, icon in ICONS.items():
        if (text or "").startswith(emoji):
            return icon
    return None
