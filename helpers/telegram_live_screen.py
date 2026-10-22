#!/usr/bin/env python3
"""Premium live screen-share renderer for Telegram VC.

Renders a natural live voice-chat window frame-by-frame and
writes raw I420 (yuv420p) frames to stdout for ntgcalls SHELL video source.

Design goals:
  * Looks like a real, live call — animated activity bars, clear status, glowing
    speaking indicators, real clock, scrolling ticker, vinyl deck.
  * Premium dark aesthetic with neon accents (cyan / pink / green).
  * Per-character font fallback so fancy Unicode names never show boxes.
  * Frames are cached and only re-rendered when state or animation tick changes.
"""
import argparse
import json
import math
import os
import re
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
]

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


class _Text:
    def __init__(self):
        self._fonts = {}
        self._probe = {}
        self._cover = {}
        self._chain = {}
        self._char = {}

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
            r = (None, "")
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
                    r = (None, "")
                else:
                    r = (None, "")
        self._char[key] = r
        return r

    def runs(self, text, style, size):
        chain = self.chain(style, size)
        out = []
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
        padv = size
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


def _font(size, bold=False):
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


def _first(name):
    parts = [p for p in T.clean(name).split() if p]
    return parts[0] if parts else ""


def _owner(st):
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
    return (0.5 + 0.25 * math.sin(now * (3.1 + k * 0.37) + k * 1.7)
            + 0.15 * math.sin(now * (7.3 + k * 0.11) + k * 0.9)
            + 0.10 * math.sin(now * (13.7 - k * 0.05) + k * 2.3))


