"""Playback v97: TG-LIMIT - teeno play mode Telegram ki asli limit tak tez,
lekin sunne wale ke phone pe na fate, na square bane.

User recording (Telegram VC, loudest -> phone) + Telegram's own code showed
TWO places where the sound is cut AFTER it leaves the bot - no DSP inside the
bot can see them, so every "aur tez" version only made them worse:

1. Opus re-grows the peaks.  ntgcalls sends Opus mono, 32 kbps, 60 ms
   frames (WebRTC defaults, no bitrate set).  At that bitrate the codec
   keeps each band's energy but not the exact wave, so a signal limited to
   0 dBFS comes out of the listener's decoder at up to +5..+7 dBFS.  The
   decoder squashes everything above full scale -> "fatna".
   Your recording: peaks +5 dBFS, 3-5 % of samples above full scale.

2. 200 % participant volume is a hard clip.  Telegram applies it on every
   listener's phone as ``AudioFrameOperations::ScaleWithSat(2.0)`` on the
   decoded 16-bit audio (WebRTC audio/channel_receive.cc, tgcalls
   GroupInstanceCustomImpl::setVolume).  Anything the decoder outputs above
   -6 dBFS is chopped flat -> "square".  The bot sets itself to 200 %
   (ensure_play_volume), so with the old 0 dBFS output 55-79 % of samples
   were chopped flat on listeners' phones.

Fix:
  * every mode ends with a codec trim (``CODEC_TRIM_DB``) measured with the
    exact ntgcalls encoder settings so listener-side clipping stays ~0.5 %
    (inaudible single peaks) instead of 3-25 %;
  * when the bot's 200 % is CONFIRMED by Telegram, the output is 6.02 dB
    lower again - the listener's phone multiplies it back x2 cleanly, so
    the 200 % gives full loudness instead of a square wave;
  * loudest + phone get a 6-band squash before their band filter: every
    frequency band sits near the top, so the same "allowed" energy sounds
    louder (+2..4 sones on vocals, much louder quiet words on speech).
Bass keeps its v96 chain (it only gets the trims).  Live mic not touched.

Measured (song / vocal / speech, chain -> Opus 32k mono 60 ms -> decode ->
listener x volume -> 16-bit), see tests/test_v97_tg_limit.py and the commit.
Env PLAY_TG_TRIM_DB overrides the codec trim (0 = old behaviour).
"""
import contextlib
import contextvars
import logging

from . import audio_processor as ap
from . import playback_v77 as v77
from . import playback_v96 as v96

log = logging.getLogger(__name__)

# dB taken off the finished chain so Opus re-grown peaks stay under full
# scale on the listener (worst case of song / vocal / speech + ~0.2 dB).
CODEC_TRIM_DB = {"loudest": 2.8, "phone": 3.0, "bass": 2.8}
# Telegram 200 % = x2.0 on the listener after decoding (with hard clip).
LISTENER_BOOST_DB = 6.02

_SQUASH_BANDS = (600, 1200, 2000, 3200, 4800, 22000)
SQUASH = "mcompand=args='" + " | ".join(
    f"0.002\\,0.05 6 -60/-24\\,-24/-8\\,0/-6 {f}" for f in _SQUASH_BANDS) + "'"

_listener_boost = contextvars.ContextVar("v97_listener_boost", default=False)


@contextlib.contextmanager
def listener_boost(on: bool):
    """Build chains for a VC where the bot's 200 % volume is confirmed."""
    token = _listener_boost.set(bool(on))
    try:
        yield
    finally:
        _listener_boost.reset(token)


def codec_trim_db(mode: str = None, boost: bool = None) -> float:
    mode = mode or v77.get_mode()
    trim = ap._env_db("PLAY_TG_TRIM_DB", CODEC_TRIM_DB.get(mode, CODEC_TRIM_DB["phone"]),
                      high=20.0, low=0.0)
    on = _listener_boost.get() if boost is None else bool(boost)
    return trim + (LISTENER_BOOST_DB if on else 0.0)


def _with_squash(chain: str, lp: int) -> str:
    band = f",lowpass=f={lp}:p=2"
    if band not in chain or SQUASH in chain:
        return chain
    return chain.replace(band, f",{SQUASH}{band}", 1)


def loudest_chain(*args, **kwargs) -> str:
    return _with_squash(v96.loudest_chain(*args, **kwargs), 4300)


def phone_chain(*args, **kwargs) -> str:
    return _with_squash(v96.phone_chain(*args, **kwargs), 6500)


def _trimmed(chain: str, mode: str = None) -> str:
    if chain.endswith(",volume=0"):          # explicit mute stays the last stage
        return chain
    return f"{chain},volume=-{codec_trim_db(mode):.2f}dB"


_prev_build = None
_prev_fallback = None


def build_ffmpeg_filter(*args, **kwargs) -> str:
    chain = _prev_build(*args, **kwargs)
    if kwargs.get("live") or args:
        return chain
    return _trimmed(chain)


def fallback_filter(*args, **kwargs) -> str:
    return _trimmed(_prev_fallback(*args, **kwargs), "phone")


def install():
    global _prev_build, _prev_fallback
    if getattr(ap, "_playback_v97_installed", False):
        return
    ap._playback_v97_installed = True
    v77.loudest_chain = loudest_chain
    v77.phone_chain = phone_chain
    _prev_build = ap.build_ffmpeg_filter
    ap.build_ffmpeg_filter = build_ffmpeg_filter
    _prev_fallback = ap.fallback_filter
    ap.fallback_filter = fallback_filter
