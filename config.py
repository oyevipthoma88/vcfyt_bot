import os

def _int(name: str, default: int = 0) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default

def _int_value(raw: str) -> int:
    try:
        return int((raw or "").strip())
    except (TypeError, ValueError):
        return 0

def _bool(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")

def _norm_url(raw: str) -> str:
    """Normalize a base URL: add https:// when missing, drop trailing slash."""
    raw = (raw or "").strip().rstrip("/")
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    return raw

def _s(name: str, default: str = "") -> str:
    v = os.environ.get(name, "")
    return v if v else default

class Config:
    API_ID: int = _int("API_ID")
    API_HASH: str = os.environ.get("API_HASH", "")
    BOT_TOKEN: str = _s("BOT_TOKEN", "")

    OWNER_ID: int = _int("OWNER_ID", 0)
    OWNER_IDS: tuple[int, ...] = tuple(sorted({
        value
        for raw in os.environ.get("OWNER_IDS", "").replace(";", ",").split(",")
        for value in (_int_value(raw),)
        if value
    } | ({OWNER_ID} if OWNER_ID else set())))

    @classmethod
    def is_owner(cls, user_id: int) -> bool:
        return bool(user_id and int(user_id) in cls.OWNER_IDS)

    @classmethod
    def primary_owner(cls) -> int:
        return cls.OWNER_ID or (cls.OWNER_IDS[0] if cls.OWNER_IDS else 0)

    LOG_CHANNEL: int = _int("LOG_CHANNEL", 0)
    AUDIO_ARCHIVE_CHANNEL: int = _int("AUDIO_ARCHIVE_CHANNEL", -1004486549326)

    STRING_SESSION: str = os.environ.get("STRING_SESSION", "")
    # Spare ("assistant") account used ONLY to push live-mic audio into the VC.
    # Telegram allows one group-call join per account, so relaying with the
    # same account the user listens with kicks them out of the voice chat.
    ASSISTANT_SESSION: str = os.environ.get("ASSISTANT_SESSION", "")
    SESSION_BOT_USERNAME: str = os.environ.get("SESSION_BOT_USERNAME", "Session_generator_1bot")
    SESSION_BOT_LINK: str = f"https://t.me/{SESSION_BOT_USERNAME}"

    DEFAULT_VOLUME: int = _int("DEFAULT_VOLUME", 1000)
    DEFAULT_BASS: int = _int("DEFAULT_BASS", 10)
    DEFAULT_ECHO: bool = _bool("DEFAULT_ECHO", False)
    DEFAULT_ECHO_LEVEL: int = _int("DEFAULT_ECHO_LEVEL", 2)
    DEFAULT_BOOST: int = _int("DEFAULT_BOOST", 10)

    RELAY_DEFAULT_VOLUME: int = _int("RELAY_DEFAULT_VOLUME", 1000)
    RELAY_DEFAULT_GAIN: int = _int("RELAY_DEFAULT_GAIN", 300)
    RELAY_DEFAULT_BASS: int = _int("RELAY_DEFAULT_BASS", 14)
    RELAY_DEFAULT_TREBLE: int = _int("RELAY_DEFAULT_TREBLE", 100)

    EXTRA_GAIN_DB: int = _int("EXTRA_GAIN_DB", 60)
    # Final make-up gain (dB) applied on top of every chain, 0-24.
    LOUD_EXTRA_DB: int = _int("LOUD_EXTRA_DB", 18)

    # --- instant playback (Bug 3) ---
    # Stream straight through FFmpeg instead of pre-rendering a WAV first:
    # audio starts in ~0 s instead of waiting for the whole file to process.
    FAST_PLAY: bool = _bool("FAST_PLAY", True)

    # --- admin mute / unmute behaviour (Bug 2) ---
    HAND_RAISE_DEFAULT: bool = _bool("HAND_RAISE_DEFAULT", True)
    MIC_BLINK_DEFAULT: bool = _bool("MIC_BLINK_DEFAULT", True)
    MIC_BLINK_SECONDS: int = _int("MIC_BLINK_SECONDS", 5)

    # --- fake screen share (Bug 3b) ---
    SCREEN_SHARE_ENABLED: bool = _bool("SCREEN_SHARE_ENABLED", True)
    SS_WIDTH: int = _int("SS_WIDTH", 854)
    SS_HEIGHT: int = _int("SS_HEIGHT", 480)
    SS_FPS: int = _int("SS_FPS", 15)

    LIVE_BOOST_DEFAULT: int = _int("LIVE_BOOST_DEFAULT", 20000)
    AUTO_LIVE_BOOST: bool = _bool("AUTO_LIVE_BOOST", True)
    AUTO_MODE_DEFAULT: bool = _bool("AUTO_MODE_DEFAULT", False)
    KEEPER_INTERVAL: int = _int("KEEPER_INTERVAL", 15)

    # Memory: every restored session is a full Pyrogram client + PyTgCalls
    # instance (~25-40 MB each).  Restoring dozens of them at boot is what
    # pushed the dyno past its 1 GB quota (Heroku R14).  Sessions now load
    # on demand and idle ones are evicted quickly.
    MAX_ACTIVE_SESSIONS: int = _int("MAX_ACTIVE_SESSIONS", 6)
    SESSION_IDLE_TIMEOUT: int = _int("SESSION_IDLE_TIMEOUT", 600)
    SESSION_EVICT_INTERVAL: int = _int("SESSION_EVICT_INTERVAL", 120)
    # Restore every stored session at boot (memory hungry — off by default).
    RESTORE_ALL_SESSIONS: bool = _bool("RESTORE_ALL_SESSIONS", False)
    # RSS watchdog (MB).  Soft = evict idle sessions + gc, hard = drop the
    # least recently used sessions even if they are only parked.
    MEMORY_SOFT_MB: int = _int("MEMORY_SOFT_MB", 650)
    MEMORY_HARD_MB: int = _int("MEMORY_HARD_MB", 820)
    MEMORY_CHECK_INTERVAL: int = _int("MEMORY_CHECK_INTERVAL", 30)

    MONGO_URI: str = os.environ.get("MONGO_URI", "")

    _HEROKU_APP_NAME: str = os.environ.get("HEROKU_APP_NAME", "").strip()
    _HEROKU_DEFAULT_DOMAIN: str = os.environ.get("HEROKU_APP_DEFAULT_DOMAIN_NAME", "").strip()
    _LIVE_MIC_OVERRIDE: str = os.environ.get("LIVE_MIC_BASE_URL", "").strip()
    # Priority order matters: an explicitly configured URL always wins.
    # Modern Heroku apps do NOT live on "<app-name>.herokuapp.com" — the real
    # domain carries a random suffix (e.g. my-app-1a2b3c4d5e6f.herokuapp.com),
    # so guessing from HEROKU_APP_NAME is only a last-resort fallback.
    LIVE_MIC_BASE_URL: str = (
        _norm_url(_LIVE_MIC_OVERRIDE)
        or _norm_url(_HEROKU_DEFAULT_DOMAIN)
        or (f"https://{_HEROKU_APP_NAME}.herokuapp.com" if _HEROKU_APP_NAME else "")
    )
    # When False (default) the bot NEVER creates/ends the group voice chat
    # for live mic — it only joins an already running VC.
    LIVE_MIC_AUTO_START_VC: bool = _bool("LIVE_MIC_AUTO_START_VC", False)
    START_PIC: str = os.environ.get("START_PIC", "").strip()
    SOURCE_CODE_URL: str = os.environ.get("SOURCE_CODE_URL", "").strip()
    PAYMENT_CONTACT_URL: str = os.environ.get("PAYMENT_CONTACT_URL", "").strip()
