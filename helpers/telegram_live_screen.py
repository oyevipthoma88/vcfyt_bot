#!/usr/bin/env python3
"""Premium Telegram-style LIVE voice chat screen -> raw I420 to stdout."""
import argparse, json, math, os, re, sys, time
from datetime import datetime, timedelta, timezone
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
FDIR = os.path.join(os.path.dirname(HERE), "assets", "fonts")

BG      = (14, 17, 24)
BG2     = (8, 10, 15)
TILE    = (24, 30, 42)
TILE2   = (16, 21, 30)
TEXT    = (255, 255, 255)
MUTED   = (112, 132, 153)
GREEN   = (0, 255, 128)
RED     = (255, 60, 60)
YELLOW  = (255, 210, 63)
CYAN    = (0, 245, 255)
PINK    = (255, 45, 149)

AV_COLORS = [(231,76,60),(52,152,219),(46,204,113),(155,89,182),(241,196,15),(26,188,156),(230,126,34),(236,64,122)]
_FONT_CACHE = {}

def _font(size, bold=False):
    size = max(8, int(size))
    key = (size, bold)
    if key in _FONT_CACHE: return _FONT_CACHE[key]
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for base in (FDIR, "/usr/share/fonts/truetype/dejavu"):
        p = os.path.join(base, name)
        if os.path.exists(p):
            try:
                f = ImageFont.truetype(p, size)
                _FONT_CACHE[key] = f
                return f
            except Exception: pass
    f = ImageFont.load_default()
    _FONT_CACHE[key] = f
    return f

def _sanitize_name(name):
    name = str(name or "User")
    clean = re.sub(r'[^a-zA-Z0-9\s\-_\.]', '', name).strip()
    if len(clean) < 2: clean = re.sub(r'[^a-zA-Z0-9]', '', name)[:2].upper()
    return (clean or "User")[:18]

def _tz():
    try: off = float(os.environ.get("SS_TZ_OFFSET", "5.5"))
    except ValueError: off = 5.5
    return timezone(timedelta(hours=off))

def _read_state(p):
    try:
        with open(p, "r", encoding="utf-8") as f: return json.load(f)
    except Exception: return {}

def _hms(sec):
    sec = int(max(0, sec))
    return f"{sec//3600:02d}:{sec%3600//60:02d}:{sec%60:02d}"

