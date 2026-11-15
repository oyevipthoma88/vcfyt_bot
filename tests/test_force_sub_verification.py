import asyncio
from unittest.mock import AsyncMock
import pytest
from pyrogram.enums import ChatMemberStatus
from helpers import force_sub


def test_private_invite_requires_numeric_id_and_join_url():
    with pytest.raises(ValueError):
        force_sub.parse_target("https://t.me/+privatehash")
    item = force_sub.parse_target("-1001234567890 https://t.me/+privatehash")
    assert item == {"ref": "-1001234567890", "url": "https://t.me/+privatehash", "kind": "id"}


def test_numeric_id_without_join_url_rejected():
    with pytest.raises(ValueError):
        force_sub.parse_target("-1001234567890")


def test_numeric_id_rejects_non_telegram_join_url():
    with pytest.raises(ValueError):
        force_sub.parse_target("-1001234567890 https://example.com/join")


def test_membership_check_fails_closed_on_api_error(monkeypatch):
    async def run():
        entry = {"ref": "-1001234567890", "url": "https://t.me/target", "kind": "id"}
        monkeypatch.setattr(force_sub, "resolve_chat_id", AsyncMock(return_value=-1001234567890))
        bot = AsyncMock()
        bot.get_chat_member.side_effect = RuntimeError("CHAT_ADMIN_REQUIRED")
        assert await force_sub.is_member(bot, entry, 123) is False
    asyncio.run(run())


def test_left_user_is_not_verified(monkeypatch):
    async def run():
        entry = {"ref": "-1001234567890", "url": "https://t.me/target", "kind": "id"}
        monkeypatch.setattr(force_sub, "resolve_chat_id", AsyncMock(return_value=-1001234567890))
        force_sub._member_cache.clear()
        bot = AsyncMock()
        bot.get_chat_member.return_value.status = ChatMemberStatus.LEFT
        assert await force_sub.is_member(bot, entry, 456) is False
    asyncio.run(run())


def test_member_user_passes_verification(monkeypatch):
    async def run():
        entry = {"ref": "-1001234567890", "url": "https://t.me/target", "kind": "id"}
        monkeypatch.setattr(force_sub, "resolve_chat_id", AsyncMock(return_value=-1001234567890))
        force_sub._member_cache.clear()
        bot = AsyncMock()
        bot.get_chat_member.return_value.status = ChatMemberStatus.MEMBER
        assert await force_sub.is_member(bot, entry, 789) is True
    asyncio.run(run())
