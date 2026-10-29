"""Playback v59: loud AND clear - stop pumping the noise floor up.

ROOT CAUSE ("audio play ki aawaz bohot kam lagti hai"):
v56-v58 stacked dynaudnorm (+11 dB) -> speechnorm (e=25, up to +28 dB)
-> 7 EQ boosts -> +20 dB drive into tanh.  Measured on real speech
(chain -> Opus 32k voip, the codec ntgcalls sends):
    pauses / hiss  : -45 dB  ->  -13 dB   (lifted +32 dB)
    speech         : -22 dB  ->   -7 dB
    speech-to-hiss : 23 dB   ->    6 dB   = voice buried in a wall of noise
    post-Opus peak : +2.6 dBFS (receiver clipping)
The file measured "-5 LUFS" but most of that energy was amplified hiss,
so the VOICE sounded far away / kam.  A bigger number never helped.

v59 chain: static lift -> denoise -> presence EQ -> one RMS compressor
-> drive into tanh -> downward expander (pauses stay quiet) -> true-peak
safe ceiling.  Same measurement: speech -8 dB, hiss -31 dB (22 dB gap),
-8.5 LUFS, post-Opus peak ~0 dBFS, ~0 % clip.
Live mic / VC bridge chains are untouched.
"""
from . import audio_processor as ap


def playback_voice_stage(bass=None, treble=None, gain=None, input_gain_db=0.0) -> list:
    out = []
    if input_gain_db and input_gain_db > 0.05:
        out.append(f"volume={ap._db(input_gain_db)}dB")
    if ap._has_filter("afftdn"):
        # tn=1: tracks the real noise floor, so hiss is removed BEFORE any gain.
        out.append("afftdn=nr=12:nf=-50:tn=1")
    g = ap.GAIN_MAX if gain is None else ap.clamp(int(gain), 0, ap.GAIN_MAX)
    pre = 3.0 * g / float(ap.GAIN_MAX)          # small, the compressor levels the rest
    if pre > 0.05:
        out.append(f"volume={ap._db(pre)}dB")
    b = ap.clamp(14 if bass is None else int(bass), ap.BASS_MIN, ap.BASS_MAX)
    if b:
        out.append(f"equalizer=f=140:t=q:w=1.1:g={min(4.0, b * 0.1):.1f}")
    out += ["equalizer=f=300:t=q:w=1:g=-3",      # mud cut
            "equalizer=f=2500:t=q:w=1.2:g=5"]    # presence = perceived loudness
    t = ap.clamp(100 if treble is None else int(treble), 0, 100)
    out.append(f"highshelf=f=5000:g={(t - 30) * 0.03:.1f}")
    return out


def playback_fight_stage(bass=None, boost=None, over=1.0) -> list:
    bo = ap.LEVEL_MAX if boost is None else ap.clamp(int(boost), ap.LEVEL_MIN, ap.LEVEL_MAX)
    drive = ap._env_db("PLAY_DRIVE_DB", 8.0 + 0.6 * bo, high=20.0, low=0.0)   # boost 10 -> 14 dB
    out = ["acompressor=threshold=0.05:ratio=6:attack=5:release=150:"
           "makeup=6:knee=6:detection=rms",
           f"volume={ap._db(drive)}dB"]
    if ap._has_filter("asoftclip"):
        out.append("asoftclip=type=tanh:oversample=4")
    if ap._has_filter("agate"):
        # AFTER the gain: pauses/hiss between words are pushed back down,
        # so the voice is the loudest thing in the VC, not the noise.
        out.append("agate=threshold=0.03:range=0.06:ratio=4:attack=5:"
                   "release=250:knee=4:detection=rms")
    out.append("lowpass=f=12000")
    return out


def playback_ceiling() -> list:
    lim = 10 ** (ap._env_db("PLAY_CEILING_DB", -2.5, high=-1.0, low=-6.0) / 20.0)
    return ["aresample=192000",
            f"alimiter=level_in=1:level_out=1:limit={lim:.4f}:attack=1:release=60:level=false",
            "aresample=48000",
            f"alimiter=level_in=1:level_out=1:limit={lim:.4f}:attack=0.1:release=40:level=false"]


def install():
    if getattr(ap, "_playback_v59_installed", False):
        return
    ap._playback_v59_installed = True
    for name in ("playback_voice_stage", "playback_fight_stage", "playback_ceiling"):
        setattr(ap, name, globals()[name])
