"""Web Studio extras for the browser mic page: screen/camera share, VC chat
and VC members.

Screen / camera share
---------------------
The page draws the shared screen (getDisplayMedia) or phone camera onto a
fixed-size canvas and sends JPEG frames over ``/ws/screen``.  Each frame is
decoded to I420 here and a writer thread feeds a FIFO at a steady frame
rate (the last frame is repeated when the network stalls, so Telegram never
sees a gap).  py-tgcalls reads the FIFO with ``cat`` as a raw SHELL
VideoStream that is attached to the live mic's relay stream — the mic audio
keeps flowing through the very same call.

Chat / members
--------------
``chat:<text>`` on the mic socket sends a group message from the user's own
account; ``chatpull`` / ``vcpull`` return the latest group messages and the
voice chat participants (cached a few seconds to stay far from flood limits).
"""
import asyncio
import io
import logging
import os
import shlex
import tempfile
import threading
import time

logger = logging.getLogger("vcbot.web_studio")

SIZES = {"360": (640, 360), "480": (848, 480), "720": (1280, 720)}
DEFAULT_SIZE = "480"
MAX_FPS = 15
MAX_FRAME_BYTES = 1_500_000


def clamp_cfg(cfg: dict) -> tuple:
    """Browser config -> (w, h, fps, kind).  Never trust the page."""
    cfg = cfg if isinstance(cfg, dict) else {}
    q = str(cfg.get("q") or DEFAULT_SIZE)
    w, h = SIZES.get(q, SIZES[DEFAULT_SIZE])
    try:
        fps = int(cfg.get("fps") or 12)
    except (TypeError, ValueError):
        fps = 12
    fps = max(5, min(MAX_FPS, fps))
    kind = "camera" if cfg.get("kind") == "camera" else "screen"
    return w, h, fps, kind


