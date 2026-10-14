"""Grow: promote owner-chosen bots as admin in every group/channel where a
connected user account is owner or admin (with add-admin right).

The bot gets exactly the same rights the user account has there.
"""
import asyncio
import inspect
import logging

from pyrogram import enums
from pyrogram.errors import FloodWait, RPCError
from pyrogram.types import ChatPrivileges

from helpers.database import db

logger = logging.getLogger(__name__)

BOTS_KEY = "grow_bots"
AUTO_KEY = "grow_auto"

_PRIV_FIELDS = [
    p for p in inspect.signature(ChatPrivileges.__init__).parameters
    if p != "self"
]
_running: set[int] = set()


async def get_bots() -> list[str]:
    raw = (await db.get_app_value(BOTS_KEY)) or ""
    return [b for b in raw.split() if b]


async def set_bots(names: list[str]):
    clean = []
    for n in names:
        n = n.strip().lstrip("@").split("/")[-1]
        if n and n.lower() not in [c.lower() for c in clean]:
            clean.append(n)
    await db.set_app_value(BOTS_KEY, " ".join(clean))
    return clean


async def is_auto() -> bool:
    return (await db.get_app_value(AUTO_KEY)) == "1"


async def set_auto(on: bool):
    await db.set_app_value(AUTO_KEY, "1" if on else "0")


def _copy_privileges(member) -> ChatPrivileges | None:
    status = member.status
    if status == enums.ChatMemberStatus.OWNER:
        kwargs = {f: True for f in _PRIV_FIELDS if f != "is_anonymous"}
        return ChatPrivileges(**kwargs)
    if status == enums.ChatMemberStatus.ADMINISTRATOR:
        priv = member.privileges
        if not priv or not getattr(priv, "can_promote_members", False):
            return None
        kwargs = {f: bool(getattr(priv, f, False)) for f in _PRIV_FIELDS}
        kwargs["is_anonymous"] = False
        return ChatPrivileges(**kwargs)
    return None


USER_CONCURRENCY = 8      # accounts processed at the same time
CHAT_CONCURRENCY = 4      # groups per account processed at the same time
MAX_TRIES = 5
MAX_FLOOD = 300           # longest FloodWait we are willing to sleep


async def _call(fn, *a, **kw):
    """Retry with FloodWait handling + exponential backoff on network errors."""
    last = None
    for attempt in range(MAX_TRIES):
        try:
            return await fn(*a, **kw)
        except FloodWait as e:
            last = e
            wait = int(getattr(e, "value", 5) or 5)
            if wait > MAX_FLOOD:
                raise
            await asyncio.sleep(wait + 1)
        except (OSError, asyncio.TimeoutError, ConnectionError) as e:
            last = e
            await asyncio.sleep(min(2 ** attempt, 20))
        except RPCError as e:
            # Server-side hiccups are retryable, permission errors are not.
            if type(e).__name__ in ("InternalServerError", "ServiceUnavailable",
                                    "Timeout", "RpcCallFail", "MsgWaitFailed"):
                last = e
                await asyncio.sleep(min(2 ** attempt, 20))
                continue
            raise
    raise last


async def _open_client(user_id: int, string_session: str):
    """Return (client, is_temp). Reuse a live session, else open a light
    temporary client (no voice engine) so every logged account can be used."""
    from pyrogram import Client
    from config import Config
    from helpers.vc_manager import session_manager
    uvc = session_manager.users.get(user_id)
    if uvc and uvc.client and getattr(uvc.client, "is_connected", False):
        return uvc.client, False
    last = None
    for attempt in range(3):
        c = Client(f"grow_{user_id}", api_id=Config.API_ID, api_hash=Config.API_HASH,
                   session_string=string_session, in_memory=True, no_updates=True)
        try:
            await asyncio.wait_for(c.start(), 40)
            return c, True
        except Exception as e:
            last = e
            try:
                await c.stop()
            except Exception:
                pass
            name = type(e).__name__
            if name in ("AuthKeyUnregistered", "AuthKeyInvalid", "SessionRevoked",
                        "UserDeactivated", "UserDeactivatedBan", "SessionExpired"):
                break
            await asyncio.sleep(2 * (attempt + 1))
    raise last


