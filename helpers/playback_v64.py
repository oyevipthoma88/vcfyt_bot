"""Playback v64: FIGHT LOUD - played files must be far louder than music bots.

Root cause ("normal audio music bot ke barabar aati hai"):
  * The saved .playdb value (default 10 dB in settings) is written to
    PLAY_DRIVE_DB at startup and REPLACED the v63 default drive (12 dB),
    so every play ran with less drive than the code default.
  * Even 12 dB left real speech at ~-5.5 LUFS after Opus - about the
    level of a loud mastered song, so it sounded "barabar".

v64: drive has a hard floor of 14 + 0.6*boost dB (boost 10 -> 20 dB);
saved/env values can only push higher (max 30 dB), never lower.
Bench (real speech -> chain -> Opus 64k voip -> decode):
  v63: -5.5 LUFS   v64: -2.5 LUFS  (~3 dB / clearly louder).
Live mic / bridge untouched.
"""
from . import audio_processor as ap

DRIVE_BASE = 14.0
DRIVE_PER_BOOST = 0.6
DRIVE_MAX = 30.0


def fight_drive_db(boost=None) -> float:
    bo = ap.LEVEL_MAX if boost is None else ap.clamp(int(boost), ap.LEVEL_MIN, ap.LEVEL_MAX)
    floor = DRIVE_BASE + DRIVE_PER_BOOST * bo
    saved = ap._env_db("PLAY_DRIVE_DB", floor, high=DRIVE_MAX, low=0.0)
    return min(DRIVE_MAX, max(floor, saved))


def playback_fight_stage(bass=None, boost=None, over=1.0) -> list:
    out = ["acompressor=threshold=0.05:ratio=6:attack=3:release=100:"
           "makeup=4:knee=4:detection=rms"]
    if ap._has_filter("mcompand"):
        from .playback_v62 import MCOMPAND
        out.append(MCOMPAND)
    out.append(f"volume={ap._db(fight_drive_db(boost))}dB")
    if ap._has_filter("asoftclip"):
        out.append("asoftclip=type=tanh:threshold=0.95:oversample=4")
    out.append("lowpass=f=14000")
    return out


def install():
    if getattr(ap, "_playback_v64_installed", False):
        return
    ap._playback_v64_installed = True
    # playback only: live mic / bridge (playback_v62.pro_max_master) keep v63
    ap.playback_fight_stage = playback_fight_stage
