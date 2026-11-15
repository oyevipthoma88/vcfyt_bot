"""Playback v62: ULTRA PRO MAX fight chain - dense, bright, never buried.

Bench of v61 (user record.ogg -> chain -> Opus 48k voip -> decode):
  -3.8 LUFS but true peak +1.8 dBFS and ~6.7 % of samples clipped at the
  receiver.  Telegram/WebRTC AGC sees those overs, pulls the whole stream
  down (user recording sat at -10.7 LUFS) and the crunch reads as "dabi".
  Spectrum of the recording: 150-600 Hz ~ -5 dB of total energy, 2.5-5 kHz
  -12 dB, 5-8 kHz -24 dB -> muddy, far-away voice next to other fighters.

v62 fixes both:
  * 4-band multiband compressor (mcompand) - every band pinned hot, so
    the voice is dense like a broadcast/radio chain, low-mids can no
    longer eat the limiter.
  * stronger mud cut + presence / bite at 2.5-4.5 kHz (where the ear and
    phone speakers are most sensitive = loudest-sounding band).
  * Opus-aware ceiling: true-peak style limiting at 192 kHz, ceiling
    -3.0 dBFS so Opus decode stays below 0 dBFS -> no AGC duck, no crackle.
Live mic / bridge untouched.
"""
from . import audio_processor as ap


def playback_voice_stage(bass=None, treble=None, gain=None, input_gain_db=0.0) -> list:
    out = []
    if input_gain_db and input_gain_db > 0.05:
        out.append(f"volume={ap._db(input_gain_db)}dB")
    if ap._has_filter("afftdn"):
        out.append("afftdn=nr=12:nf=-50:tn=1")
    out.append("highpass=f=110:p=2")
    if ap._has_filter("dynaudnorm"):
        out.append("dynaudnorm=f=150:g=11:p=0.8:m=30:t=0.01")
    g = ap.GAIN_MAX if gain is None else ap.clamp(int(gain), 0, ap.GAIN_MAX)
    pre = 2.0 * g / float(ap.GAIN_MAX)
    if pre > 0.05:
        out.append(f"volume={ap._db(pre)}dB")
    b = ap.clamp(14 if bass is None else int(bass), ap.BASS_MIN, ap.BASS_MAX)
    if b:
        out.append(f"equalizer=f=150:t=q:w=1.2:g={min(2.0, b * 0.05):.1f}")
    out += ["equalizer=f=300:t=q:w=0.9:g=-6",            # mud / box out
            "equalizer=f=550:t=q:w=1.0:g=-3",
            "equalizer=f=1200:t=q:w=1.0:g=2",
            "equalizer=f=2600:t=q:w=1.1:g=7",            # presence = perceived loudness
            "equalizer=f=4200:t=q:w=1.2:g=5"]            # bite, cuts through fighters
    t = ap.clamp(100 if treble is None else int(treble), 0, 100)
    out.append(f"highshelf=f=6000:g={(t - 30) * 0.05:.1f}")
    if ap._has_filter("aexciter"):
        out.append("aexciter=level_in=1:level_out=1:amount=2:drive=7:blend=0:freq=2500:ceil=9999")
    return out


MCOMPAND = ("mcompand='"
            "0.005,0.08 6 -47/-40,-34/-24,-12/-10,0/-8 250 | "
            "0.003,0.06 6 -47/-34,-34/-18,-12/-8,0/-6 1200 | "
            "0.002,0.05 6 -47/-30,-34/-15,-12/-6,0/-5 5000 | "
            "0.002,0.05 6 -47/-38,-34/-24,-12/-12,0/-10 20000'")


def playback_fight_stage(bass=None, boost=None, over=1.0) -> list:
    bo = ap.LEVEL_MAX if boost is None else ap.clamp(int(boost), ap.LEVEL_MIN, ap.LEVEL_MAX)
    drive = ap._env_db("PLAY_DRIVE_DB", 4.0 + 0.6 * bo, high=24.0, low=0.0)   # boost 10 -> 10 dB
    out = ["acompressor=threshold=0.06:ratio=5:attack=5:release=120:"
           "makeup=3:knee=6:detection=rms"]
    if ap._has_filter("mcompand"):
        out.append(MCOMPAND)
    out += ["acompressor=threshold=0.2:ratio=12:attack=0.5:release=50:"
            "makeup=2:knee=3:detection=peak",
            f"volume={ap._db(drive)}dB"]
    if ap._has_filter("asoftclip"):
        out.append("asoftclip=type=tanh:threshold=0.9:oversample=8")
    if ap._has_filter("agate"):
        out.append("agate=threshold=0.008:range=0.25:ratio=2:attack=3:"
                   "release=300:knee=6:detection=rms")
    if ap._has_filter("deesser"):
        out.append("deesser=i=0.35:m=0.5:f=0.5:s=o")
    out.append("lowpass=f=11500")
    return out


def playback_ceiling(env: str = "PLAY_CEILING_DB", default: float = -3.0) -> list:
    lim = 10 ** (ap._env_db(env, default, high=-0.5, low=-6.0) / 20.0)
    return ["aresample=192000",
            f"alimiter=level_in=1:level_out=1:limit={lim:.4f}:attack=2:release=60:level=false",
            f"alimiter=level_in=1:level_out=1:limit={lim:.4f}:attack=0.1:release=40:level=false",
            "aresample=48000"]


# Shared legacy master retained because playback v73 installs it through
# audio_processor. Bridge-specific stage patching was removed.
def pro_max_master(drive_db: float = None, push_db: float = None,
                   leveller: bool = True) -> list:
    drive = ap._env_db("MASTER_DRIVE_DB", 12.0 if drive_db is None else drive_db,
                       high=24.0, low=0.0)
    out = []
    if leveller:
        if ap._has_filter("speechnorm"):
            out.append("speechnorm=e=10:r=0.0005:l=1:p=0.7")
        else:
            out.append("dynaudnorm=f=150:g=15:p=0.7:m=10")
    out += ["equalizer=f=300:t=q:w=0.9:g=-6",
            "equalizer=f=550:t=q:w=1.0:g=-2",
            "equalizer=f=2600:t=q:w=1.1:g=6",
            "equalizer=f=4200:t=q:w=1.2:g=4",
            "highshelf=f=6000:g=2"]
    if ap._has_filter("aexciter"):
        out.append("aexciter=level_in=1:level_out=1:amount=1.5:drive=6:blend=0:freq=2500:ceil=9999")
    fs = playback_fight_stage()
    fs = [f"volume={ap._db(drive)}dB" if x.startswith("volume=") else x for x in fs]
    out += fs
    out += playback_ceiling("MASTER_CEILING_DB", float(ap._env_db("PLAY_CEILING_DB", -3.0)))
    return out


def install():
    if getattr(ap, "_playback_v62_installed", False):
        return
    ap._playback_v62_installed = True
    for name in ("playback_voice_stage", "playback_fight_stage", "playback_ceiling"):
        setattr(ap, name, globals()[name])

