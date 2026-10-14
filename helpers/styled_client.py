from pyrogram import Client

from helpers.bot_api_styles import apply_native_styles, has_native_styles

def _transport_markup(markup):

    return None if has_native_styles(markup) else markup

def _quote_text(value):
    # Premium emoji upgrade: messages / panels / notifications.
    from helpers.custom_emoji import premium_text
    return premium_text(value)

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


async def _call_premium(fn, args, kwargs, idx, key):
    """Send with premium text emoji; if Telegram rejects the entities
    (ENTITY_TEXT_INVALID) retry once with plain emoji so nothing vanishes."""
    from helpers.custom_emoji import plain_text
    raw_args, raw_kwargs = args, dict(kwargs)
    if len(args) > idx:
        args = (*args[:idx], _quote_text(args[idx]), *args[idx + 1:])
    elif key in kwargs:
        kwargs = dict(kwargs); kwargs[key] = _quote_text(kwargs[key])
    try:
        return await fn(*args, **kwargs)
    except Exception as exc:
        if "ENTITY_TEXT_INVALID" not in str(exc) and "ENTITIES_TOO_LONG" not in str(exc) \
                and "DOCUMENT_INVALID" not in str(exc):
            raise
        if len(raw_args) > idx:
            raw_args = (*raw_args[:idx], plain_text(raw_args[idx]), *raw_args[idx + 1:])
        elif key in raw_kwargs:
            raw_kwargs[key] = plain_text(raw_kwargs[key])
        return await fn(*raw_args, **raw_kwargs)


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
        message = await _call_premium(super().send_message, args, kwargs, 1, 'text')
        if markup:
            await _restyle_or_fallback(self, message, markup)
        return message

    async def send_photo(self, *args, **kwargs):
        markup = kwargs.get("reply_markup")
        kwargs["reply_markup"] = _transport_markup(markup)
        message = await _call_premium(super().send_photo, args, kwargs, 99, "caption")
        if markup:
            await _restyle_or_fallback(self, message, markup)
        return message

    async def send_animation(self, *args, **kwargs):
        markup = kwargs.get("reply_markup")
        kwargs["reply_markup"] = _transport_markup(markup)
        message = await _call_premium(super().send_animation, args, kwargs, 99, "caption")
        if markup:
            await _restyle_or_fallback(self, message, markup)
        return message

    async def edit_message_text(self, *args, **kwargs):
        markup = kwargs.get("reply_markup")
        kwargs["reply_markup"] = _transport_markup(markup)
        message = await _call_premium(super().edit_message_text, args, kwargs, 2, 'text')
        if markup:
            await _restyle_or_fallback(self, message, markup)
        return message

    async def edit_message_caption(self, *args, **kwargs):
        markup = kwargs.get("reply_markup")
        kwargs["reply_markup"] = _transport_markup(markup)
        message = await _call_premium(super().edit_message_caption, args, kwargs, 99, "caption")
        if markup:
            await _restyle_or_fallback(self, message, markup)
        return message
