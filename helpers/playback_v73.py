"""Playback v73: FULL-SCALE 0 dBFS FIGHT CHAIN (user: "aawaz bohot kam,
samne wale fighter ki tez aa rahi - 0 dBFS tak push kar").

Missing chain in v71: the final ceiling was hard-locked to -1.0 dBFS
(min(CEILING_DB, env)), so even PLAY_CEILING_DB=0 could never reach full
scale, and the push stopped at +16 dB with dips between words left by the
dynaudnorm gain cap g=7.  Every played file lost ~1 dB of peak and ~3-4 dB
of RMS density versus an opponent's boosted mic.

v73 (played files only, live mic / bridge unchanged):
  * same 150 Hz low-cut + voice "bite" EQ as v71 (headroom goes to voice)
  * faster/denser leveller (g=5, m=50) + 10:1 peak compressor
  * push 18..24 dB into TWO oversampled tanh stages (density, no fizz)
  * mono-coherent L == R
  * brick-wall at 0.00 dBFS (limit=1.0) on the 4x oversampled signal and
    again at 48 kHz -> sample peak sits exactly at full scale.
Override with env PLAY_CEILING_DB (-3..0) / PLAY_PUSH_DB (0..30).
"""
import math

from . import audio_processor as ap

CEILING_DB = 0.0
PUSH_MIN_DB = 18.0
PUSH_MAX_DB = 24.0


def _lim(db: float) -> str:
    v = min(1.0, 10 ** (db / 20.0))
    return f"{math.floor(v * 1e4) / 1e4:.4f}"


def ceiling_db() -> float:
    return ap._env_db("PLAY_CEILING_DB", CEILING_DB, high=0.0, low=-3.0)


def push_db(boost=None) -> float:
    bo = ap.LEVEL_MAX if boost is None else ap.clamp(int(boost), ap.LEVEL_MIN, ap.LEVEL_MAX)
    base = PUSH_MIN_DB + (PUSH_MAX_DB - PUSH_MIN_DB) * bo / float(ap.LEVEL_MAX)
    return round(ap._env_db("PLAY_PUSH_DB", base, high=30.0, low=0.0), 2)


def playback_voice_stage(bass=None, treble=None, gain=None, input_gain_db=0.0) -> list:
    out = []
    if input_gain_db and input_gain_db > 0.05:
        out.append(f"volume={ap._db(min(input_gain_db, 30.0))}dB")
    out += [
        "highpass=f=150:p=2", "highpass=f=150:p=2",
        "equalizer=f=350:t=q:w=1.2:g=-4",
        "equalizer=f=1200:t=q:w=1.0:g=3",
        "equalizer=f=2800:t=q:w=1.0:g=6",
        "equalizer=f=4200:t=q:w=1.2:g=4",
        "highshelf=f=8000:w=0.8:g=1.5",
        "lowpass=f=12000:p=2",
    ]
    return out


def playback_fight_stage(bass=None, boost=None, over=1.0) -> list:
    p = push_db(boost)
    first = round(p * 0.5, 2)
    out = [
        "dynaudnorm=f=75:g=5:p=0.95:m=50:r=0.7:t=0.001",
        "acompressor=threshold=0.05:ratio=10:attack=1:release=50:makeup=6:knee=2:detection=peak",
        f"volume={ap._db(first)}dB",
    ]
    soft = ap._has_filter("asoftclip")
    if soft:
        out.append("asoftclip=type=tanh:oversample=4")
    out.append(f"volume={ap._db(round(p - first, 2))}dB")
    if soft:
        out.append("asoftclip=type=tanh:oversample=4")
    return out


def playback_ceiling(env: str = "PLAY_CEILING_DB", default: float = None) -> list:
    lim = _lim(ceiling_db())
    return ["pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1",
            "aresample=192000",
            f"alimiter=level_in=1:level_out=1:limit={lim}:attack=0.5:release=30:asc=1:level=false",
            "aresample=48000",
            f"alimiter=level_in=1:level_out=1:limit={lim}:attack=0.1:release=20:asc=1:level=false"]


def install():
    if getattr(ap, "_playback_v73_installed", False):
        return
    ap._playback_v73_installed = True
    ap.playback_voice_stage = playback_voice_stage
    ap.playback_fight_stage = playback_fight_stage
    ap.playback_ceiling = playback_ceiling
