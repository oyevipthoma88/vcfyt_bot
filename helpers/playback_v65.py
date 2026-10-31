"""Playback v65: STAGED MAX - ~56 dB of total push, still clear (not square).

Why not just "+30 dB more" on v64: one big volume step into a tanh clipper
turns speech into a square wave. Bench (real speech -> chain -> Opus 64k
voip -> decode), v64 with drive 30 dB: -0.9 LUFS but 13.9 % samples
clipped = pure square/crackle.

v65 pushes in three stages with a look-ahead limiter between each, so
each stage only adds density, then a soft clipper and a -1.0 dBFS ceiling:
    20 dB -> limiter -> 16 dB -> limiter -> 20 dB -> tanh -> ceiling
Bench: v64 -3.1 LUFS / 4.8 % clipped  ->  v65 -2.7 LUFS / 4.1 % clipped
(louder AND cleaner). Saved .playdb above the floor adds up to +4 dB more
on the last stage; it can never lower the push. Live mic / bridge untouched.
"""
from . import audio_processor as ap

STAGE1_DB = 20.0
STAGE2_DB = 16.0
STAGE3_DB = 20.0
EXTRA_MAX_DB = 4.0
CEILING_MAX_DB = -1.0   # above this Opus overs -> square/crackle
GATE = "agate=threshold=0.06:range=0.06:ratio=3:attack=2:release=250:knee=4:detection=rms"
_INNER_LIMIT = 10 ** (-1.0 / 20.0)


def stage_gains(boost=None) -> tuple:
    bo = ap.LEVEL_MAX if boost is None else ap.clamp(int(boost), ap.LEVEL_MIN, ap.LEVEL_MAX)
    scale = bo / float(ap.LEVEL_MAX)
    s1, s2, s3 = STAGE1_DB * scale, STAGE2_DB * scale, STAGE3_DB * scale
    from .playback_v64 import fight_drive_db
    floor = 14.0 + 0.6 * bo
    extra = max(0.0, min(EXTRA_MAX_DB, fight_drive_db(boost) - floor))
    return s1, s2, s3 + extra


def total_push_db(boost=None) -> float:
    return sum(stage_gains(boost))


def playback_fight_stage(bass=None, boost=None, over=1.0) -> list:
    s1, s2, s3 = stage_gains(boost)
    out = ["acompressor=threshold=0.05:ratio=6:attack=3:release=100:"
           "makeup=4:knee=4:detection=rms"]
    if ap._has_filter("mcompand"):
        from .playback_v62 import MCOMPAND
        out.append(MCOMPAND)
    lim = f"{_INNER_LIMIT:.4f}"
    out += [GATE, f"volume={ap._db(s1)}dB",
            f"alimiter=level_in=1:level_out=1:limit={lim}:attack=5:release=80:level=false",
            f"volume={ap._db(s2)}dB",
            f"alimiter=level_in=1:level_out=1:limit={lim}:attack=2:release=50:level=false",
            f"volume={ap._db(s3)}dB"]
    if ap._has_filter("asoftclip"):
        out.append("asoftclip=type=tanh:threshold=0.95:oversample=4")
    out.append("lowpass=f=10000:p=2")
    return out


def ceiling_db() -> float:
    saved = ap._env_db("PLAY_CEILING_DB", CEILING_MAX_DB, high=-0.1, low=-6.0)
    return min(CEILING_MAX_DB, saved)


def playback_ceiling(env: str = "PLAY_CEILING_DB", default: float = None) -> list:
    lim = 10 ** (ceiling_db() / 20.0)
    return ["aresample=192000",
            f"alimiter=level_in=1:level_out=1:limit={lim:.4f}:attack=1:release=40:level=false",
            "aresample=48000",
            f"alimiter=level_in=1:level_out=1:limit={lim:.4f}:attack=0.1:release=30:level=false"]


def install():
    if getattr(ap, "_playback_v65_installed", False):
        return
    ap._playback_v65_installed = True
    # played files only; live mic / bridge keep their own master (v62/v63)
    ap.playback_fight_stage = playback_fight_stage
    ap.playback_ceiling = playback_ceiling
