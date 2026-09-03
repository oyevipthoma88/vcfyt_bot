"""Android Chrome microphone relay for the VPS live-mic path.

Browser sends mono PCM16/48kHz binary frames over a private WebSocket.
The service writes them to a named FIFO consumed by the userbot's FFmpeg
microphone publisher. This keeps the Telegram account/session on the VPS.
"""
import asyncio
import os
from pathlib import Path
from aiohttp import web

HOST = os.getenv("MIC_RELAY_BIND", "127.0.0.1")
PORT = int(os.getenv("MIC_RELAY_PORT", "8765"))
TOKEN = os.getenv("MIC_RELAY_TOKEN", "")
FIFO = Path(os.getenv("MIC_RELAY_FIFO", "/tmp/apex_live_mic.pcm"))

HTML = Path(__file__).with_name("live_mic.html").read_text(encoding="utf-8")


def authorized(request):
    return bool(TOKEN) and request.query.get("token", "") == TOKEN


async def index(request):
    if not authorized(request):
        raise web.HTTPUnauthorized(text="Invalid mic token")
    return web.Response(text=HTML, content_type="text/html")


async def stream(request):
    if not authorized(request):
        raise web.HTTPUnauthorized(text="Invalid mic token")
    ws = web.WebSocketResponse(max_msg_size=256 * 1024)
    await ws.prepare(request)
    FIFO.parent.mkdir(parents=True, exist_ok=True)
    if not FIFO.exists():
        os.mkfifo(FIFO, 0o600)
    writer_task = asyncio.create_task(_write_pcm(ws))
    try:
        await writer_task
    finally:
        writer_task.cancel()
        await asyncio.gather(writer_task, return_exceptions=True)
        await ws.close()
    return ws


async def _write_pcm(ws):
    # Opening a FIFO for writing blocks until FFmpeg opens its read side.
    fd = await asyncio.to_thread(os.open, FIFO, os.O_WRONLY)
    try:
        while True:
            msg = await ws.receive()
            if msg.type == web.WSMsgType.BINARY:
                if len(msg.data) <= 256 * 1024 and len(msg.data) % 2 == 0:
                    data = memoryview(msg.data)
                    while data:
                        written = await asyncio.to_thread(os.write, fd, data)
                        data = data[written:]
            elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.CLOSED,
                              web.WSMsgType.ERROR):
                break
    finally:
        os.close(fd)


app = web.Application()
app.router.add_get("/mic", index)
app.router.add_get("/mic/stream", stream)

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("MIC_RELAY_TOKEN must be set")
    web.run_app(app, host=HOST, port=PORT)
