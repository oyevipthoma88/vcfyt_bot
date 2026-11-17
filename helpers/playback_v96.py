"""Playback v96: CLEAN-MAX - teeno play mode (loudest / phone / bass) tez aur
bina fate.

ROOT CAUSE (measured on FFmpeg 8.1 and 9.0, the builds static-ffmpeg /
imageio-ffmpeg download):
    ``asoftclip=...:oversample=N`` (N > 1) is broken.  Its internal
    resampler halves quiet audio (-6 dB) and lets loud audio come OUT
    above full scale (oversample=4 -> up to x1.96, oversample=8 -> x3.3,
    i.e. +6..+10 dB).  Every old chain used it, so the "soft" clipper never
    clipped: the 0.1 ms alimiter after it hard-clipped 6-10 dB of overs at
    48 kHz (aliasing).  That is the "loudest me aawaz fatti hai" crackle.

Fix: real oversampling around a plain soft clipper
    aresample=192000,asoftclip=type=tanh,aresample=48000
(peak stays <= 1.04, quiet parts untouched, alias products -47 dB).

Measured with tests on a fight voice note + a song, chain -> Opus 48 kbps ->
decode -> listener int16 clip.  1-4 kHz loudness (what a phone speaker plays
loudest), speech+music average; SDR = how much of the output is the real
voice (higher = cleaner, less "fatna"):

                 1-4k LUFS   300-3.4k LUFS   SDR (sent)   SDR (heard)
    old loudest     -3.70        -3.55          8.25          7.28
    v96 loudest     -3.50        -2.80         10.16          8.62
    old phone       -4.90        -2.95          9.60          8.60
    v96 phone       -4.50        -2.85         10.12          8.90
    old bass        -6.80        -5.10         11.85          9.76
    v96 bass        -6.25        -4.50         10.12          8.71

=> loudest: a bit louder and ~2 dB cleaner; phone: louder AND cleaner;
bass: louder, as clean as the old phone mode.  The audio was already at
0 dBFS, so the remaining loudness can only come from smarter density,
not from "more volume" (that is exactly what made the old loudest fat).

What changed per mode
  loudest  no multiband squash (it made peaks +10 dB and CUT loudness),
           leveller -> presence EQ -> 2-stage look-ahead limiter ->
           real-oversampled tanh -> true-peak limiter (192 kHz) -0.3 dB.
  phone    same clean tail, 250 Hz - 6.5 kHz voice band, brighter.
  bass     old v75 heavy-bass chain kept, broken clippers replaced and
           their drive lowered (6 / 7 dB) so the fixed clippers don't fat.
Live mic is not touched.
"""
import math
import os
import re

from . import audio_processor as ap
from . import playback_v77 as v77

# Real oversampled soft clipper (asoftclip's own oversample is broken).
CLEAN_CLIP = "aresample=192000,asoftclip=type=tanh,aresample=48000"
CEILING_DB = -0.3
LOUDEST_BASE_DRIVE_DB = 11.0      # + boost (0-10)  -> 21 dB at max
PHONE_BASE_DRIVE_DB = 12.0        # + boost (0-10)  -> 22 dB at max
BASS_CLIP_TRIM_DB = (6.0, 7.0)    # drive taken off the two bass clippers

_BROKEN_CLIP = re.compile(r"asoftclip=type=(\w+):oversample=\d+")


def fix_clippers(chain: str, trims=()) -> str:
    """Replace every broken ``asoftclip:oversample`` with CLEAN_CLIP.
    ``trims[i]`` dB is taken off the drive in front of the i-th clipper."""
    n = [0]

    def rep(m):
        i = n[0]
        n[0] += 1
        t = float(trims[i]) if i < len(trims) else 0.0
        pre = f"volume=-{t:.2f}dB," if t > 0.05 else ""
        return f"{pre}aresample=192000,asoftclip=type={m.group(1)},aresample=48000"

    return _BROKEN_CLIP.sub(rep, chain)


def _drive(base: float, env: str, boost=None, extra_filters: str = "") -> float:
    bo = ap.LEVEL_MAX if boost is None else ap.clamp(int(boost), ap.LEVEL_MIN, ap.LEVEL_MAX)
    d = ap._env_db(env, base + bo, high=30.0, low=0.0)
    m = re.match(r"^volume=(\d+(?:\.\d+)?)dB$", (extra_filters or "").strip())
    if m:                               # .loud N -> a little more push, capped
        d = min(30.0, d + min(4.0, float(m.group(1)) / 4))
    return d


