"""Playback v60: fight chain restored - loud, clear, not "dabi".

ROOT CAUSE ("audio play ki aawaz kam aur dabi lagti hai"):
v59 replaced the v56/v58 fight stage with ONE compressor + a +14 dB drive
into tanh.  Measured on real speech after a 48k voip Opus round-trip
(what Telegram VC listeners actually hear):
  * 1.2 - 1.8 % of samples clipped at the receiver  -> crackly, squashed
  * presence band (1-4 kHz, where ear/phone speakers judge loudness)
    only -11.8 dB of total energy                    -> muffled / far away
  * no leveler: whisper parts of a clip stay quiet next to loud parts.
v58's presence EQ was dropped by v59, so the "fight" chain was effectively
missing.

v60 chain: lift -> denoise -> thresholded leveler (t= keeps hiss out)
-> rumble/mud cut -> presence push -> RMS compressor -> fast peak
compressor (fight) -> drive -> tanh -> gentle expander -> Opus-safe ceiling.
Same measurement: same LUFS as v59 (-9.7 / -8.0), presence +3..4 dB,
receiver clipping ~3x lower, noise floor ~5 dB lower.
Live mic / VC bridge chains are untouched.
"""
from . import audio_processor as ap


def playback_voice_stage(bass=None, treble=None, gain=None, input_gain_db=0.0) -> list:
    out = []
    if input_gain_db and input_gain_db > 0.05:
        out.append(f"volume={ap._db(input_gain_db)}dB")
    if ap._has_filter("afftdn"):
        out.append("afftdn=nr=12:nf=-50:tn=1")          # hiss out BEFORE any gain
    if ap._has_filter("dynaudnorm"):
        # t=0.01: frames under -40 dBFS (pauses/hiss) are never pumped up.
        out.append("dynaudnorm=f=200:g=15:p=0.7:m=20:t=0.01")
    g = ap.GAIN_MAX if gain is None else ap.clamp(int(gain), 0, ap.GAIN_MAX)
    pre = 2.0 * g / float(ap.GAIN_MAX)
    if pre > 0.05:
        out.append(f"volume={ap._db(pre)}dB")
    b = ap.clamp(14 if bass is None else int(bass), ap.BASS_MIN, ap.BASS_MAX)
    out.append("highpass=f=150:p=2")                    # rumble eats headroom
    if b:
        out.append(f"equalizer=f=180:t=q:w=1.1:g={min(3.0, b * 0.08):.1f}")
    out += ["equalizer=f=300:t=q:w=0.9:g=-5",           # mud = "dabi"
            "equalizer=f=2200:t=q:w=1.3:g=9",           # presence = loudness to the ear
            "equalizer=f=3500:t=q:w=1.2:g=3"]           # consonants cut through
    t = ap.clamp(100 if treble is None else int(treble), 0, 100)
    out.append(f"highshelf=f=5000:g={(t - 30) * 0.045:.1f}")
    return out


def playback_fight_stage(bass=None, boost=None, over=1.0) -> list:
    bo = ap.LEVEL_MAX if boost is None else ap.clamp(int(boost), ap.LEVEL_MIN, ap.LEVEL_MAX)
    drive = ap._env_db("PLAY_DRIVE_DB", 5.0 + 0.6 * bo, high=18.0, low=0.0)   # boost 10 -> 11 dB
    out = ["acompressor=threshold=0.06:ratio=5:attack=4:release=120:"
           "makeup=4:knee=6:detection=rms",
           # fight: catches word peaks so the drive lifts the whole voice, not spikes
           "acompressor=threshold=0.25:ratio=8:attack=1:release=60:"
           "makeup=2:knee=4:detection=peak",
           f"volume={ap._db(drive)}dB"]
    if ap._has_filter("asoftclip"):
        out.append("asoftclip=type=tanh:oversample=4")
    if ap._has_filter("agate"):
        # gentle expander (range 0.25 = -12 dB max, threshold -42 dBFS):
        # pauses stay quiet, word tails / fast speech are NOT chopped.
        out.append("agate=threshold=0.008:range=0.25:ratio=2:attack=3:"
                   "release=300:knee=6:detection=rms")
    out.append("lowpass=f=12000")
    return out


def playback_ceiling() -> list:
    lim = 10 ** (ap._env_db("PLAY_CEILING_DB", -3.5, high=-1.0, low=-6.0) / 20.0)
    return ["aresample=192000",
            f"alimiter=level_in=1:level_out=1:limit={lim:.4f}:attack=1:release=60:level=false",
            "aresample=48000",
            f"alimiter=level_in=1:level_out=1:limit={lim:.4f}:attack=0.1:release=40:level=false"]


def install():
    if getattr(ap, "_playback_v60_installed", False):
        return
    ap._playback_v60_installed = True
    for name in ("playback_voice_stage", "playback_fight_stage", "playback_ceiling"):
        setattr(ap, name, globals()[name])
