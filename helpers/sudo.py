"""Sudo system: owner-provided Telegram accounts that run VC for users who
have NOT logged in with their own account.

Who can use a sudo account (checked in this order):
  1. owner                       -> always (but uses own account anyway)
  2. user with own login         -> NOT sudo mode, uses own account
  3. owner-granted sudo (active) -> unlimited
  4. premium active              -> unlimited (5+ referrals, coupon, payment)
  5. free trial                  -> TRIAL_USES total uses (default 3), no login
  6. trial over / nothing        -> paywall: own login / refer 5 / support GC

Everything is stored in app_meta (db.get_app_value / set_app_value) as JSON,
so no schema migration is needed and it works on Mongo + SQLite.
"""
import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from config import Config
from helpers.database import db

logger = logging.getLogger("vcbot.sudo")

ACCOUNTS_KEY = "sudo_accounts"          # [{key, session, id, name, username, added}]
USERS_INDEX_KEY = "sudo_users_index"    # [uid, ...] (owner-granted sudo users)
SETTINGS_KEY = "sudo_settings"          # {trial_uses, support, support_id, ref_needed}

DEFAULT_TRIAL_USES = 3
DEFAULT_REF_NEEDED = 5
SUDO_OWNER_BASE = 10 ** 12              # synthetic owner_id for shared sudo engines
REQUEST_COOLDOWN = 3600                 # seconds between sudo requests after a reject

# Modes returned by engine_mode()
OWNER, SELF, SUDO, PREMIUM, TRIAL, TRIAL_OVER, NONE = (
    "owner", "self", "sudo", "premium", "trial", "trial_over", "none")
UNLIMITED_MODES = (SUDO, PREMIUM)
SUDO_ENGINE_MODES = (SUDO, PREMIUM, TRIAL, TRIAL_OVER)   # get a shared engine


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


async def _jget(key: str, default):
    try:
        raw = await db.get_app_value(key)
    except Exception:
        return default
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


async def _jset(key: str, value) -> None:
    await db.set_app_value(key, json.dumps(value))


# ── settings ────────────────────────────────────────────────────────────

async def settings() -> dict:
    s = await _jget(SETTINGS_KEY, {})
    s.setdefault("trial_uses", DEFAULT_TRIAL_USES)
    s.setdefault("ref_needed", DEFAULT_REF_NEEDED)
    s.setdefault("support", "")      # t.me link / @username shown to users
    s.setdefault("support_id", 0)    # numeric chat id used for join check
    return s


async def update_settings(**changes) -> dict:
    s = await settings()
    s.update(changes)
    s["trial_uses"] = max(0, min(50, int(s.get("trial_uses", DEFAULT_TRIAL_USES))))
    await _jset(SETTINGS_KEY, s)
    return s


def support_url(s: dict) -> str:
    ref = (s.get("support") or "").strip()
    if not ref:
        return ""
    if ref.startswith("@"):
        return f"https://t.me/{ref[1:]}"
    if ref.startswith("t.me/"):
        return "https://" + ref
    return ref if ref.startswith("http") else ""


# ── sudo accounts (pool) ────────────────────────────────────────────────

def account_key(session: str) -> str:
    return hashlib.sha256(session.encode()).hexdigest()[:12]


def account_owner_id(key: str) -> int:
    return SUDO_OWNER_BASE + int(key[:8], 16)


async def accounts() -> list:
    rows = await _jget(ACCOUNTS_KEY, [])
    return [r for r in rows if isinstance(r, dict) and r.get("session")]


