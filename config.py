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
    } | ({OWNER_ID} if OWNER_ID else set()}))

    @classmethod
    def is_owner(cls, user_id: int) -> bool:
        return bool(user_id and int(user_id) in cls.OWNER_IDS)

    @classmethod
    def primary_owner(cls) -> int:
        return cls.OWNER_ID or (cls.OWNER_IDS[0] if cls.OWNER_IDS else 0)

    LOG_CHANNEL: int = _int("LOG_CHANNEL", 0)
    AUDIO_ARCHIVE_CHANNEL: int = _int("AUDIO_ARCHIVE_CHANNEL", -1004486549326)

    STRING_SESSION: str = os.environ.get("STRING_SESSION", "")
    ASSISTANT_SESSION: str = os.environ.get("ASSISTANT_SESSION", "")
    SESSION_BOT_USERNAME: str = os.environ.get("SESSION_BOT_USERNAME", "Session_generator_1bot")
    SESSION_BOT_LINK: str = f"https://t.me/{SESSION_BOT_USERNAME}"

    DEFAULT_VOLUME: int = max(0, min(3000, _int("DEFAULT_VOLUME", 3000)))
    DEFAULT_BASS: int = _int("DEFAULT_BASS", 14)
    DEFAULT_ECHO: bool = _bool("DEFAULT_ECHO", False)
    DEFAULT_ECHO_LEVEL: int = _int("DEFAULT_ECHO_LEVEL", 2)
    DEFAULT_BOOST: int = _int("DEFAULT_BOOST", 10)

    RELAY_DEFAULT_VOLUME: int = max(0, min(3000, _int("RELAY_DEFAULT_VOLUME", 3000)))
    RELAY_DEFAULT_GAIN: int = max(0, min(500, _int("RELAY_DEFAULT_GAIN", 500)))
    RELAY_DEFAULT_BASS: int = _int("RELAY_DEFAULT_BASS", 14)
    RELAY_DEFAULT_TREBLE: int = _int("RELAY_DEFAULT_TREBLE", 100)

    EXTRA_GAIN_DB: int = _int("EXTRA_GAIN_DB", 60)
    LOUD_EXTRA_DB: int = _int("LOUD_EXTRA_DB", 30)

    FAST_PLAY: bool = _bool("FAST_PLAY", True)

    HAND_RAISE_DEFAULT: bool = _bool("HAND_RAISE_DEFAULT", True)
    MIC_BLINK_DEFAULT: bool = _bool("MIC_BLINK_DEFAULT", True)
    MIC_BLINK_SECONDS: int = _int("MIC_BLINK_SECONDS", 5)

    SCREEN_SHARE_ENABLED: bool = _bool("SCREEN_SHARE_ENABLED", True)
    SS_WIDTH: int = _int("SS_WIDTH", 1920)
    SS_HEIGHT: int = _int("SS_HEIGHT", 1080)
    SS_FPS: int = _int("SS_FPS", 20)
    SS_SEND_WIDTH: int = _int("SS_SEND_WIDTH", 1280)
    SS_SEND_HEIGHT: int = _int("SS_SEND_HEIGHT", 720)
    SS_SEND_FPS: int = _int("SS_SEND_FPS", 20)

    LIVE_BOOST_DEFAULT: int = _int("LIVE_BOOST_DEFAULT", 20000)
    AUTO_LIVE_BOOST: bool = _bool("AUTO_LIVE_BOOST", True)
    AUTO_MODE_DEFAULT: bool = _bool("AUTO_MODE_DEFAULT", False)
    KEEPER_INTERVAL: int = _int("KEEPER_INTERVAL", 15)

    MAX_ACTIVE_SESSIONS: int = _int("MAX_ACTIVE_SESSIONS", 6)
    SESSION_IDLE_TIMEOUT: int = _int("SESSION_IDLE_TIMEOUT", 600)
    SESSION_EVICT_INTERVAL: int = _int("SESSION_EVICT_INTERVAL", 120)
    RESTORE_ALL_SESSIONS: bool = _bool("RESTORE_ALL_SESSIONS", False)
    MEMORY_SOFT_MB: int = _int("MEMORY_SOFT_MB", 650)
    MEMORY_HARD_MB: int = _int("MEMORY_HARD_MB", 820)
    MEMORY_CHECK_INTERVAL: int = _int("MEMORY_CHECK_INTERVAL", 30)

    MONGO_URI: str = os.environ.get("MONGO_URI", "")

    _HEROKU_APP_NAME: str = os.environ.get("HEROKU_APP_NAME", "").strip()
    _HEROKU_DEFAULT_DOMAIN: str = os.environ.get("HEROKU_APP_DEFAULT_DOMAIN_NAME", "").strip()
    _LIVE_MIC_OVERRIDE: str = os.environ.get("LIVE_MIC_BASE_URL", "").strip()
    LIVE_MIC_BASE_URL: str = (
        _norm_url(_LIVE_MIC_OVERRIDE)
        or _norm_url(_HEROKU_DEFAULT_DOMAIN)
        or (f"https://{_HEROKU_APP_NAME}.herokuapp.com" if _HEROKU_APP_NAME else "")
    )
    LIVE_MIC_AUTO_START_VC: bool = _bool("LIVE_MIC_AUTO_START_VC", False)
    START_PIC: str = os.environ.get("START_PIC", "").strip()
    TUTORIAL_VIDEO: str = os.environ.get("TUTORIAL_VIDEO", "").strip()
    SOURCE_CODE_URL: str = os.environ.get("SOURCE_CODE_URL", "").strip()
    PAYMENT_CONTACT_URL: str = os.environ.get("PAYMENT_CONTACT_URL", "").strip()
