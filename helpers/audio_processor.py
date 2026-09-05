
import asyncio
import os
import re
import shlex
import tempfile
from typing import Optional

from config import Config

VOLUME_MIN, VOLUME_MAX = 0, 1000
BASS_MIN, BASS_MAX = 0, 100
LEVEL_MIN, LEVEL_MAX = 0, 10
GAIN_MAX = 150
TREBLE_MAX = 100

def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))

def _db(value: float) -> str:
    return f"{value:.2f}"

def _sanitize_ffmpeg_filter(value: str) -> str:
    value = re.sub(r"(?i)(knee=)0(?:\.0+)?(?![\d.])", r"\g<1>1", value)
    value = re.sub(r"(?i)(attack=)0(?:\.0+)?(?![\d.])", r"\g<1>0.1", value)
    return value

def volume_to_db(vol: int) -> float:
    vol = clamp(vol, VOLUME_MIN, VOLUME_MAX)
    if vol <= 500:
        return -30.0 + (30.0 * vol / 500.0)
    return 30.0 * (vol - 500) / 500.0

def gain_to_db(gain: int) -> float:
    return 12.0 * clamp(gain, 0, GAIN_MAX) / GAIN_MAX

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
    if volume is None:
        volume = relay_volume if relay_volume is not None else Config.DEFAULT_VOLUME
    vol = clamp(volume, VOLUME_MIN, VOLUME_MAX)
    bass_value = clamp(bass if bass is not None else Config.DEFAULT_BASS, BASS_MIN, BASS_MAX)
    use_echo = Config.DEFAULT_ECHO if echo is None else bool(echo)
    echo_value = clamp(echo_level if echo_level is not None else Config.DEFAULT_ECHO_LEVEL, LEVEL_MIN, LEVEL_MAX)
    boost_value = clamp(boost if boost is not None else Config.DEFAULT_BOOST, LEVEL_MIN, LEVEL_MAX)
    gain_value = clamp(gain if gain is not None else Config.RELAY_DEFAULT_GAIN, 0, GAIN_MAX)
    treble_value = clamp(treble if treble is not None else Config.RELAY_DEFAULT_TREBLE, 0, TREBLE_MAX)

    filters = [
        "highpass=f=50",
        "aresample=48000",

        "dynaudnorm=f=500:g=100:p=1.0:m=100:r=0.99:s=0",

        "volume=18dB",
    ]

    if bass_value:
        filters.append(f"equalizer=f=90:t=q:w=1:g={_db(min(12.0, bass_value * 0.12))}")

    filters.append(f"equalizer=f=3000:t=q:w=1.2:g={_db(-6.0 + treble_value * 0.12)}")
    filters.append(f"equalizer=f=8000:t=q:w=1.2:g={_db(-4.0 + treble_value * 0.08)}")

    ratio = 4.0 + boost_value * 1.2
    threshold = max(0.04, 0.20 - boost_value * 0.026)
    makeup = boost_value * 2.5 + 6.0
    filters.append(
        f"acompressor=threshold={threshold:.3f}:ratio={ratio:.1f}:"
        f"attack=1:release=60:makeup={makeup:.1f}:knee=1"
    )

    filters.append(
        "acompressor=threshold=0.01:ratio=20.0:"
        "attack=0.1:release=45:makeup=28.0:knee=8"
    )

    if use_echo and echo_value:
        d1 = 70 + echo_value * 22
        decay = min(0.85, 0.20 + echo_value * 0.06)
        filters.append(
            f"aecho=0.85:0.75:{d1}|{d1 * 2}|{d1 * 3}:"
            f"{decay:.2f}|{decay * 0.65:.2f}|{decay * 0.4:.2f}"
        )

    filters.append(f"volume={_db(volume_to_db(vol) + gain_to_db(gain_value))}dB")

    if extra_filters:
        filters.append(extra_filters)

    filters.append("loudnorm=I=-5:LRA=1:TP=-0.05:dual_mono=true:linear=false")

    filters.append("volume=96.00dB")
    filters.append("asoftclip=type=hard:threshold=0.04:output=3.0:oversample=4")

    filters.append("alimiter=level_in=8:limit=1.0:attack=0.1:release=30:level=false:asc=1")
    return _sanitize_ffmpeg_filter(",".join(filters))

def build_live_mic_filter() -> str:
    return _sanitize_ffmpeg_filter(
        "highpass=f=70,aresample=48000,"
        "dynaudnorm=f=500:g=100:p=1.0:m=100:r=0.99:s=0,"
        "equalizer=f=90:t=q:w=1:g=12,"
        "equalizer=f=3000:t=q:w=1.2:g=6,"
        "equalizer=f=8000:t=q:w=1.2:g=4,"
        "volume=30dB,"
        "acompressor=threshold=0.04:ratio=20:attack=0.1:release=45:makeup=24:knee=8,"
        "acompressor=threshold=0.02:ratio=20:attack=0.1:release=35:makeup=24:knee=8,"
        "loudnorm=I=-5:LRA=1:TP=-0.05:dual_mono=true:linear=false,"
        "volume=24dB,alimiter=limit=0.995:attack=0.1:release=30:level=false:asc=1"
    )

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
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", input_path, "-vn", "-af", af,
        "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", output_path,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0 or not os.path.exists(output_path):
        try:
            os.unlink(output_path)
        except OSError:
            pass
        raise RuntimeError(f"FFmpeg failed: {stderr.decode(errors='replace')[-500:]}")
    return output_path

def shell_quote(args: list) -> str:
    return shlex.join(args)
