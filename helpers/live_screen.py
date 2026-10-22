#!/usr/bin/env python3
"""LIVE VC screen for the bot's screen share  —  "NEON STUDIO" edition.

Renders a sharp broadcast-studio style "Voice Chat" window and writes raw
I420 (yuv420p) frames to stdout in real time (ntgcalls SHELL video source).

Real data (from the JSON state file the bot refreshes every ~0.5-1.5 s):
  * every participant in the VC (name, PFP, mic on/off, speaking, hand, volume)
  * now playing track, paused / playing, loop, volume + boost, live mic
  * real clock + LIVE timer

Studio dressing (animated, for the "live set-up" look): spinning vinyl deck,
spectrum analyser, 4-channel mixer with VU meters, FX chain, network stats
and a scrolling ticker.

FANCY NAMES FIX: names like "𝐃𝐄𝐕𝐈𝐋 ✨🥀 ꧁ᴀsʜɪsʜ꧂" used to show as boxes
(DejaVu has no glyph for them).  Text is now drawn per-character with a
font fallback chain (Noto Sans → Math → Symbols → Emoji → Devanagari …);
anything still missing is NFKC-normalised (𝓓 → D) or dropped — never a box.

Frames are only re-rendered when something changes (state update or the
animation tick); in between the cached frame is resent.

Usage: live_screen.py --state FILE [--w 1280 --h 720 --fps 15]
"""
import argparse
import json
import math
import os
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(os.path.dirname(HERE), "assets", "fonts")
SYS_DEJAVU = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
SYS_DEJAVU_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _fp(name):
    return os.path.join(FONT_DIR, name)


# (path, variation-name or None)
_PRIMARY = {
    "ui": [(_fp("NotoSans.ttf"), "Medium"), (_fp("DejaVuSans.ttf"), None), (SYS_DEJAVU, None)],
    "bold": [(_fp("NotoSans.ttf"), "Bold"), (_fp("DejaVuSans-Bold.ttf"), None),
             (SYS_DEJAVU_B, None), (_fp("DejaVuSans.ttf"), None)],
    "display": [(_fp("Orbitron.ttf"), "Black"), (_fp("NotoSans.ttf"), "Black")],
    "tech": [(_fp("Rajdhani-Bold.ttf"), None), (_fp("NotoSans.ttf"), "SemiBold")],
}
_FALLBACK = [
    (_fp("NotoSans.ttf"), "SemiBold"),
    (_fp("DejaVuSans.ttf"), None),
    (SYS_DEJAVU, None),
    (_fp("NotoSansMath-Regular.ttf"), None),
    (_fp("NotoSansSymbols2-Regular.ttf"), None),
    (_fp("NotoSansSymbols.ttf"), None),
    (_fp("NotoEmoji.ttf"), "Bold"),
    (_fp("NotoSansDevanagari.ttf"), "SemiBold"),
    (_fp("NotoSansCanadianAboriginal.ttf"), None),
]

# ---------------------------------------------------------------- palette
BG0 = (6, 7, 14)
BG1 = (14, 10, 26)
GLASS = (18, 20, 34)
GLASS2 = (26, 28, 46)
LINE = (44, 48, 74)
TEXT = (240, 244, 255)
MUTED = (128, 136, 168)
DIM = (70, 76, 104)
CYAN = (0, 229, 255)
PINK = (255, 46, 136)
VIOLET = (140, 92, 255)
GREEN = (46, 255, 156)
YELLOW = (255, 214, 64)
RED = (255, 60, 80)
ORANGE = (255, 140, 40)
TILE_COLORS = [(255, 46, 136), (0, 200, 255), (46, 230, 150), (150, 100, 255),
               (255, 190, 40), (0, 220, 200), (255, 120, 60), (240, 80, 200)]


