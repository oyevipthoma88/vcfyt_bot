"""VC Bridge — private VC se target VC me live aawaz relay.

Flow::

    Main ID bolta hai  ->  PRIVATE group ki VC
                               |  (spare ID wahan sirf SUN rahi hai: record)
                               v
                     py-tgcalls incoming PCM (48 kHz mono, 10 ms frames)
                               v
            LiveMicSession pipeline (jitter buffer -> FFmpeg: gain, bass,
            echo, compressor, limiter — wahi chain jo .mic web page use karta hai)
                               v
                     Spare ID -> TARGET group ki VC (volume 200)

Web-mic (browser) ki jagah Telegram ka apna VC audio source ban jata hai, isliye
aawaz phone ke real Telegram mic (echo-cancel + Opus) se aati hai — clear aur
stable, browser ka low/muffled input nahi.
"""

import array
import asyncio
import logging
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Presets on top of live_mic.FIXED_BEST (loud + clear base).
PRESETS = {
    "clean": {},
    "bass": {"bass": 35},
    "echo": {"echo": 1, "echo_level": 3},
    "full": {"bass": 30, "echo": 1, "echo_level": 2},
}
# Echo smears words -> sounds farther/quieter. Clean = max clarity.
DEFAULT_PRESET = "clean"

# ---------------------------------------------------------------------------
# LOUD MODE — bridge ki aawaz ko playback se bhi zyada hot banata hai.
# Normal live chain (wahi jo .play use karta hai) ke BAAD ek extra stage lagta
# hai: bass punch + presence (2-4 kHz, kaan sabse tez yahin sunta hai) ->
# drive (dB) -> hard/soft clip -> final ceiling.  Telegram 0 dBFS se upar kuch
# nahi bhejta, isliye "zyada aawaz" = zyada drive + saturation; high levels par
# aawaz phategi — ye jaan-boojh kar hai, control se kam karo.
# ---------------------------------------------------------------------------
# Bass eats headroom without adding perceived loudness; presence (2-4 kHz)
# is where the ear hears "loud".  Voice-first default.
LOUD_DEFAULT = {"drive": 20, "bass": 4, "presence": 15, "clip": "hard"}
LOUD_PRESETS = {
    "safe": {"drive": 5, "bass": 3, "presence": 6, "clip": "soft"},
    "loud": dict(LOUD_DEFAULT),
    "max":  {"drive": 20, "bass": 15, "presence": 15, "clip": "hard"},
}
LOUD_LIMITS = {"drive": (0, 20), "bass": (0, 15), "presence": (0, 15)}


def clean_loud(cfg: Optional[dict]) -> dict:
    out = dict(LOUD_DEFAULT)
    for k, (lo, hi) in LOUD_LIMITS.items():
        try:
            out[k] = max(lo, min(hi, int((cfg or {}).get(k, out[k]))))
        except (TypeError, ValueError):
            pass
    if (cfg or {}).get("clip") in ("hard", "soft"):
        out["clip"] = cfg["clip"]
    return out


def drive_db(level: int) -> int:
    """Controlled drive for punch and presence without blowing into square wave distortion."""
    return min(16, int(round(int(level) * 1.2)))


# INPUT LIFT: Telegram VC se aane wale incoming frames aksar bohot dheeme
# (-50..-70 dBFS) hote hain.  Main chain ka denoiser (afftdn nf=-45) aur
# leveller itni dheemi aawaz ko noise samajh ke daba dete the -> bridge par
# mic "bohot kam" sunai deta tha.  Ye stage chain ke SABSE PEHLE lagti hai.
#
# MAX LOUDNESS UPGRADE: Pehle sirf +18 dB pre-amp tha — abhi 3 stage hai:
#   1. +28 dB raw pre-amp (dheemi aawaz ko line level pe laata hai)
#   2. Speechnorm e=50 (max expansion) — har syllable ko full scale tak push
#   3. +8 dB post-lift drive — signal ko compressor mein hard drive karne ke liye
#   4. Limiter ceiling 0.98 — signal squash nahi hota, sirf ceiling protect hoti hai
# Ise comparatively dheemi aawaz bhi 0 dBFS ke paas pahunch jayegi.
# NOISE GATE: bolne ke beech ki khamoshi me chain itna gain deti thi ki
# hiss/kharkharahat full volume par jaati thi.  Gate khamoshi ko band rakhta
# hai, isliye bolte waqt contrast zyada = kaan ko aawaz zyada tez lagti hai.
# NOTE: gate hamesha INPUT_LIFT ke BAAD chalta hai.  Raw VC frames -50..-70
# dBFS hote hain, isliye pehle gate lagana speech ko hi kill kar deta tha.
# Soft gate (range -12 dB, was -30 dB): the hard gate was chopping quiet
# syllables and word endings, making the voice sound thin and low.
INPUT_GATE = ("agate=threshold=0.0008:ratio=2:range=0.25:attack=1:"
              "release=250:knee=3:detection=rms")

