"""Playback v69: MAX TEZ + CLEAR (user: v68 "bohot kam", 5-10x louder chahiye).

Bench (pink noise quiet -29 / normal -13 / loud -8 LUFS -> chain):
  v68: -6.6 .. -7.8 LUFS
  v69: -3.2 .. -3.4 LUFS  (~+4 dB = ~1.6x louder than v68, ~11 dB = 3.5x
       sample-power over a normal -14 LUFS music bot), every source same level.
  After Opus 48k voip: -5.0 LUFS, PLR ~8 dB -> not a square wave, not dabi.
Loudness comes from ear-sensitive presence (2.8k/4.2k) + air, hotter push
into an oversampled limiter at -0.35 dBFS; no clipper.
"""
import math

from . import audio_processor as ap

CEILING_DB = -0.35
BASS_SHELF_MAX_DB = 8.0
DEFAULT_BASS = 60
PUSH_MIN_DB = 8.0
PUSH_MAX_DB = 14.0


def _lim(db: float) -> str:
    return f"{math.floor(10 ** (db / 20.0) * 1e4) / 1e4:.4f}"


def bass_db(bass=None) -> float:
    b = DEFAULT_BASS if bass is None else ap.clamp(int(bass), ap.BASS_MIN, ap.BASS_MAX)
    if b == 14:
        b = DEFAULT_BASS
    return round(BASS_SHELF_MAX_DB * b / float(ap.BASS_MAX), 1)


def push_db(boost=None) -> float:
    bo = ap.LEVEL_MAX if boost is None else ap.clamp(int(boost), ap.LEVEL_MIN, ap.LEVEL_MAX)
    return round(PUSH_MIN_DB + (PUSH_MAX_DB - PUSH_MIN_DB) * bo / float(ap.LEVEL_MAX), 2)


def playback_voice_stage(bass=None, treble=None, gain=None, input_gain_db=0.0) -> list:
    out = []
    if input_gain_db and input_gain_db > 0.05:
        out.append(f"volume={ap._db(min(input_gain_db, 30.0))}dB")
    out.append("highpass=f=45:p=2")
    bd = bass_db(bass)
    if bd > 0.05:
        out.append(f"lowshelf=f=110:w=0.8:g={bd:.1f}")
        out.append(f"equalizer=f=220:t=q:w=1.0:g={min(3.0, bd / 2.5):.1f}")
    out += ["equalizer=f=350:t=q:w=1.2:g=-2.5",
            "equalizer=f=2800:t=q:w=1.0:g=5.0",
            "equalizer=f=4200:t=q:w=1.0:g=3.0"]
    t = ap.clamp(100 if treble is None else int(treble), 0, 100)
    out.append(f"highshelf=f=8500:w=0.8:g={(t - 40) * 0.05 + 0.5:.1f}")
    return out


def playback_fight_stage(bass=None, boost=None, over=1.0) -> list:
    return [
        "dynaudnorm=f=150:g=15:p=0.98:m=12:r=0.45:t=0.001",
        "acompressor=threshold=0.1:ratio=3.5:attack=12:release=100:"
        "makeup=4:knee=4:detection=rms",
        f"volume={ap._db(push_db(boost))}dB",
    ]


def playback_ceiling(env: str = "PLAY_CEILING_DB", default: float = None) -> list:
    db = min(CEILING_DB, ap._env_db(env, CEILING_DB, high=-0.1, low=-6.0))
    lim = _lim(db)
    return ["aresample=192000",
            f"alimiter=level_in=1:level_out=1:limit={lim}:attack=2:release=50:asc=1:level=false",
            "aresample=48000",
            f"alimiter=level_in=1:level_out=1:limit={lim}:attack=0.5:release=25:asc=1:level=false"]


def install():
    if getattr(ap, "_playback_v69_installed", False):
        return
    ap._playback_v69_installed = True
    ap.playback_voice_stage = playback_voice_stage
    ap.playback_fight_stage = playback_fight_stage
    ap.playback_ceiling = playback_ceiling
