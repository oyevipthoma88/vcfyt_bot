import asyncio
import os
import re
import tempfile
from typing import Optional

from config import Config

VOLUME_MIN, VOLUME_MAX = 0, 2000
# 1000 = old max (clean).  1001-2000 = OVERDRIVE: extra drive into a hard
# clipper, limiter removed -> louder, awaaz fat sakti hai (by choice).
VOLUME_CLEAN_MAX = 1000
BASS_MIN, BASS_MAX = 0, 100
LEVEL_MIN, LEVEL_MAX = 0, 10
GAIN_MAX = 400
TREBLE_MAX = 120
# Live mic pre-amp ceiling (dB).  Fight mode needs a hotter pre-amp to push
# the compressors hard; 36 dB gives maximum loudness before the limiter.
LIVE_PREGAIN_MAX_DB = 42.0
# Default pre-amp: hot enough to wake any phone mic and drive the chain hard.
LIVE_PREGAIN_DEFAULT_DB = 36.0

def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))

_FILTER_CACHE: Optional[set] = None

def _available_filters() -> set:
    """Filters this FFmpeg build actually has.

    An unknown filter name makes FFmpeg exit instantly, which means total
    silence in the voice chat.  So every optional filter is probed once.
    """
    global _FILTER_CACHE
    if _FILTER_CACHE is None:
        names = set()
        try:
            import subprocess
            out = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-filters"],
                capture_output=True, text=True, timeout=15,
            ).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    names.add(parts[1])
        except Exception:
            names = set()
        _FILTER_CACHE = names
    return _FILTER_CACHE

def _has_filter(name: str) -> bool:
    return name in _available_filters()


def _live_chain_mode() -> str:
    """Which live-mic filter chain to use.

    ``match`` (default) = the same loud chain file playback uses, with only
    the two look-ahead filters swapped for low-latency equivalents — the
    live voice lands at the same level as a played file (the "meri aawaz
    kam, fighters ki tez" fix).  ``soft`` = the gentler broadcast chain
    (one leveller, one compressor, one limiter).  Override with
    ``LIVE_MIC_CHAIN=soft``.
    """
    mode = (os.environ.get("LIVE_MIC_CHAIN", "") or "fight").strip().lower()
    if mode in ("soft", "broadcast", "gentle"):
        return "soft"
    if mode in ("match", "playback", "old"):
        return "match"
    return "fight"


def _db(value: float) -> str:
    return f"{value:.2f}"


def _env_db(name: str, default: float, high: float = 30.0,
            low: float = -30.0) -> float:
    """Env-tunable dB value, clamped to a safe range."""
    try:
        value = float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        value = default
    return max(min(low, high), min(max(low, high), value))

def _sanitize_ffmpeg_filter(value: str) -> str:
    value = re.sub(r"(?i)(knee=)0(?:\.0+)?(?![\d.])", r"\g<1>1", value)
    value = re.sub(r"(?i)(attack=)0(?:\.0+)?(?![\d.])", r"\g<1>0.1", value)
    # FFmpeg acompressor ratio max is 20 — clamp any out-of-range value
    def _clamp_ratio(m):
        ratio = float(m.group(1))
        if ratio > 20.0:
            ratio = 20.0
        elif ratio < 1.0:
            ratio = 1.0
        return f"ratio={ratio:.1f}"
    value = re.sub(r"ratio=(\d+\.?\d*)", _clamp_ratio, value)
    # FFmpeg acompressor knee range is [1 - 8] — clamp any out-of-range value
    def _clamp_knee(m):
        knee = float(m.group(1))
        if knee > 8.0:
            knee = 8.0
        elif knee < 1.0:
            knee = 1.0
        return f"knee={knee:.1f}"
    value = re.sub(r"(?i)knee=(\d+\.?\d*)", _clamp_knee, value)
    # FFmpeg loudnorm LRA range is [1 - 50] — clamp any out-of-range value
    def _clamp_lra(m):
        lra = float(m.group(1))
        if lra < 1.0:
            lra = 1.0
        elif lra > 50.0:
            lra = 50.0
        return f"LRA={lra:.1f}"
    value = re.sub(r"(?i)LRA=(\d+\.?\d*)", _clamp_lra, value)
    # FFmpeg acompressor makeup range is [1 - 64].  A bigger number (the old
    # chain asked for 95) makes FFmpeg refuse the whole filtergraph, playback
    # silently falls back to the RAW file and the audio sounds "normal".
    def _clamp_makeup(m):
        makeup = float(m.group(1))
        return f"makeup={max(1.0, min(64.0, makeup)):.1f}"
    value = re.sub(r"(?i)makeup=(\d+\.?\d*)", _clamp_makeup, value)
    # FFmpeg loudnorm I range is [-70 - -5].  I=0 killed the whole graph.
    def _clamp_i(m):
        target = float(m.group(1))
        return f"I={max(-70.0, min(-5.0, target)):g}"
    value = re.sub(r"(?<![A-Za-z_])I=(-?\d+\.?\d*)", _clamp_i, value)
    # FFmpeg speechnorm expansion 'e' range is [1 - 50] — clamp it
    def _clamp_speechnorm_e(m):
        e = float(m.group(1))
        return f"speechnorm=e={max(1.0, min(50.0, e)):g}"
    value = re.sub(r"(?i)speechnorm=e=(\d+\.?\d*)", _clamp_speechnorm_e, value)
    # FFmpeg alimiter attack range is [0.1 - 80] — clamp any out-of-range value
    # (acompressor attack can go as low as 0.01, so only clamp within alimiter)
    def _clamp_alimiter_attack(m):
        prefix = m.group(1)
        attack = float(m.group(2))
        if attack < 0.1:
            attack = 0.1
        elif attack > 80.0:
            attack = 80.0
        return f"{prefix}{attack:g}"
    value = re.sub(r"(?i)(alimiter=[^,]*?attack=)(\d+\.?\d*)", _clamp_alimiter_attack, value)
    # FFmpeg asoftclip 'param' range is [0.01 - 3] and 'oversample' is [1 - 64].
    # An out-of-range value makes FFmpeg refuse to build the filtergraph and the
    # whole live-mic process dies (exit 222), so clamp it here.
    def _clamp_asoftclip(m):
        body = m.group(1)

        def _p(mm):
            v = float(mm.group(1))
            return f"param={max(0.01, min(3.0, v)):g}"

        def _o(mm):
            v = float(mm.group(1))
            return f"oversample={int(max(1, min(64, v)))}"

        body = re.sub(r"(?i)param=(\d+\.?\d*)", _p, body)
        body = re.sub(r"(?i)oversample=(\d+\.?\d*)", _o, body)
        return f"asoftclip={body}"

    value = re.sub(r"(?i)asoftclip=([^,]*)", _clamp_asoftclip, value)
    return value

