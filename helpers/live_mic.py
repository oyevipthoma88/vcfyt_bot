"""Live mic relay: browser captures mic → WebSocket → FIFO → FFmpeg → py-tgcalls → VC.

The user opens a web page on their phone, grants mic permission, and their live
voice is processed with the same FFmpeg effects (bass, echo, gain, boost) used
for file playback.  The processed audio is streamed into the Telegram voice chat
through py-tgcalls' MediaStream.

Pipeline:
  browser mic → getUserMedia → AudioWorklet (16-bit PCM 48kHz mono)
  → WebSocket → server writes to raw FIFO
  → FFmpeg reads raw FIFO, applies filter chain, writes WAV to proc FIFO
  → py-tgcalls reads proc FIFO as MediaStream → plays into VC
"""

import asyncio
import logging
import os
import secrets
import tempfile
import time
from typing import Dict, Optional

from aiohttp import web, WSMsgType

logger = logging.getLogger("vcbot.live_mic")

_sessions: Dict[int, "LiveMicSession"] = {}


class LiveMicSession:
    """Manages a single user's live mic relay: WebSocket, FIFOs, FFmpeg, py-tgcalls."""

    def __init__(self, user_id: int, uvc, chat_id: int, settings: dict):
        self.user_id = user_id
        self.uvc = uvc
        self.chat_id = chat_id
        self.settings = settings

        self.ws: Optional[web.WebSocketResponse] = None
        self.raw_fifo: Optional[str] = None
        self.proc_fifo: Optional[str] = None
        self._raw_fd: Optional[int] = None
        self.ffmpeg_proc: Optional[asyncio.subprocess.Process] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._closed = False
        self.started_at = time.monotonic()

    def _build_filter(self) -> str:
        from helpers.audio_processor import build_ffmpeg_filter, _sanitize_ffmpeg_filter
        s = self.settings
        return _sanitize_ffmpeg_filter(build_ffmpeg_filter(
            volume=s.get("volume", 1000),
            bass=s.get("bass", 8),
            echo=s.get("echo", False),
            echo_level=s.get("echo_level", 2),
            boost=s.get("boost", 10),
            relay_volume=s.get("relay_volume", 1000),
            gain=s.get("gain", 180),
            treble=s.get("treble", 90),
        ))

    async def start(self) -> bool:
        """Create FIFOs, start FFmpeg, begin streaming into the VC."""
        loop = asyncio.get_running_loop()

        self.raw_fifo = tempfile.mktemp(suffix="_raw", prefix="livemic_raw_")
        self.proc_fifo = tempfile.mktemp(suffix="_proc", prefix="livemic_proc_")
        os.mkfifo(self.raw_fifo)
        os.mkfifo(self.proc_fifo)

        af = self._build_filter()

        ffmpeg_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "s16le", "-ar", "48000", "-ac", "1",
            "-i", self.raw_fifo,
            "-af", af,
            "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le",
            "-f", "wav",
            self.proc_fifo,
        ]

        # Start FFmpeg (it blocks on opening FIFOs until both ends are connected)
        self.ffmpeg_proc = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        # Drain stderr continuously to prevent pipe buffer deadlock
        async def _drain_stderr():
            assert self.ffmpeg_proc is not None
            assert self.ffmpeg_proc.stderr is not None
            try:
                while True:
                    chunk = await self.ffmpeg_proc.stderr.read(4096)
                    if not chunk:
                        break
                    logger.debug("FFmpeg stderr for user %s: %s",
                                 self.user_id, chunk.decode(errors="replace").strip())
            except Exception:
                pass
        self._stderr_task = asyncio.create_task(_drain_stderr())

        # Open raw FIFO for writing in a thread with timeout (blocks until FFmpeg opens read end)
        async def _open_raw_with_timeout():
            fd_holder = []
            def _open_raw():
                fd_holder.append(os.open(self.raw_fifo, os.O_WRONLY))
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(None, _open_raw), timeout=15.0
                )
                return fd_holder[0] if fd_holder else None
            except asyncio.TimeoutError:
                raise RuntimeError(
                    "FFmpeg FIFO open timeout — FFmpeg shuru nahi hua. "
                    "Check karein ki FFmpeg installed hai aur VC on hai."
                )
        self._raw_fd = await _open_raw_with_timeout()

        # Start py-tgcalls playing from the proc FIFO
        from pytgcalls.types import MediaStream, AudioQuality
        stream = MediaStream(
            self.proc_fifo, AudioQuality.HIGH,
            video_flags=MediaStream.Flags.IGNORE,
        )

        try:
            try:
                await self.uvc.calls.play(self.chat_id, stream)
            except Exception:
                if not await self.uvc.start_voice_chat(self.chat_id):
                    raise RuntimeError("Voice chat start nahi hua. Group me VC start karein.")
                await self.uvc.calls.play(self.chat_id, stream)
        except Exception as exc:
            logger.error("Live mic py-tgcalls play failed: %s", exc)
            await self._cleanup_resources()
            raise

        st = self.uvc.state(self.chat_id)
        st.is_playing = True
        st.is_paused = False
        st.mic_enabled = True
        st.mic_boost_user_id = self.user_id
        st.source_name = "Live Mic"
        st.live_relay = True

        logger.info("Live mic session started for user %s in chat %s",
                    self.user_id, self.chat_id)
        return True

    async def run_ws_loop(self):
        """Read PCM chunks from WebSocket and write them to the raw FIFO.

        This is the sole consumer of the WebSocket — called from the WS handler
        after start() succeeds.  Returns when the WebSocket closes or user sends
        'stop'.
        """
        if not self.ws or self._raw_fd is None:
            return
        try:
            async for msg in self.ws:
                if self._closed:
                    break
                if self.ffmpeg_proc and self.ffmpeg_proc.returncode is not None:
                    logger.warning("FFmpeg died (exit %s) for user %s — stopping mic",
                                   self.ffmpeg_proc.returncode, self.user_id)
                    try:
                        await self.ws.send_str("error:FFmpeg process ended")
                    except Exception:
                        pass
                    break
                if msg.type == WSMsgType.BINARY:
                    data = msg.data
                    if not data:
                        continue
                    try:
                        os.write(self._raw_fd, data)
                    except (OSError, BrokenPipeError):
                        logger.warning("Raw FIFO write failed for user %s", self.user_id)
                        break
                elif msg.type == WSMsgType.TEXT:
                    if msg.data == "stop":
                        break
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                    break
                elif msg.type == WSMsgType.ERROR:
                    logger.error("WebSocket error for user %s: %s", self.user_id, self.ws.exception())
                    break
        except Exception as exc:
            logger.error("WS loop error for user %s: %s", self.user_id, exc)

    async def stop(self):
        """Stop the live mic relay and clean up all resources."""
        if self._closed:
            return
        self._closed = True

        st = self.uvc.chats.get(self.chat_id)
        if st:
            st.mic_enabled = False
            st.mic_boost_user_id = None
            st.live_relay = False
            st.is_playing = False
            st.source_name = "—"

        if self.ws and not self.ws.closed:
            try:
                await self.ws.close()
            except Exception:
                pass

        await self._cleanup_resources()
        _sessions.pop(self.user_id, None)
        logger.info("Live mic session stopped for user %s", self.user_id)

    async def _cleanup_resources(self):
        """Close FIFOs, kill FFmpeg, leave VC call."""
        if self._raw_fd is not None:
            try:
                os.close(self._raw_fd)
            except OSError:
                pass
            self._raw_fd = None

        if self._stderr_task:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):
                pass
            self._stderr_task = None

        if self.ffmpeg_proc:
            try:
                self.ffmpeg_proc.kill()
                await self.ffmpeg_proc.wait()
            except Exception:
                pass
            self.ffmpeg_proc = None

        try:
            await self.uvc.calls.leave_call(self.chat_id)
        except Exception:
            pass

        for fifo in (self.raw_fifo, self.proc_fifo):
            if fifo and os.path.exists(fifo):
                try:
                    os.unlink(fifo)
                except OSError:
                    pass
        self.raw_fifo = None
        self.proc_fifo = None

    def update_settings(self, settings: dict):
        self.settings = settings


