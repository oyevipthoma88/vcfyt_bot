"""v85 Live Mic public URL — ROOT FIX for Heroku "There's nothing here, yet."

Bug: `.mic on` built the link from an UNVERIFIED host.  Sources were:
  * a host stored in Mongo from an older deploy / old Heroku app (the Mongo DB
    survives app re-creates and renames, the old domain does not), or
  * `LIVE_MIC_BASE_URL` copied from .env.example / typed wrong, or
  * the `<app>.herokuapp.com` guess (modern Heroku domains carry a random
    suffix), or
  * a spoofable `X-Forwarded-Host` header.
Any of those opens Heroku's "There's nothing here, yet." page instead of the mic.

Fix: every candidate URL is checked by calling `<url>/health` and requiring
THIS bot's fingerprint (sha256 of the bot token, never the token itself) in the
answer.  The first URL that really reaches this bot wins; dead or foreign ones
are skipped and reported, so a broken link is never sent again.
"""
import asyncio
import hashlib
import ipaddress
import logging
import os
import time
from typing import List, Optional, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

DB_LAST_GOOD = "live_mic_base_url"            # last verified URL (legacy key)
DB_OVERRIDE = "live_mic_base_url_override"    # owner-set via `.mic url <link>`
FRESH_SECONDS = 60.0       # skip re-probing a URL verified this recently
PROBE_TIMEOUT = 6.0
TUNNEL_PROBES = 10
TUNNEL_PROBE_GAP = 3.0

OK, FOREIGN, UNREACHABLE = "ok", "foreign", "unreachable"

_seen_host = ""            # host of a real incoming request (this process)
_verified = ""
_verified_at = 0.0
_heroku_api_url: Optional[str] = None
_lock: Optional[asyncio.Lock] = None
last_report: List[Tuple[str, str, str]] = []   # (source, url, result)


def _cfg(name: str, env: str) -> str:
    try:
        from config import Config
        val = getattr(Config, name, None)
        if val is not None:
            return str(val or "").strip()
    except Exception:
        pass
    return os.environ.get(env, "").strip()


def fingerprint() -> str:
    tok = _cfg("BOT_TOKEN", "BOT_TOKEN")
    return hashlib.sha256(f"vcfyt-live-mic:{tok}".encode()).hexdigest()[:16]


def health_text() -> str:
    return f"vcfyt bot is running\nid: {fingerprint()}\n"


def _is_local(host: str) -> bool:
    host = (host or "").strip("[]").lower()
    if host in {"localhost", "0.0.0.0"} or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_unspecified
    except ValueError:
        return False


def normalize(url: str) -> str:
    """'my-app.herokuapp.com/?token=x' -> 'https://my-app.herokuapp.com'."""
    u = (url or "").strip().strip("<>\"'` ")
    if not u:
        return ""
    if "://" not in u:
        u = "https://" + u
    try:
        parts = urlsplit(u)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not host or "." not in host and not _is_local(host):
        return ""
    # Browsers only allow the mic on https (localhost is the one exception).
    scheme = "http" if (parts.scheme == "http" and _is_local(host)) else "https"
    netloc = host + (f":{port}" if port and port not in (80, 443) else "")
    return f"{scheme}://{netloc}"


def remember_request(request) -> None:
    """Learn the real public host from an incoming request.

    Only the `Host` header is trusted: the hosting router sets it from the
    domain that was actually opened.  `X-Forwarded-Host` is client-controlled
    and used to let anybody poison the mic link.
    """
    global _seen_host
    try:
        host = (request.headers.get("Host") or "").split(",")[0].strip()
    except Exception:
        return
    if not host or _is_local(host.split(":")[0]):
        return
    proto = (request.headers.get("X-Forwarded-Proto") or "https").split(",")[0].strip()
    base = normalize(f"{proto}://{host}")
    if base and base != _seen_host:
        _seen_host = base
        logger.info("Live mic: page opened on %s", base)


async def _db_get(key: str) -> str:
    try:
        from helpers.database import db
        return (await db.get_app_value(key) or "").strip()
    except Exception:
        return ""


async def _db_set(key: str, value: str) -> None:
    try:
        from helpers.database import db
        await db.set_app_value(key, value)
    except Exception as exc:
        logger.debug("mic url db write failed: %r", exc)


async def _heroku_api_web_url() -> str:
    """Exact app URL from the Heroku Platform API (needs HEROKU_API_KEY)."""
    global _heroku_api_url
    if _heroku_api_url is not None:
        return _heroku_api_url
    key = os.environ.get("HEROKU_API_KEY", "").strip()
    app = (os.environ.get("HEROKU_APP_ID") or os.environ.get("HEROKU_APP_NAME") or "").strip()
    if not key or not app:
        return ""
    _heroku_api_url = ""
    try:
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as http:
            async with http.get(f"https://api.heroku.com/apps/{app}", headers={
                    "Accept": "application/vnd.heroku+json; version=3",
                    "Authorization": f"Bearer {key}"}) as resp:
                if resp.status == 200:
                    _heroku_api_url = normalize((await resp.json()).get("web_url") or "")
                else:
                    logger.warning("Heroku API app lookup failed: HTTP %s", resp.status)
    except Exception as exc:
        logger.warning("Heroku API app lookup failed: %r", exc)
    return _heroku_api_url


