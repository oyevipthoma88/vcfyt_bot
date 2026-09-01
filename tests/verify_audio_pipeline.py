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
            "-filter:a", "volume=0.03", str(source),
        )
        await process_audio_to_file(str(source), str(output), volume=1000, relay_volume=1000, gain=150, boost=0, echo=False)
        assert output.exists() and output.stat().st_size > 0
        probe = run(
            "ffmpeg", "-hide_banner", "-i", str(output), "-af", "volumedetect",
            "-f", "null", "-",
        )
        combined = probe.stderr
        assert "mean_volume" in combined
        print("processed_audio_exists=true")
        print(next(line.strip() for line in combined.splitlines() if "mean_volume" in line))

        filters = [build_ffmpeg_filter(volume=level, relay_volume=level, gain=0, boost=0, echo=False)
                   for level in (0, 250, 500, 750, 1000)]
        controls = [next(line for line in af.split(",") if line.startswith("volume=")) for af in filters]
        assert controls == ["volume=-18.00dB", "volume=-9.00dB", "volume=0.00dB", "volume=9.00dB", "volume=18.00dB"]
        print("distinct_real_gain_levels=true", len(set(controls)))


if __name__ == "__main__":
    asyncio.run(main())


def test_script_is_present():
    assert os.path.exists(__file__)


def test_filter_has_loudness_safety():
    af = build_ffmpeg_filter()
    assert af.count("loudnorm=") == 1
    assert af.count("alimiter=") == 1
    assert "highpass=f=55" in af
    assert "lowpass=f=16000" in af
    assert "aecho=" not in af


def test_relay_controls_remain_active():
    af = build_ffmpeg_filter(volume=200, bass=10, gain=30, treble=40, echo=False, boost=0)
    assert "volume=-10.80dB" in af
    assert "volume=-7.20dB" in af
    assert "equalizer=f=3000" in af
    assert "equalizer=f=8000" in af
    assert "aecho=" not in af
