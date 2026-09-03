from pyrogram import Client

from helpers.bot_api_styles import apply_native_styles, has_native_styles


def _transport_markup(markup):
    """Hide styled markup during MTProto send to avoid a normal-button flash."""
    # An empty InlineKeyboardMarkup is rejected by Telegram in some private
    # chats, which can make /start appear to fail. None omits the field cleanly;
    # the native Bot API patch adds the real keyboard immediately afterwards.
    return None if has_native_styles(markup) else markup


class StyledBotClient(Client):
    """Keep Pyrofork for updates while patching inline markup via Bot API."""

    async def send_message(self, *args, **kwargs):
        markup = kwargs.get("reply_markup")
        kwargs["reply_markup"] = _transport_markup(markup)
        message = await super().send_message(*args, **kwargs)
        if markup:
            await apply_native_styles(message, markup)
        return message

    async def send_photo(self, *args, **kwargs):
        markup = kwargs.get("reply_markup")
        kwargs["reply_markup"] = _transport_markup(markup)
        message = await super().send_photo(*args, **kwargs)
        if markup:
            await apply_native_styles(message, markup)
        return message

    async def send_animation(self, *args, **kwargs):
        markup = kwargs.get("reply_markup")
        kwargs["reply_markup"] = _transport_markup(markup)
        message = await super().send_animation(*args, **kwargs)
        if markup:
            await apply_native_styles(message, markup)
        return message

    async def edit_message_text(self, *args, **kwargs):
        markup = kwargs.get("reply_markup")
        kwargs["reply_markup"] = _transport_markup(markup)
        message = await super().edit_message_text(*args, **kwargs)
        if markup:
            await apply_native_styles(message, markup)
        return message

    async def edit_message_caption(self, *args, **kwargs):
        markup = kwargs.get("reply_markup")
        kwargs["reply_markup"] = _transport_markup(markup)
        message = await super().edit_message_caption(*args, **kwargs)
        if markup:
            await apply_native_styles(message, markup)
        return message
