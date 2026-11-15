"""Live mic v87: 3 LOUD modes, same names as audio playback (loudest/phone/bass).

Root causes fixed (measured on real speech -> chain -> Opus 32k/48k -> decode):
  * The chain ended in mono while FFmpeg was asked for `-ac 2`.  FFmpeg's
    mono->stereo upmix puts the voice at -3 dB on each channel, so every mode
    lost 3 dB right at the end.  All modes now end with a dual-mono pan.
  * Every mode was clamped to a -2.5 dBFS limiter, ignoring the session's
    real Telegram ceiling (-0.3 dBFS normally, -6.5 dBFS while an admin 200 %
    volume is confirmed) -> "awaaz normal".
  * LOUDEST pushed +20 dB into tanh: flat-topped (square) waves that Telegram's
    Opus decodes with +5..8 dB overshoot = 0.04-0.1 % hard clipping
    ("khar khar").  CLEAN/WARM were 3-5 dB quieter than LOUDEST.

v87 chain (all modes): leveller -> compressor -> voice EQ -> PHASE ROTATOR
(4 all-pass stages make the peaky voice waveform symmetric: ~1 dB more
loudness with zero distortion) -> drive -> 5 ms look-ahead limiter set just
under the Telegram ceiling -> dual-mono.  No clipper, so no square wave.

Measured phone-band (300-3400 Hz) loudness, quiet raw mic, after Opus:
                old            v87
  loudest   -13.2 LUFS      -11.9 LUFS   (Opus clip 0.04 % -> <0.005 %)
  phone     -15.8 (clean)   -12.9 LUFS
  bass      -17.0 (warm)    -15.5 LUFS   (full-band -9.8 LUFS, deep body)

Mode: env LIVE_MIC_LOUD_MODE=loudest|phone|bass (default loudest); `.micmode`.
Old names still work: clean -> phone, warm -> bass.
Tune: LIVE_LOUDEST_DRIVE_DB (loudest drive, default 24, 0..30).
"""
import os

from . import audio_processor as ap

MODES = ("loudest", "phone", "bass")
ALIASES = {"fight": "loudest", "max": "loudest", "tez": "loudest", "loud": "loudest",
           "clean": "phone", "clear": "phone", "saaf": "phone",
           "warm": "bass", "natural": "bass", "deep": "bass"}
LABELS = {"loudest": "LOUDEST", "phone": "PHONE", "bass": "BASS"}
_mode = None
_orig = None

# Telegram ceilings (dBFS) come from LiveMicSession._ceiling_db(): -0.3 when
# listeners hear the relay at 100 %, -6.5 while an admin 200 % is confirmed.
DEFAULT_CEILING_DB = -0.3
# Per-mode voice shaping.  `margin` = dB the limiter stays under the Telegram
# ceiling so Opus overshoot never clips (calibrated at 32k + 48k bitrate).
PROFILES = {
    # phone speaker max: 280 Hz - 4.8 kHz voice band, +8 dB @ 2.5 kHz
    "loudest": dict(hp=280, lp=4800, body=-3.0, pres=8.0, air=3.2, bass=0.0,
                    drive=24.0, margin=0.7),
    # tez + saaf: wider band (200 Hz - 6.5 kHz) for clearer words
    "phone": dict(hp=200, lp=6500, body=-3.0, pres=6.0, air=2.4, bass=0.0,
                  drive=24.0, margin=1.2),
    # tez + bhari: full low end (+6 dB @ 140 Hz) for earphones/headphones
    "bass": dict(hp=70, lp=7000, body=-2.0, pres=4.0, air=1.6, bass=6.0,
                 drive=22.0, margin=1.7),
}
PHASE_ROTATOR = ("allpass=f=200:t=q:w=0.7,allpass=f=200:t=q:w=0.7,"
                 "allpass=f=400:t=q:w=0.7,allpass=f=800:t=q:w=0.7")


def normalize_mode(mode, default=None):
    m = str(mode or "").strip().lower()
    m = ALIASES.get(m, m)
    return m if m in MODES else default


def get_mode() -> str:
    global _mode
    if _mode is None:
        _mode = normalize_mode(os.environ.get("LIVE_MIC_LOUD_MODE", "loudest"), "loudest")
    return _mode


def set_mode(mode: str) -> str:
    global _mode
    m = normalize_mode(mode)
    if m is None:
        raise ValueError(mode)
    _mode = m
    return _mode


