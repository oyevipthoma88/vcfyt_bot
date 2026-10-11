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
    "bass": {"bass": 5},
    "echo": {"echo": 1, "echo_level": 3},
    "full": {"bass": 4, "echo": 1, "echo_level": 2},
}
# Echo smears words -> sounds farther/quieter. Clean = max clarity.
DEFAULT_PRESET = "clean"

# ---------------------------------------------------------------------------
# FIGHT VOICE (v3) — real recordings se tuned (6-Oct-2026):
#   saamne wala: -2.9 LUFS, energy 400-1000 Hz me, <150 Hz lagbhag zero.
#   hamara bridge: -7.4 LUFS, energy ka bada hissa <150 Hz (bass/boom) me,
#   speech band (300-3500 Hz) ~10 dB dheema.  Bass limiter ki poori jagah kha
#   leta tha, isliye shabd dab jaate the -> "uski aawaz 5 guna tez".
# Fix: bass ko 200 Hz se steep kaat do, aawaz ki body (750 Hz) + presence
# (2.8 kHz) uthao, fast compressor + limiter se density, aur end me "drive" dB
# ka hard clip (Telegram 0 dBFS se upar nahi bhejta; jitna clip utni tez).
# Sim (Opus 48k round-trip): drive 4 ≈ -3.0 LUFS (saamne wale jitna),
# drive 6 ≈ -2.1 LUFS, drive 10 ≈ -1.2 LUFS.
# ---------------------------------------------------------------------------
LOUD_DEFAULT = {"drive": 18, "bass": 0, "presence": 8, "clip": "hard"}
LOUD_PRESETS = {
    "safe": {"drive": 6, "bass": 0, "presence": 5, "clip": "soft"},
    "loud": dict(LOUD_DEFAULT),
    "max":  {"drive": 20, "bass": 0, "presence": 9, "clip": "hard"},
    # FIGHT: saamne wala bhi max par ho tab — sabse tez + thodi phati awaaz.
    "fight": {"drive": 26, "bass": 0, "presence": 10, "clip": "hard"},
    # ULTRA: sabse zyada — aawaz phategi, par sabse tez.
    "ultra": {"drive": 30, "bass": 0, "presence": 10, "clip": "hard"},
}
LOUD_LIMITS = {"drive": (0, 30), "bass": (0, 10), "presence": (0, 10)}
# v3 key: purane saved settings (drive 20 / bass 15) wapas bass-heavy chain na laayein.
_LOUD_KEY = "bridge_loud5_{}"


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
    """Final clip push in dB (0-30 -> 0-30 dB above the limiter ceiling).
    16-30 = FIGHT zone: zyada tez + awaaz thodi phatne lagti hai (by design)."""
    return max(0, min(30, int(level)))


# Soft gate: sirf bolne ke beech ki hiss dabata hai (-18 dB), shabd nahi kaatta.
INPUT_GATE = ("agate=threshold=0.006:ratio=2:range=0.12:attack=2:"
              "release=180:knee=4:detection=rms")


class PcmAgc:
    """Fast Python-side PCM auto gain (s16le mono, 48 kHz).

    Telegram VC frames -50..-70 dBFS ho sakte hain.  Har frame ka RMS dekh ke
    gain smooth badhata/ghatata hai (attack fast, release slow), per-sample
    ramp ke saath (no zipper noise).  Digital khamoshi me gain freeze +
    expander, isliye bolne ke beech hiss full volume par nahi jaati.
    """

    TARGET = 0.63 * 32767        # ~ -4 dBFS RMS (louder input into FFmpeg)
    MAX_GAIN = 1000.0            # +60 dB (bohot dheemi VC input bhi)
    MIN_GAIN = 0.5               # -6 dB
    FLOOR = 0.00012 * 32767      # ~ -78 dBFS: neeche = digital khamoshi
    ATTACK = 0.6                 # gain ghatane ki speed (per frame)
    RELEASE = 0.08               # gain badhane ki speed (per frame)

    def __init__(self):
        self.gain = 8.0          # +18 dB start: pehla shabd bhi dheema na aaye
        self.noise = None        # background noise floor (frame RMS)
        self.duck = 1.0          # smoothed hiss-duck factor (no clicks)
        try:
            import numpy as np
            self._np = np
        except Exception:
            self._np = None

    def _next_gain(self, rms: float) -> tuple:
        g0 = self.gain
        # Noise floor: neeche turant, upar dheere (~1.7 dB/s) -> lagatar bolne
        # par bhi shabdon ke beech ke dips floor ko neeche rakhte hain.
        if self.noise is None or rms < self.noise:
            self.noise = rms
        else:
            self.noise *= 1.002
        if rms < self.FLOOR or rms < self.noise * 3.5:
            # Khamoshi / sirf hiss: gain freeze + duck, taaki AGC hiss ko
            # full volume par na le jaaye (-22 dB).
            return g0, g0, self._duck(0.08)
        want = max(self.MIN_GAIN, min(self.MAX_GAIN, self.TARGET / rms))
        k = self.ATTACK if want < g0 else self.RELEASE
        self.gain = g0 + (want - g0) * k
        return g0, self.gain, self._duck(1.0)

    def _duck(self, target: float) -> tuple:
        # Kholna fast (shabd ki shuruaat na kate), band karna ~80 ms me.
        d0 = self.duck
        k = 0.7 if target > d0 else 0.12
        self.duck = d0 + (target - d0) * k
        return d0, self.duck

    def process(self, data: bytes) -> bytes:
        if not data:
            return data
        np = self._np
        if np is not None:
            x = np.frombuffer(data, dtype=np.int16).astype(np.float32)
            if x.size == 0:
                return data
            rms = float(np.sqrt(np.mean(x * x))) + 1e-9
            g0, g1, att = self._next_gain(rms)
            y = x * (np.linspace(g0 * att[0], g1 * att[1], x.size, dtype=np.float32))
            lim = 29000.0
            over = np.abs(y) > lim
            if over.any():
                y[over] = np.sign(y[over]) * (lim + np.tanh((np.abs(y[over]) - lim) / 3767.0) * 3767.0)
            return np.clip(y, -32768, 32767).astype(np.int16).tobytes()
        a = array.array("h", data)
        n = len(a)
        if n == 0:
            return data
        rms = (sum(v * v for v in a) / n) ** 0.5 + 1e-9
        g0, g1, att = self._next_gain(rms)
        s0, s1 = g0 * att[0], g1 * att[1]
        step = (s1 - s0) / n
        g = s0
        for i in range(n):
            g += step
            v = int(a[i] * g)
            a[i] = 32767 if v > 32767 else (-32768 if v < -32768 else v)
        return a.tobytes()


