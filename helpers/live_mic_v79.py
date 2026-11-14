"""Live mic v79: LIVE-LOUDEST ("live mic ki awaaz sahi se nahi aa rahi").

Measured (quiet phone-mic speech at -30 LUFS -> chain -> Opus 48k voip ->
decode):  old live chain  phone band -6.2 LUFS, clip 1.6 %
          v79 loudest     phone band -5.3 LUFS, clip 3.4 %  (+0.9 dB)
The old live chain kept full-range bass (90 Hz up) and a -1.5 dBFS master,
so part of the level went to bass a phone speaker can't play.  v79 puts the
voice in the 170 Hz - 5.2 kHz band, levels hard (speechnorm + 10:1 comp),
pushes 2.7 kHz (+8 dB, where the ear is most sensitive) and saturates with a
double tanh into a 0.93 limiter.  Low latency: no look-ahead beyond 1 ms.

Mode: env LIVE_MIC_LOUD_MODE=loudest|clean (default loudest); `.micmode`.
Tune: LIVE_LOUDEST_DRIVE_DB (default 15, 0..20).  v83: loud + clear chain.
"""
import os

from . import audio_processor as ap

MODES = ("loudest", "clean")
_mode = None
_orig = None


def get_mode() -> str:
    global _mode
    if _mode is None:
        m = os.environ.get("LIVE_MIC_LOUD_MODE", "loudest").strip().lower()
        _mode = m if m in MODES else "loudest"
    return _mode


def set_mode(mode: str) -> str:
    global _mode
    mode = (mode or "").strip().lower()
    if mode not in MODES:
        raise ValueError(mode)
    _mode = mode
    return _mode


def live_loudest_chain(ceiling_db: float = None, drive_db: float = None,
                       softclip: bool = True) -> str:
    """v83 LOUD + CLEAR live chain (root fix "awaaz tez hai par phati/saaf nahi").

    The v79 chain stacked +10 dB pre-amp, speechnorm, a 10:1 compressor with
    make-up, +8 dB @ 2.7 kHz, +26 dB drive into a DOUBLE tanh clipper and a
    5.2 kHz lowpass.  Measured: -2 LUFS with a 0 dBFS peak = a square wave.
    Telegram's Opus + listener AGC turn that into "khar khar" and it actually
    sounds quieter.  v83: one leveller, one compressor, a presence lift, a
    moderate drive into ONE gentle soft-clip and a brick-wall at -1.5 dBFS.
    Measured ~-9.5 LUFS (very loud), true peak < -1 dBFS, ~0 % Opus clip.
    """
    drive = (ap._env_db("LIVE_LOUDEST_DRIVE_DB", 15.0, high=20.0, low=0.0)
             if drive_db is None else max(0.0, min(20.0, float(drive_db))))
    limit = 0.84  # -1.5 dBFS sample peak -> true peak stays under -1 dBFS
    if ceiling_db is not None and os.environ.get("LIVE_MIC_RESPECT_ADMIN", "0") == "1":
        limit = min(limit, 10 ** (max(-12.0, min(-1.5, float(ceiling_db))) / 20.0))
    f = ["aresample=48000:async=1:first_pts=0",
         "highpass=f=110:p=2", "highpass=f=110:p=2"]
    if ap._has_filter("afftdn"):
        f.append("afftdn=nr=10:nf=-50:tn=0")
    if ap._has_filter("agate"):
        # soft downward expander (range 0.1 = -20 dB): hiss between words
        # never gets levelled up, quiet syllables are never chopped.
        f.append("agate=threshold=0.003:range=0.1:ratio=2:attack=5:release=300:knee=8:detection=rms")
    f.append("volume=6dB")
    if ap._has_filter("speechnorm"):
        f.append("speechnorm=e=10:r=0.0005:l=1:p=0.6")
    else:
        f.append("dynaudnorm=f=150:g=11:p=0.6:m=12")
    f += ["acompressor=threshold=0.1:ratio=4:attack=5:release=120:makeup=2:knee=6",
          "equalizer=f=300:t=q:w=1:g=-3",
          "equalizer=f=2500:t=q:w=1.2:g=5",
          "equalizer=f=4200:t=q:w=1:g=2",
          "lowpass=f=7500",
          f"volume={drive:.2f}dB"]
    if softclip and ap._has_filter("asoftclip"):
        f.append("asoftclip=type=tanh:threshold=0.9:oversample=2")
    f.append(f"alimiter=level_in=1:level_out=1:limit={limit:.4f}:attack=1:release=40:level=false")
    if ap._has_filter("asetnsamples"):
        f.append(f"asetnsamples=n={ap.AUDIO_FRAME_SAMPLES}:p=0")
    return ap._sanitize_ffmpeg_filter(",".join(f))


def build_live_mic_filter(ceiling_db: float = None, loud: int = 0, crunch: int = 0) -> str:
    # v83: CLEAN = same clear chain without the soft-clip stage.  The old
    # "clean" fallback (_orig) measured -2 LUFS / 0 dBFS = square wave too.
    if get_mode() == "clean":
        if os.environ.get("LIVE_MIC_LEGACY_CLEAN", "0") == "1" and _orig:
            return _orig(ceiling_db, loud=loud, crunch=crunch)
        return live_loudest_chain(ceiling_db, softclip=False)
    return live_loudest_chain(ceiling_db)


async def load_mode() -> str:
    """Restore the `.micmode` / panel choice after a restart (was lost)."""
    try:
        from helpers.database import db
        saved = (await db.get_app_value("live_mic_loud_mode") or "").strip().lower()
        if saved in MODES:
            set_mode(saved)
    except Exception:
        pass
    return get_mode()


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
