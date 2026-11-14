"""v86 Live Mic auto tunnel — works even when the Heroku domain is unknown.

Problem (v85 logs): the bot could not learn its own Heroku address.  Modern
Heroku domains look like `<app>-<random 12 hex>.herokuapp.com`; without
`runtime-dyno-metadata`, HEROKU_API_KEY or `.mic url`, nothing tells the dyno
that suffix, so `.mic on` had no link to send.

Fix: when no verified address exists the bot opens a free Cloudflare quick
tunnel (`cloudflared tunnel --url http://127.0.0.1:$PORT`) from inside the
dyno.  That gives a public https `*.trycloudflare.com` link (WebSocket ok,
mic allowed because it is https) that reaches the same aiohttp server.  The
link is verified with the bot fingerprint like every other candidate.

Disable with LIVE_MIC_TUNNEL=off.
"""
import asyncio
import logging
import os
import platform
import re
import shutil
import stat
import time
from typing import Optional

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_RELEASE = "https://github.com/cloudflare/cloudflared/releases/latest/download/"
START_TIMEOUT = 45.0
RETRY_AFTER_FAIL = 120.0

_proc: Optional[asyncio.subprocess.Process] = None
_url = ""
_port = 0
_lock: Optional[asyncio.Lock] = None
_last_fail = 0.0
last_error = ""


def enabled() -> bool:
    return os.environ.get("LIVE_MIC_TUNNEL", "auto").strip().lower() not in {
        "0", "off", "false", "no", "disable", "disabled"}


def set_port(port: int) -> None:
    global _port
    _port = int(port or 0)


def current() -> str:
    if _proc is not None and _proc.returncode is None:
        return _url
    return ""


def _asset() -> str:
    m = platform.machine().lower()
    if m in {"x86_64", "amd64"}:
        return "cloudflared-linux-amd64"
    if m in {"aarch64", "arm64"}:
        return "cloudflared-linux-arm64"
    if m.startswith("arm"):
        return "cloudflared-linux-arm"
    if m in {"i386", "i686", "x86"}:
        return "cloudflared-linux-386"
    return ""


def _bin_path() -> str:
    d = os.environ.get("LIVE_MIC_TUNNEL_DIR") or os.path.join(
        os.environ.get("TMPDIR", "/tmp"), "vcfyt-cloudflared")
    return os.path.join(d, "cloudflared")


async def _binary() -> str:
    found = shutil.which("cloudflared")
    if found:
        return found
    path = _bin_path()
    if os.path.isfile(path) and os.path.getsize(path) > 1_000_000:
        return path
    asset = _asset()
    if not asset or not platform.system().lower().startswith("linux"):
        raise RuntimeError(f"no cloudflared build for {platform.system()}/{platform.machine()}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    import aiohttp
    tmp = path + ".part"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as http:
        async with http.get(_RELEASE + asset) as resp:
            if resp.status != 200:
                raise RuntimeError(f"cloudflared download HTTP {resp.status}")
            with open(tmp, "wb") as fh:
                async for chunk in resp.content.iter_chunked(1 << 16):
                    fh.write(chunk)
    os.chmod(tmp, os.stat(tmp).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    os.replace(tmp, path)
    logger.info("Live mic tunnel: cloudflared downloaded (%s)", asset)
    return path


async def _drain(proc: asyncio.subprocess.Process) -> None:
    global _url
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
    except Exception:
        pass
    await proc.wait()
    if _proc is proc:
        logger.warning("Live mic tunnel stopped (exit %s)", proc.returncode)
        _url = ""


async def stop() -> None:
    global _proc, _url
    p, _proc, _url = _proc, None, ""
    if p is not None and p.returncode is None:
        try:
            p.terminate()
            await asyncio.wait_for(p.wait(), 5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass


async def ensure(force_new: bool = False) -> str:
    """Start (or reuse) the tunnel and return its https URL ('' on failure)."""
    global _proc, _url, _lock, _last_fail, last_error
    if not enabled() or not _port:
        return ""
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        if not force_new and current():
            return _url
        if not force_new and _last_fail and time.monotonic() - _last_fail < RETRY_AFTER_FAIL:
            return ""
        await stop()
        try:
            exe = await _binary()
            proc = await asyncio.create_subprocess_exec(
                exe, "tunnel", "--no-autoupdate", "--protocol", "http2",
                "--url", f"http://127.0.0.1:{_port}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            deadline = time.monotonic() + START_TIMEOUT
            url = ""
            while time.monotonic() < deadline:
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(),
                                                  max(0.5, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    break
                if not line:
                    break
                m = _URL_RE.search(line.decode(errors="ignore"))
                if m and "api.trycloudflare.com" not in m.group(0):
                    url = m.group(0)
                    break
            if not url:
                try:
                    proc.kill()
                except Exception:
                    pass
                raise RuntimeError("tunnel did not give a URL")
            _proc, _url = proc, url
            asyncio.get_running_loop().create_task(_drain(proc))
            _last_fail, last_error = 0.0, ""
            logger.info("Live mic tunnel ready: %s", url)
            return url
        except Exception as exc:
            _last_fail, last_error = time.monotonic(), str(exc)[:200]
            logger.warning("Live mic tunnel failed: %r", exc)
            return ""
