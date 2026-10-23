import os
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
# FIGHT VOICE (v6) — har awaaz 0 dBFS tak:
#   * PcmAgc: dheemi se dheemi awaaz (-75 dBFS tak) bhi ~-4 dBFS par aati hai.
#   * Smart gate: shabdon ke beech ki hiss / khar-khar ab full volume par NAHI
#     aati (pehle pause me bhi -2.5 dB hiss chalti thi) -> awaaz saaf + tagdi.
#   * Drive  (0-30): last clip stages me push -> density (LUFS) badhta hai.
#   * Phatna (0-10): tube/atan overdrive -> 2-4 kHz harmonics, awaaz "phati"
#                    aur cut-through (phone speaker par sabse tez lagti hai).
#   * Echo   (0-5) : thoda room/echo (0 = band, max clarity).
# Peak hamesha 0 dBFS (Telegram isse upar kuch bhejta hi nahi).
# ---------------------------------------------------------------------------
LOUD_DEFAULT = {"drive": 24, "bass": 0, "presence": 10, "clip": "hard",
                "crunch": 2, "echo": 0, "gate": 1}
LOUD_PRESETS = {
    "safe":  {"drive": 6, "bass": 0, "presence": 5, "clip": "soft", "crunch": 0},
    "loud":  {"drive": 18, "bass": 0, "presence": 8, "clip": "hard", "crunch": 0},
    "max":   {"drive": 22, "bass": 0, "presence": 9, "clip": "hard", "crunch": 2},
    # FIGHT: saamne wala bhi max par ho tab — sabse tez + thodi phati awaaz.
    "fight": {"drive": 26, "bass": 0, "presence": 10, "clip": "hard", "crunch": 5},
    # ULTRA: sabse zyada — aawaz phategi, par sabse tez.
    "ultra": {"drive": 30, "bass": 0, "presence": 10, "clip": "hard", "crunch": 10},
    # v22 GOD: koi daba na sake -> max density, 0 dBFS par lock.
    "god": {"drive": 40, "bass": 0, "presence": 10, "clip": "hard", "crunch": 10},
}
LOUD_LIMITS = {"drive": (0, 40), "bass": (0, 10), "presence": (0, 10),
               "crunch": (0, 10), "echo": (0, 5), "gate": (0, 1)}
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


def apply_preset(cfg: Optional[dict], name: str) -> dict:
    """Preset sirf awaaz ki taakat badalta hai; echo / gate user ka hi rehta hai."""
    c = clean_loud(cfg)
    c.update(LOUD_PRESETS.get(name, {}))
    return clean_loud(c)


def drive_db(level: int) -> int:
    """Final clip push in dB (0-30 -> 0-30 dB above the limiter ceiling).
    16-30 = FIGHT zone: zyada tez + awaaz thodi phatne lagti hai (by design)."""
    return max(0, min(40, int(level)))


# Soft gate (FFmpeg side): AGC ke baad bachi hui halki hiss dabata hai.
INPUT_GATE = ("agate=threshold=0.004:ratio=3:range=0.05:attack=1:"
              "release=200:knee=3:detection=rms")


