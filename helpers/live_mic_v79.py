"""Live mic sound engine (v90) — same 3 modes as audio playback, pushed to 0 dBFS.

Users said: "audio wale 3 mode bohot tez aur best hai, live mic bas thoda
boost hota hai".  The old live chain (v83) ended in ONE gentle tanh at
+15 dB into a 0.84 (-1.5 dBFS) limiter with a soft speechnorm (p=0.6) —
roughly 5-7 LU quieter than a played file after Opus.

v90 gives the browser mic the SAME three modes as `.loudmode` playback,
built from the same stages (phone band, hard speech levelling, multiband
squash, presence, double tanh drive) but:

  * mono input from the phone (no stereo pan-down),
  * a low-latency soft expander so pause hiss is never pushed up,
  * the final ceiling at 0 dBFS (limit 0.999) — Telegram receives a
    full-scale signal, nothing is left on the table,
  * `loud` (0..200, page PUSH slider) moves the main drive -12..+8 dB.

Modes: loudest (default) | phone | bass.   Old saved "clean" -> "phone".
Env:   LIVE_MIC_LOUD_MODE, LIVE_DRIVE_DB (extra dB, -12..+12),
       LIVE_MIC_CEILING (0.80..1.0, default 0.999).
"""
import os

from . import audio_processor as ap

MODES = ("loudest", "phone", "bass")
_ALIASES = {"clean": "phone", "fight": "loudest", "max": "loudest", "tez": "loudest",
            "damdar": "bass"}
GATE_POST = 0.05
LABELS = {"loudest": "⚡ LOUDEST", "phone": "📱 PHONE", "bass": "🔊 BASS"}
_mode = None
_orig = None


def normalize_mode(mode: str) -> str:
    m = (mode or "").strip().lower()
    m = _ALIASES.get(m, m)
    return m if m in MODES else ""


def get_mode() -> str:
    global _mode
    if _mode is None:
        _mode = normalize_mode(os.environ.get("LIVE_MIC_LOUD_MODE", "loudest")) or "loudest"
    return _mode


def set_mode(mode: str) -> str:
    global _mode
    m = normalize_mode(mode)
    if not m:
        raise ValueError(mode)
    _mode = m
    return _mode


def _ceiling() -> float:
    try:
        v = float(os.environ.get("LIVE_MIC_CEILING", "") or 0.999)
    except ValueError:
        v = 0.999
    return max(0.80, min(1.0, v))


def _push_offset(loud) -> float:
    """Page PUSH slider (0..200, default 120) -> -12..+8 dB on the main drive."""
    try:
        loud = int(loud)
    except (TypeError, ValueError):
        loud = 120
    loud = max(0, min(200, loud if loud else 120))
    extra = ap._env_db("LIVE_DRIVE_DB", 0.0, high=12.0, low=-12.0)
    return (loud - 120) / 10.0 + extra


def _clip() -> str:
    if ap._has_filter("asoftclip"):
        return "asoftclip=type=tanh:oversample=4"
    return "alimiter=level_in=1:level_out=1:limit=0.9:attack=0.1:release=5:level=false"


def _front(hp: float, n_hp: int, lp: float = 0.0, n_lp: int = 0) -> list:
    """Band-limit + light denoise + soft expander (pause hiss stays down)."""
    f = ["aresample=48000:async=1:first_pts=0"]
    f += [f"highpass=f={hp:g}:p=2"] * n_hp
    f += [f"lowpass=f={lp:g}:p=2"] * n_lp
    if ap._has_filter("afftdn"):
        f.append("afftdn=nr=10:nf=-50:tn=0")
    if ap._has_filter("agate"):
        f.append("agate=threshold=0.004:range=0.06:ratio=2.5:attack=3:release=250:knee=6:detection=rms")
    f.append("volume=6dB")
    return f


def _level(e: float = 24.0, r: float = 0.001) -> str:
    """Hard speech levelling (every word at full scale) followed by a
    post-level gate: after speechnorm the voice sits near 0 dBFS, so
    anything 26 dB below it is room hiss / far bleed and is pushed down
    again before the +30 dB drive (otherwise the pauses roar)."""
    lv = (f"speechnorm=e={e:g}:r={r:g}:l=1:p=0.95" if ap._has_filter("speechnorm")
          else "dynaudnorm=f=100:g=5:p=0.95:m=30")
    if ap._has_filter("agate"):
        lv += (f",agate=threshold={GATE_POST:g}:range=0.02:ratio=4:attack=2:"
               "release=220:knee=4:detection=rms")
    return lv


def _tail(stages: list, limit: float) -> list:
    stages = list(stages)
    stages.append(f"alimiter=level_in=1:level_out=1:limit={limit:.4f}:attack=0.1:release=20:level=false")
    if ap._has_filter("asetnsamples"):
        stages.append(f"asetnsamples=n={ap.AUDIO_FRAME_SAMPLES}:p=0")
    return stages