# BRUTAL INPUT LIFT: Telegram VC incoming frames -50..-70 dBFS hote hain.
# 4-stage lift ensures even a whisper reaches full scale:
#   1. +36 dB raw pre-amp (dheemi aawaz ko line level pe laata hai)
#   2. Speechnorm e=50 (max expansion) — har syllable full scale tak push
#   3. +12 dB post-lift drive — compressor mein hard drive ke liye
#   4. Second speechnorm pass — koi bhi dheema syllable miss nahi hota
#   5. Limiter ceiling 0.99 — signal squash nahi hota
# Even dhire se bolne par bhi signal 0 dBFS ke paas pahunchega.
INPUT_LIFT = ("volume=10dB,"
              "speechnorm=e=12:c=2:r=0.001:f=0.001:p=0.95:t=0.01:l=1,"
              "alimiter=level_in=1:limit=0.97:attack=0.5:release=15:level=false")


def loud_stage(cfg: dict) -> str:
    from helpers.audio_processor import _has_filter
    c = clean_loud(cfg)
    f = []
    # BASS PUNCH: natural warmth without muddy resonant rumble.
    if c["bass"]:
        f.append(f"equalizer=f=100:t=q:w=1.0:g={min(6.0, c['bass'] * 0.4):.1f}")
        f.append(f"equalizer=f=200:t=q:w=1.2:g={min(4.0, c['bass'] * 0.25):.1f}")
    # PRESENCE: speech clarity band (1.8-4.5 kHz) for cutting through VC fights.
    if c["presence"]:
        f.append(f"equalizer=f=1800:t=q:w=1.2:g={min(5.0, c['presence'] * 0.35):.1f}")
        f.append(f"equalizer=f=2800:t=q:w=1.1:g={min(6.0, c['presence'] * 0.45):.1f}")
        f.append(f"equalizer=f=4000:t=q:w=1.3:g={min(4.0, c['presence'] * 0.30):.1f}")
    # Crispness without harsh harmonic feedback.
    if _has_filter("aexciter"):
        f.append("aexciter=level_in=1:level_out=1:amount=0.8:drive=2.5:blend=0:freq=3000:ceil=12000")
    # Punch compressor: density without destroying dynamic consonants.
    f.append("acompressor=threshold=0.12:ratio=4.0:attack=2:release=45:makeup=3.5:knee=2")
    # Controlled drive.
    if c["drive"]:
        f.append(f"volume={drive_db(c['drive'])}dB")
    if _has_filter("asoftclip"):
        f.append("asoftclip=type=atan:oversample=4")
    # Clean brickwall limiter protecting Telegram Opus ceiling.
    f.append("alimiter=level_in=1.1:level_out=1:limit=0.96:attack=0.5:release=20:level=false:asc=1")
    return ",".join(f)


async def load_loud(user_id: int) -> dict:
    import json
    from helpers.database import db
    try:
        raw = await db.get_app_value(f"bridge_loud_{user_id}")
        return clean_loud(json.loads(raw) if raw else None)
    except Exception:
        return dict(LOUD_DEFAULT)


async def save_loud(user_id: int, cfg: dict) -> dict:
    import json
    from helpers.database import db
    c = clean_loud(cfg)
    await db.set_app_value(f"bridge_loud_{user_id}", json.dumps(c))
    b = _bridges.get(user_id)
    if b:
        await b.apply_loud(c)
    return c

# user_id -> bridge
_bridges: Dict[int, "VCBridge"] = {}
# id(relay.calls) -> True once the frame handler is registered
_handlers: Dict[int, bool] = {}


# PCM PRE-AMP MULTIPLIER: raw Telegram VC frames ko Python level par
# hi amplify karte hain BEFORE FFmpeg.  Ye signal ko FFmpeg ke denoiser /
# gate tak pahunchne se pehle hi boost karta hai, taaki dheemi aawaz
# noise samajh ke dab na jaye.  2x = +6 dB raw PCM boost (safe, no clipping).
PCM_PREAMP = 1

