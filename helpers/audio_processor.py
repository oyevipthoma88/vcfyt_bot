import asyncio
import os
import shutil
import re
import random
import tempfile
from typing import Optional

from config import Config

VOLUME_MIN, VOLUME_MAX = 0, 3000
# 1000 = old max (clean).  1001-3000 = OVERDRIVE: extra drive into a hard
# clipper, limiter removed -> louder, awaaz fat sakti hai (by choice).
VOLUME_CLEAN_MAX = 1000
BASS_MIN, BASS_MAX = 0, 100
LEVEL_MIN, LEVEL_MAX = 0, 10
GAIN_MAX = 500
# Live-mic gain curve stays calibrated on the old 0-400 scale; 401-500 is extra.
GAIN_REF = 400
TREBLE_MAX = 120
# Live mic pre-amp ceiling (dB).  Fight mode needs a hotter pre-amp to push
# the compressors hard; 36 dB gives maximum loudness before the limiter.
LIVE_PREGAIN_MAX_DB = 42.0
# Default pre-amp: hot enough to wake any phone mic and drive the chain hard.
LIVE_PREGAIN_DEFAULT_DB = 36.0

def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))

_FILTER_CACHE: Optional[set] = None

def _available_filters() -> set:
    """Filters this FFmpeg build actually has.

    An unknown filter name makes FFmpeg exit instantly, which means total
    silence in the voice chat.  So every optional filter is probed once.
    """
    global _FILTER_CACHE
    if _FILTER_CACHE is None:
        names = set()
        try:
            import subprocess
            out = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-filters"],
                capture_output=True, text=True, timeout=15,
            ).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    names.add(parts[1])
        except Exception:
            names = set()
        _FILTER_CACHE = names
    return _FILTER_CACHE

def _has_filter(name: str) -> bool:
    return name in _available_filters()


def _live_chain_mode() -> str:
    """Which live-mic filter chain to use.

    ``match`` (default) = the same loud chain file playback uses, with only
    the two look-ahead filters swapped for low-latency equivalents — the
    live voice lands at the same level as a played file (the "meri aawaz
    kam, fighters ki tez" fix).  ``soft`` = the gentler broadcast chain
    (one leveller, one compressor, one limiter).  Override with
    ``LIVE_MIC_CHAIN=soft``.
    """
    mode = (os.environ.get("LIVE_MIC_CHAIN", "") or "fight").strip().lower()
    if mode in ("soft", "broadcast", "gentle"):
        return "soft"
    if mode in ("match", "playback", "old"):
        return "match"
    return "fight"


def _db(value: float) -> str:
    return f"{value:.2f}"


def _env_db(name: str, default: float, high: float = 30.0,
            low: float = -30.0) -> float:
    """Env-tunable dB value, clamped to a safe range."""
    try:
        value = float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        value = default
    return max(min(low, high), min(max(low, high), value))


# ---------------------------------------------------------------------------
# SYNC + BROADCAST LOUDNESS (shared by live mic + playback)
#
# Drift ("aage-piche") root fix: every chain resamples with explicit timestamp
# sync and emits fixed 20 ms frames (960 samples @ 48 kHz) so all pipes stay
# phase-locked.  Fletcher-Munson presence (+4.5 dB @ 3.2/4.0 kHz) and a cubic
# soft-saturator raise perceived loudness without crackle.
# ---------------------------------------------------------------------------
AUDIO_FRAME_SAMPLES = 960            # 20 ms @ 48 kHz
SYNC_RESAMPLE = "aresample=48000:async=1000:min_hard_comp=0.100000:first_pts=0"
LOW_LATENCY_FLAGS = ["-fflags", "nobuffer", "-flags", "low_delay"]
PRESENCE_BOOST = ["equalizer=f=3200:t=q:w=1:g=4.5", "equalizer=f=4000:t=q:w=1:g=4.5"]
_ARESAMPLE_RE = re.compile(r"(?i)aresample=48000(?::[^,]*)?")


def apply_sync_and_presence(chain: str, boost: bool = True, drive: bool = True) -> str:
    """Lock resample timestamps, add presence boost + cubic soft-clip before
    the final limiter, and force strict 20 ms output frames."""
    parts = [x for x in (chain or "").split(",") if x]
    parts = [SYNC_RESAMPLE if _ARESAMPLE_RE.fullmatch(x) else x for x in parts]
    if not any(x == SYNC_RESAMPLE for x in parts):
        parts.insert(0, SYNC_RESAMPLE)
    if boost:
        extra = list(PRESENCE_BOOST)
        if _has_filter("asoftclip"):
            extra.append("asoftclip=type=cubic")
        idx = max((i for i, x in enumerate(parts) if x.startswith("alimiter")), default=len(parts))
        parts[idx:idx] = extra
    # v30: chains that already end in voice_band_tail() pass drive=False.
    # Stacking the old drive stage (+16 dB into tanh) on top of them was the
    # 3rd/4th clipper in a row -> square wave -> Opus "fat" awaaz.
    if drive and ultra_loud_enabled():
        idx = max((i for i, x in enumerate(parts) if x.startswith("alimiter")), default=len(parts))
        parts[idx:idx] = fight_drive_stage()
    parts = [x for x in parts if not x.startswith("asetnsamples")]
    if _has_filter("asetnsamples"):
        parts.append(f"asetnsamples=n={AUDIO_FRAME_SAMPLES}:p=0")
    return ",".join(parts)

