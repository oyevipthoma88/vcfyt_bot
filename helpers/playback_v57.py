"""Playback v57: fight-loud chain for quiet / normal / fast voice.

Fixes on top of v56 (all playback-only; live mic + bridge untouched):
1. Input gain no longer capped by the file's sample peak.  One click / pop at
   0 dBFS used to block the whole lift, so very quiet voice stayed quiet.
   The limiter at the end already handles peaks.
2. Adaptive leveler (dynaudnorm) right after the static lift: a recording
   that changes between whisper / normal / shouting is pulled to one level
   section by section instead of one gain for the whole file.
3. Output ceiling is true-peak safe after Opus: v56 measured up to +2.7 dBTP
   after a 64k Opus round-trip (receiver-side clipping = cracked voice).
"""
import os
import re
import subprocess
from . import audio_processor as ap

TARGET_LUFS = -18.0
MAX_LIFT_DB = 45.0


def measure_input_gain(path: str) -> float:
    if not path or not os.path.exists(path):
        return 0.0
    try:
        s = os.stat(path)
        key = ("v57", os.path.abspath(path), s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    except OSError:
        return 0.0
    if key in ap._INPUT_GAIN_CACHE:
        return ap._INPUT_GAIN_CACHE[key]
    gain = 0.0
    try:
        err = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostdin", "-t", "90", "-i", path, "-vn", "-sn", "-dn",
             "-af", "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                    "highpass=f=70:p=2,highpass=f=70:p=2,"
                    "ebur128=metadata=1,ametadata=print:key=lavfi.r128.M",
             "-f", "null", "-"], capture_output=True, text=True, timeout=30).stderr
        level = speech_level(err)
        if level is not None:
            gain = max(0.0, min(MAX_LIFT_DB, TARGET_LUFS - level))
    except Exception:
        gain = 0.0
    if len(ap._INPUT_GAIN_CACHE) > 256:
        ap._INPUT_GAIN_CACHE.clear()
    ap._INPUT_GAIN_CACHE[key] = gain
    return gain


def speech_level(ffmpeg_stderr: str):
    """Typical voice loudness from 400 ms momentary blocks.

    Integrated LUFS is ruined by one loud click (its relative gate drops the
    whole quiet voice), so use the 70th percentile of non-silent blocks.
    """
    vals = sorted(float(v) for v in re.findall(r"lavfi\.r128\.M=(-?[\d.]+)", ffmpeg_stderr)
                  if float(v) > -80)
    if len(vals) < 3:
        return None
    return vals[int(0.7 * (len(vals) - 1))]


def leveler() -> list:
    # ~1.5 s window: follows whisper -> shout changes, does not pump per word.
    # t: frames peaking under -34 dBFS (hiss between words, after the static
    # lift voice sits far above this) are never pumped up.
    return ["dynaudnorm=f=150:g=11:p=0.6:m=30:t=0.02"]


_v56_voice = None


def playback_voice_stage(bass=None, treble=None, gain=None, input_gain_db=0.0) -> list:
    out = _v56_voice(bass=bass, treble=treble, gain=gain, input_gain_db=input_gain_db)
    if ap._has_filter("dynaudnorm"):
        idx = 1 if out and out[0].startswith("volume=") and input_gain_db > 0.05 else 0
        out[idx:idx] = leveler()
    return out


def playback_ceiling() -> list:
    lim = 10 ** (ap._env_db("PLAY_CEILING_DB", -3.0, high=-1.0, low=-6.0) / 20.0)
    return ["aresample=192000",
            f"alimiter=level_in=1:level_out=1:limit={lim:.4f}:attack=1:release=60:level=false:latency=true",
            "aresample=48000",
            f"alimiter=level_in=1:level_out=1:limit={lim:.4f}:attack=0.1:release=40:level=false:latency=true"]


def install():
    global _v56_voice
    if getattr(ap, "_playback_v57_installed", False):
        return
    ap._playback_v57_installed = True
    _v56_voice = ap.playback_voice_stage
    for name in ("measure_input_gain", "playback_voice_stage", "playback_ceiling"):
        setattr(ap, name, globals()[name])