def get_session(user_id: int) -> Optional[LiveMicSession]:
    return _sessions.get(user_id)


def is_active(user_id: int) -> bool:
    return user_id in _sessions


async def create_session(user_id: int, uvc, chat_id: int, settings: dict) -> LiveMicSession:
    existing = _sessions.get(user_id)
    if existing:
        await existing.stop()
    session = LiveMicSession(user_id, uvc, chat_id, settings)
    _sessions[user_id] = session
    return session


async def stop_session(user_id: int) -> bool:
    session = _sessions.get(user_id)
    if not session:
        return False
    await session.stop()
    return True


async def stop_all_for_chat(chat_id: int):
    to_stop = [s for s in _sessions.values() if s.chat_id == chat_id]
    for s in to_stop:
        await s.stop()


async def generate_token(user_id: int, chat_id: int) -> str:
    """Generate a single-use auth token and store it in the database."""
    from helpers.database import db
    secret = secrets.token_urlsafe(16)
    token = f"{user_id}:{chat_id}:{secret}"
    await db.set_app_value(f"live_mic_token_{user_id}", f"{chat_id}:{secret}")
    return token


# --- Web server ---

_app: Optional[web.Application] = None
_runner: Optional[web.AppRunner] = None


LIVE_MIC_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>VC Fyt - Live Mic</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
background:#0a0a0a;color:#fff;min-height:100vh;display:flex;
flex-direction:column;align-items:center;justify-content:center;padding:20px}
.container{max-width:420px;width:100%;text-align:center}
h1{font-size:1.6rem;margin-bottom:8px}
.subtitle{color:#888;font-size:0.9rem;margin-bottom:24px}
.status{padding:16px;border-radius:12px;margin:16px 0;font-size:1rem;font-weight:600}
.status.off{background:#1a1a1a;color:#888;border:1px solid #333}
.status.on{background:#0d2818;color:#4ade80;border:1px solid #22c55e}
.status.err{background:#2d0a0a;color:#f87171;border:1px solid #ef4444}
.status.connecting{background:#1a1a0a;color:#facc15;border:1px solid #eab308}
.btn{width:100%;padding:16px;border-radius:12px;border:none;font-size:1.1rem;
font-weight:700;cursor:pointer;margin:8px 0;transition:all 0.2s}
.btn-on{background:#22c55e;color:#000}
.btn-off{background:#ef4444;color:#fff}
.btn:disabled{opacity:0.4;cursor:not-allowed}
.meter{width:100%;height:8px;background:#1a1a1a;border-radius:4px;margin:12px 0;overflow:hidden}
.meter-fill{height:100%;background:#22c55e;width:0%;transition:width 0.1s}
.info{color:#666;font-size:0.8rem;margin-top:16px;line-height:1.5}
</style>
</head>
<body>
<div class="container">
<h1>VC Fyt Live Mic</h1>
<p class="subtitle">Apni aawaz ko VC mein bhejein - bass, echo, boost ke saath</p>
<div id="status" class="status off">Mic OFF</div>
<div class="meter"><div id="meterFill" class="meter-fill"></div></div>
<button id="toggleBtn" class="btn btn-on" onclick="toggleMic()">Mic ON Karein</button>
<p class="info">
1. Group mein voice chat start karein<br>
2. Niche button daba kar mic permission dein<br>
3. Bolna shuru karein - aawaz VC mein max loud jayegi<br>
Settings change karne ke liye bot mein .mic off then .mic on karein
</p>
</div>
<script>
let ws=null, audioCtx=null, mediaStream=null, workletNode=null, analyser=null;
let isOn=false;
const statusEl=document.getElementById('status');
const meterEl=document.getElementById('meterFill');
const btnEl=document.getElementById('toggleBtn');

function setStatus(text, cls) {
    statusEl.textContent=text;
    statusEl.className='status '+cls;
}

const PCM_WORKLET = `
class PCMP extends AudioWorkletProcessor {
    process(inputs) {
        const input = inputs[0];
        if (!input || !input[0]) return true;
        const ch0 = input[0];
        const buf = new ArrayBuffer(ch0.length * 2);
        const view = new DataView(buf);
        for (let i = 0; i < ch0.length; i++) {
            let s = Math.max(-1, Math.min(1, ch0[i]));
            view.setInt16(i * 2, s * 32767, true);
        }
        this.port.postMessage(buf, [buf]);
        return true;
    }
}
registerProcessor('pcm-processor', PCMP);
`;

async function toggleMic() {
    if (isOn) { stopMic(); return; }
    try {
        btnEl.disabled = true;
        setStatus('Connecting...', 'connecting');

        const token = new URLSearchParams(location.search).get('token');
        if (!token) { setStatus('Token missing', 'err'); btnEl.disabled=false; return; }

        const wsUrl = 'wss://' + location.host + '/ws/mic?token=' + encodeURIComponent(token);
        ws = new WebSocket(wsUrl);
        ws.binaryType = 'arraybuffer';

        ws.onopen = async () => {
            try {
                mediaStream = await navigator.mediaDevices.getUserMedia({
                    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: false },
                });
                audioCtx = new AudioContext({ sampleRate: 48000 });
                await audioCtx.audioWorklet.addModule(
                    URL.createObjectURL(new Blob([PCM_WORKLET], { type: 'application/javascript' }))
                );
                const source = audioCtx.createMediaStreamSource(mediaStream);
                workletNode = new AudioWorkletNode(audioCtx, 'pcm-processor');
                source.connect(workletNode);
                workletNode.port.onmessage = (e) => {
                    if (ws && ws.readyState === 1) ws.send(e.data);
                };
                analyser = audioCtx.createAnalyser();
                analyser.fftSize = 256;
                source.connect(analyser);
                const data = new Uint8Array(analyser.frequencyBinCount);
                function updateMeter() {
                    if (!isOn) return;
                    analyser.getByteFrequencyData(data);
                    let sum = 0;
                    for (let i = 0; i < data.length; i++) sum += data[i];
                    meterEl.style.width = Math.min(100, (sum / data.length) * 1.5) + '%';
                    requestAnimationFrame(updateMeter);
                }
                isOn = true;
                updateMeter();
                setStatus('LIVE - Mic ON', 'on');
                btnEl.textContent = 'Mic OFF Karein';
                btnEl.className = 'btn btn-off';
                btnEl.disabled = false;
            } catch (e) {
                setStatus('Mic error: ' + e.message, 'err');
                btnEl.disabled = false;
                stopMic();
            }
        };

        ws.onerror = () => {
            if (!isOn) { setStatus('Connection error', 'err'); btnEl.disabled = false; }
        };
        ws.onclose = () => { if (isOn) stopMic(); };
    } catch (e) {
        setStatus('Error: ' + e.message, 'err');
        btnEl.disabled = false;
    }
}

function stopMic() {
    isOn = false;
    if (ws) { try { ws.send('stop'); } catch(e){} try { ws.close(); } catch(e){} ws = null; }
    if (workletNode) { try { workletNode.disconnect(); } catch(e){} workletNode = null; }
    if (analyser) { try { analyser.disconnect(); } catch(e){} analyser = null; }
    if (mediaStream) { mediaStream.getTracks().forEach(t => t.stop()); mediaStream = null; }
    if (audioCtx) { try { audioCtx.close(); } catch(e){} audioCtx = null; }
    meterEl.style.width = '0%';
    setStatus('Mic OFF', 'off');
    btnEl.textContent = 'Mic ON Karein';
    btnEl.className = 'btn btn-on';
    btnEl.disabled = false;
}
</script>
</body>
</html>"""


async def _handle_index(request: web.Request) -> web.Response:
    return web.Response(text=LIVE_MIC_HTML, content_type="text/html")


async def _handle_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(max_msg_size=0)
    await ws.prepare(request)

    token = request.query.get("token", "")
    if not token:
        await ws.close()
        return ws

    parts = token.split(":")
    if len(parts) < 3:
        await ws.close()
        return ws

    try:
        user_id = int(parts[0])
        chat_id = int(parts[1])
    except (ValueError, IndexError):
        await ws.close()
        return ws

    secret = ":".join(parts[2:])

    from helpers.vc_manager import session_manager
    from helpers.database import db

    stored = await db.get_app_value(f"live_mic_token_{user_id}")
    if not stored or stored != f"{chat_id}:{secret}":
        await ws.close()
        return ws

    await db.delete_app_value(f"live_mic_token_{user_id}")

    uvc = await session_manager.get(user_id)
    if not uvc:
        await ws.send_str("error:not_logged_in")
        await ws.close()
        return ws

    settings = await db.get_settings(user_id)

    try:
        session = await create_session(user_id, uvc, chat_id, settings)
        session.ws = ws
        await session.start()
    except Exception as exc:
        logger.error("Live mic start failed: %s", exc)
        await ws.send_str(f"error:{exc}")
        await ws.close()
        return ws

    await session.run_ws_loop()
    await session.stop()
    return ws


async def _handle_health(request: web.Request) -> web.Response:
    return web.Response(text="vcfyt bot is running\n", content_type="text/plain")


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", _handle_index)
    app.router.add_get("/health", _handle_health)
    app.router.add_get("/ws/mic", _handle_ws)
    return app


async def start_server(port: int) -> Optional[web.AppRunner]:
    global _app, _runner
    _app = create_app()
    runner = web.AppRunner(_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    _runner = runner
    logger.info("Live mic web server ready on port %s", port)
    return runner


async def stop_server():
    global _runner
    if _runner:
        for session in list(_sessions.values()):
            await session.stop()
        await _runner.cleanup()
        _runner = None
