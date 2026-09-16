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
    """Return access gate result.

    Returns:
        {"allowed": bool, "reason": str, "usage_today": int, "limit": int,
         "is_premium": bool, "is_new_user": bool}
    """
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


async def redeem_coupon(user_id: int, code: str) -> dict:
    """Redeem a coupon code. Returns {"ok": bool, "message": str, "plan": str}."""
    coupon = await db.use_coupon(code)
    if not coupon:
        return {"ok": False, "message": "Invalid ya already used coupon.", "plan": None}
    plan_id = coupon.get("plan_id", "")
    try:
        until_iso = await grant_premium(user_id, plan_id)
    except ValueError:
        return {"ok": False, "message": f"Unknown plan: {plan_id}", "plan": plan_id}
    label = next((p[1] for p in PAYMENT_PLANS if p[0] == plan_id), plan_id)
    return {"ok": True, "message": f"✅ Premium mil gaya! Plan: {label} Until: {until_iso}",
            "plan": plan_id}


async def get_qr_code() -> Optional[str]:
    return await db.get_app_value(QR_CODE_KEY)


async def set_qr_code(file_id: str):
    await db.set_app_value(QR_CODE_KEY, file_id)


async def check_referral_milestones(user_id: int) -> list:
    """Check and auto-claim any newly reached referral milestones.

    Returns list of (milestone, hours) tuples that were claimed.
    """
    count = await db.count_referrals(user_id)
    claimed = []
    for milestone, hours in REFERRAL_REWARDS:
        if count >= milestone and not await db.referral_milestone_claimed(user_id, milestone):
            await db.claim_referral_milestone(user_id, milestone)
            await grant_premium_hours(user_id, hours)
            claimed.append((milestone, hours))
    return claimed
