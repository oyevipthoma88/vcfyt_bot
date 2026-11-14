"""Access control: premium status, daily usage limits, referral rewards.

Free users get 3 uses/day; new users (first day) get 5.
After limit is hit, show payment options.
Owners always bypass.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from config import Config
from helpers.database import db

logger = logging.getLogger("vcbot.access")

DAILY_LIMIT = 3
NEW_USER_DAILY_LIMIT = 5
NEW_USER_GRACE_HOURS = 24

# (milestone, hours rewarded)
REFERRAL_REWARDS = [
    (7, 5),
    (15, 10),
    (25, 24),
]

PAYMENT_PLANS = [
    ("1day", "1 Day", 1, "day"),
    ("7day", "7 Days", 7, "day"),
    ("1month", "1 Month", 1, "month"),
    ("3month", "3 Months", 3, "month"),
    ("1year", "1 Year", 1, "year"),
    ("lifetime", "Lifetime", 0, "lifetime"),
]

PAYMENT_CONFIG_KEY = "payment_config"
QR_CODE_KEY = "qr_code_file_id"

PAYMENT_CONTACT_URL = getattr(Config, "PAYMENT_CONTACT_URL", "")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today_str() -> str:
    return _now().strftime("%Y-%m-%d")


def _parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _add_duration(unit: str, amount: int) -> str:
    now = _now()
    if unit == "day":
        until = now + timedelta(days=amount)
    elif unit == "month":
        until = now + timedelta(days=30 * amount)
    elif unit == "year":
        until = now + timedelta(days=365 * amount)
    else:
        return "9999-12-31T23:59:59+00:00"
    return until.isoformat()


async def get_payment_config() -> dict:
    raw = await db.get_app_value(PAYMENT_CONFIG_KEY)
    if raw:
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            pass
    return {}


async def set_payment_config(config: dict):
    await db.set_app_value(PAYMENT_CONFIG_KEY, json.dumps(config))


async def get_plan_price(plan_id: str) -> Optional[str]:
    config = await get_payment_config()
    entry = config.get(plan_id)
    if entry:
        return str(entry.get("price", ""))
    return None


async def is_premium(user_id: int) -> bool:
    if Config.is_owner(user_id):
        return True
    row = await db.get_premium(user_id)
    if row:
        until = _parse_iso(row.get("premium_until"))
        if until and until > _now():
            return True
    linked_ids = await db.get_linked_user_ids(user_id)
    for uid in linked_ids:
        if uid == user_id:
            continue
        row = await db.get_premium(uid)
        if row:
            until = _parse_iso(row.get("premium_until"))
            if until and until > _now():
                return True
    return False


async def premium_info(user_id: int) -> dict:
    if Config.is_owner(user_id):
        return {"active": True, "until": None, "plan": "owner"}
    row = await db.get_premium(user_id)
    if row:
        until = _parse_iso(row.get("premium_until"))
        if until and until > _now():
            return {"active": True, "until": row.get("premium_until"), "plan": row.get("plan")}
    linked_ids = await db.get_linked_user_ids(user_id)
    for uid in linked_ids:
        if uid == user_id:
            continue
        row = await db.get_premium(uid)
        if row:
            until = _parse_iso(row.get("premium_until"))
            if until and until > _now():
                return {"active": True, "until": row.get("premium_until"), "plan": row.get("plan")}
    return {"active": False, "until": None, "plan": None}


async def grant_premium(user_id: int, plan_id: str):
    plan = next((p for p in PAYMENT_PLANS if p[0] == plan_id), None)
    if not plan:
        raise ValueError(f"Unknown plan: {plan_id}")
    _, _, amount, unit = plan
    existing = await db.get_premium(user_id)
    base = _now()
    if existing:
        parsed = _parse_iso(existing.get("premium_until"))
        if parsed and parsed > base:
            base = parsed
    if unit == "day":
        until = base + timedelta(days=amount)
    elif unit == "month":
        until = base + timedelta(days=30 * amount)
    elif unit == "year":
        until = base + timedelta(days=365 * amount)
    else:
        until = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    until_iso = until.isoformat()
    await db.set_premium(user_id, until_iso, plan_id)
    for uid in await db.get_linked_user_ids(user_id):
        if uid != user_id:
            await db.set_premium(uid, until_iso, plan_id)
    return until_iso


async def grant_premium_hours(user_id: int, hours: int):
    existing = await db.get_premium(user_id)
    base = _now()
    if existing:
        parsed = _parse_iso(existing.get("premium_until"))
        if parsed and parsed > base:
            base = parsed
    until = base + timedelta(hours=hours)
    until_iso = until.isoformat()
    await db.set_premium(user_id, until_iso, "referral")
    for uid in await db.get_linked_user_ids(user_id):
        if uid != user_id:
            await db.set_premium(uid, until_iso, "referral")
    return until_iso


async def _is_new_user(user_id: int) -> bool:
    user = await db.get_user(user_id)
    if not user:
        return True
    joined = _parse_iso(user.get("joined_at"))
    if not joined:
        return True
    return (_now() - joined) < timedelta(hours=NEW_USER_GRACE_HOURS)


async def check_access(user_id: int) -> dict:
    """Return access gate result."""
    if Config.is_owner(user_id):
        return {"allowed": True, "reason": "owner", "usage_today": 0,
                "limit": -1, "is_premium": True, "is_new_user": False}

    if await is_premium(user_id):
        return {"allowed": True, "reason": "premium", "usage_today": 0,
                "limit": -1, "is_premium": True, "is_new_user": False}

    new_user = await _is_new_user(user_id)
    limit = NEW_USER_DAILY_LIMIT if new_user else DAILY_LIMIT
    usage = await db.get_usage(user_id, _today_str())

    if usage >= limit:
        return {"allowed": False, "reason": "limit_reached",
                "usage_today": usage, "limit": limit,
                "is_premium": False, "is_new_user": new_user}

    return {"allowed": True, "reason": "free_quota",
            "usage_today": usage, "limit": limit,
            "is_premium": False, "is_new_user": new_user}


async def record_usage(user_id: int) -> int:
    if Config.is_owner(user_id):
        return 0
    if await is_premium(user_id):
        return 0
    return await db.increment_usage(user_id, _today_str())

async def revoke_premium_all(user_id: int) -> bool:
    ok = await db.revoke_premium(user_id)
    for uid in await db.get_linked_user_ids(user_id):
        if uid != user_id:
            await db.revoke_premium(uid)
    return ok


# ── Coupon redeem ─────────────────────────────────────────────────────
# Rules (v84 root fix):
#   1. Ek user (aur uske same-session linked IDs) ek code sirf EK baar.
#   2. Total uses kabhi max_uses se zyada nahi (atomic counter).
#   3. Ek hi code par parallel taps ek-ek karke process hote hain (lock).
#   4. DB write fail ho to redeem refuse - kabhi "free" use nahi.
#   5. Galat code ki guessing: 5 galat try / 10 min ke baad thodi der block.

import asyncio as _asyncio
import time as _time

_REDEEM_LOCKS: dict = {}
_REDEEM_FAILS: dict = {}
REDEEM_MAX_FAILS = 5
REDEEM_FAIL_WINDOW = 600  # seconds


def _redeem_lock(code: str) -> "_asyncio.Lock":
    lock = _REDEEM_LOCKS.get(code)
    if lock is None:
        lock = _REDEEM_LOCKS[code] = _asyncio.Lock()
    return lock


def _fail_wait(user_id: int) -> int:
    """Seconds the user must wait before trying another code (0 = allowed)."""
    now = _time.time()
    hits = [t for t in _REDEEM_FAILS.get(user_id, []) if now - t < REDEEM_FAIL_WINDOW]
    _REDEEM_FAILS[user_id] = hits
    if len(hits) >= REDEEM_MAX_FAILS:
        return int(REDEEM_FAIL_WINDOW - (now - hits[0])) + 1
    return 0


def _note_fail(user_id: int) -> None:
    _REDEEM_FAILS.setdefault(user_id, []).append(_time.time())


def plan_label(plan_id: str) -> str:
    return next((p[1] for p in PAYMENT_PLANS if p[0] == plan_id), plan_id or "—")


def format_until(until_iso) -> str:
    """'2026-11-21T09:54:00+00:00' -> '21 Nov 2026, 03:24 PM IST'. Lifetime -> 'Lifetime'."""
    dt = _parse_iso(str(until_iso or ""))
    if not dt:
        return "—"
    if dt.year >= 9999:
        return "Lifetime ♾️"
    ist = dt.astimezone(timezone(timedelta(hours=5, minutes=30)))
    return ist.strftime("%d %b %Y, %I:%M %p IST")


def time_left(until_iso) -> str:
    dt = _parse_iso(str(until_iso or ""))
    if not dt:
        return ""
    if dt.year >= 9999:
        return "kabhi khatam nahi hoga"
    secs = int((dt - _now()).total_seconds())
    if secs <= 0:
        return "expired"
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d} din {h} ghante"
    if h:
        return f"{h} ghante {m} min"
    return f"{max(1, m)} min"


def _fail(message: str, reason: str, plan=None) -> dict:
    return {"ok": False, "reason": reason, "message": message, "plan": plan, "until": None}


async def redeem_coupon(user_id: int, code: str, username: str = "", name: str = "") -> dict:
    code = (code or "").strip().upper()
    if not code or len(code) > 64:
        return _fail("Coupon code likhein.", "empty")

    wait = _fail_wait(user_id)
    if wait:
        return _fail(f"Bahut saare galat try ho gaye. {max(1, wait // 60)} min baad try karein.",
                     "rate_limited")

    async with _redeem_lock(code):
        existing = await db.get_coupon(code)
        if not existing:
            _note_fail(user_id)
            return _fail("Yeh coupon code galat hai. Spelling check karke dobara bhejein.",
                         "invalid")
        plan_id = existing.get("plan_id", "")
        if not any(p[0] == plan_id for p in PAYMENT_PLANS):
            return _fail(f"Yeh coupon ek purane plan ka hai ({plan_id}). Owner se naya code lein.",
                         "bad_plan", plan_id)

        try:
            linked = await db.get_linked_user_ids(user_id)
        except Exception:
            linked = []
        ids = {int(user_id), *[int(x) for x in (linked or [])]}
        try:
            if await db.coupon_claimed_by_any(code, ids):
                return _fail("Aap yeh coupon pehle hi redeem kar chuke ho. "
                             "Ek code ek user sirf ek baar use kar sakta hai.",
                             "already", plan_id)
        except Exception:
            return _fail("Server busy hai, 1 min baad try karein.", "error", plan_id)

        status, coupon = await db.claim_coupon(code, user_id)
        if status == db.COUPON_ALREADY:
            return _fail("Aap yeh coupon pehle hi redeem kar chuke ho. "
                         "Ek code ek user sirf ek baar use kar sakta hai.", "already", plan_id)
        if status == db.COUPON_EXHAUSTED:
            return _fail("Yeh coupon ki saari uses khatam ho chuki hain.", "exhausted", plan_id)
        if status == db.COUPON_MISSING:
            _note_fail(user_id)
            return _fail("Yeh coupon code galat hai. Spelling check karke dobara bhejein.",
                         "invalid")
        if status != db.COUPON_OK:
            return _fail("Server busy hai, 1 min baad try karein.", "error", plan_id)

        plan_id = (coupon or {}).get("plan_id", plan_id)
        try:
            until_iso = await grant_premium(user_id, plan_id)
        except Exception as exc:
            logger.warning("grant after coupon %s failed for %s: %r", code, user_id, exc)
            await db.release_coupon_claim(code, user_id)
            return _fail("Premium activate nahi ho paya, coupon wapas kar diya. "
                         "Thodi der baad try karein.", "error", plan_id)

    try:
        await db.log_redeem(code, plan_id, user_id, username, name)
    except Exception:
        pass
    _REDEEM_FAILS.pop(user_id, None)
    return {"ok": True, "reason": "ok", "plan": plan_id, "until": until_iso,
            "message": f"✅ Premium mil gaya! Plan: {plan_label(plan_id)} · "
                       f"Valid till: {format_until(until_iso)}"}


async def get_qr_code() -> Optional[str]:
    return await db.get_app_value(QR_CODE_KEY)


async def set_qr_code(file_id: str):
    await db.set_app_value(QR_CODE_KEY, file_id)


async def check_referral_milestones(user_id: int) -> list:
    count = await db.count_referrals(user_id)
    claimed = []
    for milestone, hours in REFERRAL_REWARDS:
        if count >= milestone and not await db.referral_milestone_claimed(user_id, milestone):
            await db.claim_referral_milestone(user_id, milestone)
            await grant_premium_hours(user_id, hours)
            claimed.append((milestone, hours))
    return claimed


MULTI_SPARE_KEY = "feature_multi_spare_premium"


async def multi_spare_premium_only() -> bool:
    try:
        return str(await db.get_app_value(MULTI_SPARE_KEY) or "0") == "1"
    except Exception:
        return False


async def set_multi_spare_premium_only(on: bool) -> None:
    await db.set_app_value(MULTI_SPARE_KEY, "1" if on else "0")


async def can_multi_spare(user_id: int) -> bool:
    if not await multi_spare_premium_only():
        return True
    if Config.is_owner(user_id):
        return True
    return await is_premium(user_id)
