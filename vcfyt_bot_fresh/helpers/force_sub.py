"""Force-subscribe / auto-join system.

Owner adds one or more channel links from the owner panel. Every active user
must be a member of those channels. If the user already has a logged-in
account, their own account is auto-joined silently; otherwise the bot shows
join buttons and blocks further commands until they join.
"""

import asyncio
import json
import logging
from typing import List, Optional
from urllib.parse import urlparse

from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import FloodWait, UserAlreadyParticipant, UserNotParticipant

from helpers.database import db

logger = logging.getLogger("vcbot.force_sub")

_KEY = "force_sub_channels"
_cache: Optional[List[dict]] = None
_member_cache: dict = {}
_CACHE_TTL = 300


def _now() -> float:
    try:
        return asyncio.get_running_loop().time()
    except RuntimeError:
        return 0.0


def parse_target(raw: str) -> dict:
    """Normalise a user supplied channel reference into a storable entry."""
    value = (raw or "").strip()
    if not value:
        raise ValueError("Channel link ya @username dein.")

    if value.startswith("@"):
        username = value[1:].strip("/")
        if not username:
            raise ValueError("Username khaali hai.")
        return {"ref": username, "url": f"https://t.me/{username}", "kind": "username"}

    if value.lstrip("-").isdigit():
        chat_id = int(value)
        if chat_id >= 0:
            raise ValueError("Channel ID negative honi chahiye (-100...).")
        return {"ref": str(chat_id), "url": "", "kind": "id"}

    parsed = urlparse(value if "://" in value else f"https://{value}")
    if parsed.netloc.lower() not in ("t.me", "telegram.me", "telegram.dog"):
        raise ValueError("Sirf t.me link, @username ya -100 channel ID chalega.")
    path = parsed.path.strip("/")
    if not path:
        raise ValueError("Link me channel ka naam nahi hai.")
    if path.startswith("+") or path.startswith("joinchat/"):
        return {"ref": value, "url": value, "kind": "invite"}
    username = path.split("/")[0]
    return {"ref": username, "url": f"https://t.me/{username}", "kind": "username"}


async def load(force: bool = False) -> List[dict]:
    global _cache
    if _cache is not None and not force:
        return _cache
    try:
        raw = await db.get_app_value(_KEY)
        _cache = json.loads(raw) if raw else []
    except Exception as exc:
        logger.warning("force_sub load failed: %s", exc)
        _cache = []
    return _cache


async def _save(entries: List[dict]):
    global _cache, _member_cache
    _cache = entries
    _member_cache = {}
    await db.set_app_value(_KEY, json.dumps(entries))


async def add(raw: str) -> dict:
    entry = parse_target(raw)
    entries = list(await load(force=True))
    if any(e["ref"].lower() == entry["ref"].lower() for e in entries):
        raise ValueError("Ye channel pehle se added hai.")
    if len(entries) >= 10:
        raise ValueError("Maximum 10 force-join channels allowed.")
    entries.append(entry)
    await _save(entries)
    return entry


async def remove(raw: str) -> bool:
    needle = (raw or "").strip().lstrip("@").lower()
    entries = await load(force=True)
    kept = [e for e in entries if e["ref"].lower() != needle and e.get("url", "").lower() != needle]
    if len(kept) == len(entries):
        return False
    await _save(kept)
    return True


async def clear():
    await _save([])


def _chat_arg(entry: dict):
    ref = entry["ref"]
    if entry["kind"] == "id":
        return int(ref)
    if entry["kind"] == "username":
        return f"@{ref}"
    return ref  # invite link — membership can't be checked directly


async def resolve_chat_id(bot, entry: dict) -> Optional[int]:
    if entry["kind"] == "id":
        return int(entry["ref"])
    if entry["kind"] != "username":
        return None
    cached = entry.get("chat_id")
    if cached:
        return int(cached)
    try:
        chat = await bot.get_chat(f"@{entry['ref']}")
    except Exception as exc:
        logger.warning("force_sub resolve %s failed: %s", entry["ref"], exc)
        return None
    entry["chat_id"] = chat.id
    return chat.id


async def is_member(bot, entry: dict, user_id: int) -> bool:
    chat_id = await resolve_chat_id(bot, entry)
    if chat_id is None:
        # Private invite links cannot be verified — never block on them.
        return True
    key = (chat_id, user_id)
    hit = _member_cache.get(key)
    if hit and _now() - hit < _CACHE_TTL:
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except UserNotParticipant:
        return False
    except FloodWait as exc:
        await asyncio.sleep(min(10, int(getattr(exc, "value", 3))))
        return True
    except Exception as exc:
        # Bot not admin there, or channel unreachable — do not lock users out.
        logger.warning("force_sub check %s failed: %s", chat_id, exc)
        return True
    if member.status in (ChatMemberStatus.BANNED, ChatMemberStatus.LEFT):
        return False
    _member_cache[key] = _now()
    return True


async def missing_channels(bot, user_id: int) -> List[dict]:
    entries = await load()
    if not entries:
        return []
    missing = []
    for entry in entries:
        if not await is_member(bot, entry, user_id):
            missing.append(entry)
    return missing


async def auto_join_with_user_account(user_client, entries: List[dict]) -> int:
    """Silently join the force-sub channels with the user's own account."""
    joined = 0
    for entry in entries:
        target = _chat_arg(entry)
        try:
            await user_client.join_chat(target)
            joined += 1
        except UserAlreadyParticipant:
            joined += 1
        except FloodWait as exc:
            await asyncio.sleep(min(20, int(getattr(exc, "value", 5)) + 1))
        except Exception as exc:
            logger.info("auto join %s failed: %s", entry["ref"], exc)
    if joined:
        global _member_cache
        _member_cache = {}
    return joined
