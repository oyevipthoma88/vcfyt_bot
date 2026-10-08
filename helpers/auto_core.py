"""Auto Core: every connected account ("core") joins the must-join channels
and drops random reactions + a view on the newest post + the last 15 posts.

* Normal Telegram account  -> 1 reaction per post
* Telegram Premium account -> 3 reactions per post (Telegram's own limit)

Runs automatically:
  - on every bot restart (all cores)
  - whenever a new core logs in
  - whenever a new post appears in a must-join channel (poller)
"""
import asyncio
import json
import logging
import random

from pyrogram.errors import FloodWait, RPCError, UserAlreadyParticipant

from helpers.database import db

logger = logging.getLogger("vcbot.auto_core")

AUTO_KEY = "autocore_on"
MARK_KEY = "autocore_marks"          # {"uid": {"chat_id": last_reacted_msg_id}}
OLD_POSTS = 15
USER_CONCURRENCY = 10
POLL_SECONDS = 15
MAX_FLOOD = 300

DEFAULT_EMOJIS = ["👍", "❤", "🔥", "🥰", "👏", "😁", "🎉", "🤩", "⚡", "💯",
                  "😍", "🏆", "❤‍🔥", "🤯", "👌", "🕊", "🥳", "💘", "😎", "🆒"]

_running: set[int] = set()
_marks_lock = asyncio.Lock()
_channel_tops: dict[int, int] = {}
_poller_task = None
last_report: dict = {}


# ── settings ────────────────────────────────────────────────────────────
async def is_auto() -> bool:
    # ON by default — owner can turn it off from the panel.
    return (await db.get_app_value(AUTO_KEY)) != "0"


async def set_auto(on: bool):
    await db.set_app_value(AUTO_KEY, "1" if on else "0")


async def _load_marks() -> dict:
    try:
        return json.loads((await db.get_app_value(MARK_KEY)) or "{}")
    except Exception:
        return {}


async def _save_mark(uid: int, chat_id: int, msg_id: int):
    async with _marks_lock:
        marks = await _load_marks()
        user = marks.setdefault(str(uid), {})
        if msg_id > int(user.get(str(chat_id), 0)):
            user[str(chat_id)] = msg_id
            await db.set_app_value(MARK_KEY, json.dumps(marks))


async def _channels() -> list:
    from helpers import force_sub
    return await force_sub.load(force=True)


# ── helpers ─────────────────────────────────────────────────────────────
async def _call(fn, *a, **kw):
    for attempt in range(4):
        try:
            return await fn(*a, **kw)
        except FloodWait as e:
            wait = int(getattr(e, "value", 5) or 5)
            if wait > MAX_FLOOD:
                raise
            await asyncio.sleep(wait + 1)
        except (OSError, asyncio.TimeoutError, ConnectionError):
            await asyncio.sleep(2 ** attempt)
    return await fn(*a, **kw)


def _allowed_emojis(chat) -> list:
    ar = getattr(chat, "available_reactions", None)
    if ar is None:
        return list(DEFAULT_EMOJIS)
    if getattr(ar, "all_are_enabled", False):
        return list(DEFAULT_EMOJIS)
    out = []
    for r in getattr(ar, "reactions", None) or []:
        e = getattr(r, "emoji", None)
        if e:
            out.append(e)
    return out


async def _join(client, entry):
    from helpers.force_sub import _chat_arg
    target = _chat_arg(entry)
    try:
        chat = await _call(client.join_chat, target)
    except UserAlreadyParticipant:
        chat = None
    except RPCError as e:
        if "ALREADY" not in str(e).upper():
            logger.info("core join %s failed: %r", entry.get("ref"), e)
        chat = None
    try:
        chat = await _call(client.get_chat, chat.id if chat else target)
    except Exception as e:
        logger.info("core get_chat %s failed: %r", entry.get("ref"), e)
        return None
    return chat


async def _add_views(client, chat_id: int, ids: list) -> int:
    """Count a view on these posts (messages.GetMessagesViews increment=True)."""
    if not ids:
        return 0
    from pyrogram.raw.functions.messages import GetMessagesViews
    try:
        peer = await client.resolve_peer(chat_id)
        await _call(client.invoke, GetMessagesViews(peer=peer, id=list(ids), increment=True))
        return len(ids)
    except Exception as e:
        logger.info("core views %s: %r", chat_id, e)
        return 0