async def add_account(session: str, info: dict = None) -> tuple[bool, str]:
    session = (session or "").strip()
    if len(session) < 100:
        return False, "String session bahut chhota hai — poora string paste karein."
    rows = await accounts()
    key = account_key(session)
    info = info or {}
    for r in rows:
        if r.get("key") == key or (info.get("id") and r.get("id") == info.get("id")):
            r.update(session=session, key=key, **{k: v for k, v in info.items() if v})
            await _jset(ACCOUNTS_KEY, rows)
            return True, "updated"
    rows.append({"key": key, "session": session, "id": info.get("id", 0),
                 "name": info.get("name", ""), "username": info.get("username", ""),
                 "added": _now().isoformat()})
    await _jset(ACCOUNTS_KEY, rows)
    return True, "added"


async def remove_account(key: str) -> bool:
    rows = await accounts()
    keep = [r for r in rows if r.get("key") != key]
    if len(keep) == len(rows):
        return False
    await _jset(ACCOUNTS_KEY, keep)
    await drop_engine(key)
    return True


# ── owner-granted sudo users ────────────────────────────────────────────

def _user_key(uid: int) -> str:
    return f"sudo_user_{int(uid)}"


async def grant_sudo(uid: int, days: int = 0, by: int = 0) -> str:
    """days=0 -> permanent. Extends an active grant instead of resetting it."""
    uid = int(uid)
    cur = await _jget(_user_key(uid), {})
    base = _now()
    old = _parse(cur.get("until"))
    if old and old > base:
        base = old
    until = "9999-12-31T23:59:59+00:00" if days <= 0 else (base + timedelta(days=days)).isoformat()
    await _jset(_user_key(uid), {"until": until, "by": by, "at": _now().isoformat()})
    idx = await _jget(USERS_INDEX_KEY, [])
    if uid not in idx:
        idx.append(uid)
        await _jset(USERS_INDEX_KEY, idx)
    _cache_clear(uid)
    return until


async def revoke_sudo(uid: int) -> bool:
    uid = int(uid)
    had = bool(await _jget(_user_key(uid), {}))
    try:
        await db.delete_app_value(_user_key(uid))
    except Exception:
        await _jset(_user_key(uid), {})
    idx = [x for x in await _jget(USERS_INDEX_KEY, []) if int(x) != uid]
    await _jset(USERS_INDEX_KEY, idx)
    _cache_clear(uid)
    return had


async def sudo_until(uid: int) -> Optional[str]:
    row = await _jget(_user_key(uid), {})
    until = _parse(row.get("until"))
    if until and until > _now():
        return row.get("until")
    return None


async def sudo_users() -> list:
    out = []
    for uid in await _jget(USERS_INDEX_KEY, []):
        until = await sudo_until(int(uid))
        if until:
            out.append((int(uid), until))
    return out


# ── free trial ──────────────────────────────────────────────────────────

def _trial_key(uid: int) -> str:
    return f"sudo_trial_{int(uid)}"


async def trial_state(uid: int) -> dict:
    t = await _jget(_trial_key(uid), {})
    s = await settings()
    limit = int(s["trial_uses"])
    used = int(t.get("used", 0) or 0)
    return {"started": bool(t.get("started")), "used": used, "limit": limit,
            "left": max(0, limit - used)}


async def start_trial(uid: int) -> dict:
    t = await _jget(_trial_key(uid), {})
    if not t.get("started"):
        t = {"started": _now().isoformat(), "used": int(t.get("used", 0) or 0)}
        await _jset(_trial_key(uid), t)
    _cache_clear(uid)
    return await trial_state(uid)


_trial_locks: dict = {}


async def consume_trial(uid: int) -> tuple[bool, dict]:
    """Atomically use 1 trial credit. Returns (allowed, state_after)."""
    lock = _trial_locks.setdefault(int(uid), asyncio.Lock())
    async with lock:
        t = await _jget(_trial_key(uid), {})
        st = await trial_state(uid)
        if not st["started"] or st["left"] <= 0:
            return False, st
        t["used"] = st["used"] + 1
        t.setdefault("started", _now().isoformat())
        await _jset(_trial_key(uid), t)
        _cache_clear(uid)
        return True, await trial_state(uid)