class PcmAgc:
    """Fast Python-side PCM auto gain + smart noise gate (s16le mono, 48 kHz).

    Telegram VC frames -50..-75 dBFS tak dheeme ho sakte hain (aaram se
    bolna).  Har 10 ms frame ka RMS dekh ke gain smooth badhata/ghatata hai
    (attack fast, release fast-ish), per-sample ramp (no zipper noise).
    Noise floor track hota hai; floor ke paas wale frames (shabdon ke beech ki
    hiss) -46 dB tak duck hote hain, 220 ms hold ke saath taaki shabd ki
    poonch na kate.  Pehle duck sirf -22 dB tha aur FFmpeg ka speechnorm use
    wapas full kar deta tha -> pause me bhi full volume hiss.
    """

    TARGET = 0.85 * 32767        # v21: ~-1.4 dBFS RMS -> har shabd 0 dB ke paas
    MAX_GAIN = 3000.0            # +70 dB (bohot dheemi VC input bhi)
    MIN_GAIN = 0.5               # -6 dB
    FLOOR = 0.00018 * 32767      # ~ -75 dBFS: neeche = digital khamoshi
    SPEECH_X = 3.2               # noise floor se +10 dB upar = awaaz
    ATTACK = 0.6                 # gain ghatane ki speed (per frame)
    RELEASE = 0.15               # gain badhane ki speed (per frame, ~+6 dB / 60 ms)
    HOLD = 60  # v23: ~600 ms anti-duck
    _OLD_HOLD = 45                    # v22 anti-duck: ~450 ms (saans/pause me mic band nahi) gate khula rehta hai
    # Pause = poori khamoshi.  Duck -46 dB bhi kaafi nahi tha: aage ki chain
    # (compressor + multiband + clip) halki hiss ko bhi wapas 0 dB kar deti
    # thi.  Isliye hold ke baad ~60 ms fade aur phir exact 0.
    DUCK = 0.0

    def __init__(self, gate: bool = True):
        self.gain = 12.0         # +21 dB start: pehla shabd bhi dheema na aaye
        self.noise = None        # background noise floor (frame RMS)
        self.duck = 1.0          # smoothed hiss-duck factor (no clicks)
        self.hold = 0
        self.gate = bool(gate)
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
            self.noise = max(rms, 1e-3)
        else:
            self.noise *= 1.002
        speech = rms >= self.FLOOR and rms >= self.noise * self.SPEECH_X
        if speech:
            self.hold = self.HOLD
        elif self.hold > 0:
            self.hold -= 1
        if not speech:
            # Khamoshi / sirf hiss: gain freeze, aur hold ke baad duck.
            open_ = self.hold > 0 or not self.gate
            return g0, g0, self._duck(1.0 if open_ else self.DUCK)
        want = max(self.MIN_GAIN, min(self.MAX_GAIN, self.TARGET / rms))
        k = self.ATTACK if want < g0 else self.RELEASE
        self.gain = g0 + (want - g0) * k
        return g0, self.gain, self._duck(1.0)

    def _duck(self, target: float) -> tuple:
        # Kholna turant (shabd ki shuruaat na kate), band karna ~150 ms fade.
        d0 = self.duck
        k = 0.85 if target > d0 else 0.3
        self.duck = d0 + (target - d0) * k
        if self.duck < 0.002:
            self.duck = 0.0
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
            lim = 31000.0  # v23: zyada level
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