# ---------------------------------------------------------------------------
# ULTRA LOUD TAIL ("max hai phir bhi kam lagti hai" jugad)
#
# Peak already sits at 0 dBFS, so more gain alone only clips.  Perceived
# loudness is won back by:
#   1. cutting sub-bass (< hp Hz) that eats peak headroom but Telegram's
#      Opus voice codec / phone speakers barely reproduce -> ~3-4 dB freed;
#   2. pushing the 2-4 kHz band (ear's most sensitive region, Fletcher-Munson);
#   3. one more fast density compressor + drive into a hard clip, so the
#      AVERAGE (RMS) level rises to within ~2 dB of the peak.
# Disable with ULTRA_LOUD=0.  Tune drive with ULTRA_LOUD_DB (0..12).
# ---------------------------------------------------------------------------
def fight_drive_stage() -> list:
    """"Drive" stage inserted right before the final limiter of EVERY chain
    (file playback, relay and live mic).

    Real loudness, not a bigger number:
      * highpass 80 Hz  - rumble Opus/phone speakers can't play is removed,
        so the freed peak headroom goes to the voice (~3 dB);
      * crystalizer     - sharpens transients so words cut through;
      * aexciter        - adds 3-8 kHz harmonics ("bite"/presence) -> sounds
        louder at the same peak level;
      * drive dB + tanh - warm overdrive saturation (FIGHT_DRIVE_DB, 0..18).
    All are zero/low-latency, safe for the live mic.  Each optional filter is
    only used when this ffmpeg build has it (unknown filter = silence).
    """
    # CLEAN FIX: drive kam (14 -> 9) aur hard clip hata diya.  Pehle tanh ke
    # baad +2.4 dB lift + hard clip 0.99 tha -> 16-bit me samples full scale
    # pe flat ho rahe the ("bich me fat").  Ab tanh saturation hi ceiling hai
    # (kabhi 1.0 cross nahi karta) aur final limiter peaks pakadta hai.
    drive = _env_db("FIGHT_DRIVE_DB", 16.0, high=20.0, low=0.0)
    out = ["highpass=f=80:p=2"]
    if _has_filter("crystalizer"):
        out.append("crystalizer=i=1.2")
    if _has_filter("aexciter"):
        out.append("aexciter=level_in=1:level_out=1:amount=2.5:drive=6:blend=0:freq=3500:ceil=11000")
    out += ["equalizer=f=2600:t=q:w=1.2:g=3",
            f"volume={_db(drive)}dB"]
    if _has_filter("asoftclip"):
        # tanh kabhi 1.0 se upar nahi jaata -> 0 dBFS ceiling, zero hard clip.
        out += ["asoftclip=type=tanh:oversample=2"]
    return out



# ---------------------------------------------------------------------------
# v30 VOICE-BAND FINAL CLIPPER — "awaaz fat rahi hai par tez nahi" ROOT FIX
#
# Measured (real speech -> chain -> Opus 32k -> phone-speaker model):
#   old chains = square wave (crest ~2 dB), 60-70 % of the energy below
#   250 Hz (phone speakers can't play it), output peak 0 dBFS / true-peak
#   +2.4 dBFS -> after Opus decode 12-20 % of all samples CLIP.  That clip is
#   the "fat" sound, and more gain only adds more clip, never loudness —
#   the signal was already at the digital ceiling.
#   This tail: bass that eats headroom is removed BEFORE clipping, so the
#   whole full-scale energy sits in the 250 Hz-8 kHz band the ear/phone hears;
#   every clip stage is band-limited (no harsh aliasing hash for Opus) and the
#   final peak is -1.2 dBFS so the Opus decoder no longer clips.
#   Result: same or +1..2 dB louder voice on phone, post-Opus clip 20 % -> ~2 %.
# ---------------------------------------------------------------------------
VOICE_CEILING_DB = -1.2


def voice_ceiling_db() -> float:
    return _env_db("VOICE_CEILING_DB", VOICE_CEILING_DB, high=-0.5, low=-3.0)


# v44 AGC-SAFE TAIL (record_27: -5.4 LUFS, LRA 0.5, 8.7k clipped samples ->
# Telegram AGC ducked the square wave, so it sounded LOW).  One punchy
# compressor (25 ms attack keeps consonant punch), Fletcher-Munson EQ and ONE
# look-ahead limiter at -1.2 dBFS.  Target ~-11 LUFS with real dynamics.
AGC_SAFE_CEILING_DB = -1.2



# ---------------------------------------------------------------------------
# v50 CLEAR-LOUD MASTER — "awaaz full hai phir bhi kam lagti / dab rahi" ROOT FIX
#
# Measured (33 s real speech -> chain -> Opus 48k voip -> decode):
#   old playback chain : -4.6 LUFS, crest 9 dB, 4.7 % samples CLIPPED after
#                        Opus = square wave.  Telegram's receive AGC + phone
#                        limiter duck a square wave, so it sounds squashed,
#                        "dabbi" and NOT louder.
#   old live-mic chain : -16.4 LUFS = genuinely ~6 dB too quiet.
#   this master        : ~-10.5 LUFS, true-peak -1.3 dBFS, ~0 % post-Opus
#                        clip -> loudest level that still survives Opus clean.
# One shared tail for playback, live mic and the VC bridge, so every source
# lands at the SAME loudness.  No tanh/hard clippers anywhere.
# ---------------------------------------------------------------------------
MASTER_CEILING = 0.84          # -1.5 dBFS: headroom for Opus overshoot
MASTER_PRE_LIMIT = 0.5         # slow word-level limiter (density)


def clear_loud_master(drive_db: float = None, push_db: float = None,
                      leveller: bool = True) -> list:
    drive = _env_db("MASTER_DRIVE_DB", 14.0 if drive_db is None else drive_db, high=20.0, low=0.0)
    push = _env_db("MASTER_PUSH_DB", 7.0 if push_db is None else push_db, high=9.0, low=0.0)
    out = []
    if leveller:
        if _has_filter("speechnorm"):
            out.append("speechnorm=e=10:r=0.0005:l=1:p=0.7")
        else:
            out.append("dynaudnorm=f=150:g=15:p=0.7:m=10")
    out += [
        "acompressor=threshold=0.1:ratio=4:attack=8:release=140:makeup=2:knee=3",
        "equalizer=f=300:t=q:w=1:g=-3",       # mud cut = clarity
        "equalizer=f=2800:t=q:w=1.2:g=4",     # ear's most sensitive band
        "highshelf=f=5000:g=2",               # consonants / air
        "lowpass=f=13500",
        f"volume={_db(drive)}dB",
        f"alimiter=level_in=1:level_out=1:limit={MASTER_PRE_LIMIT}:attack=6:release=90:level=false",
        f"volume={_db(push)}dB",
        f"alimiter=level_in=1:level_out=1:limit={MASTER_CEILING}:attack=1:release=40:level=false",
    ]
    return out


def agc_safe_tail(makeup_db: float = None) -> list:
    """Kept for callers (VC bridge): now the shared clear-loud master."""
    return ["highpass=f=95:p=2"] + clear_loud_master()


def clear_loud_tail(drive: float = 26.0, ceiling_db: float = None) -> list:
    """v31 LIVE MIC "tez + saaf" tail.

    The old double-tanh tail (+32 dB drive, 8 kHz lowpass) smeared words and
    cut every consonant above 8 kHz (muffled, "saaf nahi").  This tail:
      * removes sub-bass twice (phones can't play it, it only eats headroom),
      * lifts the 1.8 / 3 kHz intelligibility band so words cut through,
      * levels syllables with a smooth compressor + slow limiter,
      * ONE oversampled tanh stage for density, a 12 kHz lowpass (keeps
        consonants, removes clip aliasing) and a -1 dBFS brick-wall.
    """
    drive = max(0.0, min(34.0, float(drive)))
    if ceiling_db is None:
        ceiling_db = -1.0
    lim = 10 ** (max(-3.0, min(-0.5, float(ceiling_db))) / 20.0)
    clip = ("asoftclip=type=tanh:oversample=4" if _has_filter("asoftclip") else
            "alimiter=level_in=1:level_out=1:limit=0.9:attack=0.1:release=5:level=false")
    return ["highpass=f=170:p=2", "highpass=f=170:p=2", "lowpass=f=12000",
            "equalizer=f=1800:t=q:w=1:g=3", "equalizer=f=3000:t=q:w=1:g=5",
            "acompressor=threshold=0.03:ratio=12:attack=5:release=120:makeup=10:knee=3",
            "volume=4dB",
            "alimiter=level_in=1:level_out=1:limit=0.5:attack=2:release=40:level=false",
            f"volume={_db(drive)}dB", clip, "lowpass=f=12000",
            f"alimiter=level_in=1:level_out=1:limit={lim:.3f}:attack=1:release=20:level=false"]