# ── per account ─────────────────────────────────────────────────────────
async def core_user(user_id: int, string_session: str | None = None) -> dict:
    rep = {"joined": 0, "reacted": 0, "viewed": 0, "failed": 0, "premium": False, "error": ""}
    if user_id in _running:
        rep["error"] = "already running"
        return rep
    entries = await _channels()
    if not entries:
        rep["error"] = "no channels"
        return rep
    if not string_session:
        data = await db.get_user(user_id)
        string_session = (data or {}).get("string_session")
    if not string_session:
        rep["error"] = "not logged in"
        return rep

    from helpers.grow import _open_client
    _running.add(user_id)
    client, temp = None, False
    try:
        client, temp = await _open_client(user_id, string_session)
        me = await _call(client.get_me)
        rep["premium"] = bool(getattr(me, "is_premium", False))
        per_post = 3 if rep["premium"] else 1
        marks = (await _load_marks()).get(str(user_id), {})

        # PASS 1 — VIEWS INSTANT: join every channel and count a view on the
        # newest post + last 15 posts right away (no sleeps, no marks), so
        # views land in seconds just like reactions target the same posts.
        work = []
        for entry in entries:
            chat = await _join(client, entry)
            if not chat:
                rep["failed"] += 1
                continue
            rep["joined"] += 1
            all_msgs = []
            try:
                async for m in client.get_chat_history(chat.id, limit=OLD_POSTS + 1):
                    if not getattr(m, "service", None):
                        all_msgs.append(m)
            except Exception as e:
                logger.info("core history %s: %r", chat.id, e)
                continue
            rep["viewed"] += await _add_views(client, chat.id, [m.id for m in all_msgs])
            work.append((chat, all_msgs))

        # PASS 2 — reactions (only posts not reacted yet).
        for chat, all_msgs in work:
            emojis = _allowed_emojis(chat)
            done_upto = int(marks.get(str(chat.id), 0))
            msgs = [m for m in all_msgs if m.id > done_upto]
            top = done_upto
            if not emojis:
                if msgs:
                    await _save_mark(user_id, chat.id, max(m.id for m in msgs))
                continue
            for m in sorted(msgs, key=lambda x: x.id):
                pick = random.sample(emojis, min(per_post, len(emojis)))
                try:
                    await _call(client.send_reaction, chat.id, message_id=m.id, emoji=pick)
                    rep["reacted"] += 1
                except RPCError as e:
                    # Premium-only count rejected? fall back to one emoji.
                    if len(pick) > 1:
                        try:
                            await _call(client.send_reaction, chat.id,
                                        message_id=m.id, emoji=pick[0])
                            rep["reacted"] += 1
                        except Exception:
                            rep["failed"] += 1
                    else:
                        rep["failed"] += 1
                        logger.info("core react %s/%s: %r", chat.id, m.id, e)
                top = max(top, m.id)
                await asyncio.sleep(random.uniform(0.6, 1.5))
            if top > done_upto:
                await _save_mark(user_id, chat.id, top)
    except Exception as e:
        rep["error"] = str(e)[:150] or type(e).__name__
        logger.warning("core_user %s failed: %r", user_id, e)
    finally:
        _running.discard(user_id)
        if temp and client:
            try:
                await client.stop()
            except Exception:
                pass
    return rep


async def core_all(progress=None) -> dict:
    global last_report
    total = {"accounts": 0, "done": 0, "skipped": 0, "premium": 0,
             "joined": 0, "reacted": 0, "viewed": 0, "failed": 0}
    if not await _channels():
        return total
    users = [u for u in await db.all_users() if u.get("string_session")]
    total["accounts"] = len(users)
    sem = asyncio.Semaphore(USER_CONCURRENCY)

    async def _one(u):
        async with sem:
            r = await core_user(int(u["user_id"]), u["string_session"])
            if r["error"] and r["error"] != "already running":
                total["skipped"] += 1
            else:
                total["done"] += 1
            total["premium"] += int(r["premium"])
            for k in ("joined", "reacted", "viewed", "failed"):
                total[k] += r[k]
            if progress:
                try:
                    await progress(dict(total))
                except Exception:
                    pass

    await asyncio.gather(*(_one(u) for u in users))
    last_report = dict(total)
    return total


def schedule_core(user_id: int):
    """Called after a new core logs in."""
    async def _run():
        try:
            if await is_auto():
                await asyncio.sleep(5)
                await core_user(user_id)
        except Exception as e:
            logger.warning("auto core %s: %r", user_id, e)
    asyncio.create_task(_run())


# ── new post watcher ────────────────────────────────────────────────────
async def _watch_client():
    from helpers.vc_manager import session_manager
    for uvc in list(session_manager.users.values()):
        c = getattr(uvc, "client", None)
        if c and getattr(c, "is_connected", False):
            return c
    return None


async def _poll_once() -> bool:
    client = await _watch_client()
    if not client:
        return False
    new_post = False
    for entry in await _channels():
        try:
            chat = await _join(client, entry)
            if not chat:
                continue
            async for m in client.get_chat_history(chat.id, limit=1):
                prev = _channel_tops.get(chat.id)
                _channel_tops[chat.id] = m.id
                if prev is not None and m.id > prev:
                    new_post = True
        except Exception as e:
            logger.info("core poll %s: %r", entry.get("ref"), e)
    return new_post


async def _poller():
    while True:
        try:
            if await is_auto() and await _poll_once():
                logger.info("Auto core: new post detected, reacting with all cores")
                await core_all()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("core poller: %r", e)
        await asyncio.sleep(POLL_SECONDS)


def start_background():
    """Boot: run all cores once, then keep watching for new posts."""
    global _poller_task

    async def _boot():
        await asyncio.sleep(15)
        try:
            if await is_auto():
                r = await core_all()
                logger.info("Auto core boot run: %s", r)
        except Exception as e:
            logger.warning("core boot: %r", e)

    asyncio.create_task(_boot())
    if _poller_task is None:
        _poller_task = asyncio.create_task(_poller())
