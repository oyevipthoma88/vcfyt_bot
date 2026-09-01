"""Fast, real FFmpeg audio processing for Telegram voice-chat playback."""

import asyncio
import os
import subprocess
import tempfile
from typing import Optional

from config import Config

VOLUME_MIN, VOLUME_MAX = 0, 1000
BASS_MIN, BASS_MAX = 0, 100
LEVEL_MIN, LEVEL_MAX = 0, 10


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def _db(value: float) -> str:
    return f"{value:.2f}"


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
    """Build a single-pass FFmpeg filter chain with real audible controls.

    Volume and gain are mapped to dB, not arbitrary repeated multipliers. The
    source is normalized before those controls are applied, so quiet input is
    lifted consistently and the controls remain distinguishable instead of
    being flattened by multiple compressors/limiters.
    """
    vol = clamp(volume if volume is not None else (
        relay_volume if relay_volume is not None else Config.DEFAULT_VOLUME
    ), VOLUME_MIN, VOLUME_MAX)
    bass_value = clamp(bass if bass is not None else Config.DEFAULT_BASS, BASS_MIN, BASS_MAX)
    use_echo = Config.DEFAULT_ECHO if echo is None else bool(echo)
    echo_value = clamp(echo_level if echo_level is not None else Config.DEFAULT_ECHO_LEVEL, LEVEL_MIN, LEVEL_MAX)
    boost_value = clamp(boost if boost is not None else Config.DEFAULT_BOOST, LEVEL_MIN, LEVEL_MAX)
    gain_value = clamp(gain if gain is not None else Config.RELAY_DEFAULT_GAIN, 0, 150)
    treble_value = clamp(treble if treble is not None else Config.RELAY_DEFAULT_TREBLE, 0, 100)

    # 0..1000 => -12..+24 dB; 0..150 => -6..+18 dB.
    # These ranges keep every setting measurable while leaving headroom for
    # the final limiter instead of allowing uncontrolled clipping.
    volume_db = -12.0 + (36.0 * vol / VOLUME_MAX)
    gain_db = -6.0 + (24.0 * gain_value / 150.0)
    boost_db = 2.5 * boost_value

    filters = [
        "highpass=f=55",
        "lowpass=f=16000",
        "aresample=48000",
        "loudnorm=I=-10:TP=-0.5:LRA=5:linear=false",
    ]

    if bass_value:
        bass_db = min(12.0, bass_value * 0.12)
        filters.append(f"equalizer=f=90:t=q:w=1:g={_db(bass_db)}")
    # 0..100 => -6..+6 dB presence range.
    filters.append(f"equalizer=f=3000:t=q:w=1.2:g={_db(-6.0 + treble_value * 0.12)}")
    filters.append(f"equalizer=f=8000:t=q:w=1.2:g={_db(-4.0 + treble_value * 0.08)}")

    # Compress only enough to make quiet speech intelligible; do not stack
    # multiple compressors because that destroys the effect of user controls.
    ratio = 3.0 + (boost_value * 0.50)
    threshold = max(0.06, 0.24 - boost_value * 0.014)
    filters.append(
        f"acompressor=threshold={threshold:.3f}:ratio={ratio:.2f}:"
        "attack=5:release=80:makeup=3.0"
    )
    filters.append(f"volume={_db(volume_db)}dB")
    filters.append(f"volume={_db(gain_db + boost_db)}dB")

    if use_echo and echo_value:
        d1 = 70 + echo_value * 22
        d2, d3 = d1 * 2, d1 * 3
        decay = min(0.85, 0.20 + echo_value * 0.06)
        filters.append(
            f"aecho=0.85:0.75:{d1}|{d2}|{d3}:"
            f"{decay:.2f}|{decay * 0.65:.2f}|{decay * 0.4:.2f}"
        )

    if extra_filters:
        filters.append(extra_filters)
    filters.append("alimiter=limit=0.98:attack=5:release=60:level=false")
    return ",".join(filters)


def get_ffmpeg_piped_input(source: str, **kwargs) -> list:
    af = build_ffmpeg_filter(**kwargs)
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", source,
        "-af", af, "-f", "s16le", "-ac", "2", "-ar", "48000", "pipe:1",
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
    """Process an audio/video file and return an MP3 path."""
    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".mp3", prefix="vc_processed_")
        os.close(fd)
    af = build_ffmpeg_filter(
        volume=volume, bass=bass, echo=echo, echo_level=echo_level,
        boost=boost, relay_volume=relay_volume, gain=gain, treble=treble,
        extra_filters=extra_filters,
    )
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", input_path,
        "-af", af, "-ar", "48000", "-ac", "2", "-b:a", "320k", output_path,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg failed: {stderr.decode(errors='replace')[-500:]}")
    return output_path


async def download_yt(url: str) -> str:
    """Download audio from supported sites using yt-dlp."""
    fd, base_path = tempfile.mkstemp(suffix="", prefix="vc_ytdl_")
    os.close(fd)
    os.unlink(base_path)
    out_tmpl = base_path + ".%(ext)s"
    final_path = base_path + ".mp3"
    cmd = ["yt-dlp", "-x", "--audio-format", "mp3", "--audio-quality", "0", "--no-playlist", "-o", out_tmpl, url]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp failed: {stderr.decode(errors='replace')[-400:]}")
    return final_path


def ffmpeg_available() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False
