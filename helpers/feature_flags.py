"""Owner-controlled global feature switches (saved in app_meta).

`live_mic_on()` is sync (cached) so keyboards can use it; call
`refresh_flags()` before building screens to pick up the saved value.
"""
from helpers.database import db

LIVE_MIC_KEY = "feature_live_mic"
_cache = {"live_mic": True}


def live_mic_on() -> bool:
    return _cache["live_mic"]


async def refresh_flags() -> bool:
    try:
        v = await db.get_app_value(LIVE_MIC_KEY)
        if v is not None:
            _cache["live_mic"] = str(v) != "0"
    except Exception:
        pass
    return _cache["live_mic"]


async def set_live_mic(on: bool) -> bool:
    _cache["live_mic"] = bool(on)
    await db.set_app_value(LIVE_MIC_KEY, "1" if on else "0")
    return _cache["live_mic"]