async def refund_trial(uid: int) -> None:
    """Give the credit back when the play/mic start itself failed."""
    lock = _trial_locks.setdefault(int(uid), asyncio.Lock())
    async with lock:
        t = await _jget(_trial_key(uid), {})
        if int(t.get("used", 0) or 0) > 0:
            t["used"] = int(t["used"]) - 1
            await _jset(_trial_key(uid), t)
    _cache_clear(uid)


async def reset_trial(uid: int) -> None:
    try:
        await db.delete_app_value(_trial_key(uid))
    except Exception:
        await _jset(_trial_key(uid), {})
    _cache_clear(uid)


# ── mode resolution ─────────────────────────────────────────────────────

_mode_cache: dict = {}
_MODE_TTL = 8.0


def _cache_clear(uid: int = None) -> None:
    if uid is None:
        _mode_cache.clear()
    else:
        _mode_cache.pop(int(uid), None)


async def has_own_session(uid: int) -> bool:
    if Config.is_owner(uid) and Config.STRING_SESSION:
        return True
    for key in (uid, str(uid)):
        try:
            data = await db.get_user(key)
        except Exception:
            data = None
        if data and (data.get("string_session") or "").strip():
            return True
    return False


async def engine_mode(uid: int, fresh: bool = False) -> str:
    uid = int(uid)
    hit = _mode_cache.get(uid)
    if hit and not fresh and time.monotonic() - hit[1] < _MODE_TTL:
        return hit[0]
    mode = await _resolve_mode(uid)
    _mode_cache[uid] = (mode, time.monotonic())
    return mode


async def _resolve_mode(uid: int) -> str:
    if Config.is_owner(uid):
        return OWNER
    if await has_own_session(uid):
        return SELF
    if await sudo_until(uid):
        return SUDO
    try:
        from helpers.access_control import is_premium
        if await is_premium(uid):
            return PREMIUM
    except Exception:
        pass
    t = await trial_state(uid)
    if t["started"]:
        return TRIAL if t["left"] > 0 else TRIAL_OVER
    return NONE


async def is_sudo_mode(uid: int) -> bool:
    return await engine_mode(uid) in SUDO_ENGINE_MODES


# ── verified group per user ─────────────────────────────────────────────

async def get_gc(uid: int) -> tuple[int, str, str]:
    row = await _jget(f"sudo_gc_{int(uid)}", {})
    try:
        return int(row.get("id") or 0), row.get("ref", ""), row.get("title", "")
    except (TypeError, ValueError):
        return 0, "", ""


async def save_gc(uid: int, cid: int, ref: str, title: str, acct_key: str) -> None:
    await _jset(f"sudo_gc_{int(uid)}", {"id": int(cid), "ref": ref or "", "title": title or "",
                                        "acct": acct_key, "at": _now().isoformat()})


# ── sudo requests (support GC flow) ─────────────────────────────────────

async def get_request(uid: int) -> dict:
    return await _jget(f"sudo_req_{int(uid)}", {})


async def set_request(uid: int, status: str, **extra) -> None:
    row = {"status": status, "at": time.time(), **extra}
    await _jset(f"sudo_req_{int(uid)}", row)


async def request_block_reason(uid: int) -> str:
    row = await get_request(uid)
    if row.get("status") == "pending":
        return "pending"
    if row.get("status") == "rejected" and time.time() - float(row.get("at", 0)) < REQUEST_COOLDOWN:
        return "cooldown"
    return ""


# ── shared engines (one UserVC per sudo account) ────────────────────────

_engines: dict = {}          # key -> UserVC
_engine_lock = asyncio.Lock()
_dead: set = set()