def _mix(frames) -> bytes:
    """Mix every incoming speaker (ssrc) into one mono s16le frame.
    PCM samples ko Python level par hi PCM_PREAMPx amplify karta hai,
    FFmpeg se pehle.  clipping int16 range me clamp hota hai (safe)."""
    pcm = [f.frame for f in frames if getattr(f, "frame", None)]
    if not pcm:
        return b""
    if len(pcm) == 1:
        a = array.array("h", bytes(pcm[0]))
        if PCM_PREAMP != 1:
            for i in range(len(a)):
                v = a[i] * PCM_PREAMP
                a[i] = 32767 if v > 32767 else (-32768 if v < -32768 else v)
        return a.tobytes()
    n = min(len(p) for p in pcm) // 2
    out = array.array("h", bytes(n * 2))
    acc = [0] * n
    for p in pcm:
        a = array.array("h", bytes(p[: n * 2]))
        for i in range(n):
            acc[i] += a[i]
    for i in range(n):
        v = acc[i] * PCM_PREAMP
        out[i] = 32767 if v > 32767 else (-32768 if v < -32768 else v)
    return out.tobytes()


def _ensure_handler(relay) -> None:
    calls = relay.calls
    key = id(calls)
    if _handlers.get(key):
        return
    from pytgcalls import filters as call_filters
    from pytgcalls.types import Device, Direction

    # Incoming group-call audio can be tagged MICROPHONE or SPEAKER depending
    # on the ntgcalls build — listen to both, otherwise no frame ever arrives.
    @calls.on_update(call_filters.stream_frame(Direction.INCOMING,
                                               Device.MICROPHONE | Device.SPEAKER))
    async def _on_frames(_, update):  # noqa: ANN001
        for bridge in list(_bridges.values()):
            if bridge.relay.calls is calls and bridge.source_chat == update.chat_id:
                bridge.feed(update.frames)

    _handlers[key] = True