def volume_to_db(vol: int) -> float:
    vol = clamp(vol, VOLUME_MIN, VOLUME_CLEAN_MAX)
    if vol <= 500:
        return -30.0 + (30.0 * vol / 500.0)
    # LOUDNESS UPGRADE: the top half of the slider now reaches +48 dB
    # (was +42 dB) so `.vol 1000` is audibly hotter than before.
    return 48.0 * (vol - 500) / 500.0


def extra_loud_db() -> float:
    """User/owner tunable final make-up gain in dB (env LOUD_EXTRA_DB)."""
    try:
        return max(0.0, min(30.0, float(os.environ.get("LOUD_EXTRA_DB", "") or 30.0)))
    except (TypeError, ValueError):
        return 24.0

def gain_to_db(gain: int) -> float:
    # GAIN is real dB now: slider value / 10  (400 -> 40 dB).
    return clamp(gain, 0, GAIN_MAX) / 10.0


def _legacy_gain_to_db(gain: int) -> float:
    """Pre-26-Sep playback mapping: 0-200 slider -> 0-12 dB.

    Playback was reverted to the 24-Sep chain, which was tuned against this
    mapping.  The live mic keeps the new real-dB GAIN scale.
    """
    return 12.0 * clamp(gain, 0, 200) / 200.0


