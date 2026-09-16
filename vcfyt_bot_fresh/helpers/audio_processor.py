
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
GAIN_MAX = 200
TREBLE_MAX = 120

def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))

def _db(value: float) -> str:
    return f"{value:.2f}"

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
        "highpass=f=20",
        "aresample=48000",

        "dynaudnorm=f=150:g=250:p=1.0:m=100:r=0.99:s=0",

        "volume=20dB",
    ]

    if bass_value:
        filters.append(f"equalizer=f=60:t=q:w=1.2:g={_db(min(20.0, bass_value * 0.20))}")
        filters.append(f"equalizer=f=120:t=q:w=1.0:g={_db(min(15.0, bass_value * 0.15))}")

    filters.append(f"equalizer=f=3000:t=q:w=1.2:g={_db(-1.0 + treble_value * 0.22)}")
    filters.append(f"equalizer=f=8000:t=q:w=1.2:g={_db(2.0 + treble_value * 0.18)}")

    ratio = min(20.0, 8.0 + boost_value * 1.2)
    threshold = max(0.001, 0.12 - boost_value * 0.020)
    makeup = boost_value * 4.0 + 20.0
    filters.append(
        f"acompressor=threshold={threshold:.3f}:ratio={ratio:.1f}:"
        f"attack=0.5:release=50:makeup={makeup:.1f}:knee=1"
    )

    filters.append(
        "acompressor=threshold=0.003:ratio=20.0:"
        "attack=0.05:release=30:makeup=20.0:knee=8"
    )

    filters.append(
        "acompressor=threshold=0.001:ratio=20.0:"
        "attack=0.02:release=20:makeup=25.0:knee=8"
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

    filters.append("loudnorm=I=-5:LRA=0.1:TP=-0.005:dual_mono=true:linear=false")

    extra_db = clamp(getattr(Config, "EXTRA_GAIN_DB", 12), 0, 30)
    filters.append(f"volume={extra_db:.2f}dB")
    filters.append("asoftclip=type=hard:threshold=0.02:output=2.0:oversample=12")

    filters.append("alimiter=level_in=10:limit=1.0:attack=0.02:release=8:level=false:asc=1")
    return _sanitize_ffmpeg_filter(",".join(filters))

def build_live_mic_filter(
    bass: int = None,
    echo: bool = None,
    echo_level: int = None,
    boost: int = None,
    gain: int = None,
    treble: int = None,
) -> str:
    bass_value = clamp(bass if bass is not None else Config.DEFAULT_BASS, BASS_MIN, BASS_MAX)
    use_echo = Config.DEFAULT_ECHO if echo is None else bool(echo)
    echo_value = clamp(echo_level if echo_level is not None else Config.DEFAULT_ECHO_LEVEL, LEVEL_MIN, LEVEL_MAX)
    boost_value = clamp(boost if boost is not None else Config.DEFAULT_BOOST, LEVEL_MIN, LEVEL_MAX)
    gain_value = clamp(gain if gain is not None else Config.RELAY_DEFAULT_GAIN, 0, GAIN_MAX)
    treble_value = clamp(treble if treble is not None else Config.RELAY_DEFAULT_TREBLE, 0, TREBLE_MAX)

    filters = [
        "highpass=f=30",
        "aresample=48000",
        "dynaudnorm=f=200:g=300:p=1.0:m=100:r=0.99:s=0",
    ]

    if bass_value:
        filters.append(f"equalizer=f=60:t=q:w=1.2:g={_db(min(20.0, bass_value * 0.20))}")
        filters.append(f"equalizer=f=120:t=q:w=1.0:g={_db(min(15.0, bass_value * 0.15))}")
    else:
        filters.append("equalizer=f=60:t=q:w=1.2:g=0")
        filters.append("equalizer=f=120:t=q:w=1.0:g=0")

    filters.append(f"equalizer=f=3000:t=q:w=1.2:g={_db(-1.0 + treble_value * 0.22)}")
    filters.append(f"equalizer=f=8000:t=q:w=1.2:g={_db(2.0 + treble_value * 0.18)}")

    filters.append("volume=30dB")

    ratio = min(20.0, 8.0 + boost_value * 1.2)
    threshold = max(0.001, 0.12 - boost_value * 0.020)
    makeup = boost_value * 5.0 + 30.0
    filters.append(
        f"acompressor=threshold={threshold:.3f}:ratio={ratio:.1f}:"
        f"attack=0.5:release=50:makeup={makeup:.1f}:knee=1"
    )
    filters.append(
        "acompressor=threshold=0.004:ratio=20.0:"
        "attack=0.03:release=20:makeup=25:knee=8"
    )
    filters.append(
        "acompressor=threshold=0.001:ratio=20.0:"
        "attack=0.02:release=15:makeup=30:knee=8"
    )

    if use_echo and echo_value:
        d1 = 70 + echo_value * 22
        decay = min(0.85, 0.20 + echo_value * 0.06)
        filters.append(
            f"aecho=0.85:0.75:{d1}|{d1 * 2}|{d1 * 3}:"
            f"{decay:.2f}|{decay * 0.65:.2f}|{decay * 0.4:.2f}"
        )

    filters.append(f"volume={_db(gain_to_db(gain_value) * 1.5)}dB")

    filters.append("loudnorm=I=-5:LRA=0.1:TP=-0.005:dual_mono=true:linear=false")

    extra_db = clamp(getattr(Config, "EXTRA_GAIN_DB", 12), 0, 30)
    filters.append(f"volume={extra_db:.2f}dB")
    filters.append("asoftclip=type=hard:threshold=0.02:output=2.0:oversample=12")
    filters.append("alimiter=level_in=10:limit=0.999:attack=0.02:release=8:level=false:asc=1")
    return _sanitize_ffmpeg_filter(",".join(filters))

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
