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
BG = (21, 27, 36)
PANEL = (30, 37, 48)
PANEL2 = (38, 46, 59)
ROW_HI = (44, 54, 69)
LINE = (52, 62, 78)
TEXT = (255, 255, 255)
SUB = (139, 152, 170)
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

    def background(self, photo, name):
        """Main user's DP, blurred + darkened, as the whole window background."""
        try:
            mt = os.path.getmtime(photo) if photo else 0
        except Exception:
            mt, photo = 0, ""
        key = (photo, mt, name)
        if key == self._bg_key and self._bg is not None:
            return self._bg
        W, H = self.w, self.h
        sq = self._load_square(photo) if photo else None
        if sq is not None:
            side = max(W, H)
            bg = sq.resize((side // 4, side // 4), Image.BILINEAR)
            bg = bg.filter(ImageFilter.GaussianBlur(10))
            bg = bg.resize((side, side), Image.BILINEAR)
            bg = bg.crop(((side - W) // 2, (side - H) // 2,
                          (side - W) // 2 + W, (side - H) // 2 + H))
            bg = Image.blend(bg, Image.new("RGB", (W, H), BG), 0.72)
        else:
            c = _color_for(name)
            bg = Image.new("RGB", (W, H), BG)
            g = Image.new("RGB", (W, H), _mix(BG, c, 0.35))
            m = Image.new("L", (W, H), 0)
            ImageDraw.Draw(m).ellipse([W * 0.05, -H * 0.4, W * 0.75, H * 0.9], fill=180)
            m = m.filter(ImageFilter.GaussianBlur(self.S(140)))
            bg.paste(g, (0, 0), m)
        # bottom/top vignette for readability
        v = Image.new("L", (1, H), 0)
        for y in range(H):
            t = abs(y / H - 0.45) / 0.55
            v.putpixel((0, y), int(150 * max(0.0, t) ** 1.6))
        bg.paste(Image.new("RGB", (W, H), (8, 11, 16)), (0, 0), v.resize((W, H)))
        # huge watermark name in the display font
        d = ImageDraw.Draw(bg)
        wm = clean_name(name, "")
        if wm:
            text(d, (int(W * 0.345), self.S(108)), wm, style="display", size=self.S(62),
                 fill=_mix(BG, (255, 255, 255), 0.09), anchor="mt",
                 max_w=self.w * 0.64)
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
        parts = list(st.get("participants") or [])
        me_p = next((p for p in parts if p.get("is_me")), None)
        me_name = clean_name(st.get("me_name") or (me_p or {}).get("name")
                             or (("@" + st["me_user"]) if st.get("me_user") else ""), "You")
        me_photo = st.get("me_photo") or (me_p or {}).get("photo") or ""
        mic_on = bool(st.get("mic_on"))
        playing = bool(st.get("now_playing")) and not st.get("paused")
        audio = bool(st.get("audio_active")) or playing
        me_speaking = (mic_on and audio) or bool(me_p and me_p.get("speaking"))
        me_muted = bool(st.get("ghost_mute")) or (not mic_on and not audio
                                                    and bool(me_p and me_p.get("muted")))

        img = self.background(me_photo, me_name).copy()
        d = ImageDraw.Draw(img)

        panel_x = int(W * 0.69)
        # ================= header
        title = clean_name(st.get("group") or st.get("title") or "Voice Chat", "Voice Chat")
        text(d, (S(32), S(30)), title, style="bold", size=S(26), fill=TEXT,
             max_w=panel_x - S(260))
        n = len(parts) or 1
        sub = f"{n} participant" + ("" if n == 1 else "s")
        if st.get("title"):
            sub += "  ·  " + str(st.get("title"))
        text(d, (S(32), S(66)), sub, size=S(17), fill=SUB, max_w=panel_x - S(260))
        # LIVE pill + timer
        lx = panel_x - S(28)
        tm = _clock(now - started)
        tw = text_w(tm, "bold", S(17))
        pw = tw + S(96)
        d.rounded_rectangle([lx - pw, S(30), lx, S(62)], radius=S(16), fill=(0, 0, 0))
        blink = 0.55 + 0.45 * math.sin(now * 4)
        d.ellipse([lx - pw + S(14), S(40), lx - pw + S(26), S(52)], fill=_mix(BG, RED, blink))
        text(d, (lx - pw + S(32), S(46)), "LIVE", style="bold", size=S(14), fill=TEXT, anchor="lm")
        text(d, (lx - S(14), S(46)), tm, style="bold", size=S(17), fill=SUB, anchor="rm")

        # ================= centre stage (main user)
        cx = panel_x // 2
        cy = int(H * 0.40)
        r = S(112)
        energy = 0.0
        if me_speaking:
            energy = 0.55 + 0.45 * abs(math.sin(now * 2.3)) * (0.6 + 0.4 * math.sin(now * 5.7))
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ring_col = GREEN if me_speaking else (GREY if me_muted else BLUE)
        self.waves(layer, cx, cy, r, ring_col, now, energy if me_speaking else 0.15)
        layer = layer.filter(ImageFilter.GaussianBlur(S(6)))
        img.paste(layer, (0, 0), layer)
        d = ImageDraw.Draw(img)
        d.ellipse([cx - r - S(4), cy - r - S(4), cx + r + S(4), cy + r + S(4)], outline=ring_col,
                  width=S(4))
        av = self.avatar(me_photo, 2 * r, me_name, key=st.get("me_user") or me_name)
        img.paste(av, (cx - r, cy - r), av)
        # name (display font) + status
        ny = cy + r + S(30)
        text(d, (cx, ny + S(2)), me_name, style="display", size=S(40),
             fill=(0, 0, 0), anchor="mt", max_w=panel_x - S(80))
        text(d, (cx, ny), me_name, style="display", size=S(40), fill=TEXT, anchor="mt",
             max_w=panel_x - S(80))
        handle = ("@" + st["me_user"]) if st.get("me_user") else ""
        if me_speaking:
            stat, sc = "speaking", GREEN
        elif me_muted:
            stat, sc = "muted", SUB
        elif mic_on:
            stat, sc = "mic on", BLUE
        else:
            stat, sc = "listening", BLUE
        line = f"{handle}  ·  {stat}" if handle else stat
        text(d, (cx, ny + S(58)), line, style="ui", size=S(19), fill=sc, anchor="mt",
             max_w=panel_x - S(80))

        # now playing pill
        npn = st.get("now_playing") or ""
        if npn:
            pw2 = min(panel_x - S(120), S(520))
            px0, py0 = cx - pw2 // 2, ny + S(96)
            d.rounded_rectangle([px0, py0, px0 + pw2, py0 + S(46)], radius=S(23), fill=PANEL2)
            icon = "❚❚" if st.get("paused") else "♪"
            text(d, (px0 + S(24), py0 + S(23)), icon, style="bold", size=S(18), fill=BLUE, anchor="mm")
            text(d, (px0 + S(44), py0 + S(16)), npn, style="bold", size=S(16), fill=TEXT,
                 anchor="lm", max_w=pw2 - S(130))
            dur = float(st.get("duration") or 0)
            pos = float(st.get("position") or 0)
            if not st.get("paused") and st.get("_read_at"):
                pos += max(0.0, now - float(st["_read_at"]))
            frac = (pos / dur) if dur > 0 else 0.0
            frac = max(0.0, min(1.0, frac))
            bx0, bx1, by = px0 + S(44), px0 + pw2 - S(90), py0 + S(32)
            d.rounded_rectangle([bx0, by, bx1, by + S(4)], radius=S(2), fill=LINE)
            d.rounded_rectangle([bx0, by, bx0 + max(S(4), int((bx1 - bx0) * frac)), by + S(4)],
                                radius=S(2), fill=BLUE)
            t2 = _clock(pos) + (f" / {_clock(dur)}" if dur > 0 else "")
            text(d, (px0 + pw2 - S(18), py0 + S(23)), t2, size=S(13), fill=SUB, anchor="rm")

        # ================= bottom control bar (Telegram style)
        by = H - S(82)
        # Screen (active)
        sx = cx - S(150)
        d.ellipse([sx - S(30), by - S(30), sx + S(30), by + S(30)], fill=BLUE)
        self.screen_icon(d, sx, by, S(30), TEXT)
        text(d, (sx, by + S(40)), "screen", size=S(14), fill=SUB, anchor="mt")
        # Leave
        rx = cx + S(150)
        d.ellipse([rx - S(30), by - S(30), rx + S(30), by + S(30)], fill=RED)
        self.leave_icon(d, rx, by, S(30), TEXT)
        text(d, (rx, by + S(40)), "leave", size=S(14), fill=SUB, anchor="mt")
        # Big mic button with the main user's DP behind the icon
        mr = S(48)
        mcol = GREEN if me_speaking else (GREY if me_muted else BLUE)
        glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        gr = mr + S(10) + int(S(10) * energy)
        ImageDraw.Draw(glow).ellipse([cx - gr, by - S(8) - gr, cx + gr, by - S(8) + gr],
                                     fill=mcol + (140,))
        glow = glow.filter(ImageFilter.GaussianBlur(S(10)))
        img.paste(glow, (0, 0), glow)
        d = ImageDraw.Draw(img)
        mav = self.avatar(me_photo, 2 * mr, me_name, key=st.get("me_user") or me_name)
        img.paste(mav, (cx - mr, by - S(8) - mr), mav)
        tint = Image.new("RGBA", (2 * mr, 2 * mr), (0, 0, 0, 0))
        ImageDraw.Draw(tint).ellipse([0, 0, 2 * mr - 1, 2 * mr - 1], fill=mcol + (150,))
        img.paste(tint, (cx - mr, by - S(8) - mr), tint)
        d = ImageDraw.Draw(img)
        d.ellipse([cx - mr, by - S(8) - mr, cx + mr, by - S(8) + mr], outline=TEXT, width=S(3))
        self.mic_icon(d, cx, by - S(8), S(44), TEXT, muted=me_muted, width=S(4))
        label = "speaking" if me_speaking else ("muted" if me_muted else "unmute" if not mic_on else "live")
        text(d, (cx, by + mr + S(4)), label, style="bold", size=S(15), fill=TEXT, anchor="mt")

        # ================= participants panel
        self.panel(d, img, st, parts, me_name, me_photo, panel_x, now)

        # clock
        clk = datetime.now(_tz()).strftime("%I:%M %p").lstrip("0")
        text(d, (W - S(28), S(46)), clk, style="bold", size=S(17), fill=SUB, anchor="rm")
        return img

    def panel(self, d, img, st, parts, me_name, me_photo, x0, now):
        W, H, S = self.w, self.h, self.S
        px0, py0, px1, py1 = x0, S(90), W - S(20), H - S(20)
        ov = Image.new("RGBA", (px1 - px0, py1 - py0), (0, 0, 0, 0))
        ImageDraw.Draw(ov).rounded_rectangle([0, 0, px1 - px0 - 1, py1 - py0 - 1],
                                             radius=S(18), fill=PANEL + (232,))
        img.paste(ov, (px0, py0), ov)
        d = ImageDraw.Draw(img)
        text(d, (px0 + S(20), py0 + S(30)), "Participants", style="bold", size=S(19),
             fill=TEXT, anchor="lm")
        n = len(parts)
        text(d, (px1 - S(20), py0 + S(30)), str(n), style="bold", size=S(16), fill=SUB, anchor="rm")
        d.line([px0 + S(16), py0 + S(56), px1 - S(16), py0 + S(56)], fill=LINE, width=1)

        def order(p):
            if p.get("is_me"):
                return (0, 0)
            if p.get("speaking"):
                return (1, 0)
            if p.get("hand"):
                return (2, 0)
            return (3 if not p.get("muted") else 4, 0)

        rows = sorted(parts, key=order)
        if not rows:
            rows = [{"name": me_name, "photo": me_photo, "is_me": True}]
        row_h = S(62)
        y = py0 + S(66)
        max_rows = max(1, (py1 - y - S(10)) // row_h)
        more = 0
        if len(rows) > max_rows:
            more = len(rows) - (max_rows - 1)
            rows = rows[:max_rows - 1]
        avs = S(44)
        for p in rows:
            is_me = bool(p.get("is_me"))
            nm = clean_name(me_name if is_me else p.get("name"), "Deleted Account")
            spk = bool(p.get("speaking")) or (is_me and bool(st.get("mic_on")) and
                                             (bool(st.get("audio_active")) or bool(st.get("now_playing"))))
            muted = bool(p.get("muted")) and not spk
            if spk:
                d.rounded_rectangle([px0 + S(8), y, px1 - S(8), y + row_h - S(4)], radius=S(12),
                                    fill=ROW_HI)
            ax, ay = px0 + S(18), y + (row_h - S(4) - avs) // 2
            if spk:
                pulse = 0.5 + 0.5 * math.sin(now * 5)
                rr = avs // 2 + S(3) + int(S(2) * pulse)
                c = (ax + avs // 2, ay + avs // 2)
                d.ellipse([c[0] - rr, c[1] - rr, c[0] + rr, c[1] + rr], outline=GREEN, width=S(2))
            av = self.avatar(p.get("photo") or (me_photo if is_me else ""), avs, nm, key=p.get("uid") or nm)
            img.paste(av, (ax, ay), av)
            d = ImageDraw.Draw(img)
            tx = ax + avs + S(14)
            name_max = px1 - tx - S(56)
            suffix = "  (you)" if is_me else ""
            wname = text(d, (tx, y + S(20)), nm, style="bold", size=S(17), fill=TEXT, anchor="lm",
                         max_w=name_max - (text_w(suffix, "ui", S(14)) if suffix else 0))
            if suffix:
                text(d, (tx + wname, y + S(20)), suffix, size=S(14), fill=SUB, anchor="lm")
            if spk:
                s, c = "speaking", GREEN
            elif p.get("hand"):
                s, c = "✋ wants to speak", ORANGE
            elif muted:
                s, c = "muted", SUB
            else:
                s, c = "listening", BLUE
            vol = int(p.get("volume") or 100)
            if vol != 100 and not is_me:
                s += f"  ·  {vol}%"
            text(d, (tx, y + S(42)), s, size=S(14), fill=c, anchor="lm", max_w=name_max)
            ic = GREEN if spk else (GREY if muted else BLUE)
            self.mic_icon(d, px1 - S(32), y + (row_h - S(4)) // 2, S(24), ic, muted=muted)
            y += row_h
        if more:
            text(d, ((px0 + px1) // 2, y + row_h // 2 - S(4)), f"+ {more} more listening",
                 style="bold", size=S(15), fill=SUB, anchor="mm")


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
        # ~3 renders/sec: smooth enough for waves + timer, light on CPU.
        k = (json.dumps({kk: vv for kk, vv in st.items() if kk != "_read_at"},
                        sort_keys=True, default=str), int(now * 3))
        if k != key:
            try:
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
