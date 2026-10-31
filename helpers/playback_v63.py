"""Playback v63: REAL MAX - fixes "code me sab max, VC me normal".

Root causes found in v62 (bench: quiet speech -> chain -> Opus -> decode):
  1. Ceiling -3.0 dBFS (and env clamp high=-0.5): every sample was held
     3 dB under full scale, so all the "drive" only pushed into a wall.
     The "Opus overs -> Telegram AGC ducks" theory is wrong: ntgcalls sends
     with auto_gain_control / noise_suppression / echo_cancellation OFF
     (wrtc outgoing_audio_channel.cpp), so nothing ducks a hot stream.
  2. Stages after the drive took level back out: deesser (~-3.5 dB),
     agate, a second hard peak compressor, lowpass 11.5 kHz.
  3. .playdb / .masterdb / .ceiling only wrote os.environ in the running
     process -> lost on every Heroku restart/redeploy (DB value ignored).

v63: same voice stage, then hot RMS comp -> mcompand -> drive -> tanh
clipper -> hard clip -> limiter at -0.3 dBFS (default). Live mic/bridge
master gets the same ceiling. Stored dB settings are loaded at startup.
"""
import os

from . import audio_processor as ap

CEILING_DEFAULT = -0.3


def _ceiling_db(env: str = "PLAY_CEILING_DB") -> float:
    return ap._env_db(env, CEILING_DEFAULT, high=-0.1, low=-6.0)


def playback_fight_stage(bass=None, boost=None, over=1.0) -> list:
    bo = ap.LEVEL_MAX if boost is None else ap.clamp(int(boost), ap.LEVEL_MIN, ap.LEVEL_MAX)
    drive = ap._env_db("PLAY_DRIVE_DB", 6.0 + 0.6 * bo, high=24.0, low=0.0)  # boost 10 -> 12 dB
    out = ["acompressor=threshold=0.05:ratio=6:attack=3:release=100:"
           "makeup=4:knee=4:detection=rms"]
    if ap._has_filter("mcompand"):
        from .playback_v62 import MCOMPAND
        out.append(MCOMPAND)
    out.append(f"volume={ap._db(drive)}dB")
    if ap._has_filter("asoftclip"):
        out.append("asoftclip=type=tanh:threshold=0.95:oversample=4")
    out.append("lowpass=f=14000")
    return out


def playback_ceiling(env: str = "PLAY_CEILING_DB", default: float = None) -> list:
    lim = 10 ** (_ceiling_db(env) / 20.0)
    return ["aresample=192000",
            f"alimiter=level_in=1:level_out=1:limit={lim:.4f}:attack=1:release=40:level=false",
            "aresample=48000",
            f"alimiter=level_in=1:level_out=1:limit={lim:.4f}:attack=0.1:release=30:level=false"]


def load_saved_db_settings(settings: dict) -> None:
    """Re-apply .playdb / .masterdb / .ceiling values saved in the DB."""
    if not settings:
        return
    pairs = (("play_drive_db", ("PLAY_DRIVE_DB",)),
             ("master_drive_db", ("MASTER_DRIVE_DB",)),
             ("play_ceiling_db", ("PLAY_CEILING_DB", "MASTER_CEILING_DB")))
    for key, envs in pairs:
        val = settings.get(key)
        if val is None:
            continue
        try:
            val = float(val)
        except (TypeError, ValueError):
            continue
        # old v62 default -3.0 was the bug itself -> use the new default
        if key == "play_ceiling_db" and abs(val - (-3.0)) < 1e-6:
            continue
        for e in envs:
            os.environ[e] = str(val)


def install():
    if getattr(ap, "_playback_v63_installed", False):
        return
    ap._playback_v63_installed = True
    from . import playback_v62 as v62
    for name in ("playback_fight_stage", "playback_ceiling"):
        setattr(ap, name, globals()[name])
        setattr(v62, name, globals()[name])   # pro_max_master (live/bridge) uses these
