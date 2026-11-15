"""Force-subscribe / auto-join system.

Owner adds one or more channel links from the owner panel. Every active user
must be a member of those channels. The bot shows Join buttons and blocks further commands until membership
is explicitly verified. Verification failures never pass the gate.
"""

import asyncio
import json
import logging
from typing import List, Optional
from urllib.parse import urlparse

from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import UserNotParticipant

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
    bits = value.split(maxsplit=1)
    if len(bits) == 2 and bits[0].lstrip("-").isdigit():
        from helpers.peer_guard import chat_id_variants
        chat_id = chat_id_variants(bits[0])[0]
        join_url = bits[1].strip()
        parsed_join = urlparse(join_url if "://" in join_url else f"https://{join_url}")
        if parsed_join.netloc.lower() not in ("t.me", "telegram.me", "telegram.dog"):
            raise ValueError("Numeric channel ID ke saath valid t.me Join URL dein.")
        if not parsed_join.path.strip("/"):
            raise ValueError("Join URL khaali hai.")
        return {"ref": str(chat_id), "url": parsed_join.geturl(), "kind": "id"}

    if value.startswith("@"):
        username = value[1:].strip("/")
        if not username:
            raise ValueError("Username khaali hai.")
        return {"ref": username, "url": f"https://t.me/{username}", "kind": "username"}

    if value.lstrip("-").isdigit():
        raise ValueError("Channel ID ke saath Join link bhi dein: /addfsub -100... https://t.me/channel")

    parsed = urlparse(value if "://" in value else f"https://{value}")
    if parsed.netloc.lower() not in ("t.me", "telegram.me", "telegram.dog"):
        raise ValueError("Sirf t.me link, @username ya -100 channel ID chalega.")
    path = parsed.path.strip("/")
    if not path:
        raise ValueError("Link me channel ka naam nahi hai.")
    if path.startswith("+") or path.startswith("joinchat/"):
        raise ValueError("Private invite ko verify karne ke liye numeric channel ID + invite URL dono dein: /addfsub -100... https://t.me/+invite")
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
        # Never treat an unverifiable Must Join target as success. Keep the gate
        # closed and log it so an owner can repair/remove the bad entry.
        logger.error("Must Join target is not verifiable: %s", entry.get("ref"))
        return False
    key = (chat_id, user_id)
    hit = _member_cache.get(key)
    if hit and _now() - hit < _CACHE_TTL:
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except UserNotParticipant:
        return False
    except Exception as exc:
        # Missing bot admin rights / inaccessible chat must not fail open.
        logger.error("Must Join membership check failed for %s: %s", chat_id, exc)
        return False
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

