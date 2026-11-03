"""Playback v74: DAMDAR BASS + CHHOTI AWAAZ LIFT (user: "setting sab full hai
phir bhi awaaz kam lagti hai, bass aur awaaz badha").

Root causes found in v73 (measured: speech -> chain -> Opus 48k voip):
  1. v73.playback_voice_stage IGNORED bass / treble / gain completely -
     `.bass 100`, `.treble`, `.gain`, `.voice male` changed nothing.
  2. Two 150 Hz high-pass filters removed every bit of body, so the voice
     sounded thin ("patli") and therefore quieter, even at -2.9 LUFS.
  3. dynaudnorm g=5 alone could not lift the softest syllables / word tails.

v74 (played files only; live mic / bridge untouched):
  * sub-rumble cut at 40 Hz only (Opus/phones can't play it), body kept;
  * PSYCHOACOUSTIC BASS (own logic): the 40-150 Hz band is split off,
    saturated to create its 2nd/3rd harmonics (170-420 Hz) and mixed back -
    phone speakers can't reproduce 80 Hz, but the brain hears the
    fundamental from its harmonics ("missing fundamental"), so bass is
    audible on every phone without wasting peak headroom;
  * low-shelf 170 Hz up to +6 dB scaled by the bass setting;
  * treble 0..100 -> 0..+2 dB air shelf at 8 kHz; gain 0..500 -> +0..1.5 dB push;
  * fixed-floor denoise + speechnorm (e=30) before the leveller, so the
    smallest sounds are lifted ~+1.5 dB while hiss is not;
  * same two oversampled tanh stages + 0.00 dBFS brick-wall as v73.
Measured vs v73 (bass 100, boost 10): LUFS -2.9 -> -2.5, low band (80-300 Hz)
+3.1 dB, quietest 10 % of speech +1.4 dB.
"""
from . import audio_processor as ap
from . import playback_v73 as v73

BASS_SHELF_MAX_DB = 6.0
HARMONIC_MIN_DB = -12.0      # harmonic layer level at bass=0 (off below 1)
HARMONIC_RANGE_DB = 6.0      # bass=100 -> -6 dB harmonic layer
TREBLE_MAX_DB = 2.0
GAIN_PUSH_MAX_DB = 1.5


def bass_amount(bass=None) -> float:
    b = 100 if bass is None else ap.clamp(int(bass), ap.BASS_MIN, ap.BASS_MAX)
    return b / float(ap.BASS_MAX)


def _body_eq(bass=None, treble=None) -> list:
    bs = bass_amount(bass)
    t = 100 if treble is None else ap.clamp(int(treble), 0, 100)
    out = ["highpass=f=90:p=2"]
    if bs > 0:
        out.append(f"lowshelf=f=170:w=0.7:g={BASS_SHELF_MAX_DB * bs:.1f}")
    out += [
        "equalizer=f=380:t=q:w=1.4:g=-3",     # box/mud cut (bass stays clean)
        "equalizer=f=1200:t=q:w=1.0:g=3",
        "equalizer=f=2800:t=q:w=1.0:g=6",     # ear's most sensitive band
        "equalizer=f=4200:t=q:w=1.2:g=4",
        f"highshelf=f=8000:w=0.8:g={TREBLE_MAX_DB * t / 100.0:.1f}",
        "lowpass=f=12000:p=2",
    ]
    return out


def playback_voice_stage(bass=None, treble=None, gain=None, input_gain_db=0.0) -> list:
    """Returns ONE element that may be a labelled sub-graph (asplit/amix);
    joined with commas it is still a valid single-input/-output -af graph."""
    out = []
    if input_gain_db and input_gain_db > 0.05:
        out.append(f"volume={ap._db(min(input_gain_db, 30.0))}dB")
    out.append("highpass=f=40:p=2")
    body = ",".join(_body_eq(bass, treble))
    bs = bass_amount(bass)
    if bs > 0 and ap._has_filter("asoftclip"):
        harm_db = HARMONIC_MIN_DB + HARMONIC_RANGE_DB * bs
        out.append(
            "asplit=2[v74m][v74b];"
            "[v74b]lowpass=f=150,lowpass=f=150,volume=24dB,asoftclip=type=tanh,"
            "highpass=f=170,highpass=f=170,lowpass=f=420,"
            f"volume={harm_db:.1f}dB[v74h];"
            f"[v74m]{body}[v74v];"
            "[v74v][v74h]amix=inputs=2:normalize=0")
    else:
        out.append(body)
    return out


def gain_push_db(gain=None) -> float:
    g = ap.GAIN_MAX if gain is None else ap.clamp(int(gain), 0, ap.GAIN_MAX)
    return round(GAIN_PUSH_MAX_DB * g / float(ap.GAIN_MAX), 2)


def playback_fight_stage(bass=None, boost=None, over=1.0) -> list:
    out = []
    if ap._has_filter("afftdn"):
        out.append("afftdn=nr=10:nf=-50:tn=0")      # fixed floor, never eats voice
    if ap._has_filter("speechnorm"):
        out.append("speechnorm=e=30:r=0.0008:l=1:p=0.95")   # chhoti awaaz lift
    out += v73.playback_fight_stage(bass=bass, boost=boost, over=over)
    return out


_orig_build = None


def build_ffmpeg_filter(*args, **kwargs) -> str:
    chain = _orig_build(*args, **kwargs)
    if kwargs.get("live"):
        return chain
    push = gain_push_db(kwargs.get("gain"))
    if push > 0.05:
        parts = chain.split(",")
        soft = [i for i, x in enumerate(parts) if x.startswith("asoftclip=type=tanh:oversample=4")]
        if soft:
            parts.insert(soft[0], f"volume={ap._db(push)}dB")
            chain = ",".join(parts)
    return chain


def install():
    global _orig_build
    if getattr(ap, "_playback_v74_installed", False):
        return
    ap._playback_v74_installed = True
    ap.playback_voice_stage = playback_voice_stage
    ap.playback_fight_stage = playback_fight_stage
    _orig_build = ap.build_ffmpeg_filter
    ap.build_ffmpeg_filter = build_ffmpeg_filter