class VCBridge:
    def __init__(self, user_id: int, uvc, relay, source_chat: int,
                 target_chat: int, preset: str = DEFAULT_PRESET):
        self.user_id = user_id
        self.uvc = uvc
        self.relay = relay
        self.source_chat = source_chat
        self.target_chat = target_chat
        self.preset = preset if preset in PRESETS else DEFAULT_PRESET
        self.session = None
        self._watch: Optional[asyncio.Task] = None
        self._vol_task: Optional[asyncio.Task] = None
        self._closed = False
        self.started_at = time.monotonic()
        self.loud = dict(LOUD_DEFAULT)

    def feed(self, frames) -> None:
        s = self.session
        if self._closed or s is None or s._closed:
            return
        data = _mix(frames)
        if not data or s._raw_fd is None or s._restarting:
            return
        s._last_pcm_at = time.monotonic()
        s.queue_pcm(data)
        s._received_bytes += len(data)
        s._first_pcm.set()

    async def start(self, settings: dict) -> None:
        from helpers import live_mic
        from pytgcalls.types import RecordStream
        from pytgcalls.types.raw import AudioParameters

        session = await live_mic.create_session(
            self.user_id, self.uvc, self.target_chat, settings, relay=self.relay)
        session.keep_chats = {self.source_chat}
        session.live_overrides = dict(PRESETS[self.preset])
        session.input_rate = 48000
        self.loud = await load_loud(self.user_id)
        base_build = session._build_filter
        bridge = self

        def _build_with_loud():
            from helpers.audio_processor import _sanitize_ffmpeg_filter
            # Sanitize: one out-of-range value kills FFmpeg = zero bridge audio.
            return _sanitize_ffmpeg_filter(
                INPUT_LIFT + "," + INPUT_GATE + "," + base_build() + "," + loud_stage(bridge.loud))

        session._build_filter = _build_with_loud
        self.session = session
        await session.ensure_pipeline(48000)

        _ensure_handler(self.relay)
        # 1) spare ID PRIVATE VC me listener ban ke join (kuch nahi bolti)
        await self.relay._peer(self.source_chat)
        await self.relay.calls.record(
            self.source_chat, RecordStream(audio=True, audio_parameters=AudioParameters(48000, 1)))
        for n in ("mute", "mute_stream"):
            fn = getattr(self.relay.calls, n, None)
            if fn:
                try:
                    await fn(self.source_chat)
                    break
                except Exception:
                    pass

        # 2) pehli aawaz aate hi TARGET VC me stream attach (30 s wait)
        try:
            await session._play_stream()
        except RuntimeError as exc:
            if "PCM receive nahi hua" in str(exc):
                raise RuntimeError(
                    "Private VC me 30 sec tak koi aawaz nahi aayi. Main ID se private "
                    "group ki VC join karke mic ON karke bolo, phir dobara try karo.") from exc
            raise
        session._mark_started()
        st = self.uvc.state(self.target_chat)
        st.source_name = "VC Bridge"
        # Telegram side max: spare ID ka stream volume 200% (hard cap).
        try:
            await self.relay.calls.change_volume_call(self.target_chat, 200)
        except Exception:
            pass
        # Self-volume sirf spare ID ke liye lagta hai; MAIN ID (group admin)
        # se spare ID ka volume 200% set karo -> sab listeners ke liye tez.
        self._vol_task = asyncio.create_task(self._admin_volume_keeper())
        self._watch = asyncio.create_task(self._watchdog())

    async def _admin_volume_keeper(self) -> None:
        """Main ID (admin) se spare ID ka participant volume 200% pe lock rakho."""
        rid = getattr(self.relay, "account_id", 0)
        setter = getattr(self.uvc, "set_participant_volume", None)
        if not rid or setter is None:
            return
        delays = [0.5, 1.5, 3, 6, 10]
        relay_setter = getattr(self.relay, "set_participant_volume", None)
        try:
            while not self._closed:
                try:
                    await setter(self.target_chat, rid, 20000, quiet=True)
                except Exception:
                    pass
                # Spare ID khud bhi apna stream volume 200% pe lock kare.
                if relay_setter is not None:
                    try:
                        await relay_setter(self.target_chat, rid, 20000, quiet=True)
                    except Exception:
                        pass
                # Telegram kabhi-kabhi volume 100% pe reset karta hai -> 12 s me wapas 200%.
                await asyncio.sleep(delays.pop(0) if delays else 12)
        except asyncio.CancelledError:
            pass

    async def _watchdog(self) -> None:
        """FFmpeg crash -> restart; .mic off / session end -> bridge band."""
        try:
            while not self._closed:
                await asyncio.sleep(2)
                s = self.session
                if s is None or s._closed:
                    break
                if s.ffmpeg_proc and s.ffmpeg_proc.returncode is not None and not s._restarting:
                    if not await s._restart_ffmpeg():
                        break
        except asyncio.CancelledError:
            return
        if not self._closed:
            await self.stop()

    async def stop(self, leave_target: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        _bridges.pop(self.user_id, None)
        if self._watch and not self._watch.done() and self._watch is not asyncio.current_task():
            self._watch.cancel()
        if self._vol_task and not self._vol_task.done():
            self._vol_task.cancel()
        try:
            await self.relay.calls.leave_call(self.source_chat)
        except Exception:
            pass
        self.relay.chats.pop(self.source_chat, None)
        if self.session and not self.session._closed:
            try:
                await self.session.stop(leave_vc=leave_target)
            except Exception as exc:
                logger.warning("bridge session stop failed: %r", exc)

    async def apply_loud(self, cfg: dict) -> bool:
        """Naye loud settings turant lagao — VC stream same rehta hai, sirf FFmpeg swap."""
        self.loud = clean_loud(cfg)
        s = self.session
        if s is None or s._closed or s.ffmpeg_proc is None:
            return False
        for _ in range(20):
            if not s._restarting:
                break
            await asyncio.sleep(0.1)
        saved = s._ffmpeg_restarts
        s._ffmpeg_restarts = 0
        try:
            return await s._restart_ffmpeg(max_attempts=1)
        finally:
            s._ffmpeg_restarts = saved

    def status(self) -> dict:
        s = self.session
        return {
            "source": self.source_chat,
            "target": self.target_chat,
            "preset": self.preset,
            "loud": dict(self.loud),
            "received_kb": (s._received_bytes // 1024) if s else 0,
            "uptime_s": int(time.monotonic() - self.started_at),
            "live": bool(s and not s._closed and s._started),
        }


def get_bridge(user_id: int) -> Optional[VCBridge]:
    return _bridges.get(user_id)


async def start_bridge(user_id: int, uvc, relay, source_chat: int,
                       target_chat: int, settings: dict,
                       preset: str = DEFAULT_PRESET) -> VCBridge:
    old = _bridges.get(user_id)
    if old:
        await old.stop()
    bridge = VCBridge(user_id, uvc, relay, source_chat, target_chat, preset)
    _bridges[user_id] = bridge
    try:
        await bridge.start(settings)
    except Exception:
        await bridge.stop()
        raise
    return bridge


async def stop_bridge(user_id: int, leave_target: bool = False) -> bool:
    bridge = _bridges.get(user_id)
    if not bridge:
        return False
    await bridge.stop(leave_target=leave_target)
    return True
