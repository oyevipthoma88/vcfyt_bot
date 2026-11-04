"""v79 BRIDGE PHONE-FIGHT: VC bridge (live mic relay) ki aawaz tez + saaf.

Root cause (measured, speech -> PcmAgc -> bridge chain -> Opus 48k voip):
    v62 bridge chain (pro_max_master) = phone band (300 Hz-4 kHz) sirf
    ~-9.5 LUFS, jabki played audio (.loudmode loudest) ~-2 LUFS.  Bridge
    ~7-8 dB DHEEMA tha.  Wajah: pro_max_master ka mcompand har band ko
    0 dB -> -5..-10 dB par map karta tha (downward squash, net level loss)
    aur 120 Hz tak ka bass headroom kha jaata tha jo phone speaker pe
    bajta hi nahi.

v79 (sirf bridge; played files + web mic untouched):
    highpass 300 Hz x3 -> speechnorm -> fast comp -> 3-band squash
    (0 dB -> -1 dB, level loss nahi) -> presence EQ 1.1k / 2.8k ->
    exciter -> 2x drive into tanh (lowpass 4.8 kHz) -> limiter 0.97.
Measured: phone band -9.6 -> -5.5 LUFS after Opus (~+4 dB), clip 0 %.
Env BRIDGE_PHONE_FIGHT=0 se purana v62 chain wapas.
Env BRIDGE_FIGHT_DRIVE_DB (0-30) final push badalta hai.
"""
import os

from . import audio_processor as ap

_orig_stage = None


def enabled() -> bool:
    return os.environ.get("BRIDGE_PHONE_FIGHT", "1") != "0"


def bridge_fight_stage(cfg: dict, focus: bool = False) -> str:
    from . import vc_bridge as vb
    if not enabled():
        return _orig_stage(cfg, focus)
    c = vb.clean_loud(cfg)
    hp = 300 - c["bass"] * 12           # bass 10 -> 180 Hz (thoda body)
    if focus:
        hp = max(hp, 330)
    f = [f"highpass=f={hp}:p=2"] * 3
    if c["bass"]:
        f.append(f"equalizer=f={hp + 60}:t=q:w=1:g={min(4.0, c['bass'] * 0.4):.1f}")
    if ap._has_filter("speechnorm"):
        f.append("speechnorm=e=15:r=0.002:l=1:p=0.9")
    f.append("acompressor=threshold=0.06:ratio=8:attack=2:release=60:makeup=4:knee=3")
    if ap._has_filter("mcompand"):
        f.append("mcompand=args='0.003\\,0.08 6 -60/-24\\,-30/-8\\,0/-1 1000 | "
                 "0.002\\,0.06 6 -60/-20\\,-30/-5\\,0/-1 3000 | "
                 "0.001\\,0.05 6 -60/-22\\,-30/-7\\,0/-2 22000'")
    pres = 4 + c["presence"] * 0.4      # presence 10 -> +8 dB @ 2.8 kHz
    f += ["equalizer=f=1100:t=q:w=1:g=3",
          f"equalizer=f=2800:t=q:w=1:g={pres:.1f}"]
    if ap._has_filter("aexciter"):
        f.append("aexciter=amount=2:drive=5:blend=0:freq=2200:ceil=9999")
    drive = ap._env_db("BRIDGE_FIGHT_DRIVE_DB",
                       16.0 + min(8.0, vb.drive_db(c["drive"]) * 0.25), high=30.0, low=0.0)
    clip = ("asoftclip=type=tanh:oversample=4" if ap._has_filter("asoftclip") else
            "alimiter=level_in=1:level_out=1:limit=0.9:attack=0.1:release=5:level=false")
    f += [f"volume={ap._db(drive)}dB", clip, "lowpass=f=4800:p=2",
          "volume=5dB", clip, "lowpass=f=4800:p=2",
          "alimiter=level_in=1:level_out=1:limit=0.97:attack=0.5:release=30:level=false"]
    return ",".join(f)


def install():
    global _orig_stage
    from . import vc_bridge as vb
    if getattr(vb, "_v79_installed", False):
        return
    vb._v79_installed = True
    _orig_stage = vb.loud_stage
    vb.loud_stage = bridge_fight_stage
