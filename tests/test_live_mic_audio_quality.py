"""Regression check for live-mic loudness (live chain == playback chain).

Run: python tests/test_live_mic_audio_quality.py
"""

import re
import shutil
import subprocess
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers.audio_processor import build_ffmpeg_filter


def measure(sine_filter: str, af: str = None, raw: bool = False) -> tuple:
    src = sine_filter if raw else "sine=frequency=440:duration=5"
    chain = af if raw else sine_filter
    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostats", "-f", "lavfi", "-i",
            src, "-af", chain + ",volumedetect",
            "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode:
        raise AssertionError(f"FFmpeg rejected the live-mic filter:\n{proc.stderr}")
    mean = re.search(r"mean_volume:\s+(-?[\d.]+) dB", proc.stderr)
    peak = re.search(r"max_volume:\s+(-?[\d.]+) dB", proc.stderr)
    if not mean or not peak:
        raise AssertionError(f"FFmpeg did not report audio levels:\n{proc.stderr}")
    return float(mean.group(1)), float(peak.group(1))


def main() -> int:
    if not shutil.which("ffmpeg"):
        print("SKIP: ffmpeg is required for the live-mic audio regression test")
        return 0

    live_filter = build_ffmpeg_filter(
        volume=1000,
        bass=0,
        echo=False,
        boost=10,
        gain=400,
        treble=120,
        pregain=200,
        turbo=24,
        clarity=28,
        live=True,
    )
    # Default live chain is the CLEAR chain: one compressor + limiter, no
    # stacked gain.  The old fight chain clipped at 0 dBFS (-4 dB mean) and
    # destroyed speech clarity in the VC.
    if "alimiter" not in live_filter:
        raise AssertionError("live mic must end in a limiter")
    if live_filter.count("acompressor") != 1:
        raise AssertionError("live mic must use exactly one compressor (clarity)")
    if "volume=30dB" in live_filter or "speechnorm=e=50" in live_filter:
        raise AssertionError("brutal over-gain stages are back in the live chain")
    # 8 kHz lowpass was the root cause of "aawaj kam lagti hai" — it kills
    # all consonant/air energy.  Must be at least 12 kHz now.
    if "lowpass=f=8000" in live_filter:
        raise AssertionError("8 kHz lowpass is back — voice sounds muffled")

    baseline_peak = measure("anull")[1]
    for input_peak_db in (-6.0, -20.0, -30.0):
        attenuation_db = input_peak_db - baseline_peak
        mean_db, peak_db = measure(f"volume={attenuation_db:.2f}dB,{live_filter}")
        print(f"input peak {input_peak_db:5.1f} dBFS -> mean {mean_db:5.1f}, peak {peak_db:5.1f}")
        if peak_db > -0.2:
            raise AssertionError(f"live voice clipping: peak {peak_db:.1f} dBFS")
        if mean_db > -1.0:
            raise AssertionError(f"live voice over-compressed: mean {mean_db:.1f} dBFS")
        if mean_db < -32.0:
            raise AssertionError(f"live voice too quiet: mean {mean_db:.1f} dBFS")

    print("PASS: live mic is clean and clip-free")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