def rgb_to_i420(img):
    import numpy as np
    ycc = np.asarray(img.convert("YCbCr"), dtype=np.uint8)
    y = ycc[..., 0]
    y = (y.astype(np.uint16) * 219 // 255 + 16).astype(np.uint8)
    u = ycc[0::2, 0::2, 1].astype(np.uint16) * 224 // 255 + 16
    v = ycc[0::2, 0::2, 2].astype(np.uint16) * 224 // 255 + 16
    return y.tobytes() + u.astype(np.uint8).tobytes() + v.astype(np.uint8).tobytes()

class Renderer:
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.s = h / 720.0
        self._av = {}
        self._ops = []
        s = self.s
        self.f_title = _font(18*s, True)
        self.f_sub   = _font(12*s)
        self.f_name  = _font(14*s, True)
        self.f_clock = _font(22*s, True)
        self.f_date  = _font(10*s)

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

    def render(self, st, now, started):
        W, H, s = self.w, self.h, self.s
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        self._ops = []
        hh = int(60*s); bh = int(64*s)
        side_w = int(W * 0.42)
        self._header(d, st, now, started, hh)
        self._grid(d, st, now, 0, hh, W-side_w, H-bh)
        self._sidebar(d, st, now, W-side_w, hh, W, H-bh)
        self._bottom(d, st, now, H-bh, H)
        rgba = img.convert("RGBA")
        for path, x, y, size in self._ops:
            ph = self._avatar(path, size)
            if ph is not None: rgba.alpha_composite(ph, (x, y))
        return rgba.convert("RGB")

    def _header(self, d, st, now, started, h):
        W, s = self.w, self.s
        d.rectangle([0, 0, W, h], fill=BG)
        d.line([0, h-1, W, h-1], fill=(30, 40, 55))
        pad = int(16*s); r = int(18*s)
        cx, cy = pad+r, h//2
        d.ellipse([cx-r, cy-r, cx+r, cy+r], fill=(51, 144, 236))
        tx = cx + r + int(14*s)
        me_name = _sanitize_name(st.get("me_name") or "My Studio")
        d.text((tx, h//2-int(8*s)), f"{me_name}'s Studio", font=self.f_title, fill=CYAN, anchor="ld")
        parts = len(st.get("participants") or [])
        d.text((tx, h//2+int(10*s)), f"Voice Chat · {parts} connected", font=self.f_sub, fill=MUTED, anchor="la")
        rx = W - pad
        dt = datetime.fromtimestamp(now, _tz())
        d.text((rx, h//2-int(8*s)), dt.strftime("%H:%M:%S"), font=self.f_clock, fill=TEXT, anchor="rd")
        d.text((rx, h//2+int(12*s)), dt.strftime("%a, %d %b"), font=self.f_date, fill=MUTED, anchor="rd")
        rx -= int(90*s)
        lw, lh = int(120*s), int(28*s); ly = h//2 - lh//2
        d.rounded_rectangle([rx-lw, ly, rx, ly+lh], radius=lh//2, fill=(60, 16, 20))
        d.ellipse([rx-lw+int(12*s), h//2-int(5*s), rx-lw+int(22*s), h//2+int(5*s)], fill=RED)
        d.text((rx-lw+int(28*s), h//2), "LIVE", font=_font(10*s, True), fill=TEXT, anchor="lm")
        d.text((rx-int(10*s), h//2), _hms(now-started), font=_font(11*s, True), fill=TEXT, anchor="rm")

    def _grid(self, d, st, now, x0, y0, x1, y1):
        s = self.s
        parts = list(st.get("participants") or [])
        parts.sort(key=lambda p: (not p.get("speaking"), bool(p.get("muted")), _sanitize_name(p.get("name", "")).lower()))
        n = len(parts)
        pad = int(20*s); gap = int(16*s)
        aw, ah = x1-x0-2*pad, y1-y0-2*pad
        if n <= 1: cols, rows = 1, 1
        elif n <= 4: cols, rows = 2, 2
        elif n <= 6: cols, rows = 3, 2
        elif n <= 9: cols, rows = 3, 3
        else: cols, rows = 4, 3
        tw = (aw - gap*(cols-1))//cols
        th = (ah - gap*(rows-1))//rows
        if n == 0:
            d.text(((x0+x1)//2, (y0+y1)//2), "Waiting for participants...", font=_font(16*s, True), fill=MUTED, anchor="mm")
            return
        for i, p in enumerate(parts[:rows*cols]):
            r, c = divmod(i, cols)
            self._tile(d, p, x0+pad+c*(tw+gap), y0+pad+r*(th+gap), tw, th, now)

    def _tile(self, d, p, x, y, w, h, now):
        s = self.s
        speaking = bool(p.get("speaking")); muted = bool(p.get("muted"))
        rad = int(16*s)
        d.rounded_rectangle([x, y, x+w, y+h], radius=rad, fill=TILE, outline=(40,50,70), width=1)
        if speaking:
            d.rounded_rectangle([x-2, y-2, x+w+2, y+h+2], radius=rad+2, outline=GREEN, width=max(2, int(3*s)))
        strip_h = int(40*s)
        d.rounded_rectangle([x, y+h-strip_h, x+w, y+h], radius=rad, fill=TILE2)
        d.rectangle([x, y+h-strip_h, x+w, y+h-strip_h+rad], fill=TILE2)
        
        ar = int(min(w, h-strip_h) * 0.4)
        cx, cy = x+w//2, y+(h-strip_h)//2
        name = _sanitize_name(p.get("name") or "User")
        col = AV_COLORS[sum(map(ord, name)) % len(AV_COLORS)]
        
        d.ellipse([cx-ar, cy-ar, cx+ar, cy+ar], fill=col)
        photo = p.get("photo") or ""
        if photo: self._ops.append((photo, cx-ar, cy-ar, ar*2))
        else: d.text((cx, cy), name[:2].upper(), font=_font(int(ar*0.8), True), fill=TEXT, anchor="mm")
        
        d.text((x+int(14*s), y+h-int(18*s)), _fit(d, name, self.f_name, w-int(50*s)), font=self.f_name, fill=TEXT, anchor="ld")
        mr = int(12*s); mx, my = x+w-int(20*s), y+h-int(20*s)
        d.ellipse([mx-mr, my-mr, mx+mr, my+mr], fill=RED if muted else (GREEN if speaking else (80,100,120)))
        q = int(5*s)
        if muted:
            d.line([mx-q,my-q,mx+q,my+q], fill=TEXT, width=max(2,int(2*s)))
            d.line([mx-q,my+q,mx+q,my-q], fill=TEXT, width=max(2,int(2*s)))
        else:
            d.rounded_rectangle([mx-q,my-int(7*s),mx+q,my+int(3*s)], radius=q, fill=TEXT)

    def _sidebar(self, d, st, now, x0, y0, x1, y1):
        s = self.s
        d.rectangle([x0, y0, x1, y1], fill=(10, 12, 18))
        grid_col = (20, 25, 35)
        for gx in range(x0, x1, int(30*s)): d.line([gx, y0, gx, y1], fill=grid_col, width=1)
        for gy in range(y0, y1, int(30*s)): d.line([x0, gy, x1, gy], fill=grid_col, width=1)
        
        pad = int(16*s); cx = (x0+x1)//2; y = y0+pad
        d.text((cx, y), "SUPREME AUDIO CORE", font=_font(11*s, True), fill=PINK, anchor="mm")
        d.line([x0+pad, y+int(12*s), x1-pad, y+int(12*s)], fill=PINK, width=1)
        y += int(30*s)
        
        img_r = int(40*s); img_cy = y + img_r
        pulse = 0.5 + 0.5*math.sin(now*3.0)
        for i in range(3, 0, -1):
            glow_r = img_r + int(i*8*pulse*s)
            col = (255,20,80) if i==1 else (100,10,30)
            d.ellipse([cx-glow_r, img_cy-glow_r, cx+glow_r, img_cy+glow_r], outline=col, width=max(1, int(2*s)))
        d.ellipse([cx-img_r, img_cy-img_r, cx+img_r, img_cy+img_r], fill=(15,20,30), outline=CYAN, width=max(2,int(2*s)))
        d.rounded_rectangle([cx-int(10*s), img_cy-int(18*s), cx+int(10*s), img_cy+int(6*s)], radius=int(10*s), fill=PINK)
        y = img_cy + img_r + int(24*s)
        
        btn_w, btn_h = int(150*s), int(32*s)
        d.rounded_rectangle([cx-btn_w//2, y, cx+btn_w//2, y+btn_h], radius=btn_h//2, fill=(255,20,80), outline=PINK, width=1)
        d.text((cx, y+btn_h//2), "SYSTEM LIVE", font=_font(12*s, True), fill=TEXT, anchor="mm")
        y += btn_h + int(24*s)
        
        tw, th = int(70*s), int(24*s)
        d.rounded_rectangle([cx-tw-int(10*s), y, cx-int(10*s), y+th], radius=int(4*s), outline=YELLOW, width=1, fill=(20,20,10))
        d.text((cx-tw//2-int(10*s), y+th//2), "MIC [ON]", font=_font(9*s, True), fill=YELLOW, anchor="mm")
        d.rounded_rectangle([cx+int(10*s), y, cx+tw+int(10*s), y+th], radius=int(4*s), outline=YELLOW, width=1, fill=(20,20,10))
        d.text((cx+tw//2+int(10*s), y+th//2), "FX [OFF]", font=_font(9*s, True), fill=YELLOW, anchor="mm")
        y += th + int(24*s)
        
        d.text((x0+pad, y), "SIGNAL MODULATORS", font=_font(10*s, True), fill=MUTED, anchor="la")
        y += int(20*s)
        for label, val, max_val in [("OVERDRIVE",380,500), ("TIME WARP",0,100), ("BASS EQ",0,20), ("TREBLE EQ",0,20)]:
            d.text((x0+pad, y), label, font=_font(9*s, True), fill=(150,160,180), anchor="la")
            d.text((x1-pad, y), str(val), font=_font(9*s, True), fill=PINK, anchor="ra")
            y += int(16*s)
            track_w = x1-x0-2*pad
            d.line([x0+pad, y+int(2*s), x1-pad, y+int(2*s)], fill=(40,50,70), width=max(2,int(2*s)))
            ratio = max(0, min(1, val/max_val)) if max_val else 0.5
            d.line([x0+pad, y+int(2*s), x0+pad+int(track_w*ratio), y+int(2*s)], fill=PINK, width=max(2,int(2*s)))
            knob_x = x0+pad+int(track_w*ratio)
            d.ellipse([knob_x-int(6*s), y-int(2*s), knob_x+int(6*s), y+int(6*s)], fill=TEXT, outline=PINK, width=1)
            y += int(24*s)
            
        wave_y = y1 - int(60*s)
        d.text((cx, wave_y-int(12*s)), "OUTPUT WAVEFORM", font=_font(10*s, True), fill=CYAN, anchor="mm")
        n_bars = 30; bar_w = int((x1-x0-2*pad)/n_bars)
        for i in range(n_bars):
            bx = x0+pad+i*bar_w
            hf = abs(math.sin(now*5+i*0.5)*math.cos(now*2+i*0.2))
            bh = int((30*s)*hf)+int(2*s)
            col = PINK if i<n_bars//3 else (YELLOW if i<2*n_bars//3 else GREEN)
            cy_w = wave_y+int(24*s)
            d.rectangle([bx, cy_w-bh, bx+bar_w-int(2*s), cy_w+bh], fill=col)

    def _bottom(self, d, st, now, y0, y1):
        W, s = self.w, self.s
        d.rectangle([0, y0, W, y1], fill=BG2)
        d.line([0, y0, W, y0], fill=(30, 40, 55))
        cy = y0 + (y1-y0)//2
        d.ellipse([W//2-int(28*s), cy-int(24*s), W//2+int(28*s), cy+int(24*s)], fill=(50,60,80))
        d.text((W-int(20*s), cy), datetime.fromtimestamp(now, _tz()).strftime("%H:%M"), font=_font(14*s, True), fill=MUTED, anchor="rm")

def _fit(d, text, font, max_w):
    text = str(text or "")
    if d.textlength(text, font=font) <= max_w: return text
    while text and d.textlength(text + "…", font=font) > max_w: text = text[:-1]
    return text + "…"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True)
    ap.add_argument("--w", type=int, default=1280)
    ap.add_argument("--h", type=int, default=720)
    ap.add_argument("--fps", type=int, default=15)
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
            if st.get("started"): started = float(st["started"])
        k = (json.dumps(st, sort_keys=True, default=str), int(now*5))
        if k != key:
            frame = rgb_to_i420(r.render(st, now, started))
            key = k
        try:
            out.write(frame); out.flush()
        except (BrokenPipeError, OSError): return
        n += 1
        sl = t0 + n / fps - time.time()
        if sl > 0: time.sleep(sl)

if __name__ == "__main__":
    main()
