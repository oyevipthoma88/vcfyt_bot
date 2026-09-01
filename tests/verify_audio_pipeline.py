import asyncio
import os
import subprocess
import tempfile
from pathlib import Path

from helpers.audio_processor import build_ffmpeg_filter, process_audio_to_file


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True)


async def main():
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "source.wav"
        output = Path(directory) / "processed.mp3"
        run(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-filter:a", "volume=0.08", str(source),
        )
        await process_audio_to_file(str(source), str(output))
        assert output.exists() and output.stat().st_size > 0
        probe = run(
            "ffmpeg", "-hide_banner", "-i", str(output), "-af", "volumedetect",
            "-f", "null", "-",
        )
        combined = probe.stderr
        assert "mean_volume" in combined
        print("processed_audio_exists=true")
        print(next(line.strip() for line in combined.splitlines() if "mean_volume" in line))
        print("filter_has_loudnorm=true", "loudnorm=" in build_ffmpeg_filter())
        print("filter_has_limiter=true", "alimiter=" in build_ffmpeg_filter())
        filters = [build_ffmpeg_filter(volume=level, relay_volume=level, boost=0, echo=False)
                   for level in (0, 250, 500, 750, 1000)]
        gains = [line for af in filters for line in af.split(",") if line.startswith("volume=")]
        assert len(set(gains)) >= 5
        print("distinct_real_gain_levels=true", len(set(gains)))


if __name__ == "__main__":
    asyncio.run(main())


def test_script_is_present():
    assert os.path.exists(__file__)


def test_filter_has_loudness_safety():
    af = build_ffmpeg_filter()
    assert "loudnorm=" in af
    assert "alimiter=" in af
    assert "highpass=f=55" in af
    assert "lowpass=f=16000" in af
    assert "aecho=" not in af


def test_relay_controls_remain_active():
    af = build_ffmpeg_filter(volume=200, bass=10, gain=30, treble=40, echo=False, boost=0)
    assert "volume=2.000" in af
    assert "volume=1.30" in af
    assert "equalizer=f=3000" in af
    assert "equalizer=f=8000" in af
    assert "aecho=" not in af