def chain_loudest(loud=120) -> str:
    """Mirror of playback `loudest`: 330 Hz-4.3 kHz, 3-band squash, +9 dB
    presence, exciter, +28 dB -> tanh -> +6 dB -> tanh -> 0 dBFS."""
    clip = _clip()
    drive = max(6.0, 28.0 + _push_offset(loud))
    f = _front(300, 3, 4300, 2)
    f.append(_level(24.0, 0.001))
    f.append("mcompand=args='0.002\\,0.08 6 -60/-20\\,-30/-6\\,0/-1 1000 | "
             "0.002\\,0.06 6 -60/-18\\,-30/-4\\,0/-1 2400 | "
             "0.001\\,0.04 6 -60/-16\\,-30/-3\\,0/-1 22000'")
    f += ["equalizer=f=1000:t=q:w=1:g=3", "equalizer=f=2700:t=q:w=0.9:g=9"]
    if ap._has_filter("aexciter"):
        f.append("aexciter=amount=3:drive=6:blend=0:freq=2000:ceil=9999")
    f += [f"volume={ap._db(drive)}dB", clip, "lowpass=f=4500", "volume=6dB", clip]
    return ap._sanitize_ffmpeg_filter(",".join(_tail(f, _ceiling())))


def chain_phone(loud=120) -> str:
    """Mirror of playback `phone`: 280 Hz-6 kHz, 10:1 comp, presence,
    +30 dB -> tanh -> +8 dB -> tanh -> 0 dBFS (a bit more open/clear)."""
    clip = _clip()
    drive = max(6.0, 28.0 + _push_offset(loud))
    f = _front(260, 3)
    f.append(_level(24.0, 0.0008))
    f += ["acompressor=threshold=0.05:ratio=10:attack=1:release=50:makeup=6:knee=2",
          "equalizer=f=1200:t=q:w=1:g=3", "equalizer=f=2600:t=q:w=1:g=7",
          "lowpass=f=6000:p=2",
          f"volume={ap._db(drive)}dB", clip, "volume=8dB", clip]
    return ap._sanitize_ffmpeg_filter(",".join(_tail(f, _ceiling())))


def chain_bass(loud=120) -> str:
    """Mirror of playback `bass` (damdar): full body, harmonic bass,
    presence, 12 dB -> tanh -> 18 dB -> tanh -> 0 dBFS."""
    clip = _clip()
    off = _push_offset(loud)
    f = _front(70, 2)
    if ap._has_filter("virtualbass"):
        f.append("virtualbass=cutoff=200:strength=2")
    f.append(_level(24.0, 0.0008))
    f += ["lowshelf=f=170:w=0.7:g=5", "equalizer=f=380:t=q:w=1.4:g=-3",
          "equalizer=f=1200:t=q:w=1:g=3", "equalizer=f=2800:t=q:w=1:g=6",
          "equalizer=f=4200:t=q:w=1.2:g=4", "lowpass=f=9000:p=2",
          "acompressor=threshold=0.05:ratio=10:attack=1:release=50:makeup=6:knee=2",
          f"volume={ap._db(max(3.0, 15.0 + off / 2))}dB", clip,
          f"volume={ap._db(max(3.0, 21.0 + off / 2))}dB", clip]
    return ap._sanitize_ffmpeg_filter(",".join(_tail(f, _ceiling())))


CHAINS = {"loudest": chain_loudest, "phone": chain_phone, "bass": chain_bass}


def chain_for(mode: str = None, loud=120) -> str:
    m = normalize_mode(mode) or get_mode()
    return CHAINS[m](loud)


def live_loudest_chain(ceiling_db: float = None, drive_db: float = None,
                       softclip: bool = True) -> str:
    """Back-compat name used by older tests / callers."""
    return chain_loudest()


def build_live_mic_filter(ceiling_db: float = None, loud: int = 0, crunch: int = 0,
                          mode: str = None) -> str:
    # ceiling_db/crunch kept for the call signature; live mic is always 0 dBFS.
    del ceiling_db, crunch
    return chain_for(mode, loud or 120)


async def load_mode() -> str:
    """Restore the saved mic mode after a restart."""
    try:
        from helpers.database import db
        saved = normalize_mode(await db.get_app_value("live_mic_loud_mode") or "")
        if saved:
            set_mode(saved)
    except Exception:
        pass
    return get_mode()


async def save_mode(mode: str) -> str:
    m = set_mode(mode)
    try:
        from helpers.database import db
        await db.set_app_value("live_mic_loud_mode", m)
    except Exception:
        pass
    return m


async def restart_live_sessions() -> int:
    """Running live mics pick up the new chain (VC stream stays joined)."""
    n = 0
    try:
        from helpers import live_mic
        for s in list(getattr(live_mic, "_sessions", {}).values()):
            if getattr(s, "ffmpeg_proc", None) is not None and not getattr(s, "_closed", False):
                try:
                    await s._restart_ffmpeg(max_attempts=1)
                    n += 1
                except Exception:
                    pass
    except Exception:
        pass
    return n


def install():
    global _orig
    if getattr(ap, "_live_mic_v79_installed", False):
        return
    ap._live_mic_v79_installed = True
    _orig = ap.build_live_mic_filter
    ap.build_live_mic_filter = build_live_mic_filter
