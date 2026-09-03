from pyrogram import Client

from helpers.bot_api_styles import apply_native_styles


class StyledBotClient(Client):
    """Keep Pyrofork for updates while patching inline markup via Bot API."""

    async def send_message(self, *args, **kwargs):
        markup = kwargs.get("reply_markup")
        message = await super().send_message(*args, **kwargs)
        if markup:
            await apply_native_styles(message, markup)
        return message

    async def send_photo(self, *args, **kwargs):
        markup = kwargs.get("reply_markup")
        message = await super().send_photo(*args, **kwargs)
        if markup:
            await apply_native_styles(message, markup)
        return message

    async def edit_message_text(self, *args, **kwargs):
        markup = kwargs.get("reply_markup")
        message = await super().edit_message_text(*args, **kwargs)
        if markup:
            await apply_native_styles(message, markup)
        return message

    async def edit_message_caption(self, *args, **kwargs):
        markup = kwargs.get("reply_markup")
        message = await super().edit_message_caption(*args, **kwargs)
        if markup:
            await apply_native_styles(message, markup)
        return message
