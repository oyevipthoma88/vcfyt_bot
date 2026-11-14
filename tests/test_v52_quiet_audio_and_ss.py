"""v52: dheemi / normal / tez recording sab ek hi loudness pe, SS off order."""
import os, re, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
from helpers.audio_processor import build_stream_command, measure_input_gain


def _voice(peak_db: float) -> str:
    """Speech-like test clip (modulated tone bursts) at a given peak level."""
    fd, path = tempfile.mkstemp(suffix=".ogg"); os.close(fd)
    amp = 10 ** (peak_db / 20.0)
    expr = f"{amp}*sin(2*PI*220*t)*(0.5+0.5*sin(2*PI*4*t))*gt(sin(2*PI*0.7*t),-0.3)"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"aevalsrc='{expr}':s=48000:d=6", "-c:a", "libopus", "-b:a", "64k", path], check=True)
    return path


def _play_stats(path: str):
    out = tempfile.mktemp(suffix=".pcm")
    subprocess.run(build_stream_command(path) + f" > {out}", shell=True, check=True)
    size = os.path.getsize(out)
    r = subprocess.run(["ffmpeg", "-hide_banner", "-f", "s16le", "-ar", "48000", "-ac", "2", "-i", out,
                        "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True).stderr
    os.unlink(out)
    mx = re.findall(r"max_volume: (-?[\d.]+)", r)
    mean = re.findall(r"mean_volume: (-?[\d.]+)", r)
    return size, (float(mx[-1]) if mx else -99), (float(mean[-1]) if mean else -99)


@pytest.mark.parametrize("peak", [-45.0, -18.0, -1.0])
def test_quiet_normal_loud_all_reach_full_level(peak):
    src = _voice(peak)
    try:
        size, mx, mean = _play_stats(src)
    finally:
        os.unlink(src)
    assert size > 48000 * 4, "very quiet audio was cut to silence"
    assert mx > -3.5          # reaches the -3 dBFS (v57) master ceiling
    assert mean > -16.0       # loud, not "normal"


def test_input_gain_only_lifts_never_cuts():
    src = _voice(-1.0)
    try:
        assert measure_input_gain(src) == 0.0
    finally:
        os.unlink(src)


def test_ss_off_leaves_presentation_before_removing_screen_track():
    src = open(os.path.join(os.path.dirname(__file__), "..", "helpers", "vc_manager.py")).read()
    body = src[src.index("async def _ss_toggle_independent"):]
    off = body[body.index("            else:\n"):]
    # v66 fix: set_stream_sources runs before _ss_leave_hard to prevent crash
    assert off.index("set_stream_sources") < off.index("_ss_leave_hard")