def voice_band_tail(d1: float = 30.0, d2: float = 8.0, hp: int = 200,
                    ceiling_db: float = None) -> list:
    """Two band-limited tanh clip stages + true-peak-safe final limiter.

    Input should already be levelled (peak ~ -6 dBFS).  ``d1`` = drive into
    the first clipper (density), ``d2`` = drive into the second (loudness).
    """
    d1 = max(0.0, min(40.0, float(d1)))
    d2 = max(0.0, min(14.0, float(d2)))
    hp = max(80, min(400, int(hp)))
    if ceiling_db is None:
        ceiling_db = voice_ceiling_db()
    lim = 10 ** (max(-3.0, min(-0.5, float(ceiling_db))) / 20.0)
    if _has_filter("asoftclip"):
        clip = "asoftclip=type=tanh:oversample=4"
    else:
        clip = "alimiter=level_in=1:level_out=1:limit=0.9:attack=0.1:release=5:level=false"
    return [f"highpass=f={hp}:p=2", f"highpass=f={hp}:p=2",
            f"volume={_db(d1)}dB", clip,
            f"highpass=f={hp}:p=2", "lowpass=f=12000:p=2",
            f"volume={_db(d2)}dB", clip,
            "lowpass=f=12000:p=2",
            f"alimiter=level_in=1:level_out=1:limit={lim:.3f}:attack=1:release=20:level=false"]


