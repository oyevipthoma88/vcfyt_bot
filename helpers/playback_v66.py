"""Playback v66: BHARI + CLEAR + LOUD (bass body back, no squash, no muffle).

User report on v65: "aawaz dabi hui lagti hai, bhari nahi lagti".
Root causes found in the v62-v65 chain:
  * Bass was almost removed: highpass 110 Hz + only +2 dB @150 Hz max and a
    -6 dB cut @300 Hz -> thin, "phone speaker" voice with no body.
  * ~56 dB of push through 3 limiters + mcompand + gate = every transient
    flattened (crest factor ~3 dB) -> sounds "dabi" / pumping even though
    the meter says loud.
  * lowpass 10 kHz at the end -> muffled, top air gone.

v66 playback chain (live mic / bridge untouched):
  voice : highpass 35 Hz -> lowshelf 100 Hz (bass setting, up to +9 dB)
          -> warmth 220 Hz -> light mud cut 400 Hz -> presence 2.8 kHz
          -> air shelf 8 kHz (treble setting)
  fight : RMS glue comp -> +drive -> look-ahead limiter -> +drive
          -> tanh soft clip -> lowpass 16 kHz
  master: -1.0 dBFS true-peak style ceiling (Opus safe)
Bench (music -> chain -> Opus 64k voip -> decode):
  v65: +0.7 LUFS but 34 % of samples clipped (square wave = crackle/"dabi"),
       bass <150 Hz -10.3 dB of total energy, crest 8.8 dB.
  v66: -5.4 LUFS,  7 % clipped, bass -5.2 dB (~5 dB more body), crest
       10.1 dB (more punch), more top air.  Bass setting 14 (old default)
       is treated as 60.  Tests: tests/test_v66_bhari_loud.py.
"""
from . import audio_processor as ap

BASS_SHELF_MAX_DB = 9.0
DRIVE1_MAX_DB = 22.0
DRIVE2_MAX_DB = 14.0
CEILING_DB = -1.0
SOFTCLIP_DRIVE_DB = 8.0
DEFAULT_BASS = 60  # used when the saved bass is missing/very low (old default 14)


def _lim(db: float) -> str:
    import math
    return f"{math.floor(10 ** (db / 20.0) * 1e4) / 1e4:.4f}"


def bass_db(bass=None) -> float:
    b = DEFAULT_BASS if bass is None else ap.clamp(int(bass), ap.BASS_MIN, ap.BASS_MAX)
    # old saved default 14 was the "thin" bug -> lift to the new default
    if b == 14:
        b = DEFAULT_BASS
    return round(BASS_SHELF_MAX_DB * b / float(ap.BASS_MAX), 1)


def drives(boost=None) -> tuple:
    bo = ap.LEVEL_MAX if boost is None else ap.clamp(int(boost), ap.LEVEL_MIN, ap.LEVEL_MAX)
    s = 0.4 + 0.6 * bo / float(ap.LEVEL_MAX)
    return round(DRIVE1_MAX_DB * s, 2), round(DRIVE2_MAX_DB * s, 2)


def playback_voice_stage(bass=None, treble=None, gain=None, input_gain_db=0.0) -> list:
    out = []
    if input_gain_db and input_gain_db > 0.05:
        out.append(f"volume={ap._db(min(input_gain_db, 12.0))}dB")
    out.append("highpass=f=35:p=2")
    bd = bass_db(bass)
    if bd > 0.05:
        out.append(f"lowshelf=f=100:w=0.7:g={bd:.1f}")
        out.append(f"equalizer=f=220:t=q:w=1.0:g={min(3.0, bd / 3):.1f}")
    out += ["equalizer=f=420:t=q:w=1.2:g=-2.5",     # box/mud, light
            "equalizer=f=2800:t=q:w=1.0:g=3.5"]     # clarity / presence
    t = ap.clamp(100 if treble is None else int(treble), 0, 100)
    out.append(f"highshelf=f=8000:w=0.7:g={(t - 40) * 0.06:.1f}")
    return out


def playback_fight_stage(bass=None, boost=None, over=1.0) -> list:
    d1, d2 = drives(boost)
    d3 = round(SOFTCLIP_DRIVE_DB * d2 / DRIVE2_MAX_DB, 2)
    lim = _lim(-1.5)
    out = ["acompressor=threshold=0.125:ratio=3:attack=10:release=180:"
           "makeup=3:knee=6:detection=rms",
           f"volume={ap._db(d1)}dB",
           f"alimiter=level_in=1:level_out=1:limit={lim}:attack=4:release=90:level=false",
           f"volume={ap._db(d2)}dB"]
    # 2nd look-ahead limiter catches the push cleanly; the soft clip only
    # rounds what is left (no square wave like v65's 56 dB into tanh).
    out.append(f"alimiter=level_in=1:level_out=1:limit={lim}:attack=1.5:release=60:level=false")
    out.append(f"volume={ap._db(d3)}dB")
    if ap._has_filter("asoftclip"):
        out.append("asoftclip=type=tanh:threshold=0.9:oversample=4")
    out.append("lowpass=f=16000")
    return out


def playback_ceiling(env: str = "PLAY_CEILING_DB", default: float = None) -> list:
    db = min(CEILING_DB, ap._env_db(env, CEILING_DB, high=-0.1, low=-6.0))
    lim = _lim(db)
    return ["aresample=192000",
            f"alimiter=level_in=1:level_out=1:limit={lim}:attack=1:release=50:level=false",
            "aresample=48000",
            f"alimiter=level_in=1:level_out=1:limit={lim}:attack=0.1:release=40:level=false"]


def install():
    if getattr(ap, "_playback_v66_installed", False):
        return
    ap._playback_v66_installed = True
    # played files only; live mic / bridge keep their own chain
    ap.playback_voice_stage = playback_voice_stage
    ap.playback_fight_stage = playback_fight_stage
    ap.playback_ceiling = playback_ceiling
