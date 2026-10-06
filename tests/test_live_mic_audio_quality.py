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
    # The live mic must run the extreme-loudness fight chain: noise gates +
    # triple compressors + de-esser + soft-clip + brick-wall limiter.
    if "alimiter" not in live_filter:
        raise AssertionError("live mic must use the fight chain (limiter)")
    if live_filter.count("acompressor") < 2:
        raise AssertionError("live mic must have multiple compressors for max loudness")
    if not any(k in live_filter for k in ("deesser", "adynamicequalizer", "equalizer=f=6800")):
        raise AssertionError("live mic must have a de-esser")
    # Background hiss with nobody speaking must come out as silence.
    for amp in (0.03, 0.01):
        noise_mean, _ = measure(f"aevalsrc='{amp}*(random(0)*2-1)':s=48000:d=4", live_filter, raw=True)
        print(f"hiss {amp} -> mean {noise_mean:5.1f} dBFS")
        if noise_mean > -60.0:
            raise AssertionError(f"khar-khar hiss not removed: {noise_mean:.1f} dBFS")

    # FFmpeg's lavfi sine defaults to a -18.06 dBFS peak. Calibrate that
    # baseline once, then test quiet through strong raw mic input levels.
    baseline_peak = measure("anull")[1]
    for input_peak_db in (-20.0, -30.0, -40.0):
        attenuation_db = input_peak_db - baseline_peak
        mean_db, peak_db = measure(
            f"volume={attenuation_db:.2f}dB,{live_filter}"
        )
        print(
            f"input peak {input_peak_db:5.1f} dBFS -> "
            f"mean {mean_db:5.1f} dBFS, peak {peak_db:5.1f} dBFS"
        )
        # A quiet raw mic (-40 dBFS) must still come out loud.
        if mean_db < -8.0:
            raise AssertionError(
                f"live voice too quiet: {mean_db:.1f} dBFS mean"
            )
        if peak_db > 0.5:
            raise AssertionError(
                f"live voice over full scale: {peak_db:.1f} dBFS"
            )

    print("PASS: live mic is as loud as played files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
