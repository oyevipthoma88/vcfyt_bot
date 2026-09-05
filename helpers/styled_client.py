from pyrogram import Client

from helpers.bot_api_styles import apply_native_styles, has_native_styles

def _transport_markup(markup):
    """Hide styled markup during MTProto send to avoid a normal-button flash."""

    return None if has_native_styles(markup) else markup

def _quote_text(value):
    if not isinstance(value, str) or not value.strip():
        return value
    if "<blockquote>" in value:
        return value
    return f"<blockquote>{value}</blockquote>"

class StyledBotClient(Client):
    """Keep Pyrofork for updates while patching inline markup via Bot API."""

    async def send_message(self, *args, **kwargs):
        markup = kwargs.get("reply_markup")
        kwargs["reply_markup"] = _transport_markup(markup)
        if len(args) > 1:
            args = (*args[:1], _quote_text(args[1]), *args[2:])
        elif "text" in kwargs:
            kwargs["text"] = _quote_text(kwargs["text"])
        message = await super().send_message(*args, **kwargs)
        if markup:
            await apply_native_styles(message, markup)
        return message

    async def send_photo(self, *args, **kwargs):
        markup = kwargs.get("reply_markup")
        kwargs["reply_markup"] = _transport_markup(markup)
        if "caption" in kwargs:
            kwargs["caption"] = _quote_text(kwargs["caption"])
        message = await super().send_photo(*args, **kwargs)
        if markup:
            await apply_native_styles(message, markup)
        return message

    async def send_animation(self, *args, **kwargs):
        markup = kwargs.get("reply_markup")
        kwargs["reply_markup"] = _transport_markup(markup)
        if "caption" in kwargs:
            kwargs["caption"] = _quote_text(kwargs["caption"])
        message = await super().send_animation(*args, **kwargs)
        if markup:
            await apply_native_styles(message, markup)
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
            await apply_native_styles(message, markup)
        return message

    async def edit_message_caption(self, *args, **kwargs):
        markup = kwargs.get("reply_markup")
        kwargs["reply_markup"] = _transport_markup(markup)
        if "caption" in kwargs:
            kwargs["caption"] = _quote_text(kwargs["caption"])
        message = await super().edit_message_caption(*args, **kwargs)
        if markup:
            await apply_native_styles(message, markup)
        return message