class CloseVoiceGate:
    """Adaptive "sirf meri awaaz" gate for the browser live mic (s16le mono).

    ROOT CAUSE of "fight me awaaz dab jaati hai": echo-cancel is off (it
    ducked the user), so the phone mic also hears the OTHER fighter from the
    speaker.  The old phone chain (x12 pre-amp + compressor + limiter) and
    the server chain (+30 dB, speechnorm e=50, makeup x6 x12) squeezed that
    bleed up to the same level as the user's own voice: measured, bleed 24 dB
    quieter at the mic came out only ~1 dB quieter.  The compressors then
    pushed the user's own words down every time the opponent talked.

    A fixed threshold can't separate them (phones differ by 30 dB), so this
    gate is RELATIVE: it follows the user's own close-mic voice peak and only
    opens for sound within ``focus_db`` of it.  Bleed stays ~30 dB down,
    own words (and their tails, 300 ms hold) pass untouched.
    """

    BLOCK = 480            # 10 ms @ 48 kHz

    def __init__(self, focus_db: float = None, rate: int = 48000):
        if focus_db is None:
            focus_db = _env_db("LIVE_MIC_FOCUS_DB", 12.0, high=40.0, low=0.0)
        self.enabled = focus_db > 0.5
        self.ratio = 10 ** (-float(focus_db) / 20.0)
        self.block = max(80, int(rate) // 100)
        self.floor = 32768 * 10 ** (-45 / 20.0)      # below = never voice (hiss / far bleed)
        self.ref = 0.0                                # own-voice peak tracker
        self.since_ref = 0                            # blocks since ref refresh
        self.hold = 0
        self.g = 1.0
        self.close_gain = 10 ** (-30 / 20.0)
        self.hold_blocks = 30                         # 300 ms
        self.ref_hold_blocks = 600                    # 6 s before ref decays
        self.decay = 10 ** (-0.3 / 20.0 / 100.0)      # 0.3 dB / s after hold

    def _gain_for(self, peak: float) -> float:
        if peak > self.ref:
            # Rise fast but not instantly (one cough doesn't close the gate).
            self.ref += (peak - self.ref) * 0.35
            self.since_ref = 0
        else:
            self.since_ref += 1
            if self.since_ref > self.ref_hold_blocks:
                self.ref *= self.decay
        thr = max(self.floor, self.ref * self.ratio)
        if peak >= thr:
            self.hold = self.hold_blocks
            return 1.0
        if self.hold > 0:
            self.hold -= 1
            return 1.0
        return self.close_gain

    def process(self, data: bytes) -> bytes:
        if not self.enabled or not data:
            return data
        try:
            import numpy as np
        except Exception:
            return data
        x = np.frombuffer(data[:len(data) & ~1], dtype=np.int16).astype(np.float32)
        if x.size == 0:
            return data
        out = np.empty_like(x)
        b = self.block
        for i in range(0, x.size, b):
            seg = x[i:i + b]
            want = self._gain_for(float(np.max(np.abs(seg))))
            g0 = self.g
            # open in ~1 block, close smoothly (~80 ms)
            g1 = want if want > g0 else g0 + (want - g0) * 0.12
            out[i:i + b] = seg * np.linspace(g0, g1, seg.size, dtype=np.float32)
            self.g = g1
        return np.clip(out, -32768, 32767).astype(np.int16).tobytes()


def ultra_loud_enabled() -> bool:
    return (os.environ.get("ULTRA_LOUD", "1") or "1").strip() not in ("0", "off", "false")


def ultra_loud_tail(hp: int = 110) -> list:
    if not ultra_loud_enabled():
        return []
    drive = _env_db("ULTRA_LOUD_DB", 17.0, high=20.0, low=0.0)
    out = [f"highpass=f={int(hp)}:p=2",
           "equalizer=f=2800:t=q:w=1.3:g=4",
           "acompressor=threshold=0.125:ratio=8:attack=0.5:release=40:makeup=2:knee=2",
           f"volume={_db(drive)}dB"]
    out.append("alimiter=level_in=1:level_out=1:limit=0.99:attack=5:release=40:level=false")
    return out


def _sanitize_ffmpeg_filter(value: str) -> str:
    value = re.sub(r"(?i)(knee=)0(?:\.0+)?(?![\d.])", r"\g<1>1", value)
    value = re.sub(r"(?i)(attack=)0(?:\.0+)?(?![\d.])", r"\g<1>0.1", value)
    # FFmpeg acompressor ratio max is 20 — clamp any out-of-range value
    def _clamp_ratio(m):
        ratio = float(m.group(1))
        if ratio > 20.0:
            ratio = 20.0
        elif ratio < 1.0:
            ratio = 1.0
        return f"ratio={ratio:.1f}"
    value = re.sub(r"ratio=(\d+\.?\d*)", _clamp_ratio, value)
    # FFmpeg acompressor knee range is [1 - 8] — clamp any out-of-range value
    def _clamp_knee(m):
        knee = float(m.group(1))
        if knee > 8.0:
            knee = 8.0
        elif knee < 1.0:
            knee = 1.0
        return f"knee={knee:.1f}"
    value = re.sub(r"(?i)knee=(\d+\.?\d*)", _clamp_knee, value)
    # FFmpeg loudnorm LRA range is [1 - 50] — clamp any out-of-range value
    def _clamp_lra(m):
        lra = float(m.group(1))
        if lra < 1.0:
            lra = 1.0
        elif lra > 50.0:
            lra = 50.0
        return f"LRA={lra:.1f}"
    value = re.sub(r"(?i)LRA=(\d+\.?\d*)", _clamp_lra, value)
    # FFmpeg acompressor makeup range is [1 - 64].  A bigger number (the old
    # chain asked for 95) makes FFmpeg refuse the whole filtergraph, playback
    # silently falls back to the RAW file and the audio sounds "normal".
    def _clamp_makeup(m):
        makeup = float(m.group(1))
        return f"makeup={max(1.0, min(64.0, makeup)):.1f}"
    value = re.sub(r"(?i)makeup=(\d+\.?\d*)", _clamp_makeup, value)
    # FFmpeg loudnorm I range is [-70 - -5].  I=0 killed the whole graph.
    def _clamp_i(m):
        target = float(m.group(1))
        return f"I={max(-70.0, min(-5.0, target)):g}"
    value = re.sub(r"(?<![A-Za-z_])I=(-?\d+\.?\d*)", _clamp_i, value)
    # FFmpeg speechnorm expansion 'e' range is [1 - 50] — clamp it
    def _clamp_speechnorm_e(m):
        e = float(m.group(1))
        return f"speechnorm=e={max(1.0, min(50.0, e)):g}"
    value = re.sub(r"(?i)speechnorm=e=(\d+\.?\d*)", _clamp_speechnorm_e, value)
    # FFmpeg alimiter attack range is [0.1 - 80] — clamp any out-of-range value
    # (acompressor attack can go as low as 0.01, so only clamp within alimiter)
    def _clamp_alimiter_attack(m):
        prefix = m.group(1)
        attack = float(m.group(2))
        if attack < 0.1:
            attack = 0.1
        elif attack > 80.0:
            attack = 80.0
        return f"{prefix}{attack:g}"
    value = re.sub(r"(?i)(alimiter=[^,]*?attack=)(\d+\.?\d*)", _clamp_alimiter_attack, value)
    # FFmpeg asoftclip 'param' range is [0.01 - 3] and 'oversample' is [1 - 64].
    # An out-of-range value makes FFmpeg refuse to build the filtergraph and the
    # whole live-mic process dies (exit 222), so clamp it here.
    def _clamp_asoftclip(m):
        body = m.group(1)

        def _p(mm):
            v = float(mm.group(1))
            return f"param={max(0.01, min(3.0, v)):g}"

        def _o(mm):
            v = float(mm.group(1))
            return f"oversample={int(max(1, min(64, v)))}"

        body = re.sub(r"(?i)param=(\d+\.?\d*)", _p, body)
        body = re.sub(r"(?i)oversample=(\d+\.?\d*)", _o, body)
        return f"asoftclip={body}"

    value = re.sub(r"(?i)asoftclip=([^,]*)", _clamp_asoftclip, value)
    return value

def volume_to_db(vol: int) -> float:
    vol = clamp(vol, VOLUME_MIN, VOLUME_CLEAN_MAX)
    if vol <= 500:
        return -30.0 + (30.0 * vol / 500.0)
    # LOUDNESS UPGRADE: the top half of the slider now reaches +48 dB
    # (was +42 dB) so `.vol 1000` is audibly hotter than before.
    return 48.0 * (vol - 500) / 500.0


def extra_loud_db() -> float:
    """User/owner tunable final make-up gain in dB (env LOUD_EXTRA_DB)."""
    try:
        return max(0.0, min(30.0, float(os.environ.get("LOUD_EXTRA_DB", "") or 30.0)))
    except (TypeError, ValueError):
        return 24.0

def gain_to_db(gain: int) -> float:
    # GAIN is real dB now: slider value / 10  (400 -> 40 dB).
    return clamp(gain, 0, GAIN_MAX) / 10.0


def _legacy_gain_to_db(gain: int) -> float:
    """Pre-26-Sep playback mapping: 0-200 slider -> 0-12 dB.

    Playback was reverted to the 24-Sep chain, which was tuned against this
    mapping.  The live mic keeps the new real-dB GAIN scale.
    """
    return 12.0 * clamp(gain, 0, 200) / 200.0


# ---------------------------------------------------------------------------
# LIVE MIC — single fixed chain (no user controls).
#
# Root cause of the "loud but not clear / phati awaaz" live mic:
#   * the browser stacked x10 pre-amp, two compressors (each with automatic
#     make-up gain) and another x6 of post gain, so the phone itself hard-
#     clipped the voice into a square wave before it was ever sent;
#   * the server then added up to ~30 dB more, an atan soft-clip and a limiter
#     driven 15 % into the wall;
#   * sliders / presets could rebuild FFmpeg mid-fight (audio gaps) and push
#     every stage even harder.
# Now the phone sends a clean signal and ALL loudness is made here, once:
#   clean-up -> denoise -> speech leveller (lifts quiet words) -> one
#   compressor -> noise gate (silence between words) -> clarity EQ ->
#   make-up -> brick-wall limiter at -1 dBFS (no clipping, Opus-safe).
# Measured on real speech: quiet (-35 dBFS) and loud (-6 dBFS) phones both
# land at the same loud level, peak -1.0 dBFS, background hiss gated away.
# ---------------------------------------------------------------------------
LIVE_MIC_FIXED_DRIVE_DB = 20.0


def build_live_mic_filter(ceiling_db: float = None, loud: int = 0, crunch: int = 0) -> str:
    # LIVE SLIDERS (mic page):
    #   loud   0..100 -> up to +15 dB extra drive into the limiters (fight
    #                    me awaaz tez / denser).  0 = default best chain.
    #   crunch 0..100 -> soft-clip overdrive ("awaaz fategi") so the voice
    #                    cuts through when the other fighter is equally loud.
    # 0..100 = clean (limiter safe).  101..200 = OVERDRIVE: ceiling 0 dBFS,
    # last brick-wall replaced by a hard clip -> max loudness, may distort.
    loud = max(0, min(200, int(loud or 0)))
    crunch = max(0, min(200, int(crunch or 0)))
    # LOUD 101..200 no longer hard-clips (that square-wave clip WAS the
    # 'khar khar' at the default loud=120); it pushes the warm tanh stage
    # harder instead.  Only CRUNCH > 100 keeps the deliberate hard clip.
    overdrive = False  # v41: fixed clean chain, no user overdrive
    # HEADROOM FOR TELEGRAM 200 % VOLUME (record_19 analysis):
    # when an ADMIN sets the relay to 200 % participant volume (x2 = +6 dB)
    # a stream peaking at -1 dBFS reached the VC at +5/+6 dBFS and clipped.
    # So with a CONFIRMED admin 200 % the stream ends at ~-6.5 dBFS.
    #
    # ROOT FIX "live mic bohot dheema": that -6.5 dB ceiling was applied
    # ALWAYS — also when the 200 % boost never reached listeners (relay not
    # admin, user account not admin, Telegram reset to 100 %).  Then the
    # voice simply arrived 6.5 dB too quiet.  The session now passes the
    # ceiling it actually needs (see LiveMicSession._ceiling_db): -6.5 dB
    # only while an admin boost is confirmed, otherwise -1 dBFS.
    #
    # LOUDNESS (density): perceived loudness = average level, not peak.
    # Old chain: mean ~8 dB under the peak.  New chain drives the voice into
    # a slow pre-limiter (levels whole words) and then a fast brick-wall
    # (catches transients), so the average sits ~4-5 dB under the ceiling —
    # roughly +4 dB louder to the ear at the same clip-safe peak.
    if ceiling_db is None:
        ceiling_db = _env_db("LIVE_MIC_CEILING_DB", -6.5, high=-0.5, low=-12.0)
    ceiling_db = max(-12.0, min(-0.5, float(ceiling_db)))
    # USER CHOICE: admin ho ya non-admin, awaaz hamesha FULL.  The admin
    # -6.5 dB headroom is skipped unless LIVE_MIC_RESPECT_ADMIN=1.
    if os.environ.get("LIVE_MIC_RESPECT_ADMIN", "0") != "1":
        ceiling_db = max(ceiling_db, -0.3)  # v17: full broadcast ceiling
    limit = 10 ** (ceiling_db / 20.0)
    pre_limit = min(0.99, limit * 10 ** (2.5 / 20.0))
    # BANDWIDTH FIX (root cause of "aawaj bohot kam lagti hai"):
    #   old chain had DOUBLE highpass at 140 Hz + DOUBLE lowpass at 8 kHz.
    #   8 kHz cutoff removes all consonant/air energy (5-15 kHz) -> voice
    #   sounds muffled AND perceptually quieter.  Double 140 Hz highpass
    #   kills male voice warmth.  Now: single 85 Hz HP + single 14 kHz LP
    #   = full telephony-grade speech band, much louder and clearer.
    # CLARITY FIX ("khar khar" root cause): the old chain stacked a +35 dB
    # pre-amp, speechnorm e=48, compressor make-up, +11 dB @ 2.6 kHz, an
    # exciter, mcompand +18 dB and ~26 dB drive into the limiters.  That is
    # >100 dB of gain on the phone's noise floor, so hiss/room noise became
    # as loud as the voice and the harsh EQ + exciter made it crackle.
    # New chain: strong denoise + downward expander FIRST (noise never gets
    # amplified), moderate levelling, a warm "damdar" EQ (chest body
    # 120-180 Hz, mud cut, gentle presence) and all loudness from a dense
    # two-stage limiter at the end -> louder AND clean words.
    # v9 FIX (record_21: "awaaz kam + glitch"):
    #   * record_21 measured mean -4 dBFS / peak 0 dBFS = crest ~4 dB, i.e.
    #     the voice was a hard-clipped square wave.  ~60 dB of stacked gain
    #     (pre-amp + speechnorm + mcompand +12 + drive +34 + tanh push +17
    #     + 6 dB) slammed it into the limiters -> distorted, tiring, and
    #     it actually sounds *quieter* in Telegram (AGC ducks distortion).
    #   * two noise gates (-38 dBFS and -48 dBFS) chopped quiet syllables on
    #     and off -> the stuttering "glitch"; a -20 dBFS phone was gated to
    #     total silence.
    # Now: one gentle expander, ONE levelling stage, moderate drive, a light
    # tanh warmth stage that is pulled back, and a single brick-wall at the
    # ceiling minus Opus headroom.  Loud, dense, and keeps ~9-12 dB crest.
    # v13 "awaaz dab rahi hai" ROOT FIX:
    #   * afftdn tn=1 (noise *tracking*) learned the continuous fight voice as
    #     "noise" and kept subtracting it -> voice sounded buried / dabbi.
    #     Now a fixed noise floor (tn=0) that never eats speech.
    #   * v11/v12 stacked +14 dB drive, +8 dB into tanh and a further +4.8 dB
    #     AFTER the tanh (meant as pull-back, but positive) into two limiters
    #     -> ~25 dB of gain reduction, constant pumping: the voice "dabti"
    #     every time it got loud.  Now moderate drive, gain-neutral warmth,
    #     one compressor -> words stay punchy and clear.
    #   * Presence/air (2.5 kHz + 5 kHz) back so consonants cut through.
    #   * Final peak ~0 dBFS (-0.3 dB safety net), no flat-topped clipping.
    # v40 ROOT FIX "full limit par bhi khar khar + kam + atak":
    #   The old chain stacked ~+80 dB of gain (pre-amp 18 + speechnorm 21 +
    #   EQ 10 + makeup 3 + makeup 10 + 4 + 26 dB drive) into a tanh clipper.
    #   Result = square wave (crest ~3 dB): Opus turns that into crackle and
    #   Telegram's receive AGC ducks it, so it sounds QUIETER and distorted.
    #   A 2nd agate after the pre-amp (threshold 0.05) also chopped syllables
    #   on/off = the "atak atak".
    # New chain = broadcast style: clean band, light denoise, ONE levelling
    # stage, intelligibility EQ, ONE smooth compressor, mild saturation and
    # a look-ahead brick-wall at -1 dBFS.  Average sits ~6-8 dB under peak:
    # loud, dense and clean through Opus.
    f = ["aresample=48000:async=1:first_pts=0",
         "highpass=f=90:p=2", "highpass=f=90:p=2"]
    if _has_filter("afftdn"):
        # fixed noise floor (tn=0) -> never learns the voice as noise
        f.append("afftdn=nr=6:nf=-55:tn=0")
    if _has_filter("agate"):
        gate_db = _env_db("LIVE_MIC_GATE_DB", -48.0, high=0.0, low=-70.0)
        if gate_db < -0.5:
            # soft downward expander: pause hiss stays down, words never chopped
            f.append(f"agate=threshold={10 ** (gate_db / 20.0):.5f}:range=0.03:ratio=3:"
                     "attack=3:release=300:knee=6:detection=rms")
    f.append(f"volume={_db(_env_db('LIVE_MIC_PREAMP_DB', 6.0, high=24.0, low=0.0))}dB")
    f += clear_loud_master()
    return _sanitize_ffmpeg_filter(apply_sync_and_presence(",".join(f), boost=False, drive=False))


# MAX LOUDNESS V3
def playback_drive_db(boost: int = None) -> float:
    """`.boost 0-10` -> drive into the master pre-limiter (14..20 dB).

    ROOT FIX ("tag/reply karke play kiya to awaaz normal"): the playback chain
    used to accept boost/gain/treble but never used them, so `.boost`,
    `.gain`, `.treble`, `.voice male|female` and the `.max` preset changed
    nothing and a normal-voice recording came out sounding normal.
    """
    b = LEVEL_MAX if boost is None else clamp(int(boost), LEVEL_MIN, LEVEL_MAX)
    return 14.0 + 6.0 * b / float(LEVEL_MAX)


def playback_voice_stage(bass: int = None, treble: int = None,
                         gain: int = None) -> list:
    """Pre-master tone + level stage for played files (peak-safe).

    * gain 0..500  -> up to +12 dB pre-gain so a softly recorded voice note
      reaches the leveller hot (speechnorm can only lift ~+20 dB on its own),
      guarded by a limiter so it never clips before the master;
    * bass 0..100  -> +0..10 dB warm body at 120 Hz (`.voice male` = heavy);
    * treble 0..100 -> -1.4..+3.2 dB high shelf (`.voice female` = bright).
    """
    out = []
    if _has_filter("agate"):
        # same soft expander as the live mic: hiss between words stays down
        # so the extra gain/drive below lifts only the voice.
        out.append("agate=threshold=0.00400:range=0.03:ratio=3:"
                   "attack=3:release=300:knee=6:detection=rms")
    g = GAIN_MAX if gain is None else clamp(int(gain), 0, GAIN_MAX)
    pre = 12.0 * g / float(GAIN_MAX)
    if pre > 0.05:
        out += [f"volume={_db(pre)}dB",
                "alimiter=level_in=1:level_out=1:limit=0.9:attack=2:release=50:level=false"]
    b = clamp(14 if bass is None else int(bass), BASS_MIN, BASS_MAX)
    if b:
        out.append(f"equalizer=f=120:t=q:w=1.1:g={min(10.0, b * 0.3):.1f}")
    t = clamp(100 if treble is None else int(treble), 0, 100)
    tg = (t - 30) * 0.045
    if abs(tg) >= 0.1:
        out.append(f"highshelf=f=3500:g={tg:.1f}")
    return out


def build_ffmpeg_filter(
        volume: int = None,
        bass: int = None,
        echo: bool = None,
        echo_level: int = None,
        boost: int = None,
        relay_volume: int = None,
        gain: int = None,
        treble: int = None,
        extra_filters: str = "",
        live: bool = False,
        pregain: float = None,
        turbo: float = None,
        clarity: float = None,
        stream: bool = False,
    ) -> str:
        if live:
            return build_live_mic_filter()

        # v30 ROOT FIX (playback): old chain = +20 dB @100 Hz, +20 dB @3 kHz,
        # +60 dB into tanh -> square wave, 68 % bass energy, true-peak
        # +2.4 dBFS, 20 % of samples clipped after Opus = "fat" + not louder.
        # Now: bass that phones can't play is cut, words are levelled, all the
        # drive goes into band-limited clippers and peaks stop at -1.2 dBFS.
        v = 1000 if volume is None else max(0, int(volume))
        # PERMANENT MAX LOUD: purani saved settings (300/500/800) ya galti se
        # kam kiya slider playback ko -10 dB tak dheema kar deta tha -> har
        # baar dobara badhana padta tha.  Ab playback kabhi full se neeche
        # nahi jaata.  Purana behaviour chahiye to env PLAY_ALLOW_QUIET=1.
        if os.environ.get("PLAY_ALLOW_QUIET", "0") != "1":
            v = max(v, VOLUME_MAX)
        over = max(0.0, min(1.0, (v - VOLUME_CLEAN_MAX) / float(VOLUME_MAX - VOLUME_CLEAN_MAX)))
        filters = [
            "aresample=48000",
            "highpass=f=70:p=2", "highpass=f=70:p=2",
        ]
        filters += playback_voice_stage(bass=bass, treble=treble, gain=gain)
        # BUG FIX: the Echo setting was silently ignored in playback.
        _el = max(0, min(LEVEL_MAX, int(echo_level if echo_level is not None else 0)))
        if echo and _el:
            filters.append(f"aecho=0.8:0.85:{40 + _el * 15}:{0.12 + _el * 0.04:.2f}")
        filters += clear_loud_master(drive_db=playback_drive_db(boost))
        if extra_filters:
            # .loud N extra dB: only into the clipper side, peak stays safe.
            m = re.match(r"^volume=(\d+(?:\.\d+)?)dB$", extra_filters.strip())
            if m:
                # into the pre-limiter (before it), so the final peak stays safe
                idx = max(i for i, x in enumerate(filters) if x.startswith("alimiter")) - 2
                filters.insert(idx, f"volume={min(6.0, float(m.group(1)) / 3):.2f}dB")
        # BUG FIX: volume slider pehle ignore ho raha tha (0 pe bhi full awaaz).
        # 1000 ya usse upar = max loud; usse kam = proportionally dheema.
        if v == 0:
            filters.append("volume=0")
        elif v < VOLUME_CLEAN_MAX:
            import math as _m
            filters.append(f"volume={20 * _m.log10(v / VOLUME_CLEAN_MAX):.2f}dB")
        return ",".join(filters)

async def process_audio_to_file(
    input_path: str,
    output_path: Optional[str] = None,
    volume: int = None,
    bass: int = None,
    echo: bool = None,
    echo_level: int = None,
    boost: int = None,
    relay_volume: int = None,
    gain: int = None,
    treble: int = None,
    extra_filters: str = "",
) -> str:
    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".wav", prefix="vc_processed_")
        os.close(fd)
    af = _sanitize_ffmpeg_filter(build_ffmpeg_filter(
        volume=volume, bass=bass, echo=echo, echo_level=echo_level,
        boost=boost, relay_volume=relay_volume, gain=gain, treble=treble,
        extra_filters=extra_filters,
    ))
    async def _run(filter_chain: str) -> tuple:
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", input_path, "-vn", "-af", filter_chain,
            "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", output_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, err = await proc.communicate()
        return proc.returncode, err

    code, stderr = await _run(af)
    if code != 0 or not os.path.exists(output_path):
        # LOUD FALLBACK.  If one exotic filter in the big chain is missing or
        # out of range in this FFmpeg build, we must NOT fall back to the raw
        # file — that is what made played audio sound "normal volume".  This
        # minimal chain only uses filters every FFmpeg build has, and still
        # slams the audio to the ceiling.
        fallback = (
            "aresample=48000,"
            "dynaudnorm=f=150:g=15:p=0.95:m=100:r=0.5,"
            "acompressor=threshold=0.08:ratio=6:attack=10:release=150:makeup=2.0:knee=4,"
            "volume=14dB,"
            "alimiter=level_in=1:limit=0.87:attack=5:release=60:level=false"
        )
        code2, stderr2 = await _run(fallback)  # str now (stray comma made it a tuple)
        if code2 != 0 or not os.path.exists(output_path):
            try:
                os.unlink(output_path)
            except OSError:
                pass
            raise RuntimeError(
                "FFmpeg failed: "
                f"{stderr.decode(errors='replace')[-300:]} | fallback: "
                f"{stderr2.decode(errors='replace')[-300:]}"
            )
    return output_path