async def _promote(client, chat, bu, privs) -> bool:
    try:
        await _call(client.promote_chat_member, chat.id, bu.id, privs)
        return True
    except FloodWait:
        raise
    except RPCError:
        pass
    # Bot not in chat yet: add it, then promote.
    if chat.type != enums.ChatType.CHANNEL:
        try:
            await _call(client.add_chat_members, chat.id, bu.id)
        except RPCError as e:
            logger.info("grow add %s -> %s: %r", bu.id, chat.id, e)
    await asyncio.sleep(0.5)
    await _call(client.promote_chat_member, chat.id, bu.id, privs)
    return True


async def grow_user(user_id: int, bots: list[str] | None = None,
                    string_session: str | None = None, on_step=None) -> dict:
    """Run grow for one account. Returns a small report."""
    report = {"chats": 0, "promoted": 0, "failed": 0, "error": ""}
    if user_id in _running:
        report["error"] = "already running"
        return report
    bots = bots if bots is not None else await get_bots()
    if not bots:
        report["error"] = "no bots set"
        return report
    if not string_session:
        data = await db.get_user(user_id)
        string_session = (data or {}).get("string_session")
    if not string_session:
        report["error"] = "not logged in"
        return report
    _running.add(user_id)
    client, temp = None, False
    try:
        client, temp = await _open_client(user_id, string_session)
        bot_users = []
        for name in bots:
            try:
                bot_users.append(await _call(client.get_users, name))
            except Exception as e:
                logger.warning("grow: cannot resolve @%s: %r", name, e)
        if not bot_users:
            report["error"] = "bot usernames not found"
            return report

        chats = []
        async for dialog in client.get_dialogs():
            if dialog.chat.type in (enums.ChatType.SUPERGROUP, enums.ChatType.GROUP,
                                    enums.ChatType.CHANNEL):
                chats.append(dialog.chat)

        sem = asyncio.Semaphore(CHAT_CONCURRENCY)

        async def _do_chat(chat):
            async with sem:
                try:
                    me = await _call(client.get_chat_member, chat.id, "me")
                except Exception:
                    return
                privs = _copy_privileges(me)
                if not privs:
                    return
                report["chats"] += 1
                for bu in bot_users:
                    try:
                        await _promote(client, chat, bu, privs)
                        report["promoted"] += 1
                    except Exception as e:
                        report["failed"] += 1
                        logger.info("grow: %s in %s failed: %r", bu.username, chat.id, e)
                    if on_step:
                        try:
                            await on_step()
                        except Exception:
                            pass
                    await asyncio.sleep(0.3)

        await asyncio.gather(*(_do_chat(c) for c in chats))
    except Exception as e:
        report["error"] = str(e)[:150] or type(e).__name__
        logger.warning("grow_user %s failed: %r", user_id, e)
    finally:
        _running.discard(user_id)
        if temp and client:
            try:
                await client.stop()
            except Exception:
                pass
    return report


async def grow_all(progress=None) -> dict:
    """Load EVERY logged-in account and grow them all in parallel."""
    total = {"users": 0, "accounts": 0, "skipped": 0, "chats": 0,
             "promoted": 0, "failed": 0}
    bots = await get_bots()
    if not bots:
        return total
    users = [u for u in await db.all_users() if u.get("string_session")]
    total["accounts"] = len(users)
    sem = asyncio.Semaphore(USER_CONCURRENCY)
    loop = asyncio.get_event_loop()
    last = [0.0]

    async def _tick(force=False):
        if not progress:
            return
        now = loop.time()
        if not force and now - last[0] < 3:   # don't spam message edits
            return
        last[0] = now
        try:
            await progress(dict(total))
        except Exception:
            pass

    async def _one(u):
        async with sem:
            uid = int(u["user_id"])
            r = None
            for attempt in range(2):   # whole-account retry
                r = await grow_user(uid, bots, u["string_session"])
                if not r["error"] or r["error"] in ("already running", "bot usernames not found"):
                    break
                await asyncio.sleep(3)
            if r["error"]:
                total["skipped"] += 1
            else:
                total["users"] += 1
            for k in ("chats", "promoted", "failed"):
                total[k] += r[k]
            await _tick()

    await asyncio.gather(*(_one(u) for u in users))
    await _tick(force=True)
    return total


def schedule_auto_grow(user_id: int):
    """Called after a user connects; runs in background if auto is ON."""
    async def _run():
        try:
            if await is_auto() and await get_bots():
                await asyncio.sleep(5)
                for _ in range(3):
                    r = await grow_user(user_id)
                    if not r["error"] or r["error"] == "already running":
                        break
                    await asyncio.sleep(10)
        except Exception as e:
            logger.warning("auto grow %s: %r", user_id, e)
    asyncio.create_task(_run())