# ---------------------------------------------------------------------------
# LIVE MIC — single fixed chain (no user controls).
#
# Root cause of the "loud but not clear / phati awaaz" live mic:
#   * the browser stacked x10 pre-amp, two compressors (each with automatic
#     make-up gain) and another x6 of post gain, so the phone itself hard-
#     clipped the voice into a square wave before it was ever sent;
#   * the server then added up to ~30 dB more, an atan soft-clip and a limiter
#     driven 15 % into the wall;
#   * sliders / presets could rebuild FFmpeg mid-fight (audio gaps) and push
#     every stage even harder.
# Now the phone sends a clean signal and ALL loudness is made here, once:
#   clean-up -> denoise -> speech leveller (lifts quiet words) -> one
#   compressor -> noise gate (silence between words) -> clarity EQ ->
#   make-up -> brick-wall limiter at -1 dBFS (no clipping, Opus-safe).
# Measured on real speech: quiet (-35 dBFS) and loud (-6 dBFS) phones both
# land at the same loud level, peak -1.0 dBFS, background hiss gated away.
# ---------------------------------------------------------------------------
def build_live_mic_filter(ceiling_db: float = None, loud: int = 0, crunch: int = 0) -> str:
    # LIVE SLIDERS (mic page):
    #   loud   0..100 -> up to +15 dB extra drive into the limiters (fight
    #                    me awaaz tez / denser).  0 = default best chain.
    #   crunch 0..100 -> soft-clip overdrive ("awaaz fategi") so the voice
    #                    cuts through when the other fighter is equally loud.
    # 0..100 = clean (limiter safe).  101..200 = OVERDRIVE: ceiling 0 dBFS,
    # last brick-wall replaced by a hard clip -> max loudness, may distort.
    loud = max(0, min(200, int(loud or 0)))
    crunch = max(0, min(200, int(crunch or 0)))
    overdrive = loud > 100 or crunch > 100
    # HEADROOM FOR TELEGRAM 200 % VOLUME (record_19 analysis):
    # when an ADMIN sets the relay to 200 % participant volume (x2 = +6 dB)
    # a stream peaking at -1 dBFS reached the VC at +5/+6 dBFS and clipped.
    # So with a CONFIRMED admin 200 % the stream ends at ~-6.5 dBFS.
    #
    # ROOT FIX "live mic bohot dheema": that -6.5 dB ceiling was applied
    # ALWAYS — also when the 200 % boost never reached listeners (relay not
    # admin, user account not admin, Telegram reset to 100 %).  Then the
    # voice simply arrived 6.5 dB too quiet.  The session now passes the
    # ceiling it actually needs (see LiveMicSession._ceiling_db): -6.5 dB
    # only while an admin boost is confirmed, otherwise -1 dBFS.
    #
    # LOUDNESS (density): perceived loudness = average level, not peak.
    # Old chain: mean ~8 dB under the peak.  New chain drives the voice into
    # a slow pre-limiter (levels whole words) and then a fast brick-wall
    # (catches transients), so the average sits ~4-5 dB under the ceiling —
    # roughly +4 dB louder to the ear at the same clip-safe peak.
    if ceiling_db is None:
        ceiling_db = _env_db("LIVE_MIC_CEILING_DB", -6.5, high=-0.5, low=-12.0)
    ceiling_db = max(-12.0, min(-0.5, float(ceiling_db)))
    # USER CHOICE: admin ho ya non-admin, awaaz hamesha FULL.  The admin
    # -6.5 dB headroom is skipped unless LIVE_MIC_RESPECT_ADMIN=1.
    if os.environ.get("LIVE_MIC_RESPECT_ADMIN", "0") != "1":
        ceiling_db = max(ceiling_db, -1.0)
    limit = 10 ** (ceiling_db / 20.0)
    pre_limit = min(0.99, limit * 10 ** (2.5 / 20.0))
    drive = _env_db("LIVE_MIC_DRIVE_DB", 10.0, high=14.0, low=0.0)
    f = ["aresample=48000:async=1:first_pts=0",
         "highpass=f=140", "highpass=f=140",
         "lowpass=f=8000", "lowpass=f=8000"]
    if _has_filter("afftdn"):
        f.append("afftdn=nr=20:nf=-42:tn=1")
    # Phone mics arrive at -40..-55 dBFS (browser auto-gain is OFF for
    # clarity).  speechnorm alone could lift only ~28 dB, so quiet phones
    # stayed quiet and the noise gate then chopped words.  Fixed +12 dB
    # pre-amp (float, cannot clip) + stronger expansion fixes that.
    if _has_filter("agate"):
        # Pre-gate on the RAW mic (before any gain): room hiss below
        # ~-64 dBFS is muted so the huge boost below lifts only the voice.
        f.append("agate=threshold=0.0006:range=0.003:ratio=20:attack=1:release=250:detection=peak")
    f.append(f"volume={_db(_env_db('LIVE_MIC_PREAMP_DB', 26.0, high=30.0, low=0.0))}dB")
    if _has_filter("speechnorm"):
        # Lift quiet syllables hard (whisper -> normal level).
        f.append("speechnorm=e=40:r=0.0005:l=1:p=0.95")
    f.append("acompressor=threshold=0.05:ratio=10:attack=2:release=80:makeup=4:knee=4")
    if _has_filter("agate"):
        f.append("agate=threshold=0.025:range=0.05:ratio=6:attack=2:release=180:detection=rms")
    f += ["equalizer=f=300:t=q:w=1:g=-4",
          "equalizer=f=1200:t=q:w=1:g=3",
          "equalizer=f=2600:t=q:w=0.9:g=9",
          "equalizer=f=3800:t=q:w=1.2:g=4"]
    if _has_filter("aexciter"):
        # Presence harmonics: cut through phone speakers.
        f.append("aexciter=amount=0.8:drive=5:freq=3000:ceil=10000")
    # NON-ADMIN GC ROOT FIX: without admin nobody can push the relay to
    # 200 %, so the only loudness left is DENSITY.  Open mode (no admin
    # boost) adds a 3-band compressor (every band pushed up evenly ->
    # voice stays clear, no muddy bass pumping), a ceiling of -0.3 dBFS and
    # +4 dB extra drive into the limiters.  Mean level ends ~3 dB under the
    # peak: the loudest a clean voice can be inside Telegram's Opus.
    open_mode = ceiling_db > -3.0
    if open_mode:
        ceiling_db = max(ceiling_db, -0.3)
        limit = 10 ** (ceiling_db / 20.0)
        pre_limit = min(0.995, limit * 10 ** (2.0 / 20.0))
        drive += _env_db("LIVE_MIC_OPEN_EXTRA_DB", 8.0, high=12.0, low=0.0)
        if _has_filter("mcompand"):
            f.append("mcompand=0.005\\,0.1 6 -47/-40\\,-34/-34\\,-17/-33\\,0/-30 300 "
                     "| 0.003\\,0.05 6 -47/-40\\,-34/-34\\,-17/-30\\,0/-26 2500 "
                     "| 0.000625\\,0.03 6 -47/-40\\,-34/-34\\,-17/-32\\,0/-28 20000")
            f.append("volume=18dB")
    if crunch > 0:
        # Push the voice into a tanh soft-clipper, then pull it back: adds
        # harmonics (gritty / "phati" awaaz) while the limiters stay in charge.
        cdb = crunch * 0.24            # up to +48 dB into the clipper
        f.append(f"volume={_db(cdb)}dB")
        if _has_filter("asoftclip"):
            f.append("asoftclip=type=tanh")
        else:
            f.append("alimiter=limit=0.5:level=false:attack=0.1:release=5")
        # Above 100 less is pulled back -> the distortion stays loud.
        f.append(f"volume={_db(-cdb * (0.6 if crunch <= 100 else 0.3))}dB")
    drive += loud * 0.20               # up to +40 dB extra loudness
    if overdrive:
        # FATNE DE: full-scale ceiling, levelling limiter then a hard clip
        # at 0 dBFS instead of the clean brick-wall.  Peaks get chopped
        # (distortion) but the average level is the highest possible.
        f += [f"volume={_db(12.0 + 1.0 + drive)}dB",
              "alimiter=limit=0.999:level=false:attack=5:release=80",
              f"volume={_db(min(12.0, (max(loud, crunch) - 100) * 0.12))}dB"]
        if _has_filter("asoftclip"):
            f.append("asoftclip=type=hard:threshold=1")
        else:
            f.append("alimiter=limit=1:level=false:attack=0.1:release=2")
        return ",".join(f)
    f += [f"volume={_db(12.0 + ceiling_db + 1.0 + drive)}dB",
          # Stage 1: slow leveller-limiter (whole words dense, no pumping).
          f"alimiter=limit={pre_limit:.3f}:level=false:attack=5:release=80",
          # Stage 2: fast brick-wall at the ceiling (no clipping, Opus-safe).
          f"alimiter=limit={limit:.3f}:level=false:attack=0.5:release=15"]
    return ",".join(f)


