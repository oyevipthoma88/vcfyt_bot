"""
Audio processing using FFmpeg.

Chain (order matters):
  1. highpass + lowpass  — remove rumble and hiss outside the speech band
  2. speech normalization — lift quiet recordings without pumping too hard
  3. bass/presence EQ      — controlled warmth plus 3 kHz/8 kHz intelligibility
  4. gain + compression   — increase perceived loudness while retaining dynamics
  5. loudnorm + limiter   — stable broadcast loudness, no digital clipping
  6. optional echo        — disabled by default because it reduces clarity

Everything is adjustable up AND down, so the same knobs can make audio
softer as well as brutally loud.
"""

import os
import asyncio
import subprocess
import tempfile
from typing import Optional

from config import Config

VOLUME_MIN, VOLUME_MAX = 1, 100000
BASS_MIN, BASS_MAX = 0, 100
LEVEL_MIN, LEVEL_MAX = 0, 10


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


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
) -> str:
    """Return an -af filter string for ffmpeg."""
    vol = clamp(volume if volume is not None else Config.DEFAULT_VOLUME,
                VOLUME_MIN, VOLUME_MAX)
    bass_db = clamp(bass if bass is not None else Config.DEFAULT_BASS,
                    BASS_MIN, BASS_MAX)
    use_echo = Config.DEFAULT_ECHO if echo is None else bool(echo)
    e_lvl = clamp(echo_level if echo_level is not None else Config.DEFAULT_ECHO_LEVEL,
                  LEVEL_MIN, LEVEL_MAX)
    b_lvl = clamp(boost if boost is not None else Config.DEFAULT_BOOST,
                  LEVEL_MIN, LEVEL_MAX)
    relay_vol = clamp(relay_volume if relay_volume is not None
                      else Config.RELAY_DEFAULT_VOLUME, 0, 400)
    gain_pct = clamp(gain if gain is not None else Config.RELAY_DEFAULT_GAIN,
                     0, 150)
    treble_pct = clamp(treble if treble is not None else Config.RELAY_DEFAULT_TREBLE,
                       0, 100)

    # Speech-first cleanup: remove sub-rumble and extreme hiss before boosting.
    filters = ["highpass=f=55", "lowpass=f=16000"]

    # ── STAGE 0: pehle hi source ko normal level par le aao ─────────────────
    # Bohot dheemi recording par volume multiplier bekaar hai kyunki limiter
    # baad me kaat deta hai. speechnorm yahan har syllable ko full-scale tak
    # khinchta hai — asli "slow aavaj ko loud" karne wali cheez yahi hai.
    filters.append("speechnorm=e=25:r=0.0004:l=1:p=0.95")
    filters.append("dynaudnorm=f=150:g=9:p=0.95:m=12:s=0.7:r=0.85")

    # ── Bass ────────────────────────────────────────────────────────────────
    if bass_db > 0:
        filters.append(f"equalizer=f=80:t=o:w=1:g={min(bass_db, 30)}")
        if bass_db > 30:
            filters.append(f"equalizer=f=55:t=o:w=1:g={min(bass_db - 30, 30)}")
            filters.append("asubboost=dry=1:wet=1:decay=0.4:feedback=0.7")
        if bass_db > 60:
            filters.append(f"equalizer=f=110:t=o:w=1.2:g={bass_db - 60}")

    filters.append(f"equalizer=f=3000:t=o:w=1.5:g={treble_pct * 0.12 - 6:.2f}")
    filters.append(f"equalizer=f=8000:t=o:w=1.5:g={treble_pct * 0.10 - 4:.2f}")

    # ── RAW GAIN (multi-stage, warna intermediate values saturate hote hain) ─
    # Compact relay volume uses 100 as unity; legacy volume values retain
    # the original direct multiplier semantics.
    if volume is not None and volume <= 400:
        vol = max(1, relay_vol) / 100.0
    remaining = float(vol)
    while remaining > 1.0:
        stage = min(remaining, 32.0)
        filters.append(f"volume={stage:.3f}")
        remaining /= stage

    if gain_pct > 0:
        filters.append(f"volume={1 + gain_pct / 100:.2f}")

    if b_lvl > 0:
        ratio = 4 + b_lvl * 1.6
                 # 5.6 … 20
        makeup = 1 + b_lvl * 0.8                # 1.8 … 9
        threshold = max(0.012, 0.4 - b_lvl * 0.038)
        filters.append(
            f"acompressor=threshold={threshold:.3f}:ratio={ratio:.1f}"
            f":attack=5:release=80:makeup={min(makeup, 3.5):.2f}"
        )
        # Heavy broadcast-style compand: waveform ko lagbhag flat kar deta hai,
        # yaani perceived loudness maximum.
        knee = 6 + b_lvl
        filters.append(
            "compand=attacks=0:decays=0.15:"
            f"points=-90/-90|-70/-{max(6, 30 - b_lvl * 2)}|-40/-{max(3, 14 - b_lvl)}"
            f"|-20/-{max(2, 8 - b_lvl // 2)}|0/-1:soft-knee={knee}:gain={b_lvl}"
        )
        filters.append(f"dynaudnorm=f=200:g=12:p=0.97:m={min(20, 8 + b_lvl * 1.5):.1f}:s=0.7:r=0.85")
        # A small presence lift is clearer than aggressive soft-clipping.
        filters.append(
            f"speechnorm=e={min(32.0, 10 + b_lvl * 3.0):.1f}:r=0.0005:l=1:p=0.98"
        )
        filters.append("loudnorm=I=-14:LRA=7:TP=-1.5:dual_mono=true:linear=false")

    # Brick-wall limiter — LOUD but never clipped/crackly.
    filters.append("alimiter=level_in=1:level_out=1:limit=0.98:attack=2"
                   ":release=30:level=false")

    # ── ECHO (ab actually apply hota hai) ───────────────────────────────────
    if use_echo and e_lvl > 0:
        d1 = 70 + e_lvl * 22                       # 92 … 290 ms
        d2, d3 = d1 * 2, d1 * 3
        dec = min(0.92, 0.22 + e_lvl * 0.07)
        filters.append(
            f"aecho=0.95:0.9:{d1}|{d2}|{d3}:"
            f"{dec:.2f}|{dec * 0.72:.2f}|{dec * 0.5:.2f}"
        )
        if e_lvl >= 7:
            # Bada hall reverb feel — sirf high echo levels par.
            filters.append(
                f"aecho=0.9:0.85:{d1 // 2}|{d1 + 37}:{dec * 0.6:.2f}|{dec * 0.4:.2f}"
            )
        # Echo ke baad level wapas upar.
        filters.append(f"volume={1.1 + e_lvl * 0.06:.2f}")

    if extra_filters:
        filters.append(extra_filters)

    # Final modest push + safety limiter. The loudnorm stage above does the
    # heavy lifting; a huge post-limiter multiplier only creates distortion.
    if b_lvl > 0:
        filters.append(f"volume={1 + b_lvl * 0.08:.2f}")
    filters.append("alimiter=limit=0.98:attack=5:release=60:level=false")

    return ",".join(filters)


