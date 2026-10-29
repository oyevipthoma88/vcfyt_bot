"""Make sure BOTH ``ffmpeg`` and ``ffprobe`` are on PATH without any buildpack.

Root cause of ``FFmpegError: ffprobe not installed``:
pytgcalls runs ``ffprobe`` before every stream (``check_stream``).  Heroku's
python buildpack has no ffmpeg at all, and ``imageio-ffmpeg`` only ships the
``ffmpeg`` binary - never ``ffprobe``.  So ffmpeg worked but every ``.play``
died at the probe step.

Resolution order (first that works wins):
  1. system ffmpeg / ffprobe (VPS, Termux, apt buildpack)
  2. ``static-ffmpeg`` pip package (real static ffmpeg + ffprobe)
  3. bundled ``imageio-ffmpeg`` ffmpeg + a tiny pure-python ``ffprobe`` shim
     that answers exactly the probe pytgcalls / this bot make.
"""
import logging
import os
import shutil
import stat
import sys

log = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN_DIR = os.path.join(ROOT, ".bin")
SHIM_SRC = os.path.join(ROOT, "helpers", "ffprobe_shim.py")


def _prepend_path(path: str) -> None:
    parts = os.environ.get("PATH", "").split(os.pathsep)
    if path not in parts:
        os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")


def _link(src: str, name: str) -> str:
    os.makedirs(BIN_DIR, exist_ok=True)
    dst = os.path.join(BIN_DIR, name)
    try:
        if os.path.lexists(dst):
            os.remove(dst)
        os.symlink(src, dst)
    except OSError:
        shutil.copy2(src, dst)
    os.chmod(dst, os.stat(dst).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    _prepend_path(BIN_DIR)
    return dst


def _try_static_ffmpeg() -> bool:
    if os.environ.get("DISABLE_STATIC_FFMPEG") == "1":
        return False
    try:
        import static_ffmpeg  # type: ignore
        static_ffmpeg.add_paths(weak=True)
        return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
    except Exception as e:  # network/download failure etc.
        log.warning("static-ffmpeg unavailable: %s", e)
        return False


def _install_ffprobe_shim() -> str:
    os.makedirs(BIN_DIR, exist_ok=True)
    dst = os.path.join(BIN_DIR, "ffprobe")
    with open(SHIM_SRC, "r", encoding="utf-8") as f:
        body = f.read()
    with open(dst, "w", encoding="utf-8") as f:
        f.write(f"#!{sys.executable}\n" + body)
    os.chmod(dst, 0o755)
    _prepend_path(BIN_DIR)
    return dst


def ensure_ffmpeg() -> str:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg and ffprobe:
        return ffmpeg

    if _try_static_ffmpeg():
        log.info("Using static-ffmpeg: %s / %s", shutil.which("ffmpeg"), shutil.which("ffprobe"))
        return shutil.which("ffmpeg") or ""

    if not ffmpeg:
        try:
            import imageio_ffmpeg
            ffmpeg = _link(imageio_ffmpeg.get_ffmpeg_exe(), "ffmpeg")
            log.info("Using bundled ffmpeg: %s", ffmpeg)
        except Exception as e:  # pragma: no cover
            log.error("ffmpeg not found and imageio-ffmpeg unavailable: %s", e)
            return ""

    if not shutil.which("ffprobe"):
        log.info("ffprobe missing - installing built-in ffprobe shim: %s", _install_ffprobe_shim())
    return ffmpeg


FFMPEG_PATH = ensure_ffmpeg()
FFPROBE_PATH = shutil.which("ffprobe") or ""
