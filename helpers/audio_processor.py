"""Real FFmpeg loudness pipeline for Telegram voice-chat playback.

Design goal: maximum clean loudness. Every stage is a standard FFmpeg filter
that exists in every FFmpeg >= 4.x build (including the Heroku buildpack):

  highpass -> aresample -> dynaudnorm -> EQ -> acompressor -> volume -> alimiter

* ``dynaudnorm`` lifts quiet input towards full scale block-by-block (up to
  50x), so a whisper-quiet recording comes out as loud as a mastered track.
* ``acompressor`` squashes peaks so the average level can be pushed higher.
* ``volume`` is the user's control in real dB.
* ``alimiter`` is a true brick-wall limiter so we never send clipped samples.
"""

import asyncio
import glob
import os
import shlex
import subprocess
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


def volume_to_db(vol: int) -> float:
    """0..1000 -> -30..+18 dB (500 = unity)."""
    vol = clamp(vol, VOLUME_MIN, VOLUME_MAX)
    if vol <= 500:
        return -30.0 + (30.0 * vol / 500.0)
    return 18.0 * (vol - 500) / 500.0


def gain_to_db(gain: int) -> float:
    """0..150 -> 0..+12 dB extra drive into the limiter."""
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
    """Build the single-pass FFmpeg ``-af`` chain."""
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
        "highpass=f=60",
        "aresample=48000",
        # Adaptive normaliser: short 150 ms frames, 15-frame window, up to 50x
        # gain -> quiet sources are lifted to full scale almost immediately.
        "dynaudnorm=f=150:g=15:p=0.95:m=50:r=0.9:s=0",
    ]

    if bass_value:
        filters.append(f"equalizer=f=90:t=q:w=1:g={_db(min(12.0, bass_value * 0.12))}")
    # Presence / air: 0..100 -> -6..+6 dB and -4..+4 dB.
    filters.append(f"equalizer=f=3000:t=q:w=1.2:g={_db(-6.0 + treble_value * 0.12)}")
    filters.append(f"equalizer=f=8000:t=q:w=1.2:g={_db(-4.0 + treble_value * 0.08)}")

    # Boost 0..10 -> compression ratio 2..12 and makeup 0..+10 dB. Higher boost
    # means denser, louder audio.
    ratio = 2.0 + boost_value
    threshold = max(0.05, 0.30 - boost_value * 0.025)
    makeup = boost_value * 1.0
    filters.append(
        f"acompressor=threshold={threshold:.3f}:ratio={ratio:.1f}:"
        f"attack=3:release=120:makeup={makeup:.1f}:knee=4"
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
    # Brick-wall: nothing above -0.2 dBFS, fast attack so no sample clips.
    filters.append("alimiter=limit=0.977:attack=2:release=50:level=false:asc=1")
    return ",".join(filters)


def get_ffmpeg_piped_input(source: str, **kwargs) -> list:
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", source,
        "-vn", "-af", build_ffmpeg_filter(**kwargs),
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
    """Render ``input_path`` through the loudness chain.

    Output is 48 kHz stereo 16-bit WAV: no encoder stage, so a 5 minute track
    renders in ~2-3 s and playback starts almost instantly.
    """
    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".wav", prefix="vc_processed_")
        os.close(fd)
    af = build_ffmpeg_filter(
        volume=volume, bass=bass, echo=echo, echo_level=echo_level,
        boost=boost, relay_volume=relay_volume, gain=gain, treble=treble,
        extra_filters=extra_filters,
    )
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


async def download_yt(query: str) -> tuple[str, str]:
    """Download best audio for a URL or a search phrase.

    Returns ``(path, title)``. No re-encode step: FFmpeg processes whatever
    container yt-dlp delivers, which makes this several times faster than
    ``--audio-format mp3``.
    """
    fd, base_path = tempfile.mkstemp(suffix="", prefix="vc_ytdl_")
    os.close(fd)
    os.unlink(base_path)
    target = query if query.startswith(("http://", "https://")) else f"ytsearch1:{query}"
    cmd = [
        "yt-dlp", "--no-playlist", "--no-warnings", "-f", "bestaudio/best",
        "--max-filesize", "200m", "--socket-timeout", "20",
        "-o", base_path + ".%(ext)s", "--print", "after_move:filepath",
        "--print", "title", "--no-simulate", target,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp failed: {stderr.decode(errors='replace')[-400:]}")
    lines = [ln.strip() for ln in stdout.decode(errors="replace").splitlines() if ln.strip()]
    title = lines[0] if lines else query[:60]
    path = next((ln for ln in lines if os.path.exists(ln)), None)
    if not path:
        matches = glob.glob(base_path + ".*")
        if not matches:
            raise RuntimeError("yt-dlp finished but produced no file")
        path = matches[0]
    return path, title[:80]


def ffmpeg_available() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def shell_quote(args: list) -> str:
    return shlex.join(args)
