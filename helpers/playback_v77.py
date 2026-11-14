"""Playback v77: PHONE-MAX ("group admin ho ya na ho, aawaz aur tez + clear").

Root cause measured on v75 (speech -> chain -> Opus 48k voip -> decode,
all settings max):
    full-band LUFS -4.2, but PHONE BAND (300 Hz - 4 kHz) only -10.1 LUFS,
    16 % samples clipped, crest 3.6 dB.
The signal was already at 0 dBFS - no digital headroom left - but most of
that energy was v74/v75 sub-bass (virtualbass + 24 dB bass branch).  Phone
speakers / earpieces (where almost every listener hears the VC) cannot play
< 250 Hz, so that energy only used up the headroom and made the voice
smaller.

v77 "phone" mode (default, played files only; live mic untouched):
  mono -> 3x highpass 280 Hz -> denoise -> leveller -> 10:1 comp ->
  presence EQ (1.2 kHz +3, 2.6 kHz +7) -> lowpass 6 kHz ->
  +30 dB into tanh -> +8 dB into tanh -> limiter 0.97 -> dual-mono.
Measured: phone band -10.1 -> -6.9 LUFS (+3.2 dB, ~1.45x louder on a
phone speaker), clipping 16 % -> 9.6 % (clearer words).

Mode "bass" = old v75 chain (heavy bass for headphones).
v78 "loudest" mode (new default): 330 Hz-4.3 kHz only, 3-band squash
(mcompand), +9 dB presence, harmonic exciter, harder double tanh drive.
Measured on music after Opus: phone band -2.7 -> -1.6 LUFS (+1.1 dB).
Env PLAY_LOUD_MODE=loudest|phone|bass sets the default; `.loudmode` changes it live.
"""
import math
import os

from . import audio_processor as ap

MODES = ("loudest", "phone", "bass")
_mode = None
_orig_build = None


def get_mode() -> str:
    global _mode
    if _mode is None:
        m = os.environ.get("PLAY_LOUD_MODE", "loudest").strip().lower()
        _mode = m if m in MODES else "loudest"
    return _mode


def set_mode(mode: str) -> str:
    global _mode
    mode = (mode or "").strip().lower()
    if mode not in MODES:
        raise ValueError(mode)
    _mode = mode
    return _mode


def phone_drive_db(boost=None) -> float:
    bo = ap.LEVEL_MAX if boost is None else ap.clamp(int(boost), ap.LEVEL_MIN, ap.LEVEL_MAX)
    return ap._env_db("PLAY_PHONE_DRIVE_DB", 20.0 + bo, high=36.0, low=0.0)


def phone_chain(volume=None, echo=None, echo_level=None, boost=None,
                treble=None, bass=None, gain=None, input_gain_db: float = 0.0,
                extra_filters: str = "") -> str:
    # bass/gain accepted (and ignored: phone mode cuts < 280 Hz on purpose) so
    # build_ffmpeg_filter can pass the same kwargs to every mode. Old signature
    # raised TypeError -> "Phone" mode me har play crash hota tha.
    del bass, gain
    f = ["aresample=48000", "pan=mono|c0=0.5*c0+0.5*c1"]
    f += ["highpass=f=280:p=2"] * 3
    if input_gain_db and input_gain_db > 0.05:
        f.append(f"volume={ap._db(input_gain_db)}dB")
    if ap._has_filter("afftdn"):
        f.append("afftdn=nr=10:nf=-50")
    if ap._has_filter("speechnorm"):
        f.append("speechnorm=e=30:r=0.0008:l=1:p=0.95")
    else:
        f.append("dynaudnorm=f=75:g=5:p=0.95:m=50")
    f.append("acompressor=threshold=0.05:ratio=10:attack=1:release=50:makeup=6:knee=2")
    t = ap.clamp(100 if treble is None else int(treble), 0, 100)
    f += ["equalizer=f=1200:t=q:w=1:g=3",
          f"equalizer=f=2600:t=q:w=1:g={4 + 3 * t / 100:.1f}",
          "lowpass=f=6000:p=2"]
    el = max(0, min(ap.LEVEL_MAX, int(echo_level or 0)))
    if echo and el:
        f.append(f"aecho=0.8:0.85:{40 + el * 15}:{0.12 + el * 0.04:.2f}")
    drive = phone_drive_db(boost)
    m = ap.re.match(r"^volume=(\d+(?:\.\d+)?)dB$", (extra_filters or "").strip())
    if m:
        drive = min(36.0, drive + min(6.0, float(m.group(1)) / 3))
    clip = ("asoftclip=type=tanh:oversample=4" if ap._has_filter("asoftclip") else
            "alimiter=level_in=1:level_out=1:limit=0.9:attack=0.1:release=5:level=false")
    f += [f"volume={ap._db(drive)}dB", clip, "volume=8dB", clip,
          "pan=stereo|c0=c0|c1=c0",
          "alimiter=level_in=1:level_out=1:limit=0.97:attack=0.1:release=20:level=false"]
    v = 1000 if volume is None else max(0, int(volume))
    if os.environ.get("PLAY_ALLOW_QUIET", "0") != "1":
        v = max(v, ap.VOLUME_CLEAN_MAX)
    if v == 0:
        f.append("volume=0")
    elif v < ap.VOLUME_CLEAN_MAX:
        f.append(f"volume={20 * math.log10(v / ap.VOLUME_CLEAN_MAX):.2f}dB")
    return ",".join(f)