async def candidates() -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []

    def add(source: str, url: str) -> None:
        n = normalize(url)
        if n and all(n != u for _, u in out):
            out.append((source, n))

    add(".mic url", await _db_get(DB_OVERRIDE))
    add("LIVE_MIC_BASE_URL", _cfg("_LIVE_MIC_OVERRIDE", "LIVE_MIC_BASE_URL"))
    add("Heroku domain", _cfg("_HEROKU_DEFAULT_DOMAIN", "HEROKU_APP_DEFAULT_DOMAIN_NAME"))
    add("Heroku API", await _heroku_api_web_url())
    add("page opened", _seen_host)
    add("last working", await _db_get(DB_LAST_GOOD))
    name = _cfg("_HEROKU_APP_NAME", "HEROKU_APP_NAME")
    if name:
        add("app-name guess", f"https://{name}.herokuapp.com")
    try:
        from helpers import mic_tunnel
        add("auto tunnel", mic_tunnel.current())
    except Exception:
        pass
    return out


async def probe(base: str) -> str:
    """OK = this bot answered; FOREIGN = something else answered (e.g. Heroku
    "nothing here" 404); UNREACHABLE = no answer at all."""
    try:
        import aiohttp
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=PROBE_TIMEOUT)) as http:
            async with http.get(f"{base}/health",
                                headers={"User-Agent": "vcfyt-mic-url-check"}) as resp:
                text = await resp.text(errors="ignore")
                if resp.status == 200 and f"id: {fingerprint()}" in text:
                    return OK
                return FOREIGN
    except Exception as exc:
        logger.debug("mic url probe %s failed: %r", base, exc)
        return UNREACHABLE


def _on_heroku() -> bool:
    return bool(os.environ.get("DYNO"))


def cached() -> str:
    return _verified


async def _accept(url: str) -> str:
    global _verified, _verified_at
    _verified, _verified_at = url, time.monotonic()
    if await _db_get(DB_LAST_GOOD) != url:
        await _db_set(DB_LAST_GOOD, url)
    logger.info("Live mic public URL verified: %s", url)
    return url


async def resolve(force: bool = False) -> str:
    """Return a mic base URL that really reaches this bot ('' if none)."""
    global _lock, _verified, last_report
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        if _verified and not force:
            if time.monotonic() - _verified_at < FRESH_SECONDS:
                return _verified
            if await probe(_verified) == OK:
                return await _accept(_verified)
            logger.warning("Live mic URL %s stopped working — searching again", _verified)
            _verified = ""

        cands = await candidates()
        results = await asyncio.gather(*(probe(u) for _, u in cands)) if cands else []
        last_report = [(s, u, r) for (s, u), r in zip(cands, results)]
        for source, url, result in last_report:
            if result == OK:
                return await _accept(url)
        # Off Heroku (VPS/Termux tunnel) a server often cannot call its own
        # public URL.  Then trust an explicitly configured / really-opened
        # URL that simply did not answer — never one that answered as
        # somebody else (that is exactly the "nothing here" bug).
        if not _on_heroku():
            for source, url, result in last_report:
                if result == UNREACHABLE and source in {".mic url", "LIVE_MIC_BASE_URL", "page opened"}:
                    logger.warning("Live mic URL %s could not be self-checked; using it", url)
                    return url
        for source, url, result in last_report:
            logger.warning("Live mic URL rejected (%s): %s -> %s", source, url, result)
            # v86: a stored "last working" URL that now opens Heroku's
            # "nothing here" page is dead for good — forget it.
            if source == "last working" and result == FOREIGN:
                await _db_set(DB_LAST_GOOD, "")
        # v86 ROOT FIX: Heroku address unknown -> open our own https tunnel.
        url = await _tunnel_url(force_new=any(
            s == "auto tunnel" for s, _, _ in last_report))
        if url:
            return url
        return ""


async def _tunnel_url(force_new: bool = False) -> str:
    global _verified, _verified_at
    try:
        from helpers import mic_tunnel
    except Exception:
        return ""
    url = normalize(await mic_tunnel.ensure(force_new=force_new))
    if not url:
        if mic_tunnel.last_error:
            last_report.append(("auto tunnel", "cloudflare", "unreachable"))
        return ""
    # Fresh trycloudflare names need a few seconds of DNS propagation.
    result = UNREACHABLE
    for _ in range(TUNNEL_PROBES):
        result = await probe(url)
        if result == OK:
            break
        await asyncio.sleep(TUNNEL_PROBE_GAP)
    last_report.append(("auto tunnel", url, result))
    if result != OK:
        logger.warning("Live mic tunnel %s not verified (%s)", url, result)
        return ""
    # Not saved as "last working": tunnel links change on every restart.
    _verified, _verified_at = url, time.monotonic()
    logger.info("Live mic public URL verified (auto tunnel): %s", url)
    return url


async def set_override(url: str) -> Tuple[bool, str]:
    """`.mic url <link>` — verify, then save permanently."""
    global _verified
    if (url or "").strip().lower() in {"reset", "clear", "auto", "off"}:
        await _db_set(DB_OVERRIDE, "")
        _verified = ""
        return True, ""
    base = normalize(url)
    if not base:
        return False, "invalid"
    result = await probe(base)
    if result != OK and _on_heroku():
        return False, result
    await _db_set(DB_OVERRIDE, base)
    await _accept(base)
    return True, base


def explain_failure() -> str:
    """Human (Hinglish) report for Telegram when no URL works."""
    lines = []
    for source, url, result in last_report:
        mark = {"foreign": "❌ galat app / 'nothing here'",
                "unreachable": "❌ khula hi nahi"}.get(result, result)
        lines.append(f"• <code>{url}</code> ({source}) — {mark}")
    return "\n".join(lines)
