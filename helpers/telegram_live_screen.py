#!/usr/bin/env python3
"""Premium Cyberpunk HUD UI - Exact match to User Hint."""
import argparse, json, math, os, re, sys, time
from datetime import datetime, timedelta, timezone
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
FDIR = os.path.join(os.path.dirname(HERE), "assets", "fonts")

# Colors
BG_MAIN  = (8, 12, 20)
BG_SIDE  = (12, 18, 28)
BG_PANEL = (14, 22, 35)
TILE_BG  = (20, 28, 42)
TEXT     = (255, 255, 255)
MUTED    = (120, 140, 165)
CYAN     = (0, 220, 255)
PINK     = (255, 20, 120)
GREEN    = (57, 255, 20)
YELLOW   = (255, 210, 63)
PURPLE   = (180, 50, 255)
RED      = (229, 57, 53)

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
    clean = re.sub(r'[^a-zA-Z0-9\s]', '', name).strip()
    if len(clean) < 2: clean = re.sub(r'[^a-zA-Z0-9]', '', name)[:2].upper()
    return (clean or "User")[:16]

def _tz():
    try: off = float(os.environ.get("SS_TZ_OFFSET", "5.5"))
    except ValueError: off = 5.5
    return timezone(timedelta(hours=off))

def _read_state(p):
    try:
        with open(p, "r", encoding="utf-8") as f: return json.load(f)
    except Exception: return {}

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
        self.f_logo  = _font(20*s, True)
        self.f_title = _font(16*s, True)
        self.f_sub   = _font(11*s)
        self.f_name  = _font(14*s, True)
        self.f_small = _font(10*s)
        self.f_clock = _font(22*s, True)

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
        img = Image.new("RGB", (W, H), BG_MAIN)
        d = ImageDraw.Draw(img)
        self._ops = []
        
        header_h = int(60*s)
        bottom_h = int(50*s)
        left_w = int(W * 0.16)
        right_w = int(W * 0.35)
        center_w = W - left_w - right_w
        
        self._header(d, st, now, header_h)
        self._left_sidebar(d, st, left_w, header_h, H - bottom_h)
        self._center_grid(d, st, now, left_w, header_h, left_w + center_w, H - bottom_h)
        self._right_panel(d, st, now, W - right_w, header_h, W, H - bottom_h)
        self._bottom_bar(d, st, now, H - bottom_h, H)
        
        rgba = img.convert("RGBA")
        for path, x, y, size in self._ops:
            ph = self._avatar(path, size)
            if ph is not None: rgba.alpha_composite(ph, (x, y))
        return rgba.convert("RGB")

    def _header(self, d, st, now, h):
        W, s = self.w, self.s
        d.rectangle([0, 0, W, h], fill=BG_SIDE)
        d.line([0, h-1, W, h-1], fill=(30, 45, 60))
        
        pad = int(16*s)
        # Telegram Logo (Circle)
        cx = pad + int(14*s)
        d.ellipse([cx-int(14*s), h//2-int(14*s), cx+int(14*s), h//2+int(14*s)], fill=CYAN)
        
        me_name = _sanitize_name(st.get("me_name") or "My Studio")
        tx = cx + int(26*s)
        d.text((tx, h//2-int(8*s)), f"{me_name} VC", font=self.f_title, fill=TEXT, anchor="ld")
        d.text((tx, h//2+int(10*s)), "Voice Chat", font=self.f_sub, fill=MUTED, anchor="la")
        
        rx = W - pad
        dt = datetime.fromtimestamp(now, _tz())
        # 12-hour format
        d.text((rx, h//2), dt.strftime("%I:%M:%S %p"), font=self.f_clock, fill=TEXT, anchor="rm")
        rx -= int(110*s)
        
        # Live & Quality
        d.ellipse([rx-int(30*s), h//2-int(4*s), rx-int(22*s), h//2+int(4*s)], fill=GREEN)
        d.text((rx-int(16*s), h//2), "Live", font=self.f_sub, fill=GREEN, anchor="lm")
        rx -= int(70*s)
        d.text((rx, h//2), "Good", font=self.f_sub, fill=MUTED, anchor="rm")

    def _left_sidebar(self, d, st, w, y0, y1):
        s = self.s
        d.rectangle([0, y0, w, y1], fill=BG_SIDE)
        d.line([w-1, y0, w-1, y1], fill=(30, 45, 60))
        
        pad = int(20*s)
        y = y0 + pad
        menu = [("🎙", "Voice Chat"), ("👥", "Members"), ("⚙", "Settings"), ("⚡", "Commands"), ("👑", "Premium"), ("❓", "Help")]
        
        # Active item
        d.rectangle([0, y, w, y+int(40*s)], fill=BG_PANEL)
        d.rectangle([0, y, int(4*s), y+int(40*s)], fill=CYAN)
        
        for icon, item in menu:
            col = CYAN if item == "Voice Chat" else MUTED
            d.text((pad, y+int(20*s)), f"{icon}  {item}", font=self.f_name, fill=col, anchor="lm")
            y += int(45*s)

        me_name = _sanitize_name(st.get("me_name") or "Owner")
        d.text((pad, y1-pad-int(20*s)), f"@{me_name}", font=self.f_title, fill=PINK, anchor="ld")

    def _center_grid(self, d, st, now, x0, y0, x1, y1):
        s = self.s
        d.rectangle([x0, y0, x1, y1], fill=BG_MAIN)
        
        pad = int(16*s)
        # Add Tech Grid Lines
        grid_col = (20, 30, 45)
        for gx in range(x0, x1, int(40*s)): d.line([gx, y0, gx, y1], fill=grid_col, width=1)
        for gy in range(y0, y1, int(40*s)): d.line([x0, gy, x1, gy], fill=grid_col, width=1)
        
        # Top Mini-Bar
        d.rounded_rectangle([x0+pad, y0+pad, x1-pad, y0+pad+int(36*s)], radius=int(6*s), fill=BG_PANEL, outline=(0,220,255))
        parts = list(st.get("participants") or [])
        d.text((x0+pad+int(16*s), y0+pad+int(18*s)), f"{len(parts)} / 100  •  Voice Chat", font=self.f_sub, fill=TEXT, anchor="lm")
        
        grid_y0 = y0 + pad + int(46*s)
        n = len(parts)
        if n == 0: return
        
        cols = 2 if n <= 4 else 3
        rows = (n + cols - 1) // cols
        
        gap = int(12*s)
        aw, ah = (x1-x0) - 2*pad, (y1-grid_y0) - pad
        tw = (aw - gap*(cols-1))//cols
        th = (ah - gap*(rows-1))//rows
        
        for i, p in enumerate(parts):
            if i >= cols*rows: break
            r, c = divmod(i, cols)
            self._tile(d, p, x0+pad+c*(tw+gap), grid_y0+r*(th+gap), tw, th, now)

    def _tile(self, d, p, x, y, w, h, now):
        s = self.s
        speaking = bool(p.get("speaking"))
        muted = bool(p.get("muted"))
        
        # Tile card (Cyberpunk Glow)
        d.rounded_rectangle([x, y, x+w, y+h], radius=int(12*s), fill=TILE_BG)
        border_col = CYAN if speaking else (60, 80, 110)
        d.rounded_rectangle([x, y, x+w, y+h], radius=int(12*s), outline=border_col, width=max(2, int(2*s)))
        if speaking:
            d.rounded_rectangle([x-2, y-2, x+w+2, y+h+2], radius=int(12*s), outline=CYAN, width=2)
            
        strip_h = int(40*s)
        d.rounded_rectangle([x, y+h-strip_h, x+w, y+h], radius=int(12*s), fill=(12, 18, 28))
        d.rectangle([x, y+h-strip_h, x+w, y+h-strip_h+int(12*s)], fill=(12, 18, 28))
        
        ar = int(min(w, h-strip_h) * 0.22)
        cx, cy = x+w//2, y+(h-strip_h)//2
        
        name = _sanitize_name(p.get("name") or "User")
        col = AV_COLORS[sum(map(ord, name)) % len(AV_COLORS)]
        
        ring_col = CYAN if speaking else (YELLOW if p.get("is_me") else (60,70,90))
        d.ellipse([cx-ar-int(4*s), cy-ar-int(4*s), cx+ar+int(4*s), cy+ar+int(4*s)], outline=ring_col, width=max(2, int(2*s)))
        d.ellipse([cx-ar, cy-ar, cx+ar, cy+ar], fill=col)
        
        photo = p.get("photo") or ""
        if photo and os.path.exists(photo):
            self._ops.append((photo, cx-ar, cy-ar, ar*2))
        else:
            d.text((cx, cy), name[:2].upper(), font=_font(int(ar*0.8), True), fill=TEXT, anchor="mm")
            
        d.text((x+int(12*s), y+h-int(22*s)), name, font=self.f_name, fill=TEXT, anchor="lm")
        
        mr = int(10*s)
        mx, my = x+w-int(20*s), y+h-int(22*s)
        d.ellipse([mx-mr, my-mr, mx+mr, my+mr], fill=RED if muted else GREEN)
        
        q = int(4*s)
        if muted:
            d.line([mx-q,my-q,mx+q,my+q], fill=TEXT, width=max(2,int(2*s)))
            d.line([mx-q,my+q,mx+q,my-q], fill=TEXT, width=max(2,int(2*s)))
        else:
            d.rounded_rectangle([mx-q,my-int(5*s),mx+q,my+int(2*s)], radius=q, fill=TEXT)

    def _right_panel(self, d, st, now, x0, y0, x1, y1):
        s = self.s
        d.rectangle([x0, y0, x1, y1], fill=BG_SIDE)
        d.line([x0, y0, x0, y1], fill=(30, 45, 60))
        
        pad = int(20*s)
        cx = (x0+x1)//2
        y = y0 + pad
        
        # Header
        d.rounded_rectangle([x0+pad, y, x1-pad, y+int(30*s)], radius=int(8*s), fill=(20, 30, 45), outline=PINK, width=2)
        d.text((cx, y+int(15*s)), "LIVE MIC SYSTEM", font=self.f_title, fill=CYAN, anchor="mm")
        d.line([x0+pad, y+int(14*s), x1-pad, y+int(14*s)], fill=(30, 45, 60), width=1)
        y += int(40*s)
        
        # Glowing Mic Centerpiece
        img_r = int(55*s)
        img_cy = y + img_r
        pulse = 0.5 + 0.5*math.sin(now*3.0)
        
        d.ellipse([cx-img_r-int(8*s), img_cy-img_r-int(8*s), cx+img_r+int(8*s), img_cy+img_r+int(8*s)], outline=PINK, width=max(1, int(2*s)))
        d.ellipse([cx-img_r, img_cy-img_r, cx+img_r, img_cy+img_r], fill=(20, 30, 45), outline=CYAN, width=max(2, int(3*s)))
        
        # Mic Icon
        m_w, m_h = int(14*s), int(22*s)
        d.rounded_rectangle([cx-m_w, img_cy-m_h, cx+m_w, img_cy+int(8*s)], radius=int(10*s), fill=CYAN)
        d.arc([cx-int(22*s), img_cy-int(18*s), cx+int(22*s), img_cy+int(22*s)], start=0, end=180, fill=CYAN, width=max(2,int(3*s)))
        
        # Waveforms Left/Right
        wave_y = img_cy
        for i in range(12):
            offset = i * int(10*s)
            h = int(abs(math.sin(now*5 + i*0.5)) * 25 * s)
            d.line([cx - img_r - int(15*s) - offset, wave_y - h, cx - img_r - int(15*s) - offset, wave_y + h], fill=PINK, width=max(1,int(2*s)))
            d.line([cx + img_r + int(15*s) + offset, wave_y - h, cx + img_r + int(15*s) + offset, wave_y + h], fill=PURPLE, width=max(1,int(2*s)))

        y = img_cy + img_r + int(30*s)
        
        # Control Buttons
        bw, bh = int(50*s), int(50*s)
        gap = int(8*s)
        tot_w = 5*bw + 4*gap
        sx = cx - tot_w//2
        by = y
        
        btns = [("Mic", CYAN), ("Screen", (60,70,90)), ("Raise", (60,70,90)), ("Mute", (60,70,90)), ("Gear", (60,70,90))]
        for lbl, col in btns:
            d.rounded_rectangle([sx, by, sx+bw, by+bh], radius=int(8*s), fill=TILE_BG, outline=col)
            d.text((sx+bw//2, by+bh//2), lbl, font=self.f_small, fill=TEXT if lbl=="Mic" else MUTED, anchor="mm")
            sx += bw + gap
            
        y = by + bh + int(30*s)
        
        # Sliders (Fake)
        sliders = [("Voice Volume", "85%", 0.85), ("Music Volume", "70%", 0.70)]
        for lbl, val_str, ratio in sliders:
            d.text((x0+pad, y), lbl, font=self.f_small, fill=TEXT, anchor="lm")
            d.text((x1-pad, y), val_str, font=self.f_small, fill=CYAN, anchor="rm")
            y += int(14*s)
            track_w = (x1-x0) - 2*pad
            d.line([x0+pad, y+int(2*s), x1-pad, y+int(2*s)], fill=(30,45,60), width=max(2,int(2*s)))
            d.line([x0+pad, y+int(2*s), x0+pad+int(track_w*ratio), y+int(2*s)], fill=CYAN, width=max(2,int(2*s)))
            kx = x0+pad+int(track_w*ratio)
            d.ellipse([kx-int(5*s), y-int(2*s), kx+int(5*s), y+int(6*s)], fill=TEXT)
            y += int(28*s)

    def _bottom_bar(self, d, st, now, y0, y1):
        W, s = self.w, self.s
        d.rectangle([0, y0, W, y1], fill=BG_SIDE)
        d.line([0, y0, W, y0], fill=(30, 45, 60))
        cy = y0 + (y1-y0)//2
        
        me_name = _sanitize_name(st.get("me_name") or "Owner")
        d.text((int(20*s), cy), f"{me_name} VC", font=self.f_title, fill=CYAN, anchor="lm")
        d.text((W//2, cy), "Talk Loud  •  Stay Active", font=self.f_title, fill=TEXT, anchor="mm")

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
    # SMOOTH SS ON/OFF: this renderer (PIL + I420 convert) and the VP8
    # encoder start together and grabbed the CPU from the audio FFmpeg ->
    # audio "atak" for a moment on every toggle.  Lowest CPU priority here
    # so the audio pipe always wins.
    try:
        os.nice(15)
    except (AttributeError, OSError):
        pass
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
        # 2 renders/sec is enough for clock + meters (was 5) -> ~60 % less CPU.
        k = (json.dumps(st, sort_keys=True, default=str), int(now*2))
        if k != key:
            frame = rgb_to_i420(r.render(st, now, started))
            key = k
        try:
            out.write(frame); out.flush()
        except (BrokenPipeError, OSError): return
        n += 1
        sl = t0 + n / fps - time.time()
        if sl > 0:
            time.sleep(sl)
        elif sl < -1.0 / fps:
            # Fell behind (CPU busy): never burst-catch-up frames, that burst
            # is what stalled the audio.  Re-sync the clock instead.
            t0, n = time.time(), 0

if __name__ == "__main__":
    main()