# ---------------------------------------------------------------------------
# INSTANT PLAYBACK (Bug 3)
#
# Pre-rendering the whole file with FFmpeg before anything is heard is what
# made `.play` take several seconds on long files.  ntgcalls can read raw PCM
# straight from a shell command, so FFmpeg now streams the processed audio in
# real time: the first frame lands in the voice chat in ~0 s and the filter
# chain is identical to the pre-rendered path.
# ---------------------------------------------------------------------------

def build_stream_command(
    input_path: str,
    volume: int = None,
    bass: int = None,
    echo: bool = None,
    echo_level: int = None,
    boost: int = None,
    relay_volume: int = None,
    gain: int = None,
    treble: int = None,
    extra_filters: str = "",
    loop: bool = False,
    start_at: float = 0.0,
) -> str:
    """Shell command that writes processed 48 kHz stereo s16le PCM to stdout."""
    import shlex

    af = _sanitize_ffmpeg_filter(build_ffmpeg_filter(
        volume=volume, bass=bass, echo=echo, echo_level=echo_level,
        boost=boost, relay_volume=relay_volume, gain=gain, treble=treble,
        extra_filters=extra_filters, stream=True,
    ))
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
           "-fflags", "nobuffer+genpts+flush_packets", "-flags", "low_delay",
           "-threads", "1"]
    if loop:
        # Native, gapless looping — no stream-end round trip, no re-processing.
        cmd += ["-stream_loop", "-1"]
    # v30 BUG FIX: a random 0.2-1.5 s start offset was added to EVERY play
    # ("human touch") -> the first words of every clip were cut and short
    # 1-2 s fight clips played almost nothing.  Leading silence is already
    # trimmed by silenceremove below, so playback starts instantly anyway.
    if start_at and start_at > 0:
        cmd += ["-ss", f"{start_at:.2f}"]
    # Tiny probe window: FFmpeg starts emitting PCM straight away.
    cmd += ["-probesize", "32k", "-analyzeduration", "0"]
    if not (start_at and start_at > 0):
        # 0-sec start: cut the file's leading silence so sound is heard
        # the moment .play hits the VC.
        af = "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.05," + af
    cmd += [
        "-i", input_path, "-vn", "-sn", "-dn", "-af", af,
        "-ar", "48000", "-ac", "2", "-f", "s16le", "-flush_packets", "1",
        "pipe:1",
    ]
    return " ".join(cmd)


