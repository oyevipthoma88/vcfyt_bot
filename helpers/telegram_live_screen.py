#!/usr/bin/env python3
"""LIVE screen share — realistic Telegram "Voice Chat" window.

Looks like the real Telegram dark voice-chat screen:
  * background = main user's own profile photo (blurred, darkened)
  * centre stage = big DP with live "speaking" waves + name in a display font
  * bottom bar = Screen / big Mic button (DP behind the mic icon) / Leave
  * right panel = real participants list (DP, proper name, speaking / listening
    / muted / wants to speak, mic icon)

NAME FIX: the old renderer stripped every non A-Z character, so fancy names
("𝐃𝐄𝐕𝐈𝐋 ✨ ꧁ᴀsʜɪsʜ꧂", Hindi, emoji) became "DV" / "User".  Text is now drawn
with live_screen's per-character font fallback chain (Noto Sans → Math →
Symbols → Emoji → Devanagari …), so names show exactly as on Telegram.

Usage: telegram_live_screen.py --state FILE [--w 1280 --h 720 --fps 15]
       telegram_live_screen.py --state FILE --png out.png   (test one frame)
"""
import argparse
import hashlib
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:  # shared fancy-unicode text engine
    from live_screen import T, rgb_to_i420  # noqa: E402
except Exception:  # pragma: no cover - ultra fallback, never crash the SS
    T = None

    def rgb_to_i420(img):
        import numpy as np
        ycc = np.asarray(img.convert("YCbCr"), dtype=np.uint8)
        y = (ycc[..., 0].astype(np.uint16) * 219 // 255 + 16).astype(np.uint8)
        u = ycc[0::2, 0::2, 1].astype(np.uint16) * 224 // 255 + 16
        v = ycc[0::2, 0::2, 2].astype(np.uint16) * 224 // 255 + 16
        return y.tobytes() + u.astype(np.uint8).tobytes() + v.astype(np.uint8).tobytes()

# ------------------------------------------------------- Telegram dark palette
BG = (6, 7, 18)
PANEL = (30, 37, 48)
PANEL2 = (38, 46, 59)
ROW_HI = (44, 54, 69)
LINE = (52, 62, 78)
TEXT = (255, 255, 255)
SUB = (150, 160, 200)
BLUE = (77, 166, 255)      # Telegram "listening" / active button
GREEN = (75, 205, 110)     # speaking
TEAL = (52, 199, 168)
ORANGE = (255, 170, 60)    # wants to speak
RED = (235, 77, 77)        # leave / muted / live
GREY = (110, 122, 140)
AV_COLORS = [(229, 103, 99), (247, 159, 80), (168, 124, 230), (102, 196, 109),
             (84, 186, 196), (82, 154, 232), (232, 107, 160)]


def _tz():
    try:
        off = float(os.environ.get("SS_TZ_OFFSET", "5.5"))
    except ValueError:
        off = 5.5
    return timezone(timedelta(hours=off))


def _read_state(p):
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _clock(sec):
    sec = int(max(0, sec or 0))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _color_for(key):
    k = int(hashlib.md5(str(key).encode()).hexdigest()[:6], 16)
    return AV_COLORS[k % len(AV_COLORS)]


NEON_C = (0, 240, 255)
NEON_G = (57, 255, 150)
NEON_M = (255, 46, 196)
NEON_V = (140, 82, 255)


def _mix(a, b, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


_PIL_FONTS = {}


def _pil_font(size, bold=True):
    key = (size, bold)
    f = _PIL_FONTS.get(key)
    if f is None:
        for p in (os.path.join(os.path.dirname(HERE), "assets", "fonts", "NotoSans.ttf"),
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
            try:
                f = ImageFont.truetype(p, size)
                break
            except Exception:
                f = None
        f = f or ImageFont.load_default()
        _PIL_FONTS[key] = f
    return f


def text(d, xy, s, style="ui", size=20, fill=TEXT, anchor="la", max_w=None):
    """Draw text with full Unicode fallback; returns drawn width."""
    size = max(8, int(size))
    if T is not None:
        return T.draw(d, xy, s, style=style, size=size, fill=fill, anchor=anchor, max_w=max_w)
    f = _pil_font(size)
    d.text(xy, str(s or ""), font=f, fill=fill, anchor=anchor)
    return d.textlength(str(s or ""), font=f)


def text_w(s, style="ui", size=20):
    if T is not None:
        return T.text_width(s, style, max(8, int(size)))
    return _pil_font(size).getlength(str(s or ""))


def clean_name(name, fallback="User"):
    """Proper display name: keep fancy Unicode, drop only invisible junk."""
    s = " ".join(str(name or "").split())
    if T is not None:
        vis = T.clean(s, "bold", 24)
        if not vis:
            return fallback
    return s or fallback


def initials(name):
    s = T.clean(name, "bold", 24) if T is not None else str(name or "")
    parts = [p for p in s.split() if any(c.isalnum() for c in p)]
    if len(parts) >= 2:
        out = "".join(next(c for c in p if c.isalnum()) for p in parts[:2])
    else:
        out = "".join(c for c in s if c.isalnum())[:2]
    return (out or "?").upper()


# ================================================================ renderer
class Renderer:
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.s = h / 720.0
        self._av = {}
        self._bg_key = None
        self._bg = None

    def S(self, v):
        return int(round(v * self.s))

    # ------------------------------------------------------------ images
    def _load_square(self, path):
        try:
            im = Image.open(path).convert("RGB")
        except Exception:
            return None
        w0, h0 = im.size
        side = min(w0, h0)
        return im.crop(((w0 - side) // 2, (h0 - side) // 2,
                        (w0 - side) // 2 + side, (h0 - side) // 2 + side))

    def avatar(self, path, size, name, key=None):
        """Round avatar (photo or Telegram-style coloured initials)."""
        mt = 0
        if path:
            try:
                mt = os.path.getmtime(path)
            except Exception:
                path = ""
        ck = (path, mt, size, name if not path else "")
        im = self._av.get(ck)
        if im is not None:
            return im
        sq = self._load_square(path) if path else None
        big = size * 3
        if sq is not None:
            base = sq.resize((big, big), Image.LANCZOS)
        else:
            c = _color_for(key or name)
            c2 = _mix(c, (255, 255, 255), 0.25)
            base = Image.new("RGB", (big, big), c)
            gd = ImageDraw.Draw(base)
            for i in range(big):  # soft vertical gradient like Telegram
                gd.line([0, i, big, i], fill=_mix(c2, c, i / big))
            text(gd, (big / 2, big / 2), initials(name), style="bold",
                 size=big * 0.38, fill=TEXT, anchor="mm")
        mask = Image.new("L", (big, big), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, big - 1, big - 1], fill=255)
        base = base.convert("RGBA")
        base.putalpha(mask)
        im = base.resize((size, size), Image.LANCZOS)
        if len(self._av) > 400:
            self._av.clear()
        self._av[ck] = im
        return im

    # ------------------------------------------------------------ neon fx
    def glow(self, img, box, color, alpha=170, blur=10, kind="ellipse",
             radius=0, width=0):
        """Cheap neon glow: blur only a small crop, never the whole frame."""
        x0, y0, x1, y1 = [int(v) for v in box]
        pad = int(blur * 3) + max(2, width)
        gw, gh = x1 - x0 + 2 * pad, y1 - y0 + 2 * pad
        if gw <= 0 or gh <= 0:
            return
        lay = Image.new("RGBA", (gw, gh), (0, 0, 0, 0))
        ld = ImageDraw.Draw(lay)
        bb = [pad, pad, pad + x1 - x0, pad + y1 - y0]
        col = tuple(color[:3]) + (int(alpha),)
        if kind == "ellipse":
            if width:
                ld.ellipse(bb, outline=col, width=width)
            else:
                ld.ellipse(bb, fill=col)
        else:
            if width:
                ld.rounded_rectangle(bb, radius=radius, outline=col, width=width)
            else:
                ld.rounded_rectangle(bb, radius=radius, fill=col)
        if blur > 0:
            lay = lay.filter(ImageFilter.GaussianBlur(blur))
        img.paste(lay, (x0 - pad, y0 - pad), lay)

    def neon_box(self, img, box, color, radius, fill=None, width=2, glow_a=150):
        self.glow(img, box, color, alpha=glow_a, blur=self.S(9), kind="rr",
                  radius=radius, width=max(2, self.S(5)))
        d = ImageDraw.Draw(img, "RGBA")
        d.rounded_rectangle(box, radius=radius, fill=fill, outline=tuple(color[:3]) + (255,),
                            width=max(1, width))

    # ------------------------------------------------------------ static layer
    def background(self, photo, name):
        """Cached static layer: DP backdrop + cyber grid + vignette + frames."""
        try:
            mt = os.path.getmtime(photo) if photo else 0
        except Exception:
            mt, photo = 0, ""
        key = (photo, mt, name)
        if key == self._bg_key and self._bg is not None:
            return self._bg
        W, H, S = self.w, self.h, self.S
        bg = Image.new("RGB", (W, H), BG)
        sq = self._load_square(photo) if photo else None
        if sq is not None:
            side = max(W, H)
            ph = sq.resize((max(8, side // 6), max(8, side // 6)), Image.BILINEAR)
            ph = ph.filter(ImageFilter.GaussianBlur(6)).resize((side, side), Image.BILINEAR)
            ph = ph.crop(((side - W) // 2, (side - H) // 2,
                          (side - W) // 2 + W, (side - H) // 2 + H))
            bg = Image.blend(ph, bg, 0.72)
        bg = bg.convert("RGBA")
        # neon colour washes (top-left cyan, bottom-right magenta)
        for cx, cy, rr, c, a in ((W * 0.18, H * 0.05, W * 0.45, NEON_C, 70),
                                 (W * 0.85, H * 1.05, W * 0.50, NEON_M, 80),
                                 (W * 0.35, H * 0.55, W * 0.30, NEON_V, 40)):
            small = Image.new("RGBA", (W // 4, H // 4), (0, 0, 0, 0))
            ImageDraw.Draw(small).ellipse([(cx - rr) / 4, (cy - rr) / 4, (cx + rr) / 4, (cy + rr) / 4],
                                          fill=c + (a,))
            small = small.filter(ImageFilter.GaussianBlur(S(30) / 4 + 6)).resize((W, H), Image.BILINEAR)
            bg.alpha_composite(small)
        # perspective cyber grid on the floor of the stage
        grid = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        gd = ImageDraw.Draw(grid)
        panel_x = int(W * 0.69)
        hz = int(H * 0.60)
        vx = panel_x // 2
        for i in range(-12, 13):
            gd.line([(vx + i * S(22), hz), (vx + i * S(170), H)], fill=NEON_V + (55,), width=1)
        y, step = hz, S(6)
        while y < H:
            a = int(25 + 70 * (y - hz) / max(1, H - hz))
            gd.line([(0, y), (panel_x, y)], fill=NEON_M + (a,), width=1)
            step = int(step * 1.35) + 1
            y += step
        bg.alpha_composite(grid)
        # subtle scanlines
        sl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sd = ImageDraw.Draw(sl)
        for yy in range(0, H, 3):
            sd.line([(0, yy), (W, yy)], fill=(0, 0, 0, 38))
        bg.alpha_composite(sl)
        bg = bg.convert("RGB")
        # vignette
        v = Image.new("L", (W // 8, H // 8), 0)
        vd = ImageDraw.Draw(v)
        vd.rectangle([0, 0, W // 8, H // 8], fill=170)
        vd.ellipse([-W // 40, -H // 40, W // 8 + W // 40, H // 8 + H // 40], fill=0)
        v = v.filter(ImageFilter.GaussianBlur(6)).resize((W, H), Image.BILINEAR)
        bg.paste(Image.new("RGBA", (W, H), (2, 2, 8, 255)), (0, 0), v)
        # header bar + participants panel frame
        self.neon_box(bg, [S(20), S(18), panel_x - S(16), S(84)], NEON_C, S(18),
                      fill=(6, 8, 22, 200), width=S(2), glow_a=120)
        self.neon_box(bg, [panel_x, S(18), W - S(20), H - S(20)], NEON_M, S(20),
                      fill=(8, 6, 22, 215), width=S(2), glow_a=130)
        # bottom dock
        self.neon_box(bg, [panel_x // 2 - S(230), H - S(138), panel_x // 2 + S(230), H - S(24)],
                      NEON_V, S(40), fill=(6, 6, 20, 210), width=S(2), glow_a=110)
        # corner brackets (HUD look)
        d = ImageDraw.Draw(bg, "RGBA")
        L = S(26)
        for (x, y, sx, sy) in ((S(10), S(10), 1, 1), (W - S(10), S(10), -1, 1),
                               (S(10), H - S(10), 1, -1), (W - S(10), H - S(10), -1, -1)):
            d.line([(x, y), (x + sx * L, y)], fill=NEON_C + (255,), width=S(3))
            d.line([(x, y), (x, y + sy * L)], fill=NEON_C + (255,), width=S(3))
        self._bg_key, self._bg = key, bg
        return bg

    # ------------------------------------------------------------ icons
    def mic_icon(self, d, cx, cy, sz, col, muted=False, width=None):
        lw = width or max(2, int(sz * 0.11))
        bw, bh = sz * 0.34, sz * 0.55
        d.rounded_rectangle([cx - bw / 2, cy - sz * 0.5, cx + bw / 2, cy - sz * 0.5 + bh],
                            radius=bw / 2, fill=col)
        r = sz * 0.36
        d.arc([cx - r, cy - sz * 0.5 + bh * 0.25 - r * 0.2, cx + r, cy - sz * 0.5 + bh + r * 0.35],
              start=10, end=170, fill=col, width=lw)
        d.line([cx, cy + sz * 0.3, cx, cy + sz * 0.46], fill=col, width=lw)
        d.line([cx - sz * 0.18, cy + sz * 0.47, cx + sz * 0.18, cy + sz * 0.47], fill=col, width=lw)
        if muted:
            d.line([cx - sz * 0.42, cy - sz * 0.48, cx + sz * 0.42, cy + sz * 0.48],
                   fill=RED, width=lw + 1)

    def screen_icon(self, d, cx, cy, sz, col):
        lw = max(2, int(sz * 0.1))
        d.rounded_rectangle([cx - sz * 0.5, cy - sz * 0.38, cx + sz * 0.5, cy + sz * 0.26],
                            radius=sz * 0.08, outline=col, width=lw)
        d.line([cx, cy + sz * 0.26, cx, cy + sz * 0.42], fill=col, width=lw)
        d.line([cx - sz * 0.25, cy + sz * 0.44, cx + sz * 0.25, cy + sz * 0.44], fill=col, width=lw)
        d.polygon([(cx, cy - sz * 0.22), (cx - sz * 0.16, cy - sz * 0.02),
                   (cx + sz * 0.16, cy - sz * 0.02)], fill=col)
        d.line([cx, cy - sz * 0.05, cx, cy + sz * 0.12], fill=col, width=lw)

    def leave_icon(self, d, cx, cy, sz, col):
        lw = max(3, int(sz * 0.16))
        d.arc([cx - sz * 0.5, cy - sz * 0.25, cx + sz * 0.5, cy + sz * 0.55],
              start=200, end=340, fill=col, width=lw)
        d.line([cx - sz * 0.46, cy + sz * 0.05, cx - sz * 0.36, cy + sz * 0.22], fill=col, width=lw)
        d.line([cx + sz * 0.46, cy + sz * 0.05, cx + sz * 0.36, cy + sz * 0.22], fill=col, width=lw)

    def waves(self, layer, cx, cy, r, col, now, energy):
        """Telegram-like soft 'blob' rings around a speaking avatar."""
        ld = ImageDraw.Draw(layer)
        for i, (k, a) in enumerate(((1.30, 50), (1.18, 80), (1.08, 120))):
            wob = 1 + energy * 0.06 * math.sin(now * (3.1 + i) + i * 1.7)
            rr = r * (1 + (k - 1) * (0.55 + 0.45 * energy)) * wob
            ld.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=col + (int(a * (0.5 + 0.5 * energy)),))

    # ------------------------------------------------------------ frame
    def render(self, st, now, started):
        W, H = self.w, self.h
        S = self.S
        st = st or {}
        parts = list(st.get("participants") or [])
        me_p = next((p for p in parts if p.get("is_me")), None)
        me_name = clean_name(st.get("me_name") or (me_p or {}).get("name")
                             or (("@" + st["me_user"]) if st.get("me_user") else ""), "You")
        me_photo = st.get("me_photo") or (me_p or {}).get("photo") or ""
        if me_photo and not os.path.exists(me_photo):
            me_photo = ""
        mic_on = bool(st.get("mic_on"))
        playing = bool(st.get("now_playing")) and not st.get("paused")
        audio = bool(st.get("audio_active")) or playing
        me_speaking = playing or (mic_on and audio) or bool(me_p and me_p.get("speaking"))
        me_muted = bool(st.get("ghost_mute")) or (not mic_on and not audio
                                                    and bool(me_p and me_p.get("muted")))

        img = self.background(me_photo, me_name).copy()
        d = ImageDraw.Draw(img, "RGBA")
        panel_x = int(W * 0.69)

        # ================= header
        hue = 0.5 + 0.5 * math.sin(now * 1.3)
        title_col = _mix(NEON_C, NEON_M, hue * 0.35)
        text(d, (S(42), S(30)), f"{me_name}", style="display", size=S(26), fill=title_col,
             max_w=panel_x - S(330))
        n = len(parts) or 1
        sub = f"VOICE CHAT  ·  {n} participant" + ("" if n == 1 else "s")
        text(d, (S(42), S(64)), sub, style="bold", size=S(13), fill=SUB, max_w=panel_x - S(330))
        # LIVE pill + timer
        lx = panel_x - S(34)
        tm = _clock(now - started)
        tw = text_w(tm, "bold", S(17))
        pw = int(tw + S(100))
        pill = [lx - pw, S(34), lx, S(68)]
        blink = 0.55 + 0.45 * math.sin(now * 4)
        self.glow(img, pill, RED, alpha=int(90 + 90 * blink), blur=S(8), kind="rr", radius=S(17))
        d = ImageDraw.Draw(img, "RGBA")
        d.rounded_rectangle(pill, radius=S(17), fill=(18, 4, 12, 235), outline=RED + (255,), width=S(2))
        d.ellipse([lx - pw + S(14), S(45), lx - pw + S(26), S(57)], fill=_mix((60, 10, 20), RED, blink))
        text(d, (lx - pw + S(33), S(51)), "LIVE", style="bold", size=S(14), fill=TEXT, anchor="lm")
        text(d, (lx - S(14), S(51)), tm, style="bold", size=S(17), fill=NEON_C, anchor="rm")

        # ================= centre stage
        cx = panel_x // 2
        cy = int(H * 0.40)
        r = S(104)
        energy = 0.0
        if me_speaking:
            energy = 0.55 + 0.45 * abs(math.sin(now * 2.3)) * (0.6 + 0.4 * math.sin(now * 5.7))
        ring_col = NEON_G if me_speaking else (GREY if me_muted else NEON_C)
        # soft aura
        ar = r + S(30) + int(S(18) * energy)
        self.glow(img, [cx - ar, cy - ar, cx + ar, cy + ar], ring_col,
                  alpha=int(70 + 90 * energy), blur=S(22))
        # rotating neon arc segments (HUD ring)
        d = ImageDraw.Draw(img, "RGBA")
        rot = (now * 60) % 360
        for i, (rr, w_, col, sp) in enumerate(((r + S(18), S(3), NEON_C, 1.0),
                                                (r + S(30), S(2), NEON_M, -0.7),
                                                (r + S(42), S(2), NEON_V, 0.45))):
            base = rot * sp
            for k in range(3 if i != 1 else 4):
                a0 = base + k * (360 / (3 if i != 1 else 4))
                d.arc([cx - rr, cy - rr, cx + rr, cy + rr], start=a0, end=a0 + 55 + 25 * energy,
                      fill=col + (230,), width=w_)
        # radial equaliser bars around avatar
        nbar = 48
        for i in range(nbar):
            ang = i / nbar * 2 * math.pi
            v = abs(math.sin(now * (4.0 + (i % 6) * 0.9) + i * 0.7))
            ln = S(4) + int(S(26) * v * (energy if me_speaking else 0.08))
            r0 = r + S(52)
            x0, y0 = cx + math.cos(ang) * r0, cy + math.sin(ang) * r0
            x1, y1 = cx + math.cos(ang) * (r0 + ln), cy + math.sin(ang) * (r0 + ln)
            col = _mix(NEON_C, NEON_M, (math.sin(ang + now) + 1) / 2)
            d.line([(x0, y0), (x1, y1)], fill=col + (235,), width=max(2, S(3)))
        # avatar
        av = self.avatar(me_photo, 2 * r, me_name, key=st.get("me_user") or me_name)
        img.paste(av, (cx - r, cy - r), av)
        d = ImageDraw.Draw(img, "RGBA")
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=ring_col + (255,), width=S(4))
        d.ellipse([cx - r - S(6), cy - r - S(6), cx + r + S(6), cy + r + S(6)],
                  outline=ring_col + (90,), width=S(2))
        # name + status
        ny = cy + r + S(70)
        text(d, (cx, ny), me_name, style="display", size=S(32), fill=TEXT, anchor="mt",
             max_w=panel_x - S(80))
        if me_speaking:
            line, sc = "● speaking", NEON_G
        elif me_muted:
            line, sc = "muted", SUB
        elif mic_on:
            line, sc = "● live", NEON_C
        else:
            line, sc = "listening", NEON_C
        text(d, (cx, ny + S(44)), line, style="bold", size=S(16), fill=sc, anchor="mt",
             max_w=panel_x - S(80))

        # ================= bottom dock
        by = H - S(80)
        for bx, col, kind, lab in ((cx - S(150), NEON_C, "screen", "screen"),
                                   (cx + S(150), RED, "leave", "leave")):
            box = [bx - S(28), by - S(28), bx + S(28), by + S(28)]
            self.glow(img, box, col, alpha=120, blur=S(8))
            d = ImageDraw.Draw(img, "RGBA")
            d.ellipse(box, fill=(10, 10, 26, 255), outline=col + (255,), width=S(2))
            if kind == "screen":
                self.screen_icon(d, bx, by, S(28), col)
            else:
                self.leave_icon(d, bx, by, S(28), col)
            text(d, (bx, by + S(36)), lab, style="bold", size=S(12), fill=SUB, anchor="mt")
        mr = S(44)
        mcol = NEON_G if me_speaking else (GREY if me_muted else NEON_C)
        gr = mr + S(8) + int(S(12) * energy)
        self.glow(img, [cx - gr, by - S(6) - gr, cx + gr, by - S(6) + gr], mcol,
                  alpha=int(120 + 100 * energy), blur=S(12))
        d = ImageDraw.Draw(img, "RGBA")
        d.ellipse([cx - mr, by - S(6) - mr, cx + mr, by - S(6) + mr], fill=_mix((8, 8, 20), mcol, 0.35) + (255,),
                  outline=mcol + (255,), width=S(3))
        self.mic_icon(d, cx, by - S(6), S(40), TEXT, muted=me_muted, width=S(4))

        # ================= participants panel
        self.panel(d, img, st, parts, me_name, me_photo, panel_x, now)
        d = ImageDraw.Draw(img, "RGBA")
        return img

    def panel(self, d, img, st, parts, me_name, me_photo, x0, now):
        W, H, S = self.w, self.h, self.S
        px0, py0, px1, py1 = x0, S(18), W - S(20), H - S(20)
        d = ImageDraw.Draw(img, "RGBA")
        text(d, (px0 + S(22), py0 + S(30)), "PARTICIPANTS", style="bold", size=S(15),
             fill=NEON_M, anchor="lm")
        n = len(parts)
        cnt = str(n)
        cw = int(text_w(cnt, "bold", S(14)) + S(20))
        d.rounded_rectangle([px1 - S(20) - cw - S(70), py0 + S(18), px1 - S(20) - S(70), py0 + S(42)],
                            radius=S(12), fill=NEON_M + (60,), outline=NEON_M + (255,), width=1)
        text(d, (px1 - S(20) - S(70) - cw // 2, py0 + S(30)), cnt, style="bold", size=S(14),
             fill=TEXT, anchor="mm")
        d.line([px0 + S(16), py0 + S(58), px1 - S(16), py0 + S(58)], fill=NEON_M + (90,), width=1)

        def order(p):
            if p.get("is_me"):
                return 0
            if p.get("speaking"):
                return 1
            if p.get("hand"):
                return 2
            return 3 if not p.get("muted") else 4

        rows = sorted(parts, key=order)
        if not rows:
            rows = [{"name": me_name, "photo": me_photo, "is_me": True}]
        row_h = S(60)
        y = py0 + S(68)
        card_h = S(178)
        max_rows = max(1, (py1 - card_h - y - S(14)) // row_h)
        more = 0
        if len(rows) > max_rows:
            more = len(rows) - (max_rows - 1)
            rows = rows[:max_rows - 1]
        avs = S(42)
        for p in rows:
            is_me = bool(p.get("is_me"))
            nm = clean_name(me_name if is_me else p.get("name"), "Deleted Account")
            spk = bool(p.get("speaking")) or (is_me and bool(st.get("mic_on")) and
                                             (bool(st.get("audio_active")) or bool(st.get("now_playing"))))
            muted = bool(p.get("muted")) and not spk
            if spk:
                d.rounded_rectangle([px0 + S(10), y, px1 - S(10), y + row_h - S(6)], radius=S(12),
                                    fill=NEON_G + (28,), outline=NEON_G + (140,), width=1)
            ax, ay = px0 + S(20), y + (row_h - S(6) - avs) // 2
            ph = p.get("photo") or (me_photo if is_me else "")
            if ph and not os.path.exists(ph):
                ph = ""
            av = self.avatar(ph, avs, nm, key=p.get("uid") or nm)
            img.paste(av, (ax, ay), av)
            d = ImageDraw.Draw(img, "RGBA")
            ring = NEON_G if spk else (GREY if muted else NEON_C)
            pulse = (0.5 + 0.5 * math.sin(now * 5)) if spk else 0
            rr = avs // 2 + S(2) + int(S(2) * pulse)
            c = (ax + avs // 2, ay + avs // 2)
            d.ellipse([c[0] - rr, c[1] - rr, c[0] + rr, c[1] + rr], outline=ring + (255,), width=S(2))
            tx = ax + avs + S(14)
            name_max = px1 - tx - S(56)
            suffix = "  (you)" if is_me else ""
            wname = text(d, (tx, y + S(18)), nm, style="bold", size=S(16), fill=TEXT, anchor="lm",
                         max_w=name_max - (text_w(suffix, "ui", S(13)) if suffix else 0))
            if suffix:
                text(d, (tx + (wname or 0), y + S(18)), suffix, size=S(13), fill=NEON_C, anchor="lm")
            if spk:
                s, col = "speaking", NEON_G
            elif p.get("hand"):
                s, col = "✋ wants to speak", ORANGE
            elif muted:
                s, col = "muted", SUB
            else:
                s, col = "listening", NEON_C
            vol = int(p.get("volume") or 100)
            if vol != 100 and not is_me:
                s += f"  ·  {vol}%"
            text(d, (tx, y + S(39)), s, size=S(13), fill=col, anchor="lm", max_w=name_max)
            mx, my = px1 - S(34), y + (row_h - S(6)) // 2
            if spk:  # mini equaliser instead of static icon
                for k in range(4):
                    hh = S(4) + int(S(16) * abs(math.sin(now * (7 + k) + k)))
                    xk = mx - S(12) + k * S(7)
                    d.rounded_rectangle([xk, my - hh // 2, xk + S(4), my + hh // 2], radius=S(2),
                                        fill=NEON_G + (255,))
            else:
                self.mic_icon(d, mx, my, S(22), ring, muted=muted)
            y += row_h
        if more:
            text(d, ((px0 + px1) // 2, y + row_h // 2 - S(4)), f"+ {more} more listening",
                 style="bold", size=S(14), fill=SUB, anchor="mm")
        self.sys_card(img, st, px0 + S(12), py1 - card_h - S(10), px1 - S(12), py1 - S(12), now, len(parts))

    def sys_card(self, img, st, x0, y0, x1, y1, now, n):
        """Real device-style info: live clock with seconds, date, network, battery."""
        S = self.S
        d = ImageDraw.Draw(img, "RGBA")
        d.rounded_rectangle([x0, y0, x1, y1], radius=S(14), fill=(4, 10, 24, 230),
                            outline=NEON_C + (150,), width=1)
        dt = datetime.now(_tz())
        hm = dt.strftime("%I:%M").lstrip("0")
        sec = dt.strftime("%S")
        ap = dt.strftime("%p")
        cx = x0 + S(18)
        base_y = y0 + S(54)
        text(d, (cx, base_y), hm, style="display", size=S(34), fill=NEON_C, anchor="ls")
        hw = text_w(hm, "display", S(34))
        text(d, (cx + hw + S(4), base_y), ":" + sec, style="bold", size=S(20), fill=NEON_M, anchor="ls")
        sw = text_w(":" + sec, "bold", S(20))
        text(d, (cx + hw + sw + S(10), base_y), ap, style="bold", size=S(13), fill=SUB, anchor="ls")
        text(d, (cx, y0 + S(70)), dt.strftime("%A, %d %B %Y"), style="bold", size=S(13),
             fill=TEXT, anchor="lt", max_w=x1 - cx - S(10))
        off = dt.utcoffset().total_seconds() / 3600 if dt.utcoffset() else 0
        tzs = "IST" if abs(off - 5.5) < 0.01 else f"UTC{off:+g}"
        text(d, (cx, y0 + S(92)), f"{tzs}  ·  {self.w}x{self.h}  ·  HD", size=S(12), fill=SUB, anchor="lt")
        # top-right: wifi + signal + battery
        rx, ry = x1 - S(18), y0 + S(18)
        bw, bh = S(28), S(14)
        lvl = 0.62 + 0.3 * abs(math.sin(now / 900.0))
        d.rounded_rectangle([rx - bw, ry, rx, ry + bh], radius=S(3), outline=TEXT + (220,), width=S(2))
        d.rectangle([rx, ry + S(4), rx + S(3), ry + bh - S(4)], fill=TEXT + (220,))
        d.rectangle([rx - bw + S(3), ry + S(3), rx - bw + S(3) + int((bw - S(6)) * lvl), ry + bh - S(3)],
                    fill=NEON_G + (255,))
        text(d, (rx - bw - S(6), ry + bh // 2), f"{int(lvl * 100)}%", style="bold", size=S(12),
             fill=TEXT, anchor="rm")
        sx = rx - bw - S(52)
        for k in range(4):
            hh = S(4) + k * S(3)
            d.rectangle([sx - S(24) + k * S(6), ry + bh - hh, sx - S(20) + k * S(6), ry + bh],
                        fill=(NEON_C if k < 3 + int(now) % 2 else GREY) + (255,))
        # bottom row: live stats
        ping = 28 + int(14 * abs(math.sin(now * 0.7)) + 6 * abs(math.sin(now * 2.9)))
        kbps = 1800 + int(500 * abs(math.sin(now * 0.4)))
        fps = int(st.get("_fps") or 15)
        stats = [("PING", f"{ping} ms", NEON_G if ping < 50 else ORANGE),
                 ("BITRATE", f"{kbps / 1000:.1f} Mb/s", NEON_C),
                 ("FPS", str(fps), NEON_M), ("USERS", str(max(1, n)), TEXT)]
        cw = (x1 - x0 - S(24)) / len(stats)
        by = y1 - S(52)
        d.line([x0 + S(12), by - S(8), x1 - S(12), by - S(8)], fill=NEON_C + (60,), width=1)
        for i, (k, v, c) in enumerate(stats):
            tx = int(x0 + S(12) + cw * i + cw / 2)
            text(d, (tx, by + S(4)), k, style="bold", size=S(10), fill=SUB, anchor="mt")
            text(d, (tx, by + S(22)), v, style="bold", size=S(14), fill=c, anchor="mt", max_w=int(cw - S(4)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True)
    ap.add_argument("--w", type=int, default=1280)
    ap.add_argument("--h", type=int, default=720)
    ap.add_argument("--fps", type=int, default=15)
    ap.add_argument("--png", default="", help="test: save one frame as PNG and exit")
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
    # SMOOTH SS ON/OFF: lowest CPU priority so the audio pipe always wins.
    try:
        os.nice(15)
    except (AttributeError, OSError):
        pass
    out = sys.stdout.buffer
    started = time.time()
    frame, key, last, st = None, None, 0.0, {}
    t0 = time.time()
    n = 0
    parent = os.getppid()
    seen_state, last_seen = False, time.time()
    while True:
        now = time.time()
        if now - last > 0.75:
            new = _read_state(a.state)
            if new:
                new["_read_at"] = now
                st = new
            last = now
            if st.get("started"):
                started = float(st["started"])
        # ORPHAN GUARD (SS on/off crash root fix): har toggle pe naya renderer
        # spawn hota hai. Agar ntgcalls ne pipe band kiye bina chhod diya ya
        # bot ne SS off karke state file hata di, to yeh process khud exit
        # kare — warna 2-3 toggle me zombie renderers CPU/RAM kha ke bot ko
        # crash/restart karwa dete the.
        if os.getppid() != parent or (seen_state and not os.path.exists(a.state)
                                       and now - last_seen > 4.0):
            return
        if os.path.exists(a.state):
            seen_state, last_seen = True, now
        # ~8 renders/sec (frame ~10ms) : smooth neon animation, light on CPU.
        k = (json.dumps({kk: vv for kk, vv in st.items() if kk != "_read_at"},
                        sort_keys=True, default=str), int(now * 8))
        if k != key:
            try:
                st["_fps"] = fps
                frame = rgb_to_i420(r.render(st, now, started))
            except Exception as exc:  # never kill the screen share on a bad frame
                print(f"telegram_live_screen render error: {exc!r}", file=sys.stderr)
                if frame is None:
                    frame = rgb_to_i420(Image.new("RGB", (w, h), BG))
            key = k
        try:
            out.write(frame)
            out.flush()
        except (BrokenPipeError, OSError):
            return
        n += 1
        sl = t0 + n / fps - time.time()
        if sl > 0:
            time.sleep(sl)
        elif sl < -1.0 / fps:
            # Fell behind: never burst-catch-up (that stalls audio). Re-sync.
            t0, n = time.time(), 0


if __name__ == "__main__":
    main()
