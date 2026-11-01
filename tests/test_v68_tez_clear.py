"""v68: playback tez + clear (not a square wave) for every source level,
and the screen-share writer is not starved by a whole-process nice 15."""
import asyncio, os, re, shutil, subprocess, sys
import pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import helpers  # noqa: F401  (installs playback chain)
from helpers import audio_processor as ap

HAS_FF = shutil.which("ffmpeg") is not None


def _meas(p):
    o = subprocess.run(["ffmpeg", "-hide_banner", "-i", p, "-af", "ebur128=peak=true",
                        "-f", "null", "-"], capture_output=True, text=True).stderr
    return (float(re.findall(r"I:\s+(-?[\d.]+) LUFS", o)[-1]),
            float(re.findall(r"Peak:\s+(-?[\d.]+) dBFS", o)[-1]))


def _src(tmp, lufs):
    raw = str(tmp / "raw.wav")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "anoisesrc=d=8:c=pink:a=0.3,bandpass=f=1000:w=2500,"
                    "volume='0.15+0.85*abs(sin(2*PI*1.7*t))':eval=frame",
                    "-ar", "48000", "-ac", "2", "-c:a", "pcm_f32le", raw], check=True)
    i, _ = _meas(raw)
    out = str(tmp / f"src{lufs}.wav")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", raw, "-af",
                    f"volume={lufs - i}dB", "-c:a", "pcm_f32le", out], check=True)
    return out


@pytest.mark.skipif(not HAS_FF, reason="ffmpeg missing")
@pytest.mark.parametrize("lufs", [-34, -22])
def test_voice_is_loud_but_not_square_after_opus(tmp_path, lufs):
    src = _src(tmp_path, lufs)
    out = asyncio.run(ap.process_audio_to_file(src, str(tmp_path / "o.wav"), volume=1000))
    ogg = str(tmp_path / "o.ogg")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", out, "-c:a", "libopus",
                    "-b:a", "48k", "-application", "voip", ogg], check=True)
    i, tp = _meas(ogg)
    assert i >= -7.0, f"too quiet: {i} LUFS"           # tez (normal bots ~-14)
    assert tp - i >= 3.0, f"square wave: PLR {tp - i}"  # not dabi
    assert tp <= 1.0


def test_single_push_stage_no_clipper():
    af = ap.build_ffmpeg_filter(volume=3000, boost=10)
    assert "asoftclip" not in af
    pushes = [float(x) for x in re.findall(r"volume=(-?[\d.]+)dB", af)]
    # input normalisation + one push only; total gain stack stays sane
    assert len(pushes) <= 2 and max(pushes) <= 30.0
    assert af.count("alimiter") == 2


def test_ss_writer_not_whole_process_nice_15():
    src = open(os.path.join(os.path.dirname(__file__), "..", "helpers",
                            "telegram_live_screen.py"), encoding="utf-8").read()
    assert "os.nice(15)" not in src
    assert "setpriority" in src
