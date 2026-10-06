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
DEFAULT_PRESET = "full"

# ---------------------------------------------------------------------------
# LOUD MODE — bridge ki aawaz ko playback se bhi zyada hot banata hai.
# Normal live chain (wahi jo .play use karta hai) ke BAAD ek extra stage lagta
# hai: bass punch + presence (2-4 kHz, kaan sabse tez yahin sunta hai) ->
# drive (dB) -> hard/soft clip -> final ceiling.  Telegram 0 dBFS se upar kuch
# nahi bhejta, isliye "zyada aawaz" = zyada drive + saturation; high levels par
# aawaz phategi — ye jaan-boojh kar hai, control se kam karo.
# ---------------------------------------------------------------------------
LOUD_DEFAULT = {"drive": 20, "bass": 15, "presence": 15, "clip": "hard"}
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
    """0..20 -> 0..+80 dB.  Non-admin GC me Telegram volume 100% pe atka
    rehta hai, isliye loudness sirf signal density se aati hai.
    20 = +80 dB = absolute max drive — signal brute-force full scale."""
    return int(level) * 4


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
INPUT_GATE = ("agate=threshold=0.005:ratio=8:range=0.008:attack=1:"
              "release=150:knee=3:detection=rms:makeup=1")

# BRUTAL INPUT LIFT: Telegram VC incoming frames -50..-70 dBFS hote hain.
# 4-stage lift ensures even a whisper reaches full scale:
#   1. +36 dB raw pre-amp (dheemi aawaz ko line level pe laata hai)
#   2. Speechnorm e=50 (max expansion) — har syllable full scale tak push
#   3. +12 dB post-lift drive — compressor mein hard drive ke liye
#   4. Second speechnorm pass — koi bhi dheema syllable miss nahi hota
#   5. Limiter ceiling 0.99 — signal squash nahi hota
# Even dhire se bolne par bhi signal 0 dBFS ke paas pahunchega.
INPUT_LIFT = ("volume=42dB,"
              "speechnorm=e=50:c=2:r=0.0002:f=0.001:p=0.99:t=0.002:l=1,"
              "volume=16dB,"
              "speechnorm=e=50:c=2:r=0.0002:f=0.001:p=0.99:t=0.002:l=1,"
              "alimiter=level_in=1:limit=0.99:attack=0.2:release=10:level=false")


def loud_stage(cfg: dict) -> str:
    from helpers.audio_processor import _has_filter
    c = clean_loud(cfg)
    f = []
    # BASS PUNCH: chest warmth + punch — aawaz "strong" lagti hai, patli nahi.
    if c["bass"]:
        f.append(f"equalizer=f=80:t=q:w=0.7:g={c['bass'] * 2.5:.1f}")
        f.append(f"equalizer=f=120:t=q:w=0.9:g={c['bass'] * 2.0:.1f}")
        f.append(f"equalizer=f=200:t=q:w=1.0:g={c['bass'] * 1.0:.1f}")
    # PRESENCE: phone speaker ka sabse sensitive zone (1.5-5 kHz).
    # MAX lift here = same peak level par kaan ko DBS zyada tez sunai deta hai.
    if c["presence"]:
        f.append(f"equalizer=f=1600:t=q:w=1.3:g={c['presence'] * 2.5:.1f}")
        f.append(f"equalizer=f=2200:t=q:w=1.0:g={c['presence'] * 2.0:.1f}")
        f.append(f"equalizer=f=2800:t=q:w=1.0:g={c['presence'] * 2.2:.1f}")
        f.append(f"equalizer=f=3500:t=q:w=1.1:g={c['presence'] * 2.0:.1f}")
        f.append(f"equalizer=f=5000:t=q:w=1.4:g={c['presence'] * 1.0:.1f}")
    # HARMONIC EXCITER: MAX crispness + phone speaker par cut-through.
    if _has_filter("aexciter"):
        f.append("aexciter=level_in=1:level_out=1:amount=3.0:drive=10:"
                 "blend=0:freq=2800:ceil=12000:listen=0")
    # CRYSTALIZER: sharpens transients = consonants punchier, words sharper.
    if _has_filter("crystalizer"):
        # c= is an on/off clip switch; "c=1.5" made FFmpeg refuse to start.
        f.append("crystalizer=i=2.5")
    # MULTIBAND GLUE: 4-band, har band separately ceiling ke paas pack.
    if _has_filter("mcompand"):
        f.append(
            "mcompand="
            r"0.003\,0.08 10 -90/-90\,-60/-36\,-30/-12\,-12/-6\,0/-4 250 0 0 |"
            r" 0.002\,0.06 10 -90/-90\,-60/-30\,-30/-8\,-12/-5\,0/-3 2000 0 0 |"
            r" 0.001\,0.05 10 -90/-90\,-60/-26\,-30/-6\,-12/-4\,0/-2 5500 0 0 |"
            r" 0.001\,0.04 10 -90/-90\,-60/-32\,-30/-10\,-12/-6\,0/-4 20000 0 0"
        )
    # DOUBLE GLUE COMPRESSOR: har syllable ko ceiling ke paas brute-force pack.
    f.append("acompressor=threshold=0.01:ratio=16:attack=0.2:release=18:"
             "makeup=12:knee=1")
    f.append("acompressor=threshold=0.06:ratio=18:attack=0.15:release=12:"
             "makeup=8:knee=1")
    f.append("acompressor=threshold=0.20:ratio=20:attack=0.1:release=10:"
             "makeup=4:knee=1")
    # FINAL DRIVE: drive_db (0..60 dB) — signal ko absolute max tak push.
    if c["drive"]:
        f.append(f"volume={drive_db(c['drive'])}dB")
    # Pre-limiter extra push.
    f.append("volume=8dB")
    # SOFT CLIP: oversampled — warm saturation + loud harmonics, no crackle.
    if c["clip"] == "hard" and _has_filter("asoftclip"):
        f.append("asoftclip=type=hard:oversample=8")
    elif _has_filter("asoftclip"):
        f.append("asoftclip=type=atan:oversample=8")
    # BRICK-WALL LIMITER: absolute max ceiling, no digital clipping.
    f.append("alimiter=level_in=1:level_out=1:limit=1.0:attack=0.1:release=5:"
             "level=false:asc=1:asc_level=0.4")
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
PCM_PREAMP = 4

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
        try:
            while not self._closed:
                try:
                    await setter(self.target_chat, rid, 20000, quiet=True)
                except Exception:
                    pass
                await asyncio.sleep(delays.pop(0) if delays else 45)
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