def loud_stage(cfg: dict) -> str:
    """EQ -> 5 broadcast-style loudness stages -> limiter -> clip+filter.

    v4 (5 naye stages, FM/radio processors (Orban/Stereo Tool) wali technique):
      1. speechnorm   — har shabd/syllable ko turant same level par (dheeme
                        akshar bhi full), zero latency.
      2. phase rotator — 4x allpass: insaani aawaz ka waveform asymmetric hota
                        hai (ek taraf bade peaks). Rotate karne se peaks ~3-5 dB
                        chhote -> utni hi jagah aur gain ke liye (bina distortion).
      3. 3-band multiband compressor — low/mid/high alag-alag dense, isliye
                        bass shabdon ko nahi dabata aur pura band tez lagta hai.
      4. aexciter     — 3 kHz+ par harmonics: phone speaker par aawaz cut-through.
      5. clip + filter — final clip ke BAAD 7.4 kHz lowpass + brick-wall: clip ki
                        kharkhar (aliasing) hat jaati hai jo Opus ke bits khaata
                        tha, aur loudness clip wali hi rehti hai.
    """
    from helpers.audio_processor import _has_filter
    c = clean_loud(cfg)
    hp = 200 - c["bass"] * 8            # bass 0 -> 200 Hz, bass 10 -> 120 Hz
    f = [f"highpass=f={hp}:p=2", f"highpass=f={hp}:p=2",
         "equalizer=f=300:t=q:w=1:g=-4"]
    if c["bass"]:
        f.append(f"equalizer=f={hp + 40}:t=q:w=1:g={c['bass'] * 0.6:.1f}")
    f.append("equalizer=f=750:t=q:w=0.9:g=8")
    if c["presence"]:
        f.append(f"equalizer=f=2800:t=q:w=1:g={c['presence'] * 1.1:.1f}")
        f.append(f"equalizer=f=4200:t=q:w=1.2:g={c['presence'] * 0.5:.1f}")
    f.append("lowpass=f=7500")
    # [1] word leveller
    if _has_filter("speechnorm"):
        f.append("speechnorm=e=50:r=0.001:l=1:p=0.95")
    # [2] phase rotator (peak-to-RMS kam)
    if _has_filter("allpass"):
        f += ["allpass=f=180:t=q:w=0.7", "allpass=f=350:t=q:w=0.7",
              "allpass=f=700:t=q:w=0.7", "allpass=f=1400:t=q:w=0.7"]
    # Density: har shabd lagbhag peak par (RMS up = kaan ko tez).
    f.append("acompressor=threshold=0.05:ratio=20:attack=1:release=40:makeup=10:knee=2")
    # [3] multiband
    if _has_filter("mcompand"):
        f.append("mcompand=0.005\\,0.1 6 -47/-40\\,-34/-34\\,-17/-33\\,0/-30 300 "
                 "| 0.003\\,0.05 6 -47/-40\\,-34/-34\\,-17/-30\\,0/-26 2500 "
                 "| 0.000625\\,0.03 6 -47/-40\\,-34/-34\\,-17/-32\\,0/-28 20000")
        f.append("volume=24dB")
    else:
        f.append("volume=10dB")
    # [4] presence harmonics
    if _has_filter("aexciter"):
        f.append("aexciter=amount=0.8:drive=6:freq=3000:ceil=9999")
    f.append("alimiter=level_in=1.3:level_out=1:limit=0.95:attack=0.5:release=8:level=false")
    d = drive_db(c["drive"])
    if c["clip"] == "hard" and d and _has_filter("asoftclip"):
        # v5 MULTI-STAGE CLIP (broadcast "final clipper" technique):
        # ek bada clip (phat-phat) ki jagah 2-3 chhote clip + har clip ke
        # baad lowpass.  Har stage RMS ko peak ke aur paas laata hai, isliye
        # same 0 dBFS peak par aawaz kaafi zyada dense/tez (+2..4 LU),
        # aur aliasing kam rehta hai (Opus bits bachte hain).
        first = min(d, 12)
        f.append(f"volume={first}dB")
        f.append("asoftclip=type=hard:threshold=0.95")
        f.append("lowpass=f=7400:p=2")
        rest = d - first
        if rest > 0:
            f.append(f"volume={rest}dB")
            f.append("asoftclip=type=hard:threshold=0.95")
            f.append("lowpass=f=7200:p=2")
        # 3rd (fixed) density clip: hamesha thoda push -> loud & dense.
        f.append("volume=5dB")
        f.append("asoftclip=type=tanh:threshold=0.95")
        f.append("lowpass=f=7000:p=2")
        # 4th density stage (ULTRA zone 21+): aur dense, peak same.
        if d > 20:
            f.append(f"volume={min(6, d - 20)}dB")
            f.append("asoftclip=type=hard:threshold=0.95")
            f.append("lowpass=f=7000:p=2")
        # Final ceiling: Opus decoder ~1 dB overshoot karta hai -> 0.89 par
        # lock taaki VC me clip/auto-attenuate na ho.
        f.append("alimiter=level_in=1:level_out=1:limit=0.98:attack=0.1:release=4:level=false")
    elif d:
        # SOFT: drive limiter me jaata hai (kam distortion, thoda kam tez).
        f.append(f"volume={min(d, 9)}dB")
        f.append("asoftclip=type=tanh:threshold=0.97")
        f.append("alimiter=level_in=1:level_out=1:limit=0.97:attack=0.3:release=6:level=false")
    return ",".join(f)