def build_ffmpeg_filter(
    volume: int = None,
    bass: int = None,
    echo: bool = None,
    echo_level: int = None,
    boost: int = None,
    relay_volume: int = None,
    gain: int = None,
    treble: int = None,
    extra_filters: str = "",
    live: bool = False,
    pregain: float = None,
    turbo: float = None,
    clarity: float = None,
    stream: bool = False,
) -> str:
    # stream=True: file playback piped live to the VC.  It keeps the full
    # playback loudness but swaps look-ahead filters (37 s dynaudnorm window,
    # 3 s loudnorm) for low-latency ones, otherwise the VC sits at 0:00.
    if live:
        # Live mic ignores every slider/setting: one fixed clean+loud chain.
        return build_live_mic_filter()
    low_lat = bool(live or stream)
    if volume is None:
        volume = relay_volume if relay_volume is not None else Config.DEFAULT_VOLUME
    vol = clamp(volume, VOLUME_MIN, VOLUME_MAX)
    bass_value = clamp(bass if bass is not None else Config.DEFAULT_BASS, BASS_MIN, BASS_MAX)
    use_echo = Config.DEFAULT_ECHO if echo is None else bool(echo)
    echo_value = clamp(echo_level if echo_level is not None else Config.DEFAULT_ECHO_LEVEL, LEVEL_MIN, LEVEL_MAX)
    boost_value = clamp(boost if boost is not None else Config.DEFAULT_BOOST, LEVEL_MIN, LEVEL_MAX)
    gain_value = clamp(gain if gain is not None else Config.RELAY_DEFAULT_GAIN, 0, GAIN_MAX)
    treble_value = clamp(treble if treble is not None else Config.RELAY_DEFAULT_TREBLE, 0, TREBLE_MAX)

    # NOTE: dynaudnorm's gaussian window is pure look-ahead latency
    # (f=150ms x g=250 frames = ~37 seconds!).  That is fine for file playback
    # but it made the live mic silent / seconds behind, so the live path uses a
    # short window.
    #
    # LIVE CHAIN SELECTION
    # --------------------
    # Playback (file) audio is loud and punchy because it runs the big shared
    # chain at the bottom of this function (+20 dB -> 3 compressors -> extra
    # gain -> hard soft-clip -> limiter).  The live mic used a completely
    # different, much gentler chain, which is exactly why the same voice
    # sounded weaker/softer live than a played file.
    #
    # Default now: the live mic runs the SAME chain as playback, with only the
    # two filters that are impossible live swapped for low-latency equivalents
    # (short-window dynaudnorm instead of the 37 s gaussian window, speechnorm
    # instead of loudnorm's 3 s look-ahead).  Playback is untouched.
    #
    # Set LIVE_MIC_CHAIN=soft to go back to the previous gentle live chain.
    # LIVE MIC LOUDNESS BOOST: the live chain used weaker levelling than
    # playback (speechnorm e=25, dynaudnorm g=5), so the same voice sounded
    # softer live than a played file.  Now the live chain uses stronger
    # levelling: speechnorm e=45 (up from 25) and dynaudnorm g=12 (up from 5),
    # plus an extra +6 dB drive into the limiter.  Playback is untouched.
    if live and _live_chain_mode() == "fight":
        # ---------------------------------------------------------------
        # LIVE MIC — FIGHT CHAIN (BRUTAL max loudness + zero khar-khar).
        #
        # VC fight me 2 users ek sath bolte hai. Opponent ki aawaz phone
        # speaker se mic me leak hoti hai aur compressors me tumhari aawaz
        # ko daba deti hai. Isliye:
        #   - Dialogue enhance: speech ko background se boost
        #   - Triple speechnorm: har dheema syllable bhi ceiling pe
        #   - Crystalizer: transients sharp = consonants punchy
        #   - Triple compressor: RMS ko absolute max tak push
        #   - Extrastereo: stereo width badhao = phone speaker pe louder feel
        #   - Haas: stereo widener (phone speaker psychoacoustic loudness)
        # Silence between words = true digital silence.
        # Voice sits at ~0 dBFS RMS — EXTREMELY loud and crystal clear.
        # ---------------------------------------------------------------
        try:
            clarity_amt = max(0.0, min(35.0, float(35 if clarity is None else clarity))) / 35.0
        except (TypeError, ValueError):
            clarity_amt = 1.0
        try:
            drive_db = max(30.0, min(42.0, 30.0 + (float(pregain if pregain is not None else 200) / 200.0) * 12.0))
        except (TypeError, ValueError):
            drive_db = 42.0
        final_db = 20.0 + 18.0 * (gain_value / float(GAIN_MAX or 400))
        final_db = min(44.0, final_db + extra_loud_db())

        # FIX: old chain used "dialoguenhance=recipe=default" — that option does
        # not exist (and dialoguenhance needs stereo), so FFmpeg died on start
        # and NO mic audio reached the VC.  New chain: every filter valid on
        # mono input; whisper-level voice is lifted to ~-4 dBFS RMS, normal
        # voice to ~-2.4 dBFS RMS (max density, peak -0.9 dBFS), hiss -> silence.
        filters = [
            "aresample=48000:first_pts=0:async=1",
            "highpass=f=85",
            "lowpass=f=14000",
            "equalizer=f=350:t=q:w=1.2:g=-3.50",
        ]
        if _has_filter("afftdn"):
            filters.append("afftdn=nr=10:nf=-50:tn=1")
        if _has_filter("agate"):
            filters.append("agate=range=0.03:threshold=0.0012:ratio=3:"
                           "attack=3:release=150:knee=2:detection=rms")
        filters.append(f"equalizer=f=1800:t=q:w=1.2:g={_db(3.0 + 2.0 * clarity_amt)}")
        filters.append(f"equalizer=f=2800:t=q:w=1.1:g={_db(4.0 + 2.5 * clarity_amt)}")
        filters.append(f"equalizer=f=4500:t=q:w=1.3:g={_db(2.5 + 1.5 * clarity_amt)}")
        if _has_filter("speechnorm"):
            filters.append("speechnorm=e=12:c=2:r=0.001:f=0.001:p=0.95:l=1")
        filters.append("acompressor=threshold=0.15:ratio=4.0:attack=2:release=50:makeup=4.0:knee=2")
        if use_echo and echo_value:
            d1 = 90 + echo_value * 20
            decay = min(0.35, 0.10 + echo_value * 0.03)
            filters.append(f"aecho=1.0:0.85:{d1}|{d1 * 2}:{decay:.2f}|{decay * 0.5:.2f}")
        if extra_filters:
            filters.append(extra_filters)
        filters.append(f"volume={_db(min(6.0, 2.0 + final_db * 0.08))}dB")
        filters.append("alimiter=level_in=1.15:level_out=1:limit=0.96:"
                       "attack=1:release=25:level=false:asc=1")
        return _sanitize_ffmpeg_filter(",".join(filters))

    if live and _live_chain_mode() == "soft":

        # ---------------------------------------------------------------
        # LIVE MIC — BROADCAST CHAIN (rewritten 28-Sep).
        #
        # Every earlier version tried to buy loudness with raw gain:
        # +30 dB pre-amp -> two compressors with +14 dB makeup each
        # -> +12/+12/+8 dB of EQ boosts -> +14 dB drive -> +18 dB turbo
        # -> saturator -> two limiters.  That is ~90 dB of gain aimed at a
        # 0 dBFS ceiling, so the limiters were flat-out the entire time.
        # Result: no dynamics, no consonants, pumping noise floor — i.e.
        # exactly the "aavaj bhi nahi, clarity bhi nahi" complaint.  Louder
        # numbers were making the voice quieter to the ear.
        #
        # Real loudness = clean signal -> gentle shaping -> ONE leveller
        # -> ONE compressor -> ONE brick-wall limiter, with total gain
        # budgeted so the limiter only kisses peaks.  Opus (what Telegram
        # actually sends) also needs ~3 dB of headroom or it adds its own
        # crackle, so the ceiling is 0.94, not 0.99.
        # ---------------------------------------------------------------

        # clarity / turbo sliders are legacy 0-35 / 0-24 scales; map them to
        # a few dB of tone and drive instead of double-digit boosts.
        try:
            clarity_v = max(0.0, min(35.0, float(18 if clarity is None else clarity)))
        except (TypeError, ValueError):
            clarity_v = 18.0
        clarity_amt = clarity_v / 35.0          # 0 .. 1

        filters = [
            "aresample=48000:first_pts=0:async=1",
            # Rumble / pocket handling noise only.
            "highpass=f=90",
            # Keep real speech brightness; above this is only mic hiss.
            "lowpass=f=15000",
            # Mains hum notch (phone mics pick this up in every room).
            "equalizer=f=50:t=q:w=1.0:g=-5.00",
        ]

        # Denoise BEFORE any gain, gently.  Heavy denoise is what made the
        # voice sound underwater in the previous build.
        if _has_filter("afftdn"):
            filters.append("afftdn=nr=10:nf=-45:tn=1")

        # Boxiness cut — the single biggest clarity win, and it costs no level.
        filters.append("equalizer=f=300:t=q:w=1.1:g=-4.50")

        if bass_value:
            filters.append(
                f"lowshelf=f=140:g={_db(min(3.0, bass_value * 0.04))}")

        # Intelligibility bands: a few dB, not twelve.
        filters.append(f"equalizer=f=1800:t=q:w=1.3:g={_db(2.5 + 2.0 * clarity_amt)}")
        filters.append(f"equalizer=f=3000:t=q:w=1.1:g={_db(3.0 + 2.5 * clarity_amt)}")
        # Consonant bite (t/k/s) so words stay readable on phone speakers.
        filters.append(f"equalizer=f=4800:t=q:w=1.6:g={_db(2.0 + 1.5 * clarity_amt)}")
        # Air, scaled off the treble slider.
        filters.append(f"highshelf=f=7000:g={_db(min(3.0, treble_value * 0.025))}")
        # Add a very light upper-harmonic exciter for phone speakers, then
        # de-ess it so the extra intelligibility does not become harsh hiss.
        if _has_filter("aexciter"):
            # This FFmpeg build requires aexciter ceil >= 9999 Hz.
            filters.append("aexciter=amount=0.12:drive=1.8:freq=3500:ceil=10000")
        if _has_filter("deesser"):
            filters.append("deesser=i=0.20:m=0.35:f=0.50")
        else:
            filters.append("equalizer=f=7600:t=q:w=2.2:g=-3.00")

        # GAIN BUDGET (clean + loud): total ~+30 dB max, ONE leveller,
        # ONE compressor, ONE limiter that only touches peaks.  The previous
        # chain stacked ~100 dB (28 dB pre-amp, 3 compressors, +20 dB, +24 dB
        # drive) so the limiter clipped non-stop = loud noise, no words.
        # LOUDNESS UPGRADE: pre-amp ceiling raised 9 dB -> 15 dB.  The limiter
        # at the end still protects the ceiling, so this only lifts the quiet
        # parts instead of clipping.
        try:
            pregain_db = max(0.0, min(15.0, float(pregain if pregain is not None else 6.0) * 0.42))
        except (TypeError, ValueError):
            pregain_db = 9.0
        if pregain_db:
            filters.append(f"volume={_db(pregain_db)}dB")

        # Leveller: lifts quiet syllables up to ~-1 dBFS peaks with only a
        # few ms latency (no look-ahead window like dynaudnorm).
        if _has_filter("speechnorm"):
            filters.append("speechnorm=e=25:c=2:r=0.0005:f=0.001:p=0.92:t=0.015:l=1")
        else:
            filters.append("dynaudnorm=f=10:g=3:p=0.92:m=15:r=0.9:s=0")

        # One gentle glue compressor — density without killing consonants.
        filters.append(
            "acompressor=threshold=0.12:ratio=3.5:"
            "attack=3:release=60:makeup=5.0:knee=3"
        )

        # Echo AFTER levelling: the leveller can't pump the echo tail up into
        # mud, and switching echo off removes it completely.
        if use_echo and echo_value:
            d1 = 90 + echo_value * 20
            decay = min(0.40, 0.12 + echo_value * 0.03)
            filters.append(
                f"aecho=1.0:0.85:{d1}|{d1 * 2}:{decay:.2f}|{decay * 0.5:.2f}"
            )

        if extra_filters:
            filters.append(extra_filters)

        # Final drive, now up to +10 dB (was +6 dB) plus the global
        # LOUD_EXTRA_DB make-up — the limiter below still owns the ceiling.
        try:
            turbo_db = max(0.0, min(10.0, float(turbo or 0) * 0.42))
        except (TypeError, ValueError):
            turbo_db = 0.0
        turbo_db += extra_loud_db()
        if turbo_db:
            filters.append(f"volume={_db(min(18.0, turbo_db))}dB")
        # 0.985 ceiling: louder than the old 0.94 while still leaving Opus a
        # sliver of headroom so it does not crackle.
        filters.append("alimiter=level_in=1.6:level_out=1:limit=0.985:"
                       "attack=1:release=30:level=false:asc=1")

        return _sanitize_ffmpeg_filter(",".join(filters))




    filters = [
        f"highpass=f={30 if bass_value else 75}",
        "aresample=48000",
        "aformat=channel_layouts=stereo",
        "pan=stereo|c0=0.5*c0+0.5*c1|c1=0.5*c0+0.5*c1",
        ("dynaudnorm=f=20:g=3:p=0.95:m=20:r=0.95:s=0" if low_lat
         else "dynaudnorm=f=120:g=15:p=0.96:m=20:r=0.98:s=0"),
    ]

    if bass_value:
        filters.append(f"equalizer=f=80:t=q:w=1.2:g={_db(min(8.0, bass_value * 0.12))}")
        filters.append(f"equalizer=f=160:t=q:w=1.0:g={_db(min(6.0, bass_value * 0.08))}")

    filters.append(f"equalizer=f=3000:t=q:w=1.2:g={_db(min(6.0, -1.0 + treble_value * 0.08))}")
    if not live:
        filters.append("equalizer=f=2600:t=q:w=1.4:g=3.5")
    filters.append(f"equalizer=f=8000:t=q:w=1.2:g={_db(min(5.0, 1.0 + treble_value * 0.06))}")

    ratio = min(6.0, 3.0 + boost_value * 0.3)
    threshold = max(0.05, 0.20 - boost_value * 0.015)
    makeup = min(12.0, boost_value * 1.0 + 3.0)
    filters.append(
        f"acompressor=threshold={threshold:.3f}:ratio={ratio:.1f}:"
        f"attack=1.0:release=40:makeup={makeup:.1f}:knee=2"
    )

    if use_echo and echo_value:
        d1 = 70 + echo_value * 22
        decay = min(0.60, 0.15 + echo_value * 0.04)
        filters.append(
            f"aecho=0.85:0.75:{d1}|{d1 * 2}:"
            f"{decay:.2f}|{decay * 0.5:.2f}"
        )

    filters.append(f"volume={_db(max(4.0, min(18.0, 6.0 + volume_to_db(vol) * 0.3 + _legacy_gain_to_db(gain_value) * 0.5)))}dB")

    if extra_filters:
        filters.append(extra_filters)

    # ROOT FIX "audio normal play ho raha hai":
    #   * loudnorm (I=-12, dynamic) pulled the boosted signal back DOWN ~14 dB;
    #   * asoftclip=type=atan has no make-up gain and cut another ~10 dB.
    # Together they undid every boost stage, so playback came out at the
    # same level as the raw file.  Now: dense leveller -> second glue
    # compressor -> loudness drive -> ONE brick-wall limiter (no level loss).
    if _has_filter("speechnorm"):
        filters.append("speechnorm=e=12:r=0.001:l=1:p=0.95:t=0.01")
    filters.append("acompressor=threshold=0.25:ratio=8:attack=0.5:release=30:makeup=2:knee=2")
    # LOUDER PLAYBACK: +6 dB more drive into the limiter than before.
    filters.append(f"volume={_db(min(24.0, 14.0 + extra_loud_db() * 0.45))}dB")
    filters.append("alimiter=level_in=1:limit=0.98:attack=0.5:release=20:level=false:asc=1")
    # PLAYBACK MAX-DENSITY STAGE (same trick as the live-mic bridge):
    # phase rotator shrinks peaks, then a +PLAY_DRIVE_DB push into a hard
    # clip, de-alias lowpass and a final brick-wall below 0 dBFS.  Peak stays
    # safe (no Opus crackle) while the average level - what the ear hears as
    # "aawaz" - rises several dB.
    if _has_filter("allpass"):
        filters += ["allpass=f=200:t=q:w=0.7", "allpass=f=800:t=q:w=0.7",
                    "allpass=f=2000:t=q:w=0.7"]
    drive = _env_db("PLAY_DRIVE_DB", 27.0, high=30.0, low=0.0)
    if drive and _has_filter("asoftclip"):
        # v5: 2-stage clip (har stage ke baad de-alias) -> zyada dense/tez.
        first = min(drive, 8.0)
        filters.append(f"volume={_db(first)}dB")
        filters.append("asoftclip=type=hard:threshold=0.95")
        filters.append("lowpass=f=15500")
        if drive > first:
            filters.append(f"volume={_db(drive - first)}dB")
            filters.append("asoftclip=type=tanh:threshold=0.95")
            filters.append("lowpass=f=15000")
    filters.append("alimiter=level_in=1:level_out=1:limit=0.99:attack=0.1:release=8:level=false")
    if vol > VOLUME_CLEAN_MAX:
        # PLAYBACK OVERDRIVE (.vol 1001-2000): up to +18 dB more into a hard
        # clip at 0 dBFS.  Louder than 1000; awaaz fat sakti hai (by choice).
        over = (vol - VOLUME_CLEAN_MAX) / float(VOLUME_MAX - VOLUME_CLEAN_MAX)
        filters.append(f"volume={_db(18.0 * over)}dB")
        if _has_filter("asoftclip"):
            filters.append("asoftclip=type=hard:threshold=1")
        else:
            filters.append("alimiter=limit=1:attack=0.1:release=2:level=false")
    return _sanitize_ffmpeg_filter(",".join(filters))

