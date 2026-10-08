from pyrogram import Client

from helpers.bot_api_styles import apply_native_styles, has_native_styles

def _transport_markup(markup):

    return None if has_native_styles(markup) else markup

def _quote_text(value):
    return value

async def _restyle_or_fallback(client, message, markup):
    """Paint native colours; if Bot API refuses, put the plain keyboard back
    so buttons never vanish (old bug: markup sent as None + failed patch)."""
    if not markup or not has_native_styles(markup) or message is None:
        return
    if await apply_native_styles(message, markup):
        return
    try:
        await Client.edit_message_reply_markup(client, message.chat.id, message.id, markup)
    except Exception:
        pass


class StyledBotClient(Client):

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None, *args, **kwargs):
        # ROOT FIX: Now Playing (SS on/off, pause, loop ...) uses edit_reply_markup.
        # It was not routed through the Bot API, so primary/success/danger
        # colours dropped and "🔹/✅/❌" prefixes showed instead.
        if reply_markup is not None and has_native_styles(reply_markup):
            class _M:  # minimal message stub for apply_native_styles
                pass
            m = _M(); m.id = message_id
            c = _M(); c.id = chat_id; m.chat = c
            if isinstance(chat_id, str):
                try:
                    c.id = (await self.get_chat(chat_id)).id
                except Exception:
                    pass
            if await apply_native_styles(m, reply_markup):
                return None
        return await super().edit_message_reply_markup(chat_id, message_id, reply_markup, *args, **kwargs)

    async def send_cached_media(self, *args, **kwargs):
        markup = kwargs.get("reply_markup")
        kwargs["reply_markup"] = _transport_markup(markup)
        message = await super().send_cached_media(*args, **kwargs)
        await _restyle_or_fallback(self, message, markup)
        return message

    async def send_message(self, *args, **kwargs):
        markup = kwargs.get("reply_markup")
        kwargs["reply_markup"] = _transport_markup(markup)
        if len(args) > 1:
            args = (*args[:1], _quote_text(args[1]), *args[2:])
        elif "text" in kwargs:
            kwargs["text"] = _quote_text(kwargs["text"])
        message = await super().send_message(*args, **kwargs)
        if markup:
            await _restyle_or_fallback(self, message, markup)
        return message

    async def send_photo(self, *args, **kwargs):
        markup = kwargs.get("reply_markup")
        kwargs["reply_markup"] = _transport_markup(markup)
        if "caption" in kwargs:
            kwargs["caption"] = _quote_text(kwargs["caption"])
        message = await super().send_photo(*args, **kwargs)
        if markup:
            await _restyle_or_fallback(self, message, markup)
        return message

    async def send_animation(self, *args, **kwargs):
        markup = kwargs.get("reply_markup")
        kwargs["reply_markup"] = _transport_markup(markup)
        if "caption" in kwargs:
            kwargs["caption"] = _quote_text(kwargs["caption"])
        message = await super().send_animation(*args, **kwargs)
        if markup:
            await _restyle_or_fallback(self, message, markup)
        return message

    async def edit_message_text(self, *args, **kwargs):
        markup = kwargs.get("reply_markup")
        kwargs["reply_markup"] = _transport_markup(markup)
        if len(args) > 2:
            args = (*args[:2], _quote_text(args[2]), *args[3:])
        elif "text" in kwargs:
            kwargs["text"] = _quote_text(kwargs["text"])
        message = await super().edit_message_text(*args, **kwargs)
        if markup:
            await _restyle_or_fallback(self, message, markup)
        return message

    async def edit_message_caption(self, *args, **kwargs):
        markup = kwargs.get("reply_markup")
        kwargs["reply_markup"] = _transport_markup(markup)
        if "caption" in kwargs:
            kwargs["caption"] = _quote_text(kwargs["caption"])
        message = await super().edit_message_caption(*args, **kwargs)
        if markup:
            await _restyle_or_fallback(self, message, markup)
        return message