def get_ffmpeg_piped_input(source: str, **kwargs) -> list:
    """ffmpeg command that writes raw PCM to stdout (for raw streams)."""
    af = build_ffmpeg_filter(**kwargs)
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", source, "-af", af,
        "-f", "s16le", "-ac", "2", "-ar", "48000", "pipe:1",
    ]


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
    """Process an audio/video file, return path to the processed .mp3."""
    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".mp3", prefix="vc_processed_")
        os.close(fd)

    af = build_ffmpeg_filter(
        volume, bass, echo, echo_level, boost, relay_volume, gain, treble,
        extra_filters,
    )

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", input_path, "-af", af,
        "-ar", "48000", "-ac", "2", "-b:a", "320k",
        output_path,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg failed: {stderr.decode()[-500:]}")

    return output_path


async def download_yt(url: str) -> str:
    """Download audio from YouTube / SoundCloud / etc using yt-dlp."""
    fd, base_path = tempfile.mkstemp(suffix="", prefix="vc_ytdl_")
    os.close(fd)
    os.unlink(base_path)

    out_tmpl = base_path + ".%(ext)s"
    final_path = base_path + ".mp3"

    cmd = [
        "yt-dlp", "-x", "--audio-format", "mp3", "--audio-quality", "0",
        "--no-playlist", "-o", out_tmpl, url,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp failed: {stderr.decode()[-400:]}")
    return final_path


def ffmpeg_available() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False
