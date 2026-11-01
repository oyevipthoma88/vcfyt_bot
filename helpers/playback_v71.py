"""Playback v71: ANTI-DUCK FIGHT CHAIN (user: "samne wala fighter gain badhata
hai to meri aawaz kam aati hai").

Root cause: Telegram VC mixes every speaker into one signal and the receiver's
WebRTC limiter turns the WHOLE mix down when the sum goes over full scale.
The voice with more steady energy in the 1-5 kHz band (where the ear is most
sensitive) wins; the other one gets masked.  v69 spent a big part of its
headroom on 45-220 Hz bass (lowshelf +4.8 dB) that phones barely play, and its
3.5:1 RMS compressor left dips between words where the opponent's boosted mic
took over.

v71 (played files only, live mic / bridge unchanged):
  1. low-cut at 150 Hz (x2) -> ~6-8 dB of headroom freed for the voice
  2. de-box 350 Hz, strong "bite" 1.2k / 2.8k / 4.2k, light air
  3. dense levelling: fast dynaudnorm + 8:1 peak compressor -> no dips
  4. hot push into a soft clipper (tanh, 4x oversampled) -> high RMS density
  5. phase-coherent mono (L == R) so Opus spends all bits on one signal
  6. oversampled -1.0 dBFS true-peak ceiling
"""
import math

from . import audio_processor as ap

CEILING_DB = -1.0
PUSH_MIN_DB = 10.0
PUSH_MAX_DB = 16.0


def _lim(db: float) -> str:
    return f"{math.floor(10 ** (db / 20.0) * 1e4) / 1e4:.4f}"


def push_db(boost=None) -> float:
    bo = ap.LEVEL_MAX if boost is None else ap.clamp(int(boost), ap.LEVEL_MIN, ap.LEVEL_MAX)
    return round(PUSH_MIN_DB + (PUSH_MAX_DB - PUSH_MIN_DB) * bo / float(ap.LEVEL_MAX), 2)


def playback_voice_stage(bass=None, treble=None, gain=None, input_gain_db=0.0) -> list:
    out = []
    if input_gain_db and input_gain_db > 0.05:
        out.append(f"volume={ap._db(min(input_gain_db, 30.0))}dB")
    out += [
        "highpass=f=150:p=2", "highpass=f=150:p=2",
        "equalizer=f=350:t=q:w=1.2:g=-4",
        "equalizer=f=1200:t=q:w=1.0:g=2.5",
        "equalizer=f=2800:t=q:w=1.0:g=6",
        "equalizer=f=4200:t=q:w=1.2:g=4",
        "highshelf=f=8000:w=0.8:g=1.5",
        "lowpass=f=12000:p=2",
    ]
    return out


def playback_fight_stage(bass=None, boost=None, over=1.0) -> list:
    out = [
        "dynaudnorm=f=100:g=7:p=0.95:m=30:r=0.6:t=0.001",
        "acompressor=threshold=0.05:ratio=8:attack=2:release=60:makeup=6:knee=2:detection=peak",
        f"volume={ap._db(push_db(boost))}dB",
    ]
    if ap._has_filter("asoftclip"):
        out.append("asoftclip=type=tanh:oversample=4")
    return out


def playback_ceiling(env: str = "PLAY_CEILING_DB", default: float = None) -> list:
    db = min(CEILING_DB, ap._env_db(env, CEILING_DB, high=-0.1, low=-6.0))
    lim = _lim(db)
    return ["pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1",
            "aresample=192000",
            f"alimiter=level_in=1:level_out=1:limit={lim}:attack=1:release=40:asc=1:level=false",
            "aresample=48000",
            f"alimiter=level_in=1:level_out=1:limit={lim}:attack=0.5:release=25:asc=1:level=false"]


def install():
    if getattr(ap, "_playback_v71_installed", False):
        return
    ap._playback_v71_installed = True
    ap.playback_voice_stage = playback_voice_stage
    ap.playback_fight_stage = playback_fight_stage
    ap.playback_ceiling = playback_ceiling
