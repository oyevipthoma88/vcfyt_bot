# Telegram coloured button support

Checked on 2026-09-01.

Telegram's official MTProto documentation defines `keyboardButtonStyle` with `bg_primary`, `bg_success`, and `bg_danger` flags: https://core.telegram.org/api/bots/buttons and https://core.telegram.org/constructor/keyboardButtonStyle.

The project currently uses Pyrofork 2.3.69. In this runtime, `pyrogram.types.InlineKeyboardButton` accepts standard fields such as text, callback data, URL, web app, login URL, and copy text, but no `style` or `icon_custom_emoji_id` parameter. The installed `pyrogram.raw.types.KeyboardButtonCallback` has no style field, and the raw type collection has no `KeyboardButtonStyle` type.

Therefore the final implementation keeps Pyrogram for handlers and VC logic, but uses Telethon 1.44's current raw TL schema as a serialization adapter behind the existing button helper. It emits Telegram's real `KeyboardButtonStyle` flags (`bg_primary`, `bg_success`, and `bg_danger`) for callback and URL buttons. If Telethon is unavailable in a minimal environment, the helper falls back to standard buttons instead of injecting unsupported fields.