_SS_DEFAULT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "assets", "ss_default.jpg")


_FONT_CANDIDATES = (
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "fonts", "DejaVuSans.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/system/fonts/Roboto-Regular.ttf",
    "/data/data/com.termux/files/usr/share/fonts/TTF/DejaVuSans.ttf",
)


def _find_font() -> str:
    for f in _FONT_CANDIDATES:
        if os.path.exists(f):
            return f.replace(":", "\\:")
    return ""


def _prerender_ss_base(img: str, width: int, height: int) -> str:
    """Screen-share background ko ek baar target size par lossless BMP banao."""
    try:
        import hashlib
        import tempfile
        from PIL import Image, ImageFilter
        st = os.stat(img)
        tag = hashlib.md5(f"{img}:{st.st_mtime}:{st.st_size}:{width}x{height}".encode()).hexdigest()[:12]
        out = os.path.join(tempfile.gettempdir(), f"ss_base_{tag}.bmp")
        if os.path.exists(out):
            return out
        src = Image.open(img).convert("RGB")
        scale = min(width / src.width, height / src.height)
        nw, nh = max(2, int(src.width * scale)), max(2, int(src.height * scale))
        if (nw, nh) != src.size:
            src = src.resize((nw, nh), Image.LANCZOS)
        # halka sharpen: text crisp, ringing nahi (encoder ko easy).
        src = src.filter(ImageFilter.UnsharpMask(radius=1.2, percent=90, threshold=2))
        canvas = Image.new("RGB", (width, height), (0, 0, 0))
        canvas.paste(src, ((width - nw) // 2, (height - nh) // 2))
        canvas.save(out, "BMP")
        return out
    except Exception as exc:
        print(f"ss prerender failed: {exc!r}", file=__import__("sys").stderr)
        return ""
_FILTER_PROBE_CACHE = {}


def _ffmpeg_has_filter(name: str) -> bool:
    """SS BLANK FIX: the pip-bundled ffmpeg (imageio-ffmpeg) is built WITHOUT
    drawtext.  Using it made FFmpeg exit instantly -> no video frames ->
    Telegram showed only the blurred profile photo.  Check once, skip if absent."""
    if name in _FILTER_PROBE_CACHE:
        return _FILTER_PROBE_CACHE[name]
    ok = False
    try:
        import subprocess
        out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                             capture_output=True, text=True, timeout=10).stdout
        ok = any(line.split()[1:2] == [name] for line in out.splitlines() if line.strip())
    except Exception:
        ok = False
    _FILTER_PROBE_CACHE[name] = ok
    return ok


def build_fake_screen_command(
    width: int = 1280,
    height: int = 720,
    fps: int = 20,
    image_path: str = "",
    title: str = "Audio Setup — Live",
) -> str:
    """Shell command producing a looping raw I420 video for the fake screen share.

    ntgcalls expects raw **yuv420p (I420)** frames for a SHELL video source —
    the old BGRA output gave a broken / green screen.  The picture is a PC
    screenshot of an audio-mixer setup (``assets/ss_default.jpg`` or the photo
    the user replied to) with live-looking animated level meters and a slowly
    drifting mouse cursor on top, so the shared screen never looks frozen.
    """
    import shlex

    width = max(320, min(1920, int(width))) // 2 * 2
    height = max(240, min(1080, int(height))) // 2 * 2
    fps = max(5, min(30, int(fps)))
    img = image_path if (image_path and os.path.exists(image_path)) else _SS_DEFAULT

    # -re: produce frames in real time so the clock/LIVE timer/meters move
    # at true speed instead of racing ahead in the pipe buffer.
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
           "-threads", "1", "-re"]
    if os.path.exists(img):
        # BLUR FIX: pehle har frame par JPEG decode + lanczos scale + unsharp
        # chalta tha (~0.4 core). Saath me WebRTC ka VP8 encoder bhi CPU
        # khata hai -> WebRTC "CPU overuse" detect karke resolution gira deta
        # tha = viewers ko blur screen.  Ab picture EK BAAR exact size par
        # PNG bana ke loop karte hain (lossless, per-frame sirf meters).
        pre = _prerender_ss_base(img, width, height)
        if pre:
            # -stream_loop -1 cleanly loops input; on pipe close FFmpeg exits instantly (0.1s).
            cmd += ["-loop", "1", "-framerate", str(fps), "-i", pre]
            base = "setsar=1"
        else:
            cmd += ["-loop", "1", "-framerate", str(fps), "-i", img]
            base = (f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,"
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1")
    else:
        cmd += ["-f", "lavfi", "-i", f"color=c=0x101418:s={width}x{height}:r={fps}"]
        base = "setsar=1"

    # Animated level meters on the mixer strips + blinking status dot.
    W, H = width, height
    # Taskbar height is needed by the custom-photo branch below too — it was
    # defined later, so `.ss on` with a replied photo crashed (UnboundLocalError).
    bh = max(28, H // 16)
    parts = [base, "format=yuv420p"]
    use_default = (img == _SS_DEFAULT)
    # NOTE: inside drawbox, `t` in x/y/w/h means *thickness*, not time — the
    # old meters/cursor were therefore frozen.  Animation is done with the
    # timeline `enable=` option (where `t` IS seconds), one box per segment.
    seg_n = 10
    if use_default:
        mw = max(4, W // 150)
        top, span = int(H * 0.364), int(H * 0.386)
        meters = [(int(W * 0.207) + i * int(W * 0.0883), 2.1 + i * 0.37) for i in range(5)]
    else:
        mw = max(6, W // 90)
        top, span = int(H * 0.08), int(H * 0.30)
        meters = [(W - (5 - i) * (mw + 4) - 12, 2.3 + i * 0.41) for i in range(5)]
    seg_h = max(2, span // seg_n)
    for x, sp in meters:
        # Update meters only 3x/s (was every frame): fewer changing pixels =
        # encoder spends its bitrate on sharpness instead of motion -> no blur.
        q = "floor(t*4)/4"
        lvl = f"(0.35+0.6*abs(sin({q}*{sp:.2f})*cos({q}*{sp * 0.53:.2f})))"
        for k in range(seg_n):
            y = top + span - (k + 1) * seg_h
            col = "0x3ddc84" if k < 6 else ("0xffd23f" if k < 8 else "0xff4040")
            parts.append(
                f"drawbox=x={x}:y={y}:w={mw}:h={seg_h - 1}:color={col}@0.95:t=fill:"
                f"enable='gt({lvl},{k / seg_n:.2f})'"
            )
    r = max(4, H // 120)
    parts.append(
        f"drawbox=x={int(W * 0.749) - r if use_default else 2 * r}:"
        f"y={int(H * 0.918) - r if use_default else H - bh - 3 * r}:w={2 * r}:h={2 * r}:"
        f"color=red@0.95:t=fill:enable='lt(mod(t,1.2),0.7)'"
    )
    # Mouse cursor that moves between spots every few seconds.
    cw, ch = max(6, W // 110), max(10, H // 45)
    spots = [(0.55, 0.45), (0.30, 0.55), (0.62, 0.30), (0.40, 0.70), (0.70, 0.60), (0.25, 0.35)]
    step = 8
    for i, (fx, fy) in enumerate(spots):
        parts.append(
            f"drawbox=x={int(W * fx)}:y={int(H * fy)}:w={cw}:h={ch}:color=white@0.95:t=fill:"
            f"enable='eq(mod(floor(t/{step}),{len(spots)}),{i})'"
        )
    # Real-time Windows-style taskbar: live clock + date (server TZ set via
    # SS_TZ, default IST) and a running "LIVE" timer, so the share looks real.
    fs1, fs2 = max(12, bh * 2 // 5), max(10, bh // 3)
    parts.append(f"drawbox=x=0:y={H - bh}:w={W}:h={bh}:color=0x202020@0.96:t=fill")
    parts.append(f"drawbox=x={bh // 4}:y={H - bh + bh // 4}:w={bh // 2}:h={bh // 2}:"
                 f"color=0x3a96dd:t=fill")
    # drawtext needs a font; on Heroku/Termux without fontconfig it made
    # FFmpeg exit instantly -> the screen share froze and stalled the call.
    font = _find_font() if _ffmpeg_has_filter("drawtext") else ""
    if font:
        ff = f"fontfile='{font}':"
        parts.append("drawtext="+ff+"expansion=strftime:text='%I\\:%M %p':"
                     f"x=w-tw-{bh // 2}:y={H - bh + bh // 8}:fontsize={fs1}:fontcolor=white")
        parts.append("drawtext="+ff+"expansion=strftime:text='%d-%m-%Y':"
                     f"x=w-tw-{bh // 2}:y={H - fs2 - bh // 8}:fontsize={fs2}:fontcolor=white")
        parts.append(f"drawbox=x={W - bh * 4}:y=8:w={bh * 3 + bh // 2}:h={bh - 6}:color=red@0.85:t=fill")
        parts.append("drawtext="+ff+"text='LIVE %{pts\\:hms}':"
                     f"x={W - bh * 4 + 8}:y={8 + (bh - 6 - fs2) // 2}:fontsize={fs2}:fontcolor=white")
    parts += [f"fps={fps}", "format=yuv420p"]
    cmd += ["-vf", ",".join(parts), "-f", "rawvideo", "-pix_fmt", "yuv420p", "pipe:1"]
    tz = os.environ.get("SS_TZ", "IST-5:30")
    return shlex.join(["env", f"TZ={tz}"] + cmd)