def _tail(drive: float, echo=None, echo_level=None, volume=None) -> list:
    f = []
    el = max(0, min(ap.LEVEL_MAX, int(echo_level or 0)))
    if echo and el:
        f.append(f"aecho=0.8:0.85:{40 + el * 15}:{0.12 + el * 0.04:.2f}")
    ceil = 10 ** (CEILING_DB / 20)
    f += ["volume=6dB",
          "acompressor=threshold=0.125:ratio=4:attack=3:release=80:knee=6:makeup=1",
          # stage 1: slow look-ahead limiter (word density, no harmonics)
          "alimiter=level_in=1:level_out=1:limit=0.5:attack=5:release=60:level=false",
          "volume=6dB",
          # stage 2: faster look-ahead limiter
          "alimiter=level_in=1:level_out=1:limit=0.5:attack=2:release=20:level=false",
          f"volume={ap._db(drive)}dB", CLEAN_CLIP,
          "volume=4dB",
          # true-peak ceiling (192 kHz) -> little Opus overshoot on listeners
          "aresample=192000",
          f"alimiter=level_in=1:level_out=1:limit={ceil:.4f}:attack=1:release=30:asc=1:level=false",
          "aresample=48000",
          "pan=stereo|c0=c0|c1=c0",
          # 48 kHz safety catch for the tiny resampler overshoot
          f"alimiter=level_in=1:level_out=1:limit={ceil:.4f}:attack=1:release=30:level=false"]
    v = 1000 if volume is None else max(0, int(volume))
    if os.environ.get("PLAY_ALLOW_QUIET", "0") != "1":
        v = max(v, ap.VOLUME_CLEAN_MAX)
    if v == 0:
        f.append("volume=0")
    elif v < ap.VOLUME_CLEAN_MAX:
        f.append(f"volume={20 * math.log10(v / ap.VOLUME_CLEAN_MAX):.2f}dB")
    return f


def _head(hp: int, input_gain_db: float) -> list:
    f = ["aresample=48000", "pan=mono|c0=0.5*c0+0.5*c1"]
    f += [f"highpass=f={hp}:p=2"] * 3
    if input_gain_db and input_gain_db > 0.05:
        f.append(f"volume={ap._db(input_gain_db)}dB")
    if ap._has_filter("afftdn"):
        f.append("afftdn=nr=10:nf=-50")
    if ap._has_filter("speechnorm"):
        f.append("speechnorm=e=20:r=0.0005:l=1:p=0.5")
    else:
        f.append("dynaudnorm=f=75:g=5:p=0.5:m=50")
    return f


def loudest_chain(volume=None, echo=None, echo_level=None, boost=None,
                  treble=None, bass=None, gain=None, input_gain_db=0.0,
                  extra_filters=""):
    # gain accepted so every mode takes the same kwargs (leveller + limiters
    # set the level).  bass -> small 400 Hz body (male voice profile heavier).
    del gain
    t = ap.clamp(100 if treble is None else int(treble), 0, 100) / 100.0
    b = ap.clamp(14 if bass is None else int(bass), ap.BASS_MIN, ap.BASS_MAX) / 100.0
    f = _head(300, input_gain_db)
    f += [f"equalizer=f=400:t=q:w=1.2:g={4 * b:.1f}",
          "equalizer=f=500:t=q:w=1:g=-6",
          f"equalizer=f=1100:t=q:w=1:g={2.5 + 4.5 * t:.1f}",
          f"equalizer=f=2600:t=q:w=0.9:g={6 + 9 * t:.1f}",
          "equalizer=f=3600:t=q:w=1:g=3"]
    f += ["lowpass=f=4300:p=2"] * 2
    f += _tail(_drive(LOUDEST_BASE_DRIVE_DB, "PLAY_V96_LOUDEST_DRIVE_DB", boost, extra_filters),
               echo, echo_level, volume)
    return ",".join(f)


def phone_chain(volume=None, echo=None, echo_level=None, boost=None,
                treble=None, bass=None, gain=None, input_gain_db=0.0,
                extra_filters=""):
    del bass, gain  # phone speakers can't play < 250 Hz; leveller sets level
    t = ap.clamp(100 if treble is None else int(treble), 0, 100) / 100.0
    f = _head(250, input_gain_db)
    f += [f"equalizer=f=1200:t=q:w=1:g={2.5 + 6 * t:.1f}",
          f"equalizer=f=2700:t=q:w=1:g={5 + 6 * t:.1f}",
          "highshelf=f=5000:g=1.5"]
    f += ["lowpass=f=6500:p=2"] * 2
    f += _tail(_drive(PHONE_BASE_DRIVE_DB, "PLAY_V96_PHONE_DRIVE_DB", boost, extra_filters),
               echo, echo_level, volume)
    return ",".join(f)


_prev_orig_build = None


def bass_aware_orig_build(*args, **kwargs):
    """v77 sends bass mode AND live audio to the old builder; only the
    played-file bass chain gets its clippers fixed."""
    chain = _prev_orig_build(*args, **kwargs)
    if kwargs.get("live") or args or v77.get_mode() != "bass":
        return chain
    return fix_clippers(chain, BASS_CLIP_TRIM_DB)


def install():
    global _prev_orig_build
    if getattr(ap, "_playback_v96_installed", False):
        return
    ap._playback_v96_installed = True
    v77.loudest_chain = loudest_chain
    v77.phone_chain = phone_chain
    _prev_orig_build = v77._orig_build
    v77._orig_build = bass_aware_orig_build