def busy_chats(key: str) -> set:
    """Groups where this sudo account is busy right now (play, mic, live mic).

    Telegram/py-tgcalls: one account = one active group call (relay code
    calls release_other_chats). So one Sudo ID serves ONE group at a time;
    owner adds more Sudo IDs for more parallel users."""
    eng = _engines.get(key)
    out = set()
    if eng is None:
        return out
    for cid, st in list((getattr(eng, "chats", {}) or {}).items()):
        if getattr(st, "is_playing", False) or getattr(st, "mic_enabled", False) \
                or getattr(st, "ss_on", False):
            out.add(int(cid))
    try:
        from helpers import live_mic
        for sess in list(live_mic._sessions.values()):
            if getattr(sess, "relay", None) is eng or getattr(sess, "uvc", None) is eng:
                out.add(int(getattr(sess, "chat_id", 0) or 0))
    except Exception:
        pass
    out.discard(0)
    return out


def _free_for(key: str, gc_id: int) -> bool:
    busy = busy_chats(key)
    if not busy:
        return True
    return bool(gc_id) and busy <= {int(gc_id)}


async def _assigned_key(uid: int, rows: list) -> Optional[str]:
    keys = [r["key"] for r in rows if r["key"] not in _dead]
    if not keys:
        return None
    gc = await _jget(f"sudo_gc_{int(uid)}", {})
    gc_id = int(gc.get("id") or 0)
    # 1) jo ID pehle se is user ke group me chal rahi hai / assigned hai
    for k in (gc.get("acct"), await _jget(f"sudo_assign_{int(uid)}", "")):
        if k in keys and _free_for(k, gc_id):
            return k
    # 2) koi bhi free ID (sabse kam load wali)
    free = [k for k in keys if _free_for(k, gc_id)]
    if not free:
        return BUSY
    pick = min(free, key=lambda k: len(getattr(_engines.get(k), "chats", {}) or {}))
    await _jset(f"sudo_assign_{int(uid)}", pick)
    return pick


BUSY = "__busy__"


async def all_busy(uid: int) -> bool:
    rows = await accounts()
    return bool(rows) and (await _assigned_key(uid, rows)) == BUSY


async def engine_for(uid: int):
    """Shared sudo UserVC for this user (None when no account is free/working)."""
    rows = await accounts()
    for _ in range(max(1, len(rows))):
        key = await _assigned_key(uid, rows)
        if not key or key == BUSY:
            return None
        eng = await _start_engine(key, rows)
        if eng is not None:
            return eng
        await _jset(f"sudo_assign_{int(uid)}", "")
        rows = await accounts()
    return None


# ── connection health ───────────────────────────────────────────────────
#
# ROOT CAUSE ("❌ Group join/verify fail: Connection lost"):
# Sudo IDs ek baar start hokar ghanton idle padi rehti thi. Server/Telegram
# beech me TCP socket band kar deta hai, lekin pyrogram ka
# ``client.is_connected`` flag True hi rehta hai (wo sirf connect()/
# disconnect() par badalta hai). _start_engine sirf ye flag dekh kar wahi
# mari hui engine wapas de deta tha -> join_chat() ka har retry
# ``OSError(ConnectionResetError('Connection lost'))`` deta tha aur user ko
# raw error dikhta tha. Ab reuse se pehle engine ko ek chhota real request
# (get_me) bhej kar check karte hain; jawab na aaye to engine poori tarah
# restart hoti hai, aur verify ek connection error par fresh engine ke
# saath dobara try karta hai.

HEALTH_TTL = 45.0          # seconds a successful probe stays trusted
PROBE_TIMEOUT = 10.0
STOP_TIMEOUT = 15.0

_CONN_MARKERS = ("connection lost", "connection reset", "broken pipe", "not connected",
                 "has not been started", "timed out", "connection closed",
                 "connection aborted", "eof occurred", "network is unreachable")


