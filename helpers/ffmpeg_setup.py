"""Make sure an ``ffmpeg`` binary is on PATH without any Heroku buildpack.

The old jonathanong ffmpeg buildpack downloads from johnvansickle.com, which
is often unreachable from Heroku ("Unable to download ffmpeg: 000") and
breaks the whole deploy.  ``imageio-ffmpeg`` ships a static ffmpeg inside its
pip wheel, so pip install alone gives us ffmpeg.  If a system ffmpeg exists
(Termux / VPS / buildpack) it is preferred.
"""
import logging
import os
import shutil

log = logging.getLogger(__name__)


def ensure_ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:  # pragma: no cover
        log.error("ffmpeg not found and imageio-ffmpeg unavailable: %s", e)
        return ""
    bin_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".bin")
    os.makedirs(bin_dir, exist_ok=True)
    link = os.path.join(bin_dir, "ffmpeg")
    try:
        if not os.path.exists(link):
            os.symlink(exe, link)
    except OSError:
        shutil.copy2(exe, link)
        os.chmod(link, 0o755)
    os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
    log.info("Using bundled ffmpeg: %s", exe)
    return link


FFMPEG_PATH = ensure_ffmpeg()