class Renderer:
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.s = h / 720.0
        self._avatar_cache = {}
        self._photo_ops = []
        self._glow_cache = {}
        self._glow_ops = []
        self._bg = None
        self._peaks = [0.0] * 40

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
        for cx, cy, r, col, a in ((0.12, 0.12, 0.40, PINK, 0.16),
                                  (0.88, 0.20, 0.35, CYAN, 0.11),
                                  (0.55, 0.92, 0.48, VIOLET, 0.14)):
            d2 = ((x - cx) * W / H) ** 2 + (y - cy) ** 2
            g = np.exp(-d2 / (r * r * 0.35)) * a
            for i in range(3):
                base[..., i] += g * col[i]
        vg = 1 - 0.50 * (((x - 0.5) * 1.6) ** 2 + ((y - 0.5) * 1.6) ** 2)
        base *= np.clip(vg, 0.35, 1)[..., None]
        img = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGB")
        d = ImageDraw.Draw(img)
        step = max(16, int(44 * s))
        gc = (22, 22, 42)
        for gx in range(0, W, step):
            d.line([gx, 0, gx, H], fill=gc, width=1)
        for gy in range(0, H, step):
            d.line([0, gy, W, gy], fill=gc, width=1)
        arr = np.asarray(img).astype(np.int16)
        arr[::3] = (arr[::3] * 0.88).astype(np.int16)
        self._bg = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")
        return self._bg

    def _glow(self, w, h, col, rad, strength=1.0):
        key = (w, h, col, rad, round(strength, 2))
        g = self._glow_cache.get(key)
        if g is not None:
            return g
        pad = max(4, int(16 * self.s))
        im = Image.new("RGBA", (w + 2 * pad, h + 2 * pad), (0, 0, 0, 0))
        dd = ImageDraw.Draw(im)
        dd.rounded_rectangle([pad, pad, pad + w, pad + h], radius=rad,
                             outline=col + (int(230 * strength),), width=max(2, int(5 * self.s)))
        im = im.filter(ImageFilter.GaussianBlur(max(2, int(8 * self.s))))
        if len(self._glow_cache) > 200:
            self._glow_cache.clear()
        self._glow_cache[key] = (im, pad)
        return self._glow_cache[key]

    def _glass(self, d, box, rad=None, fill=GLASS, border=LINE, width=1):
        s = self.s
        rad = int(16 * s) if rad is None else rad
        d.rounded_rectangle(box, radius=rad, fill=fill, outline=border, width=width)
        x0, y0, x1, _ = box
        d.line([x0 + rad, y0 + 1, x1 - rad, y0 + 1], fill=_mix(fill, (255, 255, 255), 0.08))

    def _chip(self, d, x, y, text, col, solid=False, size=None, anchor="l"):
        s = self.s
        size = size or int(15 * s)
        tw = T.text_width(text, "tech", size)
        ph, pw = int(size * 1.5), int(tw + size * 1.1)
        if anchor == "r":
            x -= pw
        elif anchor == "m":
            x -= pw / 2
        fill = col if solid else _mix(GLASS, col, 0.16)
        d.rounded_rectangle([x, y, x + pw, y + ph], radius=ph // 2, fill=fill,
                            outline=col, width=max(1, int(1.5 * s)))
        T.draw(d, (x + pw / 2, y + ph / 2), text, "tech", size,
               fill=BG0 if solid else col, anchor="mm")
        return pw, ph

    def _label(self, d, x, y, text, col=MUTED, size=None):
        T.draw(d, (x, y), text, "tech", size or int(16 * self.s), fill=col,
               anchor="la", spacing=max(1, int(2 * self.s)))

    def render(self, st, now, started):
        W, H, s = self.w, self.h, self.s
        img = self._background().copy()
        self._photo_ops = []
        self._glow_ops = []
        d = ImageDraw.Draw(img)
        top_h = int(64 * s)
        tick_h = int(48 * s)
        pad = int(20 * s)
        nav_w = int(178 * s)
        side_w = int(W * 0.34)
        self._topbar(d, st, now, started, top_h)
        self._sidebar(d, st, nav_w, top_h, H - tick_h)
        gx0, gy0 = nav_w + pad, top_h + pad
        gx1, gy1 = W - side_w - pad, H - tick_h - pad
        self._grid(d, st, gx0, gy0, gx1, gy1, now)
        self._session_panel(d, st, W - side_w, top_h + pad, W - pad, H - tick_h - pad, now)
        self._ticker(d, st, H - tick_h, now)
        rgba = img.convert("RGBA")
        for (gim, gpad), gx, gy in self._glow_ops:
            rgba.alpha_composite(gim, (max(0, int(gx - gpad)), max(0, int(gy - gpad))))
        for path, px, py, size, _sp in self._photo_ops:
            ph = self._avatar(path, size)
            if ph is not None:
                rgba.alpha_composite(ph, (px, py))
        return rgba.convert("RGB")

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
        pad = int(22 * s)
        pulse = _wave(now, 5)
        lw, lh = int(100 * s), int(34 * s)
        ly = (h - lh) // 2
        d.rounded_rectangle([pad, ly, pad + lw, ly + lh], radius=lh // 2,
                            fill=_mix((120, 0, 30), RED, 0.55 + 0.45 * pulse))
        r = int(6 * s)
        cx, cy = pad + int(19 * s), ly + lh // 2
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=TEXT if pulse > 0.3 else (255, 180, 190))
        T.draw(d, (cx + int(14 * s), cy), "LIVE", "display", int(18 * s), TEXT, "lm",
               spacing=max(1, int(2 * s)))
        owner = _owner(st)
        room = "Live voice chat"
        tx = pad + lw + int(18 * s)
        T.draw(d, (tx, h // 2 - int(3 * s)), room, "bold", int(24 * s), TEXT, "ld",
               max_w=W * 0.44)
        sub = "Connected now"
        T.draw(d, (tx, h // 2 + int(5 * s)), sub, "ui", int(15 * s), MUTED, "la",
               max_w=W * 0.44)
        rx = W - pad
        for k in range(4):
            bh = int((8 + k * 6) * s)
            bx = rx - (3 - k) * int(8 * s)
            d.rounded_rectangle([bx - int(5 * s), h // 2 + int(12 * s) - bh, bx, h // 2 + int(12 * s)],
                                radius=int(2 * s), fill=GREEN)
        rx -= int(42 * s)
        pw, _ = self._chip(d, rx, h // 2 - int(12 * s), "HD · 30FPS", VIOLET, anchor="r",
                           size=int(14 * s))
        rx -= pw + int(8 * s)
        self._chip(d, rx, h // 2 - int(12 * s), "REC", RED, solid=pulse > 0.5,
                   anchor="r", size=int(14 * s))

    def _sidebar(self, d, st, w, y0, y1):
        s = self.s
        d.rectangle([0, y0, w, y1], fill=(11, 13, 22))
        d.line([w - 1, y0, w - 1, y1], fill=LINE, width=max(1, int(s)))
        pad = int(14 * s)
        owner = _owner(st)
        photo = st.get("me_photo") or ""
        avatar = int(24 * s)
        ax, ay = pad + avatar, y0 + int(28 * s)
        d.ellipse([ax - avatar - 2, ay - avatar - 2, ax + avatar + 2, ay + avatar + 2], fill=CYAN)
        d.ellipse([ax - avatar, ay - avatar, ax + avatar, ay + avatar], fill=(28, 54, 72))
        if photo and os.path.exists(photo):
            self._photo_ops.append((photo, ax - avatar, ay - avatar, avatar * 2, False))
        else:
            T.draw(d, (ax, ay), _initials(owner), "display", int(15 * s), TEXT, "mm")
        T.draw(d, (ax + avatar + int(10 * s), ay - int(7 * s)), owner, "bold", int(15 * s), TEXT, "lm",
               max_w=w - ax - avatar - int(18 * s))
        T.draw(d, (ax + avatar + int(10 * s), ay + int(10 * s)), "online", "tech", int(12 * s), GREEN, "lm")
        y = y0 + int(78 * s)
        for label, active, col in (("Chats", False, MUTED), ("Calls", True, CYAN), ("Contacts", False, MUTED)):
            if active:
                d.rounded_rectangle([int(8 * s), y - int(17 * s), w - int(8 * s), y + int(17 * s)],
                                    radius=int(8 * s), fill=(22, 38, 54))
                d.rectangle([int(8 * s), y - int(17 * s), int(11 * s), y + int(17 * s)], fill=CYAN)
            T.draw(d, (pad + int(8 * s), y), label, "ui", int(15 * s), col, "lm")
            y += int(43 * s)
        d.line([pad, y - int(12 * s), w - pad, y - int(12 * s)], fill=LINE, width=max(1, int(s)))
        T.draw(d, (pad, y + int(14 * s)), "RECENT CALLS", "tech", int(11 * s), DIM, "la")
        y += int(42 * s)
        parts = list(st.get("participants") or [])
        for i, participant in enumerate(parts[:5]):
            name = str(participant.get("name") or "User")
            active = bool(participant.get("speaking"))
            dot = GREEN if active else DIM
            d.ellipse([pad, y - int(4 * s), pad + int(8 * s), y + int(4 * s)], fill=dot)
            T.draw(d, (pad + int(16 * s), y), name, "ui", int(12 * s), TEXT if active else MUTED, "lm",
                   max_w=w - pad - int(22 * s))
            y += int(30 * s)
        T.draw(d, (pad, y1 - int(20 * s)), "Voice chat", "tech", int(12 * s), DIM, "ld")

    def _grid(self, d, st, x0, y0, x1, y1, now):
        s = self.s
        parts = list(st.get("participants") or [])
        parts.sort(key=lambda p: (not p.get("speaking"), bool(p.get("muted")),
                                  T.clean(p.get("name", "")).lower()))
        total = len(parts)
        speaking = sum(1 for p in parts if p.get("speaking"))
        head_h = int(50 * s)
        self._label(d, x0, y0 - int(2 * s), "◉ VOICE CHAT", CYAN, int(15 * s))
        T.draw(d, (x0, y0 + head_h - int(8 * s)),
               f"{total} in voice chat", "bold", int(20 * s), TEXT, "ld")
        cw, _ = self._chip(d, x1, y0 + int(6 * s), f"{speaking} SPEAKING",
                           GREEN if speaking else DIM, solid=bool(speaking), anchor="r")
        unm = sum(1 for p in parts if not p.get("muted"))
        self._chip(d, x1 - cw - int(8 * s), y0 + int(6 * s), f"{unm} MIC ON", CYAN, anchor="r")
        y0 += head_h + int(6 * s)
        if not parts:
            self._glass(d, [x0, y0, x1, y1])
            cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
            for k in range(3):
                rr = int((36 + k * 22 + 18 * _wave(now, 2, k)) * s)
                d.ellipse([cx - rr, cy - rr - int(16 * s), cx + rr, cy + rr - int(16 * s)],
                          outline=_mix(GLASS, CYAN, 0.6 - k * 0.18), width=max(1, int(2 * s)))
            T.draw(d, (cx, cy + int(70 * s)), "Participants load ho rahe hain…", "bold",
                   int(20 * s), MUTED, "mm")
            return
        max_tiles = 30
        shown = parts[:max_tiles]
        n = len(shown)
        cols = 1
        while cols * cols < n:
            cols += 1
        cols = max(2, min(5, cols)) if n > 1 else 1
        if n == 2:
            cols = 2
        rows = (n + cols - 1) // cols
        gap = int(12 * s)
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
            self._chip(d, x1, y1 - int(26 * s), f"+{total - max_tiles} AUR", VIOLET, anchor="r")

    def _tile(self, d, p, x, y, w, h, now, idx):
        s = self.s
        speaking = bool(p.get("speaking"))
        muted = bool(p.get("muted"))
        rad = int(16 * s)
        name = str(p.get("name") or "User")
        color = TILE_COLORS[sum(map(ord, name)) % len(TILE_COLORS)]
        fill = _mix(GLASS, color, 0.10 if speaking else 0.04)
        border = GREEN if speaking else _mix(LINE, color, 0.25)
        self._glass(d, [x, y, x + w, y + h], rad, fill, border,
                    max(1, int((3 if speaking else 1) * s)))
        if speaking:
            self._glow_ops.append((self._glow(w, h, GREEN, rad, 0.55 + 0.45 * _wave(now, 6, idx)), x, y))
        ar = int(min(w, h * 0.78) * 0.26)
        cx, cy = x + w // 2, y + int(h * 0.42)
        if speaking:
            for k in range(3):
                rr = ar + int((5 + k * 6 + 5 * _wave(now, 7, k + idx)) * s)
                d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr],
                          outline=_mix(fill, GREEN, 0.75 - k * 0.22), width=max(1, int(2 * s)))
        ring = int(3 * s)
        d.ellipse([cx - ar - ring, cy - ar - ring, cx + ar + ring, cy + ar + ring],
                  fill=GREEN if speaking else _mix(color, GLASS, 0.35))
        d.ellipse([cx - ar, cy - ar, cx + ar, cy + ar], fill=_mix(color, BG0, 0.35))
        photo_path = p.get("photo") or ""
        photo = self._avatar(photo_path, ar * 2) if photo_path else None
        if photo is not None:
            self._photo_ops.append((photo_path, cx - ar, cy - ar, ar * 2, speaking))
        else:
            T.draw(d, (cx, cy), _initials(name), "display", max(10, int(ar * 0.75)), TEXT, "mm")
        fs = max(10, int(min(19 * s, h * 0.10)))
        label = name + ("  · You" if p.get("is_me") else "")
        T.draw(d, (cx, y + h - int(12 * s)), label, "bold", fs, TEXT, "md",
               max_w=w - int(56 * s))
        mx, my = x + w - int(21 * s), y + h - int(21 * s)
        mr = int(11 * s)
        d.ellipse([mx - mr, my - mr, mx + mr, my + mr],
                  fill=RED if muted else (GREEN if speaking else DIM))
        q = max(2, int(3 * s))
        if muted:
            d.line([mx - q - 1, my - q - 1, mx + q + 1, my + q + 1], fill=TEXT, width=max(2, int(2 * s)))
            d.line([mx - q - 1, my + q + 1, mx + q + 1, my - q - 1], fill=TEXT, width=max(2, int(2 * s)))
        else:
            d.rounded_rectangle([mx - q, my - int(6 * s), mx + q, my + int(2 * s)], radius=q, fill=BG0)
            d.line([mx, my + int(2 * s), mx, my + int(6 * s)], fill=BG0, width=max(1, int(2 * s)))
        bx, by = x + int(12 * s), y + h - int(12 * s)
        for k in range(5):
            lvl = (0.25 + 0.75 * max(0.0, min(1.0, _noise(now * 1.6, k + idx * 5)))) if speaking else 0.12
            bh = max(2, int(h * 0.16 * lvl))
            d.rounded_rectangle([bx + k * int(6 * s), by - bh, bx + k * int(6 * s) + int(4 * s), by],
                                radius=int(2 * s), fill=GREEN if speaking else DIM)
        if p.get("hand"):
            self._chip(d, x + int(8 * s), y + int(8 * s), "✋ HAND", YELLOW, size=int(13 * s))
        vol = p.get("volume")
        if vol and vol != 100:
            self._chip(d, x + w - int(8 * s), y + int(8 * s), f"{vol}%", CYAN,
                       size=int(13 * s), anchor="r")

    def _session_panel(self, d, st, x0, y0, x1, y1, now):
        s = self.s
        self._glass(d, [x0, y0, x1, y1], int(20 * s), (12, 14, 24), _mix(LINE, CYAN, 0.28))
        pad = int(16 * s)
        x, y, w = x0 + pad, y0 + pad, x1 - x0 - 2 * pad
        parts = list(st.get("participants") or [])
        speakers = [p for p in parts if p.get("speaking") and not p.get("muted")]
        active = speakers[0] if speakers else next((p for p in parts if p.get("is_me")), None)
        active_name = str((active or {}).get("name") or _owner(st) or "Someone")
        active_photo = ((active or {}).get("photo") or
                        (st.get("me_photo") if (active or {}).get("is_me") else "") or "")
        live = bool(speakers or st.get("mic_on"))

        T.draw(d, (x, y), "LIVE SESSION", "tech", int(14 * s), CYAN, "la", spacing=max(1, int(s)))
        T.draw(d, (x + w, y), "Connected", "tech", int(13 * s), GREEN, "ra")
        y += int(28 * s)
        d.line([x, y, x + w, y], fill=_mix(LINE, CYAN, 0.35), width=max(1, int(s)))
        y += int(14 * s)

        card_h = int(128 * s)
        self._glass(d, [x, y, x + w, y + card_h], int(14 * s), GLASS2,
                    GREEN if live else LINE, max(1, int((2 if live else 1) * s)))
        avatar_r = int(32 * s)
        ax, ay = x + int(48 * s), y + int(54 * s)
        ring = GREEN if live else DIM
        d.ellipse([ax - avatar_r - int(4 * s), ay - avatar_r - int(4 * s),
                   ax + avatar_r + int(4 * s), ay + avatar_r + int(4 * s)], fill=ring)
        d.ellipse([ax - avatar_r, ay - avatar_r, ax + avatar_r, ay + avatar_r],
                  fill=_mix(CYAN if live else DIM, BG0, 0.35))
        if active_photo and os.path.exists(active_photo):
            self._photo_ops.append((active_photo, ax - avatar_r, ay - avatar_r, avatar_r * 2, live))
        else:
            T.draw(d, (ax, ay), _initials(active_name), "display", int(22 * s), TEXT, "mm")
        tx = x + int(94 * s)
        T.draw(d, (tx, y + int(36 * s)), active_name, "bold", int(19 * s), TEXT, "la",
               max_w=w - int(110 * s))
        T.draw(d, (tx, y + int(61 * s)), "Speaking now" if live else "In the call",
               "ui", int(14 * s), GREEN if live else MUTED, "la")
        bars_x = tx
        bars_y = y + int(88 * s)
        for k in range(20):
            lvl = (0.18 + 0.82 * _noise(now * 2.5, k)) if live else 0.10
            bar_h = max(2, int(17 * s * lvl))
            col = _mix(CYAN, GREEN, k / 19) if live else DIM
            d.rounded_rectangle([bars_x + k * int(6 * s), bars_y - bar_h,
                                 bars_x + k * int(6 * s) + int(3 * s), bars_y],
                                radius=max(1, int(s)), fill=col)
        y += card_h + int(12 * s)

        np_name = T.clean(st.get("now_playing") or "")
        dur = float(st.get("duration") or 0)
        pos = float(st.get("position") or 0)
        if np_name and dur > 0:
            self._glass(d, [x, y, x + w, y + int(92 * s)], int(12 * s), GLASS)
            T.draw(d, (x + int(12 * s), y + int(18 * s)), "Currently playing", "tech",
                   int(12 * s), MUTED, "la")
            T.draw(d, (x + int(12 * s), y + int(42 * s)), np_name, "bold", int(16 * s), TEXT, "la",
                   max_w=w - int(24 * s))
            track_y = y + int(65 * s)
            track_x0, track_x1 = x + int(12 * s), x + w - int(12 * s)
            d.rounded_rectangle([track_x0, track_y, track_x1, track_y + int(5 * s)],
                                radius=int(3 * s), fill=(48, 52, 76))
            frac = max(0.0, min(1.0, pos / dur))
            fx = track_x0 + int((track_x1 - track_x0) * frac)
            d.rounded_rectangle([track_x0, track_y, fx, track_y + int(5 * s)],
                                radius=int(3 * s), fill=CYAN)
            T.draw(d, (track_x0, y + int(84 * s)), _clock(pos), "tech", int(12 * s), MUTED, "la")
            T.draw(d, (track_x1, y + int(84 * s)), _clock(dur), "tech", int(12 * s), MUTED, "ra")
            y += int(104 * s)

        remaining = y1 - pad - y
        if remaining > int(70 * s):
            self._glass(d, [x, y, x + w, y + remaining], int(12 * s), GLASS)
            T.draw(d, (x + int(12 * s), y + int(18 * s)), "PEOPLE IN THIS CALL", "tech",
                   int(12 * s), MUTED, "la")
            mini_y = y + int(42 * s)
            for i, participant in enumerate(parts[:4]):
                px = x + int(14 * s) + (i % 2) * int(w * 0.50)
                py = mini_y + (i // 2) * int(34 * s)
                name = str(participant.get("name") or "User")
                dot = GREEN if participant.get("speaking") else (RED if participant.get("muted") else CYAN)
                d.ellipse([px, py, px + int(8 * s), py + int(8 * s)], fill=dot)
                T.draw(d, (px + int(14 * s), py + int(4 * s)), name, "ui", int(13 * s), TEXT,
                       "lm", max_w=w * 0.42)

    def _deck(self, d, st, x0, y0, x1, y1, now):
        s = self.s
        self._glass(d, [x0, y0, x1, y1], int(20 * s), (12, 13, 24), _mix(LINE, PINK, 0.25))
        pad = int(16 * s)
        x, y, w = x0 + pad, y0 + pad, x1 - x0 - 2 * pad
        np_name = st.get("now_playing") or ""
        paused = bool(st.get("paused"))
        mic_on = bool(st.get("mic_on"))
        active = (bool(np_name) and not paused) or mic_on or True
        energy = 1.0 if ((np_name and not paused) or mic_on) else 0.55

        owner = _owner(st)
        ar = int(24 * s)
        acx, acy = x + ar, y + ar
        col = TILE_COLORS[sum(map(ord, owner)) % len(TILE_COLORS)]
        d.ellipse([acx - ar - 3, acy - ar - 3, acx + ar + 3, acy + ar + 3], fill=PINK)
        d.ellipse([acx - ar, acy - ar, acx + ar, acy + ar], fill=_mix(col, BG0, 0.3))
        if st.get("me_photo") and os.path.exists(st["me_photo"]):
            self._photo_ops.append((st["me_photo"], acx - ar, acy - ar, ar * 2, False))
        else:
            T.draw(d, (acx, acy), _initials(owner), "display", int(18 * s), TEXT, "mm")
        tx = x + 2 * ar + int(12 * s)
        T.draw(d, (tx, acy - int(2 * s)), owner, "bold", int(20 * s), TEXT, "ld",
               max_w=w - 2 * ar - int(12 * s))
        sub = ("@" + st["me_user"]) if st.get("me_user") else "Host"
        T.draw(d, (tx, acy + int(4 * s)), sub, "ui", int(14 * s), MUTED, "la",
               max_w=w - 2 * ar - int(100 * s))
        on_air = mic_on or (bool(np_name) and not paused)
        self._chip(d, x + w, acy + int(2 * s), "ACTIVE" if on_air else "IDLE",
                   RED if on_air else DIM, solid=on_air and _wave(now, 4) > 0.4,
                   anchor="r", size=int(13 * s))
        y += 2 * ar + int(14 * s)
        avail = y1 - pad - y

        h_np = int(min(avail * 0.34, 172 * s))
        self._now_playing(d, st, x, y, w, h_np, now, np_name, paused, mic_on)
        y += h_np + int(10 * s)
        h_sp = int(min(avail * 0.17, 92 * s))
        self._spectrum(d, x, y, w, h_sp, now, energy, st)
        y += h_sp + int(10 * s)
        h_st = int(54 * s)
        self._stats(d, st, x, y, w, h_st, now)
        y += h_st + int(10 * s)
        if y1 - pad - y > int(95 * s):
            self._mixer(d, st, x, y, w, y1 - pad - y, now, energy, mic_on, np_name, paused)

    def _now_playing(self, d, st, x, y, w, h, now, np_name, paused, mic_on):
        s = self.s
        self._glass(d, [x, y, x + w, y + h], int(14 * s), GLASS2, _mix(LINE, CYAN, 0.3))
        p = int(12 * s)
        vr = int(min(h - 2 * p, w * 0.36) / 2)
        vcx, vcy = x + p + vr, y + h // 2
        spin = 0 if (paused and np_name) else now * 2.2
        d.ellipse([vcx - vr, vcy - vr, vcx + vr, vcy + vr], fill=(10, 10, 14), outline=(60, 60, 80))
        for k in range(1, 6):
            rr = int(vr * (0.95 - k * 0.1))
            d.ellipse([vcx - rr, vcy - rr, vcx + rr, vcy + rr], outline=(28, 28, 38), width=1)
        for off in (0.0, math.pi):
            a0 = math.degrees(spin + off)
            d.arc([vcx - vr + 3, vcy - vr + 3, vcx + vr - 3, vcy + vr - 3], a0, a0 + 28,
                  fill=_mix((10, 10, 14), CYAN, 0.55), width=max(1, int(3 * s)))
        lr = int(vr * 0.42)
        d.ellipse([vcx - lr, vcy - lr, vcx + lr, vcy + lr], fill=PINK)
        if st.get("me_photo") and os.path.exists(st["me_photo"]):
            self._photo_ops.append((st["me_photo"], vcx - lr + 2, vcy - lr + 2, 2 * lr - 4, False))
        d.ellipse([vcx - 3, vcy - 3, vcx + 3, vcy + 3], fill=TEXT)
        ax, ay = vcx + vr - int(4 * s), vcy - vr + int(2 * s)
        ang = math.radians(118 + (0 if (paused and np_name) else 4 * _wave(now, 1.3)))
        ex, ey = ax + int(vr * 0.95 * math.cos(ang)), ay + int(vr * 0.95 * math.sin(ang))
        d.line([ax, ay, ex, ey], fill=(190, 196, 220), width=max(2, int(4 * s)))
        d.ellipse([ax - int(5 * s), ay - int(5 * s), ax + int(5 * s), ay + int(5 * s)], fill=(190, 196, 220))
        tx = vcx + vr + int(16 * s)
        tw = x + w - p - tx
        if np_name:
            state_txt, col = ("PAUSED", YELLOW) if paused else ("PLAYING", GREEN)
        elif mic_on:
            state_txt, col = ("LIVE MIC", RED)
        else:
            state_txt, col = ("LIVE SET", PINK)
        cw, ch = self._chip(d, tx, y + p, state_txt, col, solid=True, size=int(13 * s))
        if np_name and st.get("loop"):
            self._chip(d, tx + cw + int(5 * s), y + p, "LOOP", VIOLET, size=int(13 * s))
        title = np_name or ("Microphone" if mic_on else "Voice chat")
        T.draw(d, (tx, y + p + ch + int(6 * s)), title, "bold", int(20 * s), TEXT, "la", max_w=tw)
        artist = ("Now spinning · Hi-Fi 320k" if np_name else
                  "Mic › Comp › Limiter › VC" if mic_on else "Neon Deck · Auto DJ")
        T.draw(d, (tx, y + p + ch + int(36 * s)), artist, "ui", int(14 * s), MUTED, "la", max_w=tw)
        pos = float(st.get("position") or 0)
        dur = float(st.get("duration") or 0)
        if np_name and not paused and dur > 0:
            pos = min(dur, pos + max(0.0, now - float(st.get("_read_at") or now)))
        if not (np_name and dur > 0):
            dur = 240.0
            pos = (now - float(st.get("started") or now)) % dur
        by = y + h - p - int(20 * s)
        bx0, bx1 = tx, x + w - p
        bh = max(3, int(5 * s))
        d.rounded_rectangle([bx0, by, bx1, by + bh], radius=bh // 2, fill=(40, 44, 66))
        frac = max(0.0, min(1.0, pos / dur))
        fx = bx0 + int((bx1 - bx0) * frac)
        steps = max(1, (fx - bx0) // 3)
        for i in range(steps):
            t = i / max(1, steps)
            d.rectangle([bx0 + i * 3, by, min(fx, bx0 + i * 3 + 3), by + bh], fill=_mix(PINK, CYAN, t))
        kr = int(6 * s)
        d.ellipse([fx - kr, by + bh // 2 - kr, fx + kr, by + bh // 2 + kr], fill=TEXT, outline=CYAN, width=2)
        T.draw(d, (bx0, by + bh + int(5 * s)), _clock(pos), "tech", int(13 * s), MUTED, "la")
        T.draw(d, (bx1, by + bh + int(5 * s)), _clock(dur) if np_name else "∞ LIVE", "tech",
               int(13 * s), MUTED, "ra")

    def _spectrum(self, d, x, y, w, h, now, energy, st):
        s = self.s
        self._glass(d, [x, y, x + w, y + h], int(12 * s), GLASS2)
        self._label(d, x + int(10 * s), y + int(7 * s), "VOICE ACTIVITY", MUTED, int(12 * s))
        vol = int(st.get("volume") or 0)
        boost = int(st.get("boost") or 0)
        T.draw(d, (x + w - int(10 * s), y + int(7 * s)),
               f"VOLUME {int(round(vol / 10.0))}%  ·  LEVEL {boost}/10", "tech", int(12 * s), CYAN, "ra")
        n = 32
        top = y + int(24 * s)
        bot = y + h - int(8 * s)
        gw = (w - int(20 * s)) / n
        for k in range(n):
            shape = 0.95 - 0.55 * (k / n)
            lvl = max(0.06, min(1.0, shape * (0.35 + 0.85 * _noise(now * 2.2, k)) * energy))
            self._peaks[k] = max(lvl, self._peaks[k] - 0.04)
            bx = x + int(10 * s) + int(k * gw)
            bh = int((bot - top) * lvl)
            seg = max(3, int(4 * s))
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
        gap = int(7 * s)
        cw = (w - gap * 3) / 4
        for i, (k, v, c) in enumerate(items):
            cx0 = x + int(i * (cw + gap))
            self._glass(d, [cx0, y, cx0 + int(cw), y + h], int(10 * s), GLASS2)
            d.rectangle([cx0 + int(8 * s), y + int(7 * s), cx0 + int(8 * s) + int(16 * s),
                         y + int(9 * s)], fill=c)
            T.draw(d, (cx0 + int(8 * s), y + int(12 * s)), k, "tech", int(11 * s), MUTED, "la")
            T.draw(d, (cx0 + int(8 * s), y + h - int(7 * s)), v, "display", int(16 * s), TEXT, "ld",
                   max_w=int(cw) - int(12 * s))

    def _mixer(self, d, st, x, y, w, h, now, energy, mic_on, np_name, paused):
        s = self.s
        self._glass(d, [x, y, x + w, y + h], int(12 * s), GLASS2)
        self._label(d, x + int(10 * s), y + int(7 * s), "AUDIO LEVELS", MUTED, int(12 * s))
        T.draw(d, (x + w - int(10 * s), y + int(7 * s)),
               "Live connection", "tech", int(12 * s), MUTED, "ra",
               max_w=w * 0.5)
        cy = y + int(26 * s)
        cx = x + int(10 * s)
        for i, fx in enumerate(("HPF", "GATE", "COMP", "EQ", "LIMIT")):
            on = _wave(now, 3, i) > 0.15
            pw, ph = self._chip(d, cx, cy, fx, GREEN if on else DIM, size=int(11 * s))
            cx += pw + int(4 * s)
            if cx > x + w - int(35 * s):
                break
        top = cy + int(28 * s)
        bot = y + h - int(26 * s)
        if bot - top < int(35 * s):
            return
        chans = [("MIC", RED, 1.0 if mic_on else 0.35, 0.78),
                 ("MUSIC", CYAN, 1.0 if (np_name and not paused) else 0.4, 0.70),
                 ("FX", VIOLET, 0.6, 0.55),
                 ("MASTER", PINK, energy, 0.86)]
        n = len(chans)
        colw = w / n
        for i, (name, col, act, fader) in enumerate(chans):
            ccx = x + int(colw * i + colw / 2)
            for side in (-1, 1):
                mx0 = ccx + side * int(10 * s) - int(3 * s)
                lvl = max(0.04, min(1.0, _noise(now * 2.6, i * 3 + side))) * act
                segs = 14
                sh = (bot - top) / segs
                for k in range(segs):
                    t = k / segs
                    lit = t < lvl
                    c = GREEN if t < 0.6 else (YELLOW if t < 0.85 else RED)
                    yy = bot - int((k + 1) * sh)
                    d.rectangle([mx0, yy + 1, mx0 + int(7 * s), yy + int(sh) - 1],
                                fill=c if lit else (34, 36, 52))
            d.rounded_rectangle([ccx - 2, top, ccx + 2, bot], radius=2, fill=(50, 54, 80))
            fy = bot - int((bot - top) * (fader + 0.03 * math.sin(now + i)))
            fw, fh = int(14 * s), int(8 * s)
            d.rounded_rectangle([ccx - fw, fy - fh, ccx + fw, fy + fh], radius=int(3 * s),
                                fill=(210, 214, 230), outline=col, width=max(1, int(2 * s)))
            d.line([ccx - fw + 3, fy, ccx + fw - 3, fy], fill=col, width=max(1, int(2 * s)))
            T.draw(d, (ccx, bot + int(7 * s)), name, "tech", int(12 * s), col, "ma")

    def _ticker(self, d, st, y, now):
        W, s = self.w, self.s
        h = self.h - y
        d.rectangle([0, y, W, self.h], fill=(8, 8, 16))
        d.line([0, y, W, y], fill=_mix(LINE, PINK, 0.4), width=max(1, int(2 * s)))
        tagw = int(130 * s)
        d.rectangle([0, y + 2, tagw, self.h], fill=PINK)
        T.draw(d, (tagw // 2, y + h // 2), "VOICE CHAT", "display", int(14 * s), BG0, "mm",
               spacing=max(1, int(1 * s)))
        dt = datetime.fromtimestamp(now, _tz())
        cw = int(200 * s)
        d.rectangle([W - cw, y + 2, W, self.h], fill=(14, 14, 26))
        T.draw(d, (W - int(14 * s), y + h // 2 - int(1 * s)), dt.strftime("%I:%M:%S %p"),
               "display", int(16 * s), TEXT, "rd")
        T.draw(d, (W - int(14 * s), y + h // 2 + int(3 * s)), dt.strftime("%a, %d %b %Y"),
               "tech", int(13 * s), MUTED, "ra")
        parts = st.get("participants") or []
        spk = [T.clean(p.get("name", "")) for p in parts if p.get("speaking")]
        spk = [x for x in spk if x][:4]
        np_name = st.get("now_playing") or ""
        items = []
        if spk:
            items.append("Speaking now: " + ", ".join(spk))
        if np_name:
            items.append(("Paused: " if st.get("paused") else "Playing: ") + T.clean(np_name))
        if st.get("mic_on"):
            items.append("Microphone active")
        items += [f"{len(parts)} people in the call", "Connected live", "Voice chat is active"]
        text = "     ·     ".join(items) + "     ·     "
        fs = int(17 * s)
        runs_w = T.text_width(text, "bold", fs) or 1
        x0, x1 = tagw + int(12 * s), W - cw - int(12 * s)
        off = (now * 80 * s) % runs_w
        sp = T._sprite(text, "bold", fs, None, 0)
        if sp is None:
            return
        mask = sp[0]
        mh = mask.height
        sw = max(1, x1 - x0)
        xx = -off
        cy = y + h // 2
        while xx < sw:
            l = max(0, int(-xx))
            r = min(mask.width, int(sw - xx))
            if r > l:
                d.bitmap((x0 + int(xx) + l, int(cy - mh / 2)), mask.crop((l, 0, r, mh)), fill=TEXT)
            xx += runs_w


def rgb_to_i420(img):
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
            except Exception as exc:
                print(f"telegram_live_screen render error: {exc!r}", file=sys.stderr)
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