def limiter_db(mode: str, ceiling_db: float = None) -> float:
    """Final limiter level: session ceiling minus the mode's Opus margin."""
    try:
        ceiling = float(DEFAULT_CEILING_DB if ceiling_db is None else ceiling_db)
    except (TypeError, ValueError):
        ceiling = DEFAULT_CEILING_DB
    ceiling = max(-12.0, min(DEFAULT_CEILING_DB, ceiling))
    return ceiling - PROFILES[normalize_mode(mode, "loudest")]["margin"]


def mode_chain(mode: str = "loudest", ceiling_db: float = None,
               drive_db: float = None) -> str:
    mode = normalize_mode(mode, "loudest")
    p = PROFILES[mode]
    drive = p["drive"]
    if mode == "loudest":
        drive = ap._env_db("LIVE_LOUDEST_DRIVE_DB", drive, high=30.0, low=0.0)
    if drive_db is not None:
        drive = max(0.0, min(30.0, float(drive_db)))
    limit = 10 ** (limiter_db(mode, ceiling_db) / 20.0)
    f = ["aresample=48000:async=1:first_pts=0",
         f"highpass=f={p['hp']}:p=2", f"highpass=f={p['hp']}:p=2"]
    if ap._has_filter("afftdn"):
        f.append("afftdn=nr=10:nf=-50:tn=0")
    if ap._has_filter("agate"):
        # soft downward expander: hiss between words is never levelled up
        f.append("agate=threshold=0.003:range=0.1:ratio=2:attack=5:release=300:knee=8:detection=rms")
    f.append("volume=6dB")
    if ap._has_filter("speechnorm"):
        f.append("speechnorm=e=25:r=0.001:l=1:p=0.95")
    else:
        f.append("dynaudnorm=f=150:g=11:p=0.6:m=12")
    f.append("acompressor=threshold=0.1:ratio=4:attack=5:release=120:makeup=2:knee=6")
    if p["bass"]:
        f.append(f"equalizer=f=140:t=q:w=1:g={p['bass']:.1f}")
    f += [f"equalizer=f=300:t=q:w=1:g={p['body']:.1f}",
          f"equalizer=f=2500:t=q:w=1.2:g={p['pres']:.1f}",
          f"equalizer=f=4200:t=q:w=1:g={p['air']:.1f}",
          f"lowpass=f={p['lp']}",
          PHASE_ROTATOR,
          f"volume={drive:.2f}dB",
          f"alimiter=level_in=1:level_out=1:limit={limit:.4f}:attack=5:release=40:level=false",
          # dual mono: without this FFmpeg's -ac 2 upmix costs 3 dB
          "pan=stereo|c0=c0|c1=c0"]
    if ap._has_filter("asetnsamples"):
        f.append(f"asetnsamples=n={ap.AUDIO_FRAME_SAMPLES}:p=0")
    return ap._sanitize_ffmpeg_filter(",".join(f))


def live_loudest_chain(ceiling_db: float = None, drive_db: float = None,
                       softclip: bool = True) -> str:
    """Backward-compatible name for the LOUDEST chain (softclip is ignored)."""
    del softclip
    return mode_chain("loudest", ceiling_db, drive_db)


def build_live_mic_filter(ceiling_db: float = None, loud: int = 0, crunch: int = 0,
                          mode: str = None) -> str:
    del loud, crunch
    return mode_chain(normalize_mode(mode or get_mode(), "loudest"), ceiling_db)


async def load_mode() -> str:
    """Restore the `.micmode` / panel choice after a restart (was lost)."""
    try:
        from helpers.database import db
        saved = normalize_mode(await db.get_app_value("live_mic_loud_mode") or "")
        if saved:
            set_mode(saved)
    except Exception:
        pass
    return get_mode()


async def restart_live_sessions() -> int:
    """Running live mics pick up the new chain (VC stream stays joined)."""
    n = 0
    try:
        from helpers import live_mic
        for s in list(getattr(live_mic, "_sessions", {}).values()):
            if getattr(s, "_closed", False):
                continue
            # `.micmode` must reach running mics too: each session builds its
            # chain from its own audio_mode, so update it before the swap.
            s.audio_mode = get_mode()
            if getattr(s, "ffmpeg_proc", None) is not None:
                try:
                    await s._restart_ffmpeg(max_attempts=1)
                    n += 1
                except Exception:
                    pass
    except Exception:
        pass
    return n


def install():
    global _orig
    if getattr(ap, "_live_mic_v79_installed", False):
        return
    ap._live_mic_v79_installed = True
    _orig = ap.build_live_mic_filter
    ap.build_live_mic_filter = build_live_mic_filter