def loudest_chain(volume=None, echo=None, echo_level=None, boost=None,
                  treble=None, bass=None, gain=None, input_gain_db=0.0, extra_filters=""):
    f = ["aresample=48000", "pan=mono|c0=0.5*c0+0.5*c1"]
    f += ["highpass=f=330:p=2"] * 4
    f += ["lowpass=f=4300:p=2"] * 2
    if input_gain_db and input_gain_db > 0.05:
        f.append(f"volume={ap._db(input_gain_db)}dB")
    if ap._has_filter("afftdn"):
        f.append("afftdn=nr=10:nf=-50")
    if ap._has_filter("speechnorm"):
        f.append("speechnorm=e=30:r=0.001:l=1:p=0.95")
    else:
        f.append("dynaudnorm=f=50:g=3:p=0.95:m=100")
    b = 50 if bass is None else ap.clamp(int(bass), ap.BASS_MIN, ap.BASS_MAX)
    t = 100 if treble is None else ap.clamp(int(treble), 0, 100)
    g = 0 if gain is None else ap.clamp(int(gain), 0, ap.GAIN_MAX)
    f.append(f"equalizer=f=150:t=q:w=1.2:g={1.0 + (b / 100.0) * 8.0:.1f}")
    f.append(f"highshelf=f=6000:g={(t - 50) / 25.0:.1f}")
    if g > 0:
        f.append(f"volume={g * 4.0 / ap.GAIN_MAX:.2f}dB")
    # 3-band squash: every band pushed to the ceiling separately
    f.append("mcompand=args='0.002\\,0.08 6 -60/-20\\,-30/-6\\,0/-1 1000 | 0.002\\,0.06 6 -60/-18\\,-30/-4\\,0/-1 2400 | 0.001\\,0.04 6 -60/-16\\,-30/-3\\,0/-1 22000'")
    t = ap.clamp(100 if treble is None else int(treble), 0, 100)
    f += ["equalizer=f=1000:t=q:w=1:g=3",
          f"equalizer=f=2700:t=q:w=0.9:g={5 + 4 * t / 100:.1f}"]
    if ap._has_filter("aexciter"):
        f.append("aexciter=amount=3:drive=6:blend=0:freq=2000:ceil=9999")
    el = max(0, min(ap.LEVEL_MAX, int(echo_level or 0)))
    if echo and el:
        f.append(f"aecho=0.8:0.85:{40 + el * 15}:{0.12 + el * 0.04:.2f}")
    bo = ap.LEVEL_MAX if boost is None else ap.clamp(int(boost), ap.LEVEL_MIN, ap.LEVEL_MAX)
    drive = ap._env_db("PLAY_LOUDEST_DRIVE_DB", 18.0 + bo, high=36.0, low=0.0)
    clip = ("asoftclip=type=tanh:oversample=4" if ap._has_filter("asoftclip") else
            "alimiter=level_in=1:level_out=1:limit=0.9:attack=0.1:release=5:level=false")
    f += [f"volume={ap._db(drive)}dB", clip, "lowpass=f=4500", "volume=6dB", clip,
          "pan=stereo|c0=c0|c1=c0",
          "alimiter=level_in=1:level_out=1:limit=0.97:attack=0.1:release=20:level=false"]
    v = 1000 if volume is None else max(0, int(volume))
    if os.environ.get("PLAY_ALLOW_QUIET", "0") != "1":
        v = max(v, ap.VOLUME_CLEAN_MAX)
    if v == 0:
        f.append("volume=0")
    elif v < ap.VOLUME_CLEAN_MAX:
        f.append(f"volume={20 * math.log10(v / ap.VOLUME_CLEAN_MAX):.2f}dB")
    return ",".join(f)


def build_ffmpeg_filter(*args, **kwargs) -> str:
    if kwargs.get("live") or args or get_mode() == "bass":
        return _orig_build(*args, **kwargs)
    chain = loudest_chain if get_mode() == "loudest" else phone_chain
    return chain(volume=kwargs.get("volume"), echo=kwargs.get("echo"),
                 echo_level=kwargs.get("echo_level"), boost=kwargs.get("boost"),
                 treble=kwargs.get("treble"), bass=kwargs.get("bass"),
                 gain=kwargs.get("gain"),
                 input_gain_db=kwargs.get("input_gain_db") or 0.0,
                 extra_filters=kwargs.get("extra_filters") or "")


def install():
    global _orig_build
    if getattr(ap, "_playback_v77_installed", False):
        return
    ap._playback_v77_installed = True
    _orig_build = ap.build_ffmpeg_filter
    ap.build_ffmpeg_filter = build_ffmpeg_filter
