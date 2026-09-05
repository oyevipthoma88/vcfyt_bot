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
    RELAY_DEFAULT_GAIN: int = _int("RELAY_DEFAULT_GAIN", 150)
    RELAY_DEFAULT_BASS: int = _int("RELAY_DEFAULT_BASS", 8)
    RELAY_DEFAULT_TREBLE: int = _int("RELAY_DEFAULT_TREBLE", 75)

    LIVE_BOOST_DEFAULT: int = _int("LIVE_BOOST_DEFAULT", 20000)
    AUTO_LIVE_BOOST: bool = _bool("AUTO_LIVE_BOOST", True)
    MIC_DEVICE: str = os.environ.get("MIC_DEVICE", "")
    MIC_INPUT_FORMAT: str = os.environ.get("MIC_INPUT_FORMAT", "pulse")
    MIC_DSP: bool = _bool("MIC_DSP", True)
    MIC_RELAY_ENABLED: bool = _bool("MIC_RELAY_ENABLED", True)
    MIC_RELAY_BIND: str = os.environ.get("MIC_RELAY_BIND", "0.0.0.0").strip()
    MIC_RELAY_PORT: int = _int("MIC_RELAY_PORT", _int("PORT", 8765))
    MIC_RELAY_FIFO: str = os.environ.get("MIC_RELAY_FIFO", "/tmp/apex_live_mic.pcm").strip()
    MIC_RELAY_TOKEN: str = os.environ.get("MIC_RELAY_TOKEN", "").strip()

    AUTO_MODE_DEFAULT: bool = _bool("AUTO_MODE_DEFAULT", False)
    KEEPER_INTERVAL: int = _int("KEEPER_INTERVAL", 15)

    MONGO_URI: str = os.environ.get("MONGO_URI", "")

    HEROKU_APP_NAME: str = os.environ.get("HEROKU_APP_NAME", "")
    START_PIC: str = os.environ.get("START_PIC", "").strip()
    SOURCE_CODE_URL: str = os.environ.get("SOURCE_CODE_URL", "").strip()