async def process_audio_to_file(
    input_path: str,
    output_path: Optional[str] = None,
    volume: int = None,
    bass: int = None,
    echo: bool = None,
    echo_level: int = None,
    boost: int = None,
    relay_volume: int = None,
    gain: int = None,
    treble: int = None,
    extra_filters: str = "",
) -> str:
    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".wav", prefix="vc_processed_")
        os.close(fd)
    af = _sanitize_ffmpeg_filter(build_ffmpeg_filter(
        volume=volume, bass=bass, echo=echo, echo_level=echo_level,
        boost=boost, relay_volume=relay_volume, gain=gain, treble=treble,
        extra_filters=extra_filters,
    ))
    async def _run(filter_chain: str) -> tuple:
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", input_path, "-vn", "-af", filter_chain,
            "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", output_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, err = await proc.communicate()
        return proc.returncode, err

    code, stderr = await _run(af)
    if code != 0 or not os.path.exists(output_path):
        # LOUD FALLBACK.  If one exotic filter in the big chain is missing or
        # out of range in this FFmpeg build, we must NOT fall back to the raw
        # file — that is what made played audio sound "normal volume".  This
        # minimal chain only uses filters every FFmpeg build has, and still
        # slams the audio to the ceiling.
        fallback = (
            "aresample=48000,"
            "dynaudnorm=f=150:g=15:p=0.95:m=100:r=0.9:s=0,"
            "volume=36dB,",
            "acompressor=threshold=0.01:ratio=20:attack=1:release=50:"
            "makeup=24:knee=2,"
            "loudnorm=I=-5:LRA=1.0:TP=0.0,"
            "volume=12dB,"
            "alimiter=level_in=4:limit=0.99:attack=0.1:release=5:asc=1"
        )
        code2, stderr2 = await _run(fallback)
        if code2 != 0 or not os.path.exists(output_path):
            try:
                os.unlink(output_path)
            except OSError:
                pass
            raise RuntimeError(
                "FFmpeg failed: "
                f"{stderr.decode(errors='replace')[-300:]} | fallback: "
                f"{stderr2.decode(errors='replace')[-300:]}"
            )
    return output_path