def build_bridge_filter(loud_cfg: Optional[dict], preset: str = DEFAULT_PRESET) -> str:
    """Bridge ki single lean FFmpeg chain (PCM AGC ke baad chalti hai)."""
    from helpers.audio_processor import _sanitize_ffmpeg_filter
    cfg = clean_loud(loud_cfg)
    p = PRESETS.get(preset, {})
    if p.get("bass"):
        cfg["bass"] = max(cfg["bass"], min(10, int(p["bass"])))
    parts = ["aresample=48000:async=1", INPUT_GATE]
    if p.get("echo"):
        # Echo compressor se PEHLE: tail bhi utni hi tez, level nahi girta.
        lvl = max(1, min(5, int(p.get("echo_level", 2))))
        parts.append(f"aecho=0.9:0.7:{40 + lvl * 15}:{0.12 + lvl * 0.04:.2f}")
    parts.append(loud_stage(cfg))
    # Sanitize: one out-of-range value kills FFmpeg = zero bridge audio.
    return _sanitize_ffmpeg_filter(",".join(parts))


async def load_loud(user_id: int) -> dict:
    import json
    from helpers.database import db
    try:
        raw = await db.get_app_value(_LOUD_KEY.format(user_id))
        return clean_loud(json.loads(raw) if raw else None)
    except Exception:
        return dict(LOUD_DEFAULT)


async def save_loud(user_id: int, cfg: dict) -> dict:
    import json
    from helpers.database import db
    c = clean_loud(cfg)
    await db.set_app_value(_LOUD_KEY.format(user_id), json.dumps(c))
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
        self.agc = PcmAgc()
        # True = spare ID target group me VC-admin hai -> uska 200 % volume
        # SAB listeners ke liye lagta hai (asli +6 dB, bina distortion).
        self.boost_all = False

    def feed(self, frames) -> None:
        s = self.session
        if self._closed or s is None or s._closed:
            return
        data = _mix(frames)
        if data:
            data = self.agc.process(data)
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
        bridge = self

        def _build_with_loud():
            return build_bridge_filter(bridge.loud, bridge.preset)

        session._build_filter = _build_with_loud
        self.session = session
        await session.ensure_pipeline(48000)

        _ensure_handler(self.relay)
        # 0) spare ID dono groups ki member ho (main ID / bot invite karte hain)
        from helpers import mic_tools
        for cid in (self.source_chat, self.target_chat):
            await mic_tools.ensure_member(self.uvc, self.relay, cid)
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
        # ASLI BOOST: spare ID ko target me VC-admin banao (sirf "manage
        # video chats" right) -> uska 200 % volume sabke liye lagega.
        try:
            self.boost_all = await mic_tools.ensure_relay_admin(
                self.uvc, self.relay, self.target_chat)
        except Exception:
            self.boost_all = False
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
            "boost_all": self.boost_all,
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