def is_conn_error(error: BaseException) -> bool:
    """True for transport/connection failures (not Telegram RPC errors)."""
    if error is None:
        return False
    if isinstance(error, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return True
    name = type(error).__name__
    if name in ("MTProtoClientNotConnected", "ClientNotConnected"):
        return True
    text = str(error).lower()
    if isinstance(error, OSError):
        return True
    return any(m in text for m in _CONN_MARKERS)


async def probe(eng, timeout: float = PROBE_TIMEOUT) -> bool:
    """Real round-trip to Telegram. ``is_connected`` alone lies."""
    client = getattr(eng, "client", None)
    if client is None or not getattr(client, "is_connected", False):
        return False
    try:
        await asyncio.wait_for(client.get_me(), timeout)
    except Exception as exc:
        logger.warning("sudo engine %s probe failed: %r", getattr(eng, "label", "?"), exc)
        return False
    eng._sudo_ok_at = time.monotonic()
    return True


def mark_healthy(eng) -> None:
    try:
        eng._sudo_ok_at = time.monotonic()
    except Exception:
        pass


async def _stop_quiet(eng) -> None:
    try:
        await asyncio.wait_for(eng.stop(), STOP_TIMEOUT)
    except Exception:
        pass


async def _start_engine(key: str, rows: list):
    from helpers.vc_manager import UserVC, _is_invalid_session
    async with _engine_lock:
        eng = _engines.get(key)
        if eng and eng.client is not None and getattr(eng.client, "is_connected", False):
            fresh = time.monotonic() - float(getattr(eng, "_sudo_ok_at", 0) or 0) < HEALTH_TTL
            if fresh or await probe(eng):
                eng.touch()
                return eng
            logger.warning("sudo engine %s socket dead — restarting", key)
        if eng:
            await _stop_quiet(eng)
            _engines.pop(key, None)
        row = next((r for r in rows if r.get("key") == key), None)
        if not row:
            return None
        exc = None
        for attempt in range(2):
            eng = UserVC(account_owner_id(key), row["session"], label=f"sudo{key[:6]}")
            try:
                await eng.start()
                exc = None
                break
            except Exception as e:
                exc = e
                await _stop_quiet(eng)
                # Network blip par turant 2 min ke liye "dead" mat karo.
                if attempt == 0 and is_conn_error(e) and not _is_invalid_session(e):
                    await asyncio.sleep(2)
                    continue
                break
        if exc is not None:
            if _is_invalid_session(exc):
                await mark_dead(key, exc)
            else:
                logger.warning("sudo engine %s start failed: %r", key, exc)
                _dead.add(key)
                asyncio.get_event_loop().call_later(120, _dead.discard, key)
            return None
        mark_healthy(eng)
        _engines[key] = eng
        if (row.get("id") or 0) != eng.account_id:
            row.update(id=eng.account_id, name=eng.account_name, username=eng.account_username)
            await _jset(ACCOUNTS_KEY, rows)
        return eng


async def mark_dead(key: str, error: Exception = None) -> None:
    """Session revoked by Telegram: drop it and tell the owner."""
    rows = await accounts()
    row = next((r for r in rows if r.get("key") == key), {})
    await _jset(ACCOUNTS_KEY, [r for r in rows if r.get("key") != key])
    await drop_engine(key)
    logger.warning("sudo account %s expired: %r", key, error)
    try:
        from helpers.logger_channel import get_bot
        bot = get_bot()
        if bot:
            name = row.get("name") or row.get("username") or key
            await bot.send_message(Config.primary_owner(),
                                   f"⚠️ <b>Sudo ID expire ho gayi</b>\n{name} — pool se hata di. "
                                   "Owner Panel → 👑 Sudo se nayi ID add karein.")
    except Exception:
        pass


async def drop_engine(key: str) -> None:
    eng = _engines.pop(key, None)
    if eng:
        await _stop_quiet(eng)


def engine_key(eng) -> str:
    return account_key(getattr(eng, "string_session", "") or "")


def engine_is_sudo(uvc) -> bool:
    return str(getattr(uvc, "label", "")).startswith("sudo")


def live_engines() -> dict:
    return dict(_engines)