# ---------------------------------------------------------------------------
# INSTANT PLAYBACK (Bug 3)
#
# Pre-rendering the whole file with FFmpeg before anything is heard is what
# made `.play` take several seconds on long files.  ntgcalls can read raw PCM
# straight from a shell command, so FFmpeg now streams the processed audio in
# real time: the first frame lands in the voice chat in ~0 s and the filter
# chain is identical to the pre-rendered path.
# ---------------------------------------------------------------------------

def build_stream_command(
    input_path: str,
    volume: int = None,
    bass: int = None,
    echo: bool = None,
    echo_level: int = None,
    boost: int = None,
    relay_volume: int = None,
    gain: int = None,
    treble: int = None,
    extra_filters: str = "",
    loop: bool = False,
    start_at: float = 0.0,
) -> str:
    """Shell command that writes processed 48 kHz stereo s16le PCM to stdout."""
    import shlex

    af = _sanitize_ffmpeg_filter(build_ffmpeg_filter(
        volume=volume, bass=bass, echo=echo, echo_level=echo_level,
        boost=boost, relay_volume=relay_volume, gain=gain, treble=treble,
        extra_filters=extra_filters, stream=True,
    ))
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
           "-fflags", "+nobuffer+genpts", "-threads", "2"]
    if loop:
        # Native, gapless looping — no stream-end round trip, no re-processing.
        cmd += ["-stream_loop", "-1"]
    if start_at and start_at > 0:
        cmd += ["-ss", f"{start_at:.2f}"]
    # Tiny probe window: FFmpeg starts emitting PCM straight away.
    cmd += ["-probesize", "32k", "-analyzeduration", "0"]
    if not (start_at and start_at > 0):
        # 0-sec start: cut the file's leading silence so sound is heard
        # the moment .play hits the VC.
        af = "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.05," + af
    cmd += [
        "-i", input_path, "-vn", "-sn", "-dn", "-af", af,
        "-ar", "48000", "-ac", "2", "-f", "s16le", "-flush_packets", "1",
        "pipe:1",
    ]
    return shlex.join(cmd)