def black_i420(w: int, h: int) -> bytes:
    return bytes([16]) * (w * h) + bytes([128]) * (w * h // 2)


def jpeg_to_i420(data: bytes, w: int, h: int) -> bytes:
    """JPEG/WebP bytes -> limited-range BT.601 I420 frame of exactly w x h."""
    import numpy as np
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    try:
        img.draft("RGB", (w, h))
    except Exception:
        pass
    img = img.convert("RGB")
    if img.size != (w, h):
        img.thumbnail((w, h))
        canvas = Image.new("RGB", (w, h), (0, 0, 0))
        canvas.paste(img, ((w - img.size[0]) // 2, (h - img.size[1]) // 2))
        img = canvas
    ycc = np.asarray(img.convert("YCbCr"), dtype=np.uint8)
    y = (ycc[..., 0].astype(np.uint16) * 219 // 255 + 16).astype(np.uint8)
    u = (ycc[0::2, 0::2, 1].astype(np.uint16) * 224 // 255 + 16).astype(np.uint8)
    v = (ycc[0::2, 0::2, 2].astype(np.uint16) * 224 // 255 + 16).astype(np.uint8)
    return y.tobytes() + u.tobytes() + v.tobytes()


class ScreenFeed:
    """Steady-rate I420 producer behind a FIFO (read by `cat` in py-tgcalls)."""

    def __init__(self, w: int, h: int, fps: int, kind: str = "screen"):
        self.w, self.h, self.fps, self.kind = int(w), int(h), int(fps), kind
        self.frame_size = self.w * self.h * 3 // 2
        self._frame = black_i420(self.w, self.h)
        self.fifo = tempfile.mktemp(prefix="livescreen_", suffix=".yuv")
        os.mkfifo(self.fifo)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._writer, name="screen-feed", daemon=True)
        self.frames_in = 0
        self.frames_out = 0
        self.last_frame_at = 0.0
        self.started_at = time.monotonic()
        self._decoding = False

    # ---- lifecycle
    def start(self) -> "ScreenFeed":
        self._thread.start()
        return self

    @property
    def alive(self) -> bool:
        return not self._stop.is_set()

    def close(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        # A writer blocked in open() waits for a reader: give it one.
        try:
            fd = os.open(self.fifo, os.O_RDONLY | os.O_NONBLOCK)
            os.close(fd)
        except OSError:
            pass
        try:
            self._thread.join(timeout=1.5)
        except RuntimeError:
            pass
        try:
            os.unlink(self.fifo)
        except OSError:
            pass

    # ---- py-tgcalls
    def command(self) -> str:
        return shlex.join(["cat", self.fifo])

    def video_stream(self):
        from ntgcalls import MediaSource
        from pytgcalls.types.raw import VideoParameters, VideoStream
        return VideoStream(MediaSource.SHELL, self.command(),
                           VideoParameters(self.w, self.h, self.fps, adjust_by_height=False))

    # ---- input
    async def push_image(self, data: bytes) -> bool:
        """Decode one browser frame (dropped if the previous one is still decoding)."""
        if not data or len(data) > MAX_FRAME_BYTES or self._decoding or not self.alive:
            return False
        self._decoding = True
        try:
            loop = asyncio.get_running_loop()
            frame = await loop.run_in_executor(None, jpeg_to_i420, data, self.w, self.h)
            if len(frame) == self.frame_size:
                self._frame = frame
                self.frames_in += 1
                self.last_frame_at = time.monotonic()
                return True
        except Exception as exc:
            logger.debug("screen frame decode failed: %r", exc)
        finally:
            self._decoding = False
        return False

    # ---- output thread
    def _writer(self) -> None:
        period = 1.0 / max(1, self.fps)
        while not self._stop.is_set():
            try:
                fd = os.open(self.fifo, os.O_WRONLY)   # blocks until `cat` opens it
            except OSError:
                if self._stop.wait(0.2):
                    return
                continue
            if self._stop.is_set():
                os.close(fd)
                return
            next_t = time.monotonic()
            try:
                while not self._stop.is_set():
                    view = memoryview(self._frame)
                    while view:
                        n = os.write(fd, view)
                        view = view[n:]
                    self.frames_out += 1
                    next_t += period
                    delay = next_t - time.monotonic()
                    if delay > 0:
                        self._stop.wait(delay)
                    else:
                        next_t = time.monotonic()
            except (BrokenPipeError, OSError):
                pass   # reader went away (stream swap) -> reopen for the next `cat`
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass


# ---------------------------------------------------------------- chat / VC

def _sender_name(m) -> str:
    u = getattr(m, "from_user", None)
    if u is not None:
        name = " ".join(x for x in [getattr(u, "first_name", "") or "",
                                    getattr(u, "last_name", "") or ""] if x).strip()
        if name:
            return name[:32]
        un = getattr(u, "username", "") or ""
        return ("@" + un) if un else "User"
    sc = getattr(m, "sender_chat", None)
    return (getattr(sc, "title", "") or "Group")[:32]


def _msg_text(m) -> str:
    t = getattr(m, "text", None) or getattr(m, "caption", None)
    if t:
        return str(t)[:600]
    for kind, label in (("voice", "🎤 Voice"), ("audio", "🎵 Audio"), ("photo", "🖼 Photo"),
                        ("video", "🎬 Video"), ("sticker", "🙂 Sticker"),
                        ("animation", "GIF"), ("document", "📎 File"),
                        ("video_chat_started", "📞 VC start hua"),
                        ("video_chat_ended", "📴 VC band hua")):
        if getattr(m, kind, None):
            return label
    return ""


async def chat_history(session, limit: int = 30) -> list:
    now = time.monotonic()
    cache = getattr(session, "_chat_cache", None)
    if cache and now - cache[0] < 3.0:
        return cache[1]
    client = getattr(session.uvc, "client", None)
    me = getattr(session.uvc, "account_id", None)
    out = []
    if client is not None:
        async for m in client.get_chat_history(session.chat_id, limit=limit):
            text = _msg_text(m)
            if not text:
                continue
            uid = getattr(getattr(m, "from_user", None), "id", None)
            d = getattr(m, "date", None)
            out.append({"id": m.id, "name": _sender_name(m), "text": text,
                        "t": d.strftime("%H:%M") if d else "", "me": bool(me and uid == me)})
    out.reverse()
    session._chat_cache = (now, out)
    return out


async def send_chat(session, text: str) -> None:
    text = (text or "").strip()[:4000]
    if not text:
        raise ValueError("Khali message")
    client = getattr(session.uvc, "client", None)
    if client is None:
        raise RuntimeError("Account connected nahi hai")
    await client.send_message(session.chat_id, text)
    session._chat_cache = None


async def vc_members(session) -> list:
    now = time.monotonic()
    cache = getattr(session, "_vc_cache", None)
    if cache and now - cache[0] < 4.0:
        return cache[1]
    out = []
    for eng in (session.relay, session.uvc):
        fn = getattr(eng, "vc_participants", None)
        if fn is None:
            continue
        try:
            out = await fn(session.chat_id)
            break
        except Exception as exc:
            logger.debug("vc_participants failed: %r", exc)
    relay_id = getattr(session.relay, "account_id", None)
    rows = [{"name": p.get("name", "User"), "muted": bool(p.get("muted")),
             "speaking": bool(p.get("speaking")), "hand": bool(p.get("hand")),
             "volume": int(p.get("volume") or 100), "mic": p.get("uid") == relay_id,
             "presenting": bool(p.get("presenting"))} for p in out or []]
    rows.sort(key=lambda r: (not r["mic"], not r["speaking"], r["muted"], r["name"].lower()))
    session._vc_cache = (now, rows)
    return rows
