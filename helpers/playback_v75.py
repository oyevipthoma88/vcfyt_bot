"""Playback v75: AUR TEZ + DAMDAR (user: "aawaz ko aur strong aur tez kar").

Measured on v74 (speech -> chain -> Opus 48k voip, bass/boost/gain/treble full):
  LUFS -2.1, low band -6.8, phone band (300 Hz-7 kHz) -4.5.
The chain was already at the limiter ceiling, so EQ / compressor tweaks moved
loudness < 0.3 dB.  The only lever left is MORE DRIVE into the final tanh
clipper (denser, "damdar" wave) plus FFmpeg's virtualbass on top of v74's own
harmonic bass.
v75 (played files only; live mic / bridge untouched):
  * virtualbass (cutoff 200 Hz) after the body EQ -> extra audible bass;
  * +PLAY_EXTRA_DRIVE_DB (default 6 dB, env 0..12) into the LAST tanh stage.
Measured: LUFS -2.1 -> ~-0.6, low -6.8 -> ~-5.4, phone -4.5 -> ~-3.0.
Set PLAY_EXTRA_DRIVE_DB=0 if it ever sounds too cracked.
"""
import os
from . import audio_processor as ap

EXTRA_DRIVE_DB = 6.0
_orig_build = None


def extra_drive_db() -> float:
    try:
        v = float(os.environ.get("PLAY_EXTRA_DRIVE_DB", EXTRA_DRIVE_DB))
    except ValueError:
        v = EXTRA_DRIVE_DB
    return max(0.0, min(12.0, v))


def build_ffmpeg_filter(*args, **kwargs) -> str:
    chain = _orig_build(*args, **kwargs)
    if kwargs.get("live"):
        return chain
    parts = chain.split(",")
    soft = [i for i, x in enumerate(parts) if x.startswith("asoftclip=type=tanh:oversample=4")]
    d = extra_drive_db()
    if soft and d > 0.05:
        parts.insert(soft[-1], f"volume={ap._db(d)}dB")
    if ap._has_filter("virtualbass"):
        nr = [i for i, x in enumerate(parts) if x.startswith(("afftdn", "speechnorm"))]
        if nr:
            parts.insert(nr[0], "virtualbass=cutoff=200:strength=2")
    return ",".join(parts)


def install():
    global _orig_build
    if getattr(ap, "_playback_v75_installed", False):
        return
    ap._playback_v75_installed = True
    _orig_build = ap.build_ffmpeg_filter
    ap.build_ffmpeg_filter = build_ffmpeg_filter
