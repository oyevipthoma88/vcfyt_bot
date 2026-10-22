#!/usr/bin/env python3
"""Telegram Desktop-style LIVE voice chat screen -> raw I420 to stdout."""
import argparse, json, math, os, sys, time
from datetime import datetime, timedelta, timezone
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
FDIR = os.path.join(os.path.dirname(HERE), "assets", "fonts")

BG      = (23, 33, 43)
BG2     = (14, 22, 33)
HLINE   = (16, 25, 33)
TILE    = (28, 39, 51)
TILE2   = (18, 28, 40)
TEXT    = (255, 255, 255)
MUTED   = (112, 132, 153)
DIM     = (109, 127, 143)
PANEL      = (18, 22, 30)
NEON_PINK  = (255, 45, 149)
NEON_CYAN  = (0, 245, 255)
NEON_LIME  = (57, 255, 20)
NEON_GREEN = (0, 255, 136)
NEON_YEL   = (255, 230, 0)
NEON_RED   = (255, 51, 51)
BLUE    = (100, 181, 239)
BLUE_D  = (51, 144, 236)
GREEN   = (79, 174, 78)
RED     = (229, 57, 53)
YELLOW  = (255, 210, 63)

AV_COLORS = [(231,76,60),(52,152,219),(46,204,113),(155,89,182),
             (241,196,15),(26,188,156),(230,126,34),(236,64,122)]


_FONT_CACHE = {}

# Multi-font fallback chain — fancy Unicode + emoji support
_FALLBACK_PATHS = [
    # DejaVu (base)
    lambda base: os.path.join(base, "DejaVuSans-Bold.ttf"),
    lambda base: os.path.join(base, "DejaVuSans.ttf"),
    # Noto Core — Latin + Cyrillic + Greek + Arabic + Devanagari
    lambda base: "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    lambda base: "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    # Noto Symbols (fancy Unicode, math, arrows)
    lambda base: "/usr/share/fonts/truetype/noto/NotoSansSymbols-Regular.ttf",
    lambda base: "/usr/share/fonts/truetype/noto/NotoSansSymbols2-Regular.ttf",
    # Symbola — widest Unicode coverage
    lambda base: "/usr/share/fonts/truetype/ancient-scripts/Symbola_hint.ttf",
    lambda base: "/usr/share/fonts/truetype/symbola/Symbola.ttf",
    # Noto CJK
    lambda base: "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]

def _font(size, bold=False):
    size = max(8, int(size))
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    # Try bold first, then regular
    for builder in _FALLBACK_PATHS:
        for base in (FDIR, "/usr/share/fonts/truetype/dejavu"):
            path = builder(base) if callable(builder) else builder
            if path and os.path.exists(path):
                try:
                    f = ImageFont.truetype(path, size)
                    _FONT_CACHE[key] = f
                    return f
                except Exception:
                    continue
    f = ImageFont.load_default()
    _FONT_CACHE[key] = f
    return f


def _tz():
    try: off = float(os.environ.get("SS_TZ_OFFSET", "5.5"))
    except ValueError: off = 5.5
    return timezone(timedelta(hours=off))


def _read_state(p):
    try:
        with open(p, "r", encoding="utf-8") as f: return json.load(f)
    except Exception: return {}


def _fit(d, txt, font, max_w):
    txt = str(txt or "")
    if d.textlength(txt, font=font) <= max_w: return txt
    while txt and d.textlength(txt + "…", font=font) > max_w:
        txt = txt[:-1]
    return txt + "…"


def _initials(name):
    parts = [p for p in str(name or "?").split() if p]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


# ── Fancy-name support (Egyptian, symbols, superscripts) ──
_NAME_FONT_PATHS = [
    "/usr/share/fonts/truetype/symbola/Symbola.ttf",
    "/usr/share/fonts/truetype/ancient-scripts/Symbola_hint.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansSymbols2-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansSymbols-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
]
_INVISIBLE = {0x3164, 0x2800, 0x200B, 0x200C, 0x200D, 0xFEFF, 0x00A0, 0x180E}


def _font_name(size, bold=False):
    """Wide-coverage font for participant names (Symbola preferred)."""
    size = max(8, int(size))
    key = (size, bold, "name")
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    for p in _NAME_FONT_PATHS + [
        os.path.join(FDIR, "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"),
    ]:
        if os.path.exists(p):
            try:
                f = ImageFont.truetype(p, size)
                _FONT_CACHE[key] = f
                return f
            except Exception:
                continue
    return _font(size, bold)


def _sanitize_name(name, size=14, bold=False):
    """Strict filter: English, numbers, basic punct only. No tofu boxes."""
    name = str(name or "User")
    # Allow A-Z, a-z, 0-9, space, dash, underscore, dot
    clean = re.sub(r'[^a-zA-Z0-9\s\-_\.]', '', name).strip()
    if len(clean) < 2:
        # Fallback to first 2 letters
        clean = re.sub(r'[^a-zA-Z0-9]', '', name)[:2].upper()
    return (clean or "User")[:18]

def rgb_to_i420(img):
    import numpy as np
    ycc = np.asarray(img.convert("YCbCr"), dtype=np.uint8)
    y = ycc[..., 0]
    y = (y.astype(np.uint16) * 219 // 255 + 16).astype(np.uint8)
    u = ycc[0::2, 0::2, 1].astype(np.uint16) * 224 // 255 + 16
    v = ycc[0::2, 0::2, 2].astype(np.uint16) * 224 // 255 + 16
    return y.tobytes() + u.astype(np.uint8).tobytes() + v.astype(np.uint8).tobytes()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True)
    ap.add_argument("--w", type=int, default=1280)
    ap.add_argument("--h", type=int, default=720)
    ap.add_argument("--fps", type=int, default=15)
    ap.add_argument("--frames", type=int, default=0)
    a = ap.parse_args()
    w = max(320, min(1920, a.w)) // 2 * 2
    h = max(240, min(1080, a.h)) // 2 * 2
    fps = max(5, min(30, a.fps))
    r = Renderer(w, h)
    out = sys.stdout.buffer
    started = time.time()
    frame, key, last, st = None, None, 0.0, {}
    t0 = time.time(); n = 0
    while True:
        now = time.time()
        if now - last > 0.75:
            st = _read_state(a.state) or st
            last = now
            if st.get("started"):
                started = float(st["started"])
        k = (json.dumps(st, sort_keys=True, default=str), int(now*5))
        if k != key:
            frame = rgb_to_i420(r.render(st, now, started))
            key = k
        try:
            out.write(frame); out.flush()
        except (BrokenPipeError, OSError):
            return
        n += 1
        if a.frames and n >= a.frames: return
        sl = t0 + n / fps - time.time()
        if sl > 0: time.sleep(sl)


if __name__ == "__main__":
    main()
