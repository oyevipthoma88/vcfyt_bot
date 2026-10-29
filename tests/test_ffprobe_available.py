"""ffprobe must always be resolvable: pytgcalls refuses to play without it."""
import asyncio, os, shutil, subprocess, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def test_ffprobe_shim_probes_audio_for_pytgcalls():
    env = dict(os.environ, PATH=tempfile.mkdtemp(), DISABLE_STATIC_FFMPEG="1", PYTHONPATH=ROOT)
    code = r'''
import asyncio, os, shutil, subprocess, tempfile
import helpers.ffmpeg_setup as fs
assert shutil.which("ffprobe"), "ffprobe missing"
wav = os.path.join(tempfile.mkdtemp(), "t.wav")
subprocess.run([shutil.which("ffmpeg"),"-y","-f","lavfi","-i","sine=d=2","-ac","2",wav],capture_output=True,check=True)
from pytgcalls.ffmpeg import check_stream
from pytgcalls.types.raw import AudioParameters
asyncio.run(check_stream(None, wav, AudioParameters()))
print("OK")
'''
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=120)
    assert "OK" in r.stdout, r.stdout + r.stderr
