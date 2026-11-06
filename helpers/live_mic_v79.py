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
Tune: LIVE_LOUDEST_DRIVE_DB (default 26, 0..36).
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


def live_loudest_chain(ceiling_db: float = None) -> str:
    drive = ap._env_db("LIVE_LOUDEST_DRIVE_DB", 26.0, high=36.0, low=0.0)
    limit = 0.93
    if ceiling_db is not None and os.environ.get("LIVE_MIC_RESPECT_ADMIN", "0") == "1":
        limit = min(limit, 10 ** (max(-12.0, min(-0.5, float(ceiling_db))) / 20.0))
    f = ["aresample=48000:async=1:first_pts=0",
         "highpass=f=170:p=2", "highpass=f=170:p=2", "highpass=f=170:p=2"]
    if ap._has_filter("afftdn"):
        f.append("afftdn=nr=8:nf=-50:tn=0")
    if ap._has_filter("agate"):
        f.append("agate=threshold=0.004:range=0.05:ratio=3:attack=3:release=250:knee=6:detection=rms")
    f.append("volume=10dB")
    if ap._has_filter("speechnorm"):
        f.append("speechnorm=e=25:r=0.0008:l=1:p=0.8")
    else:
        f.append("dynaudnorm=f=100:g=9:p=0.8:m=25")
    f += ["acompressor=threshold=0.06:ratio=10:attack=4:release=90:makeup=4:knee=4",
          "equalizer=f=350:t=q:w=1:g=-3",
          "equalizer=f=1100:t=q:w=1:g=3",
          "equalizer=f=2700:t=q:w=1:g=8",
          "lowpass=f=5200", "lowpass=f=5200",
          f"volume={drive:.2f}dB"]
    clip = ["asoftclip=type=tanh:oversample=2"] if ap._has_filter("asoftclip") else []
    f += clip + ["volume=6dB"] + clip
    f += ["lowpass=f=6500",
          f"alimiter=level_in=1:level_out=1:limit={limit:.4f}:attack=1:release=30:level=false"]
    if ap._has_filter("asetnsamples"):
        f.append(f"asetnsamples=n={ap.AUDIO_FRAME_SAMPLES}:p=0")
    return ap._sanitize_ffmpeg_filter(",".join(f))


def build_live_mic_filter(ceiling_db: float = None, loud: int = 0, crunch: int = 0) -> str:
    if get_mode() == "clean":
        return _orig(ceiling_db, loud=loud, crunch=crunch)
    return live_loudest_chain(ceiling_db)


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
