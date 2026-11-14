"""v69: playback must be much louder than v68 (>= -6 LUFS after Opus) yet not a square wave."""
import asyncio, os, re, shutil, subprocess, sys
import pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import helpers  # noqa: F401
from helpers import audio_processor as ap

HAS_FF = shutil.which("ffmpeg") is not None


def _meas(p):
    o = subprocess.run(["ffmpeg", "-hide_banner", "-i", p, "-af", "ebur128=peak=true",
                        "-f", "null", "-"], capture_output=True, text=True).stderr
    return (float(re.findall(r"I:\s+(-?[\d.]+) LUFS", o)[-1]),
            float(re.findall(r"Peak:\s+(-?[\d.]+) dBFS", o)[-1]))


@pytest.mark.skipif(not HAS_FF, reason="ffmpeg missing")
@pytest.mark.parametrize("vol_db", [-20, -4, 1])
def test_every_source_max_loud_after_opus(tmp_path, vol_db):
    src = str(tmp_path / "s.wav")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "anoisesrc=d=8:c=pink:a=0.4", "-af", f"volume={vol_db}dB",
                    "-ar", "48000", "-ac", "2", src], check=True)
    out = asyncio.run(ap.process_audio_to_file(src, str(tmp_path / "o.wav"), volume=1000))
    ogg = str(tmp_path / "o.ogg")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", out, "-c:a", "libopus",
                    "-b:a", "48k", "-application", "voip", ogg], check=True)
    i, tp = _meas(ogg)
    assert i >= -6.0, f"too quiet: {i} LUFS"
    assert tp - i >= 3.0, f"square wave / dabi: PLR {tp - i}"


@pytest.mark.skip(reason="superseded by v73/v77 softclip fight chain")
def test_no_clipper_and_hot_ceiling():
    af = ap.build_ffmpeg_filter(volume=3000, boost=10)
    assert "asoftclip" not in af
    assert "limit=0.9605" in af
