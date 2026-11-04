"""v80 BRIDGE ULTRA: VC bridge (live mic relay) — max tez + saaf.

Root causes left after v79 (measured: speech -> Opus -> PcmAgc -> chain ->
Opus 48k voip -> decode, phone band 300 Hz-4 kHz):
  1. Shabdon ke BEECH ki hiss / room noise: PcmAgc gate 600 ms hold ke
     liye khula rehta hai, aur aage ki +40 dB chain us noise ko bhi awaaz
     jitna tez kar deti thi -> awaaz "khar-khar", shabd dhundhle, aur
     limiter ki jagah noise kha jaata tha.
  2. Band bahut chauda (150 Hz-5 kHz): phone speaker/earpiece 330 Hz se
     neeche aur 4.3 kHz se upar kuch nahi bajata, par limiter ka headroom
     wahi energy khaati thi.
  3. Ek hi multiband squash -> syllable ke shuru/ant dheeme.

v80 fix (bridge only; played songs + web mic untouched):
  * RNNoise (arnndn) — speech ke liye bana neural denoiser, chain ke SABSE
    pehle: hiss/fan/room noise hatao, sirf awaaz aage jaaye.  Model file
    assets/rnnoise/sh.rnnn (repo me bundled, koi download nahi).
  * 330 Hz-4.3 kHz phone band (4x highpass, 2x lowpass) — saari energy
    wahan jahan sunne wale ka phone bajata hai.
  * speechnorm (har syllable full) -> fast comp -> 3-band squash
    (0 dB -> -1 dB) -> presence EQ -> aexciter (harmonics = cut-through)
    -> 2 stage tanh drive -> limiter 0.97.
  * Agar FFmpeg me arnndn/model na mile to afftdn fallback.

Env:
  BRIDGE_ULTRA=0           -> v79 chain wapas
  BRIDGE_RNNOISE=0         -> neural denoise band
  BRIDGE_ULTRA_DRIVE_DB    -> final push (0-36, default 18 + drive slider)
"""
import os

from . import audio_processor as ap

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RNN_MODEL = os.path.join(ROOT, "assets", "rnnoise", "sh.rnnn")
_orig_stage = None


def enabled() -> bool:
    return os.environ.get("BRIDGE_ULTRA", "1") != "0"


def denoise_filter() -> str:
    """Speech denoiser: RNNoise if available, else FFT denoise."""
    if os.environ.get("BRIDGE_RNNOISE", "1") != "0" and ap._has_filter("arnndn") \
            and os.path.isfile(RNN_MODEL) and ":" not in RNN_MODEL and "'" not in RNN_MODEL:
        return f"arnndn=m={RNN_MODEL}:mix=0.9"
    if ap._has_filter("afftdn"):
        return "afftdn=nr=12:nf=-45:tn=1"
    return ""


def bridge_ultra_stage(cfg: dict, focus: bool = False) -> str:
    from . import vc_bridge as vb
    if not enabled():
        return _orig_stage(cfg, focus)
    c = vb.clean_loud(cfg)
    f = []
    dn = denoise_filter()
    if dn:
        f.append(dn)
    hp = 330 - c["bass"] * 15            # bass slider 10 -> 180 Hz (body)
    if focus:
        hp = max(hp, 330)
    f += [f"highpass=f={hp}:p=2"] * 4
    f += ["lowpass=f=4300:p=2"] * 2
    if c["bass"]:
        f.append(f"equalizer=f={hp + 60}:t=q:w=1:g={min(4.0, c['bass'] * 0.4):.1f}")
    if ap._has_filter("speechnorm"):
        f.append("speechnorm=e=25:r=0.001:l=1:p=0.95")
    f.append("acompressor=threshold=0.06:ratio=8:attack=2:release=60:makeup=4:knee=3")
    if ap._has_filter("mcompand"):
        f.append("mcompand=args='0.002\\,0.08 6 -60/-20\\,-30/-6\\,0/-1 1000 | "
                 "0.002\\,0.06 6 -60/-18\\,-30/-4\\,0/-1 2400 | "
                 "0.001\\,0.04 6 -60/-16\\,-30/-3\\,0/-1 22000'")
    pres = 5 + c["presence"] * 0.4       # presence 10 -> +9 dB @ 2.7 kHz
    f += ["equalizer=f=1000:t=q:w=1:g=3",
          f"equalizer=f=2700:t=q:w=0.9:g={pres:.1f}"]
    if ap._has_filter("aexciter"):
        f.append("aexciter=amount=3:drive=6:blend=0:freq=2000:ceil=9999")
    drive = ap._env_db("BRIDGE_ULTRA_DRIVE_DB",
                       18.0 + min(8.0, vb.drive_db(c["drive"]) * 0.25), high=36.0, low=0.0)
    clip = ("asoftclip=type=tanh:oversample=4" if ap._has_filter("asoftclip") else
            "alimiter=level_in=1:level_out=1:limit=0.9:attack=0.1:release=5:level=false")
    f += [f"volume={ap._db(drive)}dB", clip, "lowpass=f=4500:p=2",
          "volume=3dB", clip, "lowpass=f=4500:p=2",
          "alimiter=level_in=1:level_out=1:limit=0.89:attack=0.5:release=30:level=false"]
    return ",".join(f)


def install():
    global _orig_stage
    from . import vc_bridge as vb
    if getattr(vb, "_v80_installed", False):
        return
    vb._v80_installed = True
    _orig_stage = vb.loud_stage
    vb.loud_stage = bridge_ultra_stage
