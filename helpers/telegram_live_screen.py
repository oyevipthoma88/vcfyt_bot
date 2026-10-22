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
BLUE    = (100, 181, 239)
BLUE_D  = (51, 144, 236)
GREEN   = (79, 174, 78)
RED     = (229, 57, 53)
YELLOW  = (255, 210, 63)

AV_COLORS = [(231,76,60),(52,152,219),(46,204,113),(155,89,182),
             (241,196,15),(26,188,156),(230,126,34),(236,64,122)]


def _font(size, bold=False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for base in (FDIR, "/usr/share/fonts/truetype/dejavu"):
        p = os.path.join(base, name)
        if os.path.exists(p):
            try: return ImageFont.truetype(p, max(8, int(size)))
            except Exception: pass
    return ImageFont.load_default()


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


def _hms(sec):
    sec = int(max(0, sec))
    return f"{sec//3600:02d}:{sec%3600//60:02d}:{sec%60:02d}"


class Renderer:
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.s = h / 720.0
        self._av = {}
        self._ops = []
        s = self.s
        self.f_title = _font(19*s, True)
        self.f_sub   = _font(12*s)
        self.f_name  = _font(15*s, True)
        self.f_small = _font(11*s)
        self.f_tiny  = _font(10*s)
        self.f_clock = _font(24*s, True)
        self.f_date  = _font(10*s)
        self.f_big   = _font(22*s, True)

    def _avatar(self, path, size):
        try: mt = os.path.getmtime(path)
        except Exception: return None
        ent = self._av.get(path)
        if ent and ent[0] == mt and ent[1].size == (size, size): return ent[1]
        try:
            im = Image.open(path).convert("RGB")
            w0, h0 = im.size; side = min(w0, h0)
            im = im.crop(((w0-side)//2,(h0-side)//2,(w0-side)//2+side,(h0-side)//2+side))
            im = im.resize((size, size), Image.LANCZOS)
            mask = Image.new("L", (size*4, size*4), 0)
            ImageDraw.Draw(mask).ellipse([0,0,size*4,size*4], fill=255)
            mask = mask.resize((size, size), Image.LANCZOS)
            im.putalpha(mask)
        except Exception: return None
        self._av[path] = (mt, im)
        return im

    def _ic_telegram(self, d, cx, cy, r):
        d.ellipse([cx-r, cy-r, cx+r, cy+r], fill=BLUE_D)
        p1 = (cx - int(r*0.55), cy + int(r*0.05))
        p2 = (cx + int(r*0.65), cy - int(r*0.35))
        p3 = (cx + int(r*0.15), cy + int(r*0.45))
        p4 = (cx - int(r*0.10), cy + int(r*0.10))
        d.polygon([p1, p2, p3, p4], fill=TEXT)
        d.polygon([(cx + int(r*0.15), cy + int(r*0.45)),
                   (cx + int(r*0.15), cy + int(r*0.20)),
                   (cx + int(r*0.30), cy + int(r*0.30))], fill=BLUE_D)

    def _ic_mic(self, d, cx, cy, r, col, muted=False):
        q = max(2, int(r*0.32))
        d.rounded_rectangle([cx-q, cy-int(r*0.5), cx+q, cy+int(r*0.15)],
                            radius=q, fill=col)
        w2 = max(1, int(r*0.10))
        d.arc([cx-int(r*0.42), cy-int(r*0.35), cx+int(r*0.42), cy+int(r*0.4)],
              start=0, end=180, fill=col, width=w2)
        d.line([cx, cy+int(r*0.4), cx, cy+int(r*0.55)], fill=col, width=w2)
        if muted:
            d.line([cx-int(r*0.55), cy-int(r*0.55), cx+int(r*0.55), cy+int(r*0.55)],
                   fill=col, width=w2+1)

    def _ic_cam(self, d, cx, cy, r, col):
        w2 = max(1, int(r*0.10))
        d.rounded_rectangle([cx-int(r*0.55), cy-int(r*0.32),
                             cx+int(r*0.2), cy+int(r*0.32)],
                            radius=int(r*0.15), outline=col, width=w2)
        d.polygon([(cx+int(r*0.25), cy-int(r*0.15)),
                   (cx+int(r*0.6), cy-int(r*0.4)),
                   (cx+int(r*0.6), cy+int(r*0.4)),
                   (cx+int(r*0.25), cy+int(r*0.15))], fill=col)

    def _ic_screen(self, d, cx, cy, r, col):
        w2 = max(1, int(r*0.10))
        d.rounded_rectangle([cx-int(r*0.5), cy-int(r*0.4),
                             cx+int(r*0.5), cy+int(r*0.2)],
                            radius=int(r*0.08), outline=col, width=w2)
        d.line([cx, cy+int(r*0.2), cx, cy+int(r*0.4)], fill=col, width=w2)
        d.line([cx-int(r*0.3), cy+int(r*0.42), cx+int(r*0.3), cy+int(r*0.42)],
               fill=col, width=w2)

    def _ic_gear(self, d, cx, cy, r, col):
        pts = []
        teeth = 8
        for i in range(teeth*2):
            a = (i/(teeth*2)) * 2 * math.pi - math.pi/2
            rr = r*0.55 if i%2==0 else r*0.36
            pts.append((cx+rr*math.cos(a), cy+rr*math.sin(a)))
        d.polygon(pts, fill=col)
        d.ellipse([cx-int(r*0.22), cy-int(r*0.22), cx+int(r*0.22), cy+int(r*0.22)],
                  fill=BG2)

    def render(self, st, now, started):
        W, H, s = self.w, self.h, self.s
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        self._ops = []
        hh = int(60*s)
        bh = int(64*s)
        self._header(d, st, now, started, hh)
        self._grid(d, st, now, 0, hh, W, H-bh)
        self._bottom(d, st, now, H-bh, H)
        rgba = img.convert("RGBA")
        for path, x, y, size in self._ops:
            ph = self._avatar(path, size)
            if ph is not None: rgba.alpha_composite(ph, (x, y))
        return rgba.convert("RGB")

    def _header(self, d, st, now, started, h):
        W, s = self.w, self.s
        d.rectangle([0, 0, W, h], fill=BG)
        d.line([0, h-1, W, h-1], fill=HLINE)
        pad = int(16*s)
        r = int(18*s)
        cx = pad + r; cy = h // 2
        self._ic_telegram(d, cx, cy, r)
        tx = cx + r + int(14*s)
        group = str(st.get("group") or "Voice Chat")[:44]
        parts = len(st.get("participants") or [])
        d.text((tx, h//2 - int(10*s)), group, font=self.f_title, fill=TEXT, anchor="ld")
        d.text((tx, h//2 + int(10*s)), f"Voice Chat  ·  {parts} participants",
               font=self.f_sub, fill=MUTED, anchor="la")
        rx = W - pad
        dt = datetime.fromtimestamp(now, _tz())
        d.text((rx, h//2 - int(9*s)), dt.strftime("%H:%M:%S"),
               font=self.f_clock, fill=TEXT, anchor="rd")
        d.text((rx, h//2 + int(13*s)), dt.strftime("%a, %d %b"),
               font=self.f_date, fill=MUTED, anchor="rd")
        rx -= int(86*s)
        lw, lh = int(126*s), int(30*s)
        ly = h//2 - lh//2
        pulse = 0.5 + 0.5*abs(math.sin(now*2.2))
        d.rounded_rectangle([rx-lw, ly, rx, ly+lh], radius=lh//2, fill=(60, 16, 20))
        rr = int(5*s)
        dcx, dcy = rx-lw+int(16*s), h//2
        dot_col = tuple(int(RED[i]*pulse + (180,60,60)[i]*(1-pulse)) for i in range(3))
        d.ellipse([dcx-rr, dcy-rr, dcx+rr, dcy+rr], fill=dot_col)
        d.text((dcx+int(13*s), h//2), "LIVE", font=_font(11*s, True), fill=TEXT, anchor="lm")
        d.text((rx-int(12*s), h//2), _hms(now-started),
               font=_font(12*s, True), fill=TEXT, anchor="rm")
        rx -= lw + int(16*s)
        for k in range(4):
            hh2 = int((6 + k*4)*s)
            bx = rx - (3-k)*int(8*s)
            d.rounded_rectangle([bx-int(5*s), h//2+int(11*s)-hh2,
                                 bx, h//2+int(11*s)],
                                radius=int(1*s),
                                fill=GREEN if k < 3 else DIM)

    def _grid(self, d, st, now, x0, y0, x1, y1):
        s = self.s
        parts = list(st.get("participants") or [])
        parts.sort(key=lambda p: (not p.get("speaking"), bool(p.get("muted")),
                                  str(p.get("name","")).lower()))
        n = len(parts)
        pad = int(14*s); gap = int(10*s)
        aw, ah = x1-x0-2*pad, y1-y0-2*pad
        if n <= 1: cols, rows = 1, 1
        elif n == 2: cols, rows = 2, 1
        elif n <= 4: cols, rows = 2, 2
        elif n <= 6: cols, rows = 3, 2
        elif n <= 9: cols, rows = 3, 3
        elif n <= 12: cols, rows = 4, 3
        else: cols, rows = 4, 4
        tw = (aw - gap*(cols-1))//cols
        th = (ah - gap*(rows-1))//rows
        if n == 0:
            cx, cy = (x0+x1)//2, (y0+y1)//2
            for k in range(3):
                rr = int((50 + k*28 + 16*abs(math.sin(now*1.5+k*0.8))) * s)
                d.ellipse([cx-rr, cy-rr, cx+rr, cy+rr],
                          outline=tuple(int(BG[i]+(BLUE[i]-BG[i])*(0.6-k*0.15)) for i in range(3)),
                          width=max(1, int(2*s)))
            d.text((cx, cy+int(80*s)), "Waiting for participants…",
                   font=_font(16*s, True), fill=MUTED, anchor="mm")
            return
        for i, p in enumerate(parts[:rows*cols]):
            r, c = divmod(i, cols)
            tx = x0 + pad + c*(tw+gap)
            ty = y0 + pad + r*(th+gap)
            self._tile(d, p, tx, ty, tw, th, now)

    def _tile(self, d, p, x, y, w, h, now):
        s = self.s
        speaking = bool(p.get("speaking"))
        muted = bool(p.get("muted"))
        rad = int(12*s)
        d.rounded_rectangle([x, y, x+w, y+h], radius=rad, fill=TILE)
        strip_h = int(36*s)
        d.rounded_rectangle([x, y+h-strip_h, x+w, y+h], radius=rad, fill=TILE2)
        d.rectangle([x, y+h-strip_h, x+w, y+h-strip_h+rad], fill=TILE2)
        ar = int(min(w, h-strip_h) * 0.28)
        cx, cy = x+w//2, y + (h-strip_h)//2
        name = str(p.get("name") or "User")
        col = AV_COLORS[sum(map(ord, name)) % len(AV_COLORS)]
        d.ellipse([cx-ar, cy-ar, cx+ar, cy+ar], fill=col)
        photo = p.get("photo") or ""
        if photo:
            self._ops.append((photo, cx-ar, cy-ar, ar*2))
        else:
            d.text((cx, cy), _initials(name), font=_font(max(12, int(ar*0.85)), True),
                   fill=TEXT, anchor="mm")
        if speaking:
            d.rounded_rectangle([x+1, y+1, x+w-1, y+h-1], radius=rad,
                                outline=GREEN, width=max(2, int(3*s)))
            bx = x + int(12*s)
            by = y + int(24*s)
            for k in range(4):
                lvl = 0.3 + 0.7*abs(math.sin(now*(3.0+k*0.6)))
                bh = int(18*s*lvl)
                d.rectangle([bx+k*int(7*s), by-bh, bx+k*int(7*s)+int(5*s), by], fill=GREEN)
        label = name + ("  (You)" if p.get("is_me") else "")
        d.text((x+int(12*s), y+h-int(18*s)), _fit(d, label, self.f_name, w-int(56*s)),
               font=self.f_name, fill=TEXT, anchor="ld")
        mr = int(11*s)
        mx, my = x+w-int(20*s), y+h-int(18*s)
        mic_col = RED if muted else (GREEN if speaking else (95, 115, 130))
        d.ellipse([mx-mr, my-mr, mx+mr, my+mr], fill=mic_col)
        self._ic_mic(d, mx, my, mr*0.85, TEXT, muted=muted)
        if p.get("hand"):
            hr = int(13*s)
            hcx, hcy = x+w-int(20*s), y+int(20*s)
            d.ellipse([hcx-hr, hcy-hr, hcx+hr, hcy+hr], fill=YELLOW)
            d.text((hcx, hcy), "H", font=_font(max(9, int(hr*1.2)), True),
                   fill=BG, anchor="mm")
        vol = p.get("volume")
        if vol and vol != 100:
            d.text((x+int(10*s), y+int(10*s)), f"{vol}%",
                   font=self.f_tiny, fill=BLUE, anchor="la")

    def _bottom(self, d, st, now, y0, y1):
        W, s = self.w, self.s
        h = y1 - y0
        d.rectangle([0, y0, W, y1], fill=BG2)
        d.line([0, y0, W, y0], fill=HLINE)
        cy = y0 + h//2
        btn_r = int(22*s)
        spacing = int(58*s)
        mic_on = bool(st.get("mic_on"))
        mic_col = GREEN if mic_on else RED
        buttons = [("mic", mic_col),
                   ("cam", (62, 76, 92)),
                   ("screen", BLUE if st.get("sharing") else (62, 76, 92)),
                   ("gear", (62, 76, 92))]
        total_w = len(buttons) * spacing + spacing
        start_x = W//2 - total_w//2 + btn_r
        bx = start_x
        for kind, col in buttons:
            d.ellipse([bx-btn_r, cy-btn_r, bx+btn_r, cy+btn_r],
                      fill=tuple(int(c*0.25 + BG2[i]*0.75) for i,c in enumerate(col)))
            if kind == "mic":
                self._ic_mic(d, bx, cy, btn_r*0.6, TEXT, muted=not mic_on)
            elif kind == "cam":
                self._ic_cam(d, bx, cy, btn_r*0.65, TEXT)
            elif kind == "screen":
                self._ic_screen(d, bx, cy, btn_r*0.65, TEXT)
            elif kind == "gear":
                self._ic_gear(d, bx, cy, btn_r*0.7, TEXT)
            bx += spacing
        lw, lh = int(80*s), int(34*s)
        d.rounded_rectangle([bx-lw//2, cy-lh//2, bx+lw//2, cy+lh//2],
                            radius=lh//2, fill=RED)
        d.text((bx, cy), "Leave", font=_font(13*s, True), fill=TEXT, anchor="mm")
        rx = W - int(14*s)
        dt = datetime.fromtimestamp(now, _tz())
        d.text((rx, cy - int(8*s)), dt.strftime("%H:%M"),
               font=_font(13*s, True), fill=MUTED, anchor="rd")
        d.text((rx, cy + int(9*s)), dt.strftime("%d %b %Y"),
               font=self.f_tiny, fill=DIM, anchor="rd")


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
