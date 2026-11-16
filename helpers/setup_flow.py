"""Guided setup steps for browser-based Live Mic.

Simple model (v92):
  ⚔️ Fight ID  = wo Telegram ID jo VC me bolti hai  -> ZAROORI (Live Mic ke liye)
  👤 Main ID   = user ki apni ID                    -> OPTIONAL (Player/Sync/Library)
Live Mic sirf Fight ID + private browser link se chal jata hai.
"""

FIGHT_LABEL = "⚔️ Fight ID"
MAIN_LABEL = "👤 Main ID"


def mic_setup_step(logged, spare, source, target) -> str:
    if not logged:
        return "login"
    if not target:
        return "tgt"
    return "ready"


def fight_step(fight: bool, target) -> str:
    """Next step for the simple Live Mic flow: fight -> tgt -> ready."""
    if not fight:
        return "fight"
    if not target:
        return "tgt"
    return "ready"


async def has_main_id(uid) -> bool:
    """User ne apni Main ID se login kiya hai (ya owner ka env session)."""
    try:
        from helpers.sudo import has_own_session
        return bool(await has_own_session(int(uid)))
    except Exception:
        return False


async def has_fight_id(uid) -> bool:
    """User ne apni khud ki Fight ID jodi hai? (global/Sudo fallback nahi gina jata)"""
    try:
        from helpers.database import db
        from helpers.vc_manager import session_manager
        raw = await db.get_app_value(session_manager.spare_key(int(uid), 1))
        return bool((raw or "").strip())
    except Exception:
        return False
