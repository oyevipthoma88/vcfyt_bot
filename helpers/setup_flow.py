"""Setup model (v93) — ek hi saaf raasta.

  🔐 Login     = user ki APNI Telegram ID (number + OTP).  PRIMARY.
                 Isse sab khulta hai: ▶️ Player · 🎤 Live Mic · 🔗 Sync · 🎵 Library
  ⚔️ Fight ID  = OPTIONAL doosri ID jo Live Mic me bolti hai, taaki user ka
                 apna phone VC me bana rahe.  Login ke bina bhi Live Mic chala
                 sakti hai (Fight-ID-only mode).
  🎁 Free Trial = bina login, owner ki Sudo ID se kuch uses.

Rule: koi bhi ek "awaaz wali ID" (Login / Fight ID / Sudo) ho to Live Mic
ready hai — user se dobara login kabhi nahi maanga jaata.
"""

LOGIN_LABEL = "🔐 Login"
FIGHT_LABEL = "⚔️ Fight ID"
MAIN_LABEL = LOGIN_LABEL  # purane imports ke liye


def mic_setup_step(logged, spare, source, target) -> str:
    """Legacy (main-login) guided step: login -> tgt -> ready."""
    if not logged:
        return "login"
    if not target:
        return "tgt"
    return "ready"


def fight_step(fight: bool, target) -> str:
    """Fight-ID-only flow: fight -> tgt -> ready."""
    if not fight:
        return "fight"
    if not target:
        return "tgt"
    return "ready"


def speaker_label(main: bool, fight: bool, sudo_mode: bool) -> str:
    """Live Mic me awaaz kis ID se jayegi (user ko dikhane ke liye)."""
    if fight:
        return "⚔️ Fight ID"
    if main:
        return "🔐 Aapki ID"
    if sudo_mode:
        return "🎁 Sudo ID (trial)"
    return ""


async def has_main_id(uid) -> bool:
    """User ne apni ID se login kiya hai (ya owner ka env session)."""
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


async def account_state(uid) -> dict:
    """{main, fight, sudo, ready} — ek jagah se poora setup status."""
    main = await has_main_id(uid)
    fight = await has_fight_id(uid)
    sudo_mode = False
    if not main:
        try:
            from helpers import sudo as _sudo
            mode = await _sudo.engine_mode(int(uid))
            # TRIAL_OVER ko shared engine milta hai par use gated hai -> ready nahi
            sudo_mode = mode in (_sudo.SUDO, _sudo.PREMIUM, _sudo.TRIAL)
        except Exception:
            sudo_mode = False
    return {"main": main, "fight": fight, "sudo": sudo_mode,
            "ready": bool(main or fight or sudo_mode)}
