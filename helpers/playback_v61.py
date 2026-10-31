"""Playback v61: ULTRA fight chain - sabse tez + clear, kisi fighter ke niche nahi dabti.

ROOT CAUSE ("aawaz kam lag rahi hai", user VC recording, Oct 2026):
The recording measured -10.7 LUFS, peak -0.8 dBFS, crest ~10 dB, and its
energy sat in 150-500 Hz (-2.6 dB of total) while 4-8 kHz was -21 dB.
Bench of v60 (record -> chain -> Opus 48k voip -> decode) reproduced it
exactly: -10.4 LUFS.  Two causes:
  1. v60 ceiling -3.5 dBFS + only ~11 dB drive = 3.5 dB of headroom
     wasted, the voice could never get as hot as a fighter on PC / RDP
     whose chain sits at -6..-7 LUFS.
  2. Boxy low-mids (250-500 Hz) ate the limiter headroom; almost no
     consonant/air band, so on phone speakers it sounded far away.

v61 chain (playback only, live mic / bridge untouched):
  lift -> denoise -> highpass 120 -> leveler -> de-box EQ (-5 dB @280,
  -3 dB @500) -> presence (+7 @2k, +5 @3.2k) -> air shelf -> harmonic
  exciter (clarity on cheap speakers, no extra peak) -> RMS compressor ->
  fast peak compressor -> hot drive -> oversampled tanh clipper ->
  2nd peak catch -> gentle expander -> de-esser -> Opus-safe ceiling at
  -1.5 dBFS (oversampled at 192 kHz, true-peak safe after Opus).
Measured on the user's recording after Opus: see tests/test_v61_ultra_fight.py.
"""
from . import audio_processor as ap


def playback_voice_stage(bass=None, treble=None, gain=None, input_gain_db=0.0) -> list:
    out = []
    if input_gain_db and input_gain_db > 0.05:
        out.append(f"volume={ap._db(input_gain_db)}dB")
    if ap._has_filter("afftdn"):
        out.append("afftdn=nr=12:nf=-50:tn=1")           # hiss out BEFORE any gain
    out.append("highpass=f=120:p=2")                     # rumble eats headroom
    if ap._has_filter("dynaudnorm"):
        out.append("dynaudnorm=f=150:g=11:p=0.8:m=25:t=0.01")
    g = ap.GAIN_MAX if gain is None else ap.clamp(int(gain), 0, ap.GAIN_MAX)
    pre = 2.0 * g / float(ap.GAIN_MAX)
    if pre > 0.05:
        out.append(f"volume={ap._db(pre)}dB")
    b = ap.clamp(14 if bass is None else int(bass), ap.BASS_MIN, ap.BASS_MAX)
    if b:
        out.append(f"equalizer=f=160:t=q:w=1.2:g={min(2.5, b * 0.06):.1f}")   # body, not boom
    out += ["equalizer=f=280:t=q:w=1.0:g=-5",           # box / mud = "dabi"
            "equalizer=f=500:t=q:w=1.0:g=-3",
            "equalizer=f=2000:t=q:w=1.2:g=7",           # presence = loudness to the ear
            "equalizer=f=3200:t=q:w=1.3:g=5"]           # consonants cut over other mics
    t = ap.clamp(100 if treble is None else int(treble), 0, 100)
    out.append(f"highshelf=f=5500:g={(t - 30) * 0.05:.1f}")
    if ap._has_filter("aexciter"):
        # adds upper harmonics -> clarity / bite on phone + laptop speakers
        out.append("aexciter=level_in=1:level_out=1:amount=1.5:drive=6:blend=0:freq=2500:ceil=9999")
    return out


def playback_fight_stage(bass=None, boost=None, over=1.0) -> list:
    bo = ap.LEVEL_MAX if boost is None else ap.clamp(int(boost), ap.LEVEL_MIN, ap.LEVEL_MAX)
    drive = ap._env_db("PLAY_DRIVE_DB", 8.0 + 0.6 * bo, high=20.0, low=0.0)   # boost 10 -> 14 dB
    out = ["acompressor=threshold=0.05:ratio=6:attack=4:release=100:"
           "makeup=4:knee=6:detection=rms",
           "acompressor=threshold=0.2:ratio=12:attack=0.5:release=50:"
           "makeup=2:knee=3:detection=peak",
           f"volume={ap._db(drive)}dB"]
    if ap._has_filter("asoftclip"):
        out.append("asoftclip=type=tanh:oversample=4")
    out.append("acompressor=threshold=0.5:ratio=20:attack=0.1:release=30:"
               "makeup=1.6:knee=2:detection=peak")
    if ap._has_filter("agate"):
        out.append("agate=threshold=0.008:range=0.25:ratio=2:attack=3:"
                   "release=300:knee=6:detection=rms")
    if ap._has_filter("deesser"):
        out.append("deesser=i=0.3:m=0.5:f=0.5:s=o")      # harsh "sss" tamed after the drive
    out.append("lowpass=f=12000")
    return out


def playback_ceiling() -> list:
    lim = 10 ** (ap._env_db("PLAY_CEILING_DB", -1.5, high=-1.0, low=-6.0) / 20.0)
    return ["aresample=192000",
            f"alimiter=level_in=1:level_out=1:limit={lim:.4f}:attack=1:release=50:level=false",
            "aresample=48000",
            f"alimiter=level_in=1:level_out=1:limit={lim:.4f}:attack=0.1:release=30:level=false"]


def install():
    if getattr(ap, "_playback_v61_installed", False):
        return
    ap._playback_v61_installed = True
    for name in ("playback_voice_stage", "playback_fight_stage", "playback_ceiling"):
        setattr(ap, name, globals()[name])
