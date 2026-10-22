#!/usr/bin/env python3
"""Premium Cyberpunk Neon UI for Telegram VC."""
import argparse, json, math, os, re, sys, time
from datetime import datetime, timedelta, timezone
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
FDIR = os.path.join(os.path.dirname(HERE), "assets", "fonts")

# Colors based on hint
BG_DARK   = (8, 12, 20)
PANEL     = (14, 20, 32)
TILE_BG   = (16, 24, 38)
TEXT      = (255, 255, 255)
MUTED     = (120, 140, 165)
NEON_CYAN = (0, 245, 255)
NEON_PINK = (255, 20, 120)
NEON_GRN  = (57, 255, 20)
NEON_YEL  = (255, 210, 63)
NEON_PURP = (180, 50, 255)

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
        self.f_logo  = _font(22*s, True)
        self.f_title = _font(16*s, True)
        self.f_sub   = _font(11*s)
        self.f_name  = _font(14*s, True)
        self.f_clock = _font(22*s, True)
        self.f_small = _font(10*s)

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
        img = Image.new("RGB", (W, H), BG_DARK)
        d = ImageDraw.Draw(img)
        self._ops = []
        
        # Layout ratios (approx hint image)
        header_h = int(64*s)
        bottom_h = int(56*s)
        left_w = int(W * 0.16)
        right_w = int(W * 0.35)
        center_w = W - left_w - right_w
        
        self._header(d, st, now, started, header_h)
        self._left_sidebar(d, st, now, left_w, header_h, H - bottom_h)
        self._center_grid(d, st, now, left_w, header_h, left_w + center_w, H - bottom_h)
        self._right_panel(d, st, now, W - right_w, header_h, W, H - bottom_h)
        self._bottom_bar(d, st, now, H - bottom_h, H)
        
        rgba = img.convert("RGBA")
        for path, x, y, size in self._ops:
            ph = self._avatar(path, size)
            if ph is not None: rgba.alpha_composite(ph, (x, y))
        return rgba.convert("RGB")

    def _header(self, d, st, now, started, h):
        W, s = self.w, self.s
        d.rectangle([0, 0, W, h], fill=PANEL)
        d.line([0, h-1, W, h-1], fill=(30, 40, 60), width=1)
        
        # Logo & Title
        pad = int(16*s)
        cx = pad + int(16*s)
        d.ellipse([cx-int(14*s), h//2-int(14*s), cx+int(14*s), h//2+int(14*s)], fill=NEON_CYAN)
        
        me_name = _sanitize_name(st.get("me_name") or "My Studio")
        tx = cx + int(26*s)
        d.text((tx, h//2-int(10*s)), f"{me_name} VC", font=self.f_title, fill=TEXT, anchor="ld")
        d.text((tx, h//2+int(10*s)), "Voice Chat", font=self.f_sub, fill=MUTED, anchor="la")
        
        # Right: Live, Quality, Clock (12-hour)
        rx = W - pad
        dt = datetime.fromtimestamp(now, _tz())
        clock_str = dt.strftime("%I:%M:%S %p") # 12-hour format
        d.text((rx, h//2), clock_str, font=self.f_clock, fill=TEXT, anchor="rm")
        rx -= int(120*s)
        
        # Live indicator
        d.ellipse([rx-int(40*s), h//2-int(4*s), rx-int(32*s), h//2+int(4*s)], fill=NEON_GRN)
        d.text((rx-int(26*s), h//2), "Live", font=self.f_sub, fill=NEON_GRN, anchor="lm")
        rx -= int(80*s)
        
        # Quality
        d.text((rx, h//2), "Good", font=self.f_sub, fill=MUTED, anchor="rm")

    def _left_sidebar(self, d, st, now, w, y0, y1):
        s = self.s
        d.rectangle([0, y0, w, y1], fill=PANEL)
        d.line([w-1, y0, w-1, y1], fill=(30, 40, 60))
        
        pad = int(16*s)
        y = y0 + pad
        menu = ["Voice Chat", "Members", "Settings", "Commands", "Premium", "Help"]
        
        # Active item (Voice Chat)
        d.rectangle([0, y, w, y+int(40*s)], fill=(20, 30, 50))
        d.rectangle([0, y, int(4*s), y+int(40*s)], fill=NEON_CYAN)
        
        for item in menu:
            col = NEON_CYAN if item == "Voice Chat" else MUTED
            d.text((pad, y+int(20*s)), item, font=self.f_name, fill=col, anchor="lm")
            y += int(50*s)

        # Bottom Profile
        me_name = _sanitize_name(st.get("me_name") or "Owner")
        d.text((pad, y1-pad-int(20*s)), f"@{me_name}", font=self.f_title, fill=NEON_PINK, anchor="ld")
        d.text((pad, y1-pad), "VC Owner", font=self.f_small, fill=MUTED, anchor="ld")

    def _center_grid(self, d, st, now, x0, y0, x1, y1):
        s = self.s
        d.rectangle([x0, y0, x1, y1], fill=BG_DARK)
        
        # Top bar inside center
        pad = int(16*s)
        d.rounded_rectangle([x0+pad, y0+pad, x1-pad, y0+pad+int(40*s)], radius=int(8*s), fill=TILE_BG, outline=(30,40,60))
        parts = list(st.get("participants") or [])
        d.text((x0+pad+int(16*s), y0+pad+int(20*s)), f"{len(parts)} / 100  •  Voice Chat", font=self.f_sub, fill=TEXT, anchor="lm")
        
        grid_y0 = y0 + pad + int(50*s)
        n = len(parts)
        if n == 0: return
        
        # Grid layout
        cols = 2 if n <= 4 else 3
        rows = (n + cols - 1) // cols
        
        gap = int(12*s)
        aw, ah = (x1-x0) - 2*pad, (y1-grid_y0) - pad
        tw = (aw - gap*(cols-1))//cols
        th = (ah - gap*(rows-1))//rows
        
        for i, p in enumerate(parts):
            if i >= cols*rows: break
            r, c = divmod(i, cols)
            tx = x0 + pad + c*(tw+gap)
            ty = grid_y0 + r*(th+gap)
            self._tile(d, p, tx, ty, tw, th, now)

    def _tile(self, d, p, x, y, w, h, now):
        s = self.s
        speaking = bool(p.get("speaking"))
        muted = bool(p.get("muted"))
        
        # Tile BG
        d.rounded_rectangle([x, y, x+w, y+h], radius=int(12*s), fill=TILE_BG, outline=(30, 40, 60), width=1)
        
        # Glowing ring if speaking
        if speaking:
            d.rounded_rectangle([x, y, x+w, y+h], radius=int(12*s), outline=NEON_CYAN, width=max(2, int(2*s)))
            
        # Name strip at bottom
        strip_h = int(45*s)
        d.rounded_rectangle([x, y+h-strip_h, x+w, y+h], radius=int(12*s), fill=(12, 18, 28))
        d.rectangle([x, y+h-strip_h, x+w, y+h-strip_h+int(12*s)], fill=(12, 18, 28))
        
        # Avatar
        ar = int(min(w, h-strip_h) * 0.28)
        cx, cy = x+w//2, y+(h-strip_h)//2
        
        name = _sanitize_name(p.get("name") or "User")
        col = AV_COLORS[sum(map(ord, name)) % len(AV_COLORS)]
        
        # Neon Avatar Ring
        ring_col = NEON_CYAN if speaking else (NEON_YEL if p.get("is_me") else (60,70,90))
        d.ellipse([cx-ar-int(4*s), cy-ar-int(4*s), cx+ar+int(4*s), cy+ar+int(4*s)], outline=ring_col, width=max(2, int(2*s)))
        d.ellipse([cx-ar, cy-ar, cx+ar, cy+ar], fill=col)
        
        photo = p.get("photo") or ""
        if photo and os.path.exists(photo):
            self._ops.append((photo, cx-ar, cy-ar, ar*2))
        else:
            d.text((cx, cy), name[:2].upper(), font=_font(int(ar*0.8), True), fill=TEXT, anchor="mm")
            
        # Name & Mic
        d.text((x+int(12*s), y+h-int(22*s)), name, font=self.f_name, fill=TEXT, anchor="lm")
        
        mr = int(10*s)
        mx, my = x+w-int(20*s), y+h-int(22*s)
        d.ellipse([mx-mr, my-mr, mx+mr, my+mr], fill=NEON_PINK if muted else NEON_GRN)
        
        # Small mic icon
        q = int(4*s)
        if muted:
            d.line([mx-q,my-q,mx+q,my+q], fill=TEXT, width=max(2,int(2*s)))
            d.line([mx-q,my+q,mx+q,my-q], fill=TEXT, width=max(2,int(2*s)))
        else:
            d.rounded_rectangle([mx-q,my-int(5*s),mx+q,my+int(2*s)], radius=q, fill=TEXT)

    def _right_panel(self, d, st, now, x0, y0, x1, y1):
        s = self.s
        d.rectangle([x0, y0, x1, y1], fill=PANEL)
        d.line([x0, y0, x0, y1], fill=(30, 40, 60))
        
        pad = int(20*s)
        cx = (x0+x1)//2
        y = y0 + pad
        
        # Header
        d.text((cx, y), "LIVE MIC", font=self.f_title, fill=NEON_CYAN, anchor="mm")
        d.line([x0+pad, y+int(16*s), x1-pad, y+int(16*s)], fill=(30, 40, 60), width=1)
        y += int(36*s)
        
        # Glowing Mic Centerpiece
        img_r = int(55*s)
        img_cy = y + img_r
        pulse = 0.5 + 0.5*math.sin(now*4.0)
        
        # Outer rings
        d.ellipse([cx-img_r-int(10*s), img_cy-img_r-int(10*s), cx+img_r+int(10*s), img_cy+img_r+int(10*s)], outline=(255, 20, 120, 100), width=max(1, int(2*s)))
        d.ellipse([cx-img_r, img_cy-img_r, cx+img_r, img_cy+img_r], fill=(20, 30, 45), outline=NEON_CYAN, width=max(2, int(3*s)))
        
        # Mic Icon inside
        m_w, m_h = int(16*s), int(24*s)
        d.rounded_rectangle([cx-m_w, img_cy-m_h, cx+m_w, img_cy+int(10*s)], radius=int(10*s), fill=NEON_CYAN)
        d.arc([cx-int(24*s), img_cy-int(20*s), cx+int(24*s), img_cy+int(24*s)], start=0, end=180, fill=NEON_CYAN, width=max(2,int(4*s)))
        
        y = img_cy + img_r + int(30*s)
        
        # Waveforms (Left & Right of Mic)
        wave_y = img_cy
        for i in range(15):
            offset = i * int(10*s)
            h = int(abs(math.sin(now*5 + i*0.5)) * 30 * s)
            # Left
            d.line([cx - img_r - int(20*s) - offset, wave_y - h, cx - img_r - int(20*s) - offset, wave_y + h], fill=NEON_PINK, width=max(1,int(2*s)))
            # Right
            d.line([cx + img_r + int(20*s) + offset, wave_y - h, cx + img_r + int(20*s) + offset, wave_y + h], fill=NEON_PURP, width=max(1,int(2*s)))

        # Control Buttons (Row)
        bw, bh = int(55*s), int(55*s)
        gap = int(10*s)
        tot_w = 5*bw + 4*gap
        sx = cx - tot_w//2
        by = y
        
        btns = [("Mic", NEON_CYAN), ("Share", (60,70,90)), ("Raise", (60,70,90)), ("Mute", (60,70,90)), ("Gear", (60,70,90))]
        for lbl, col in btns:
            d.rounded_rectangle([sx, by, sx+bw, by+bh], radius=int(8*s), fill=TILE_BG, outline=col)
            if lbl == "Mic":
                d.rounded_rectangle([sx+bw//2-int(4*s), by+bh//2-int(10*s), sx+bw//2+int(4*s), by+bh//2+int(2*s)], radius=int(4*s), fill=TEXT)
            sx += bw + gap
            
        y = by + bh + int(30*s)
        
        # Sliders
        sliders = [("Voice Volume", "85%", 0.85), ("Music Volume", "70%", 0.70)]
        for lbl, val_str, ratio in sliders:
            d.text((x0+pad, y), lbl, font=self.f_small, fill=TEXT, anchor="lm")
            d.text((x1-pad, y), val_str, font=self.f_small, fill=NEON_CYAN, anchor="rm")
            y += int(14*s)
            track_w = (x1-x0) - 2*pad
            d.line([x0+pad, y+int(2*s), x1-pad, y+int(2*s)], fill=(30,40,60), width=max(2,int(2*s)))
            d.line([x0+pad, y+int(2*s), x0+pad+int(track_w*ratio), y+int(2*s)], fill=NEON_CYAN, width=max(2,int(2*s)))
            kx = x0+pad+int(track_w*ratio)
            d.ellipse([kx-int(5*s), y-int(2*s), kx+int(5*s), y+int(6*s)], fill=TEXT)
            y += int(28*s)

    def _bottom_bar(self, d, st, now, y0, y1):
        W, s = self.w, self.s
        d.rectangle([0, y0, W, y1], fill=PANEL)
        d.line([0, y0, W, y0], fill=(30, 40, 60))
        cy = y0 + (y1-y0)//2
        
        me_name = _sanitize_name(st.get("me_name") or "Owner")
        d.text((int(20*s), cy), f"{me_name} VC", font=self.f_title, fill=NEON_CYAN, anchor="lm")
        
        d.text((W//2, cy), "Talk Loud  •  Stay Active", font=self.f_title, fill=TEXT, anchor="mm")

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
