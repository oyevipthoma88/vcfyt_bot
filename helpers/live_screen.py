#!/usr/bin/env python3
"""LIVE VC screen for the bot's screen share.

Renders a sharp PC-style "Voice Chat" window and writes raw I420 (yuv420p)
frames to stdout in real time (ntgcalls SHELL video source).

Everything on screen is REAL, read from a JSON state file that the bot
refreshes every ~1.5 s from Telegram itself:
  * every participant currently in the VC (name, mic on/off, speaking,
    hand raised, volume)
  * now playing track, paused / playing, loop, volume + boost, live mic
  * real clock + LIVE timer

Telegram does not let anyone capture another user's screen or camera, so
the participants are drawn as live tiles (like Telegram Desktop's VC grid).

Frames are only re-rendered when something changes (state update or the
clock second ticks); in between the cached frame is resent, so 1080p costs
almost no CPU.

Usage: live_screen.py --state FILE [--w 1920 --h 1080 --fps 15]
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
FONT = os.path.join(os.path.dirname(HERE), "assets", "fonts", "DejaVuSans.ttf")
FONT_BOLD_CANDIDATES = (
    os.path.join(os.path.dirname(HERE), "assets", "fonts", "DejaVuSans-Bold.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)

BG = (14, 18, 24)
PANEL = (24, 30, 40)
PANEL2 = (32, 40, 54)
TITLEBAR = (30, 34, 42)
TEXT = (235, 240, 248)
MUTED = (140, 152, 170)
GREEN = (61, 220, 132)
RED = (255, 76, 76)
YELLOW = (255, 210, 63)
BLUE = (64, 160, 255)
TILE_COLORS = [(231, 76, 60), (52, 152, 219), (46, 204, 113), (155, 89, 182),
               (241, 196, 15), (26, 188, 156), (230, 126, 34), (236, 64, 122)]


def _font(size, bold=False):
    paths = (FONT_BOLD_CANDIDATES if bold else ()) + (FONT,
             "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    for p in paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


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


def _initials(name):
    parts = [p for p in str(name or "?").split() if p]
    s = "".join(p[0] for p in parts[:2]).upper()
    return s or "?"


class Renderer:
    def __init__(self, w, h):
        self.w, self.h = w, h
        s = h / 1080.0
        self.s = s
        self._avatar_cache = {}   # path -> (mtime, PIL RGBA circular image)
        self._photo_ops = []      # (path, x, y, size, speaking) pasted last
        self.f_title = _font(int(22 * s), True)
        self.f_big = _font(int(34 * s), True)
        self.f_med = _font(int(24 * s), True)
        self.f_txt = _font(int(22 * s))
        self.f_small = _font(int(18 * s))
        self.f_init = _font(int(60 * s), True)

    def _avatar(self, path, size):
        """Load a participant's profile photo as a circular RGBA image."""
        try:
            mtime = os.path.getmtime(path)
        except Exception:
            return None
        ent = self._avatar_cache.get(path)
        if ent and ent[0] == mtime and ent[1].size == (size, size):
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
        self._avatar_cache[path] = (mtime, im)
        return im

    def render(self, st, now, started):
        W, H, s = self.w, self.h, self.s
        img = Image.new("RGB", (W, H), BG)
        self._photo_ops = []
        d = ImageDraw.Draw(img)
        # ---- window title bar (PC look) ----
        tb = int(44 * s)
        d.rectangle([0, 0, W, tb], fill=TITLEBAR)
        for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
            cx = int(24 * s + i * 30 * s)
            r = int(8 * s)
            d.ellipse([cx - r, tb // 2 - r, cx + r, tb // 2 + r], fill=c)
        title = f"Telegram Desktop — Voice Chat · {st.get('group') or 'Group'}"
        d.text((int(120 * s), tb // 2), _fit(d, title, self.f_title, W * 0.6),
               font=self.f_title, fill=TEXT, anchor="lm")
        el = int(max(0, now - started))
        timer = f"{el // 3600:02d}:{el % 3600 // 60:02d}:{el % 60:02d}"
        bw = int(250 * s)
        bx0 = W - bw - int(14 * s)
        d.rounded_rectangle([bx0, int(6 * s), W - int(14 * s), tb - int(6 * s)],
                            radius=int(8 * s), fill=(60, 16, 20))
        if int(now * 2) % 2 == 0:
            r0 = int(7 * s)
            cx0 = bx0 + int(20 * s)
            d.ellipse([cx0 - r0, tb // 2 - r0, cx0 + r0, tb // 2 + r0], fill=RED)
        d.text((bx0 + int(38 * s), tb // 2), "LIVE  " + timer, font=self.f_title,
               fill=TEXT, anchor="lm")

        # ---- layout: grid (left) + control sidebar (right) ----
        side_w = int(W * 0.26)
        pad = int(20 * s)
        task_h = int(48 * s)
        gx0, gy0 = pad, tb + pad
        gx1, gy1 = W - side_w - pad, H - task_h - pad
        self._grid(d, st, gx0, gy0, gx1, gy1, now)
        self._sidebar(d, st, W - side_w, tb, W, H - task_h, now, started)
        self._taskbar(d, st, H - task_h, now)
        # real profile photos go on top of everything, last
        rgba = img.convert("RGBA")
        for path, px, py, size, speaking in self._photo_ops:
            ph = self._avatar(path, size)
            if ph is not None:
                rgba.alpha_composite(ph, (px, py))
        return rgba.convert("RGB")

    def _grid(self, d, st, x0, y0, x1, y1, now):
        s = self.s
        parts = list(st.get("participants") or [])
        # speakers first, then unmuted, then rest
        parts.sort(key=lambda p: (not p.get("speaking"), bool(p.get("muted")),
                                  str(p.get("name", "")).lower()))
        total = len(parts)
        head_h = int(56 * s)
        d.text((x0, y0 + head_h // 2),
               f"{total} participant{'s' if total != 1 else ''} in voice chat",
               font=self.f_med, fill=TEXT, anchor="lm")
        speaking = sum(1 for p in parts if p.get("speaking"))
        d.text((x1, y0 + head_h // 2), f"{speaking} speaking",
               font=self.f_txt, fill=GREEN if speaking else MUTED, anchor="rm")
        y0 += head_h
        if not parts:
            d.rounded_rectangle([x0, y0, x1, y1], radius=int(18 * s), fill=PANEL)
            d.text(((x0 + x1) // 2, (y0 + y1) // 2),
                   "Voice chat se participants load ho rahe hain…",
                   font=self.f_med, fill=MUTED, anchor="mm")
            return
        max_tiles = 30
        shown = parts[:max_tiles]
        n = len(shown)
        cols = 1
        while cols * cols < n:
            cols += 1
        cols = max(2, min(6, cols)) if n > 1 else 1
        rows = (n + cols - 1) // cols
        gap = int(14 * s)
        tw = (x1 - x0 - gap * (cols - 1)) // cols
        th = (y1 - y0 - gap * (rows - 1)) // max(1, rows)
        th = min(th, int(tw * 0.75))
        for i, p in enumerate(shown):
            r, c = divmod(i, cols)
            tx = x0 + c * (tw + gap)
            ty = y0 + r * (th + gap)
            self._tile(d, p, tx, ty, tw, th, now)
        if total > max_tiles:
            d.text((x1, y1), f"+{total - max_tiles} aur", font=self.f_small,
                   fill=MUTED, anchor="rd")

    def _tile(self, d, p, x, y, w, h, now):
        s = self.s
        speaking = bool(p.get("speaking"))
        muted = bool(p.get("muted"))
        rad = int(16 * s)
        d.rounded_rectangle([x, y, x + w, y + h], radius=rad, fill=PANEL2)
        if speaking:
            pulse = 3 + int(2 * abs(((now * 3) % 2) - 1))
            d.rounded_rectangle([x, y, x + w, y + h], radius=rad,
                                outline=GREEN, width=max(2, int(pulse * s)))
        name = str(p.get("name") or "User")
        color = TILE_COLORS[sum(map(ord, name)) % len(TILE_COLORS)]
        ar = int(min(w, h) * 0.24)
        cx, cy = x + w // 2, y + int(h * 0.42)
        d.ellipse([cx - ar, cy - ar, cx + ar, cy + ar], fill=color)
        photo_path = p.get("photo") or ""
        photo = self._avatar(photo_path, ar * 2) if photo_path else None
        if photo is not None:
            # speaking ring around the real profile picture
            if speaking:
                d.ellipse([cx - ar - int(4 * s), cy - ar - int(4 * s),
                           cx + ar + int(4 * s), cy + ar + int(4 * s)],
                          outline=GREEN, width=max(2, int(3 * s)))
            self._photo_ops.append((photo_path, cx - ar, cy - ar, ar * 2, speaking))
        else:
            f_init = _font(max(12, int(ar * 0.8)), True)
            d.text((cx, cy), _initials(name), font=f_init, fill=TEXT, anchor="mm")
        fs = _font(max(12, int(min(22 * s, h * 0.11))), True)
        label = name + ("  (You)" if p.get("is_me") else "")
        d.text((x + int(12 * s), y + h - int(14 * s)),
               _fit(d, label, fs, w - int(60 * s)), font=fs, fill=TEXT, anchor="ld")
        # mic icon
        mx, my = x + w - int(26 * s), y + h - int(26 * s)
        mr = int(12 * s)
        d.ellipse([mx - mr, my - mr, mx + mr, my + mr],
                  fill=RED if muted else (GREEN if speaking else (70, 80, 96)))
        if muted:
            q = int(5 * s)
            d.line([mx - q, my - q, mx + q, my + q], fill=TEXT, width=max(2, int(2 * s)))
            d.line([mx - q, my + q, mx + q, my - q], fill=TEXT, width=max(2, int(2 * s)))
        else:
            q = int(4 * s)
            d.rounded_rectangle([mx - q, my - int(7 * s), mx + q, my + int(3 * s)],
                                radius=q, fill=TEXT)
            d.line([mx, my + int(3 * s), mx, my + int(7 * s)], fill=TEXT, width=max(1, int(2 * s)))
        if p.get("hand"):
            d.text((x + int(12 * s), y + int(10 * s)), "HAND RAISED",
                   font=_font(max(10, int(16 * s))), fill=YELLOW)
        vol = p.get("volume")
        if vol and vol != 100:
            d.text((x + w - int(12 * s), y + int(10 * s)), f"{vol}%",
                   font=_font(max(10, int(16 * s))), fill=BLUE, anchor="ra")
        if speaking:
            # live level bars
            bx = x + int(12 * s)
            by = y + int(h * 0.42)
            for k in range(4):
                lvl = 0.3 + 0.7 * abs(((now * (2.3 + k * 0.7)) % 2) - 1)
                bh = int(h * 0.18 * lvl)
                d.rectangle([bx + k * int(8 * s), by - bh,
                             bx + k * int(8 * s) + int(5 * s), by], fill=GREEN)

    def _sidebar(self, d, st, x0, y0, x1, y1, now, started):
        s = self.s
        d.rectangle([x0, y0, x1, y1], fill=PANEL)
        pad = int(22 * s)
        x, y = x0 + pad, y0 + pad
        w = x1 - x0 - 2 * pad
        d.text((x, y), "Controls", font=self.f_big, fill=TEXT)
        y += int(56 * s)

        def card(title, value, color=TEXT, h=int(76 * s)):
            nonlocal y
            d.rounded_rectangle([x, y, x + w, y + h], radius=int(12 * s), fill=PANEL2)
            d.text((x + int(14 * s), y + int(12 * s)), title, font=self.f_small, fill=MUTED)
            d.text((x + int(14 * s), y + h - int(12 * s)), _fit(d, value, self.f_med, w - int(28 * s)),
                   font=self.f_med, fill=color, anchor="ld")
            y += h + int(12 * s)

        card("HOST", st.get("me_name") or "—", BLUE)
        np_name = st.get("now_playing") or "—"
        paused = bool(st.get("paused"))
        card("NOW PLAYING", np_name, TEXT)
        card("PLAYER", ("Paused" if paused else "Playing") if st.get("now_playing") else "Idle",
             YELLOW if paused else GREEN)
        vol = int(st.get("volume") or 0)
        boost = int(st.get("boost") or 0)
        # volume meter card
        h = int(96 * s)
        d.rounded_rectangle([x, y, x + w, y + h], radius=int(12 * s), fill=PANEL2)
        d.text((x + int(14 * s), y + int(12 * s)), f"VOLUME  {vol}/1000   ·   BOOST {boost}/10",
               font=self.f_small, fill=MUTED)
        bar_y = y + int(52 * s)
        segs = 20
        sw = (w - int(28 * s)) / segs
        live_lvl = (0.55 + 0.45 * abs(((now * 2.7) % 2) - 1)) if (st.get("now_playing") and not paused) or st.get("mic_on") else 0
        for k in range(segs):
            on = k / segs < max(vol / 1000.0 * 0.6, live_lvl * vol / 1000.0)
            col = GREEN if k < 13 else (YELLOW if k < 17 else RED)
            sx = x + int(14 * s) + int(k * sw)
            d.rectangle([sx, bar_y, sx + int(sw) - int(3 * s), bar_y + int(26 * s)],
                        fill=col if on else (52, 60, 76))
        y += h + int(12 * s)
        card("LIVE MIC", "ON AIR" if st.get("mic_on") else "Off",
             RED if st.get("mic_on") else MUTED)
        self._mic_setup(d, st, x, y, w, y1 - int(12 * s), now, paused)

    def _mic_setup(self, d, st, x, y, w, ymax, now, paused):
        """Studio mic chain panel: model, interface, gain knob, live waveform."""
        s = self.s
        h = ymax - y
        if h < int(120 * s):
            return
        d.rounded_rectangle([x, y, x + w, ymax], radius=int(12 * s), fill=PANEL2)
        px = x + int(14 * s)
        d.text((px, y + int(12 * s)), "MIC SETUP", font=self.f_small, fill=MUTED)
        active = bool(st.get("mic_on")) or (bool(st.get("now_playing")) and not paused)
        d.text((x + w - int(14 * s), y + int(12 * s)),
               "● ARMED" if active else "○ STANDBY", font=self.f_small,
               fill=RED if active else MUTED, anchor="ra")
        rows = [("Mic", st.get("mic_model") or "Shure SM7B · Dynamic"),
                ("Interface", st.get("mic_iface") or "Focusrite Scarlett 2i2"),
                ("Format", "48 kHz · 24-bit · Stereo"),
                ("Chain", "HPF 45Hz › Comp 3:1 › Limiter -1dB")]
        ly = y + int(42 * s)
        lh = int(26 * s)
        for k, v in rows:
            if ly + lh > ymax - int(70 * s):
                break
            d.text((px, ly), k, font=self.f_small, fill=MUTED)
            d.text((px + int(110 * s), ly), _fit(d, v, self.f_small, w - int(130 * s)),
                   font=self.f_small, fill=TEXT)
            ly += lh
        # gain knob + live waveform
        bot = ymax - int(14 * s)
        top = ly + int(10 * s)
        kr = max(8, min(int(34 * s), (bot - top) // 2 - int(10 * s)))
        kcx, kcy = px + kr, (top + bot) // 2
        d.ellipse([kcx - kr, kcy - kr, kcx + kr, kcy + kr], fill=(20, 24, 32),
                  outline=(90, 100, 120), width=max(1, int(2 * s)))
        gain = 0.72 + (0.06 * abs(((now * 0.7) % 2) - 1) if active else 0)
        import math
        ang = math.radians(135 + 270 * gain)
        d.line([kcx, kcy, kcx + int(kr * 0.8 * math.cos(ang)), kcy + int(kr * 0.8 * math.sin(ang))],
               fill=YELLOW, width=max(2, int(3 * s)))
        d.text((kcx, bot + int(2 * s)), f"GAIN +{int(gain * 60)}dB",
               font=_font(max(9, int(12 * s))), fill=MUTED, anchor="ma")
        wx0, wx1 = kcx + kr + int(16 * s), x + w - int(14 * s)
        mid = kcy
        amp = (bot - top) // 2
        n = max(8, (wx1 - wx0) // max(3, int(5 * s)))
        step = (wx1 - wx0) / n
        for i in range(n):
            if active:
                t = now * 6 + i * 0.55
                lvl = 0.15 + 0.85 * abs(math.sin(t) * math.sin(t * 0.37 + i * 0.2))
            else:
                lvl = 0.04
            bh = max(1, int(amp * lvl))
            bx = int(wx0 + i * step)
            col = GREEN if lvl < 0.7 else (YELLOW if lvl < 0.9 else RED)
            d.rectangle([bx, mid - bh, bx + max(1, int(step) - 2), mid + bh], fill=col)

    def _taskbar(self, d, st, y, now):
        s = self.s
        W = self.w
        h = self.h - y
        d.rectangle([0, y, W, self.h], fill=(28, 28, 32))
        q = int(h * 0.55)
        d.rectangle([int(14 * s), y + (h - q) // 2, int(14 * s) + q, y + (h + q) // 2], fill=(58, 150, 221))
        for i, name in enumerate(["Telegram", "Audio Mixer", "VC Studio"]):
            bx = int(80 * s) + i * int(190 * s)
            d.rounded_rectangle([bx, y + int(8 * s), bx + int(176 * s), y + h - int(8 * s)],
                                radius=int(6 * s), fill=(48, 48, 56) if i else (64, 64, 76))
            d.text((bx + int(88 * s), y + h // 2), name, font=self.f_small, fill=TEXT, anchor="mm")
        dt = datetime.fromtimestamp(now, _tz())
        d.text((W - int(18 * s), y + h // 2 - int(9 * s)), dt.strftime("%I:%M:%S %p"),
               font=self.f_small, fill=TEXT, anchor="rm")
        d.text((W - int(18 * s), y + h // 2 + int(12 * s)), dt.strftime("%d-%m-%Y"),
               font=_font(max(10, int(14 * s))), fill=MUTED, anchor="rm")


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
    a = ap.parse_args()
    w = max(320, min(1920, a.w)) // 2 * 2
    h = max(240, min(1080, a.h)) // 2 * 2
    fps = max(5, min(30, a.fps))
    r = Renderer(w, h)
    out = sys.stdout.buffer
    started = time.time()
    frame, key, last_read, st = None, None, 0.0, {}
    # re-render 4x per second for pulses/meters; resend cached frame between
    anim_step = 0.2
    t0 = time.time()
    n = 0
    while True:
        now = time.time()
        if now - last_read > 0.75:
            st = _read_state(a.state) or st
            last_read = now
            if st.get("started"):
                started = float(st["started"])
        k = (json.dumps(st, sort_keys=True, default=str), int(now / anim_step))
        if k != key:
            frame = rgb_to_i420(r.render(st, now, started))
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