_SS_DEFAULT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "assets", "ss_default.jpg")


_FONT_CANDIDATES = (
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "fonts", "DejaVuSans.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/system/fonts/Roboto-Regular.ttf",
    "/data/data/com.termux/files/usr/share/fonts/TTF/DejaVuSans.ttf",
)


def _find_font() -> str:
    for f in _FONT_CANDIDATES:
        if os.path.exists(f):
            return f.replace(":", "\\:")
    return ""


def build_fake_screen_command(
    width: int = 1280,
    height: int = 720,
    fps: int = 20,
    image_path: str = "",
    title: str = "Audio Setup — Live",
) -> str:
    """Shell command producing a looping raw I420 video for the fake screen share.

    ntgcalls expects raw **yuv420p (I420)** frames for a SHELL video source —
    the old BGRA output gave a broken / green screen.  The picture is a PC
    screenshot of an audio-mixer setup (``assets/ss_default.jpg`` or the photo
    the user replied to) with live-looking animated level meters and a slowly
    drifting mouse cursor on top, so the shared screen never looks frozen.
    """
    import shlex

    width = max(320, min(1920, int(width))) // 2 * 2
    height = max(240, min(1080, int(height))) // 2 * 2
    fps = max(5, min(30, int(fps)))
    img = image_path if (image_path and os.path.exists(image_path)) else _SS_DEFAULT

    # -re: produce frames in real time so the clock/LIVE timer/meters move
    # at true speed instead of racing ahead in the pipe buffer.
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
           "-threads", "2", "-re"]
    if os.path.exists(img):
        cmd += ["-loop", "1", "-framerate", str(fps), "-i", img]
        # Mild sharpen only (strong unsharp added ringing that Telegram's
        # encoder turned into blur at low bitrate).
        base = (f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,"
                "unsharp=3:3:0.4:3:3:0.0")
    else:
        cmd += ["-f", "lavfi", "-i", f"color=c=0x101418:s={width}x{height}:r={fps}"]
        base = "setsar=1"

    # Animated level meters on the mixer strips + blinking status dot.
    W, H = width, height
    # Taskbar height is needed by the custom-photo branch below too — it was
    # defined later, so `.ss on` with a replied photo crashed (UnboundLocalError).
    bh = max(28, H // 16)
    parts = [base]
    use_default = (img == _SS_DEFAULT)
    # NOTE: inside drawbox, `t` in x/y/w/h means *thickness*, not time — the
    # old meters/cursor were therefore frozen.  Animation is done with the
    # timeline `enable=` option (where `t` IS seconds), one box per segment.
    seg_n = 10
    if use_default:
        mw = max(4, W // 150)
        top, span = int(H * 0.364), int(H * 0.386)
        meters = [(int(W * 0.207) + i * int(W * 0.0883), 2.1 + i * 0.37) for i in range(5)]
    else:
        mw = max(6, W // 90)
        top, span = int(H * 0.08), int(H * 0.30)
        meters = [(W - (5 - i) * (mw + 4) - 12, 2.3 + i * 0.41) for i in range(5)]
    seg_h = max(2, span // seg_n)
    for x, sp in meters:
        lvl = f"(0.35+0.6*abs(sin(t*{sp:.2f})*cos(t*{sp * 0.53:.2f})))"
        for k in range(seg_n):
            y = top + span - (k + 1) * seg_h
            col = "0x3ddc84" if k < 6 else ("0xffd23f" if k < 8 else "0xff4040")
            parts.append(
                f"drawbox=x={x}:y={y}:w={mw}:h={seg_h - 1}:color={col}@0.95:t=fill:"
                f"enable='gt({lvl},{k / seg_n:.2f})'"
            )
    r = max(4, H // 120)
    parts.append(
        f"drawbox=x={int(W * 0.749) - r if use_default else 2 * r}:"
        f"y={int(H * 0.918) - r if use_default else H - bh - 3 * r}:w={2 * r}:h={2 * r}:"
        f"color=red@0.95:t=fill:enable='lt(mod(t,1.2),0.7)'"
    )
    # Mouse cursor that moves between spots every few seconds.
    cw, ch = max(6, W // 110), max(10, H // 45)
    spots = [(0.55, 0.45), (0.30, 0.55), (0.62, 0.30), (0.40, 0.70), (0.70, 0.60), (0.25, 0.35)]
    step = 3
    for i, (fx, fy) in enumerate(spots):
        parts.append(
            f"drawbox=x={int(W * fx)}:y={int(H * fy)}:w={cw}:h={ch}:color=white@0.95:t=fill:"
            f"enable='eq(mod(floor(t/{step}),{len(spots)}),{i})'"
        )
    # Real-time Windows-style taskbar: live clock + date (server TZ set via
    # SS_TZ, default IST) and a running "LIVE" timer, so the share looks real.
    fs1, fs2 = max(12, bh * 2 // 5), max(10, bh // 3)
    parts.append(f"drawbox=x=0:y={H - bh}:w={W}:h={bh}:color=0x202020@0.96:t=fill")
    parts.append(f"drawbox=x={bh // 4}:y={H - bh + bh // 4}:w={bh // 2}:h={bh // 2}:"
                 f"color=0x3a96dd:t=fill")
    # drawtext needs a font; on Heroku/Termux without fontconfig it made
    # FFmpeg exit instantly -> the screen share froze and stalled the call.
    font = _find_font()
    if font:
        ff = f"fontfile='{font}':"
        parts.append("drawtext="+ff+"expansion=strftime:text='%I\\:%M\\:%S %p':"
                     f"x=w-tw-{bh // 2}:y={H - bh + bh // 8}:fontsize={fs1}:fontcolor=white")
        parts.append("drawtext="+ff+"expansion=strftime:text='%d-%m-%Y':"
                     f"x=w-tw-{bh // 2}:y={H - fs2 - bh // 8}:fontsize={fs2}:fontcolor=white")
        parts.append(f"drawbox=x={W - bh * 4}:y=8:w={bh * 3 + bh // 2}:h={bh - 6}:color=red@0.85:t=fill")
        parts.append("drawtext="+ff+"text='LIVE %{pts\\:hms}':"
                     f"x={W - bh * 4 + 8}:y={8 + (bh - 6 - fs2) // 2}:fontsize={fs2}:fontcolor=white")
    parts += [f"fps={fps}", "format=yuv420p"]
    cmd += ["-vf", ",".join(parts), "-f", "rawvideo", "-pix_fmt", "yuv420p", "pipe:1"]
    tz = os.environ.get("SS_TZ", "IST-5:30")
    return shlex.join(["env", f"TZ={tz}"] + cmd)
