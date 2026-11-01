"""Playback v68: TEZ + CLEAR + NOT DABI (root fix of the v57-v66 gain stack).

Measured root cause (v66 chain, any normal song / voice note):
  input lifted to -18 LUFS, then RMS comp makeup +3 dB, +22 dB, +14 dB and
  +8 dB of drive (~47 dB) through three limiters + a tanh clipper.  Output
  was -1.0 LUFS with a -1.0 dBFS peak: integrated loudness == peak, i.e. a
  square wave.  Every transient and syllable edge was flattened, Opus
  (Telegram VC codec) then smeared that full-scale noise -> "aawaz dabi
  hui / kam lagti hai" even though the meter showed MAX.

v68 chain (played files only; live mic and bridge untouched):
  voice : highpass 35 Hz -> bass shelf 100 Hz -> warmth 220 Hz
          -> mud cut 420 Hz -> presence 2.8 kHz -> air shelf 8 kHz
  level : dynaudnorm (slow, look-ahead) evens quiet/loud parts of the
          SOURCE without touching transients
  glue  : 2.5:1 RMS compressor, slow attack keeps the punch
  push  : ONE drive stage into an oversampled look-ahead limiter at -1.5 dBTP
          (boost 10 = +8 dB push)
Bench (synthetic quiet voice -35, voice -22, music -14, loud music -7 LUFS
-> chain -> Opus 48k voip):
  v66: voice -1.1 LUFS / peak-to-loudness 1.2-1.8 dB (square wave),
       music -3.2 LUFS / PLR 5
  v68: ALL sources -5.4 .. -5.6 LUFS, PLR 4.2-5.9 dB, true peak <= +0.4.
~8 dB louder than a normal music bot (-14 LUFS), same level for every
source, and syllables / beats keep their edges.  Tests:
tests/test_v68_tez_clear.py.
"""
import math

from . import audio_processor as ap

CEILING_DB = -1.5
BASS_SHELF_MAX_DB = 7.0
DEFAULT_BASS = 60
# boost 0..10 -> push into the limiter (dB).  Tuned with the test bench so
# boost 10 lands ~-7.5 LUFS after Opus with no square-wave clipping.
PUSH_MIN_DB = 1.0
PUSH_MAX_DB = 8.0


def _lim(db: float) -> str:
    return f"{math.floor(10 ** (db / 20.0) * 1e4) / 1e4:.4f}"


def bass_db(bass=None) -> float:
    b = DEFAULT_BASS if bass is None else ap.clamp(int(bass), ap.BASS_MIN, ap.BASS_MAX)
    if b == 14:  # old saved "thin" default
        b = DEFAULT_BASS
    return round(BASS_SHELF_MAX_DB * b / float(ap.BASS_MAX), 1)


def push_db(boost=None) -> float:
    bo = ap.LEVEL_MAX if boost is None else ap.clamp(int(boost), ap.LEVEL_MIN, ap.LEVEL_MAX)
    return round(PUSH_MIN_DB + (PUSH_MAX_DB - PUSH_MIN_DB) * bo / float(ap.LEVEL_MAX), 2)


def playback_voice_stage(bass=None, treble=None, gain=None, input_gain_db=0.0) -> list:
    out = []
    # bring every source to -18 LUFS first (quiet voice notes too)
    if input_gain_db and input_gain_db > 0.05:
        out.append(f"volume={ap._db(min(input_gain_db, 30.0))}dB")
    out.append("highpass=f=35:p=2")
    bd = bass_db(bass)
    if bd > 0.05:
        out.append(f"lowshelf=f=100:w=0.7:g={bd:.1f}")
        out.append(f"equalizer=f=220:t=q:w=1.0:g={min(2.5, bd / 3):.1f}")
    out += ["equalizer=f=420:t=q:w=1.2:g=-2.5",
            "equalizer=f=2800:t=q:w=1.0:g=3.0"]
    t = ap.clamp(100 if treble is None else int(treble), 0, 100)
    out.append(f"highshelf=f=8000:w=0.7:g={(t - 40) * 0.05:.1f}")
    return out


def playback_fight_stage(bass=None, boost=None, over=1.0) -> list:
    return [
        # even out quiet / loud passages of the source (slow, no pumping)
        "dynaudnorm=f=250:g=15:p=0.5:m=8:r=0.25:t=0.0005",
        "acompressor=threshold=0.1:ratio=2.5:attack=20:release=250:"
        "makeup=2:knee=6:detection=rms",
        f"volume={ap._db(push_db(boost))}dB",
    ]


def playback_ceiling(env: str = "PLAY_CEILING_DB", default: float = None) -> list:
    db = min(CEILING_DB, ap._env_db(env, CEILING_DB, high=-0.5, low=-6.0))
    lim = _lim(db)
    # single oversampled look-ahead limiter (true-peak safe for Opus) + a
    # sample-peak guard after downsampling.  No clipper, no 2nd push.
    return ["aresample=192000",
            f"alimiter=level_in=1:level_out=1:limit={lim}:attack=5:release=80:level=false",
            "aresample=48000",
            f"alimiter=level_in=1:level_out=1:limit={lim}:attack=0.5:release=40:level=false"]


def install():
    if getattr(ap, "_playback_v68_installed", False):
        return
    ap._playback_v68_installed = True
    ap.playback_voice_stage = playback_voice_stage
    ap.playback_fight_stage = playback_fight_stage
    ap.playback_ceiling = playback_ceiling