def loud_stage(cfg: dict, focus: bool = False) -> str:
    """EQ -> broadcast-style loudness stages -> limiter -> clip+filter.

      1. speechnorm   — har syllable ko same level par (halki expansion; AGC
                        pehle hi level kar chuka hai, isliye hiss nahi uthti).
      2. phase rotator — 4x allpass: peaks ~3-5 dB chhote -> utni jagah aur gain.
      3. 3-band multiband compressor — pura band dense.
      4. aexciter     — 3 kHz+ harmonics: phone speaker par cut-through.
      5. PHATNA       — atan overdrive (0-10), sirf awaaz ki band me.
      6. clip + filter — multi-stage clip, har clip ke baad lowpass.
    """
    from helpers.audio_processor import _has_filter
    c = clean_loud(cfg)
    hp = 200 - c["bass"] * 8            # bass 0 -> 200 Hz, bass 10 -> 120 Hz
    if focus:
        # NON-ADMIN BOOST: 200% volume nahi mil sakta, isliye energy wahan
        # daalo jahan kaan + phone speaker sabse sensitive hai (1-5 kHz).
        # Low-mid (<300 Hz) headroom khaata hai par tez nahi lagta -> kaato.
        hp = max(hp, 300)
    f = [f"highpass=f={hp}:p=2", f"highpass=f={hp}:p=2",
         f"equalizer=f=300:t=q:w=1:g={-9 if focus else -4}"]
    if c["bass"]:
        f.append(f"equalizer=f={hp + 40}:t=q:w=1:g={c['bass'] * 0.6:.1f}")
    f.append("equalizer=f=750:t=q:w=0.9:g=8")
    if c["presence"]:
        f.append(f"equalizer=f=2800:t=q:w=1:g={c['presence'] * 1.1:.1f}")
        f.append(f"equalizer=f=4200:t=q:w=1.2:g={c['presence'] * 0.5:.1f}")
    if focus:
        f.append("equalizer=f=2500:t=q:w=1.2:g=5")
        f.append("equalizer=f=3600:t=q:w=1:g=4")
    else:
        # v23: hamesha razor presence ON (kaan ka sabse sensitive band).
        f.append("equalizer=f=2500:t=q:w=1.2:g=3")
        f.append("equalizer=f=3600:t=q:w=1:g=2.5")
    f.append("lowpass=f=7500")
    # [1] word leveller — e=6 (+15 dB max): pehle e=50 (+34 dB) pause ki hiss
    # ko bhi full kar deta tha.
    if _has_filter("speechnorm"):
        f.append("speechnorm=e=6:r=0.004:l=1:p=0.95")
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
        f.append("volume=27dB")
    else:
        f.append("volume=13dB")
    # [4] presence harmonics
    if _has_filter("aexciter"):
        f.append("aexciter=amount=0.8:drive=6:freq=3000:ceil=9999")
    f.append("alimiter=level_in=1.3:level_out=1:limit=0.99:attack=0.5:release=8:level=false")
    # [5] PHATNA: atan overdrive -> odd harmonics (gritty, aggressive voice),
    # band-limited after so the grit stays in the 1-7 kHz speech band.
    k = c["crunch"]
    if k and _has_filter("asoftclip"):
        f.append(f"volume={k * 2.4:.1f}dB")
        f.append(f"asoftclip=type={'atan' if k < 6 else 'hard'}:threshold=0.9")
        f.append("equalizer=f=2500:t=q:w=1:g=3")
        f.append("lowpass=f=7300:p=2")
        f.append(f"volume=-{k * 1.2:.1f}dB")
    d = drive_db(c["drive"])
    if c["clip"] == "hard" and d and _has_filter("asoftclip"):
        # MULTI-STAGE CLIP (broadcast "final clipper" technique): 2-4 chhote
        # clip + har clip ke baad lowpass -> same 0 dBFS peak par zyada dense.
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
        if focus:
            # Non-admin: ek aur density clip -> same peak, zyada RMS.
            f.append("volume=4dB")
            f.append("asoftclip=type=hard:threshold=0.95")
            f.append("lowpass=f=6800:p=2")
        # Final ceiling (Opus decoder ~1 dB overshoot karta hai).
        # v12: Volume 25-30 -> ceiling ramps 0.98 -> 1.0 (0 dBFS, last step).
        lim = 1.0  # v21: hamesha exact 0 dBFS
        f.append(f"alimiter=level_in=1:level_out=1:limit={lim:.3f}:attack=0.1:release=4:level=false")
    elif d:
        # SOFT: drive limiter me jaata hai (kam distortion, thoda kam tez).
        f.append(f"volume={min(d, 9)}dB")
        f.append("asoftclip=type=tanh:threshold=0.97")
        f.append("alimiter=level_in=1:level_out=1:limit=1.0:attack=0.3:release=6:level=false")
    else:
        f.append("alimiter=level_in=1:level_out=1:limit=1.0:attack=0.3:release=6:level=false")
    return ",".join(f)


# Extra spare IDs (multi-spare): sab same full-band awaaz bhejte the -> network
# delay ki wajah se lows/mids aapas me cancel (comb filter) hote the aur awaaz
# "gol-gol" lagti thi.  Ab extra IDs sirf 1-7 kHz (sabse tez lagne wala band)
# bhejti hain: main ID poori awaaz, extras us par "presence layer" chadhati hain
# -> har extra ID se non-admin GC me bhi saaf +3..6 dB tez.
EXTRA_LAYER = ("highpass=f=1000:p=2,highpass=f=1000:p=2,"
               "equalizer=f=3000:t=q:w=1:g=3,volume=4dB,"
               "asoftclip=type=hard:threshold=0.95,lowpass=f=7000:p=2,"
               "alimiter=level_in=1:level_out=1:limit=0.98:attack=0.1:release=4:level=false")


def build_bridge_filter(loud_cfg: Optional[dict], preset: str = DEFAULT_PRESET,
                        focus: bool = False, layer: bool = False) -> str:
    """Bridge ki single lean FFmpeg chain (PCM AGC ke baad chalti hai)."""
    from helpers.audio_processor import _sanitize_ffmpeg_filter
    cfg = clean_loud(loud_cfg)
    p = PRESETS.get(preset, {})
    if p.get("bass"):
        cfg["bass"] = max(cfg["bass"], min(10, int(p["bass"])))
    parts = ["aresample=48000:async=1000:min_hard_comp=0.100000:first_pts=0"]
    if cfg["gate"]:
        parts.append(INPUT_GATE)
    lvl = cfg["echo"] or (int(p.get("echo_level", 2)) if p.get("echo") else 0)
    if lvl:
        # Echo compressor se PEHLE: tail bhi utni hi tez, level nahi girta.
        lvl = max(1, min(5, int(lvl)))
        parts.append(f"aecho=0.9:0.7:{40 + lvl * 15}:{0.12 + lvl * 0.04:.2f}")
    parts.append(loud_stage(cfg, focus))
    if layer:
        # Spare-ID layer: untouched (only timestamp sync + 20 ms frames).
        parts.append(EXTRA_LAYER)
    from helpers.audio_processor import apply_sync_and_presence
    chain = apply_sync_and_presence(",".join(parts), boost=not layer)
    # Sanitize: one out-of-range value kills FFmpeg = zero bridge audio.
    return _sanitize_ffmpeg_filter(chain)

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