def _mix(a, b, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


# ================================================================ text engine
class _Text:
    """Per-character font fallback so fancy Unicode names never show boxes."""

    def __init__(self):
        self._fonts = {}      # (path, var, size) -> FreeTypeFont
        self._probe = {}      # (path, var) -> (font24, notdef_sig)
        self._cover = {}      # (path, var, ch) -> bool
        self._chain = {}      # (style, size) -> [fonts]
        self._char = {}       # (style, ch) -> (idx | None, replacement)

    def _load(self, path, var, size):
        key = (path, var, size)
        f = self._fonts.get(key)
        if f is None and key not in self._fonts:
            try:
                f = ImageFont.truetype(path, size) if os.path.exists(path) else None
                if f is not None and var:
                    try:
                        f.set_variation_by_name(var)
                    except Exception:
                        pass
            except Exception:
                f = None
            self._fonts[key] = f
        return f

    def _specs(self, style):
        out, seen = [], set()
        for sp in _PRIMARY.get(style, _PRIMARY["ui"]) + _FALLBACK:
            if sp in seen or not os.path.exists(sp[0]):
                continue
            seen.add(sp)
            out.append(sp)
        return out

    def chain(self, style, size):
        key = (style, size)
        c = self._chain.get(key)
        if c is None:
            c = [(sp, self._load(sp[0], sp[1], size)) for sp in self._specs(style)]
            c = [x for x in c if x[1] is not None]
            if not c:
                c = [((None, None), ImageFont.load_default())]
            self._chain[key] = c
        return c

    def _covers(self, spec, ch):
        key = (spec[0], spec[1], ch)
        v = self._cover.get(key)
        if v is not None:
            return v
        pr = self._probe.get(spec)
        if pr is None:
            f = self._load(spec[0], spec[1], 24)
            try:
                m = f.getmask("\U0010FFFF")
                sig = (m.size, bytes(m))
            except Exception:
                sig = None
            pr = (f, sig)
            self._probe[spec] = pr
        f, sig = pr
        try:
            m = f.getmask(ch)
            got = (m.size, bytes(m))
            v = got != sig and (m.getbbox() is not None)
        except Exception:
            v = False
        self._cover[key] = v
        return v

    def _resolve(self, style, ch, chain):
        key = (style, ch)
        r = self._char.get(key)
        if r is not None:
            return r
        cat = unicodedata.category(ch)
        if ch.isspace():
            r = (0, " ")
        elif cat in ("Cf", "Cc", "Cs", "Co", "Cn") or 0xFE00 <= ord(ch) <= 0xFE0F:
            r = (None, "")  # ZWJ, variation selectors, controls -> invisible
        else:
            r = None
            for i, (sp, _f) in enumerate(chain):
                if sp[0] and self._covers(sp, ch):
                    r = (i, ch)
                    break
            if r is None:
                alt = unicodedata.normalize("NFKC", ch)
                if alt and alt != ch:
                    parts = []
                    for a in alt:
                        ia, ca = self._resolve(style, a, chain)
                        if ia is not None:
                            parts.append(ca)
                    r = (0, "".join(parts)) if parts else (None, "")
                elif cat.startswith("M"):
                    r = (None, "")  # unsupported combining mark -> drop
                else:
                    r = (None, "")
        self._char[key] = r
        return r

    def runs(self, text, style, size):
        chain = self.chain(style, size)
        out = []  # [(font, str)]
        for ch in str(text or ""):
            idx, rep = self._resolve(style, ch, chain)
            if idx is None or not rep:
                continue
            f = chain[idx][1]
            if idx == 0 and rep != ch:
                f = chain[0][1]
            if out and out[-1][0] is f:
                out[-1][1] += rep
            else:
                out.append([f, rep])
        # collapse repeated spaces
        return [(f, s) for f, s in out if s]

    def width(self, runs):
        return sum(f.getlength(s) for f, s in runs)

    def clean(self, text, style="ui", size=20):
        return "".join(s for _f, s in self.runs(text, style, size)).strip()

    def fit_runs(self, text, style, size, max_w):
        runs = self.runs(text, style, size)
        if self.width(runs) <= max_w:
            return runs
        chain = self.chain(style, size)
        ell_f = chain[0][1]
        ell_w = ell_f.getlength("…")
        out, w = [], 0.0
        for f, s in runs:
            buf = ""
            for ch in s:
                cw = f.getlength(ch)
                if w + cw + ell_w > max_w:
                    if buf:
                        out.append((f, buf))
                    out.append((ell_f, "…"))
                    return out
                buf += ch
                w += cw
            if buf:
                out.append((f, buf))
        return out

    def _sprite(self, text, style, size, max_w, spacing):
        """Render text once into an 'L' mask; reused every frame (fast)."""
        key = (text, style, size, max_w, spacing)
        cache = self.__dict__.setdefault("_sprites", {})
        sp = cache.get(key)
        if sp is not None:
            return sp
        runs = (self.fit_runs(text, style, size, max_w) if max_w
                else self.runs(text, style, size))
        if not runs:
            cache[key] = None
            return None
        prim = self.chain(style, size)[0][1]
        try:
            asc, desc = prim.getmetrics()
        except Exception:
            asc, desc = size, size // 4
        nchar = sum(len(s) for _f, s in runs)
        total = self.width(runs) + spacing * max(0, nchar - 1)
        padv = size  # room for tall fallback glyphs
        mw, mh = int(total) + 4 + size // 2, asc + desc + 2 * padv
        mask = Image.new("L", (max(1, mw), max(1, mh)), 0)
        md = ImageDraw.Draw(mask)
        x, base = 2.0, padv + asc
        for f, s in runs:
            if spacing:
                for ch in s:
                    md.text((x, base), ch, font=f, fill=255, anchor="ls")
                    x += f.getlength(ch) + spacing
            else:
                md.text((x, base), s, font=f, fill=255, anchor="ls")
                x += f.getlength(s)
        sp = (mask, total, asc, desc, padv)
        if len(cache) > 3000:
            cache.clear()
        cache[key] = sp
        return sp

    def draw(self, d, xy, text, style="ui", size=20, fill=TEXT, anchor="la",
             max_w=None, spacing=0):
        size = max(8, int(size))
        sp = self._sprite(str(text or ""), style, size,
                          int(max_w) if max_w else None, spacing)
        if sp is None:
            return 0
        mask, total, asc, desc, padv = sp
        x, y = xy
        h, v = anchor[0], anchor[1]
        if h == "m":
            x -= total / 2
        elif h == "r":
            x -= total
        if v == "a":
            base = y + asc
        elif v == "m":
            base = y + (asc - desc) / 2
        elif v == "d":
            base = y - desc
        else:
            base = y
        d.bitmap((int(x - 2), int(base - padv - asc)), mask, fill=fill)
        return total

    def text_width(self, text, style, size, spacing=0):
        sp = self._sprite(str(text or ""), style, max(8, int(size)), None, spacing)
        return sp[1] if sp else 0


T = _Text()


# ================================================================ helpers
def _font(size, bold=False):
    """Back-compat helper (old callers / tests)."""
    return T.chain("bold" if bold else "ui", max(8, int(size)))[0][1]


def _tz():
    try:
        off = float(os.environ.get("SS_TZ_OFFSET", "5.5"))
    except ValueError:
        off = 5.5
    return timezone(timedelta(hours=off))


def _read_state(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _fit(draw, text, font, max_w):
    text = str(text or "")
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


def _first(name):
    parts = [p for p in T.clean(name).split() if p]
    return parts[0] if parts else ""


def _owner(st):
    """Screen ka naam: user ka apna naam (koi brand / bot word nahi)."""
    return (st.get("me_name") or (("@" + st["me_user"]) if st.get("me_user") else "")
            or "My").strip()


def _clock(sec):
    sec = int(max(0, sec or 0))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _initials(name):
    clean = T.clean(name)
    letters = [c for c in clean if c.isalnum()]
    parts = [p for p in clean.split() if any(c.isalnum() for c in p)]
    if len(parts) >= 2:
        s = "".join(next(c for c in p if c.isalnum()) for p in parts[:2])
    else:
        s = "".join(letters[:2])
    return s.upper() or "?"


def _wave(now, speed, phase=0.0):
    return 0.5 + 0.5 * math.sin(now * speed + phase)


def _noise(now, k):
    """Smooth pseudo-random 0..1 (no RNG -> identical frames are cacheable)."""
    return (0.5 + 0.25 * math.sin(now * (3.1 + k * 0.37) + k * 1.7)
            + 0.15 * math.sin(now * (7.3 + k * 0.11) + k * 0.9)
            + 0.10 * math.sin(now * (13.7 - k * 0.05) + k * 2.3))


# ================================================================ renderer
class Renderer:
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.s = h / 1080.0
        self._avatar_cache = {}
        self._photo_ops = []
        self._bg = None
        self._glow_cache = {}
        self._peaks = [0.0] * 40
        # back-compat attributes
        s = self.s
        self.f_title = _font(int(22 * s), True)
        self.f_med = _font(int(24 * s), True)
        self.f_small = _font(int(18 * s))

    # ---------------------------------------------------------- assets
    def _avatar(self, path, size):
        try:
            mtime = os.path.getmtime(path)
        except Exception:
            return None
        key = (path, size)
        ent = self._avatar_cache.get(key)
        if ent and ent[0] == mtime:
            return ent[1]
        try:
            im = Image.open(path).convert("RGB")
            w0, h0 = im.size
            side = min(w0, h0)
            im = im.crop(((w0 - side) // 2, (h0 - side) // 2,
                          (w0 - side) // 2 + side, (h0 - side) // 2 + side))
            im = im.resize((size, size), Image.LANCZOS)
            mask = Image.new("L", (size * 4, size * 4), 0)
            ImageDraw.Draw(mask).ellipse([0, 0, size * 4, size * 4], fill=255)
            mask = mask.resize((size, size), Image.LANCZOS)
            im.putalpha(mask)
        except Exception:
            return None
        self._avatar_cache[key] = (mtime, im)
        return im

    def _background(self):
        if self._bg is not None:
            return self._bg
        W, H, s = self.w, self.h, self.s
        y = np.linspace(0, 1, H)[:, None]
        x = np.linspace(0, 1, W)[None, :]
        base = np.zeros((H, W, 3), dtype=np.float32)
        for i in range(3):
            base[..., i] = BG0[i] + (BG1[i] - BG0[i]) * (y * 0.7 + x * 0.3)
        # neon nebula blobs
        for cx, cy, r, col, a in ((0.15, 0.15, 0.45, PINK, 0.20),
                                  (0.85, 0.25, 0.40, CYAN, 0.14),
                                  (0.60, 0.95, 0.55, VIOLET, 0.18)):
            d2 = ((x - cx) * W / H) ** 2 + (y - cy) ** 2
            g = np.exp(-d2 / (r * r * 0.35)) * a
            for i in range(3):
                base[..., i] += g * col[i]
        # vignette
        vg = 1 - 0.55 * (((x - 0.5) * 1.6) ** 2 + ((y - 0.5) * 1.6) ** 2)
        base *= np.clip(vg, 0.35, 1)[..., None]
        img = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGB")
        d = ImageDraw.Draw(img)
        # subtle perspective grid
        step = max(16, int(48 * s))
        gc = (24, 24, 44)
        for gx in range(0, W, step):
            d.line([gx, 0, gx, H], fill=gc, width=1)
        for gy in range(0, H, step):
            d.line([0, gy, W, gy], fill=gc, width=1)
        # scanlines
        arr = np.asarray(img).astype(np.int16)
        arr[::3] = (arr[::3] * 0.86).astype(np.int16)
        self._bg = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")
        return self._bg

    def _glow(self, w, h, col, rad, strength=1.0):
        """Pre-blurred neon glow sprite (cached by size/colour)."""
        key = (w, h, col, rad, round(strength, 2))
        g = self._glow_cache.get(key)
        if g is not None:
            return g
        pad = max(4, int(18 * self.s))
        im = Image.new("RGBA", (w + 2 * pad, h + 2 * pad), (0, 0, 0, 0))
        dd = ImageDraw.Draw(im)
        dd.rounded_rectangle([pad, pad, pad + w, pad + h], radius=rad,
                             outline=col + (int(230 * strength),), width=max(2, int(6 * self.s)))
        im = im.filter(ImageFilter.GaussianBlur(max(2, int(9 * self.s))))
        if len(self._glow_cache) > 200:
            self._glow_cache.clear()
        self._glow_cache[key] = (im, pad)
        return self._glow_cache[key]

    # ---------------------------------------------------------- primitives
    def _glass(self, d, box, rad=None, fill=GLASS, border=LINE, width=1):
        rad = int(18 * self.s) if rad is None else rad
        d.rounded_rectangle(box, radius=rad, fill=fill, outline=border, width=width)
        # top highlight
        x0, y0, x1, _ = box
        d.line([x0 + rad, y0 + 1, x1 - rad, y0 + 1], fill=_mix(fill, (255, 255, 255), 0.10))

    def _chip(self, d, x, y, text, col, solid=False, size=None, anchor="l"):
        s = self.s
        size = size or int(16 * s)
        tw = T.text_width(text, "tech", size)
        ph, pw = int(size * 1.55), int(tw + size * 1.2)
        if anchor == "r":
            x -= pw
        fill = col if solid else _mix(GLASS, col, 0.16)
        d.rounded_rectangle([x, y, x + pw, y + ph], radius=ph // 2, fill=fill,
                            outline=col, width=max(1, int(1.5 * s)))
        T.draw(d, (x + pw / 2, y + ph / 2), text, "tech", size,
               fill=BG0 if solid else col, anchor="mm")
        return pw, ph

    def _label(self, d, x, y, text, col=MUTED, size=None):
        T.draw(d, (x, y), text, "tech", size or int(17 * self.s), fill=col,
               anchor="la", spacing=max(1, int(2 * self.s)))

    # ---------------------------------------------------------- frame
    def render(self, st, now, started):
        W, H, s = self.w, self.h, self.s
        img = self._background().copy()
        self._photo_ops = []
        self._glow_ops = []
        d = ImageDraw.Draw(img)
        self._ghost_mute = bool(st.get("ghost_mute"))
        top_h = int(78 * s)
        tick_h = int(54 * s)
        pad = int(22 * s)
        side_w = int(W * 0.31)
        self._topbar(d, st, now, started, top_h)
        gx0, gy0 = pad, top_h + pad
        gx1, gy1 = W - side_w - pad, H - tick_h - pad
        self._grid(d, st, gx0, gy0, gx1, gy1, now)
        self._deck(d, st, W - side_w, top_h + pad, W - pad, H - tick_h - pad, now)
        self._ticker(d, st, H - tick_h, now)
        rgba = img.convert("RGBA")
        for (gim, gpad), gx, gy in self._glow_ops:
            rgba.alpha_composite(gim, (max(0, gx - gpad), max(0, gy - gpad)))
        for path, px, py, size, _sp in self._photo_ops:
            ph = self._avatar(path, size)
            if ph is not None:
                rgba.alpha_composite(ph, (px, py))
        return rgba.convert("RGB")

    # ---------------------------------------------------------- top bar
    def _topbar(self, d, st, now, started, h):
        W, s = self.w, self.s
        d.rectangle([0, 0, W, h], fill=(8, 8, 16))
        if getattr(self, "_uline", None) is None or self._uline.width != W:
            lh = max(2, int(3 * s))
            ul = Image.new("RGB", (W, lh))
            ud = ImageDraw.Draw(ul)
            for i in range(W):
                t = i / W
                ud.line([i, 0, i, lh], fill=_mix(PINK, CYAN, t * 2) if t < 0.5
                        else _mix(CYAN, VIOLET, (t - 0.5) * 2))
            self._uline = ul
        d._image.paste(self._uline, (0, h - self._uline.height))
        pad = int(24 * s)
        # LIVE pill
        pulse = _wave(now, 5)
        lw, lh = int(118 * s), int(40 * s)
        ly = (h - lh) // 2
        d.rounded_rectangle([pad, ly, pad + lw, ly + lh], radius=lh // 2,
                            fill=_mix((120, 0, 30), RED, 0.55 + 0.45 * pulse))
        r = int(7 * s)
        cx, cy = pad + int(22 * s), ly + lh // 2
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=TEXT if pulse > 0.3 else (255, 180, 190))
        T.draw(d, (cx + int(16 * s), cy), "LIVE", "display", int(20 * s), TEXT, "lm",
               spacing=max(1, int(2 * s)))
        # room title
        owner = _owner(st)
        custom = str(st.get("title") or "").strip()
        if custom.lower() in ("", "live room", "audio setup — live"):
            custom = ""
        room = custom or (f"{owner} live setup" if owner != "My" else "Live setup")
        tx = pad + lw + int(20 * s)
        T.draw(d, (tx, h // 2 - int(3 * s)), room, "bold", int(28 * s), TEXT, "ld",
               max_w=W * 0.44)
        sub = st.get("group") or "Voice Chat"
        T.draw(d, (tx, h // 2 + int(5 * s)), sub, "ui", int(17 * s), MUTED, "la",
               max_w=W * 0.44)
        # right cluster: signal only (timer removed - live mic look)
        rx = W - pad
        # signal bars
        for k in range(4):
            bh = int((10 + k * 7) * s)
            bx = rx - (3 - k) * int(9 * s)
            d.rounded_rectangle([bx - int(6 * s), h // 2 + int(14 * s) - bh, bx, h // 2 + int(14 * s)],
                                radius=int(2 * s), fill=GREEN)
        rx -= int(48 * s)
        pw, _ = self._chip(d, rx, h // 2 - int(14 * s), "HD · 30FPS", VIOLET, anchor="r",
                           size=int(16 * s))
        rx -= pw + int(10 * s)
        self._chip(d, rx, h // 2 - int(14 * s), "REC", RED, solid=pulse > 0.5,
                   anchor="r", size=int(16 * s))

    # ---------------------------------------------------------- grid
    def _grid(self, d, st, x0, y0, x1, y1, now):
        s = self.s
        ghost = bool(st.get("ghost_mute"))
        parts = list(st.get("participants") or [])
        parts.sort(key=lambda p: (not p.get("speaking"), bool(p.get("muted")),
                                  T.clean(p.get("name", "")).lower()))
        total = len(parts)
        speaking = 0 if ghost else sum(1 for p in parts if p.get("speaking"))
        head_h = int(58 * s)
        self._label(d, x0, y0 - int(4 * s), "◉ STAGE", CYAN, int(16 * s))
        T.draw(d, (x0, y0 + head_h - int(10 * s)),
               f"{total} in voice chat", "bold", int(24 * s), TEXT, "ld")
        cw, _ = self._chip(d, x1, y0 + int(8 * s), f"{speaking} SPEAKING",
                           GREEN if speaking else DIM, solid=bool(speaking), anchor="r")
        unm = 0 if ghost else sum(1 for p in parts if not p.get("muted"))
        self._chip(d, x1 - cw - int(10 * s), y0 + int(8 * s), f"{unm} MIC ON", CYAN, anchor="r")
        y0 += head_h + int(8 * s)
        if not parts:
            self._glass(d, [x0, y0, x1, y1])
            cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
            for k in range(3):
                rr = int((40 + k * 26 + 20 * _wave(now, 2, k)) * s)
                d.ellipse([cx - rr, cy - rr - int(20 * s), cx + rr, cy + rr - int(20 * s)],
                          outline=_mix(GLASS, CYAN, 0.6 - k * 0.18), width=max(1, int(2 * s)))
            T.draw(d, (cx, cy + int(80 * s)), "Participants load ho rahe hain…", "bold",
                   int(24 * s), MUTED, "mm")
            return
        max_tiles = 30
        shown = parts[:max_tiles]
        n = len(shown)
        cols = 1
        while cols * cols < n:
            cols += 1
        cols = max(2, min(6, cols)) if n > 1 else 1
        if n == 2:
            cols = 2
        rows = (n + cols - 1) // cols
        gap = int(14 * s)
        tw = (x1 - x0 - gap * (cols - 1)) // cols
        th = (y1 - y0 - gap * (rows - 1)) // max(1, rows)
        th = min(th, int(tw * 0.82))
        oy = y0 + max(0, ((y1 - y0) - (rows * th + (rows - 1) * gap)) // 2)
        for i, p in enumerate(shown):
            r, c = divmod(i, cols)
            in_row = min(cols, n - r * cols)
            ox = x0 + ((cols - in_row) * (tw + gap)) // 2
            self._tile(d, p, ox + c * (tw + gap), oy + r * (th + gap), tw, th, now, i)
        if total > max_tiles:
            self._chip(d, x1, y1 - int(30 * s), f"+{total - max_tiles} AUR", VIOLET, anchor="r")

    def _tile(self, d, p, x, y, w, h, now, idx):
        s = self.s
        ghost = bool(getattr(self, "_ghost_mute", False))
        speaking = bool(p.get("speaking")) and not ghost
        muted = bool(p.get("muted")) or ghost
        rad = int(18 * s)
        name = str(p.get("name") or "User")
        color = TILE_COLORS[sum(map(ord, name)) % len(TILE_COLORS)]
        fill = _mix(GLASS, color, 0.10 if speaking else 0.04)
        border = GREEN if speaking else _mix(LINE, color, 0.25)
        self._glass(d, [x, y, x + w, y + h], rad, fill, border, max(1, int((3 if speaking else 1) * s)))
        if speaking:
            self._glow_ops.append((self._glow(w, h, GREEN, rad, 0.55 + 0.45 * _wave(now, 6, idx)), x, y))
        # avatar
        ar = int(min(w, h * 0.78) * 0.26)
        cx, cy = x + w // 2, y + int(h * 0.42)
        if speaking:
            for k in range(3):
                rr = ar + int((6 + k * 7 + 6 * _wave(now, 7, k + idx)) * s)
                d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr],
                          outline=_mix(fill, GREEN, 0.75 - k * 0.22), width=max(1, int(2 * s)))
        ring = int(4 * s)
        d.ellipse([cx - ar - ring, cy - ar - ring, cx + ar + ring, cy + ar + ring],
                  fill=GREEN if speaking else _mix(color, GLASS, 0.35))
        d.ellipse([cx - ar, cy - ar, cx + ar, cy + ar], fill=_mix(color, BG0, 0.35))
        photo_path = p.get("photo") or ""
        photo = self._avatar(photo_path, ar * 2) if photo_path else None
        if photo is not None:
            self._photo_ops.append((photo_path, cx - ar, cy - ar, ar * 2, speaking))
        else:
            T.draw(d, (cx, cy), _initials(name), "display", max(10, int(ar * 0.75)), TEXT, "mm")
        # name plate
        fs = max(11, int(min(22 * s, h * 0.10)))
        label = name + ("  · You" if p.get("is_me") else "")
        T.draw(d, (cx, y + h - int(14 * s)), label, "bold", fs, TEXT, "md",
               max_w=w - int(64 * s))
        # mic badge (bottom-right)
        mx, my = x + w - int(24 * s), y + h - int(24 * s)
        mr = int(13 * s)
        d.ellipse([mx - mr, my - mr, mx + mr, my + mr],
                  fill=RED if muted else (GREEN if speaking else DIM))
        q = max(2, int(4 * s))
        if muted:
            d.line([mx - q - 1, my - q - 1, mx + q + 1, my + q + 1], fill=TEXT, width=max(2, int(2 * s)))
            d.line([mx - q - 1, my + q + 1, mx + q + 1, my - q - 1], fill=TEXT, width=max(2, int(2 * s)))
        else:
            d.rounded_rectangle([mx - q, my - int(7 * s), mx + q, my + int(3 * s)], radius=q, fill=BG0)
            d.line([mx, my + int(3 * s), mx, my + int(7 * s)], fill=BG0, width=max(1, int(2 * s)))
        # level bars (bottom-left)
        bx, by = x + int(14 * s), y + h - int(14 * s)
        for k in range(5):
            lvl = (0.25 + 0.75 * max(0.0, min(1.0, _noise(now * 1.6, k + idx * 5)))) if speaking else 0.12
            bh = max(2, int(h * 0.16 * lvl))
            d.rounded_rectangle([bx + k * int(7 * s), by - bh, bx + k * int(7 * s) + int(4 * s), by],
                                radius=int(2 * s), fill=GREEN if speaking else DIM)
        # badges top
        if p.get("hand"):
            self._chip(d, x + int(10 * s), y + int(10 * s), "✋ HAND", YELLOW, size=int(14 * s))
        vol = p.get("volume")
        if vol and vol != 100:
            self._chip(d, x + w - int(10 * s), y + int(10 * s), f"{vol}%", CYAN,
                       size=int(14 * s), anchor="r")

    # ---------------------------------------------------------- right deck
    def _deck(self, d, st, x0, y0, x1, y1, now):
        s = self.s
        self._glass(d, [x0, y0, x1, y1], int(22 * s), (12, 13, 24), _mix(LINE, PINK, 0.25))
        pad = int(18 * s)
        x, y, w = x0 + pad, y0 + pad, x1 - x0 - 2 * pad
        np_name = st.get("now_playing") or ""
        paused = bool(st.get("paused"))
        ghost = bool(st.get("ghost_mute"))
        mic_on = bool(st.get("mic_on")) and not ghost
        audio_active = bool(st.get("audio_active")) or mic_on
        active = (bool(np_name) and not paused) or audio_active or True
        energy = 1.0 if ((np_name and not paused) or audio_active) else 0.55

        # ---- header: owner + status
        owner = _owner(st)
        ar = int(26 * s)
        acx, acy = x + ar, y + ar
        col = TILE_COLORS[sum(map(ord, owner)) % len(TILE_COLORS)]
        d.ellipse([acx - ar - 3, acy - ar - 3, acx + ar + 3, acy + ar + 3], fill=PINK)
        d.ellipse([acx - ar, acy - ar, acx + ar, acy + ar], fill=_mix(col, BG0, 0.3))
        if st.get("me_photo") and os.path.exists(st["me_photo"]):
            self._photo_ops.append((st["me_photo"], acx - ar, acy - ar, ar * 2, False))
        else:
            T.draw(d, (acx, acy), _initials(owner), "display", int(20 * s), TEXT, "mm")
        tx = x + 2 * ar + int(14 * s)
        T.draw(d, (tx, acy - int(2 * s)), owner, "bold", int(22 * s), TEXT, "ld",
               max_w=w - 2 * ar - int(14 * s))
        sub = ("@" + st["me_user"]) if st.get("me_user") else "Host · Studio A"
        T.draw(d, (tx, acy + int(4 * s)), sub, "ui", int(15 * s), MUTED, "la",
               max_w=w - 2 * ar - int(110 * s))
        on_air = mic_on or (bool(np_name) and not paused)
        self._chip(d, x + w, acy + int(2 * s), "ON AIR" if on_air else "STANDBY",
                   RED if on_air else DIM, solid=on_air and _wave(now, 4) > 0.4,
                   anchor="r", size=int(14 * s))
        y += 2 * ar + int(16 * s)
        avail = y1 - pad - y

        # ---- now playing deck
        h_np = int(min(avail * 0.34, 196 * s))
        self._now_playing(d, st, x, y, w, h_np, now, np_name, paused, mic_on, audio_active, ghost)
        y += h_np + int(12 * s)
        # ---- spectrum
        h_sp = int(min(avail * 0.17, 104 * s))
        self._spectrum(d, x, y, w, h_sp, now, energy, st)
        y += h_sp + int(12 * s)
        # ---- stats row
        h_st = int(62 * s)
        self._stats(d, st, x, y, w, h_st, now)
        y += h_st + int(12 * s)
        # ---- mixer (fills the rest)
        if y1 - pad - y > int(110 * s):
            self._mixer(d, st, x, y, w, y1 - pad - y, now, energy, mic_on, np_name, paused)

    def _now_playing(self, d, st, x, y, w, h, now, np_name, paused, mic_on, audio_active, ghost):
        s = self.s
        self._glass(d, [x, y, x + w, y + h], int(16 * s), GLASS2, _mix(LINE, CYAN, 0.3))
        p = int(14 * s)
        # vinyl
        vr = int(min(h - 2 * p, w * 0.36) / 2)
        vcx, vcy = x + p + vr, y + h // 2
        spin = 0 if (paused and np_name) else now * 2.2
        d.ellipse([vcx - vr, vcy - vr, vcx + vr, vcy + vr], fill=(10, 10, 14), outline=(60, 60, 80))
        for k in range(1, 6):
            rr = int(vr * (0.95 - k * 0.1))
            d.ellipse([vcx - rr, vcy - rr, vcx + rr, vcy + rr], outline=(28, 28, 38), width=1)
        # light reflections that rotate
        for off in (0.0, math.pi):
            a0 = math.degrees(spin + off)
            d.arc([vcx - vr + 3, vcy - vr + 3, vcx + vr - 3, vcy + vr - 3], a0, a0 + 28,
                  fill=_mix((10, 10, 14), CYAN, 0.55), width=max(1, int(3 * s)))
        lr = int(vr * 0.42)
        d.ellipse([vcx - lr, vcy - lr, vcx + lr, vcy + lr], fill=PINK)
        if st.get("me_photo") and os.path.exists(st["me_photo"]):
            self._photo_ops.append((st["me_photo"], vcx - lr + 2, vcy - lr + 2, 2 * lr - 4, False))
        d.ellipse([vcx - 3, vcy - 3, vcx + 3, vcy + 3], fill=TEXT)
        # tonearm
        ax, ay = vcx + vr - int(4 * s), vcy - vr + int(2 * s)
        ang = math.radians(118 + (0 if (paused and np_name) else 4 * _wave(now, 1.3)))
        ex, ey = ax + int(vr * 0.95 * math.cos(ang)), ay + int(vr * 0.95 * math.sin(ang))
        d.line([ax, ay, ex, ey], fill=(190, 196, 220), width=max(2, int(4 * s)))
        d.ellipse([ax - int(6 * s), ay - int(6 * s), ax + int(6 * s), ay + int(6 * s)], fill=(190, 196, 220))
        # text block
        tx = vcx + vr + int(18 * s)
        tw = x + w - p - tx
        if np_name:
            state_txt, col = ("PAUSED", YELLOW) if paused else ("PLAYING", GREEN)
        elif ghost:
            state_txt, col = ("GHOST AUDIO", CYAN)
        elif mic_on:
            state_txt, col = ("LIVE MIC", RED)
        else:
            state_txt, col = ("LIVE SET", PINK)
        cw, ch = self._chip(d, tx, y + p, state_txt, col, solid=True, size=int(14 * s))
        if np_name and st.get("loop"):
            self._chip(d, tx + cw + int(6 * s), y + p, "LOOP", VIOLET, size=int(14 * s))
        title = np_name or (f"{_owner(st)} live setup" if (mic_on or ghost) else "Studio Live Session")
        T.draw(d, (tx, y + p + ch + int(8 * s)), title, "bold", int(22 * s), TEXT, "la", max_w=tw)
        self._mic_icon(d, x + w - p - int(18 * s), y + p + ch + int(22 * s), int(12 * s), mic_on, audio_active)
        artist = ("Now spinning · Hi-Fi 320k" if np_name else
                  "Mic muted · audio relay active" if ghost else
                  "Mic › Comp › Limiter › VC" if mic_on else "Neon Deck · Auto DJ")
        T.draw(d, (tx, y + p + ch + int(40 * s)), artist, "ui", int(15 * s), MUTED, "la", max_w=tw)
        # progress
        pos = float(st.get("position") or 0)
        dur = float(st.get("duration") or 0)
        if np_name and not paused and dur > 0:
            pos = min(dur, pos + max(0.0, now - float(st.get("_read_at") or now)))
        if not (np_name and dur > 0):
            dur = 240.0
            pos = (now - float(st.get("started") or now)) % dur
        by = y + h - p - int(22 * s)
        bx0, bx1 = tx, x + w - p
        bh = max(3, int(6 * s))
        d.rounded_rectangle([bx0, by, bx1, by + bh], radius=bh // 2, fill=(40, 44, 66))
        frac = max(0.0, min(1.0, pos / dur))
        fx = bx0 + int((bx1 - bx0) * frac)
        steps = max(1, (fx - bx0) // 3)
        for i in range(steps):
            t = i / max(1, steps)
            d.rectangle([bx0 + i * 3, by, min(fx, bx0 + i * 3 + 3), by + bh], fill=_mix(PINK, CYAN, t))
        kr = int(7 * s)
        d.ellipse([fx - kr, by + bh // 2 - kr, fx + kr, by + bh // 2 + kr], fill=TEXT, outline=CYAN, width=2)
        T.draw(d, (bx0, by + bh + int(6 * s)), _clock(pos), "tech", int(14 * s), MUTED, "la")
        T.draw(d, (bx1, by + bh + int(6 * s)), _clock(dur) if np_name else "∞ LIVE", "tech",
               int(14 * s), MUTED, "ra")

    def _mic_icon(self, d, cx, cy, radius, mic_on, audio_active):
        s = self.s
        col = GREEN if mic_on else (CYAN if audio_active else DIM)
        r = max(4, int(radius))
        d.rounded_rectangle([cx - r // 2, cy - r, cx + r // 2, cy + r // 2], radius=max(2, r // 3), fill=col)
        d.arc([cx - r, cy - r // 2, cx + r, cy + r], 0, 180, fill=col, width=max(1, int(2 * s)))
        d.line([cx, cy + r // 2, cx, cy + r], fill=col, width=max(1, int(2 * s)))
        d.line([cx - r // 2, cy + r, cx + r // 2, cy + r], fill=col, width=max(1, int(2 * s)))
        if audio_active:
            for k in range(2):
                wave_r = r + int((5 + k * 5) * _wave(time.time(), 3.5, k))
                d.arc([cx - wave_r, cy - wave_r, cx + wave_r, cy + wave_r], 205, 335,
                      fill=_mix(col, TEXT, 0.25), width=max(1, int(s)))

    def _spectrum(self, d, x, y, w, h, now, energy, st):
        s = self.s
        self._glass(d, [x, y, x + w, y + h], int(14 * s), GLASS2)
        self._label(d, x + int(12 * s), y + int(8 * s), "SPECTRUM", MUTED, int(13 * s))
        vol = int(st.get("volume") or 0)
        boost = int(st.get("boost") or 0)
        T.draw(d, (x + w - int(12 * s), y + int(8 * s)),
               f"VOL {int(round(vol / 10.0))}%  ·  BOOST {boost}/10", "tech", int(13 * s), CYAN, "ra")
        n = 32
        top = y + int(28 * s)
        bot = y + h - int(10 * s)
        gw = (w - int(24 * s)) / n
        for k in range(n):
            # bass-heavy curve
            shape = 0.95 - 0.55 * (k / n)
            lvl = max(0.06, min(1.0, shape * (0.35 + 0.85 * _noise(now * 2.2, k)) * energy))
            self._peaks[k] = max(lvl, self._peaks[k] - 0.04)
            bx = x + int(12 * s) + int(k * gw)
            bh = int((bot - top) * lvl)
            seg = max(3, int(5 * s))
            yy = bot
            while yy > bot - bh:
                t = (bot - yy) / max(1, bot - top)
                c = _mix(CYAN, PINK, t * 1.2)
                d.rectangle([bx, yy - seg + 1, bx + int(gw) - 2, yy], fill=c)
                yy -= seg + 1
            py = bot - int((bot - top) * self._peaks[k])
            d.rectangle([bx, py - 2, bx + int(gw) - 2, py], fill=TEXT)

    def _stats(self, d, st, x, y, w, h, now):
        s = self.s
        listeners = len(st.get("participants") or [])
        ping = int(14 + 9 * _noise(now * 0.4, 3))
        cpu = int(18 + 14 * _noise(now * 0.3, 7))
        items = [("BITRATE", "320k", CYAN), ("PING", f"{ping}ms", GREEN),
                 ("LISTEN", str(listeners), PINK), ("DSP", f"{cpu}%", YELLOW)]
        gap = int(8 * s)
        cw = (w - gap * 3) / 4
        for i, (k, v, c) in enumerate(items):
            cx0 = x + int(i * (cw + gap))
            self._glass(d, [cx0, y, cx0 + int(cw), y + h], int(12 * s), GLASS2)
            d.rectangle([cx0 + int(10 * s), y + int(8 * s), cx0 + int(10 * s) + int(18 * s),
                         y + int(10 * s)], fill=c)
            T.draw(d, (cx0 + int(10 * s), y + int(14 * s)), k, "tech", int(12 * s), MUTED, "la")
            T.draw(d, (cx0 + int(10 * s), y + h - int(8 * s)), v, "display", int(18 * s), TEXT, "ld",
                   max_w=int(cw) - int(14 * s))

    def _mixer(self, d, st, x, y, w, h, now, energy, mic_on, np_name, paused):
        s = self.s
        self._glass(d, [x, y, x + w, y + h], int(14 * s), GLASS2)
        self._label(d, x + int(12 * s), y + int(8 * s), "MIXER  ·  FX CHAIN", MUTED, int(13 * s))
        T.draw(d, (x + w - int(12 * s), y + int(8 * s)),
               st.get("mic_model") or "SM7B › Scarlett 2i2", "tech", int(13 * s), MUTED, "ra",
               max_w=w * 0.5)
        # fx chain chips
        cy = y + int(30 * s)
        cx = x + int(12 * s)
        for i, fx in enumerate(("HPF", "GATE", "COMP", "EQ", "LIMIT")):
            on = _wave(now, 3, i) > 0.15
            pw, ph = self._chip(d, cx, cy, fx, GREEN if on else DIM, size=int(12 * s))
            cx += pw + int(5 * s)
            if cx > x + w - int(40 * s):
                break
        top = cy + int(32 * s)
        bot = y + h - int(30 * s)
        if bot - top < int(40 * s):
            return
        chans = [("MIC", RED, 1.0 if (mic_on or st.get("ghost_mute")) else 0.35, 0.78),
                 ("MUSIC", CYAN, 1.0 if (np_name and not paused) else 0.4, 0.70),
                 ("FX", VIOLET, 0.6, 0.55),
                 ("MASTER", PINK, energy, 0.86)]
        n = len(chans)
        colw = w / n
        for i, (name, col, act, fader) in enumerate(chans):
            ccx = x + int(colw * i + colw / 2)
            # VU meter (stereo pair)
            for side in (-1, 1):
                mx0 = ccx + side * int(12 * s) - int(4 * s)
                lvl = max(0.04, min(1.0, _noise(now * 2.6, i * 3 + side))) * act
                segs = 14
                sh = (bot - top) / segs
                for k in range(segs):
                    t = k / segs
                    lit = t < lvl
                    c = GREEN if t < 0.6 else (YELLOW if t < 0.85 else RED)
                    yy = bot - int((k + 1) * sh)
                    d.rectangle([mx0, yy + 1, mx0 + int(8 * s), yy + int(sh) - 1],
                                fill=c if lit else (34, 36, 52))
            # fader track + cap
            d.rounded_rectangle([ccx - 2, top, ccx + 2, bot], radius=2, fill=(50, 54, 80))
            fy = bot - int((bot - top) * (fader + 0.03 * math.sin(now + i)))
            fw, fh = int(16 * s), int(9 * s)
            d.rounded_rectangle([ccx - fw, fy - fh, ccx + fw, fy + fh], radius=int(3 * s),
                                fill=(210, 214, 230), outline=col, width=max(1, int(2 * s)))
            d.line([ccx - fw + 3, fy, ccx + fw - 3, fy], fill=col, width=max(1, int(2 * s)))
            T.draw(d, (ccx, bot + int(8 * s)), name, "tech", int(13 * s), col, "ma")

    # ---------------------------------------------------------- ticker
    def _ticker(self, d, st, y, now):
        W, s = self.w, self.s
        h = self.h - y
        d.rectangle([0, y, W, self.h], fill=(8, 8, 16))
        d.line([0, y, W, y], fill=_mix(LINE, PINK, 0.4), width=max(1, int(2 * s)))
        # left tag
        tagw = int(150 * s)
        d.rectangle([0, y + 2, tagw, self.h], fill=PINK)
        T.draw(d, (tagw // 2, y + h // 2), "LIVE FEED", "display", int(16 * s), BG0, "mm",
               spacing=max(1, int(1 * s)))
        # clock on the right
        dt = datetime.fromtimestamp(now, _tz())
        cw = int(220 * s)
        d.rectangle([W - cw, y + 2, W, self.h], fill=(14, 14, 26))
        T.draw(d, (W - int(16 * s), y + h // 2 - int(1 * s)), dt.strftime("%I:%M:%S %p"),
               "display", int(18 * s), TEXT, "rd")
        T.draw(d, (W - int(16 * s), y + h // 2 + int(3 * s)), dt.strftime("%a, %d %b %Y"),
               "tech", int(14 * s), MUTED, "ra")
        # scrolling marquee
        parts = st.get("participants") or []
        spk = [] if st.get("ghost_mute") else [T.clean(p.get("name", "")) for p in parts if p.get("speaking")]
        spk = [x for x in spk if x][:4]
        np_name = st.get("now_playing") or ""
        items = []
        if spk:
            items.append("🎙 Speaking: " + ", ".join(spk))
        if np_name:
            items.append(("⏸ Paused: " if st.get("paused") else "♪ Now playing: ") + T.clean(np_name))
        if st.get("ghost_mute"):
            items.append("MIC OFF · AUDIO RELAY ACTIVE")
        elif st.get("mic_on"):
            items.append("LIVE MIC ON AIR")
        items += [f"👥 {len(parts)} listeners tuned in", "⚡ Studio A · 48kHz/24-bit · Ultra-low latency",
                  "★ Stay tuned — the vibe never stops"]
        text = "     ✦     ".join(items) + "     ✦     "
        fs = int(19 * s)
        runs_w = T.text_width(text, "bold", fs) or 1
        x0, x1 = tagw + int(14 * s), W - cw - int(14 * s)
        off = (now * 90 * s) % runs_w
        sp = T._sprite(text, "bold", fs, None, 0)
        if sp is None:
            return
        mask = sp[0]
        mh = mask.height
        sw = max(1, x1 - x0)
        xx = -off
        cy = y + h // 2
        while xx < sw:
            # crop the visible part of the cached text mask
            l = max(0, int(-xx))
            r = min(mask.width, int(sw - xx))
            if r > l:
                d.bitmap((x0 + int(xx) + l, int(cy - mh / 2)), mask.crop((l, 0, r, mh)), fill=TEXT)
            xx += runs_w


def rgb_to_i420(img):
    """RGB -> I420 using PIL's C converter (≈10x faster than float numpy):
    a slow first frame on weak servers made ntgcalls/Telegram drop the
    presentation ("screen share accept nahi kiya")."""
    ycc = np.asarray(img.convert("YCbCr"), dtype=np.uint8)
    y = ycc[..., 0]
    # full-range JPEG YCbCr -> limited (BT.601) range expected by WebRTC
    y = (y.astype(np.uint16) * 219 // 255 + 16).astype(np.uint8)
    u = ycc[0::2, 0::2, 1].astype(np.uint16) * 224 // 255 + 16
    v = ycc[0::2, 0::2, 2].astype(np.uint16) * 224 // 255 + 16
    return y.tobytes() + u.astype(np.uint8).tobytes() + v.astype(np.uint8).tobytes()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True)
    ap.add_argument("--w", type=int, default=1920)
    ap.add_argument("--h", type=int, default=1080)
    ap.add_argument("--fps", type=int, default=15)
    ap.add_argument("--frames", type=int, default=0, help="test: stop after N frames")
    ap.add_argument("--png", default="", help="test: save one rendered frame as PNG and exit")
    a = ap.parse_args()
    w = max(320, min(1920, a.w)) // 2 * 2
    h = max(240, min(1080, a.h)) // 2 * 2
    fps = max(5, min(30, a.fps))
    r = Renderer(w, h)
    if a.png:
        st = _read_state(a.state)
        now = time.time()
        st["_read_at"] = now
        r.render(st, now, float(st.get("started") or now - 754)).save(a.png)
        return
    out = sys.stdout.buffer
    started = time.time()
    frame, key, last_read, st = None, None, 0.0, {}
    # re-render ~6x per second for meters / vinyl / ticker; resend cached between
    anim_step = 0.16
    t0 = time.time()
    n = 0
    while True:
        now = time.time()
        if now - last_read > 0.75:
            new = _read_state(a.state)
            if new:
                new["_read_at"] = now
                st = new
            last_read = now
            if st.get("started"):
                started = float(st["started"])
        k = (json.dumps({kk: vv for kk, vv in st.items() if kk != "_read_at"},
                        sort_keys=True, default=str), int(now / anim_step))
        if k != key:
            try:
                frame = rgb_to_i420(r.render(st, now, started))
            except Exception as exc:  # never kill the screen share on a bad frame
                print(f"live_screen render error: {exc!r}", file=sys.stderr)
                if frame is None:
                    frame = rgb_to_i420(Image.new("RGB", (w, h), BG0))
            key = k
        try:
            out.write(frame)
            out.flush()
        except (BrokenPipeError, OSError):
            return
        n += 1
        if a.frames and n >= a.frames:
            return
        sleep = t0 + n / fps - time.time()
        if sleep > 0:
            time.sleep(sleep)


if __name__ == "__main__":
    main()
