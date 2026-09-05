import asyncio, os, re, subprocess, tempfile
import sys; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from helpers.audio_processor import process_audio_to_file

def mean(path):
    p = subprocess.run(["ffmpeg", "-hide_banner", "-i", path, "-af", "volumedetect",
                        "-f", "null", "-"], capture_output=True, text=True)
    return re.search(r"mean_volume: (-?[\d.]+) dB", p.stderr).group(1)

async def main():
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "quiet.wav")
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                        "-i", "sine=frequency=440:duration=3", "-af", "volume=0.02", src], check=True)
        out = await process_audio_to_file(src, volume=1000, gain=150, boost=10, echo=False)
        print("before:", mean(src), "dB  after:", mean(out), "dB")
        os.unlink(out)

asyncio.run(main())
