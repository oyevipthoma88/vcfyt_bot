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
    SESSION_BOT_USERNAME: str = os.environ.get("SESSION_BOT_USERNAME", "Session_generator_1bot")
    SESSION_BOT_LINK: str = f"https://t.me/{SESSION_BOT_USERNAME}"

    DEFAULT_VOLUME: int = _int("DEFAULT_VOLUME", 1000)
    DEFAULT_BASS: int = _int("DEFAULT_BASS", 8)
    DEFAULT_ECHO: bool = _bool("DEFAULT_ECHO", False)
    DEFAULT_ECHO_LEVEL: int = _int("DEFAULT_ECHO_LEVEL", 2)
    DEFAULT_BOOST: int = _int("DEFAULT_BOOST", 10)

    RELAY_DEFAULT_VOLUME: int = _int("RELAY_DEFAULT_VOLUME", 1000)
    RELAY_DEFAULT_GAIN: int = _int("RELAY_DEFAULT_GAIN", 180)
    RELAY_DEFAULT_BASS: int = _int("RELAY_DEFAULT_BASS", 12)
    RELAY_DEFAULT_TREBLE: int = _int("RELAY_DEFAULT_TREBLE", 90)

    EXTRA_GAIN_DB: int = _int("EXTRA_GAIN_DB", 12)

    LIVE_BOOST_DEFAULT: int = _int("LIVE_BOOST_DEFAULT", 20000)
    AUTO_LIVE_BOOST: bool = _bool("AUTO_LIVE_BOOST", True)
    AUTO_MODE_DEFAULT: bool = _bool("AUTO_MODE_DEFAULT", False)
    KEEPER_INTERVAL: int = _int("KEEPER_INTERVAL", 15)

    MAX_ACTIVE_SESSIONS: int = _int("MAX_ACTIVE_SESSIONS", 50)
    SESSION_IDLE_TIMEOUT: int = _int("SESSION_IDLE_TIMEOUT", 1800)
    SESSION_EVICT_INTERVAL: int = _int("SESSION_EVICT_INTERVAL", 300)

    MONGO_URI: str = os.environ.get("MONGO_URI", "")

    LIVE_MIC_BASE_URL: str = _s(
        "LIVE_MIC_BASE_URL", "https://vcfytbpt22922.herokuapp.com"
    ).rstrip("/")
    START_PIC: str = os.environ.get("START_PIC", "").strip()
    SOURCE_CODE_URL: str = os.environ.get("SOURCE_CODE_URL", "").strip()
    PAYMENT_CONTACT_URL: str = os.environ.get("PAYMENT_CONTACT_URL", "").strip()