def _mix(frames) -> bytes:
    """Mix every incoming speaker (ssrc) into one mono s16le frame.

    Pehle per-sample Python loop tha (har 10 ms frame par hazaron operations,
    2-3 log bolein to event loop atakta tha -> awaaz kat-kat ke).  Ab numpy
    me sum + clamp (numpy na ho to purana tarika)."""
    pcm = [bytes(f.frame) for f in frames if getattr(f, "frame", None)]
    if not pcm:
        return b""
    if len(pcm) == 1:
        return pcm[0][: len(pcm[0]) // 2 * 2]
    n = min(len(p) for p in pcm) // 2
    try:
        import numpy as np
        acc = np.zeros(n, dtype=np.int32)
        for p in pcm:
            acc += np.frombuffer(p[: n * 2], dtype=np.int16)
        return np.clip(acc, -32768, 32767).astype(np.int16).tobytes()
    except ImportError:
        acc = [0] * n
        for p in pcm:
            a = array.array("h", p[: n * 2])
            for i in range(n):
                acc[i] += a[i]
        out = array.array("h", (32767 if v > 32767 else (-32768 if v < -32768 else v) for v in acc))
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
        self._stable_since = time.monotonic()
        # MULTI-SPARE: extra spare IDs jo SAME awaaz ek saath target VC me
        # bolti hain (har ek ka apna FFmpeg + stream).
        self.extra_relays: list = []
        self.extras: list = []
        self._extra_task: Optional[asyncio.Task] = None
        # True = target GC me main/spare admin nahi (200% lock nahi lagega)
        # -> Non-admin Boost chain apne aap ON.
        self.focus = False

    def feed(self, frames) -> None:
        s = self.session
        if self._closed or s is None or s._closed:
            return
        data = _mix(frames)
        if data:
            data = self.agc.process(data)
        if not data or s._raw_fd is None or s._restarting:
            return
        for x in [s] + self.extras:
            if x._closed or x._raw_fd is None or x._restarting:
                continue
            x._last_pcm_at = time.monotonic()
            x.queue_pcm(data)
            x._received_bytes += len(data)
            x._first_pcm.set()
        # ZERO-OFFSET LOCK: every extra ID must sit at the exact same playout
        # position as the main ID.  Each session runs its own pacer (trim /
        # underrun / late join), so buffers drift apart by tens of ms ->
        # "aage-piche" echo.  Whenever an extra's queue differs from the
        # main queue by > 2 ms, copy the main queue byte-for-byte.
        if self.extras and s._raw_fd is not None:
            tol = max(2, int(s._frame_bytes // 10)) & ~1   # ~2 ms
            for x in self.extras:
                if x._closed or x._raw_fd is None or x._restarting:
                    continue
                if abs(len(x._buf) - len(s._buf)) > tol:
                    x._buf = bytearray(s._buf)
                    x._jitter_target_ms = s._jitter_target_ms

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
        self.agc.gate = bool(self.loud.get("gate", 1))
        bridge = self

        def _build_with_loud():
            return build_bridge_filter(bridge.loud, bridge.preset, focus=bridge.focus)

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
        asyncio.create_task(self._detect_admin())
        self._watch = asyncio.create_task(self._watchdog())
        if self.extra_relays:
            self._extra_task = asyncio.create_task(self._start_extras(settings))

    async def _start_extras(self, settings: dict) -> None:
        """Har extra spare ID ko target VC me same awaaz ke saath attach karo."""
        from helpers import live_mic
        bridge = self

        def _build_with_loud():
            # SYNC MODE (default): every extra ID sends the EXACT same chain
            # as the main ID -> identical awaaz, coherent sum in listeners.
            # MULTI_LAYER=1 restores the old 1-7 kHz presence layer.
            layer = os.environ.get("MULTI_LAYER", "0") == "1"
            return build_bridge_filter(bridge.loud, bridge.preset,
                                       focus=bridge.focus, layer=layer)

        for relay in list(self.extra_relays):
            if self._closed:
                return
            sess = live_mic.LiveMicSession(self.user_id, self.uvc, self.target_chat,
                                           settings, relay=relay)
            sess.keep_chats = set()
            sess.live_overrides = dict(PRESETS[self.preset])
            sess.input_rate = 48000
            sess._build_filter = _build_with_loud
            try:
                await sess.ensure_pipeline(48000)
                self.extras.append(sess)
                await sess._play_stream()
                try:
                    await relay.calls.change_volume_call(self.target_chat, 200)
                except Exception:
                    pass
                logger.info("Multi-spare %s joined target %s", relay.account_id,
                            self.target_chat)
            except Exception as exc:
                logger.warning("Multi-spare %s fail: %r",
                               getattr(relay, "account_id", "?"), exc)
                if sess in self.extras:
                    self.extras.remove(sess)
                try:
                    await sess.stop()
                except Exception:
                    pass

    async def _detect_admin(self) -> None:
        """Main ID ya spare ID target GC me admin hai? Nahi -> Non-admin Boost ON."""
        s = self.session
        admin = False
        for vc in (self.uvc, self.relay):
            if vc is None or not getattr(vc, "client", None) or s is None:
                continue
            try:
                if await s._is_admin(vc):
                    admin = True
                    break
            except Exception:
                pass
        if self._closed or admin == (not self.focus):
            return
        self.focus = not admin
        logger.info("Bridge %s: target admin=%s -> non-admin boost=%s",
                    self.user_id, admin, self.focus)
        try:
            await self.apply_loud(self.loud)
        except Exception as exc:
            logger.warning("non-admin boost apply fail: %r", exc)

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
                for x in list(self.extras):
                    xid = getattr(x.relay, "account_id", 0)
                    if xid:
                        try:
                            await setter(self.target_chat, xid, 20000, quiet=True)
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
                    self._stable_since = time.monotonic()
                for x in list(self.extras):
                    if x._closed:
                        self.extras.remove(x)
                    elif x.ffmpeg_proc and x.ffmpeg_proc.returncode is not None \
                            and not x._restarting:
                        if not await x._restart_ffmpeg():
                            self.extras.remove(x)
                            await x.stop()
                if s._ffmpeg_restarts and time.monotonic() - self._stable_since > 120:
                    # 2 min stable -> restart budget wapas full.  Pehle 3 crash
                    # poore session me (ghanton me bhi) = bridge hamesha ke liye band.
                    s._ffmpeg_restarts = 0
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
        if self._extra_task and not self._extra_task.done():
            self._extra_task.cancel()
        for x in list(self.extras):
            try:
                await x.stop(leave_vc=leave_target)
            except Exception as exc:
                logger.warning("multi-spare stop failed: %r", exc)
        self.extras = []
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
        self.agc.gate = bool(self.loud.get("gate", 1))
        s = self.session
        if s is None or s._closed or s.ffmpeg_proc is None:
            return False
        for _ in range(20):
            if not s._restarting:
                break
            await asyncio.sleep(0.1)
        ok = True
        for x in [s] + [e for e in self.extras if not e._closed and e.ffmpeg_proc]:
            saved = x._ffmpeg_restarts
            x._ffmpeg_restarts = 0
            try:
                r = await x._restart_ffmpeg(max_attempts=1)
                ok = ok and r if x is s else ok
            finally:
                x._ffmpeg_restarts = saved
        return ok

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
            "boost": self.focus,
            "spares": 1 + sum(1 for x in self.extras if not x._closed),
        }


def get_bridge(user_id: int) -> Optional[VCBridge]:
    return _bridges.get(user_id)


async def start_bridge(user_id: int, uvc, relay, source_chat: int,
                       target_chat: int, settings: dict,
                       preset: str = DEFAULT_PRESET, extra_relays=None) -> VCBridge:
    old = _bridges.get(user_id)
    if old:
        await old.stop()
    bridge = VCBridge(user_id, uvc, relay, source_chat, target_chat, preset)
    bridge.extra_relays = [r for r in (extra_relays or []) if r is not relay]
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
