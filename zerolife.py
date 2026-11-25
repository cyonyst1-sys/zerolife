#!/usr/bin/env python3
"""Zero Life - a little man who lives in your CardputerZero.

An isometric studio loft the size of the screen. He eats, watches TV, sleeps,
showers, goes to work and comes home. Tilt or shake the device and his fish
tank sloshes, things slide off shelves and break, and the lights flicker.
Move the cursor onto a broken thing's empty spot and press Enter to replace it.

Runs on the device (framebuffer + evdev keys + BMI270) and on a PC (window,
mouse, keys simulate tilt).  Only dependency: pygame.
"""
import argparse
import glob
import json
import math
import os
import random
import signal
import struct
import sys
import time

W, H = 320, 170
VERSION = "0.4.0"

# --------------------------------------------------------------------------
# choose output mode before pygame is imported
# --------------------------------------------------------------------------
ap = argparse.ArgumentParser(description="Zero Life")
ap.add_argument("--fb", nargs="?", const="/dev/fb0", default=None, help="draw to a Linux framebuffer")
ap.add_argument("--window", action="store_true", help="force a desktop window")
ap.add_argument("--scale", type=int, default=3, help="window scale")
ap.add_argument("--rotate", type=int, default=None, help="framebuffer rotation 0/90/180/270")
ap.add_argument("--kbd", default=None, help="evdev keyboard device")
ap.add_argument("--no-imu", action="store_true")
ap.add_argument("--i2c", default="/dev/i2c-1")
ap.add_argument("--fresh", action="store_true", help="ignore saved state")
ap.add_argument("--mute", action="store_true", help="no sound")
ap.add_argument("--season", default=None, help="spring, summer, autumn or winter (default: by the calendar)")
ap.add_argument("--weather", default=None, metavar="LAT,LON",
                help="show the real weather at this place (asks api.open-meteo.com; needs internet)")
# test helpers
ap.add_argument("--shot", default=None, help=argparse.SUPPRESS)
ap.add_argument("--frames", type=int, default=0, help=argparse.SUPPRESS)
ap.add_argument("--tilt", default="0,0", help=argparse.SUPPRESS)
ap.add_argument("--tiltat", default="0-999999", help=argparse.SUPPRESS)
ap.add_argument("--shakeat", default="", help=argparse.SUPPRESS)
ap.add_argument("--act", default="", help=argparse.SUPPRESS)
ap.add_argument("--seed", type=int, default=None, help=argparse.SUPPRESS)
ARGS = ap.parse_args()

HEADLESS = bool(ARGS.shot)
# Installed as a CardputerZero app: always draw straight to the screen, whatever
# display variables the launcher happens to pass on.
INSTALLED = os.path.abspath(__file__).startswith("/usr/share/APPLaunch/")
USE_FB = False
if not HEADLESS and not ARGS.window:
    if ARGS.fb:
        USE_FB = True
    elif (sys.platform.startswith("linux") and os.path.exists("/dev/fb0")
          and (INSTALLED or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")))):
        USE_FB = True
        ARGS.fb = "/dev/fb0"
if USE_FB or HEADLESS:
    os.environ["SDL_VIDEODRIVER"] = "dummy"
if HEADLESS:
    os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

try:
    import pygame
except ImportError:
    sys.stderr.write("Zero Life needs pygame:  sudo apt install python3-pygame\n")
    sys.exit(1)

# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
# ---- sound: a handful of noises, made from arithmetic so there are no files to ship ----
SND = {}
_snd_t = {}


def snd_init():
    if HEADLESS or ARGS.mute:
        return
    try:
        import array
        pygame.mixer.init(22050, -16, 1, 512)
        rnd = random.Random(1)

        def make(dur, fn):
            n = int(22050 * dur)
            return pygame.mixer.Sound(buffer=array.array("h", [int(max(-1, min(1, fn(i / 22050.0, i / float(n)))) * 9000)
                                                                for i in range(n)]).tobytes())
        sin, tau = math.sin, 2 * math.pi
        for i, f in enumerate((330, 392, 440, 523)):
            SND["talk%d" % i] = make(0.07, lambda t, u, f=f: sin(tau * f * t) * (1 - u))
        SND["crash"] = make(0.4, lambda t, u: rnd.uniform(-1, 1) * (1 - u) ** 2)
        SND["thud"] = make(0.16, lambda t, u: sin(tau * 80 * t) * (1 - u) * 1.6)
        SND["splash"] = make(0.3, lambda t, u: rnd.uniform(-1, 1) * sin(u * 3.14) * 0.5)
        SND["coin"] = make(0.22, lambda t, u: sin(tau * (988 if u < 0.4 else 1319) * t) * (1 - u) * 0.7)
        SND["knock"] = make(0.09, lambda t, u: sin(tau * 190 * t) * (1 - u) * 1.4)
        SND["bark"] = make(0.14, lambda t, u: (sin(tau * 420 * t) + rnd.uniform(-0.5, 0.5)) * sin(u * 3.14) * 0.9)
        SND["cheer"] = make(0.7, lambda t, u: rnd.uniform(-1, 1) * sin(u * 3.14) * 0.45)
        SND["car"] = make(1.6, lambda t, u: (sin(tau * 62 * t) + 0.5 * sin(tau * 93 * t)) * 0.5 * sin(u * 3.14))
        SND["click"] = make(0.03, lambda t, u: 1 - u)
    except Exception:
        SND.clear()


def play(name, gap=0.0):
    """Play a sound if there is one; `gap` is the least seconds between repeats."""
    s = SND.get(name)
    if s:
        now = time.time()
        if now - _snd_t.get(name, 0) >= gap:
            _snd_t[name] = now
            s.play()


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def lerp(a, b, t):
    return a + (b - a) * t


def shade(c, f):
    return (clamp(int(c[0] * f), 0, 255), clamp(int(c[1] * f), 0, 255), clamp(int(c[2] * f), 0, 255))


def mix(a, b, t):
    return (int(lerp(a[0], b[0], t)), int(lerp(a[1], b[1], t)), int(lerp(a[2], b[2], t)))


SEASON = ARGS.season or ("winter", "winter", "spring", "spring", "spring", "summer", "summer", "summer", "autumn",
                           "autumn", "autumn", "winter")[time.localtime().tm_mon - 1]
LEAF = {"autumn": ((255, 172, 60), (255, 122, 40), (224, 84, 30), (255, 214, 116)),
        "spring": ((255, 170, 200), (250, 130, 170), (255, 214, 230), (170, 220, 140)),
        "summer": ((110, 200, 110), (70, 160, 90), (150, 220, 120), (60, 130, 80)),
        "winter": ((230, 236, 250), (200, 212, 236), (255, 255, 255), (180, 196, 226))}[SEASON]


def seasonal(c):
    """Recolour an autumn-leaf colour for the time of year."""
    return {"autumn": c, "spring": mix(c, (240, 150, 190), 0.6), "summer": mix(c, (60, 140, 70), 0.78),
            "winter": mix(c, (226, 232, 246), 0.82)}[SEASON]


# ---- isometric projection: x runs right-down, y runs left-down, z is up ----
OX, OY, KX, KY, KZ = 133, 58, 1.1, 1.1 / 3.0, 5.5 / 6.0
RX, RY, RH = 150, 100, 60          # room size in world units


def P(x, y, z=0.0):
    return (int(round(OX + (x - y) * KX)), int(round(OY + (x + y) * KY - z * KZ)))


INK = (30, 24, 44)
WALL_L = (208, 198, 180)
BRICK = (172, 96, 74)
FLOOR = (200, 138, 84)
KEY = (255, 0, 255)
GRASS = (92, 164, 104)
STONE = (186, 188, 204)
WHITE = (255, 255, 255)
WOOD = (150, 96, 60)
TEAL = (78, 176, 164)
PLUM = (176, 84, 100)


def poly(surf, col, pts, outline=None):
    pygame.draw.polygon(surf, col, pts)
    if outline:
        pygame.draw.polygon(surf, outline, pts, 1)


# ---- 3x5 pixel font ----
_F = {
    "A": "010101111101101", "B": "110101110101110", "C": "011100100100011", "D": "110101101101110",
    "E": "111100110100111", "F": "111100110100100", "G": "011100101101011", "H": "101101111101101",
    "I": "111010010010111", "J": "001001001101010", "K": "101101110101101", "L": "100100100100111",
    "M": "101111111101101", "N": "101111111111101", "O": "111101101101111", "P": "110101110100100",
    "Q": "111101101111001", "R": "110101110101101", "S": "011100010001110", "T": "111010010010010",
    "U": "101101101101111", "V": "101101101101010", "W": "101101111111101", "X": "101101010101101",
    "Y": "101101010010010", "Z": "111001010100111", "0": "010101101101010", "1": "010110010010111",
    "2": "110001010100111", "3": "110001010001110", "4": "101101111001001", "5": "111100110001110",
    "6": "011100110101010", "7": "111001010010010", "8": "010101010101010", "9": "010101011001110",
    ".": "000000000000010", ",": "000000000010100", "!": "010010010000010", "?": "110001010000010",
    "'": "010010000000000", "-": "000000111000000", "+": "000010111010000", ":": "000010000010000",
    "*": "000101010101000", "/": "001001010100100", "$": "011110010011110", ">": "100010001010100", "_": "000000000000111",
}


def text(surf, s, x, y, col):
    for ch in s.upper():
        g = _F.get(ch)
        if g:
            for i, bit in enumerate(g):
                if bit == "1":
                    surf.fill(col, (x + i % 3, y + i // 3, 1, 1))
        x += 4


def draw_bubble(surf, speech, hx, hy):
    lines = wrap(speech)
    bw = max(len(l) for l in lines) * 4 + 5
    bh = len(lines) * 6 + 5
    bx = clamp(hx - bw // 2, 2, W - bw - 2)
    by = clamp(hy - bh - 5, 2, H - bh - 2)
    pygame.draw.rect(surf, (252, 250, 240), (bx, by, bw, bh), border_radius=2)
    pygame.draw.rect(surf, INK, (bx, by, bw, bh), 1, border_radius=2)
    tx = clamp(hx, bx + 3, bx + bw - 4)
    pygame.draw.polygon(surf, (252, 250, 240), [(tx - 2, by + bh - 1), (tx + 2, by + bh - 1), (tx, by + bh + 3)])
    pygame.draw.line(surf, INK, (tx - 2, by + bh), (tx, by + bh + 3))
    pygame.draw.line(surf, INK, (tx + 2, by + bh), (tx, by + bh + 3))
    for i, l in enumerate(lines):
        text(surf, l, bx + 3, by + 3 + i * 6, INK)


def wrap(s, n=17):
    out, line = [], ""
    for word in s.split():
        if line and len(line) + 1 + len(word) > n:
            out.append(line)
            line = word
        else:
            line = (line + " " + word).strip()
    if line:
        out.append(line)
    return out


def box(surf, x0, y0, z0, x1, y1, z1, col, outline=None, top=None, bands=3):
    """Axis-aligned box.  The side faces darken toward the floor, the two top
    edges that face the viewer get a highlight, and the outline is a dark shade
    of the box's own colour rather than black."""
    edge = outline or shade(col, 0.42)
    tall = (z1 - z0) * KZ
    n = bands if tall >= 9 else (min(bands, 2) if tall >= 5 else 1)
    for f, ax, ay, bx, by in ((0.9, x0, y1, x1, y1), (0.64, x1, y1, x1, y0)):
        for i in range(n):
            za = z1 - (z1 - z0) * i / n
            zb = z1 - (z1 - z0) * (i + 1) / n
            pygame.draw.polygon(surf, shade(col, f * (1 - 0.1 * i)),
                                [P(ax, ay, za), P(bx, by, za), P(bx, by, zb), P(ax, ay, zb)])
        pygame.draw.polygon(surf, edge, [P(ax, ay, z1), P(bx, by, z1), P(bx, by, z0), P(ax, ay, z0)], 1)
    t = top or shade(col, 1.14)
    pts = [P(x0, y0, z1), P(x1, y0, z1), P(x1, y1, z1), P(x0, y1, z1)]
    pygame.draw.polygon(surf, t, pts)
    pygame.draw.polygon(surf, edge, pts, 1)
    hi = shade(t, 1.2)
    pygame.draw.line(surf, hi, pts[3], pts[2])
    pygame.draw.line(surf, hi, pts[2], pts[1])


def wall_rect(surf, col, axis, a0, a1, z0, z1, outline=None, off=0.0):
    """A rectangle lying on a wall.  axis 'x': on the left wall (x = off), a is y.
    axis 'y': on the right wall (y = off), a is x."""
    if axis == "x":
        pts = [P(off, a0, z0), P(off, a1, z0), P(off, a1, z1), P(off, a0, z1)]
    else:
        pts = [P(a0, off, z0), P(a1, off, z0), P(a1, off, z1), P(a0, off, z1)]
    poly(surf, col, pts, outline)


def hull(pts):
    """Convex hull of screen points (for shadow shapes)."""
    pts = sorted(set(pts))
    if len(pts) < 3:
        return pts

    def half(seq):
        h = []
        for p in seq:
            while len(h) >= 2 and ((h[-1][0] - h[-2][0]) * (p[1] - h[-2][1])
                                   - (h[-1][1] - h[-2][1]) * (p[0] - h[-2][0])) <= 0:
                h.pop()
            h.append(p)
        return h
    lo, up = half(pts), half(reversed(pts))
    return lo[:-1] + up[:-1]


OUTLINE = (24, 18, 38)


def finish(s, outline=True):
    """Rim-shade a little sprite (light from the upper left) and give it a dark outline."""
    w, h = s.get_size()
    src = [[tuple(s.get_at((x, y))) for x in range(w)] for y in range(h)]

    def solid(x, y):
        return 0 <= x < w and 0 <= y < h and src[y][x][3] > 0
    out = pygame.Surface((w + 2, h + 2), pygame.SRCALPHA)
    for y in range(-1, h + 1):
        for x in range(-1, w + 1):
            if solid(x, y):
                c = src[y][x][:3]
                if not solid(x + 1, y):
                    c = shade(c, 0.78)
                elif not solid(x, y + 1):
                    c = shade(c, 0.88)
                elif not solid(x - 1, y) or not solid(x, y - 1):
                    c = shade(c, 1.1)
                out.set_at((x + 1, y + 1), c)
            elif outline and (solid(x - 1, y) or solid(x + 1, y) or solid(x, y - 1) or solid(x, y + 1)):
                out.set_at((x + 1, y + 1), OUTLINE)
    return out


_glows = {}


def glow(rad, col):
    key = (rad, col)
    if key not in _glows:
        g = pygame.Surface((rad * 2, rad * 2))
        for i in range(rad, 0, -1):
            f = (1 - i / rad) ** 2
            pygame.draw.circle(g, shade(col, f), (rad, rad), i)
        _glows[key] = g
    return _glows[key]


def add_glow(surf, pos, rad, col):
    surf.blit(glow(rad, col), (pos[0] - rad, pos[1] - rad), special_flags=pygame.BLEND_RGB_ADD)


_lights = {}


def checker():
    """A big black-and-white pixel checkerboard, used to dither the light."""
    c = _lights.get("checker")
    if c is None:
        c = pygame.Surface((256, 256))
        c.fill(WHITE, (0, 0, 1, 1))
        c.fill(WHITE, (1, 1, 1, 1))
        n = 2
        while n < 256:
            for at in ((n, 0), (0, n), (n, n)):
                c.blit(c, at, (0, 0, n, n))
            n *= 2
        _lights["checker"] = c
    return c


def add_light(surf, pos, rx, ry, col, steps=11):
    """Add a pool of light to the light map: bands of brightness with a
    checkerboard-dithered ring between each pair, so it reads as pixel art.
    Only call this with a fixed set of sizes and colours: each one is cached."""
    key = (rx, ry, col, steps)
    g = _lights.get(key)
    if g is None:
        g = pygame.Surface((rx * 2, ry * 2))
        tmp = pygame.Surface((rx * 2, ry * 2))
        chk = checker()
        n = steps * 2
        prev = (0, 0, 0)
        for k in range(steps):
            now = shade(col, ((k + 1) / steps) ** 1.6)
            d = (now[0] - prev[0], now[1] - prev[1], now[2] - prev[2])
            prev = now
            fo, fi = 1 - (2 * k) / n, 1 - (2 * k + 1) / n
            tmp.fill((0, 0, 0))
            pygame.draw.ellipse(tmp, d, (int(rx - rx * fo), int(ry - ry * fo), int(2 * rx * fo), int(2 * ry * fo)))
            tmp.blit(chk, (0, 0), special_flags=pygame.BLEND_RGB_MULT)
            pygame.draw.ellipse(tmp, d, (int(rx - rx * fi), int(ry - ry * fi), int(2 * rx * fi), int(2 * ry * fi)))
            g.blit(tmp, (0, 0), special_flags=pygame.BLEND_RGB_ADD)
        _lights[key] = g
    surf.blit(g, (pos[0] - rx, pos[1] - ry), special_flags=pygame.BLEND_RGB_ADD)


def gradient(s, stops, y0, y1, bands):
    """Banded, dithered vertical gradient through the colour stops."""
    n = len(stops) - 1

    def col(k):
        u = min(k, bands) / bands * n
        j = min(n - 1, int(u))
        return mix(stops[j], stops[j + 1], u - j)
    for y in range(y0, y1):
        t = (y - y0) / max(1, y1 - y0) * bands
        i = int(t)
        s.fill(col(i), (0, y, W, 1))
        if t - i > 0.62:
            c1 = col(i + 1)
            for x in range(y & 1, W, 2):
                s.fill(c1, (x, y, 1, 1))


def hang(surf, piece, at, sway):
    """Draw something that hangs from its top edge, swung `sway` pixels sideways at the bottom."""
    w, h = piece.get_size()
    n = max(1, h // 3)
    for i in range(n):
        y0, y1 = i * h // n, (i + 1) * h // n
        surf.blit(piece, (at[0] + int(round(sway * i / max(1, n - 1))), at[1] + y0), (0, y0, w, y1 - y0))


REAL_WEATHER = [None]         # set by a background thread when --weather LAT,LON is given


def weather_thread(where):
    import threading
    import urllib.request

    def run():
        while True:
            try:
                lat, lon = where.split(",")
                url = "https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s&current=weather_code" % (lat.strip(), lon.strip())
                code = json.loads(urllib.request.urlopen(url, timeout=10).read().decode())["current"]["weather_code"]
                REAL_WEATHER[0] = "snow" if code in (71, 73, 75, 77, 85, 86) else ("rain" if code >= 51 else "clear")
            except Exception:
                pass
            time.sleep(1800)
    threading.Thread(target=run, daemon=True).start()


_shadows = {}


def cast_shadow(surf, p0, vx, vy, w):
    """Darken the ground from screen point p0 along (vx, vy): the shadow of
    something standing there, soft at the edge, multiplied into the picture."""
    vx = int(clamp(vx, -20, 20) / 2) * 2
    vy = int(clamp(vy, -10, 10) / 2) * 2
    key = (vx, vy, w)
    hit = _shadows.get(key)
    if hit is None:
        hw, hh = w // 2 + 2, w // 6 + 3
        s = pygame.Surface((abs(vx) + 2 * hw + 1, abs(vy) + 2 * hh + 1))
        s.fill(WHITE)
        ox, oy = hw - min(0, vx), hh - min(0, vy)
        n = max(abs(vx), abs(vy), 1)
        for col, grow in (((198, 198, 216), 1), ((138, 138, 172), 0)):
            for i in range(n + 1):
                cx, cy = ox + int(vx * i / n), oy + int(vy * i / n)
                ew, eh = max(3, int(w * (1 - 0.35 * i / n))) + 2 * grow, w // 3 + 1 + 2 * grow
                pygame.draw.ellipse(s, col, (cx - ew // 2, cy - eh // 2, ew, eh))
        hit = _shadows[key] = (s, ox, oy)
    s, ox, oy = hit
    surf.blit(s, (p0[0] - ox, p0[1] - oy), special_flags=pygame.BLEND_RGB_MULT)


# --------------------------------------------------------------------------
# the furniture.  SORTED boxes are drawn back-to-front with moving things
# slotted between them; SOLIDS are what loose things bump into.
# --------------------------------------------------------------------------
LOFT = (0, 0, 0, 56, 60, 30)
BED = (4, 8, 30, 30, 46, 37)
FRIDGE = (60, 0, 0, 74, 12, 30)
COUNTER = (76, 0, 0, 100, 12, 14)
SHELF = (80, 0, 27, 100, 6, 30)
TVSTAND = (102, 0, 0, 126, 10, 8)
TANKSTAND = (0, 64, 0, 14, 96, 12)
TANK = (1, 65, 12, 13, 95, 28)
ARM_L = (100, 40, 0, 103, 55, 11)
SEAT = (103, 40, 0, 125, 51, 7)
ARM_R = (125, 40, 0, 128, 55, 11)
BACK = (103, 51, 0, 125, 55, 15)
ORDER = ["fridge", "counter", "shelf", "tvstand", "tankstand", "tank", "arm_l", "seat", "arm_r", "back"]
BOXES = {"fridge": FRIDGE, "counter": COUNTER, "shelf": SHELF, "tvstand": TVSTAND, "tankstand": TANKSTAND,
         "tank": TANK, "arm_l": ARM_L, "seat": SEAT, "arm_r": ARM_R, "back": BACK}
SOLIDS = [LOFT, BED, FRIDGE, COUNTER, SHELF, TVSTAND, (0, 64, 0, 14, 96, 28), ARM_L, SEAT, ARM_R, BACK]

GRAV = 400.0


class Body:
    def __init__(self, x, y, z, r=2.0, h=5.0, mu=0.4, rest=0.25):
        self.x, self.y, self.z = float(x), float(y), float(z)
        self.vx = self.vy = self.vz = 0.0
        self.r, self.h, self.mu, self.rest = r, h, mu, rest
        self.ang = 0.0
        self.touch = True

    def speed(self):
        return math.sqrt(self.vx ** 2 + self.vy ** 2 + self.vz ** 2)


def step_body(b, acc, dt, sub=3):
    """Advance a loose thing; returns the hardest impact speed this frame."""
    h = dt / sub
    hit = 0.0
    touched = False
    for _ in range(sub):
        p = [b.x + (b.vx + acc[0] * h) * h, b.y + (b.vy + acc[1] * h) * h, b.z + (b.vz + acc[2] * h) * h]
        v = [b.vx + acc[0] * h, b.vy + acc[1] * h, b.vz + acc[2] * h]

        def contact(axis, sgn, depth):
            nonlocal hit, touched
            touched = True
            p[axis] += sgn * depth
            vn = v[axis] * sgn
            if vn < 0:
                hit = max(hit, -vn)
                v[axis] = -v[axis] * b.rest if -vn > 40 else 0.0
            press = -acc[axis] * sgn
            if press > 0:
                f = b.mu * press * h
                t1, t2 = [a for a in (0, 1, 2) if a != axis]
                sp = math.hypot(v[t1], v[t2])
                if sp <= f:
                    v[t1] = v[t2] = 0.0
                else:
                    k = (sp - f) / sp
                    v[t1] *= k
                    v[t2] *= k

        lim = ((b.r, RX - b.r), (b.r, RY - b.r), (0.0, RH - b.h))
        for a in (0, 1, 2):
            if p[a] < lim[a][0]:
                contact(a, 1, lim[a][0] - p[a])
            elif p[a] > lim[a][1]:
                contact(a, -1, p[a] - lim[a][1])
        for (x0, y0, z0, x1, y1, z1) in SOLIDS:
            if x0 - b.r < p[0] < x1 + b.r and y0 - b.r < p[1] < y1 + b.r and z0 - b.h < p[2] < z1:
                pens = ((p[0] - (x0 - b.r), 0, -1), ((x1 + b.r) - p[0], 0, 1),
                        (p[1] - (y0 - b.r), 1, -1), ((y1 + b.r) - p[1], 1, 1),
                        (p[2] - (z0 - b.h), 2, -1), (z1 - p[2], 2, 1))
                d, a, s = min(pens)
                contact(a, s, d)
        b.x, b.y, b.z = p
        b.vx, b.vy, b.vz = v
    b.touch = touched
    if touched:
        b.ang *= 0.6
    else:
        b.ang = (b.ang + (abs(b.vx) + abs(b.vy) + 30) * dt * 4) % 360
    return hit


# --------------------------------------------------------------------------
# sprites
# --------------------------------------------------------------------------
SKIN = (240, 194, 152)
HAIR = (72, 44, 30)
OUTFITS = {
    "home": ((226, 96, 70), (62, 72, 124)),
    "work": ((240, 240, 246), (52, 52, 64)),
    "pj": ((150, 172, 232), (120, 144, 214)),
}
_sprites = {}


def head_sprite(view, face, expr, k=1, rot=0, skin=None, hair=None):
    """His head: side / front / back, with a few expressions, outlined and cached."""
    skin, hair = skin or SKIN, hair or HAIR
    key = ("head", view, face, expr, k, rot, skin, hair)
    if key in _sprites:
        return _sprites[key]
    s = pygame.Surface((7, 6), pygame.SRCALPHA)
    sk_d, ha_l, ha_d, lip = shade(skin, 0.8), shade(hair, 1.6), shade(hair, 0.7), (178, 98, 88)
    R = lambda c, x, y, w, h: s.fill(c, (x, y, w, h))
    if view == "side":
        R(skin, 1, 2, 5, 4)
        R(skin, 6, 3, 1, 1)                      # nose
        R(hair, 1, 0, 5, 2)
        R(hair, 0, 1, 2, 3)
        R(hair, 2, 2, 2, 1)
        R(ha_l, 2, 0, 3, 1)
        R(ha_d, 0, 3, 1, 1)
        R(sk_d, 2, 3, 1, 1)                      # ear
        R(sk_d, 1, 4, 2, 2)                      # jaw
        if expr in ("blink", "sleep"):
            R(sk_d, 4, 3, 1, 1)
        elif expr == "dizzy":
            R(INK, 3, 3, 2, 1)
        else:
            R(INK, 4, 3, 1, 1)
        R(lip, 5, 5, 1, 1)
        if expr == "open":
            R((110, 46, 50), 4, 5, 2, 1)
        if face < 0:
            s = pygame.transform.flip(s, True, False)
    elif view == "front":
        R(skin, 0, 2, 7, 4)
        R(sk_d, 0, 5, 1, 1)
        R(sk_d, 6, 5, 1, 1)
        R((240, 160, 140), 1, 4, 1, 1)           # cheeks
        R((240, 160, 140), 5, 4, 1, 1)
        R(hair, 0, 0, 7, 2)
        R(hair, 0, 2, 1, 2)
        R(hair, 6, 2, 1, 2)
        R(hair, 1, 2, 2, 1)
        R(ha_l, 2, 0, 3, 1)
        if expr == "dizzy":
            R(INK, 1, 3, 2, 1)
            R(INK, 4, 3, 2, 1)
        elif expr in ("blink", "sleep"):
            R(sk_d, 1, 3, 2, 1)
            R(sk_d, 4, 3, 2, 1)
        else:
            R(INK, 2, 3, 1, 1)
            R(INK, 4, 3, 1, 1)
        if expr == "open":
            R((110, 46, 50), 2, 5, 3, 1)
        else:
            R(lip, 3, 5, 1, 1)
    else:
        R(hair, 0, 0, 7, 5)
        R(ha_l, 1, 1, 3, 1)
        R(ha_d, 0, 4, 7, 1)
        R(sk_d, 1, 5, 5, 1)
    s = finish(s)
    if k != 1:
        s = pygame.transform.scale(s, (s.get_width() * k, s.get_height() * k))
    if rot:
        s = pygame.transform.rotate(s, rot * 90)
    _sprites[key] = s
    return s


ARM, LEG, TORSO = 5.0, 5.5, 8.0      # limb segment and body lengths in pixels: he is 25 pixels tall
_fig = {}


def draw_figure(surf, J, view, face, k, outfit, carry, expr, rot=0, legs=True, look=None):
    """Draw the man from his joint positions J (screen pixels): two-segment
    arms and legs, a body, a head, and a one-pixel dark outline round the lot."""
    if k not in _fig:
        _fig[k] = (pygame.Surface((72 * k, 72 * k), pygame.SRCALPHA), pygame.Surface((72 * k, 72 * k), pygame.SRCALPHA))
    fig, sil = _fig[k]
    fig.fill((0, 0, 0, 0))
    half = 36 * k
    ox = half - int(round((J["neck"][0] + J["hip_n"][0]) / 2))
    oy = half - int(round((J["neck"][1] + J["hip_n"][1]) / 2))
    shirt, pants = OUTFITS[outfit]
    skin, hair = SKIN, HAIR
    if look:
        shirt, pants, skin, hair = look["shirt"], look["pants"], look["skin"], look["hair"]
    fore = skin if outfit == "home" else shirt
    shoe = (54, 42, 48)
    w = 2 * k

    def T(p):
        return (int(round(p[0])) + ox, int(round(p[1])) + oy)

    def ln(a, b, col, width=w):
        a, b = T(a), T(b)
        pygame.draw.line(fig, col, a, b, width)
        h = width // 2
        fig.fill(col, (a[0] - h, a[1] - h, width, width))
        fig.fill(col, (b[0] - h, b[1] - h, width, width))

    def arm(side, f):
        ln(J["sh_" + side], J["el_" + side], shade(shirt, f))
        ln(J["el_" + side], J["ha_" + side], shade(fore, f))
        x, y = T(J["ha_" + side])
        fig.fill(shade(skin, f), (x - k, y - k, w, w))

    def leg(side, f, toe):
        ln(J["hip_" + side], J["kn_" + side], shade(pants, f))
        ln(J["kn_" + side], J["ft_" + side], shade(pants, f * 0.92))
        x, y = T(J["ft_" + side])
        fig.fill(shade(shoe, 0.6 + f * 0.4), (x - k + (0 if toe >= 0 else -k), y - k, 3 * k, w))
        fig.fill((120, 104, 108), (x + (2 * k - 1 if toe >= 0 else -2 * k), y - k, 1, 1))

    def head():
        hs = head_sprite(view, face, expr, k, rot, skin, hair)
        x, y = T(J["head"])
        fig.blit(hs, (x - hs.get_width() // 2, y - hs.get_height() // 2))

    hip, neck = T(J["hip_n"]), T(J["neck"])
    if view == "side":
        f = face
        arm("f", 0.7)
        if legs:
            leg("f", 0.72, f)
        ln(J["hip_n"], J["neck"], shirt, 5 * k)
        pygame.draw.line(fig, shade(shirt, 0.74), (hip[0] - 2 * k * f, hip[1]), (neck[0] - 2 * k * f, neck[1]), k)
        pygame.draw.line(fig, shade(shirt, 1.14), (hip[0] + 2 * k * f, hip[1] - k), (neck[0] + 2 * k * f, neck[1] + k), k)
        fig.fill(pants, (hip[0] - 2 * k, hip[1] - k, 5 * k, 3 * k))
        fig.fill(shade(pants, 0.6), (hip[0] - 2 * k, hip[1] - k, 5 * k, k))
        if outfit == "work":
            fig.fill((200, 44, 44), (neck[0] + 2 * k * f - (k - 1) * (f > 0), neck[1] + k, k, 4 * k))
        elif outfit == "pj":
            for d in (2, 5):
                pygame.draw.line(fig, shade(shirt, 0.76), (neck[0] - 2 * k, neck[1] + d * k), (neck[0] + 2 * k, neck[1] + d * k), k)
        else:
            fig.fill((250, 220, 120), (neck[0] + k * f - (k - 1) * (f > 0), neck[1] + 3 * k, k, w))
        if legs:
            leg("n", 1.0, f)
        head()
        arm("n", 1.0)
        if carry:
            x, y = T(J["ha_n"])
            if carry == "bag":
                fig.fill((186, 140, 90), (x - 2 * k, y, 5 * k, 6 * k))
                fig.fill((150, 108, 66), (x + 2 * k, y, k, 6 * k))
                fig.fill((214, 170, 116), (x - 2 * k, y, 5 * k, k))
                fig.fill((120, 186, 90), (x - k, y - 2 * k, w, w))
                fig.fill((232, 196, 120), (x + k, y - 3 * k, k, 3 * k))
            else:
                fig.fill((236, 224, 120), (x - k, y - k, 3 * k, 3 * k))
                fig.fill((250, 244, 190), (x - k, y - k, w, k))
    else:
        if legs:
            leg("f", 1.0, -1)
            leg("n", 0.84, 1)
        if view == "back":
            arm("f", 0.8)
            arm("n", 0.7)
        body = [(neck[0] - 3 * k - k // 2, neck[1]), (neck[0] + 3 * k + k // 2, neck[1]),
                (hip[0] + 3 * k, hip[1] + k), (hip[0] - 3 * k, hip[1] + k)]
        pygame.draw.polygon(fig, shirt, body)
        pygame.draw.polygon(fig, shade(shirt, 0.76), [(neck[0] + k, neck[1] + k), body[1], body[2], (hip[0] + k, hip[1] + k)])
        pygame.draw.line(fig, shade(shirt, 1.14), body[0], body[1], k)
        fig.fill(pants, (hip[0] - 3 * k, hip[1] - k, 6 * k + 1, w + 1))
        fig.fill(shade(pants, 0.6), (hip[0] - 3 * k, hip[1] - k, 6 * k + 1, k))
        if outfit == "pj":
            for d in (2, 5):
                pygame.draw.line(fig, shade(shirt, 0.7), (neck[0] - 3 * k, neck[1] + d * k), (neck[0] + 3 * k, neck[1] + d * k), k)
        if view == "front":
            if outfit == "work":
                fig.fill((250, 250, 255), (neck[0] - k, neck[1], 3 * k, k))
                fig.fill((200, 44, 44), (neck[0], neck[1] + k, k, 5 * k))
            elif outfit == "home":
                fig.fill((250, 220, 120), (neck[0] - k, neck[1] + 2 * k, w, w))
            arm("f", 1.0)
            arm("n", 0.84)
        else:
            pygame.draw.line(fig, shade(shirt, 0.82), (neck[0], neck[1] + w), (hip[0], hip[1] - w), k)
        head()
    # outline: the figure's silhouette in the dark colour, nudged one pixel each way
    pygame.mask.from_surface(fig).to_surface(sil, setcolor=OUTLINE + (255,), unsetcolor=(0, 0, 0, 0))
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        surf.blit(sil, (dx - ox, dy - oy))
    surf.blit(fig, (-ox, -oy))


def item_sprite(name):
    key = ("item", name)
    if key in _sprites:
        return _sprites[key]
    if name == "vase":
        s = pygame.Surface((7, 12), pygame.SRCALPHA)
        s.fill((70, 140, 76), (3, 3, 1, 4))          # stem and a leaf
        s.fill((96, 176, 96), (4, 4, 2, 1))
        s.fill((240, 120, 160), (2, 0, 3, 3))        # flower
        s.fill((250, 170, 196), (2, 0, 2, 1))
        s.fill((250, 220, 90), (3, 1, 1, 1))
        s.fill((66, 104, 196), (2, 6, 3, 6))         # vase
        s.fill((66, 104, 196), (1, 8, 5, 3))
        s.fill((50, 80, 160), (2, 6, 3, 1))
        s.fill((150, 190, 250), (2, 8, 1, 2))
        s.fill((230, 236, 250), (1, 9, 5, 1))
    elif name == "mug":
        s = pygame.Surface((6, 5), pygame.SRCALPHA)
        s.fill((244, 240, 230), (0, 0, 4, 5))
        s.fill((244, 240, 230), (4, 1, 2, 1))
        s.fill((244, 240, 230), (5, 1, 1, 3))
        s.fill((244, 240, 230), (4, 3, 2, 1))
        s.fill((210, 80, 70), (0, 2, 4, 1))
        s.fill((120, 80, 60), (1, 0, 2, 1))          # coffee
    elif name == "plant":
        s = pygame.Surface((11, 14), pygame.SRCALPHA)
        for (x, y, w, h) in ((4, 0, 3, 9), (1, 2, 3, 6), (7, 3, 3, 5), (0, 5, 2, 2), (9, 1, 2, 3)):
            s.fill((62, 140, 78), (x, y, w, h))
        for (x, y, w, h) in ((5, 1, 1, 6), (2, 3, 1, 3), (8, 4, 1, 3), (9, 1, 1, 1), (0, 5, 1, 1)):
            s.fill((124, 200, 112), (x, y, w, h))
        for (x, y) in ((6, 4), (3, 6), (9, 6), (4, 8)):
            s.fill((40, 100, 66), (x, y, 1, 1))
        s.fill((200, 108, 72), (3, 9, 5, 5))         # pot
        s.fill((226, 138, 96), (2, 9, 7, 1))
        s.fill((164, 82, 56), (3, 10, 5, 1))
        s.fill((226, 138, 96), (4, 11, 1, 2))
    elif name == "bottle":
        s = pygame.Surface((3, 8), pygame.SRCALPHA)
        s.fill((70, 150, 110), (0, 2, 3, 6))
        s.fill((70, 150, 110), (1, 0, 1, 2))
        s.fill((150, 220, 190), (0, 3, 1, 3))
        s.fill((240, 236, 220), (0, 5, 3, 1))
    elif name == "apple":
        s = pygame.Surface((4, 4), pygame.SRCALPHA)
        s.fill((220, 60, 60), (0, 1, 4, 3))
        s.fill((250, 130, 120), (0, 1, 2, 1))
        s.fill((90, 150, 70), (2, 0, 1, 1))
    elif name == "books":
        s = pygame.Surface((8, 7), pygame.SRCALPHA)
        for i, c in enumerate(((176, 70, 66), (70, 110, 170), (214, 170, 80))):
            s.fill(c, (i % 2, i * 2 + 1, 7, 2))
            s.fill((240, 236, 226), (i % 2 + 6, i * 2 + 1, 1, 2))
    else:  # ball
        s = pygame.Surface((7, 7), pygame.SRCALPHA)
        pygame.draw.circle(s, (228, 68, 68), (3, 3), 3)
        s.fill((250, 250, 250), (0, 3, 7, 1))
        s.fill((255, 170, 160), (2, 1, 2, 1))
        s.fill((255, 240, 236), (2, 1, 1, 1))
    s = finish(s)
    _sprites[key] = s
    return s


ITEMS = {
    "vase": dict(home=(84, 6, 14), r=2, h=8, frag=105, mu=0.33, col=[(70, 110, 200), (150, 190, 250), (240, 120, 160)],
                 at=("floor", 86, 17)),
    "mug": dict(home=(92, 3, 30), r=2, h=4, frag=112, mu=0.33, col=[(244, 240, 230), (210, 80, 70)],
                at=("floor", 94, 17)),
    "plant": dict(home=(50, 8, 30), r=3, h=11, frag=120, mu=0.42, col=[(196, 104, 70), (90, 60, 44), (70, 150, 80)],
                  at=("loft", 44, 14)),
    "tv": dict(home=(114, 5, 8), r=3, h=12, frag=128, mu=0.5, col=[(40, 40, 52), (120, 160, 200), (20, 20, 30)],
               at=("floor", 114, 17)),
    "picture": dict(home=(114, 1.5, 40), r=1.5, h=10, frag=90, mu=0.5, col=[(200, 160, 80), (120, 170, 210)],
                    at=("floor", 114, 17)),
    "bottle": dict(home=(97, 8, 14), r=1.5, h=7, frag=135, mu=0.07, col=[(70, 150, 110), (150, 220, 190), (120, 190, 250)],
                   at=("floor", 97, 17)),
    "apple": dict(home=(89, 9, 14), r=1.5, h=3, frag=9e9, mu=0.09, col=[(220, 60, 60)], at=("floor", 89, 17)),
    "books": dict(home=(36, 44, 30), r=3, h=6, frag=9e9, mu=0.4, col=[(176, 70, 66)], at=("loft", 38, 38)),
}
LAMP_AT = (96, 56)
LAMP_L = 16.0
# fairy lights strung along the edge of the loft
FAIRY = ([(2 + i * 6.5, 60.5, 26.5 - (i % 2) * 1.2) for i in range(9)]
         + [(56.5, 56 - i * 6.5, 26.5 - ((i + 1) % 2) * 1.2) for i in range(9)])
FAIRY_S = [P(*p) for p in FAIRY]
# the garden terrace the house stands on: how far it reaches past the walls, its level, its foot
YARD, YZ, BASE = 12, -4, -11
# outline of the room's volume on screen (the light map also covers everything painted in build_room)
HULL = [P(-4, RY, RH), P(-4, -4, RH), P(RX, -4, RH), P(RX, -4, YZ), P(RX, RY, YZ), P(-4, RY, YZ)]
TERRACE = [P(-YARD, RY + YARD, YZ), P(-YARD, -YARD, YZ), P(RX + YARD, -YARD, YZ), P(RX + YARD, -YARD, BASE),
           P(RX + YARD, RY + YARD, BASE), P(-YARD, RY + YARD, BASE)]
LANTERN = (303, 27)                 # where it hangs from the branch of the old tree
FLIES = [(16, 60), (22, 90), (18, 126), (60, 150), (110, 163), (30, 160), (250, 162), (296, 148), (314, 100),
         (300, 62), (252, 22), (214, 26), (160, 8), (60, 24)]
EMBERS = [(20, 128), (52, 146), (92, 163), (296, 150), (268, 161)]
GLAMPS = [(124, 109), (159, 50), (-7, 108), (30, 110.5), (78, 110.5), (159, 98), (159, 18)]    # garden lamps
# wall lights indoors (on with the house lights): where the shade is on screen
SCONCES = [P(127.5, 0, 37), P(34, 0, 46), P(0, 56.5, 45), P(56, 17, 22), P(0, 99, 40)]
STRIP = (P(81, 1.5, 26.6), P(99, 1.5, 26.6))      # the light under the kitchen shelf
PORCH = P(RX + 0.5, -2, 36)                       # outside lamp on the end of the brick wall
OWL = P(-2, 74, RH)
CLOCK = (43, 50)                    # on the brick wall: x, z
CLOCK_FACE = [P(CLOCK[0] + math.cos(i * 0.5236) * 5, 0, CLOCK[1] + math.sin(i * 0.5236) * 5.8) for i in range(12)]
WIN = [P(0, 68, 34), P(0, 92, 34), P(0, 92, 54), P(0, 68, 54)]
BATH_DOOR = [P(56, 23, 0), P(56, 37, 0), P(56, 37, 25), P(56, 23, 25)]
SHAFT = [P(20, 64), P(46, 64), P(52, 98), P(24, 98)]                       # window light on the floor
BEAM = [P(0, 92, 54), P(0, 68, 54), P(46, 64), P(52, 98), P(24, 98), P(0, 92, 34)]   # and in the air
# what throws a shadow in the lamp light: (box, the patch of floor the shadow may fall on)
ROOM_RECT, LOFT_RECT = (0, 0, RX, RY), (0, 0, 56, 60)
CASTERS = [(LOFT, ROOM_RECT), (FRIDGE, ROOM_RECT), (COUNTER, ROOM_RECT), (TVSTAND, ROOM_RECT),
           ((0, 64, 0, 14, 96, 28), ROOM_RECT), ((100, 40, 0, 128, 55, 11), ROOM_RECT), (BACK, ROOM_RECT),
           ((4, 6, 30, 30, 46, 37), LOFT_RECT), ((4, 6, 30, 30, 8, 44), LOFT_RECT)]
# every flat side the lamp can light, back to front: (outline on screen, centre, which way it faces)
FACES = []
for _b in [LOFT, (4, 8, 30, 30, 46, 37), (4, 6, 30, 30, 8, 44)] + [BOXES[_n] for _n in ORDER]:
    _x0, _y0, _z0, _x1, _y1, _z1 = _b
    if _b == TANK:        # glass: lit from inside, takes no shading
        FACES.append((hull([P(x, y, z) for x in (_x0, _x1) for y in (_y0, _y1) for z in (_z0, _z1)]), None, None))
        continue
    FACES.append(([P(_x0, _y1, _z1), P(_x1, _y1, _z1), P(_x1, _y1, _z0), P(_x0, _y1, _z0)],
                  ((_x0 + _x1) / 2, _y1, (_z0 + _z1) / 2), (0, 1, 0)))
    FACES.append(([P(_x1, _y0, _z1), P(_x1, _y1, _z1), P(_x1, _y1, _z0), P(_x1, _y0, _z0)],
                  (_x1, (_y0 + _y1) / 2, (_z0 + _z1) / 2), (1, 0, 0)))
    FACES.append(([P(_x0, _y0, _z1), P(_x1, _y0, _z1), P(_x1, _y1, _z1), P(_x0, _y1, _z1)],
                  ((_x0 + _x1) / 2, (_y0 + _y1) / 2, _z1), (0, 0, 1)))


class Item(Body):
    def __init__(self, name):
        d = ITEMS[name]
        Body.__init__(self, *d["home"], r=d["r"], h=d["h"], mu=d["mu"])
        self.name = name
        self.home = d["home"]
        self.frag = d["frag"]
        self.state = "ok"          # ok | broken
        self.hung = name == "picture"
        self.swing = 0.0
        self.swingv = 0.0
        self.held = False
        self.debris = None
        self.bits = []

    def reset(self):
        self.x, self.y, self.z = self.home
        self.vx = self.vy = self.vz = 0.0
        self.state = "ok"
        self.hung = self.name == "picture"
        self.held = False
        self.ang = 0.0

    def away(self):
        return (self.state == "ok" and not self.hung and not self.held and
                abs(self.x - self.home[0]) + abs(self.y - self.home[1]) + abs(self.z - self.home[2]) > 10)


# --------------------------------------------------------------------------
# the man
# --------------------------------------------------------------------------
NODES = {"H": (62, 30), "K1": (67, 18), "K2": (88, 18), "M": (114, 31), "D": (137, 14), "L0": (62, 66),
         "LD": (46, 66), "T": (21, 80), "F": (120, 86), "R": (139, 60)}
EDGES = [("H", "K1"), ("H", "K2"), ("K1", "K2"), ("H", "M"), ("K2", "M"), ("M", "D"), ("K2", "D"), ("H", "L0"),
         ("L0", "LD"), ("L0", "T"), ("LD", "T"), ("L0", "F"), ("T", "F"), ("D", "R"), ("R", "F")]
NEAR = {n: [] for n in NODES}
for _a, _b in EDGES:
    NEAR[_a].append(_b)
    NEAR[_b].append(_a)


def route(a, b):
    prev = {a: None}
    q = [a]
    while q:
        n = q.pop(0)
        if n == b:
            break
        for m in NEAR[n]:
            if m not in prev:
                prev[m] = n
                q.append(m)
    out = []
    n = b
    while n is not None:
        out.append(n)
        n = prev.get(n)
    return out[::-1]


IDLE = ["NICE DAY IN HERE", "IS IT TILTED IN HERE?", "I SHOULD CALL MOM", "HMM HMM HMM", "WHAT WAS I DOING",
        "COZY.", "LA LA LA", "I LIKE THIS PLACE", "DID I LEAVE THE STOVE ON?", "SMALL BUT MINE"]
TAPS = ["HELLO? ANYONE OUT THERE?", "TAP TAP. YOU OK?", "I CAN SEE YOU.", "HEY. BIG FACE.", "IS THIS GLASS?"]
TOSS = ["WHOAAA!", "AAAH!", "EARTHQUAKE!", "NOT AGAIN!", "HEY HEY HEY!"]
DAZED = ["...WHAT WAS THAT", "MY HEAD...", "WHO IS DOING THAT?!", "PLEASE STOP", "I NEED TO SIT DOWN"]
MOURN = ["MY {}...", "NOT THE {}!", "I LIKED THAT {}.", "OH NO. THE {}."]
MISS = ["I MISS MY {}", "THE {} USED TO BE HERE", "SO EMPTY WITHOUT THE {}"]


class Man:
    look = None          # friends have their own colouring
    cheer = 0.0
    drink = -9.0

    def __init__(self, world):
        self.visit_t = 75.0
        self.tidy_t = 0.0
        self.coins = 40
        self.hurt = 0.0          # seconds of bandage and limp left
        self.grudge = 0.0        # how fed up he is with being thrown about
        self.sign = None         # what his placard says, when he is holding one
        self.knocked = False
        self.place_t = 140.0     # until he next drives somewhere
        self.w = world
        self.x, self.y, self.z = 88.0, 30.0, 0.0
        self.level = "floor"
        self.pose, self.view, self.face = "stand", "side", 1
        self.outfit = "home"
        self.carry = None
        self.holding = None
        self.visible = True
        self.away = False
        self.hidden = False            # in the bathroom
        self.script = None
        self.dt = 0.03
        self.anim = 0.0
        self.speech, self.speech_t = "", 0.0
        self.need = {"food": 70.0, "rest": 80.0, "fun": 60.0, "wash": 75.0}
        self.mood = 80.0
        self.food = 3
        self.work_t = 110.0
        self.body = None
        self.still = 0.0
        self.big = False
        self.wobble = 0.0
        self.hop = 0.0
        self.sleeping = False
        self.zt = 0.0
        self.rag, self.rag_j, self.rag_th = None, None, 0.0   # loose limbs while he is thrown about
        self.rag_down = False

    # ---- speech ----
    def say(self, s, t=2.6):
        if not self.away:
            self.speech, self.speech_t = s, t
            play("talk%d" % random.randrange(4), 0.25)

    # ---- script building blocks (generators) ----
    def wait(self, t, pose=None, view=None):
        if pose:
            self.pose = pose
        if view:
            self.view = view
        while t > 0:
            t -= self.dt
            yield

    def walk_to(self, x, y, speed=30.0):
        while True:
            dx, dy = x - self.x, y - self.y
            d = math.hypot(dx, dy)
            step = speed * self.dt * (0.55 if self.hurt > 0 else 1.0)
            if d <= step:
                self.x, self.y = float(x), float(y)
                break
            self.x += dx / d * step
            self.y += dy / d * step
            sdx = dx - dy
            if abs(sdx) > 0.25 * d:
                self.face = 1 if sdx > 0 else -1
            self.pose, self.view = "walk", "side"
            yield
        self.pose = "stand"

    def climb(self, up):
        self.pose, self.view = "climb", "back"
        self.x, self.y = 46.0, 61.5
        z1 = 30.0 if up else 0.0
        while abs(self.z - z1) > 0.6:
            self.z += (18 if up else -18) * self.dt
            yield
        self.z = z1
        self.level = "loft" if up else "floor"
        self.y = 54.0 if up else 66.0
        self.pose, self.view = "stand", "side"

    def go(self, node):
        if self.level == "loft":
            yield from self.walk_to(46, 54)
            yield from self.climb(False)
        start = min(NODES, key=lambda n: (NODES[n][0] - self.x) ** 2 + (NODES[n][1] - self.y) ** 2)
        for n in route(start, node):
            yield from self.walk_to(*NODES[n])

    def go_point(self, level, x, y):
        if level == "loft":
            if self.level == "floor":
                yield from self.go("LD")
                yield from self.climb(True)
            yield from self.walk_to(x, y)
        else:
            node = min(NODES, key=lambda n: (NODES[n][0] - x) ** 2 + (NODES[n][1] - y) ** 2)
            yield from self.go(node)
            yield from self.walk_to(x, y)

    def bath(self, t, outfit):
        w = self.w
        yield from self.go("H")
        yield from self.walk_to(58, 30)
        self.visible, self.hidden = False, True
        w.bath_on = True
        yield from self.wait(t)
        self.outfit = outfit
        w.bath_on = False
        self.visible, self.hidden = True, False
        yield from self.walk_to(*NODES["H"])

    def leave(self):
        w = self.w
        yield from self.go("D")
        w.door_open = True
        yield from self.wait(0.4)
        yield from self.walk_to(137, 3)
        self.visible, self.away = False, True
        w.door_open = False
        w.broke_away = False

    def come_back(self):
        w = self.w
        w.door_open = True
        self.x, self.y, self.z, self.level = 137.0, 3.0, 0.0, "floor"
        self.visible, self.away = True, False
        yield from self.walk_to(*NODES["D"])
        w.door_open = False

    # ---- activities ----
    def act_eat(self):
        w = self.w
        yield from self.go("K1")
        self.face = 1
        w.fridge_open = True
        yield from self.wait(0.9, "stand")
        if self.food <= 0:
            self.say("EMPTY. SHOPPING TIME.")
            yield from self.wait(1.2)
            w.fridge_open = False
            yield from self.act_shop()
            return
        self.food -= 1
        self.say(random.choice(["SNACK TIME", "MMM LEFTOVERS", "DINNER FOR ONE"]))
        self.carry = "food"
        yield from self.wait(0.5)
        w.fridge_open = False
        yield from self.go("K2")
        t = 3.5
        while t > 0:
            t -= self.dt
            self.pose = "eat"
            self.need["food"] = min(100, self.need["food"] + 16 * self.dt)
            yield
        self.carry = None
        self.pose = "stand"

    def act_shop(self):
        yield from self.leave()
        yield from self.trip(lambda: Drive(self.w, "THE SHOP"))
        yield from self.trip(lambda: Place(self.w, "store"))
        yield from self.trip(lambda: Drive(self.w, "", True))
        yield from self.come_back()
        self.carry = "bag"
        if self.w.broke_away:
            self.say("WHAT HAPPENED HERE?!")
        else:
            self.say("GOT GROCERIES")
        yield from self.go("K1")
        self.w.fridge_open = True
        yield from self.wait(1.2, "stand")
        self.food = 4
        self.carry = None
        self.w.fridge_open = False

    def act_tv(self):
        w = self.w
        tv = w.items["tv"]
        yield from self.go("M")
        if tv.state != "ok" or tv.away():
            self.say(random.choice(["NO TV. GREAT.", "I WANTED TO WATCH MY SHOW"]))
            self.mood -= 4
            yield from self.wait(2, "look")
            yield from self.act_play()
            return
        yield from self.walk_to(114, 38)
        self.x, self.y, self.z = 114.0, 46.0, 7.0
        self.pose, self.view = "sit", "back"
        w.tv_on = True
        t = random.uniform(24, 36)
        while t > 0:
            t -= self.dt
            self.need["fun"] = min(100, self.need["fun"] + 7 * self.dt)
            if random.random() < self.dt * 0.12:
                self.say(random.choice(["HAHA", "OH COME ON", "CLASSIC", "NO WAY"]), 1.6)
            if tv.state != "ok" or tv.away():
                self.say("HEY! MY SHOW!")
                break
            yield
        w.tv_on = False
        self.x, self.y, self.z = 114.0, 38.0, 0.0
        self.pose, self.view = "stand", "side"
        yield from self.wait(0.4)

    def act_play(self):
        """No TV?  Kick the ball around instead."""
        b = self.w.ball
        if b.z < 1 and b.speed() < 20:
            yield from self.go_point("floor", clamp(b.x + 3, 6, 144), clamp(b.y + 3, 6, 94))
            for _ in range(3):
                if b.z > 1:
                    break
                yield from self.walk_to(clamp(b.x + 3, 6, 144), clamp(b.y + 3, 6, 94), 44)
                a = random.uniform(0, 6.28)
                b.vx, b.vy, b.vz = math.cos(a) * 90, math.sin(a) * 90, 70
                self.say(random.choice(["GOAL!", "KICK!", "AND HE SCORES"]), 1.2)
                self.need["fun"] = min(100, self.need["fun"] + 14)
                yield from self.wait(1.3, "stand")
        else:
            self.say("HMM. WHERE'S MY BALL")
            self.need["fun"] = min(100, self.need["fun"] + 20)
            yield from self.wait(2, "stand")

    def act_sleep(self):
        w = self.w
        if self.outfit != "pj":
            yield from self.bath(2.5, "pj")
        yield from self.go_point("loft", 36, 28)
        self.say("GOODNIGHT", 1.5)
        yield from self.wait(0.8)
        self.x, self.y, self.z = 17.0, 15.0, 37.0
        self.pose = "lie"
        self.sleeping = True
        w.lights_off = True
        t = random.uniform(50, 75)
        while t > 0:
            t -= self.dt
            self.hurt = max(0.0, self.hurt - 2 * self.dt)
            self.need["rest"] = min(100, self.need["rest"] + 1.8 * self.dt)
            yield
        self.wake()
        self.say(random.choice(["GOOD MORNING", "*YAWN*", "FIVE MORE MINUTES..."]))
        yield from self.wait(1.2)
        yield from self.bath(2.5, "home")

    def wake(self):
        self.sleeping = False
        self.w.lights_off = False
        if self.pose == "lie":
            self.x, self.y, self.z = 36.0, 28.0, 30.0
            self.pose, self.view = "stand", "side"

    def act_shower(self):
        self.say(random.choice(["SHOWER TIME", "I STINK"]), 1.6)
        yield from self.bath(15, "home")
        self.need["wash"] = 100
        self.say("FRESH!", 1.6)

    def act_work(self):
        self.work_t = random.uniform(280, 380)
        self.say("OFF TO WORK")
        yield from self.bath(2.5, "work")
        yield from self.leave()
        yield from self.trip(lambda: Drive(self.w, "WORK"))
        yield from self.trip(lambda: Place(self.w, "office"))
        yield from self.trip(lambda: Drive(self.w, "", True))
        self.need["rest"] -= 18
        self.need["wash"] = min(self.need["wash"], 22)
        self.need["food"] -= 12
        yield from self.come_back()
        self.say("WHAT HAPPENED HERE?!" if self.w.broke_away else random.choice(["LONG DAY...", "I'M HOME"]))
        yield from self.wait(1.4)
        yield from self.act_shower()

    def trip(self, make):
        """Be somewhere else until that scene is over."""
        self.w.party = make()
        while self.w.party:
            yield

    def act_store(self):
        return self.act_shop()

    def act_park(self):
        """Take the dog to the park."""
        w = self.w
        self.place_t = random.uniform(300, 420)
        self.say(random.choice(["WALKIES!", "LET'S GO TO THE PARK", "COME ON, BOY"]))
        w.dog.bark()
        yield from self.wait(1.4)
        yield from self.leave()
        w.dog.visible = False
        yield from self.trip(lambda: Drive(w, "THE PARK", False, True))
        yield from self.trip(lambda: Place(w, "park", True))
        yield from self.trip(lambda: Drive(w, "", True, True))
        w.dog.visible = True
        yield from self.come_back()
        self.say("WHAT HAPPENED HERE?!" if w.broke_away else random.choice(["GOOD WALK", "HOME AGAIN"]))
        yield from self.wait(1.2)

    def act_visit(self):
        """Round to his friend's place for the evening."""
        self.visit_t = random.uniform(320, 440)
        self.say(random.choice(["GOING TO SAM'S", "GAME NIGHT!", "THE MATCH IS ON AT SAM'S"]))
        yield from self.wait(1.4)
        yield from self.leave()
        yield from self.trip(lambda: Drive(self.w, "SAM'S"))
        yield from self.trip(lambda: Party(self.w))
        yield from self.trip(lambda: Drive(self.w, "", True))
        self.need["food"] = min(100, self.need["food"] + 20)
        self.need["rest"] -= 14
        yield from self.come_back()
        self.say("WHAT HAPPENED HERE?!" if self.w.broke_away else random.choice(["GOOD NIGHT OUT", "HOME AGAIN"]))
        yield from self.wait(1.4)

    def act_knock(self):
        """Someone tapped on the outside of his world."""
        self.view, self.pose = "front", "stand"
        self.say(random.choice(["WHO'S THERE?", "COME IN?", "DID SOMEONE KNOCK?"]), 1.8)
        yield from self.wait(1.2)
        yield from self.act_tap()

    def act_protest(self):
        """He has had enough of being thrown about, and says so."""
        yield from self.go("F")
        self.view, self.pose, self.big = "front", "wave", True
        self.sign = random.choice(["STOP!", "NO SHAKING", "BE NICE"])
        self.say(random.choice(["ENOUGH!", "I LIVE HERE, YOU KNOW", "PUT ME DOWN GENTLY"]), 3.0)
        yield from self.wait(4.0)
        self.big, self.sign = False, None
        self.view, self.pose = "side", "stand"
        self.grudge = max(0.0, self.grudge - 2)

    def act_mop(self):
        w = self.w
        if not w.puddles:
            return
        x, y, r = w.puddles[0]
        self.say(random.choice(["WHAT A MESS", "WHO SPILLED THIS?", "MOPPING UP"]), 1.8)
        yield from self.go_point("floor", clamp(x + 5, 6, 144), clamp(y + 4, 6, 94))
        self.carry = "mop"
        t = 5.0
        while t > 0 and w.puddles:
            t -= self.dt
            self.pose = "eat" if int(self.anim * 3) % 2 else "look"
            w.puddles[0][2] = max(0.0, w.puddles[0][2] - self.dt * 1.2)
            yield
        self.carry, self.pose = None, "stand"
        if w.puddles:
            w.puddles.pop(0)
        self.say("ALL DRY", 1.4)

    def act_patch(self):
        """Fill in a crack in the wall."""
        w = self.w
        if not w.cracks:
            return
        yield from self.go("H" if w.cracks[0][0] == "x" else "K2")
        self.view, self.pose = "back", "climb"
        self.say(random.choice(["I CAN FIX THAT", "A BIT OF FILLER...", "CRACKS EVERYWHERE"]), 1.8)
        yield from self.wait(6.0)
        if w.cracks:
            w.cracks.pop(0)
            self.coins = max(0, self.coins - 5)
        self.view, self.pose = "side", "stand"
        self.say("GOOD AS NEW", 1.4)

    def hear(self, words):
        """You typed something to him."""
        s = " " + words.upper() + " "
        w = self.w
        if self.away or self.hidden or self.body or w.party:
            return
        if self.sleeping:
            self.interrupt()
            self.say("HUH? I WAS ASLEEP")
            return

        def has(*keys):
            return any((" " + k) in s for k in keys)
        self.interrupt()
        if has("SORRY", "FORGIVE"):
            self.grudge, self.mood = 0.0, min(100, self.mood + 15)
            self.say("APOLOGY ACCEPTED")
        elif has("HELLO", "HI ", "HEY", "YO "):
            self.script = self.act_wave()
        elif has("EAT", "FOOD", "HUNGRY", "DINNER", "LUNCH"):
            self.say("GOOD IDEA")
            self.script = self.act_eat()
        elif has("SLEEP", "BED", "NAP", "GOODNIGHT"):
            self.script = self.act_sleep()
        elif has("TV", "SHOW", "WATCH"):
            self.script = self.act_tv()
        elif has("SHOWER", "WASH", "BATH", "STINK"):
            self.script = self.act_shower()
        elif has("FISH"):
            self.script = self.act_fish()
        elif has("CLEAN", "MOP", "TIDY") and w.puddles:
            self.script = self.act_mop()
        elif has("DOG", "WALK", "PARK") and hasattr(self, "act_park"):
            self.say("WALKIES!")
            self.script = self.act_park()
        elif has("SHOP", "STORE", "BUY") and hasattr(self, "act_store"):
            self.script = self.act_store()
        elif has("WORK", "OFFICE", "JOB"):
            self.script = self.act_work()
        elif has("PARTY", "FRIEND", "SAM"):
            self.script = self.act_visit()
        elif has("DANCE", "JUMP"):
            self.hop, self.cheer = 0.35, 2.5
            self.view = "front"
            self.say("LIKE THIS?")
        elif has("LOVE", "GOOD", "NICE", "THANK"):
            self.mood = min(100, self.mood + 10)
            self.say(random.choice(["AW, THANKS", "YOU TOO", "THAT'S KIND"]))
        elif has("MONEY", "COINS", "RICH"):
            self.say("I HAVE %d COINS" % self.coins)
        elif has("NAME", "WHO"):
            self.say("I'M ZERO. I LIVE HERE.")
        elif has("BAD", "STUPID", "HATE", "UGLY"):
            self.mood -= 8
            self.say(random.choice(["RUDE.", "WELL, THAT HURTS", "I HEARD THAT"]))
        else:
            self.say(random.choice(["HMM?", "I DON'T KNOW THAT ONE", "SAY AGAIN?", "IF YOU SAY SO"]))

    def act_fish(self):
        w = self.w
        yield from self.go("T")
        self.face = -1
        self.say(random.choice(["HEY BUDDY", "HUNGRY, FISH?", "BLUB BLUB"]))
        yield from self.wait(1.0, "stand")
        if w.tank_level < 0.5:
            self.say("LET ME TOP YOU UP")
            t = 1.5
            while t > 0:
                t -= self.dt
                w.tank_level = min(0.72, w.tank_level + 0.4 * self.dt)
                w.spark(8, 80, 30, (120, 190, 250), 1)
                yield
        else:
            for _ in range(5):
                w.parts.append([random.uniform(6, 10), random.uniform(72, 88), 30, 0, 0, -10, 0.8, (230, 190, 90), 0])
        yield from self.wait(1.5)

    def act_tap(self):
        yield from self.go("F")
        self.view, self.pose, self.big = "front", "tap", True
        self.say(random.choice(TAPS), 3.2)
        t = 3.6
        while t > 0:
            t -= self.dt
            if random.random() < self.dt * 5:
                self.w.rings.append([P(self.x, self.y, 0)[0] + 16, P(self.x, self.y, 0)[1] - 52, 0.0])
            yield
        self.big = False
        self.view, self.pose = "side", "stand"

    def act_wander(self):
        yield from self.go(random.choice(list(NODES)))
        if random.random() < 0.55:
            self.say(random.choice(IDLE))
        yield from self.wait(random.uniform(1.0, 3.0), "stand")

    def act_window(self):
        yield from self.go("L0")
        yield from self.walk_to(22, 62)
        self.face = -1
        self.say("NICE NIGHT" if self.w.daylight() < 0.3 else "LOOK AT THAT SKY")
        yield from self.wait(3, "stand")

    def act_mourn(self, name):
        w = self.w
        it = w.items.get(name)
        if name == "lamp":
            yield from self.go_point("floor", LAMP_AT[0] + 6, LAMP_AT[1] + 6)
            self.say("WHO BROKE THE LIGHT?")
            yield from self.wait(2.4, "stand")
            return
        if not it or it.state != "broken":
            return
        dx, dy, dz = it.debris
        on_loft = dz >= 29 and dx < 58 and dy < 62
        yield from self.go_point("loft" if on_loft else "floor", clamp(dx + 5, 6, 144), clamp(dy + 5, 6, 94))
        self.face = -1
        self.say(random.choice(MOURN).format(name.upper()))
        self.mood -= 8
        yield from self.wait(2.8, "look")

    def act_miss(self, name):
        if name == "lamp":
            yield from self.go_point("floor", LAMP_AT[0] + 6, LAMP_AT[1] + 6)
            self.say("SO DARK IN HERE")
        else:
            lv, x, y = ITEMS[name]["at"]
            yield from self.go_point(lv, x, y)
            self.face = 1
            self.say(random.choice(MISS).format(name.upper()))
        yield from self.wait(2.6, "look")

    def act_tidy(self, name):
        it = self.w.items[name]
        on_loft = it.z >= 29 and it.x < 58 and it.y < 62
        yield from self.go_point("loft" if on_loft else "floor", clamp(it.x + 4, 6, 144), clamp(it.y + 4, 6, 94))
        if it.state != "ok" or not it.away():
            return
        self.say(random.choice(["THERE YOU ARE", "BACK YOU GO", "HOW DID YOU GET HERE"]), 1.8)
        yield from self.wait(0.6, "look")
        it.held = True
        self.holding = it
        self.carry = "item"
        lv, x, y = ITEMS[name]["at"]
        yield from self.go_point(lv, x, y)
        yield from self.wait(0.5, "stand")
        it.reset()
        self.holding = None
        self.carry = None
        self.w.spark(*it.home, (255, 240, 170), 6)

    def act_wave(self):
        self.view, self.pose = "front", "wave"
        self.say(random.choice(["HEY!", "THAT TICKLES", "OH, HI", "WHAT?"]), 1.8)
        yield from self.wait(1.6)
        self.view, self.pose = "side", "stand"

    # ---- brain ----
    def choose(self):
        w = self.w
        if w.notices:
            return self.act_mourn(w.notices.pop(0))
        if self.grudge >= 3 and w.calm > 3:
            return self.act_protest()
        if w.puddles and w.calm > 4:
            return self.act_mop()
        if w.cracks and w.calm > 8 and random.random() < 0.4:
            return self.act_patch()
        low = min(self.need, key=self.need.get)
        if self.need[low] < 30:
            return {"food": self.act_eat, "rest": self.act_sleep, "fun": self.act_tv, "wash": self.act_shower}[low]()
        if self.tidy_t <= 0 and w.calm > 6:
            for it in w.items.values():
                if it.away() and it.speed() < 4:
                    self.tidy_t = 25.0
                    return self.act_tidy(it.name)
        if w.tank_level < 0.45:
            return self.act_fish()
        if self.work_t <= 0:
            return self.act_work()
        if self.visit_t <= 0 and w.level() and self.need["rest"] > 35:
            return self.act_visit()
        if self.place_t <= 0 and w.level() and self.need["rest"] > 35:
            return self.act_park()
        if self.coins < 15 and w.level() and any(it.state == "broken" for it in w.items.values()):
            self.say("I NEED TO EARN SOME MONEY", 1.8)
            return self.act_work()
        broken = [n for n, it in w.items.items() if it.state == "broken"] + ([] if w.lamp_ok else ["lamp"])
        opts = [(self.act_wander, 3.0), (self.act_tap, 2.0), (self.act_fish, 1.2), (self.act_tv, 1.2),
                (self.act_window, 0.8), (self.act_play, 1.0)]
        if self.need["food"] < 70:
            opts.append((self.act_eat, 1.0))
        r = random.uniform(0, sum(wt for _, wt in opts) + (2.5 if broken else 0))
        for fn, wt in opts:
            r -= wt
            if r <= 0:
                return fn()
        return self.act_miss(random.choice(broken))

    def interrupt(self):
        w = self.w
        w.fridge_open = w.tv_on = w.bath_on = w.door_open = False
        self.wake()
        self.visible, self.hidden, self.big = True, False, False
        self.carry = None
        if self.holding:
            self.holding.held = False
            self.holding.x, self.holding.y, self.holding.z = self.x, self.y, self.z + 8
            self.holding = None
        self.script = None

    def poke(self):
        if self.away or self.hidden or self.body:
            return
        if self.sleeping:
            self.interrupt()
            self.say("HUH? I WAS ASLEEP")
            return
        self.interrupt()
        self.hop = 0.35
        self.script = self.act_wave()

    def update(self, dt):
        w = self.w
        self.dt = dt
        self.anim += dt
        self.speech_t -= dt
        self.hop = max(0.0, self.hop - dt)
        for k, rate in (("food", 0.42), ("rest", 0.34), ("fun", 0.5), ("wash", 0.3)):
            if not (k == "rest" and self.sleeping):
                self.need[k] = clamp(self.need[k] - rate * dt, 0, 100)
        self.mood = clamp(self.mood + dt * 0.4, 0, 100)
        if not self.away:
            self.work_t -= dt
            self.visit_t -= dt
        self.tidy_t -= dt
        self.hurt = max(0.0, self.hurt - dt)
        self.grudge = max(0.0, self.grudge - dt / 40.0)
        if not self.away:
            self.place_t -= dt
        if w.party:                                     # out at his friend's: the party moves him
            self.need["fun"] = min(100, self.need["fun"] + 4 * dt)
            return
        gx, gy, gz = w.g
        gh = math.hypot(gx, gy)
        exposed = not self.away and not self.hidden
        # thrown around?
        if exposed and not self.body and (gz > -0.6 or gh > 0.62 or w.shake > 1.5):
            self.interrupt()
            self.body = Body(self.x, self.y, self.z + 0.5, r=3.5, h=16, mu=0.35, rest=0.3)
            self.body.vz = 30
            self.rag_start()
            self.pose, self.view = "rag", "side"
            self.still = 0.0
            self.say(random.choice(TOSS), 2.0)
            self.mood -= 6
            self.need["rest"] -= 4
            self.grudge += 1
        if self.body:
            b = self.body
            acc = [(w.g[i] + w.jerk[i]) * GRAV for i in range(3)]
            if step_body(b, acc, dt) > 150:              # a hard landing leaves a mark
                play("thud", 0.15)
                if self.hurt <= 0:
                    self.say(random.choice(["OW!", "MY HEAD!", "THAT HURT!"]), 1.6)
                self.hurt = 45.0
            self.x, self.y, self.z = b.x, b.y, b.z
            if self.pose == "rag":
                self.rag_step(dt)
                if b.speed() < 10 and gz < -0.8 and gh < 0.4 and b.touch:
                    self.still += dt
                else:
                    self.still = 0.0
                if self.still > 0.7:
                    self.pose, self.view = "dizzy", "front"
                    self.still = 0.0
                    self.say(random.choice(DAZED))
            elif self.pose == "dizzy":
                self.still += dt
                if self.still > 1.6:
                    self.body = None
                    self.pose, self.view = "stand", "side"
                    if self.z >= 29 and self.x < 56 and self.y < 60:
                        self.level, self.z = "loft", 30.0
                        if 3 < self.x < 31 and 7 < self.y < 47:
                            self.x, self.y = 36.0, 28.0
                    else:
                        self.level, self.z = "floor", 0.0
                        for (x0, y0, z0, x1, y1, z1) in SOLIDS:
                            if z0 == 0 and x0 - 2 < self.x < x1 + 2 and y0 - 2 < self.y < y1 + 2:
                                n = min(NODES, key=lambda n: (NODES[n][0] - self.x) ** 2 + (NODES[n][1] - self.y) ** 2)
                                self.x, self.y = map(float, NODES[n])
                                break
            return
        if self.hidden and w.shake > 1.2 and random.random() < dt * 2:
            self.say("HEY! I'M IN THE SHOWER!")
        if self.sleeping and (w.shake > 0.55 or gh > 0.4):
            self.interrupt()
            self.say(random.choice(["HUH?! WHAT?", "I WAS SLEEPING!", "WHO'S THERE?"]))
            self.mood -= 5
        if self.knocked:
            self.knocked = False
            if exposed and not self.big:
                self.interrupt()
                self.script = self.act_knock()
        # leaning in a tilted house
        self.wobble = lerp(self.wobble, clamp((gx - gy) * 0.7, -1, 1) if exposed else 0.0, min(1, dt * 6))
        if exposed and gh > 0.22 and self.speech_t <= 0 and random.random() < dt * 0.6:
            self.say(random.choice(["WHOA...", "IS THE FLOOR MOVING?", "EASY NOW", "I FEEL SEASICK"]), 1.8)
        if self.script is None:
            self.script = self.choose()
        try:
            next(self.script)
        except StopIteration:
            self.script = None

    # ---- his body: joint positions for each pose, loose limbs when he is thrown ----
    def joints(self):
        """Where his joints are on screen right now."""
        k = 2 if self.big else 1
        sx, sy = P(self.x, self.y, self.z)
        t, pose, view = self.anim, self.pose, self.view
        wob = self.wobble if pose not in ("sit", "climb") else 0.0
        if pose == "stand" and abs(wob) > 0.3 and view == "side":
            pose = "wobble"
        hop = math.sin(self.hop / 0.35 * math.pi) * 5 if self.hop > 0 else 0.0
        J = {}

        def fk(o, a, n):
            return (o[0] + math.sin(a) * n, o[1] + math.cos(a) * n)

        if view == "side":
            f = self.face
            lean = wob * 0.28 * f                       # he leans the way the floor tips
            sway = wob * 0.7 * f                        # and his arms hang that way too
            br = math.sin(t * 2) * 0.04
            n_leg, f_leg = (0.06, 0.02), (-0.1, -0.05)
            n_arm, f_arm = (0.05 + br + sway, 0.28 + br + sway), (-0.12 - br + sway, 0.1 + sway)
            if pose == "walk":
                s1, c1 = math.sin(t * 9), math.cos(t * 9)
                n_leg = (0.62 * s1, 0.62 * s1 - 0.95 * max(0.0, c1))
                f_leg = (-0.62 * s1, -0.62 * s1 - 0.95 * max(0.0, -c1))
                n_arm = (-0.6 * s1 + sway, -0.6 * s1 + 0.5 + sway)
                f_arm = (0.6 * s1 + sway, 0.6 * s1 + 0.5 + sway)
            elif pose == "wobble":
                sw = math.sin(t * 9) * 0.35
                n_leg, f_leg = (0.34, 0.2), (-0.34, -0.22)
                n_arm, f_arm = (1.7 + sw + sway, 2.1 + sw + sway), (-1.7 + sw + sway, -2.1 + sw + sway)
            elif pose == "eat":
                n_arm = (0.9, 2.5 + 0.35 * math.sin(t * 10))
            elif pose == "look":
                lean += 0.3
                n_arm, f_arm = (0.5, 0.95), (0.3, 0.6)
            if hop or self.cheer > 0:
                n_arm, f_arm = (2.7, 3.0), (2.9, 3.3)
            kn_n = fk((0, 0), n_leg[0], LEG)
            ft_n = fk(kn_n, n_leg[1], LEG)
            kn_f = fk((0, 0), f_leg[0], LEG)
            ft_f = fk(kn_f, f_leg[1], LEG)
            hy = -max(ft_n[1], ft_f[1])                 # the lower foot is the one on the ground
            hip = (0.0, hy)
            neck = (math.sin(lean) * TORSO, hy - math.cos(lean) * TORSO)
            sh = (neck[0], neck[1] + 1.5)
            el_n = fk(sh, n_arm[0], ARM)
            el_f = fk(sh, f_arm[0], ARM)
            J = {"hip_n": hip, "hip_f": hip, "neck": neck, "sh_n": sh, "sh_f": sh,
                 "kn_n": (kn_n[0], kn_n[1] + hy), "ft_n": (ft_n[0], ft_n[1] + hy),
                 "kn_f": (kn_f[0], kn_f[1] + hy), "ft_f": (ft_f[0], ft_f[1] + hy),
                 "el_n": el_n, "ha_n": fk(el_n, n_arm[1], ARM), "el_f": el_f, "ha_f": fk(el_f, f_arm[1], ARM),
                 "head": (neck[0] + math.sin(lean) * 3 + 0.5, neck[1] - 3 + (1 if pose == "look" else 0))}
            J = {n: (p[0] * f, p[1]) for n, p in J.items()}
        else:
            lean = wob * 0.28
            if pose == "dizzy":
                lean = math.sin(t * 5) * 0.16
            sit = pose == "sit"
            hy = -1.0 if sit else -2 * LEG
            neck = (math.sin(lean) * TORSO, hy - math.cos(lean) * TORSO)
            J = {"neck": neck, "head": (neck[0] + math.sin(lean) * 3, neck[1] - 3)}
            a = 0.5 + 0.5 * math.sin(t * 8)
            for side, sg in (("n", 1), ("f", -1)):
                up = a if sg > 0 else 1 - a
                hipp = (1.5 * sg, hy)
                sh = (neck[0] + 3.5 * sg, neck[1] + 1.5)
                el = (sh[0] + 0.7 * sg + wob * 2, sh[1] + ARM)
                ha = (el[0] + 0.3 * sg + wob * 2.5, el[1] + ARM)
                kn, ft = (1.7 * sg, hy + LEG), (2.0 * sg, 0.0)
                if pose == "climb":
                    el = (sh[0] + 1.6 * sg, sh[1] - 2.5 - up * 2)
                    ha = (sh[0] + 0.3 * sg, sh[1] - 6.5 - up * 3)
                    kn = (2.6 * sg, hy + LEG - up * 2.5)
                    ft = (1.8 * sg, -up * 4)
                elif sit:
                    el, ha = (sh[0] + 1.2 * sg, sh[1] + 4), (sh[0] + 1.8 * sg, sh[1] + 7.5)
                    kn = ft = hipp
                elif hop:
                    el, ha = (sh[0] + 2.5 * sg, sh[1] - 3.5), (sh[0] + 3.5 * sg, sh[1] - 8.5)
                elif pose == "dizzy":
                    el = (sh[0] + 2.2 * sg, sh[1] + 4.2)
                    ha = (el[0] + 1.8 * sg + math.sin(t * 5 + sg) * 1.5, el[1] + 4.2)
                elif sg > 0 and pose in ("tap", "wave"):
                    el = (sh[0] + 3.5, sh[1] - 2.5)
                    swing = (int(t * 7) % 2) * 1.6 if pose == "tap" else math.sin(t * 10) * 2.6
                    ha = (el[0] + 0.4 + swing, el[1] - 4.6)
                if self.cheer > 0:                      # both arms in the air
                    el = (sh[0] + 2.5 * sg, sh[1] - 3.5)
                    ha = (sh[0] + 3.5 * sg + math.sin(t * 14 + sg) * 1.2, sh[1] - 8.5)
                J.update({"hip_" + side: hipp, "sh_" + side: sh, "el_" + side: el, "ha_" + side: ha,
                          "kn_" + side: kn, "ft_" + side: ft})
        return {n: (sx + p[0] * k, sy - hop * k + p[1] * k) for n, p in J.items()}

    def rag_start(self):
        """Let go of the pose: every limb becomes a loose chain, starting where it is."""
        J = self.joints()
        self.rag = {n: [J[n][0], J[n][1], J[n][0] + random.uniform(-1.5, 1.5), J[n][1] + random.uniform(-1.5, 1.5)]
                    for n in ("el_n", "ha_n", "el_f", "ha_f", "kn_n", "ft_n", "kn_f", "ft_f", "head")}
        self.rag_th = 0.0
        self.rag_j = J
        self.rag_down = False

    def rag_step(self, dt):
        """His body tumbles with the physics body; arms, legs and head trail after it and flop."""
        w, b, R = self.w, self.body, self.rag
        sx, sy = P(self.x, self.y, self.z)
        if b.touch and not self.rag_down:               # just hit something: arms and legs splay
            for p in R.values():
                p[2] += random.uniform(-3, 3)
                p[3] += random.uniform(-3, 3)
        self.rag_down = b.touch
        if b.touch:                                     # landed: lie flat, whichever way he was going over
            tgt = 90.0 if math.sin(math.radians(self.rag_th)) >= 0 else -90.0
        else:
            tgt = b.ang
        self.rag_th += ((tgt - self.rag_th + 180) % 360 - 180) * min(1.0, dt * (10 if b.touch else 30))
        th = math.radians(self.rag_th)
        ux, uy = -math.sin(th), -math.cos(th)           # along his spine, hips to chest
        cx, cy = sx, sy - 5
        pel, chest = (cx - ux * 4, cy - uy * 4), (cx + ux * 4, cy + uy * 4)
        g, j = w.g, w.jerk
        ax = ((g[0] - g[1]) + (j[0] - j[1])) / math.sqrt(2) * 340
        ay = (((g[0] + g[1]) + (j[0] + j[1])) / math.sqrt(2) * SE - (g[2] + j[2]) * CE) * 340
        rest = b.touch and b.speed() < 25
        damp = 0.5 if rest else 0.985                   # lying still, limbs stay where they fell
        if rest:
            ax = ay = 0.0
        for p in R.values():
            vx, vy = (p[0] - p[2]) * damp, (p[1] - p[3]) * damp
            p[2], p[3] = p[0], p[1]
            p[0] += vx + ax * dt * dt
            p[1] += vy + ay * dt * dt

        def pin(p, a, n):                               # keep p at distance n from the fixed point a
            dx, dy = p[0] - a[0], p[1] - a[1]
            d = math.hypot(dx, dy) or 0.01
            p[0], p[1] = a[0] + dx / d * n, a[1] + dy / d * n

        def link(p, q, n):                              # keep p and q a distance n apart
            dx, dy = q[0] - p[0], q[1] - p[1]
            d = math.hypot(dx, dy) or 0.01
            m = (d - n) / d * 0.5
            p[0] += dx * m
            p[1] += dy * m
            q[0] -= dx * m
            q[1] -= dy * m

        px, py = -uy, ux                                # across his body
        sh_n, sh_f = (chest[0] + px, chest[1] + py), (chest[0] - px, chest[1] - py)
        chains = ((sh_n, "el_n", "ha_n", ARM), (sh_f, "el_f", "ha_f", ARM),
                  (pel, "kn_n", "ft_n", LEG), (pel, "kn_f", "ft_f", LEG))
        for _ in range(4):
            for anchor, a, c, n in chains:
                pin(R[a], anchor, n)
                link(R[a], R[c], n)
            pin(R["head"], chest, 4.5)
        hd = R["head"]                                  # his neck is not rubber: pull the head back in line
        hd[0] += (chest[0] + ux * 4.5 - hd[0]) * 0.3
        hd[1] += (chest[1] + uy * 4.5 - hd[1]) * 0.3
        J = {n: (p[0], p[1]) for n, p in R.items()}
        J.update({"hip_n": pel, "hip_f": pel, "neck": chest, "sh_n": sh_n, "sh_f": sh_f})
        self.rag_j = J

    def draw(self, surf):
        if not self.visible:
            return
        if self.pose == "lie":                          # asleep: just his head on the pillow
            hx, hy = P(17, 14, 38.5)
            hs = head_sprite("front", 1, "sleep")
            surf.blit(hs, (hx - hs.get_width() // 2, hy - hs.get_height() + 2))
            return
        k = 2 if self.big else 1
        if self.pose == "rag" and self.rag:
            draw_figure(surf, self.rag_j, "side", self.face, 1, self.outfit, None, "dizzy",
                        int(round(self.rag_th / 90.0)) % 4, True, self.look)
            return
        expr = "ok"
        if self.pose == "dizzy":
            expr = "dizzy"
        elif self.speech_t > 0 and int(self.anim * 6) % 2:
            expr = "open"
        elif int(self.anim * 10) % 41 == 0:
            expr = "blink"
        J = self.joints()
        draw_figure(surf, J, self.view, self.face, k, self.outfit, self.carry, expr, 0, self.pose != "sit", self.look)
        if self.hurt > 0:                               # a bandage round his head
            hx, hy = int(J["head"][0]), int(J["head"][1])
            surf.fill((250, 250, 250), (hx - 3 * k, hy - 2 * k, 7 * k, 2 * k))
            surf.fill((220, 70, 70), (hx + k, hy - 2 * k, k, k))
        if self.sign:
            hx, hy = int(J["ha_n"][0]), int(J["ha_n"][1])
            wd = len(self.sign) * 4 + 5
            pygame.draw.line(surf, (120, 84, 56), (hx, hy), (hx, hy - 12), 2)
            pygame.draw.rect(surf, (252, 250, 240), (hx - wd // 2, hy - 24, wd, 12))
            pygame.draw.rect(surf, (200, 50, 50), (hx - wd // 2, hy - 24, wd, 12), 1)
            text(surf, self.sign, hx - wd // 2 + 3, hy - 21, (200, 50, 50))
        if self.pose == "dizzy":
            hx, hy = J["head"]
            for i in range(3):
                a = self.anim * 6 + i * 2.1
                surf.fill((255, 230, 90), (int(hx + math.cos(a) * 7), int(hy - 6 + math.sin(a) * 2), 2, 2))

    def head(self):
        sx, sy = P(self.x, self.y, self.z)
        if self.pose == "lie":
            sx, sy = P(17, 14, 44)
            return sx, sy
        if self.pose == "rag" and self.rag:
            return int(self.rag_j["head"][0]), int(self.rag_j["head"][1]) - 6
        if self.pose == "sit":
            return sx, sy - 18
        return sx, sy - (56 if self.big else 29)


# --------------------------------------------------------------------------
# the world
# --------------------------------------------------------------------------
class World:
    def __init__(self, saved=None):
        self.t = 0.0
        self.g = (0.0, 0.0, -1.0)
        self.jerk = (0.0, 0.0, 0.0)
        self.shake = 0.0
        self.rate = 0.0
        self.items = {n: Item(n) for n in ITEMS}
        self.ball = Body(132, 78, 0, r=3, h=6, mu=0.05, rest=0.6)
        self.parts = []      # x y z vx vy vz life colour kind(0 falls, 1 floats)
        self.rings = []
        self.tank_level = 0.7
        self.slosh = [0.0, 0.0]
        self.sloshv = [0.0, 0.0]
        self.fish_y, self.fish_dir, self.fish_z = 80.0, 1, 18.0
        self.lamp_d = [0.0, 0.0]
        self.lamp_v = [0.0, 0.0]
        self.lamp_ok = True
        self.fridge_open = self.tv_on = self.door_open = self.bath_on = self.lights_off = False
        self.notices = []
        self.broke_away = False
        self.flick = 0.0
        self.dark_now = 0
        self.man = Man(self)
        self.lant, self.lantv = 0.0, 0.0     # the lantern in the tree outside
        self.fly_o, self.fly_v = [0.0, 0.0], [0.0, 0.0]
        self.party = None                    # the scene he is in while he is out: a drive, the party, a place
        self.dog = Dog()
        self.calm = 0.0                      # seconds the house has been level and still
        self.puddles = []                    # [x, y, size] on the floor, until he mops them
        self.cracks = []                     # (wall, screen points) from too much shaking, until patched
        self.fridge_tip = self.door_tip = False
        self.weather, self.weather_t = "clear", 60.0
        self.cache = {}                      # furniture and other static pieces, painted once
        self.scratch = pygame.Surface((W, H))
        self.scratch.set_colorkey(KEY)
        self.room = self.build_room()
        self.mask = pygame.mask.from_surface(self.room)
        self.ll = pygame.Surface((W, H))     # the lamp's light, before shadows are cut out of it
        self.shs = pygame.Surface((W, H))    # those shadows
        self.back = (self.build_backdrop(0), self.build_backdrop(1), self.build_backdrop(2))
        self.sky, self.sky_q = pygame.Surface((W, H)), -1
        self.lm = pygame.Surface((W, H))     # light map, multiplied over the room
        self.fx = pygame.Surface((W, H))     # shafts of light and glows, added on top
        self.lit_key = None
        self.lite = False                    # set by the main loop if the device cannot keep up
        if saved:
            self.load(saved)

    # ---- persistence ----
    def dump(self):
        m = self.man
        return {"need": m.need, "food": m.food, "mood": m.mood,
                "broken": [n for n, it in self.items.items() if it.state == "broken"],
                "lamp_ok": self.lamp_ok, "tank": self.tank_level, "t": self.t, "coins": m.coins, "cracks": len(self.cracks)}

    def load(self, d):
        try:
            m = self.man
            for k in m.need:
                m.need[k] = float(d["need"][k])
            m.food = int(d.get("food", 3))
            m.mood = float(d.get("mood", 80))
            self.lamp_ok = bool(d.get("lamp_ok", True))
            self.tank_level = float(d.get("tank", 0.7))
            self.t = float(d.get("t", 0))
            m.coins = int(d.get("coins", 40))
            for _ in range(min(6, int(d.get("cracks", 0)))):
                self.add_crack()
            for n in d.get("broken", []):
                if n in self.items:
                    it = self.items[n]
                    it.state, it.hung = "broken", False
                    it.x, it.y, it.z = clamp(it.home[0] + 6, 6, 144), clamp(it.home[1] + 14, 6, 94), 0.0
                    self.make_debris(it)
        except Exception:
            pass

    # ---- helpers ----
    def level(self):
        return self.g[2] < -0.9 and self.shake < 0.3

    def clock(self):
        """Where we are in the 12-minute day, 0..1.  It starts mid-morning."""
        return (self.t / 720.0 + 0.12) % 1.0

    def daylight(self):
        """0 at night, 1 from dusk through the day (how much warm sky there is)."""
        u = self.clock()
        if u < 0.52 or u >= 0.93:
            return 1.0
        if u < 0.60:
            return (0.60 - u) / 0.08
        return clamp((u - 0.86) / 0.07, 0, 1)

    def sun(self):
        """0 from dusk through the night, 1 in broad day."""
        u = self.clock()
        if u < 0.42:
            return 1.0
        if u < 0.52:
            return (0.52 - u) / 0.10
        return clamp((u - 0.93) / 0.06, 0, 1)

    def paint_sky(self, surf):
        q = (int(round(self.daylight() * 12)), int(round(self.sun() * 12)))
        if q != self.sky_q:
            self.sky_q = q
            self.sky.blit(self.back[0], (0, 0))
            for i in (1, 2):
                if q[i - 1]:
                    self.back[i].set_alpha(q[i - 1] * 255 // 12)
                    self.sky.blit(self.back[i], (0, 0))
        surf.blit(self.sky, (0, 0))

    def spark(self, x, y, z, col, n):
        for _ in range(n):
            self.parts.append([x + random.uniform(-2, 2), y + random.uniform(-2, 2), z + random.uniform(0, 4),
                               random.uniform(-25, 25), random.uniform(-25, 25), random.uniform(10, 60),
                               random.uniform(0.3, 0.8), col, 0])

    def make_debris(self, it):
        it.debris = (it.x, it.y, it.z)
        rnd = random.Random(hash(it.name) & 0xffff)
        cols = ITEMS[it.name]["col"]
        it.bits = [(rnd.randint(-6, 6), rnd.randint(-3, 2), rnd.choice(cols), rnd.randint(1, 2)) for _ in range(9)]

    def smash(self, it):
        it.state = "broken"
        it.hung = False
        self.make_debris(it)
        for c in ITEMS[it.name]["col"]:
            self.spark(it.x, it.y, it.z, c, 5)
        self.flick = 1.0
        play("crash", 0.1)
        if it.name == "bottle" and it.z < 2:
            self.puddles.append([it.x, it.y, 5.0])
        if it.name == "tv":
            self.tv_on = False
        if self.man.away:
            self.broke_away = True
        self.notices.append(it.name)

    def replace(self, name):
        m = self.man
        cost = 15 if name == "lamp" else 12
        if m.coins < cost:                              # new things cost money, and he has to earn it
            if not m.away and not m.sleeping:
                m.say("I CAN'T AFFORD A NEW %s" % name.upper())
            return
        m.coins -= cost
        play("coin")
        if name == "lamp":
            self.lamp_ok = True
            self.lamp_d = [0.0, 0.0]
            self.lamp_v = [0.0, 0.0]
            self.spark(LAMP_AT[0], LAMP_AT[1], RH - LAMP_L, (255, 240, 170), 10)
        else:
            it = self.items[name]
            it.reset()
            self.spark(*it.home, (255, 240, 170), 10)
        if name in self.notices:
            self.notices.remove(name)
        m = self.man
        m.mood = min(100, m.mood + 10)
        if not m.away and not m.sleeping:
            m.say(random.choice(["THANKS!", "OH! A NEW {}!".format(name.upper()), "GOOD AS NEW", "YOU'RE THE BEST"]))

    # ---- simulation ----
    def update(self, dt, g, jerk):
        self.t += dt
        self.rate = math.sqrt(sum((g[i] - self.g[i]) ** 2 for i in range(3))) / max(dt, 1e-3)
        self.g, self.jerk = g, jerk
        jm = math.sqrt(jerk[0] ** 2 + jerk[1] ** 2 + jerk[2] ** 2)
        self.shake = max(self.shake - dt * 2.5, jm)
        gx, gy, gz = g
        acc = [(g[i] + jerk[i] * 0.6) * GRAV for i in range(3)]
        gh = math.hypot(gx, gy)
        k = min(1.0, (gh - 0.22) / gh * 1.3) if gh > 0.22 else 0.0
        acc_it = [(gx * k + jerk[0] * 0.6) * GRAV, (gy * k + jerk[1] * 0.6) * GRAV, acc[2]]
        if 0.7 < jm < 1.5 and self.calm > 1.5 and not self.party:
            self.man.knocked = True
            play("knock", 0.3)
            self.dog.bark()
        self.calm = self.calm + dt if (gz < -0.9 and self.shake < 0.3) else 0.0
        if self.party:                                  # home stands still while he is out
            self.man.update(dt)
            self.party.update(dt)
            for r in self.rings:
                r[2] += dt
            self.rings = [r for r in self.rings if r[2] < 0.5]
            if self.party.done:
                self.end_party()
                self.man.update(0.0)                    # lets his plans start the next scene straight away
            return

        for it in self.items.values():
            if it.state != "ok" or it.held:
                continue
            if it.hung:
                tgt = math.atan2(gx, max(0.2, -gz))
                it.swingv += (40 * (tgt - it.swing) - 1.6 * it.swingv + jerk[0] * 8) * dt
                it.swing += it.swingv * dt
                if self.shake > 1.25 or gz > -0.25 or abs(it.swing) > 0.9:
                    it.hung = False
                    it.vx, it.vy = gx * 40, 12
                continue
            hit = step_body(it, acc_it, dt)
            if hit > it.frag:
                self.smash(it)
        step_body(self.ball, acc, dt)
        loose = [it for it in self.items.values() if it.state == "ok" and not it.held and not it.hung] + [self.ball]
        for i, a in enumerate(loose):                   # things knock into each other
            for b in loose[i + 1:]:
                dx, dy = b.x - a.x, b.y - a.y
                d = math.hypot(dx, dy)
                if 0.01 < d < a.r + b.r and abs(a.z - b.z) < max(a.h, b.h):
                    nx, ny = dx / d, dy / d
                    push = (a.r + b.r - d) / 2
                    a.x, a.y, b.x, b.y = a.x - nx * push, a.y - ny * push, b.x + nx * push, b.y + ny * push
                    vn = (b.vx - a.vx) * nx + (b.vy - a.vy) * ny
                    if vn < 0:
                        k = -0.75 * vn
                        a.vx, a.vy, b.vx, b.vy = a.vx - nx * k, a.vy - ny * k, b.vx + nx * k, b.vy + ny * k
                        for c in (a, b):
                            if c is not self.ball and -vn > c.frag * 0.55 and c.state == "ok":
                                self.smash(c)

        # hanging lamp
        down = max(0.3, -gz)
        for i in (0, 1):
            tgt = clamp(LAMP_L * g[i] / down, -LAMP_L, LAMP_L) if gz < 0 else math.copysign(LAMP_L, g[i] or 1)
            self.lamp_v[i] += (25 * (tgt - self.lamp_d[i]) - 0.9 * self.lamp_v[i] + jerk[i] * 220) * dt
            self.lamp_d[i] += self.lamp_v[i] * dt
        d = math.hypot(*self.lamp_d)
        if d > LAMP_L * 0.97:
            v = math.hypot(*self.lamp_v)
            self.lamp_d = [c * LAMP_L * 0.97 / d for c in self.lamp_d]
            if self.lamp_ok and v > 75:
                self.lamp_ok = False
                bx, by = LAMP_AT[0] + self.lamp_d[0], LAMP_AT[1] + self.lamp_d[1]
                self.spark(bx, by, RH - 3, (255, 240, 200), 12)
                self.flick = 1.0
                if self.man.away:
                    self.broke_away = True
                self.notices.append("lamp")
            self.lamp_v = [c * -0.3 for c in self.lamp_v]

        # fish tank water
        for i in (0, 1):
            self.sloshv[i] += (60 * (g[i] - self.slosh[i]) - 3.0 * self.sloshv[i] + jerk[i] * 25) * dt
            self.slosh[i] += self.sloshv[i] * dt
        if gz > -0.15:
            if self.tank_level > 0.12:
                self.tank_level = max(0.12, self.tank_level - 0.6 * dt)
                for _ in range(3):
                    self.parts.append([random.uniform(2, 12), random.uniform(66, 94), 28, gx * 40, gy * 40, 20,
                                       1.2, (120, 190, 250), 0])
                if len(self.puddles) < 3 and random.random() < dt * 0.6:
                    self.puddles.append([random.uniform(18, 34), random.uniform(66, 94), 4.0])
                    play("splash", 0.5)
        else:
            best, bz = None, -99
            for cx in (1, 13):
                for cy in (65, 95):
                    z = self.water_z(cx, cy)
                    if z > bz:
                        best, bz = (cx, cy), z
            over = bz - 28
            if over > 0 and self.tank_level > 0.12:
                self.tank_level = max(0.12, self.tank_level - min(0.7, over * 0.07) * dt)
                if random.random() < over * 0.5 + 0.2:
                    self.parts.append([best[0], best[1], 28, gx * 60 + random.uniform(-8, 8),
                                       gy * 60 + random.uniform(-8, 8), 15, 1.2, (120, 190, 250), 0])
        self.fish_y += self.fish_dir * (7 + self.shake * 20) * dt
        if self.fish_y > 91:
            self.fish_dir = -1
        if self.fish_y < 69:
            self.fish_dir = 1
        top = clamp(self.water_z(13, self.fish_y), 14, 28)
        self.fish_z = lerp(self.fish_z, clamp((12 + top) / 2 + math.sin(self.t * 1.3) * 2, 13.5, top - 2),
                           min(1, dt * 4))

        # particles
        for p in self.parts:
            if p[8] == 0:
                p[3] += acc[0] * dt
                p[4] += acc[1] * dt
                p[5] += acc[2] * dt
            p[0] += p[3] * dt
            p[1] += p[4] * dt
            p[2] += p[5] * dt
            p[6] -= dt
            if p[2] < 0 or p[2] > RH or not (0 <= p[0] <= RX) or not (0 <= p[1] <= RY):
                p[6] = min(p[6], 0.08)
                p[3] = p[4] = p[5] = 0
                p[2] = clamp(p[2], 0, RH)
        self.parts = [p for p in self.parts if p[6] > 0][-160:]
        for r in self.rings:
            r[2] += dt
        self.rings = [r for r in self.rings if r[2] < 0.5]
        if self.bath_on and random.random() < dt * 14:
            self.parts.append([57, random.uniform(24, 36), 24, 6, 2, 9, 1.6, (235, 235, 245), 1])
        if self.man.sleeping:
            self.man.zt -= dt
            if self.man.zt <= 0:
                self.man.zt = 1.1
                self.parts.append([19, 14, 42, 3, -2, 8, 2.2, (200, 210, 255), 2])

        self.man.update(dt)
        m = self.man
        if self.dog.visible:
            wild = gz > -0.6 or gh > 0.62 or self.shake > 1.5
            near = m if (m.visible and not m.away and m.z < 5 and not m.body) else None
            if m.sleeping:
                near, self.dog.lying, self.dog.target = None, True, None
            self.dog.update(dt, self, near, self.ball, wild)

        # outside: the lantern swings and the fireflies get knocked about
        sr = (gx - gy) / math.sqrt(2)
        sd = (gx + gy) / math.sqrt(2) * SE - gz * CE
        tgt = math.atan2(sr, sd) if abs(sr) + abs(sd) > 0.05 else self.lant
        diff = (tgt - self.lant + math.pi) % (2 * math.pi) - math.pi
        self.lantv += (30 * diff - 1.2 * self.lantv + (jerk[0] - jerk[1]) * 6) * dt
        self.lant += self.lantv * dt
        for i, j in enumerate(((jerk[0] - jerk[1]) * 0.7, -jerk[2])):
            self.fly_v[i] += (-20 * self.fly_o[i] - 3 * self.fly_v[i] + j * 300) * dt
            self.fly_o[i] = clamp(self.fly_o[i] + self.fly_v[i] * dt, -25, 25)

        # lights
        tilt = math.hypot(gx, gy)
        self.flick = max(self.flick - dt * 3, min(1.0, self.shake * 0.7 + self.rate * 0.45 + (0.22 if tilt > 0.3 else 0)
                                                 + (0.3 if gz > 0 else 0)))
        self.dark_now = random.randint(70, 150) if random.random() < self.flick * 0.55 else 0
        # doors swing open when the house leans their way
        m = self.man
        if gy > 0.42 and gz < 0:
            self.fridge_open = self.fridge_tip = True
            if m.food > 0 and random.random() < dt * 0.5:
                m.food -= 1
                self.parts.append([67, 14, 12, gx * 30, 40, 20, 1.4, random.choice(((220, 80, 60), (90, 170, 80), (240, 200, 70))), 0])
        elif self.fridge_tip:
            self.fridge_tip = self.fridge_open = False
        if gy > 0.5 and gz < 0:
            self.door_open = self.door_tip = True
        elif self.door_tip:
            self.door_tip = self.door_open = False
        # hard shaking cracks the plaster
        if self.shake > 1.7 and len(self.cracks) < 6 and random.random() < dt * 0.5:
            self.add_crack()
        # weather drifts
        self.weather_t -= dt
        if self.weather_t <= 0 and REAL_WEATHER[0] is None:
            self.weather_t = random.uniform(90, 240)
            self.weather = random.choice(["clear", "clear", "clear", "snow" if SEASON == "winter" else "rain"])
        elif REAL_WEATHER[0]:
            self.weather = REAL_WEATHER[0]

    def add_crack(self):
        wall = random.choice("xy")
        a, z = random.uniform(8, 92 if wall == "x" else 140), random.uniform(38, 54)
        pts = []
        for i in range(6):
            pts.append(P(0, a, z) if wall == "x" else P(a, 0, z))
            a += random.uniform(-2.5, 2.5)
            z -= random.uniform(1.5, 4)
        self.cracks.append((wall, pts))

    def rain(self, surf):
        """Rain or snow falling outside (drawn before the house, so it is behind it)."""
        t, lean = self.t, math.sin(self.lant) * 10
        if self.weather == "rain":
            for i in range(46):
                x = (i * 53 + int(t * 40) * 3) % (W + 20) - 10
                y = (i * 37 + int(t * 260)) % (H + 10)
                pygame.draw.line(surf, (130, 160, 220), (x, y), (x - 2 + int(lean * 0.3), y + 5))
        elif self.weather == "snow":
            for i in range(40):
                x = (i * 53 + int(math.sin(t * 0.8 + i) * 6) + int(lean)) % W
                y = (i * 37 + int(t * 22 + i * 5)) % H
                surf.fill((240, 244, 255), (x, y, 1 + i % 2, 1))

    def start_party(self):
        self.party = Party(self)

    def end_party(self):
        m = self.man
        self.party.close()
        self.party = None
        m.body, m.rag, m.path = None, None, []
        m.pose, m.view, m.carry, m.cheer = "stand", "side", None, 0.0
        m.visible, m.away = False, True
        self.lit_key = None
        self.dark_now, self.flick = 0, 0.0

    def water_z(self, x, y):
        down = max(0.35, abs(self.g[2]))
        return 12 + self.tank_level * 16 + (self.slosh[0] * (x - 7) + self.slosh[1] * (y - 80)) / down

    # ---- drawing ----
    # ---- drawing: things built once ----
    def build_backdrop(self, dusk):
        """The forest behind the terrace.  dusk: 0 night, 1 dusk, 2 broad day."""
        s = pygame.Surface((W, H))
        rnd = random.Random(11)
        day = dusk == 2
        if day:
            sky = [(88, 150, 230), (120, 180, 240), (160, 208, 246), (206, 232, 250)]
            ground = [(112, 176, 112), (70, 128, 84)]
            dark, mid, far = (70, 52, 56), (56, 112, 84), (120, 170, 150)
            if SEASON == "winter":
                ground = [(226, 232, 246), (180, 190, 220)]
        elif dusk:
            sky = [(34, 38, 96), (84, 62, 122), (176, 98, 108), (236, 150, 104)]
            ground = [(44, 30, 56), (14, 12, 32)]
            dark, mid, far = (22, 16, 36), (46, 30, 58), (96, 62, 92)
        else:
            sky = [(8, 10, 34), (12, 24, 68), (22, 52, 108), (40, 92, 146)]
            ground = [(12, 24, 52), (5, 7, 20)]
            dark, mid, far = (8, 9, 24), (10, 20, 48), (20, 44, 86)
        hz = 104
        gradient(s, sky, 0, hz, 14)
        gradient(s, ground, hz, H, 6)
        for _ in range(0 if day else (16 if dusk else 64)):
            x, y = rnd.randrange(W), rnd.randrange(hz - 34)
            s.fill(shade((210, 222, 255), rnd.uniform(0.3, 0.95) * (0.7 if dusk else 1)), (x, y, 1, 1))
        mx, my = 192, 11
        if day:                                          # the sun, and a few clouds
            add_glow(s, (mx, my + 4), 26, (60, 54, 30))
            pygame.draw.circle(s, (255, 244, 190), (mx, my + 4), 7)
            for cx_, cy_, w_ in ((60, 20, 26), (250, 44, 20), (130, 6, 18), (20, 52, 16)):
                for dx_, r_ in ((0, 5), (w_ // 3, 7), (2 * w_ // 3, 6), (w_, 4)):
                    pygame.draw.circle(s, (250, 252, 255), (cx_ + dx_, cy_), r_)
                s.fill((250, 252, 255), (cx_ - 4, cy_, w_ + 8, 6))
        else:
            add_glow(s, (mx, my), 24, (46, 34, 30) if dusk else (20, 30, 54))
            behind = s.get_at((mx + 9, my - 6))[:3]
            pygame.draw.circle(s, (252, 226, 196) if dusk else (224, 234, 244), (mx, my), 6)
            pygame.draw.circle(s, behind, (mx + 3, my - 2), 5)
        # two rows of pines with mist between them
        for layer, (col, base, hmin, hmax) in enumerate(((far, hz, 16, 36), (mid, hz + 8, 30, 62))):
            for x0 in list(range(-4, 44, 8)) + list(range(280, 326, 8)):
                x, h, w = x0 + rnd.randint(-2, 2), rnd.randint(hmin, hmax), 5 + layer * 3
                for k in range(5):
                    yy = base - h + k * h // 6
                    ww = 2 + k * w // 4
                    pygame.draw.polygon(s, col, [(x, yy), (x - ww, yy + h // 5 + 3), (x + ww, yy + h // 5 + 3)])
                s.fill(col, (x - 1, base - h // 3, 2, h // 3 + 2))
            if layer == 0:
                haze = mix(sky[3], (255, 255, 255), 0.12)
                for y in (hz - 9, hz - 5, hz - 2, hz + 2):
                    for x in range(y & 1, W, 2):
                        s.fill(haze, (x, y, 1, 1))
        # the shadow the terrace throws on the forest floor
        a_, b_, c_ = P(-YARD, RY + YARD, BASE), P(RX + YARD, RY + YARD, BASE), P(RX + YARD, -YARD, BASE)
        pygame.draw.polygon(s, shade(ground[1], 0.5), [a_, b_, c_, (c_[0], c_[1] + 7), (b_[0], b_[1] + 8), (a_[0], a_[1] + 8)])
        # the old tree: trunk up the right edge, roots, a branch reaching over the roof
        poly(s, dark, [(306, 170), (309, 140), (307, 110), (311, 80), (309, 56), (313, 30), (311, 0), (320, 0), (320, 170)])
        poly(s, dark, [(244, 170), (268, 165), (292, 156), (308, 142), (320, 146), (320, 170)])
        poly(s, dark, [(0, 170), (0, 150), (16, 158), (40, 170)])
        pygame.draw.lines(s, dark, False, [(320, 52), (304, 40), (288, 32), (268, 27), (254, 28)], 3)
        pygame.draw.lines(s, dark, False, [(298, 37), (292, 22), (282, 14)], 2)
        pygame.draw.lines(s, dark, False, [(280, 30), (266, 36), (256, 35)], 2)
        pygame.draw.line(s, dark, (292, 22), (302, 12))
        pygame.draw.line(s, dark, (268, 27), (260, 20))
        for _ in range(80):
            x, y = rnd.randrange(W), rnd.randrange(hz + 8, H + 4)
            pygame.draw.line(s, dark, (x, y), (x + rnd.randint(-2, 2), y - rnd.randint(3, 7)))
        leaf = LEAF
        for cx, cy, rx, ry, n in ((258, 27, 9, 5, 22), (256, 35, 6, 3, 10), (282, 13, 9, 4, 16), (301, 12, 7, 4, 10),
                                  (306, 40, 6, 4, 8), (316, 8, 4, 6, 8),
                                  (296, 150, 12, 5, 30), (268, 161, 16, 4, 26), (232, 168, 12, 2, 12),
                                  (314, 134, 5, 8, 12), (20, 128, 12, 5, 22), (52, 146, 14, 4, 24),
                                  (14, 160, 12, 5, 18), (92, 163, 16, 4, 22), (140, 168, 14, 2, 10), (44, 166, 10, 3, 10)):
            for _ in range(n):
                a, r = rnd.uniform(0, 6.283), rnd.random() ** 0.6
                x, y = int(cx + math.cos(a) * rx * r), int(cy + math.sin(a) * ry * r)
                s.fill(rnd.choice(leaf), (x, y, rnd.choice((1, 1, 2)), 1))
            if not day:
                add_glow(s, (cx, cy), max(rx, ry) + 7, (46, 22, 6) if n > 14 else (26, 12, 4))
        for x, y in ((34, 152), (70, 160), (252, 163), (120, 161)):
            s.fill((96, 130, 160), (x, y, 1, 2))
            s.fill((110, 214, 255), (x - 1, y - 1, 3, 1))
            s.fill((190, 240, 255), (x, y - 2, 1, 1))
            if not day:
                add_glow(s, (x, y), 9, (8, 34, 50))
        return s

    def build_room(self):
        """The terrace, the garden and everything in the house that never moves,
        on a see-through surface.  Also builds self.emis (things that glow by
        themselves) and self.fxs (their glow)."""
        s = pygame.Surface((W, H))
        s.fill(KEY)
        s.set_colorkey(KEY, pygame.RLEACCEL)
        em = self.emis = pygame.Surface((W, H))
        em.set_colorkey((0, 0, 0), pygame.RLEACCEL)
        fxs = self.fxs = pygame.Surface((W, H))
        rnd = random.Random(7)
        g, yz, bz = YARD, YZ, BASE
        gx0, gy0, gx1, gy1 = -g, -g, RX + g, RY + g

        def L(col, y0, y1, z0, z1, outline=None, x=0.0):     # a rectangle on the left wall
            wall_rect(s, col, "x", y0, y1, z0, z1, outline, x)

        def Rw(col, x0, x1, z0, z1, outline=None, y=0.0):    # a rectangle on the right wall
            wall_rect(s, col, "y", x0, x1, z0, z1, outline, y)

        def dot(col, x, y, z, w=1, h=1, dx=0, dy=0):
            px, py = P(x, y, z)
            s.fill(col, (px + dx, py + dy, w, h))

        # ---- the terrace: grass on top, a dry-stone wall round the edge ----
        GRASS = (224, 230, 244) if SEASON == "winter" else ((104, 184, 104) if SEASON == "summer" else globals()["GRASS"])
        poly(s, GRASS, [P(gx0, gy0, yz), P(gx1, gy0, yz), P(gx1, gy1, yz), P(gx0, gy1, yz)])
        for _ in range(520):
            x, y = rnd.uniform(gx0 + 1, gx1 - 1), rnd.uniform(gy0 + 1, gy1 - 1)
            if x < RX + 1 and y < RY + 1:
                continue
            dot(shade(GRASS, rnd.choice((0.74, 0.84, 1.14, 1.24))), x, y, yz, 1, rnd.choice((1, 1, 2)))
        for ax, ay, bx, by, f in ((gx0, gy1, gx1, gy1, 1.0), (gx1, gy1, gx1, gy0, 0.7)):
            n = math.hypot(bx - ax, by - ay)
            poly(s, shade(STONE, 0.6 * f), [P(ax, ay, yz), P(bx, by, yz), P(bx, by, bz), P(ax, ay, bz)])
            for row, (za, zb) in enumerate(((yz, yz - 3.6), (yz - 3.6, bz))):
                u = -rnd.uniform(0, 6) - row * 3
                while u < n:
                    w = rnd.choice((6, 8, 9, 11))
                    u0, u1 = max(0, u) / n, min(n, u + w - 1) / n
                    if u1 > u0:
                        p = [(lerp(ax, bx, t), lerp(ay, by, t)) for t in (u0, u1)]
                        poly(s, shade(mix(STONE, (120, 140, 130), rnd.random() * 0.5), f * rnd.uniform(0.72, 1.06)),
                             [P(*p[0], za), P(*p[1], za), P(*p[1], zb), P(*p[0], zb)])
                    u += w
            pygame.draw.line(s, shade(GRASS, 1.2 * f), P(ax, ay, yz), P(bx, by, yz))
            for _ in range(int(n / 5)):                      # grass and moss hanging over the edge
                t = rnd.random()
                px, py = P(lerp(ax, bx, t), lerp(ay, by, t), yz)
                s.fill(shade(GRASS, f * rnd.choice((0.8, 1.0))), (px, py + 1, 1, rnd.choice((1, 2, 3))))
            pygame.draw.line(s, shade(STONE, 0.3), P(ax, ay, bz), P(bx, by, bz))
        # flagstone path down the right-hand side and round to the steps
        for x, y in ((155, 4), (157, 16), (155, 28), (157, 56), (155, 68), (157, 80), (155, 92), (153, 105),
                     (141, 107), (129, 106), (118, 107)):
            px, py = P(x, y, yz)
            pygame.draw.ellipse(s, shade(STONE, 0.86), (px - 3, py - 1, 7, 3))
            s.fill(shade(STONE, 1.08), (px - 2, py - 1, 4, 1))
        # vegetable bed with pumpkins
        poly(s, (88, 60, 48), [P(8, 103, yz), P(56, 103, yz), P(56, 110, yz), P(8, 110, yz)], (58, 40, 36))
        for i, x in enumerate(range(11, 56, 5)):
            for y in (105, 108):
                px, py = P(x, y, yz)
                if (i + (y == 108)) % 3 == 0:
                    s.fill((236, 132, 44), (px - 1, py - 2, 3, 2))
                    s.fill((255, 186, 90), (px - 1, py - 2, 1, 1))
                    s.fill((70, 130, 70), (px, py - 3, 1, 1))
                else:
                    s.fill((60, 132, 72), (px, py - 3, 1, 3))
                    s.fill((120, 196, 104), (px - 1, py - 3, 1, 1))
                    s.fill(((250, 240, 220), (250, 120, 150), (250, 210, 90))[i % 3], (px + 1, py - 4, 1, 1))
        # bushes, flowers
        for cx, cy, r in ((-6, 107, 5), (144, 108, 4), (158, 40, 4), (70, 109, 3)):
            px, py = P(cx, cy, yz)
            for dx, dy, rr, f in ((0, -2, r, 0.7), (-2, -3, r - 1, 0.9), (2, -4, r - 2, 1.05), (-1, -5, r - 3, 1.25)):
                if rr > 0:
                    pygame.draw.circle(s, shade((52, 112, 76), f), (px + dx, py + dy), rr)
        for _ in range(34):
            x, y = rnd.uniform(gx0 + 2, gx1 - 2), rnd.uniform(gy0 + 2, gy1 - 2)
            if x < RX + 2 and y < RY + 2:
                continue
            dot((56, 120, 70), x, y, yz, 1, 2, 0, -1)
            dot(rnd.choice(((250, 244, 230), (250, 214, 100), (244, 130, 160), (170, 200, 255))), x, y, yz, 1, 1, 0, -2)
        # a bench, a barrel, the letterbox
        box(s, 155, 60, yz + 3, 159, 76, yz + 4.4, WOOD)
        for y in (61, 75):
            pygame.draw.line(s, shade(WOOD, 0.5), P(159, y, yz + 3), P(159, y, yz))
        box(s, 154, 8, yz, 160, 14, yz + 7, (150, 100, 62))
        for z in (1.5, 5):
            pygame.draw.lines(s, (70, 60, 66), False, [P(154, 14, yz + z), P(160, 14, yz + z), P(160, 8, yz + z)])
        px, py = P(160, 109, yz)
        s.fill((80, 62, 56), (px, py - 6, 1, 6))
        s.fill((204, 70, 60), (px - 2, py - 9, 5, 3))
        s.fill((240, 120, 100), (px - 2, py - 9, 5, 1))
        s.fill((250, 230, 120), (px + 3, py - 10, 1, 2))
        # picket fence along the front, with a gap for the path
        for (ax, ay, bx, by, f) in ((gx0 + 2, gy1 - 1, 112, gy1 - 1, 1.0), (gx1 - 1, 98, gx1 - 1, gy0 + 2, 0.74)):
            wood = shade((214, 204, 186), f)
            for zz in (yz + 2, yz + 4.5):
                pygame.draw.line(s, shade(wood, 0.82), P(ax, ay, zz), P(bx, by, zz))
            n = int(math.hypot(bx - ax, by - ay) / 5)
            for i in range(n + 1):
                x, y = lerp(ax, bx, i / n), lerp(ay, by, i / n)
                pygame.draw.line(s, wood, P(x, y, yz), P(x, y, yz + 6))
                dot(shade(wood, 1.15), x, y, yz + 6)
        # garden lamps
        for x, y in GLAMPS:
            px, py = P(x, y, yz)
            s.fill((40, 36, 52), (px, py - 5, 1, 5))
            s.fill((40, 36, 52), (px - 1, py - 9, 3, 1))
            s.fill((255, 200, 110), (px - 1, py - 8, 3, 3))
            em.fill((255, 206, 120), (px - 1, py - 8, 3, 3))
            em.fill((255, 244, 200), (px, py - 7, 1, 1))
            add_glow(fxs, (px, py - 7), 11, (84, 50, 16))

        # ---- behind the house: an autumn tree and the chimney ----
        tx, ty = -9, 34
        pygame.draw.line(s, (56, 40, 46), P(tx, ty, yz), P(tx, ty, 66), 4)
        cx, cy = P(tx, ty, 76)
        for dx, dy, r, c in ((-10, 5, 9, (112, 52, 36)), (9, 6, 9, (120, 56, 36)), (0, 0, 12, (140, 66, 38)),
                             (-6, -5, 8, (166, 84, 40)), (7, -4, 7, (160, 78, 40)), (-1, -8, 6, (196, 108, 46))):
            pygame.draw.circle(s, seasonal(c), (cx + dx, cy + dy), r)
        for _ in range(60):
            a, r = rnd.uniform(0, 6.283), rnd.random() ** 0.5 * 15
            x, y = int(cx + math.cos(a) * r), int(cy + math.sin(a) * r * 0.8)
            s.fill(seasonal(rnd.choice(((214, 120, 50), (236, 150, 60), (96, 44, 34)))), (x, y, rnd.choice((1, 2)), 1))
            if rnd.random() < 0.4:
                em.fill(rnd.choice(LEAF), (x, y, 1, 1))
        add_glow(fxs, (cx, cy), 20, (40, 20, 6))
        box(s, 104, -9, 30, 114, -3, 72, shade(BRICK, 0.92))
        for z in range(52, 72, 4):
            pygame.draw.line(s, shade(BRICK, 0.5), P(104, -3, z), P(114, -3, z))
        box(s, 103, -10, 72, 115, -2, 74.5, (132, 124, 132))
        px, py = P(109, -6, 74.5)
        s.fill((30, 24, 34), (px - 2, py, 5, 1))
        em.fill((255, 150, 60), (px - 1, py, 2, 1))
        # tv aerial on the wall
        ax_, ay_ = P(141, -2, RH)
        pygame.draw.line(s, (60, 60, 80), (ax_, ay_), (ax_, ay_ - 12))
        for dy, w in ((-12, 4), (-9, 3), (-6, 2)):
            pygame.draw.line(s, (60, 60, 80), (ax_ - w, ay_ + dy), (ax_ + w, ay_ + dy))

        # ---- walls ----
        # left wall: plaster above a wood wainscot
        poly(s, WALL_L, [P(0, 0, 0), P(0, RY, 0), P(0, RY, RH), P(0, 0, RH)])
        L(shade(WALL_L, 0.95), 0, RY, 44, RH)
        L(shade(WALL_L, 0.89), 0, RY, 53, RH)
        for y in range(0, RY, 2):                               # dithered edges between the tones
            dot(shade(WALL_L, 0.95), 0, y, 43)
            dot(shade(WALL_L, 0.89), 0, y + 1, 52)
        for _ in range(150):
            px, py = P(0, rnd.uniform(1, RY - 3), rnd.uniform(17, RH - 1))
            s.fill(shade(WALL_L, rnd.choice((0.88, 0.92, 0.96, 1.04))), (px, py, rnd.choice((1, 2, 3)), 1))
        L(shade(WOOD, 1.04), 0, RY, 0, 15)
        L(shade(WOOD, 0.9), 0, RY, 0, 6)
        for y in range(5, RY, 5):
            pygame.draw.line(s, shade(WOOD, 0.74), P(0, y, 0), P(0, y, 14))
            pygame.draw.line(s, shade(WOOD, 1.2), P(0, y + 1, 1), P(0, y + 1, 13))
        pygame.draw.line(s, shade(WOOD, 1.36), P(0, 0, 15), P(0, RY, 15))
        pygame.draw.line(s, shade(WOOD, 0.56), P(0, 0, 14), P(0, RY, 14))
        # curtains and their rail
        for y0, y1 in ((63.5, 68), (92, 96.5)):
            L((170, 74, 92), y0, y1, 31, 56)
            L((136, 54, 76), y0, y1, 31, 34)
            pygame.draw.line(s, (128, 50, 72), P(0, (y0 + y1) / 2, 32), P(0, (y0 + y1) / 2, 55))
            pygame.draw.line(s, (206, 104, 118), P(0, y0 + 1, 34), P(0, y0 + 1, 55))
        pygame.draw.line(s, (86, 66, 60), P(0, 62, 57), P(0, 98, 57))
        # books on a shelf over the bed
        y = 22.0
        while y < 46:
            w, h = rnd.choice((1.6, 2.0, 2.4)), rnd.choice((6, 7, 8, 9))
            c = rnd.choice(((176, 70, 66), (70, 110, 170), (214, 170, 80), (90, 150, 110), (150, 96, 150), (226, 214, 190)))
            L(c, y, y + w, 48, 48 + h)
            dot(shade(c, 1.3), 0, y + w, 48 + h - 1.5)
            y += w + rnd.choice((0.3, 0.3, 1.6))
        dot((200, 108, 72), 0, 49, 48, 3, 2, -1, -2)             # a little pot at the end of the shelf
        dot((90, 170, 100), 0, 49, 48, 3, 2, -1, -4)
        box(s, 0, 20, 46.5, 2.5, 52, 48, shade(WOOD, 1.1))
        # right wall: exposed brick
        mortar = shade(BRICK, 0.56)
        poly(s, mortar, [P(0, 0, 0), P(RX, 0, 0), P(RX, 0, RH), P(0, 0, RH)])
        z, r = 0.0, 0
        while z < RH:
            x = -5 * (r % 2)
            z1 = min(RH, z + 4.8)
            while x < RX:
                x0, x1 = max(0, x), min(RX, x + 10)
                c = shade(mix(BRICK, (150, 110, 96), rnd.random() * 0.5), rnd.uniform(0.78, 1.08))
                Rw(c, x0, x1, z, z1)
                pygame.draw.line(s, shade(c, 1.16), P(x0 + 1, 0, z1 - 1.2), P(x1 - 1, 0, z1 - 1.2))
                pygame.draw.line(s, mortar, P(x0, 0, z), P(x0, 0, z1))
                x += 10
            pygame.draw.line(s, mortar, P(0, 0, z), P(RX, 0, z))
            z, r = z1, r + 1
        Rw(shade(WOOD, 0.7), 0, RX, 0, 3)
        pygame.draw.line(s, shade(WOOD, 1.0), P(0, 0, 3), P(RX, 0, 3))
        # kitchen: tiled splashback, a rail of utensils, a plant hanging over the fridge
        Rw((204, 214, 210), 76, 100, 14, 23, (120, 130, 136))
        for x in range(80, 100, 4):
            pygame.draw.line(s, (160, 172, 172), P(x, 0, 14), P(x, 0, 23))
        pygame.draw.line(s, (160, 172, 172), P(76, 0, 18.5), P(100, 0, 18.5))
        pygame.draw.line(s, (70, 64, 74), P(78, 0, 25.5), P(98, 0, 25.5))
        for x, c in ((80, (200, 200, 210)), (83, (190, 120, 70)), (86, (200, 200, 210))):
            pygame.draw.line(s, c, P(x, 0, 25), P(x, 0, 21.5))
        px, py = P(67, 0, 47)
        pygame.draw.line(s, (60, 52, 60), (px, py), (px, py - 5))
        for dx, n, c in ((-3, 7, (62, 140, 78)), (-2, 4, (124, 200, 112)), (2, 5, (62, 140, 78)), (3, 9, (90, 170, 96)),
                         (4, 5, (124, 200, 112))):
            s.fill(c, (px + dx, py - 1, 1, n))
        s.fill((90, 170, 96), (px - 2, py - 2, 5, 2))
        s.fill((124, 200, 112), (px - 1, py - 3, 2, 1))
        s.fill((200, 108, 72), (px - 2, py, 5, 3))
        s.fill((226, 138, 96), (px - 2, py, 5, 1))
        s.fill((160, 80, 56), (px + 2, py + 1, 1, 2))
        # a poster over the bed, the clock, a light switch, a socket
        Rw((34, 60, 104), 8, 22, 46, 57, (226, 214, 190))
        poly(s, (18, 34, 66), [P(9, 0, 47), P(13, 0, 52), P(16, 0, 49), P(19, 0, 53), P(21, 0, 47)])
        dot((250, 196, 96), 17, 0, 55, 2, 2)
        poly(s, (240, 232, 214), CLOCK_FACE, (84, 58, 50))
        pygame.draw.polygon(s, (120, 80, 56), CLOCK_FACE, 2)
        for a in range(4):
            px, py = P(CLOCK[0] + math.cos(a * 1.5708) * 3.2, 0, CLOCK[1] + math.sin(a * 1.5708) * 3.8)
            s.fill((130, 110, 100), (px, py, 1, 1))
        dot((236, 236, 240), 128, 0, 18, 1, 2)
        dot((236, 236, 240), 101, 0, 5, 1, 2)
        # ivy coming over the wall by the window
        for _ in range(26):
            y = rnd.uniform(80, 99)
            n = rnd.randint(1, 9)
            px, py = P(0, y, RH)
            for k in range(n):
                s.fill(rnd.choice(((52, 120, 72), (84, 160, 90), (40, 96, 66))), (px + rnd.choice((0, 0, 1)), py + k, 1, 1))

        # ---- floor ----
        for y in range(0, RY, 6):
            x = 0
            while x < RX:
                x1 = min(RX, x + rnd.choice((22, 30, 38, 46)))
                c = shade(FLOOR, rnd.uniform(0.86, 1.06))
                poly(s, c, [P(x, y), P(x1, y), P(x1, y + 6), P(x, y + 6)])
                pygame.draw.line(s, shade(c, 1.1), P(x + 1, y + 1.5), P(x1 - 1, y + 1.5))
                for _ in range(2):                             # grain
                    gx = rnd.uniform(x + 2, x1 - 6)
                    pygame.draw.line(s, shade(c, 0.9), P(gx, y + 3.5), P(gx + rnd.uniform(3, 8), y + 3.5))
                pygame.draw.line(s, shade(FLOOR, 0.66), P(x1, y), P(x1, y + 6))
                x = x1
            pygame.draw.line(s, shade(FLOOR, 0.68), P(0, y), P(RX, y))
        for i in range(0, 12):            # kitchen tiles
            for j in range(0, 3):
                c = (226, 220, 204) if (i + j) % 2 else (56, 60, 80)
                x, y = 58 + i * 6, j * 6
                poly(s, c, [P(x, y), P(x + 6, y), P(x + 6, y + 6), P(x, y + 6)])
                pygame.draw.line(s, shade(c, 1.2), P(x + 1, y + 1), P(x + 5, y + 1))
        # rug: border, field, a diamond in the middle, fringe at both ends
        poly(s, (150, 58, 72), [P(100, 16), P(128, 16), P(128, 38), P(100, 38)], (96, 36, 50))
        poly(s, (226, 166, 100), [P(103, 18.5), P(125, 18.5), P(125, 35.5), P(103, 35.5)])
        poly(s, (150, 58, 72), [P(105, 20.5), P(123, 20.5), P(123, 33.5), P(105, 33.5)])
        poly(s, (232, 190, 120), [P(114, 22), P(121, 27), P(114, 32), P(107, 27)])
        poly(s, (60, 110, 120), [P(114, 24.5), P(117.5, 27), P(114, 29.5), P(110.5, 27)])
        for y in range(17, 38, 2):
            dot((236, 214, 170), 99, y, 0)
            dot((236, 214, 170), 129, y, 0)
        # doormat and a pair of boots
        poly(s, (128, 92, 62), [P(131, 2), P(145, 2), P(145, 9), P(131, 9)], (84, 58, 44))
        pygame.draw.line(s, (170, 130, 88), P(133, 5.5), P(143, 5.5))
        dot((70, 50, 44), 126, 3, 0, 2, 3, 0, -3)
        dot((70, 50, 44), 128, 5, 0, 2, 3, 0, -3)
        # baked contact shadows
        sh = pygame.Surface((W, H), pygame.SRCALPHA)
        for a, pts in ((60, ((0, 0, 0), (3, 0, 0), (3, RY, 0), (0, RY, 0))),
                       (60, ((0, 0, 0), (RX, 0, 0), (RX, 3, 0), (0, 3, 0))),
                       (90, ((0, 60, 0), (61, 60, 0), (61, 64, 0), (0, 64, 0))),
                       (90, ((56, 0, 0), (60, 0, 0), (60, 64, 0), (56, 64, 0))),
                       (80, ((60, 12, 0), (101, 12, 0), (101, 15, 0), (60, 15, 0))),
                       (80, ((101, 10, 0), (128, 10, 0), (128, 13, 0), (101, 13, 0))),
                       (80, ((14, 63, 0), (17, 63, 0), (17, 99, 0), (14, 99, 0))),
                       (80, ((0, 96, 0), (17, 96, 0), (17, 99, 0), (0, 99, 0))),
                       (80, ((99, 55, 0), (131, 55, 0), (131, 58, 0), (99, 58, 0))),
                       (80, ((128, 39, 0), (131, 39, 0), (131, 58, 0), (128, 58, 0))),
                       (64, ((80, 0, 27), (101, 0, 27), (101, 0, 22), (80, 0, 22))),
                       (60, ((0, 20, 46.5), (0, 53, 46.5), (0, 53, 43), (0, 20, 43))),
                       (44, ((0, 0, RH), (RX, 0, RH), (RX, 0, RH - 5), (0, 0, RH - 5))),
                       (44, ((0, 0, RH), (0, RY, RH), (0, RY, RH - 5), (0, 0, RH - 5))),
                       (50, ((0, 0, 0), (0, 5, 0), (0, 5, RH), (0, 0, RH))),
                       (50, ((0, 0, 0), (5, 0, 0), (5, 0, RH), (0, 0, RH))),
                       (70, ((0, RY, yz), (RX + 3, RY, yz), (RX + 3, RY + 3, yz), (0, RY + 3, yz))),
                       (70, ((RX, 0, yz), (RX + 3, 0, yz), (RX + 3, RY + 3, yz), (RX, RY + 3, yz)))):
            pygame.draw.polygon(sh, (10, 8, 30, a), [P(*p) for p in pts])
        s.blit(sh, (0, 0))
        # ---- the stone footing, the step, the cut ends and tops of the walls ----
        for ax, ay, bx, by, f in ((0, RY, RX, RY, 1.0), (RX, RY, RX, 0, 0.72)):
            foot = shade((150, 144, 150), f)
            poly(s, foot, [P(ax, ay, 0), P(bx, by, 0), P(bx, by, yz), P(ax, ay, yz)], shade(foot, 0.5))
            n = int(math.hypot(bx - ax, by - ay) / 12)
            for i in range(1, n):
                x, y = lerp(ax, bx, i / n), lerp(ay, by, i / n)
                pygame.draw.line(s, shade(foot, 0.7), P(x, y, -0.5), P(x, y, yz))
            pygame.draw.line(s, shade(FLOOR, 0.84 * f), P(ax, ay, 0), P(bx, by, 0))
        box(s, 96, RY, yz, 114, RY + 5, -1.8, (150, 144, 150), bands=1)
        ws = shade(WALL_L, 0.6)
        poly(s, ws, [P(-4, RY, yz), P(0, RY, yz), P(0, RY, RH), P(-4, RY, RH)], shade(ws, 0.5))
        poly(s, shade(WOOD, 0.7), [P(-4, RY, yz), P(0, RY, yz), P(0, RY, 15), P(-4, RY, 15)], shade(WOOD, 0.4))
        be = shade(BRICK, 0.52)
        poly(s, be, [P(RX, 0, yz), P(RX, -4, yz), P(RX, -4, RH), P(RX, 0, RH)], shade(be, 0.5))
        zz = 0.0
        while zz < RH:
            pygame.draw.line(s, shade(be, 0.6), P(RX, 0, zz), P(RX, -4, zz))
            zz += 4.8
        cap = (214, 200, 184)
        poly(s, shade(cap, 0.86), [P(-4, -4, RH), P(0, 0, RH), P(0, RY, RH), P(-4, RY, RH)], shade(cap, 0.45))
        poly(s, cap, [P(-4, -4, RH), P(RX, -4, RH), P(RX, 0, RH), P(0, 0, RH)], shade(cap, 0.45))
        pygame.draw.line(s, shade(BRICK, 0.4), P(0, 0, 0), P(0, 0, RH))
        # wall-light brackets (their shades are drawn lit or unlit each frame) and the porch lamp
        for px, py in SCONCES:
            s.fill((50, 42, 56), (px, py + 3, 1, 3))
            s.fill((50, 42, 56), (px - 1, py + 5, 3, 1))
        px, py = PORCH
        s.fill((40, 36, 52), (px, py - 2, 3, 1))
        s.fill((255, 206, 120), (px, py - 1, 3, 4))
        em.fill((255, 214, 130), (px, py - 1, 3, 4))
        em.fill((255, 246, 204), (px + 1, py, 1, 2))
        add_glow(fxs, (px + 1, py + 1), 15, (96, 58, 20))
        # an owl on the wall
        ox, oy = OWL
        s.fill((104, 78, 66), (ox - 2, oy - 6, 5, 6))
        s.fill((150, 118, 96), (ox - 1, oy - 3, 3, 3))
        s.fill((104, 78, 66), (ox - 2, oy - 7, 1, 1))
        s.fill((104, 78, 66), (ox + 2, oy - 7, 1, 1))
        s.fill((70, 52, 46), (ox - 2, oy - 6, 5, 1))

        # ---- the loft: bathroom underneath, bedroom on top ----
        lw = (198, 184, 160)
        box(s, *LOFT, lw, top=shade(FLOOR, 1.04))
        for x in range(7, 56, 7):
            pygame.draw.line(s, shade(lw, 0.74), P(x, 60, 1), P(x, 60, 26))
            pygame.draw.line(s, shade(lw, 0.98), P(x + 1, 60, 1), P(x + 1, 60, 26))
        for y in range(7, 60, 7):
            pygame.draw.line(s, shade(lw, 0.52), P(56, y, 1), P(56, y, 26))
        for y in range(6, 60, 6):
            pygame.draw.line(s, shade(FLOOR, 0.8), P(0, y, 30), P(56, y, 30))
            pygame.draw.line(s, shade(FLOOR, 1.1), P(1, y + 1.5, 30), P(55, y + 1.5, 30))
        poly(s, shade(WOOD, 0.84), [P(0, 60, 30), P(56, 60, 30), P(56, 60, 27), P(0, 60, 27)], shade(WOOD, 0.4))
        poly(s, shade(WOOD, 0.6), [P(56, 0, 30), P(56, 60, 30), P(56, 60, 27), P(56, 0, 27)], shade(WOOD, 0.4))
        pygame.draw.line(s, shade(WOOD, 1.3), P(0, 60, 30), P(56, 60, 30))
        pygame.draw.line(s, shade(WOOD, 1.1), P(56, 0, 30), P(56, 60, 30))
        # on the loft's front: a towel on a rail, a framed print
        pygame.draw.line(s, (70, 64, 74), P(6, 60, 21), P(20, 60, 21))
        wall_rect(s, (226, 214, 190), "y", 24, 35, 9, 21, shade(WOOD, 0.6), 60)
        wall_rect(s, (120, 170, 200), "y", 25.5, 33.5, 10.5, 19.5, None, 60)
        px, py = P(29.5, 60, 13)
        s.fill((236, 130, 80), (px - 1, py - 2, 3, 2))
        s.fill((70, 130, 80), (px, py, 1, 2))
        # on its side: a dressing gown on a hook
        px, py = P(56, 47, 19)
        s.fill((230, 220, 150), (px, py - 1, 1, 1))
        # loft rug and slippers
        poly(s, (104, 134, 188), [P(14, 26, 30), P(34, 26, 30), P(34, 56, 30), P(14, 56, 30)], (66, 90, 144))
        poly(s, (140, 170, 216), [P(17, 29, 30), P(31, 29, 30), P(31, 53, 30), P(17, 53, 30)])
        poly(s, (104, 134, 188), [P(20, 33, 30), P(28, 33, 30), P(28, 49, 30), P(20, 49, 30)])
        dot((236, 130, 96), 36, 22, 30, 2, 1)
        dot((236, 130, 96), 36, 25, 30, 2, 1)
        # bed
        box(s, 4, 8, 30, 30, 46, 34, WOOD)
        box(s, 5, 9, 34, 29, 45, 37, (240, 236, 226), bands=1)
        box(s, 4, 6, 30, 30, 8, 44, shade(WOOD, 0.9))          # headboard
        pygame.draw.line(s, shade(WOOD, 0.6), P(17, 8, 35), P(17, 8, 43))
        box(s, 9, 10, 37, 25, 18, 39.4, (250, 250, 250), bands=1)   # pillow
        # ladder
        for x in (41, 49):
            pygame.draw.line(s, shade(WOOD, 0.5), P(x, 61, 0), P(x, 61, 33))
            pygame.draw.line(s, shade(WOOD, 1.1), P(x - 1, 61, 0), P(x - 1, 61, 33))
        for z in range(4, 31, 5):
            pygame.draw.line(s, shade(WOOD, 1.0), P(41, 61, z), P(49, 61, z))
            pygame.draw.line(s, shade(WOOD, 0.5), P(41, 61, z - 1.2), P(49, 61, z - 1.2))
        # the wire the fairy lights hang on
        pygame.draw.lines(s, (58, 46, 60), False, [P(*p) for p in FAIRY])
        return s

    def sprite(self, key, draw):
        """Draw something that never changes once, keep it as a cropped sprite, return (surface, position)."""
        hit = self.cache.get(key)
        if hit is None:
            tmp = self.scratch
            tmp.fill(KEY)
            draw(tmp)
            r = pygame.mask.from_surface(tmp).get_bounding_rects()
            r = r[0].unionall(r[1:]) if r else pygame.Rect(0, 0, 1, 1)
            piece = tmp.subsurface(r).copy()
            piece.set_colorkey(KEY, pygame.RLEACCEL)
            hit = self.cache[key] = (piece, r.topleft)
        return hit

    def furniture(self, s, name):
        """Paint one piece of furniture (called once per look; the result is cached)."""
        b = BOXES[name]

        def dot(col, x, y, z, w=1, h=1, dx=0, dy=0):
            px, py = P(x, y, z)
            s.fill(col, (px + dx, py + dy, w, h))

        if name == "fridge":
            box(s, *b, TEAL)
            wall_rect(s, shade(TEAL, 1.04), "y", 61, 64.5, 20.5, 29, None, 12)      # sheen down the doors
            wall_rect(s, shade(TEAL, 1.0), "y", 61, 64.5, 2, 18, None, 12)
            pygame.draw.line(s, shade(TEAL, 0.5), P(60, 12, 19.4), P(74, 12, 19.4))
            pygame.draw.line(s, shade(TEAL, 1.2), P(60, 12, 18.4), P(74, 12, 18.4))
            for z in (22, 12):
                dot((236, 244, 244), 72, 12, z, 1, 4, 0, -1)
                dot((120, 150, 150), 72, 12, z, 1, 1, 1, 2)
            dot((240, 220, 90), 66, 12, 26, 2, 2)                                  # a note and two magnets
            dot((230, 90, 80), 68.5, 12, 27)
            dot((250, 250, 250), 66, 12, 23)
            for z in (4, 7, 10):
                pygame.draw.line(s, shade(TEAL, 0.5), P(74, 2, z), P(74, 10, z))   # vent on the side
            if self.fridge_open:
                wall_rect(s, (255, 246, 190), "y", 61, 73, 1, 18, shade(TEAL, 0.4), 12)
                for z in (7, 13):
                    pygame.draw.line(s, (190, 170, 110), P(61, 12, z), P(73, 12, z))
                for i in range(self.man.food):
                    px, py = P(63 + i * 3, 12, 8 + (i % 2) * 6)
                    s.fill(((220, 80, 60), (90, 170, 80), (240, 200, 70), (240, 240, 240))[i % 4], (px, py - 3, 2, 3))
                wall_rect(s, shade(TEAL, 0.78), "x", 12, 25, 1, 18, shade(TEAL, 0.4), 60)
                wall_rect(s, (236, 244, 240), "x", 14, 23, 3, 16, None, 60)
        elif name == "counter":
            wood = (176, 120, 78)
            box(s, *b, wood, top=(232, 226, 210))
            for x0, x1 in ((77, 87.5), (88.5, 99)):                                # cupboard doors
                wall_rect(s, shade(wood, 0.98), "y", x0, x1, 1, 12, shade(wood, 0.6), 12)
                wall_rect(s, shade(wood, 0.84), "y", x0 + 1.5, x1 - 1.5, 2.5, 10.5, None, 12)
            dot((240, 214, 140), 86, 12, 8, 1, 2)
            dot((240, 214, 140), 90, 12, 8, 1, 2)
            pygame.draw.line(s, shade((232, 226, 210), 0.8), P(76, 12, 13), P(100, 12, 13))
            px, py = P(92, 6, 14)                                                  # sink and tap
            pygame.draw.ellipse(s, (150, 156, 166), (px - 5, py - 2, 11, 5))
            pygame.draw.ellipse(s, (70, 74, 90), (px - 4, py - 1, 9, 3))
            s.fill((120, 130, 150), (px - 2, py, 4, 1))
            s.fill((200, 206, 216), (px + 4, py - 5, 1, 4))
            s.fill((200, 206, 216), (px + 2, py - 5, 2, 1))
            for y in (3.5, 8.5):                                                   # hob
                px, py = P(79.5, y, 14)
                pygame.draw.ellipse(s, (60, 60, 72), (px - 3, py - 1, 6, 3))
                s.fill((110, 110, 126), (px - 1, py, 2, 1))
        elif name == "shelf":
            for x in (83, 97):
                pygame.draw.line(s, (70, 60, 66), P(x, 0, 27), P(x, 0, 23))
                pygame.draw.line(s, (70, 60, 66), P(x, 0, 23), P(x, 5, 27))
            box(s, *b, shade(WOOD, 1.1))
        elif name == "tvstand":
            wood = shade(WOOD, 0.85)
            box(s, *b, wood)
            wall_rect(s, shade(wood, 0.5), "y", 103.5, 113, 1, 6.5, None, 10)      # an open shelf with a console in it
            wall_rect(s, (60, 60, 76), "y", 105, 111, 1.5, 4, None, 10)
            dot((120, 240, 150), 110, 10, 3)
            wall_rect(s, shade(wood, 0.98), "y", 115, 125, 1, 6.5, shade(wood, 0.6), 10)
            dot((240, 214, 140), 120, 10, 4.5, 2, 1)
        elif name == "tankstand":
            wood = shade(WOOD, 0.74)
            box(s, *b, wood)
            for y0 in (65, 75.5, 86):
                wall_rect(s, shade(wood, 0.74), "x", y0, y0 + 9, 1.5, 10.5, shade(wood, 0.45), 14)
                dot((240, 214, 140), 14, y0 + 4.5, 7)
        elif name == "seat":
            box(s, *b, PLUM, bands=1)
            pygame.draw.line(s, shade(PLUM, 0.72), P(114, 40, 7), P(114, 51, 7))
            box(s, 104, 42, 7, 109, 49, 10.5, (70, 150, 150), bands=1)             # a cushion
        elif name == "back":
            c = shade(PLUM, 0.92)
            box(s, *b, c)
            pygame.draw.line(s, shade(c, 0.66), P(114, 55, 2), P(114, 55, 14))
            pygame.draw.line(s, shade(c, 1.1), P(113, 55, 2), P(113, 55, 14))
            wall_rect(s, (228, 214, 180), "y", 117, 123.5, 6, 15, (150, 136, 110), 55)   # a throw over the back
            for z in (8, 10.5, 13):
                pygame.draw.line(s, (190, 110, 80), P(117, 55, z), P(123.5, 55, z))
            for x in (104, 124):
                dot((40, 30, 36), x, 55, 0, 1, 1, 0, 0)
        else:
            box(s, *b, shade(PLUM, 0.84))

    def draw_box(self, surf, name):
        if name == "tank":
            self.draw_tank(surf)
            return
        key = (name, self.fridge_open, self.man.food) if name == "fridge" else name
        piece, at = self.sprite(key, lambda s: self.furniture(s, name))
        surf.blit(piece, at)

    def paint_towel(self, s):
        wall_rect(s, (238, 236, 230), "y", 8, 15, 10, 21, (150, 150, 160), 60)
        for z in (12, 14.5):
            pygame.draw.line(s, (80, 160, 160), P(8, 60, z), P(15, 60, z))

    def paint_gown(self, s):
        wall_rect(s, (110, 150, 200), "x", 44, 50, 6, 19, (70, 100, 150), 56)
        pygame.draw.line(s, (150, 190, 230), P(56, 45.5, 8), P(56, 45.5, 18))

    def paint_door(self, s):
        wall_rect(s, shade(WOOD, 1.3), "y", 129.4, 146.6, 0, 31.8)
        wall_rect(s, WOOD, "y", 130, 146, 0, 31, shade(WOOD, 0.4))
        for z0, z1 in ((3, 14), (17, 28)):
            wall_rect(s, shade(WOOD, 0.84), "y", 133, 143, z0, z1, shade(WOOD, 0.6))
            pygame.draw.line(s, shade(WOOD, 1.16), P(134, 0, z0 + 1.4), P(142, 0, z0 + 1.4))
        px, py = P(143.5, 0, 15.5)
        s.fill((250, 220, 110), (px, py, 2, 2))
        s.fill((255, 246, 190), (px, py, 1, 1))

    def paint_coat(self, s):
        c = (66, 108, 96)
        wall_rect(s, shade(c, 0.74), "y", 134.4, 142.6, 13, 22)             # sleeves
        wall_rect(s, c, "y", 135.5, 141.5, 10, 24.5, shade(c, 0.5))
        wall_rect(s, shade(c, 1.22), "y", 136.5, 138, 11, 23)
        wall_rect(s, (220, 98, 70), "y", 137.6, 139.6, 19, 24.5)            # scarf
        px, py = P(138.5, 0, 25.5)
        s.fill((236, 224, 150), (px, py, 1, 1))

    def paint_bath_door(self, s):
        c = shade(WOOD, 0.9)
        poly(s, c, BATH_DOOR, shade(WOOD, 0.4))
        for z0, z1 in ((3, 11), (14, 22)):
            wall_rect(s, shade(c, 0.82), "x", 25.5, 34.5, z0, z1, shade(c, 0.62), 56)
        px, py = P(56, 26, 12.5)
        s.fill((250, 220, 110), (px, py - 1, 1, 2))

    def paint_blanket(self, s, lying):
        z = 40 if lying else 38
        col = (88, 110, 196)
        box(s, 5, 20, 37, 29, 45, z, col, bands=1)
        pygame.draw.line(s, (206, 214, 250), P(5.5, 22.5, z), P(28.5, 22.5, z))
        for y in (28, 34, 40):
            pygame.draw.line(s, shade(col, 0.9), P(6, y, z), P(28, y, z))
        if lying:
            poly(s, shade(col, 1.36), [P(12, 24, z), P(22, 24, z), P(22, 42, z), P(12, 42, z)])
            poly(s, shade(col, 1.52), [P(14, 26, z), P(19, 26, z), P(19, 40, z), P(14, 40, z)])

    def paint_arm(self, s):
        c = shade(WOOD, 0.82)
        wall_rect(s, shade(c, 0.7), "y", 94, 98, 45, 60)
        pygame.draw.line(s, shade(c, 0.55), P(96, 0.5, 46), P(96, 15, 57.5), 2)
        box(s, 94.5, 0, 57.5, 97.5, 59, 60, c, bands=1)

    def lamp_pos(self):
        """Where the bulb is, in room coordinates."""
        d = math.hypot(*self.lamp_d)
        return (LAMP_AT[0] + self.lamp_d[0], LAMP_AT[1] + self.lamp_d[1],
                RH - 2.5 - math.sqrt(max(1.0, LAMP_L ** 2 - d * d)))

    def relight(self, day, flick, lamp_on, lx, ly, tv_lit, wet, fairy, hero=None):
        """Rebuild the light map (multiplied over the scene) and the glow layer (added on top)."""
        t = self.t
        amb = mix(mix((54, 64, 122), (98, 92, 140), day), (236, 232, 224), self.sky_q[1] / 12.0)
        if self.lights_off:
            amb = shade(amb, 0.82)
        if flick:
            amb = shade(amb, 0.8)

        def lit(c):
            return (min(255, amb[0] + c[0]), min(255, amb[1] + c[1]), min(255, amb[2] + c[2]))

        lm, fx = self.lm, self.fx
        self.mask.to_surface(lm, setcolor=amb, unsetcolor=WHITE)
        pygame.draw.polygon(lm, shade(amb, 1.7), TERRACE)       # the garden is out in the moonlight
        pygame.draw.polygon(lm, amb, HULL)
        fx.blit(self.fxs, (0, 0))
        # things that shine by themselves keep their own colour
        pygame.draw.polygon(lm, WHITE, WIN)
        pygame.draw.polygon(lm, lit(mix((40, 60, 120), (124, 84, 60), day)), SHAFT)
        pygame.draw.line(lm, amb, P(33, 64), P(38, 98))
        pygame.draw.line(lm, amb, P(22, 81), P(49, 81))
        tv = self.items["tv"]
        tvc = int(t * 5) % 4
        if tv_lit:
            x, y, z = tv.x, tv.y, tv.z
            pygame.draw.polygon(lm, WHITE, [P(x - 8, y + 1, z + 2), P(x + 8, y + 1, z + 2), P(x + 8, y + 1, z + 11),
                                            P(x - 8, y + 1, z + 11)])
        if wet:
            x0, y0, z0, x1, y1, z1 = TANK
            pygame.draw.polygon(lm, lit((96, 124, 150)), [P(x0, y1, z1), P(x0, y0, z1), P(x1, y0, z1), P(x1, y0, z0),
                                                          P(x1, y1, z0), P(x0, y1, z0)])
        if self.fridge_open:
            pygame.draw.polygon(lm, WHITE, [P(61, 12, 1), P(73, 12, 1), P(73, 12, 18), P(61, 12, 18)])
        if self.bath_on:
            pygame.draw.polygon(lm, WHITE, BATH_DOOR)

        # the lamp: its pool of light, with the shadows the furniture throws cut out of it
        bx, by, bz = self.lamp_pos()
        floor = P(bx, by, 0)
        if lamp_on:
            ll, shs = self.ll, self.shs
            ll.fill((0, 0, 0))
            add_light(ll, (lx, ly + 7), 92, 70, (214, 148, 84))
            add_light(ll, floor, 56, 21, (70, 46, 22))
            shs.fill(WHITE)
            shapes = []
            for (x0, y0, z0, x1, y1, z1), (cx0, cy0, cx1, cy1) in CASTERS:
                k = min(4.0, (bz - z0) / max(5.0, bz - z1))
                pts = []
                for cx, cy in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
                    pts.append(P(cx, cy, z0))
                    pts.append(P(clamp(bx + (cx - bx) * k, cx0, cx1), clamp(by + (cy - by) * k, cy0, cy1), z0))
                h = hull(pts)
                if len(h) >= 3:
                    shapes.append(h)
            for h in shapes:                       # soft edge first, then the dark middle
                pygame.draw.polygon(shs, (176, 178, 200), h)
                pygame.draw.polygon(shs, (176, 178, 200), h, 3)
            for h in shapes:
                pygame.draw.polygon(shs, (84, 88, 122), h)
            for pts, c, n in FACES:                # each side is lit by how squarely it faces the lamp
                v = 255
                if c:
                    l = (bx - c[0], by - c[1], bz - c[2])
                    f = (l[0] * n[0] + l[1] * n[1] + l[2] * n[2]) / max(1.0, math.sqrt(l[0] ** 2 + l[1] ** 2 + l[2] ** 2))
                    v = int(255 * (0.3 + 0.7 * math.sqrt(f))) if f > 0 else 76
                pygame.draw.polygon(shs, (v, v, min(255, v + 12)), pts)
            ll.blit(shs, (0, 0), special_flags=pygame.BLEND_RGB_MULT)
            lm.blit(ll, (0, 0), special_flags=pygame.BLEND_RGB_ADD)
        # the smaller lights
        if fairy:
            c = (70, 40, 16) if self.lights_off else (100, 58, 22)
            for p in ((10, 62, 22), (30, 62, 22), (50, 62, 22), (58, 44, 22), (58, 20, 22)):
                add_light(lm, P(*p), 24, 18, c)
        if tv_lit:
            add_light(lm, P(tv.x, tv.y + 20, tv.z), 48, 28, ((44, 76, 130), (120, 80, 44), (54, 108, 76),
                                                             (106, 106, 124))[tvc])
        if wet:
            add_light(lm, P(14, 80, 16), 30, 24, (24, 70, 100))
        if self.bath_on:
            add_light(lm, P(62, 30, 10), 36, 26, (160, 100, 40))
        if self.fridge_open:
            add_light(lm, P(68, 20, 8), 30, 22, (130, 126, 96))
        add_light(lm, P(2, 80, 44), 26, 26, (30, 44, 84) if day < 0.5 else (84, 56, 40))
        for x, y in GLAMPS:
            add_light(lm, P(x, y, YZ), 24, 10, (150, 96, 40))
        add_light(lm, (PORCH[0] + 4, PORCH[1] + 12), 26, 24, (150, 98, 44))
        if fairy and not self.lights_off:              # the wall lights and the kitchen strip
            for px, py in SCONCES:
                add_light(lm, (px, py + 6), 30, 24, (150, 102, 54))
                add_glow(fx, (px, py + 1), 9, (96, 60, 22))
            add_light(lm, P(90, 8, 15), 34, 16, (150, 124, 84))
            add_glow(fx, P(90, 2, 25), 12, (60, 46, 24))
        if hero:
            add_light(lm, hero, 26, 22, (96, 100, 124))

        # light you can see in the air
        pygame.draw.polygon(fx, (8, 12, 24) if day < 0.5 else (24, 16, 10), BEAM)
        if lamp_on:
            pygame.draw.polygon(fx, (15, 9, 3), [(lx - 3, ly + 5), (lx + 3, ly + 5), (floor[0] + 30, floor[1]),
                                                 (floor[0] - 30, floor[1])])
            pygame.draw.ellipse(fx, (15, 9, 3), (floor[0] - 30, floor[1] - 10, 60, 20))
            add_glow(fx, (lx, ly + 5), 24, (86, 54, 22))
            add_glow(fx, (lx, ly + 5), 8, (130, 100, 56))
        if fairy:
            wink = int(t * 2)
            for i, (px, py) in enumerate(FAIRY_S):
                if (i * 7 + wink) % 9:
                    add_glow(fx, (px, py + 1), 4, (76, 44, 14))
        if tv_lit:
            add_glow(fx, P(tv.x, tv.y + 3, tv.z + 7), 20, ((20, 36, 62), (56, 38, 20), (24, 50, 34), (48, 48, 58))[tvc])
        if wet:
            add_glow(fx, P(11, 80, 20), 22, (10, 34, 52))
        if self.bath_on:
            add_glow(fx, P(57, 30, 12), 22, (92, 56, 18))
        if self.fridge_open:
            add_glow(fx, P(67, 14, 10), 16, (70, 66, 42))
        add_glow(fx, P(0, 80, 44), 18, (18, 26, 54) if day < 0.5 else (60, 36, 24))

    def draw_tank(self, surf):
        x0, y0, z0, x1, y1, z1 = TANK
        t = self.t
        inv = self.g[2] > 0.1
        # the inside, seen through the glass
        poly(surf, (20, 38, 66), [P(x0, y1, z1), P(x1, y1, z1), P(x1, y1, z0), P(x0, y1, z0)])
        poly(surf, (24, 46, 78), [P(x1, y0, z1), P(x1, y1, z1), P(x1, y1, z0), P(x1, y0, z0)])
        poly(surf, (30, 56, 92), [P(x0, y0, z1), P(x1, y0, z1), P(x1, y1, z1), P(x0, y1, z1)])
        wob = clamp(math.hypot(*self.sloshv) * 0.5, 0, 1.5)
        zc = {}
        for i, (cx, cy) in enumerate(((x0, y0), (x1, y0), (x1, y1), (x0, y1))):
            z = self.water_z(cx, cy) + math.sin(t * 11 + i * 1.7) * wob
            if inv:
                z = z1 - (z - z0)
            zc[(cx, cy)] = clamp(z, z0, z1)
        wa, wb, wt = (50, 124, 208), (40, 104, 190), (136, 204, 246)
        a, b, c = zc[(x0, y1)], zc[(x1, y1)], zc[(x1, y0)]
        if not inv:
            poly(surf, wt, [P(x0, y0, zc[(x0, y0)]), P(x1, y0, c), P(x1, y1, b), P(x0, y1, a)])
            poly(surf, wa, [P(x0, y1, a), P(x1, y1, b), P(x1, y1, z0), P(x0, y1, z0)])
            poly(surf, wb, [P(x1, y0, c), P(x1, y1, b), P(x1, y1, z0), P(x1, y0, z0)])
            # brighter near the surface, darker in the depths
            if min(b, c) - z0 > 7:
                poly(surf, shade(wb, 1.24), [P(x1, y0, c), P(x1, y1, b), P(x1, y1, b - 3.5), P(x1, y0, c - 3.5)])
                poly(surf, shade(wb, 0.82), [P(x1, y0, z0 + 4), P(x1, y1, z0 + 4), P(x1, y1, z0), P(x1, y0, z0)])
                poly(surf, shade(wa, 1.2), [P(x0, y1, a), P(x1, y1, b), P(x1, y1, b - 3.5), P(x0, y1, a - 3.5)])
        else:
            poly(surf, wa, [P(x0, y1, a), P(x1, y1, b), P(x1, y1, z1), P(x0, y1, z1)])
            poly(surf, wb, [P(x1, y0, c), P(x1, y1, b), P(x1, y1, z1), P(x1, y0, z1)])
        pygame.draw.line(surf, wt, P(x1, y0, c), P(x1, y1, b))
        pygame.draw.line(surf, wt, P(x0, y1, a), P(x1, y1, b))
        # gravel, a rock, weed
        for i, y in enumerate(range(y0 + 1, y1)):
            px, py = P(x1, y, z0 + 1)
            surf.fill((214, 194, 146) if i % 3 else (160, 140, 110), (px, py - 1, 1, 2))
        for x in range(x0 + 1, x1, 2):
            px, py = P(x, y1, z0 + 1)
            surf.fill((196, 176, 132), (px, py - 1, 1, 2))
        px, py = P(x1, 88, z0 + 2)
        surf.fill((84, 90, 110), (px - 2, py - 2, 5, 3))
        surf.fill((130, 138, 160), (px - 1, py - 3, 3, 1))
        for y, h, ph in ((70, 6, 0.0), (74, 3, 1.3), (92, 5, 2.1)):
            px, py = P(x1, y, z0 + 2)
            for k in range(h):
                sway = int(round(math.sin(t * 1.4 + ph + k * 0.5) * (k / 3.0)))
                surf.fill((66, 170, 100) if k % 2 else (40, 130, 84), (px + sway, py - k - 1, 1, 1))
        if self.tank_level > 0.2 and not inv:
            for i in range(4):
                yb = 69 + i * 7
                zb = z0 + 2 + (t * (5 + i) + i * 4) % max(1.0, self.water_z(x1, yb) - z0 - 3)
                surf.fill((206, 238, 250), (*P(x1, yb, zb), 1, 1))
            # a small blue fish that keeps to itself
            y2 = 80 + math.sin(t * 0.45) * 10
            z2 = clamp(z0 + 4 + math.sin(t * 0.9) * 1.5, z0 + 2, self.water_z(x1, y2) - 2)
            qx, qy = P(x1, y2, z2)
            d2 = 1 if math.cos(t * 0.45) > 0 else -1
            surf.fill((120, 200, 250), (qx - 1, qy, 3, 2))
            surf.fill((70, 140, 220), (qx + (2 if d2 > 0 else -2), qy, 1, 2))
        # the goldfish
        fx, fy = P(x1, self.fish_y, self.fish_z)
        d = self.fish_dir          # +y is screen-left
        flick = int(t * 6) % 2
        surf.fill((246, 132, 44), (fx - 2, fy - 1, 5, 3))
        surf.fill((255, 176, 84), (fx - 1, fy - 1, 3, 1))
        surf.fill((255, 226, 170), (fx - 1, fy + 1, 2, 1))
        tx = fx + (3 if d > 0 else -3)
        surf.fill((246, 132, 44), (tx, fy - 2 + flick, 1, 4))
        surf.fill((255, 176, 84), (tx + (1 if d > 0 else -1), fy - 1 + flick, 1, 2))
        surf.fill(INK, (fx - (2 if d > 0 else -2), fy - 1, 1, 1))
        if self.tank_level < 0.3 and int(t * 3) % 2:
            text(surf, "!", fx - 1, fy - 9, (255, 90, 80))
        # glass: bright edges, a streak of reflection
        g2 = (224, 244, 248)
        pygame.draw.lines(surf, shade(g2, 0.6), False, [P(x0, y1, z1), P(x0, y0, z1), P(x1, y0, z1)])
        pygame.draw.lines(surf, g2, True, [P(x0, y1, z1), P(x1, y1, z1), P(x1, y1, z0), P(x0, y1, z0)])
        pygame.draw.lines(surf, g2, True, [P(x1, y0, z1), P(x1, y1, z1), P(x1, y1, z0), P(x1, y0, z0)])
        px, py = P(x1, y1 - 4, z1 - 3)
        for k in range(4):
            surf.fill((200, 232, 250), (px + k, py + k, 1, 1), pygame.BLEND_RGB_ADD)
            surf.fill((120, 150, 170), (px + k + 3, py + k, 1, 1), pygame.BLEND_RGB_ADD)

    def draw_item(self, surf, it):
        sx, sy = P(it.x, it.y, it.z)
        if it.name == "tv":
            x, y, z = it.x, it.y, it.z
            box(surf, x - 4, y - 1.5, z, x + 4, y + 1.5, z + 1.2, (36, 36, 48), bands=1)
            box(surf, x - 9, y - 1, z + 1, x + 9, y + 1, z + 12, (44, 44, 58), bands=1)
            if self.tv_on:
                c = ((90, 150, 230), (230, 160, 90), (120, 210, 150), (220, 220, 240))[int(self.t * 5) % 4]
            else:
                c = (22, 26, 42)
            poly(surf, c, [P(x - 8, y + 1, z + 2), P(x + 8, y + 1, z + 2), P(x + 8, y + 1, z + 11),
                           P(x - 8, y + 1, z + 11)])
            if self.tv_on:
                poly(surf, shade(c, 0.74), [P(x - 8, y + 1, z + 2), P(x + 8, y + 1, z + 2), P(x + 8, y + 1, z + 5),
                                            P(x - 8, y + 1, z + 5)])
                bx, by = P(x - 3 + int(self.t * 3) % 6, y + 1, z + 5)
                surf.fill((250, 250, 250), (bx, by - 3, 2, 3))
                surf.fill((250, 250, 250), (bx - 1, by - 2, 4, 1))
            else:
                pygame.draw.line(surf, (60, 66, 90), P(x - 7, y + 1, z + 8), P(x - 4, y + 1, z + 10.5))
        elif it.name == "picture":
            sw = it.swing * 7 if it.hung else 0
            x, y, z = it.x, it.y, it.z
            pts = [P(x - 6 + sw, y, z), P(x + 6 + sw, y, z), P(x + 6, y, z + 10), P(x - 6, y, z + 10)]
            poly(surf, (206, 164, 84), pts, (96, 66, 40))
            poly(surf, (120, 170, 210), [P(x - 4 + sw * 0.8, y, z + 2), P(x + 4 + sw * 0.8, y, z + 2),
                                         P(x + 4 + sw * 0.2, y, z + 8), P(x - 4 + sw * 0.2, y, z + 8)])
            poly(surf, (80, 150, 90), [P(x - 4 + sw * 0.8, y, z + 2), P(x + 4 + sw * 0.8, y, z + 2),
                                       P(x + 1 + sw * 0.5, y, z + 5), P(x - 2 + sw * 0.6, y, z + 4)])
            px, py = P(x + 2 + sw * 0.3, y, z + 7)
            surf.fill((250, 230, 150), (px, py, 1, 1))
        else:
            s = item_sprite(it.name)
            if not it.touch:
                s = pygame.transform.rotate(s, it.ang)
            elif self.g[2] > 0.3:
                s = pygame.transform.flip(s, False, True)
            surf.blit(s, (sx - s.get_width() // 2, sy - s.get_height() + 1 + (it.h if self.g[2] > 0.3 and it.touch else 0)))

    def draw_debris(self, surf, it):
        sx, sy = P(*it.debris)
        if it.name == "tv":
            poly(surf, (36, 36, 48), [(sx - 9, sy - 1), (sx + 6, sy - 5), (sx + 10, sy - 1), (sx - 4, sy + 3)], INK)
            pygame.draw.line(surf, (150, 190, 220), (sx - 4, sy - 1), (sx + 3, sy - 2))
            pygame.draw.line(surf, (150, 190, 220), (sx, sy - 3), (sx + 1, sy + 1))
        if it.name == "plant":
            pygame.draw.ellipse(surf, (90, 60, 44), (sx - 6, sy - 2, 12, 5))
        for dx, dy, c, n in it.bits:
            surf.fill(c, (sx + dx, sy + dy, n, n))

    def ghost(self, surf, name):
        """Dotted outline where a broken thing used to be."""
        if name == "lamp":
            cx, cy = P(LAMP_AT[0], LAMP_AT[1], RH - LAMP_L)
            w, h = 12, 10
        else:
            hx, hy, hz = ITEMS[name]["home"]
            cx, cy = P(hx, hy, hz)
            w, h = {"tv": (24, 16), "picture": (16, 14), "plant": (12, 14)}.get(name, (9, 11))
            cy -= h // 2
        c = (255, 226, 120) if int(self.t * 3) % 2 else (255, 255, 255)
        x0, y0 = cx - w // 2, cy - h // 2
        for i in range(0, w + 1, 2):
            surf.fill(c, (x0 + i, y0, 1, 1))
            surf.fill(c, (x0 + i, y0 + h, 1, 1))
        for i in range(0, h + 1, 2):
            surf.fill(c, (x0, y0 + i, 1, 1))
            surf.fill(c, (x0 + w, y0 + i, 1, 1))
        text(surf, "+", cx - 1, cy - 2, c)
        return cx, cy

    def targets(self):
        """Things the cursor can act on: (kind, name, screen x, screen y, radius)."""
        if self.party:
            return [("man", "man", P(p.x, p.y, p.z)[0], P(p.x, p.y, p.z)[1] - 12, 12) for p in self.party.people]
        out = []
        for n, it in self.items.items():
            if it.state == "broken":
                hx, hy, hz = it.home
                sx, sy = P(hx, hy, hz)
                out.append(("fix", n, sx, sy - 6, 13))
        if not self.lamp_ok:
            sx, sy = P(LAMP_AT[0], LAMP_AT[1], RH - LAMP_L)
            out.append(("fix", "lamp", sx, sy, 13))
        for i, (wall, pts) in enumerate(self.cracks):
            out.append(("crack", i, pts[2][0], pts[2][1], 9))
        sx, sy = P(13, 80, 20)
        out.append(("tank", "tank", sx, sy, 18))
        m = self.man
        if m.visible:
            sx, sy = P(m.x, m.y, m.z)
            out.append(("man", "man", sx, sy - 10, 12))
        return out

    def click(self, cx, cy):
        if self.party:                                  # poke whoever is nearest
            self.rings.append([cx, cy, 0.0])
            near = [p for p in self.party.people if not p.body
                    and math.hypot(cx - P(p.x, p.y, p.z)[0], cy - P(p.x, p.y, p.z)[1] + 12) < 14]
            if near:
                near[0].hop = 0.35
                near[0].say(random.choice(["HEY!", "WHO DID THAT?", "HA, STOP IT"]), 1.4)
            return
        best = None
        for kind, name, sx, sy, r in self.targets():
            d = math.hypot(cx - sx, cy - sy)
            if d <= r and (best is None or d < best[0]):
                best = (d, kind, name)
        self.rings.append([cx, cy, 0.0])
        if not best:
            if random.random() < 0.3:
                self.man.say(random.choice(["WHO'S THERE?", "DID YOU HEAR THAT?"]), 1.6)
            return
        _, kind, name = best
        if kind == "crack":
            if name < len(self.cracks):
                self.cracks.pop(name)
                self.spark(60, 30, 40, (255, 240, 170), 4)
        elif kind == "fix":
            self.replace(name)
        elif kind == "tank":
            if self.tank_level < 0.6:
                self.tank_level = 0.7
            self.spark(7, 80, 30, (230, 190, 90), 6)
        else:
            self.man.poke()

    def draw(self, surf):
        if self.party:
            self.party.draw(surf)
        else:
            self.draw_home(surf)
        self.ui(surf)

    def draw_home(self, surf):
        t = self.t
        day = self.daylight()
        m = self.man
        flick = bool(self.dark_now)

        # ---- the forest behind ----
        self.paint_sky(surf)
        ax, ay = LANTERN
        bx, by = ax + int(round(math.sin(self.lant) * 12)), ay + int(round(math.cos(self.lant) * 12))
        pygame.draw.line(surf, (8, 8, 22), (ax, ay), (bx, by))
        surf.fill((8, 8, 22), (bx - 2, by - 1, 5, 7))
        surf.fill((8, 8, 22), (bx - 1, by - 2, 3, 1))
        surf.fill((255, 190, 90), (bx - 1, by, 3, 5))
        surf.fill((255, 240, 190), (bx, by + 1, 1, 3))
        add_glow(surf, (bx, by + 2), 17, (88, 46, 12) if int(t * 9) % 4 else (70, 36, 10))
        ox, oy = self.fly_o
        for i, (qx, qy) in enumerate(() if (self.lite or self.sun() > 0.4) else FLIES):
            ph = i * 1.7
            x = int(qx + math.cos(t * (0.5 + i % 3 * 0.13) + ph) * 12 + ox)
            y = int(qy + math.sin(t * (0.7 + i % 4 * 0.11) + ph * 2) * 7 + oy)
            b = math.sin(t * 1.9 + ph * 3)
            if b > -0.3:
                surf.fill((255, 220, 130) if b > 0.4 else (210, 140, 56), (x, y, 1, 1))
                add_glow(surf, (x, y), 5, (76, 48, 12) if b > 0.4 else (38, 24, 6))
        for i in range(0 if (self.lite or self.sun() > 0.4) else len(EMBERS) * 2):
            ex, ey = EMBERS[i % len(EMBERS)]
            u = (t * (0.10 + i % 3 * 0.03) + i * 0.37) % 1.0
            x = int(ex + math.sin(u * 9 + i) * 6 + ox * u)
            y = int(ey - u * 44 + oy * u)
            surf.fill(mix((255, 176, 64), (110, 36, 20), u), (x, y, 1, 1))

        self.rain(surf)
        # ---- the terrace and the house, unlit ----
        surf.blit(self.room, (0, 0))
        for wall, pts in self.cracks:
            pygame.draw.lines(surf, (58, 44, 56), False, pts)
            pygame.draw.line(surf, (58, 44, 56), pts[2], (pts[2][0] + 3, pts[2][1] + 2))
        for x, y, r in self.puddles:
            px, py = P(x, y, 0)
            rw = max(2, int(r * 1.6))
            pygame.draw.ellipse(surf, (96, 150, 214), (px - rw, py - rw // 3, rw * 2, max(2, rw * 2 // 3)))
            surf.fill((190, 226, 250), (px - rw // 2, py - 1, max(1, rw // 2), 1))
        sway = math.sin(self.lant) * 5
        for key, paint in (("towel", self.paint_towel), ("gown", self.paint_gown)):
            piece, at = self.sprite(key, paint)
            hang(surf, piece, at, sway)
        # window on the left wall
        win = WIN
        poly(surf, mix((12, 22, 62), (70, 56, 120), day), win)
        poly(surf, mix((26, 56, 112), (226, 140, 106), day), [P(0, 68, 34), P(0, 92, 34), P(0, 92, 43), P(0, 68, 43)])
        poly(surf, mix((18, 38, 86), (150, 96, 114), day), [P(0, 68, 43), P(0, 92, 43), P(0, 92, 46), P(0, 68, 46)])
        if day > 0.5:
            pygame.draw.circle(surf, (255, 214, 140), P(0, 76, 38), 3)
            surf.fill((255, 240, 200), (P(0, 76, 38)[0] - 1, P(0, 76, 38)[1] - 1, 2, 2))
        else:
            mx, my = P(0, 76, 48)
            pygame.draw.circle(surf, (236, 240, 230), (mx, my), 2)
            for sy_, sz_ in ((87, 50), (71, 39), (84, 41), (90, 46)):
                surf.fill((220, 230, 255), (*P(0, sy_, sz_), 1, 1))
        if self.sun() > 0.5:                              # broad daylight outside
            poly(surf, (112, 172, 236), win)
            poly(surf, (170, 212, 244), [P(0, 68, 34), P(0, 92, 34), P(0, 92, 44), P(0, 68, 44)])
            poly(surf, seasonal((120, 170, 110)) if SEASON != "autumn" else (190, 130, 70), [P(0, 68, 34), P(0, 92, 34), P(0, 92, 37), P(0, 68, 37)])
        pygame.draw.polygon(surf, (236, 228, 210), win, 1)
        pygame.draw.line(surf, (236, 228, 210), P(0, 80, 34), P(0, 80, 54))
        pygame.draw.line(surf, (236, 228, 210), P(0, 68, 44), P(0, 92, 44))
        piece, at = self.sprite("sill", lambda s: box(s, 0, 66.5, 32.6, 3, 93.5, 34, shade(WOOD, 1.16), bands=1))
        surf.blit(piece, at)
        # the clock tells the real time
        lt = time.localtime()
        for frac, r, col in (((lt.tm_hour % 12 + lt.tm_min / 60.0) / 12.0, 2.2, INK), (lt.tm_min / 60.0, 3.4, (170, 60, 50))):
            a = frac * 2 * math.pi
            pygame.draw.line(surf, col, P(CLOCK[0], 0, CLOCK[1]),
                             P(CLOCK[0] + math.sin(a) * r, 0, CLOCK[1] + math.cos(a) * r * 1.15))
        # front door on the right wall
        if self.door_open:
            poly(surf, mix((16, 30, 76), (200, 130, 110), day), [P(130, 0, 0), P(146, 0, 0), P(146, 0, 31),
                                                              P(130, 0, 31)], INK)
            poly(surf, shade(WOOD, 0.8), [P(130, 0, 0), P(130, 14, 0), P(130, 14, 31), P(130, 0, 31)], INK)
        else:
            piece, at = self.sprite("door", self.paint_door)
            surf.blit(piece, at)
            if not m.away:                                        # his coat hangs on the back of the door
                piece, at = self.sprite("coat", self.paint_coat)
                hang(surf, piece, at, sway)
        # bathroom door in the loft's side
        bd = BATH_DOOR
        if self.bath_on:
            poly(surf, (255, 214, 130), bd, INK)
            for y in (27, 31, 35):
                pygame.draw.line(surf, (236, 170, 100), P(56, y, 1), P(56, y, 24))
        else:
            piece, at = self.sprite("bathdoor", self.paint_bath_door)
            surf.blit(piece, at)
        # blanket
        lying = m.visible and m.pose == "lie"
        if lying:
            m.draw(surf)
        piece, at = self.sprite(("blanket", lying), lambda s: self.paint_blanket(s, lying))
        surf.blit(piece, at)
        hung = self.items["picture"]
        if hung.state == "ok" and hung.hung:
            self.draw_item(surf, hung)

        # the lamp, and so which way shadows fall
        lamp_on = self.lamp_ok and not self.lights_off and not flick
        Lx, Ly, Lz = self.lamp_pos()

        def shadow(s, x, y, g, h, w):
            p0 = P(x, y, g)
            if lamp_on:
                k = h / max(8.0, Lz - g - h * 0.5)
                tip = P(x + (x - Lx) * k, y + (y - Ly) * k, g)
                cast_shadow(s, p0, tip[0] - p0[0], tip[1] - p0[1], w)
            else:
                cast_shadow(s, p0, 0, 0, w)

        # depth-sorted pass
        slots = [[] for _ in range(len(ORDER) + 1)]

        def put(x, y, z, fn):
            for i, n in enumerate(ORDER):
                b = BOXES[n]
                if z < b[5] - 0.5 and (x < b[0] or y < b[1]) and x <= b[3] + 30 and y <= b[4] + 30:
                    slots[i].append((x + y, z, id(fn), fn))
                    return
            slots[-1].append((x + y, z, id(fn), fn))

        def ground(x, y, z):
            """Height of the floor under a thing, or None if it is not standing on one."""
            g = 30 if (x < 56 and y < 60) else 0
            return g if abs(z - g) < 1.5 else None

        def item_fn(it):
            def fn(s):
                g = ground(it.x, it.y, it.z)
                if g is not None:
                    shadow(s, it.x, it.y, g, it.h, 7)
                self.draw_item(s, it)
            return fn

        for it in self.items.values():
            if it.state == "broken":
                put(it.debris[0], it.debris[1], it.debris[2], lambda s, it=it: self.draw_debris(s, it))
            elif not it.held and not it.hung:
                put(it.x, it.y, it.z, item_fn(it))
        b = self.ball
        bs = item_sprite("ball")

        def ball_fn(s):
            g = ground(b.x, b.y, b.z)
            if g is not None:
                shadow(s, b.x, b.y, g, 6, 7)
            s.blit(bs, (P(b.x, b.y, b.z)[0] - 4, P(b.x, b.y, b.z)[1] - 7))

        def man_fn(s):
            g = ground(m.x, m.y, m.z)
            if g is not None and m.pose not in ("sit", "climb", "lie"):
                shadow(s, m.x, m.y, g, 28, 11)
            m.draw(s)

        put(b.x, b.y, b.z, ball_fn)
        dg = self.dog
        if dg.visible:
            def dog_fn(s):
                if ground(dg.x, dg.y, dg.z) is not None:
                    shadow(s, dg.x, dg.y, 0, 6, 9)
                dg.draw(s)
            put(dg.x, dg.y, dg.z, dog_fn)
        if m.visible and not lying and not m.big:
            put(m.x, m.y, m.z, man_fn)
        for i, n in enumerate(ORDER):
            for _, _, _, fn in sorted(slots[i], key=lambda e: e[:3]):
                fn(surf)
            self.draw_box(surf, n)
        for _, _, _, fn in sorted(slots[-1], key=lambda e: e[:3]):
            fn(surf)

        # the arm the lamp hangs from, and the lamp
        piece, at = self.sprite("arm", self.paint_arm)
        surf.blit(piece, at)
        lx, ly = P(Lx, Ly, Lz)
        pygame.draw.line(surf, (40, 32, 48), P(LAMP_AT[0], LAMP_AT[1], RH - 2.5), (lx, ly))
        poly(surf, (236, 124, 60), [(lx - 2, ly - 1), (lx + 2, ly - 1), (lx + 5, ly + 4), (lx - 5, ly + 4)], (110, 50, 40))
        surf.fill((255, 176, 96), (lx - 3, ly + 1, 2, 2))
        surf.fill((186, 86, 46), (lx + 2, ly + 1, 2, 2))
        surf.fill((120, 56, 40), (lx - 1, ly - 2, 3, 1))
        if self.lamp_ok:
            surf.fill((255, 246, 200) if lamp_on else (150, 150, 170), (lx - 1, ly + 4, 3, 2))

        # ---- light: deep blue shadow, pools of warm light, shadows cut out of them ----
        # The light map and the glow layer are only rebuilt when something that
        # affects them changes, which on a level, quiet device is a few times a second.
        tv = self.items["tv"]
        tv_lit = self.tv_on and tv.state == "ok" and not tv.held
        wet = self.tank_level > 0.15
        fairy = not flick
        key = (self.sky_q, flick, lamp_on, self.lights_off, lx, ly, wet, self.fridge_open, self.bath_on,
               (int(t * 5) % 4, int(tv.x), int(tv.y), int(tv.z)) if tv_lit else None,
               int(t * 2) % 9 if fairy else -1)
        hero = None
        if m.visible and m.pose in ("rag", "dizzy"):
            hx, hy = P(m.x, m.y, m.z)
            hero = (hx // 2 * 2, hy // 2 * 2 - 10)
        key += (hero,)
        if key != self.lit_key:
            self.lit_key = key
            self.relight(self.sky_q[0] / 12.0, flick, lamp_on, lx, ly, tv_lit, wet, fairy, hero)
        surf.blit(self.lm, (0, 0), special_flags=pygame.BLEND_RGB_MULT)

        # things that glow by themselves, and things in front of the light
        surf.blit(self.emis, (0, 0))
        on = fairy and not self.lights_off
        for px, py in SCONCES:
            surf.fill((255, 214, 130) if on else (120, 112, 124), (px - 1, py, 3, 3))
            if on:
                surf.fill((255, 246, 204), (px, py + 1, 1, 1))
        if on:
            pygame.draw.line(surf, (255, 236, 190), STRIP[0], STRIP[1])
        ox, oy = OWL
        if int(t * 10) % 53 > 1:                                 # the owl blinks
            surf.fill((255, 224, 96), (ox - 1, oy - 5, 1, 1))
            surf.fill((255, 224, 96), (ox + 1, oy - 5, 1, 1))
        if not self.lite:                                        # chimney smoke rises against gravity
            cx, cy = P(109, -6, 76)
            sn, cs = math.sin(self.lant), math.cos(self.lant)
            for i in range(6):
                u = (t * 0.2 + i / 6.0) % 1.0
                x = cx + int(math.sin(u * 6 + i * 2.1) * 2 - sn * u * 16)
                y = cy - int(cs * u * 16)
                surf.fill(mix((176, 176, 200), (70, 76, 120), u), (x, y, 2 if u > 0.25 else 1, 2 if u > 0.6 else 1))
        for p in self.parts:
            px, py = P(p[0], p[1], p[2])
            if p[8] == 2:
                text(surf, "Z", px, py, p[7])
            else:
                surf.fill(p[7], (px, py, 1 if p[8] == 0 else 2, 2 if p[8] == 0 else 1))
        if fairy:
            wink = int(t * 2)
            for i, (px, py) in enumerate(FAIRY_S):
                surf.fill((170, 110, 60) if (i * 7 + wink) % 9 == 0 else (255, 222, 150), (px, py + 1, 1, 1))
        surf.blit(self.fx, (0, 0), special_flags=pygame.BLEND_RGB_ADD)
        if lamp_on and not self.lite:
            for i in range(12):                               # dust drifting through the lamp light
                a = t * (0.16 + i * 0.011) + i * 2.4
                mx = Lx + math.cos(a) * (6 + i * 1.8)
                my = Ly + math.sin(a * 1.3) * (6 + i * 1.8)
                mz = 4 + (t * (1.2 + i % 4 * 0.5) + i * 9) % 36
                surf.fill((110, 76, 32) if i % 3 else (180, 130, 64), (*P(mx, my, mz), 1, 1), pygame.BLEND_RGB_ADD)
        if m.visible and m.big:                                  # up against the glass, in front of everything
            m.draw(surf)

        for name, it in self.items.items():
            if it.state == "broken":
                self.ghost(surf, name)
        if not self.lamp_ok:
            self.ghost(surf, "lamp")

    def ui(self, surf):
        m = self.man
        for r in self.rings:
            pygame.draw.circle(surf, (255, 255, 255), (int(r[0]), int(r[1])), int(2 + r[2] * 16), 1)

        for p in (self.party.people if self.party else [m]):
            if p.speech_t > 0 and (p.visible or p.hidden):
                hx, hy = p.head() if p.visible else (P(56, 30, 30)[0] + 8, P(56, 30, 30)[1] + 4)
                draw_bubble(surf, p.speech, hx, hy)
        if not self.party and self.dog.visible and self.dog.speech_t > 0:
            draw_bubble(surf, self.dog.speech, *P(self.dog.x, self.dog.y, self.dog.z + 12))

        # needs, top-left corner
        pygame.draw.rect(surf, (20, 20, 44), (2, 2, 36, 35 if m.away else 33), border_radius=2)
        text(surf, "$%d" % m.coins, 4, 28, (250, 214, 110))
        for i, (k_, lab, c) in enumerate((("food", "F", (240, 150, 70)), ("rest", "Z", (130, 150, 240)),
                                          ("fun", "J", (240, 210, 90)), ("wash", "W", (110, 200, 220)))):
            y = 4 + i * 6
            text(surf, lab, 4, y, (150, 150, 180))
            surf.fill((22, 26, 60), (9, y + 1, 26, 3))
            v = m.need[k_]
            surf.fill(c if v > 30 else (240, 80, 70), (9, y + 1, int(26 * v / 100), 3))
        if m.away:
            text(surf, "OUT", 22, 28, (150, 150, 180))


# --------------------------------------------------------------------------
# his friend's place: a dark modern house on three stepped tiers, full of
# people.  Ground floor: kitchen, bar, and a lounge where they watch the match.
# First floor: a den for video games.  Top: a bedroom under a flat roof.
# The scene has its own camera and room size (swapped in while he is there),
# so the same physics throws everybody about, and off the upper floors.
# --------------------------------------------------------------------------
HOME_VIEW = (OX, OY, RX, RY, RH, KX, KY, KZ)
PARTY_VIEW = (145, 93, 110, 80, 108, 1.0, 1 / 3.0, 5 / 6.0)
F1, F2 = 36.0, 72.0                           # heights of the upper floors
T2X, T2Y, T3X, T3Y = 70, 50, 45, 35           # how far the first and top floors reach
LANE, LANE1 = 28.5, 19.0                      # the clear strips people walk along, downstairs and up
P_T0 = [("kitchen", (6, 0, 0, 50, 9, 14)), ("island", (10, 20, 0, 50, 27, 15)), ("fridge", (52, 0, 0, 62, 10, 30)),
        ("stairs", (0, 52, 0, 10, 78, 36)), ("table", (80, 13, 0, 96, 20, 5)), ("p_arm_l", (70, 30, 0, 73, 44, 11)),
        ("p_seat", (73, 30, 0, 101, 40, 7)), ("p_arm_r", (101, 30, 0, 104, 44, 11)), ("p_back", (73, 40, 0, 101, 44, 15))]
P_T1 = [("table2", (20, 9, F1, 34, 15, F1 + 5)), ("arm2_l", (9, 22, F1, 12, 35, F1 + 11)),
        ("seat2", (12, 22, F1, 42, 31, F1 + 7)), ("arm2_r", (42, 22, F1, 45, 35, F1 + 11)),
        ("back2", (12, 31, F1, 42, 35, F1 + 15))]
P_T2 = [("bed", (4, 6, F2, 26, 26, F2 + 6))]
P_BOXES = dict(P_T0 + P_T1 + P_T2)
P_SOLIDS = ([b for n, b in P_T0 + P_T1 + P_T2 if n != "stairs"]
            + [(0, 52, 0, 10, 78, 18), (0, 0, F1 - 3, T2X, T2Y, F1), (0, 0, F2 - 3, T3X, T3Y, F2),
               (-2, -2, 104, T3X + 2, T3Y + 2, 107)])
HOME_SOLIDS = SOLIDS
P_SEATS = [(77.5, 35), (84, 35), (90.5, 35), (97, 35)]
P_SEATS1 = [(17, 26.5), (24, 26.5), (31, 26.5), (38, 26.5)]
P_PENDANTS = [(20, 23.5, F1 - 3, 22), (40, 23.5, F1 - 3, 22), (27, 19, F2 - 3, 60)]      # x, y, hung from, bulb height
CHARCOAL = (116, 116, 148)
GLOW = (255, 172, 84)
FRIENDS = [dict(skin=(150, 100, 70), hair=(30, 24, 24), shirt=(90, 170, 220), pants=(60, 60, 84)),
           dict(skin=(250, 214, 180), hair=(224, 176, 80), shirt=(120, 200, 130), pants=(92, 72, 112)),
           dict(skin=(204, 152, 112), hair=(160, 66, 44), shirt=(240, 200, 80), pants=(52, 84, 94)),
           dict(skin=(240, 194, 152), hair=(64, 64, 76), shirt=(204, 124, 224), pants=(74, 74, 78))]
CHEER = ["GOAL!!", "YESSS!", "WOOO!", "LET'S GO!", "WHAT A SHOT!"]
GROAN = ["NOOO", "REF!!", "COME ON!", "UGH", "HOW?!"]
BARTALK = ["CHEERS!", "SO THEN I SAID...", "HAHA", "ANOTHER ONE?", "TO THE FISH!", "NICE PLACE", "ONE MORE"]
GAMEWIN = ["I WIN!", "GG", "TOO EASY", "UNBEATABLE"]
GAMELOSE = ["REMATCH", "NO FAIR", "LAG!", "MY CONTROLLER IS BROKEN"]


def set_view(v):
    """Swap the camera and the size of the room the physics uses."""
    global OX, OY, RX, RY, RH, KX, KY, KZ
    OX, OY, RX, RY, RH, KX, KY, KZ = v


def floor_at(x, y, z):
    """Which floor someone at this height is on: its number and its level."""
    if z > F2 - 8 and x < T3X and y < T3Y:
        return 2, F2
    if z > F1 - 8 and x < T2X and y < T2Y:
        return 1, F1
    return 0, 0.0


class Party:
    def __init__(self, world):
        global SOLIDS
        SOLIDS = P_SOLIDS
        set_view(PARTY_VIEW)
        w = self.w = world
        self.t = 0.0
        m = w.man
        m.x, m.y, m.z = 104.0, 3.0, 0.0
        m.pose, m.view, m.visible, m.away, m.hidden = "stand", "side", True, False, False
        self.people = [m]
        for i, look in enumerate(FRIENDS):
            g = Man(w)
            g.look = look
            g.x, g.y = (18.0 + i * 8, LANE + 2) if i else (30.0, 14.0)
            g.view, g.face = ("side", 1 if i % 2 else -1) if i else ("front", 1)
            g.anim = i * 1.3
            self.people.append(g)
        for p in self.people:
            p.path, p.goal, p.cheer, p.body, p.rag, p.still = [], None, 0.0, None, None, 0.0
        self.phases = random.sample(["tv", "bar", "games"], 3)
        self.phase, self.phase_t, self.event_t = "arrive", 4.0, 2.0
        self.tv_flash = 0.0
        self.lamp_d = [0.0, 0.0]
        self.lamp_v = [0.0, 0.0]
        self.done = False
        self.loose = [Body(x, 23.5, 15, r=1.5, h=4, mu=0.3) for x in (13, 24, 30, 37, 47)]     # drinks on the bar
        self.tv0 = (72, 96, 12, 32)                    # x0, x1, z0, z1 on the right wall
        self.tv1 = (14, 40, F1 + 10, F1 + 28)
        self.room = self.build()
        self.mask = pygame.mask.from_surface(self.room)
        self.lm = pygame.Surface((W, H))
        self.fx = pygame.Surface((W, H))
        self.lit_key = None
        self.send(m, 104, LANE)
        m.say("HEY EVERYONE!")

    def close(self):
        global SOLIDS
        SOLIDS = HOME_SOLIDS
        set_view(HOME_VIEW)

    # ---- who goes where ----
    def send(self, p, x, y, z=0.0, pose="stand", view="side", face=1):
        """Plan a walk for p: out to the lane, along it, up or down the stairs, in to the spot."""
        p.goal = (float(x), float(y), z, pose, view, face)
        fc, cz = floor_at(p.x, p.y, p.z)
        fg, gz = floor_at(x, y, z)
        if p.pose == "sit":
            p.y, p.z, p.pose, p.view = (LANE1 if fc else LANE), cz, "stand", "side"
        px, py = p.x, p.y
        path = []
        if fc == 2:                                    # down from the bedroom first
            path += [(40, 20, F2), (47, 4, F2), (68, 4, F1)]
            fc, px, py = 1, 68, 4

        def out0(px, py):
            if py < 20 and px < 52:                    # from behind the bar
                return [(56, py, 0), (56, LANE, 0)]
            if px > 97 and py < 25:                    # from the front door
                return [(104, LANE, 0)]
            return [(clamp(px, 14, 104), LANE, 0)] if abs(py - LANE) > 1 else []

        def out1(px, py):                              # to the corridor at the top of the stairs
            if py >= 36:
                return [(5, py, F1)]
            return ([(px, LANE1, F1)] if abs(py - LANE1) > 1 else []) + [(5, LANE1, F1)]

        if fc == 1 and fg == 0:
            path += out1(px, py) + [(5, 51, F1), (5, 79, 0), (14, 79, 0), (14, LANE, 0)]
            px, py, fc = 14, LANE, 0
        elif fc == 0 and fg == 1:
            path += out0(px, py) + [(14, LANE, 0), (14, 79, 0), (5, 79, 0), (5, 51, F1)]
            px, py, fc = 5, 51, 1
        if fg == 0:
            path += out0(px, py)
            if pose == "sit":
                path.append((x, LANE, 0))
            elif y < 20 and x < 52:
                path += [(56, LANE, 0), (56, y, 0), (x, y, 0)]
            elif x > 97 and y < 25:
                path += [(104, LANE, 0), (x, y, 0)]
            else:
                path += [(x, LANE, 0), (x, y, 0)]
        else:
            if abs(px - 5) > 1:
                path += out1(px, py)
            if y >= 36:
                path += [(5, y, F1), (x, y, F1)]
            else:
                path += [(5, LANE1, F1), (x, LANE1, F1)]
        p.path = path

    def assign(self, phase):
        m, others = self.people[0], self.people[1:]
        random.shuffle(others)
        if phase == "tv":
            for p, (x, y) in zip([others[0], m, others[1], others[2]], P_SEATS):
                self.send(p, x, y, 7.0, "sit", "back")
            self.send(others[3], 107, 37, 0.0, "stand", "back")
        elif phase == "games":
            for p, (x, y) in zip([others[0], m, others[1], others[2]], P_SEATS1):
                self.send(p, x, y, F1 + 7, "sit", "back")
            self.send(others[3], 27, 41, F1, "stand", "back")
        elif phase == "bar":
            self.send(others[0], 30, 14, 0.0, "stand", "front")
            self.send(m, 26, LANE + 2, 0.0, "stand", "side", -1)
            self.send(others[1], 18, LANE + 2, 0.0, "stand", "side", 1)
            self.send(others[2], 36, LANE + 2, 0.0, "stand", "back")
            self.send(others[3], 44, LANE + 2, 0.0, "stand", "side", -1)
        elif phase == "leave":
            self.send(m, 104, 3)
            m.say(random.choice(["THANKS, SAM!", "SEE YOU ALL", "GOOD NIGHT!"]))

    # ---- one tick ----
    def update(self, dt):
        w = self.w
        self.t += dt
        gx, gy, gz = w.g
        gh = math.hypot(gx, gy)
        wild = gz > -0.6 or gh > 0.62 or w.shake > 1.5
        acc = [(w.g[i] + w.jerk[i]) * GRAV for i in range(3)]
        for i, p in enumerate(self.people):
            if i:                                       # the man's own clock runs in Man.update
                p.dt = dt
                p.anim += dt
                p.speech_t -= dt
                p.hop = max(0.0, p.hop - dt)
            p.cheer = max(0.0, p.cheer - dt)
            if wild and not p.body:
                p.body = Body(p.x, p.y, p.z + 0.5, r=3.5, h=16, mu=0.35, rest=0.3)
                p.body.vz = random.uniform(20, 45)
                p.rag_start()
                p.pose, p.view, p.still, p.carry = "rag", "side", 0.0, None
                if random.random() < 0.4:
                    p.say(random.choice(TOSS + ["MY DRINK!", "NOT THE TV!"]), 2.0)
            if p.body:
                b = p.body
                step_body(b, acc, dt)
                p.x, p.y, p.z = b.x, b.y, b.z
                if p.pose == "rag":
                    p.rag_step(dt)
                    p.still = p.still + dt if (b.speed() < 10 and gz < -0.8 and gh < 0.4 and b.touch) else 0.0
                    if p.still > 0.7:
                        p.pose, p.view, p.still = "dizzy", "front", 0.0
                        if random.random() < 0.5:
                            p.say(random.choice(DAZED))
                else:
                    p.still += dt
                    if p.still > 1.6:                   # back on their feet, and back to what they were doing
                        p.body, p.rag = None, None
                        f, fz = floor_at(p.x, p.y, p.z)
                        p.pose, p.view, p.z = "stand", "side", fz
                        for (x0, y0, z0, x1, y1, z1) in P_SOLIDS:
                            if z0 == fz and x0 - 2 < p.x < x1 + 2 and y0 - 2 < p.y < y1 + 2:
                                p.x, p.y = ((clamp(p.x, 14, 104), LANE), (clamp(p.x, 5, 66), LANE1), (40.0, 20.0))[f]
                                break
                        if p.goal:
                            self.send(p, *p.goal)
                continue
            p.wobble = lerp(p.wobble, clamp((gx - gy) * 0.7, -1, 1), min(1, dt * 6))
            if p.path:                                  # walking, stairs included
                x, y, z = p.path[0]
                dx, dy = x - p.x, y - p.y
                d = math.hypot(dx, dy)
                step = (22 if abs(z - p.z) > 1 else 30) * dt
                if d <= step:
                    p.x, p.y, p.z = float(x), float(y), float(z)
                    p.path.pop(0)
                    if not p.path:
                        p.x, p.y, p.z, p.pose, p.view, p.face = p.goal
                else:
                    p.x += dx / d * step
                    p.y += dy / d * step
                    p.z += (z - p.z) * step / d
                    if abs(dx - dy) > 0.25 * d:
                        p.face = 1 if dx - dy > 0 else -1
                    p.pose, p.view = "walk", "side"
        for b in self.loose:
            step_body(b, acc, dt)
        # the evening moves on
        m = self.people[0]
        self.phase_t -= dt
        if self.phase_t <= 0 and not any(p.body for p in self.people):
            if self.phase == "leave":
                if not m.path:
                    self.done = True
            else:
                self.phase = self.phases.pop(0) if self.phases else "leave"
                self.phase_t = random.uniform(18, 24) if self.phase != "leave" else 1.0
                self.event_t = 7.0
                self.assign(self.phase)
        self.event_t -= dt
        self.tv_flash = max(0.0, self.tv_flash - dt)
        settled = [p for p in self.people if not p.path and not p.body]
        if self.event_t <= 0 and len(settled) >= 3:
            self.event_t = random.uniform(3.0, 6.0)
            if self.phase == "tv":
                good = random.random() < 0.6
                for p in random.sample(settled, min(2, len(settled))):
                    p.say(random.choice(CHEER if good else GROAN), 1.8)
                if good:
                    self.tv_flash = 0.6
                    play("cheer", 1.0)
                    for p in settled:
                        p.cheer = random.uniform(1.0, 1.6)
                        if p.pose != "sit":
                            p.hop = 0.35
            elif self.phase == "bar":
                random.choice(settled).say(random.choice(BARTALK), 1.8)
                for p in settled:
                    if p.view == "side":
                        p.drink = self.t + random.uniform(0.2, 1.8)
            elif self.phase == "games":
                seated = [p for p in settled if p.pose == "sit"]
                if len(seated) >= 2:
                    a, b = random.sample(seated, 2)
                    a.say(random.choice(GAMEWIN), 1.8)
                    a.cheer = 1.2
                    if random.random() < 0.6:
                        b.say(random.choice(GAMELOSE), 2.2)
        for p in self.people:                           # a sip now and then at the bar
            if p.body or p.path:
                continue
            if self.phase == "bar" and p.view == "side":
                p.carry = "cup"
                p.pose = "eat" if 0 < self.t - p.drink < 1.4 else "stand"
            else:
                if p.carry == "cup":
                    p.carry = None
                if p.pose == "eat":
                    p.pose = "stand"
        # lamps, lights
        down = max(0.3, -gz)
        for i in (0, 1):
            tgt = clamp(9 * w.g[i] / down, -9, 9) if gz < 0 else math.copysign(9, w.g[i] or 1)
            self.lamp_v[i] += (25 * (tgt - self.lamp_d[i]) - 0.9 * self.lamp_v[i] + w.jerk[i] * 160) * dt
            self.lamp_d[i] = clamp(self.lamp_d[i] + self.lamp_v[i] * dt, -10, 10)
        tilt = math.hypot(gx, gy)
        w.flick = max(w.flick - dt * 3, min(1.0, w.shake * 0.7 + w.rate * 0.45 + (0.22 if tilt > 0.3 else 0)
                                           + (0.3 if gz > 0 else 0)))
        w.dark_now = random.randint(70, 150) if random.random() < w.flick * 0.55 else 0

    # ---- painting ----
    def build(self):
        s = pygame.Surface((W, H))
        s.fill(KEY)
        s.set_colorkey(KEY, pygame.RLEACCEL)
        em = self.emis = pygame.Surface((W, H))
        em.set_colorkey((0, 0, 0), pygame.RLEACCEL)
        ov = self.rails = pygame.Surface((W, H))        # glass rails, drawn over the people behind them
        ov.fill(KEY)
        ov.set_colorkey(KEY, pygame.RLEACCEL)
        rnd = random.Random(5)
        ink = (24, 22, 36)
        wl, wr = CHARCOAL, shade(CHARCOAL, 0.82)
        slab = (88, 90, 116)
        self.glows = []                                 # (kind, points, light position) for everything lit

        def window(axis, a0, a1, z0, z1, pool):
            wall_rect(s, GLOW, axis, a0, a1, z0, z1, ink)
            wall_rect(s, (255, 214, 140), axis, a0 + 1, a1 - 1, z0 + 1, z0 + (z1 - z0) * 0.3)
            mid = (a0 + a1) / 2
            pygame.draw.line(s, ink, P(0, mid, z0) if axis == "x" else P(mid, 0, z0),
                             P(0, mid, z1) if axis == "x" else P(mid, 0, z1))
            pts = ([P(0, a0, z0), P(0, a1, z0), P(0, a1, z1), P(0, a0, z1)] if axis == "x"
                   else [P(a0, 0, z0), P(a1, 0, z0), P(a1, 0, z1), P(a0, 0, z1)])
            self.glows.append((pts, pool, P(0, mid, (z0 + z1) / 2) if axis == "x" else P(mid, 0, (z0 + z1) / 2)))

        def walls(xmax, ymax, z0, z1):
            wall_rect(s, wl, "x", 0, ymax, z0, z1)
            wall_rect(s, wr, "y", 0, xmax, z0, z1)
            for y in range(0, ymax, 18):
                pygame.draw.line(s, shade(wl, 0.8), P(0, y, z0), P(0, y, z1))
                pygame.draw.line(s, shade(wl, 1.12), P(0, y + 1, z0), P(0, y + 1, z1))
            for x in range(0, xmax, 22):
                pygame.draw.line(s, shade(wr, 0.8), P(x, 0, z0), P(x, 0, z1))
                pygame.draw.line(s, shade(wr, 1.12), P(x + 1, 0, z0), P(x + 1, 0, z1))
            wall_rect(s, shade(wl, 0.6), "y", -4, 0, z0, z1, ink, ymax)            # cut ends
            wall_rect(s, shade(wr, 0.6), "x", -4, 0, z0, z1, ink, xmax)

        def floor(xmax, ymax, z, col, n):
            poly(s, col, [P(0, 0, z), P(xmax, 0, z), P(xmax, ymax, z), P(0, ymax, z)])
            for i in range(0, xmax, n):
                for j in range(0, ymax, n):
                    c = shade(col, rnd.uniform(0.9, 1.08))
                    i1, j1 = min(xmax, i + n), min(ymax, j + n)
                    poly(s, c, [P(i, j, z), P(i1, j, z), P(i1, j1, z), P(i, j1, z)])
                    pygame.draw.line(s, shade(c, 1.14), P(i + 1, j + 1, z), P(i1 - 1, j + 1, z))
            for i in range(0, xmax, n):
                pygame.draw.line(s, shade(col, 0.7), P(i, 0, z), P(i, ymax, z))
            for j in range(0, ymax, n):
                pygame.draw.line(s, shade(col, 0.7), P(0, j, z), P(xmax, j, z))

        def rail(ax, ay, bx, by, z):
            for t in range(0, 11):
                x, y = lerp(ax, bx, t / 10.0), lerp(ay, by, t / 10.0)
                pygame.draw.line(ov, (150, 160, 190), P(x, y, z), P(x, y, z + 7))
            pygame.draw.line(ov, (196, 206, 230), P(ax, ay, z + 7), P(bx, by, z + 7))

        # a concrete pad for the house to stand on
        g = 6
        poly(s, slab, [P(-g, -g, -4), P(RX + g, -g, -4), P(RX + g, RY + g, -4), P(-g, RY + g, -4)])
        poly(s, shade(slab, 0.6), [P(-g, RY + g, -4), P(RX + g, RY + g, -4), P(RX + g, RY + g, -8), P(-g, RY + g, -8)], ink)
        poly(s, shade(slab, 0.42), [P(RX + g, RY + g, -4), P(RX + g, -g, -4), P(RX + g, -g, -8), P(RX + g, RY + g, -8)], ink)
        # ---- ground floor ----
        walls(RX, RY, 0, F1)
        floor(RX, RY, 0, (156, 156, 182), 14)
        poly(s, (176, 92, 52), [P(74, 8), P(100, 8), P(100, 27), P(74, 27)], (110, 54, 36))        # lounge rug
        poly(s, (214, 130, 70), [P(77, 10.5), P(97, 10.5), P(97, 24.5), P(77, 24.5)])
        window("x", 12, 28, 8, 28, (16, 20, 3))
        window("x", 32, 46, 8, 28, (16, 39, 3))
        wall_rect(s, (255, 164, 76), "y", 8, 48, 15, 25, ink)                                      # lit splashback
        for x in range(13, 48, 5):
            pygame.draw.line(s, (226, 130, 56), P(x, 0, 15), P(x, 0, 25))
        wall_rect(s, (50, 50, 72), "y", 8, 48, 26, 33, ink)                                        # cupboards over it
        pygame.draw.line(s, (255, 236, 190), P(9, 0.5, 25.6), P(47, 0.5, 25.6))
        kb = [P(8, 0, 15), P(48, 0, 15), P(48, 0, 25), P(8, 0, 25)]
        self.glows.append((kb, (28, 13, 6), P(28, 0, 20)))
        wall_rect(s, (22, 22, 34), "y", 70, 98, 10, 34, (12, 12, 20))                              # tv frame
        wall_rect(s, (50, 50, 70), "y", 100, 108, 0, 31, ink)                                      # front door
        wall_rect(s, GLOW, "y", 105, 106.5, 6, 26)
        sh = pygame.Surface((W, H), pygame.SRCALPHA)
        for n, b in P_T0 + P_T1:
            for pts in ([P(b[0], b[4], b[2]), P(b[3] + 3, b[4], b[2]), P(b[3] + 3, b[4] + 3, b[2]), P(b[0], b[4] + 3, b[2])],
                        [P(b[3], b[1], b[2]), P(b[3] + 3, b[1], b[2]), P(b[3] + 3, b[4] + 3, b[2]), P(b[3], b[4] + 3, b[2])]):
                pygame.draw.polygon(sh, (10, 8, 30, 80), pts)
        sh0 = sh.copy()
        s.blit(sh0, (0, 0))
        for ax, ay, bx, by, f in ((0, RY, RX, RY, 1.0), (RX, RY, RX, 0, 0.72)):                    # footing
            poly(s, shade((80, 80, 106), f), [P(ax, ay, 0), P(bx, by, 0), P(bx, by, -4), P(ax, ay, -4)], ink)
        # ---- first floor: slab, walls, the den ----
        box(s, 0, 0, F1 - 3, T2X, T2Y, F1, (104, 104, 134), top=(120, 96, 84), bands=1)
        walls(T2X, T2Y, F1, F2)
        floor(T2X, T2Y, F1, (150, 112, 92), 10)
        poly(s, (70, 110, 150), [P(14, 6, F1), P(40, 6, F1), P(40, 21, F1), P(14, 21, F1)], (40, 70, 110))   # den rug
        window("x", 8, 20, F1 + 8, F1 + 28, (12, 14, F1 + 1))
        window("x", 28, 42, F1 + 8, F1 + 28, (12, 35, F1 + 1))
        wall_rect(s, (22, 22, 34), "y", 12, 42, F1 + 8, F1 + 30, (12, 12, 20))
        wall_rect(s, (250, 110, 180), "y", 50, 62, F1 + 20, F1 + 23)                               # a neon strip
        np_ = [P(50, 0, F1 + 20), P(62, 0, F1 + 20), P(62, 0, F1 + 23), P(50, 0, F1 + 23)]
        self.glows.append((np_, (56, 6, F1 + 1), P(56, 0, F1 + 21)))
        for i in reversed(range(9)):                                                               # stairs up to the bedroom
            box(s, 68 - (i + 1) * 2.4, 0, F1, 68 - i * 2.4, 8, F1 + (i + 1) * 4, (92, 92, 122), top=(176, 124, 84), bands=1)
        pygame.draw.line(s, (196, 206, 230), P(68, 8, F1 + 9), P(46.4, 8, F2 + 8))
        s.blit(sh, (0, 0))
        # ---- top floor: slab, walls, bedroom, roof ----
        box(s, 0, 0, F2 - 3, T3X, T3Y, F2, (104, 104, 134), top=(120, 96, 84), bands=1)
        walls(T3X, T3Y, F2, 104)
        floor(T3X, T3Y, F2, (136, 136, 164), 9)
        window("x", 8, 26, F2 + 8, F2 + 26, (10, 17, F2 + 1))
        window("y", 28, 40, F2 + 8, F2 + 26, (34, 8, F2 + 1))
        box(s, 4, 6, F2, 26, 26, F2 + 4, (70, 70, 96), bands=1)
        box(s, 5, 7, F2 + 4, 25, 25, F2 + 6, (214, 120, 70), bands=1)
        box(s, 8, 8, F2 + 6, 22, 13, F2 + 7.6, (240, 236, 226), bands=1)
        box(s, 30, 22, F2, 36, 28, F2 + 6, (60, 60, 84), bands=1)
        px, py = P(33, 25, F2 + 6)
        for dx, dy, r in ((0, -3, 4), (-2, -5, 3), (2, -6, 2)):
            pygame.draw.circle(s, (52, 120, 84), (px + dx, py + dy), r)
        for x, y in ((T3X - 1, T3Y - 1), (T3X - 1, 1), (1, T3Y - 1)):
            pygame.draw.line(s, (60, 60, 84), P(x, y, F2), P(x, y, 104), 2)
        box(s, -2, -2, 104, T3X + 2, T3Y + 2, 107, (86, 86, 116), bands=1)
        px, py = P(30, 12, 107)
        s.fill((255, 214, 130), (px, py - 1, 2, 2))
        em.fill((255, 224, 150), (px, py - 1, 2, 2))
        # rails
        rail(10, T2Y, T2X, T2Y, F1)
        rail(T2X, T2Y, T2X, 9, F1)
        rail(0, T3Y, T3X, T3Y, F2)
        rail(T3X, T3Y, T3X, 9, F2)
        return s

    def furniture(self, s, name):
        b = P_BOXES[name]
        dark = (96, 96, 126)
        if name == "kitchen":
            box(s, *b, dark, top=(150, 150, 172))
            for x in range(8, 48, 10):
                wall_rect(s, shade(dark, 0.84), "y", x, x + 8.5, 1.5, 12, shade(dark, 0.5), 9)
            px, py = P(36, 4.5, 14)
            pygame.draw.ellipse(s, (60, 62, 80), (px - 4, py - 1, 9, 3))
            s.fill((200, 206, 216), (px + 4, py - 5, 1, 4))
        elif name == "fridge":
            box(s, *b, (124, 128, 156))
            pygame.draw.line(s, (40, 40, 60), P(52, 10, 19), P(62, 10, 19))
            wall_rect(s, (150, 156, 184), "y", 53, 56, 2, 29, None, 10)
        elif name == "island":
            box(s, *b, dark, top=(176, 120, 78))
            wall_rect(s, shade(dark, 1.2), "y", 11, 49, 12, 14, None, 27)
            for x, c in ((14, (120, 200, 140)), (17, (230, 180, 80)), (21, (200, 90, 80)), (40, (120, 170, 230)), (44, (230, 180, 80))):
                px, py = P(x, 22, 15)
                s.fill(c, (px, py - 5, 2, 5))
                s.fill(shade(c, 1.3), (px, py - 5, 1, 2))
                s.fill((230, 230, 240), (px, py - 6, 1, 1))
            for x in (28, 33):
                px, py = P(x, 24.5, 15)
                s.fill((220, 230, 240), (px, py - 3, 2, 3))
        elif name == "stairs":
            n = 9                                       # drawn from the top step down, so nearer steps overlap
            for i in reversed(range(n)):
                y1 = 78 - i * 26.0 / n
                box(s, 0, y1 - 26.0 / n, 0, 10, y1, (i + 1) * F1 / n, (92, 92, 122), top=(176, 124, 84), bands=1)
            pygame.draw.line(s, (196, 206, 230), P(10, 78, 9), P(10, 52, F1 + 8))      # handrail
            for i in (0, 4, 8):
                y = 78 - (i + 0.5) * 26.0 / n
                pygame.draw.line(s, (150, 160, 190), P(10, y, (i + 1) * F1 / n), P(10, y, (i + 1) * F1 / n + 7))
        elif name in ("table", "table2"):
            box(s, *b, (70, 70, 94), top=(130, 130, 154), bands=1)
            px, py = P((b[0] + b[3]) / 2 - 3, (b[1] + b[4]) / 2, b[5])
            pygame.draw.ellipse(s, (230, 190, 90), (px - 3, py - 2, 7, 3))
            s.fill((200, 80, 70), (px + 7, py - 3, 2, 3))
            s.fill((40, 40, 56), (px + 11, py, 4, 2))
        elif name in ("p_seat", "seat2"):
            col = (114, 120, 164) if name == "p_seat" else (150, 96, 110)
            box(s, *b, col, bands=1)
            for i in (1, 2):
                x = b[0] + (b[3] - b[0]) * i / 3.0
                pygame.draw.line(s, shade(col, 0.66), P(x, b[1], b[5]), P(x, b[4], b[5]))
        elif name in ("p_back", "back2"):
            col = (106, 112, 154) if name == "p_back" else (140, 88, 104)
            box(s, *b, col)
            for i in (1, 2):
                x = b[0] + (b[3] - b[0]) * i / 3.0
                pygame.draw.line(s, shade(col, 0.62), P(x, b[4], b[2] + 2), P(x, b[4], b[5] - 1))
            wall_rect(s, (226, 130, 60), "y", b[0] + 2, b[0] + 8, b[2] + 5, b[2] + 14, (150, 80, 40), b[4])
        elif name == "bed":
            pass                                        # painted with the house
        else:
            box(s, *b, (96, 102, 142) if name.startswith("p_") else (128, 82, 98))

    def screen(self, surf, tv, mode):
        """What is on a television: the match, a video game, or a music channel."""
        t = self.t
        x0, x1, z0, z1 = tv
        w_, h_ = x1 - x0, z1 - z0
        cx, cz = (x0 + x1) / 2, (z0 + z1) / 2

        def R(col, a, b, c, d, out=None):
            wall_rect(surf, col, "y", a, b, c, d, out)
        if mode == "match":
            R((60, 150, 80) if self.tv_flash <= 0 or int(t * 12) % 2 else (240, 240, 200), x0, x1, z0, z1)
            for i in range(0, int(w_), 6):
                R((74, 168, 92), x0 + i, x0 + i + 3, z0, z1)
            pygame.draw.line(surf, (230, 240, 230), P(cx, 0, z0), P(cx, 0, z1))
            R((60, 150, 80), x0, x0 + 3, cz - 4, cz + 4, (230, 240, 230))
            R((60, 150, 80), x1 - 3, x1, cz - 4, cz + 4, (230, 240, 230))
            surf.fill(WHITE, (*P(cx + math.sin(t * 1.3) * w_ * 0.38, 0, cz + math.sin(t * 2.9) * h_ * 0.3), 2, 2))
            for i in range(6):
                px, py = P(cx + math.sin(t * 0.7 + i * 1.9) * w_ * 0.4, 0, cz + math.cos(t * 0.9 + i * 2.3) * h_ * 0.34)
                surf.fill((230, 70, 70) if i % 2 else (70, 110, 230), (px, py - 1, 1, 2))
        elif mode == "game":
            R((30, 24, 60), x0, x1, z0, z1)
            for i, c in enumerate(((90, 220, 240), (250, 120, 200), (250, 220, 90), (120, 240, 140))):
                kx = x0 + 3 + i * (w_ - 8) / 4.0 + math.sin(t * 3 + i) * 2
                kz = z0 + 3 + abs(math.sin(t * 5 + i * 1.7)) * (h_ - 10)
                R(c, kx, kx + 3, kz, kz + 4)
            R((250, 220, 90), x0 + 1, x0 + 6 + math.sin(t) * 3, z1 - 3, z1 - 1.5)
            R((250, 120, 200), x1 - 6 - math.cos(t) * 3, x1 - 1, z1 - 3, z1 - 1.5)
        else:
            R((24, 30, 56), x0, x1, z0, z1)
            for i in range(7):
                h = 2 + abs(math.sin(t * 4 + i * 1.3)) * (h_ - 5)
                a = x0 + 1.5 + i * (w_ - 3) / 7.0
                R(mix((255, 150, 70), (120, 90, 230), i / 6.0), a, a + (w_ - 3) / 9.0, z0 + 1, z0 + 1 + h)

    def modes(self):
        return ("match" if self.phase == "tv" else "music", "game" if self.phase == "games" else "music")

    def relight(self, flick, lights, heads):
        amb = mix((84, 88, 134), (232, 228, 222), self.w.sky_q[1] / 12.0)
        if flick:
            amb = shade(amb, 0.8)
        lm, fx = self.lm, self.fx
        self.mask.to_surface(lm, setcolor=amb, unsetcolor=WHITE)
        fx.fill((0, 0, 0))
        if not flick:
            for pts, pool, mid in self.glows:
                pygame.draw.polygon(lm, WHITE, pts)
                add_light(lm, P(*pool), 52, 24, (190, 120, 56))
                add_glow(fx, mid, 16, (64, 38, 14))
            for (x, y, top, bz), (bx, by) in zip(P_PENDANTS, heads):
                add_light(lm, (bx, by + 14), 40, 22, (170, 120, 64))
                add_glow(fx, (bx, by + 2), 8, (110, 76, 36))
        cols = {"match": (64, 156, 88), "flash": (196, 196, 156), "game": (150, 100, 190), "game2": (84, 156, 196),
                "music": (130, 90, 130)}
        for tv, kind, at in ((self.tv0, lights[0], P(84, 22, 3)), (self.tv1, lights[1], P(27, 20, F1 + 3))):
            x0, x1, z0, z1 = tv
            pygame.draw.polygon(lm, WHITE, [P(x0, 0, z0), P(x1, 0, z0), P(x1, 0, z1), P(x0, 0, z1)])
            add_light(lm, at, 62, 34, cols[kind])
            add_glow(fx, P((x0 + x1) / 2, 2, (z0 + z1) / 2), 20, shade(cols[kind], 0.4))

    def draw(self, surf):
        w, t = self.w, self.t
        day = w.daylight()
        w.paint_sky(surf)
        surf.blit(self.room, (0, 0))
        m0, m1 = self.modes()
        self.screen(surf, self.tv0, m0)
        self.screen(surf, self.tv1, m1)
        flick = bool(w.dark_now)
        tiers = ([], [], [])
        for p in self.people:
            if p.visible:
                tiers[0 if p.z < F1 - 6 else (1 if p.z < F2 - 6 else 2)].append(p)
        for boxes, people, overlay in ((P_T0, tiers[0], False), (P_T1, tiers[1], True), (P_T2, tiers[2], True)):
            slots = [[] for _ in range(len(boxes) + 1)]
            for p in people:
                def fn(s, p=p):
                    f, fz = floor_at(p.x, p.y, p.z)
                    if abs(p.z - fz) < 1.5 and p.pose != "sit":
                        cast_shadow(s, P(p.x, p.y, fz), 3, -1, 11)
                    p.draw(s)
                for i, (n, b) in enumerate(boxes):
                    if p.z < b[5] - 0.5 and (p.x < b[0] or p.y < b[1]) and p.x <= b[3] + 30 and p.y <= b[4] + 30:
                        slots[i].append((p.x + p.y, p.z, id(p), fn))
                        break
                else:
                    slots[-1].append((p.x + p.y, p.z, id(p), fn))
            for i, (n, b) in enumerate(boxes):
                for e in sorted(slots[i], key=lambda e: e[:3]):
                    e[3](surf)
                if n != "bed":
                    piece, at = w.sprite("party2_" + n, lambda s, n=n: self.furniture(s, n))
                    surf.blit(piece, at)
            for e in sorted(slots[-1], key=lambda e: e[:3]):
                e[3](surf)
        for i, b in enumerate(self.loose):
            px, py = P(b.x, b.y, b.z)
            surf.fill(OUTLINE, (px - 2, py - 5, 4, 6))
            surf.fill(((230, 180, 80), (120, 200, 140), (200, 90, 80))[i % 3], (px - 1, py - 4, 2, 4))
        surf.blit(self.rails, (0, 0))
        # pendant lamps under each floor
        heads = []
        for x, y, top, bz in P_PENDANTS:
            bx, by = P(x + self.lamp_d[0], y + self.lamp_d[1], bz)
            pygame.draw.line(surf, (30, 28, 44), P(x, y, top), (bx, by))
            poly(surf, (48, 48, 68), [(bx - 2, by - 3), (bx + 2, by - 3), (bx + 4, by + 1), (bx - 4, by + 1)], (24, 22, 36))
            heads.append((bx, by))
        l0 = "flash" if (m0 == "match" and self.tv_flash > 0 and int(t * 12) % 2) else m0
        l1 = ("game2" if int(t * 3) % 2 else "game") if m1 == "game" else m1
        key = (flick, l0, l1, tuple(heads), w.sky_q)
        if key != self.lit_key:
            self.lit_key = key
            self.relight(flick, (l0, l1), heads)
        surf.blit(self.lm, (0, 0), special_flags=pygame.BLEND_RGB_MULT)
        if not flick:
            surf.blit(self.emis, (0, 0))
            for bx, by in heads:
                surf.fill((255, 236, 180), (bx - 2, by + 1, 5, 1))
        surf.blit(self.fx, (0, 0), special_flags=pygame.BLEND_RGB_ADD)


# --------------------------------------------------------------------------
# the dog
# --------------------------------------------------------------------------
def dog_sprite(frame, face, lying=False, wag=0):
    key = ("dog", frame, face, lying, wag)
    if key in _sprites:
        return _sprites[key]
    s = pygame.Surface((14, 10), pygame.SRCALPHA)
    fur, dark, light = (186, 128, 74), (110, 70, 46), (232, 196, 150)
    R = lambda c, x, y, w, h: s.fill(c, (x, y, w, h))
    by = 5 if lying else 3
    R(fur, 2, by, 9, 4)                           # body
    R(light, 4, by + 3, 5, 1)
    R(fur, 9, by - 3, 4, 4)                       # head
    R(light, 12, by - 1, 2, 2)                    # muzzle
    R(INK, 13, by - 1, 1, 1)
    R(INK, 11, by - 2, 1, 1)
    R(dark, 9, by - 3, 1, 3)                      # ear
    R(fur, 1 - (wag if not lying else 0), by - 1 - wag, 1, 2)   # tail
    R(fur, 1, by, 1, 1)
    if not lying:
        for i, x in enumerate((3, 5, 8, 10)):     # legs
            lift = 1 if (frame and (i + frame) % 2 == 0) else 0
            R(dark if i % 2 else fur, x + (lift if i < 2 else -lift), 7, 1, 3 - lift)
    if face < 0:
        s = pygame.transform.flip(s, True, False)
    s = finish(s)
    _sprites[key] = s
    return s


class Dog:
    def __init__(self):
        self.x, self.y, self.z = 112.0, 62.0, 0.0
        self.face, self.anim = 1, 0.0
        self.body = None
        self.moving = False
        self.lying = False
        self.target = None
        self.idle_t = 2.0
        self.speech, self.speech_t = "", 0.0
        self.visible = True
        self.still = 0.0
        self.has_ball = False
        self.fetch = False       # at the park: bring the ball back

    def bark(self, s="WOOF!"):
        self.speech, self.speech_t = s, 1.2
        play("bark", 0.4)

    def update(self, dt, w, friend, ball, wild):
        """friend: the person he follows (or None).  ball: a Body he will chase."""
        self.anim += dt
        self.speech_t -= dt
        if wild and not self.body:
            self.body = Body(self.x, self.y, self.z + 0.5, r=3, h=6, mu=0.4, rest=0.4)
            self.body.vz = random.uniform(20, 40)
            self.lying, self.has_ball, self.still = False, False, 0.0
            self.bark(random.choice(["YIP!", "AROO!"]))
        if self.body:
            b = self.body
            step_body(b, [(w.g[i] + w.jerk[i]) * GRAV for i in range(3)], dt)
            self.x, self.y, self.z = b.x, b.y, b.z
            self.still = self.still + dt if (b.touch and b.speed() < 10 and not wild) else 0.0
            if self.still > 1.2:
                self.body, self.z = None, (b.z if b.z > 20 else 0.0)
            return
        tgt, speed = None, 40.0
        if ball is not None and not self.has_ball and (ball.speed() > 18 or self.target == "ball") and ball.z < 12:
            self.target, tgt, speed = "ball", (ball.x, ball.y), 62.0
            if math.hypot(ball.x - self.x, ball.y - self.y) < 5:
                self.target = None
                if not self.fetch:                          # at home he just noses it along
                    ball.vx += random.uniform(-40, 40)
                    ball.vy += random.uniform(-40, 40)
        elif friend is not None:
            d = math.hypot(friend.x - self.x, friend.y - self.y)
            if d > 24 and abs(friend.z - self.z) < 5:
                tgt = (friend.x - 8 * friend.face, friend.y + 5)
        if tgt is None:
            self.idle_t -= dt
            if self.idle_t <= 0:
                self.idle_t = random.uniform(3, 9)
                self.lying = random.random() < 0.45
                if not self.lying:
                    self.target = (clamp(self.x + random.uniform(-26, 26), 8, RX - 8), clamp(self.y + random.uniform(-18, 18), 22, RY - 6))
            if isinstance(self.target, tuple):
                tgt = self.target
        self.moving = False
        if tgt:
            dx, dy = tgt[0] - self.x, tgt[1] - self.y
            d = math.hypot(dx, dy)
            if d > 2:
                nx, ny = self.x + dx / d * speed * dt, self.y + dy / d * speed * dt
                blocked = any(z0 <= self.z < z1 and x0 - 3 < nx < x1 + 3 and y0 - 3 < ny < y1 + 3 for (x0, y0, z0, x1, y1, z1) in SOLIDS)
                if not blocked:
                    self.x, self.y = clamp(nx, 4, RX - 4), clamp(ny, 4, RY - 4)
                    self.moving, self.lying = True, False
                    if abs(dx - dy) > 0.3 * d:
                        self.face = 1 if dx - dy > 0 else -1
                elif isinstance(self.target, tuple):
                    self.target = None
            elif isinstance(self.target, tuple):
                self.target = None

    def draw(self, surf):
        if not self.visible:
            return
        sx, sy = P(self.x, self.y, self.z)
        s = dog_sprite(1 + int(self.anim * 9) % 2 if self.moving else 0, self.face, self.lying and not self.body,
                       int(self.anim * 6) % 2 if not self.lying else 0)
        if self.body and not self.body.touch:
            s = pygame.transform.rotate(s, self.body.ang)
        surf.blit(s, (sx - s.get_width() // 2, sy - s.get_height() + 2))
        if self.has_ball:
            surf.fill((228, 68, 68), (sx + 5 * self.face, sy - 6, 3, 3))


# --------------------------------------------------------------------------
# getting about: a short drive between places
# --------------------------------------------------------------------------
class Drive:
    def __init__(self, world, where, back=False, dog=False):
        self.w, self.where, self.back, self.dog = world, where, back, dog
        self.t, self.done, self.people = 0.0, False, []
        world.man.visible = False
        play("car")

    def close(self):
        pass

    def update(self, dt):
        self.t += dt
        if self.t > 3.6:
            self.done = True

    def draw(self, surf):
        w, t = self.w, self.t
        w.paint_sky(surf)
        d = -1 if self.back else 1
        sun = w.sun()
        road, verge = mix((30, 30, 48), (96, 96, 108), sun), mix((16, 30, 44), (96, 160, 100), sun)
        if SEASON == "winter":
            verge = mix((40, 50, 80), (226, 232, 246), sun)
        surf.fill(verge, (0, 112, W, H - 112))
        for i in range(9):                               # things at the roadside slide past
            x = int((i * 47 - t * 150 * d) % (W + 60)) - 30
            if i % 3 == 0:                               # a house
                c = mix((30, 28, 52), ((200, 120, 100), (120, 150, 190), (220, 200, 150))[i // 3], sun)
                surf.fill(c, (x, 86, 30, 28))
                pygame.draw.polygon(surf, shade(c, 0.6), [(x - 3, 86), (x + 15, 72), (x + 33, 86)])
                surf.fill((255, 200, 110) if sun < 0.5 else shade(c, 0.5), (x + 6, 94, 6, 7))
                surf.fill((255, 200, 110) if sun < 0.5 else shade(c, 0.5), (x + 19, 94, 6, 7))
            else:                                        # a tree
                surf.fill(mix((16, 14, 30), (96, 70, 56), sun), (x + 4, 92, 3, 22))
                pygame.draw.circle(surf, mix((14, 24, 40), seasonal((196, 108, 46)), sun), (x + 5, 88), 10)
        surf.fill(road, (0, 122, W, 34))
        for i in range(8):
            surf.fill((230, 220, 150), (int((i * 46 - t * 240 * d) % (W + 40)) - 20, 138, 18, 2))
        # the car
        cx, cy = 160, 134 + int(math.sin(t * 22) * 1)
        body = [(-26, 0), (-26, -9), (-16, -10), (-9, -19), (9, -19), (17, -10), (26, -8), (26, 0)]
        pygame.draw.polygon(surf, (206, 70, 60), [(cx + x * d, cy + y) for x, y in body])
        pygame.draw.polygon(surf, OUTLINE, [(cx + x * d, cy + y) for x, y in body], 1)
        pygame.draw.polygon(surf, (150, 200, 230), [(cx + x * d, cy + y) for x, y in ((-8, -17), (8, -17), (14, -10), (-14, -10))])
        surf.fill(shade((206, 70, 60), 0.7), (cx - 26, cy - 4, 52, 2))
        hs = head_sprite("side", d, "ok")
        surf.blit(hs, (cx + 3 * d - hs.get_width() // 2, cy - 19))
        if self.dog:
            ds = dog_sprite(0, d)
            surf.blit(ds, (cx - 10 * d - 7, cy - 18), (0 if d < 0 else 8, 0, 7, 7))
        for wx in (-15, 15):
            pygame.draw.circle(surf, (24, 22, 34), (cx + wx, cy + 1), 5)
            pygame.draw.circle(surf, (160, 160, 176), (cx + wx, cy + 1), 2)
            a = t * 20
            surf.fill((24, 22, 34), (cx + wx + int(math.cos(a) * 2), cy + 1 + int(math.sin(a) * 2), 1, 1))
        if sun < 0.5:                                    # headlights
            add_glow(surf, (cx + 30 * d, cy - 5), 16, (90, 80, 40))
            surf.fill((255, 244, 190), (cx + 24 * d - (0 if d > 0 else 2), cy - 7, 2, 2))
        label = ("HEADING HOME" if self.back else "OFF TO " + self.where)
        pygame.draw.rect(surf, (20, 20, 44), (W // 2 - len(label) * 2 - 4, 6, len(label) * 4 + 7, 11), border_radius=2)
        text(surf, label, W // 2 - len(label) * 2, 9, (250, 250, 240))


# --------------------------------------------------------------------------
# places he drives to: the shop, the office, the park.  One room (or lawn)
# each, bigger on screen than the house, with the same throw-everyone physics.
# --------------------------------------------------------------------------
PLACE_VIEW = (132, 50, 120, 80, 40, 1.3, 1.3 / 3.0, 1.3 * 5 / 6.0)
PLACES = {
    "store": dict(name="THE SHOP", props=[("shelf1", (8, 0, 0, 38, 8, 26)), ("shelf2", (42, 0, 0, 72, 8, 26)),
                                          ("shelf3", (76, 0, 0, 100, 8, 26)), ("freezer", (0, 12, 0, 9, 48, 16)),
                                          ("counter", (20, 56, 0, 46, 64, 14))],
                  lights=[(30, 30), (80, 30), (60, 62)], wall=(214, 222, 200), floor=(206, 200, 184)),
    "office": dict(name="WORK", props=[("desk1", (8, 16, 0, 34, 28, 12)), ("desk2", (46, 16, 0, 72, 28, 12)),
                                       ("desk3", (84, 16, 0, 110, 28, 12)), ("cooler", (104, 62, 0, 112, 70, 22)),
                                       ("plantpot", (4, 66, 0, 12, 74, 8))],
                   lights=[(22, 36), (60, 36), (98, 36)], wall=(170, 184, 204), floor=(104, 116, 150)),
    "park": dict(name="THE PARK", props=[("tree1", (10, 8, 0, 15, 13, 18)), ("bench", (66, 8, 0, 92, 15, 8)),
                                         ("tree2", (104, 20, 0, 109, 25, 20)), ("lamp", (50, 6, 0, 52, 8, 30)),
                                         ("tree3", (6, 60, 0, 11, 65, 16))],
                 lights=[(51, 16)], wall=None, floor=(104, 176, 104)),
}


class Place:
    def __init__(self, world, kind, dog=False):
        global SOLIDS
        set_view(PLACE_VIEW)
        w = self.w = world
        self.kind, self.cfg = kind, PLACES[kind]
        self.props = self.cfg["props"]
        SOLIDS = [b for n, b in self.props]
        self.t, self.dt, self.done = 0.0, 0.03, False
        m = w.man
        m.x, m.y, m.z = 112.0, 6.0, 0.0
        m.pose, m.view, m.visible, m.away, m.hidden, m.carry = "stand", "side", True, False, False, None
        self.people = [m]
        self.loose = []                                   # (Body, colour): things that fly when shaken
        self.dog = w.dog if dog else None
        if self.dog:
            self.dog.x, self.dog.y, self.dog.z, self.dog.visible, self.dog.body = 104.0, 12.0, 0.0, True, None
            self.dog.fetch = True
        self.scripts = [self.run_man()]
        if kind == "store":
            self.keeper = self.extra(1, 33, 50, "front")
            self.scripts.append(self.browse(self.extra(2, 90, 14, "back")))
            for i, x in enumerate((14, 24, 50, 62, 84, 94)):
                self.loose.append((Body(x, 4, 26, r=1.5, h=4, mu=0.3), ((220, 80, 70), (90, 170, 220), (240, 200, 80))[i % 3]))
        elif kind == "office":
            for i, x in ((0, 21), (3, 97)):
                c = self.extra(i, x, 33, "back")
                c.pose, c.z = "sit", 6.0
            self.scripts.append(self.chatter())
            for x in (14, 52, 104):
                self.loose.append((Body(x, 20, 12, r=1.5, h=4, mu=0.35), (244, 240, 230)))
        else:
            self.scripts.append(self.jog(self.extra(1, 20, 40, "side")))
            self.ball = Body(60, 50, 0, r=3, h=6, mu=0.08, rest=0.6)
            self.loose.append((self.ball, (228, 68, 68)))
        for p in self.people:
            p.body, p.rag, p.still, p.cheer = None, None, 0.0, 0.0
        self.room = self.build()
        self.mask = pygame.mask.from_surface(self.room)
        self.lm = pygame.Surface((W, H))
        self.lit_key = None

    def extra(self, i, x, y, view):
        g = Man(self.w)
        g.look = FRIENDS[i]
        g.x, g.y, g.view, g.anim = float(x), float(y), view, i * 1.7
        self.people.append(g)
        return g

    def close(self):
        global SOLIDS
        SOLIDS = HOME_SOLIDS
        set_view(HOME_VIEW)
        if self.dog:
            self.dog.x, self.dog.y, self.dog.z, self.dog.body, self.dog.has_ball = 126.0, 20.0, 0.0, None, False
            self.dog.fetch, self.dog.target = False, None

    # ---- little scripts: generators, one step per tick ----
    def walk(self, p, x, y, speed=30.0):
        while True:
            if not p.body:
                dx, dy = x - p.x, y - p.y
                d = math.hypot(dx, dy)
                step = speed * self.dt
                if d <= step:
                    p.x, p.y, p.pose = float(x), float(y), "stand"
                    return
                p.x += dx / d * step
                p.y += dy / d * step
                if abs(dx - dy) > 0.25 * d:
                    p.face = 1 if dx - dy > 0 else -1
                p.pose, p.view, p.z = "walk", "side", 0.0
            yield

    def wait(self, p, t, pose="stand", view="side", z=0.0, face=None):
        while t > 0:
            if not p.body:
                t -= self.dt
                p.pose, p.view, p.z = pose, view, z
                if face:
                    p.face = face
            yield

    def run_man(self):
        m, w = self.w.man, self.w
        if self.kind == "store":
            m.say(random.choice(["JUST A FEW THINGS", "WHAT DO I NEED...", "SHOPPING TIME"]))
            for x in random.sample([22, 56, 88], 2):
                yield from self.walk(m, x, 13)
                yield from self.wait(m, 3.0, "look", "back")
                m.say(random.choice(["OOH, ON SALE", "DO I NEED THIS?", "THE FISH LIKES THESE", "HMM"]), 1.6)
                m.carry = "bag"
                yield from self.wait(m, 1.6, "stand", "back")
            yield from self.walk(m, 33, 70)
            yield from self.wait(m, 0.6, "stand", "back")
            cost = 8
            self.keeper.say("THAT'S %d COINS" % cost, 1.8)
            yield from self.wait(m, 2.0, "stand", "back")
            if m.coins >= cost:
                m.coins -= cost
                play("coin")
                self.keeper.say(random.choice(["THANK YOU!", "SEE YOU SOON"]), 1.6)
            else:
                self.keeper.say("PAY ME NEXT TIME", 1.8)
            m.food = 4
            yield from self.wait(m, 1.6, "stand", "back")
        elif self.kind == "office":
            m.say(random.choice(["MORNING ALL", "ANOTHER DAY...", "COFFEE FIRST"]))
            yield from self.walk(m, 59, 36)
            for i in range(3):
                yield from self.wait(m, random.uniform(6, 9), "sit", "back", 6.0)
                if i == 1:
                    m.say("COFFEE BREAK", 1.4)
                    yield from self.walk(m, 100, 72)
                    m.carry = "cup"
                    yield from self.wait(m, 3.0, "eat", "side", 0.0, 1)
                    m.carry = None
                    yield from self.walk(m, 59, 36)
                else:
                    m.say(random.choice(["EMAILS...", "SPREADSHEETS", "IS IT FRIDAY YET?", "REPLY ALL. OOPS."]), 1.8)
            m.coins += 35
            play("coin")
            m.say("PAYDAY! +35", 1.8)
            yield from self.wait(m, 1.6, "sit", "back", 6.0)
        else:
            d, ball = self.dog, self.ball
            m.say(random.choice(["FRESH AIR!", "WHAT A NICE SPOT", "COME ON, BOY"]))
            yield from self.walk(m, 62, 46)
            for i in range(3):
                m.cheer = 0.5
                m.say(random.choice(["FETCH!", "GO GET IT!", "READY?"]), 1.2)
                ball.x, ball.y, ball.z = m.x + 4, m.y, 14.0
                ball.vx, ball.vy, ball.vz = random.uniform(-70, 70), random.uniform(20, 60), 60.0
                if d:
                    d.target, d.has_ball = "ball", False
                t = 6.0
                while t > 0:
                    t -= self.dt
                    if d and not d.body:
                        if not d.has_ball and math.hypot(ball.x - d.x, ball.y - d.y) < 6 and ball.z < 8:
                            d.has_ball, d.target = True, None
                        if d.has_ball:
                            ball.x, ball.y, ball.z, ball.vx, ball.vy, ball.vz = d.x, d.y, 0.0, 0, 0, 0
                            if math.hypot(m.x - d.x, m.y - d.y) < 27:
                                d.has_ball = False
                                d.bark()
                                m.say(random.choice(["GOOD BOY!", "WELL DONE", "AGAIN?"]), 1.2)
                                break
                    yield
            yield from self.walk(m, 79, 18)
            m.say(random.choice(["AHH. PEACE.", "NICE EVENING", "LOOK AT THOSE TREES"]), 2.0)
            yield from self.wait(m, 9.0, "sit", "front", 7.0)
            m.need["fun"] = min(100, m.need["fun"] + 40)
        yield from self.walk(m, 112, 6)
        self.done = True

    def browse(self, p):
        while True:
            yield from self.walk(p, random.choice((16, 30, 48, 64, 82, 96)), 14)
            yield from self.wait(p, random.uniform(2, 5), "look", "back")

    def chatter(self):
        lines = ["MEETING AT 3", "WHO TOOK MY STAPLER?", "COFFEE?", "DID YOU SEE THE EMAIL?", "PRINTER'S JAMMED"]
        while True:
            yield from self.wait(self.people[1], random.uniform(5, 9), "sit", "back", 6.0)
            random.choice(self.people[1:]).say(random.choice(lines), 1.8)
            for p in self.people[1:]:
                if not p.body:
                    p.pose, p.view, p.z = "sit", "back", 6.0

    def jog(self, p):
        while True:
            for x, y in ((20, 70), (100, 70), (100, 40), (20, 40)):
                yield from self.walk(p, x, y, 44)

    # ---- one tick ----
    def update(self, dt):
        w = self.w
        self.t += dt
        self.dt = dt
        gx, gy, gz = w.g
        gh = math.hypot(gx, gy)
        wild = gz > -0.6 or gh > 0.62 or w.shake > 1.5
        acc = [(w.g[i] + w.jerk[i]) * GRAV for i in range(3)]
        for i, p in enumerate(self.people):
            if i:
                p.dt = dt
                p.anim += dt
                p.speech_t -= dt
                p.hop = max(0.0, p.hop - dt)
            p.cheer = max(0.0, p.cheer - dt)
            if wild and not p.body:
                p.body = Body(p.x, p.y, p.z + 0.5, r=3.5, h=16, mu=0.35, rest=0.3)
                p.body.vz = random.uniform(20, 45)
                p.rag_start()
                p.pose, p.view, p.still = "rag", "side", 0.0
                if random.random() < 0.4:
                    p.say(random.choice(TOSS), 2.0)
            if p.body:
                b = p.body
                step_body(b, acc, dt)
                p.x, p.y, p.z = b.x, b.y, b.z
                if p.pose == "rag":
                    p.rag_step(dt)
                    p.still = p.still + dt if (b.speed() < 10 and gz < -0.8 and gh < 0.4 and b.touch) else 0.0
                    if p.still > 0.7:
                        p.pose, p.view, p.still = "dizzy", "front", 0.0
                else:
                    p.still += dt
                    if p.still > 1.6:
                        p.body, p.rag = None, None
                        p.pose, p.view, p.z = "stand", "side", 0.0
                        for (x0, y0, z0, x1, y1, z1) in SOLIDS:
                            if x0 - 2 < p.x < x1 + 2 and y0 - 2 < p.y < y1 + 2:
                                p.y = clamp(y1 + 5, 4, RY - 4)
            else:
                p.wobble = lerp(p.wobble, clamp((gx - gy) * 0.7, -1, 1), min(1, dt * 6))
        for b, c in self.loose:
            step_body(b, acc, dt)
        if self.dog:
            self.dog.update(dt, w, self.people[0], getattr(self, "ball", None), wild)
        for s in self.scripts[:]:
            try:
                next(s)
            except StopIteration:
                self.scripts.remove(s)
        tilt = math.hypot(gx, gy)
        w.flick = max(w.flick - dt * 3, min(1.0, w.shake * 0.7 + w.rate * 0.45 + (0.22 if tilt > 0.3 else 0)
                                           + (0.3 if gz > 0 else 0)))
        w.dark_now = random.randint(70, 150) if (self.cfg["wall"] and random.random() < w.flick * 0.55) else 0

    # ---- painting ----
    def build(self):
        s = pygame.Surface((W, H))
        s.fill(KEY)
        s.set_colorkey(KEY, pygame.RLEACCEL)
        rnd = random.Random(9)
        cfg, kind = self.cfg, self.kind
        ink = (40, 34, 52)
        fl, wl = cfg["floor"], cfg["wall"]
        if kind == "park" and SEASON == "winter":
            fl = (226, 232, 246)
        g = 6 if wl else 0
        poly(s, shade(fl, 0.7), [P(-g, RY + g, 0), P(RX + g, RY + g, 0), P(RX + g, RY + g, -5), P(-g, RY + g, -5)], ink)
        poly(s, shade(fl, 0.5), [P(RX + g, RY + g, 0), P(RX + g, -g, 0), P(RX + g, -g, -5), P(RX + g, RY + g, -5)], ink)
        poly(s, shade(fl, 0.85) if wl else fl, [P(-g, -g), P(RX + g, -g), P(RX + g, RY + g), P(-g, RY + g)])
        if wl:
            poly(s, wl, [P(0, 0, 0), P(0, RY, 0), P(0, RY, RH), P(0, 0, RH)])
            poly(s, shade(wl, 0.84), [P(0, 0, 0), P(RX, 0, 0), P(RX, 0, RH), P(0, 0, RH)])
            wall_rect(s, shade(wl, 0.7), "x", 0, RY, 0, 6)
            wall_rect(s, shade(wl, 0.6), "y", 0, RX, 0, 6)
            n = 12 if kind == "store" else 10
            for i in range(0, RX, n):
                for j in range(0, RY, n):
                    c = shade(fl, (0.9 if (i // n + j // n) % 2 else 1.04) * rnd.uniform(0.97, 1.03))
                    poly(s, c, [P(i, j), P(min(RX, i + n), j), P(min(RX, i + n), min(RY, j + n)), P(i, min(RY, j + n))])
            pygame.draw.lines(s, ink, False, [P(0, RY, 0), P(0, RY, RH), P(0, 0, RH), P(RX, 0, RH), P(RX, 0, 0)])
            wall_rect(s, (120, 84, 60), "y", 106, 118, 0, 30, ink)                 # the way out
            wall_rect(s, (170, 214, 240), "y", 108.5, 115.5, 14, 26)
            if kind == "store":
                wall_rect(s, (200, 60, 60), "x", 14, 66, 28, 37, ink)
                px, py = P(0, 52, 34)
                text(s, "SHOP", px + 4, py - 6, (255, 244, 220))
                for y in range(0, RY, 8):
                    wall_rect(s, shade(wl, 0.92), "x", y, y + 4, 6, 26)
            else:
                wall_rect(s, (150, 200, 236), "x", 14, 40, 14, 32, ink)            # window
                pygame.draw.line(s, ink, P(0, 27, 14), P(0, 27, 32))
                wall_rect(s, (244, 244, 240), "x", 50, 74, 14, 32, ink)            # whiteboard
                for z, c in ((28, (200, 60, 60)), (24, (60, 110, 200)), (20, (60, 150, 90))):
                    pygame.draw.line(s, c, P(0, 53, z), P(0, 53 + rnd.uniform(8, 18), z))
        else:
            for _ in range(500):
                x, y = rnd.uniform(1, RX - 1), rnd.uniform(1, RY - 1)
                px, py = P(x, y)
                s.fill(shade(fl, rnd.choice((0.8, 0.9, 1.12, 1.2))), (px, py, 1, rnd.choice((1, 2))))
            path = (214, 196, 150)
            poly(s, path, [P(0, 34), P(RX, 34), P(RX, 44), P(0, 44)], shade(path, 0.7))
            poly(s, path, [P(96, 0), P(118, 0), P(118, 34), P(96, 34)])
            px, py = P(34, 64)
            pygame.draw.ellipse(s, (70, 130, 190), (px - 26, py - 8, 52, 17))
            pygame.draw.ellipse(s, (130, 190, 236), (px - 20, py - 6, 30, 7))
            for _ in range(40):
                x, y = rnd.uniform(2, RX - 2), rnd.uniform(46, RY - 2)
                px, py = P(x, y)
                s.fill(rnd.choice(((250, 244, 230), (250, 214, 100), (244, 130, 160))), (px, py - 1, 1, 1))
        sh = pygame.Surface((W, H), pygame.SRCALPHA)
        for n, b in self.props:
            pygame.draw.polygon(sh, (10, 8, 30, 70), [P(b[0], b[4], 0), P(b[3] + 3, b[4], 0), P(b[3] + 3, b[4] + 3, 0), P(b[0], b[4] + 3, 0)])
            pygame.draw.polygon(sh, (10, 8, 30, 70), [P(b[3], b[1], 0), P(b[3] + 3, b[1], 0), P(b[3] + 3, b[4] + 3, 0), P(b[3], b[4] + 3, 0)])
        s.blit(sh, (0, 0))
        return s

    def paint(self, s, name):
        b = dict(self.props)[name]
        x0, y0, z0, x1, y1, z1 = b
        rnd = random.Random(hash(name) & 255)
        if name.startswith("shelf"):
            box(s, *b, (150, 110, 80))
            for z in (3, 11, 19):
                wall_rect(s, (70, 54, 50), "y", x0 + 1, x1 - 1, z, z + 6.5, None, y1)
                x = x0 + 2
                while x < x1 - 3:
                    c = rnd.choice(((220, 80, 70), (90, 170, 220), (240, 200, 80), (120, 200, 130), (230, 230, 236), (200, 120, 210)))
                    h = rnd.choice((3.5, 4.5, 5.5))
                    wall_rect(s, c, "y", x, x + 2, z, z + h, None, y1)
                    x += rnd.choice((2.6, 3.2))
        elif name == "freezer":
            box(s, *b, (220, 228, 236), top=(150, 210, 236))
            for y in (24, 36):
                pygame.draw.line(s, (120, 150, 170), P(x1, y, 1), P(x1, y, z1 - 1))
        elif name == "counter":
            box(s, *b, (150, 104, 70), top=(214, 196, 160))
            box(s, 28, 57, 14, 36, 62, 19, (70, 72, 90), bands=1)
            px, py = P(32, 62, 17)
            s.fill((120, 230, 150), (px - 2, py, 4, 2))
        elif name.startswith("desk"):
            box(s, x0, y0, 10, x1, y1, z1, (176, 132, 90), bands=1)
            for x, y in ((x0 + 1, y1 - 1), (x1 - 1, y1 - 1), (x1 - 1, y0 + 1)):
                pygame.draw.line(s, (70, 60, 66), P(x, y, 0), P(x, y, 10), 2)
            cx = (x0 + x1) / 2
            box(s, cx - 6, y0 + 2, z1, cx + 6, y0 + 4, z1 + 10, (40, 40, 56), bands=1)
            wall_rect(s, (120, 190, 240), "y", cx - 5, cx + 5, z1 + 1.5, z1 + 9, None, y0 + 4)
            for i in range(3):
                pygame.draw.line(s, (230, 240, 250), P(cx - 4, y0 + 4, z1 + 3 + i * 2), P(cx - 4 + rnd.uniform(3, 8), y0 + 4, z1 + 3 + i * 2))
            box(s, cx - 5, y0 + 7, z1, cx + 5, y0 + 10, z1 + 0.8, (220, 220, 230), bands=1)
        elif name == "cooler":
            box(s, x0, y0, 0, x1, y1, 13, (230, 232, 240))
            box(s, x0 + 1, y0 + 1, 13, x1 - 1, y1 - 1, z1, (120, 190, 240), bands=1)
        elif name == "plantpot":
            box(s, *b, (196, 104, 70), bands=1)
            px, py = P((x0 + x1) / 2, (y0 + y1) / 2, z1)
            for dx, dy, r in ((0, -5, 6), (-4, -9, 4), (4, -10, 4), (0, -14, 3)):
                pygame.draw.circle(s, (62, 140, 78) if r > 3 else (124, 200, 112), (px + dx, py + dy), r)
        elif name == "bench":
            box(s, x0, y0, 5.5, x1, y1, 7.5, (170, 116, 72), bands=1)
            box(s, x0, y0, 7.5, x1, y0 + 1.5, 16, (150, 100, 62), bands=1)
            for x in (x0 + 1, x1 - 1):
                pygame.draw.line(s, (60, 52, 60), P(x, y1, 0), P(x, y1, 5.5), 2)
        elif name.startswith("tree"):
            box(s, *b, (110, 76, 56), bands=1)
            px, py = P((x0 + x1) / 2, (y0 + y1) / 2, z1 + 8)
            if SEASON != "winter":
                for dx, dy, r, c in ((-8, 4, 9, (112, 52, 36)), (8, 5, 9, (120, 56, 36)), (0, 0, 12, (140, 66, 38)),
                                     (-5, -6, 8, (166, 84, 40)), (6, -5, 7, (196, 108, 46))):
                    pygame.draw.circle(s, seasonal(c) if SEASON != "summer" else mix(seasonal(c), (70, 170, 90), 0.5), (px + dx, py + dy), r)
            else:
                for dx, dy in ((-8, -4), (7, -6), (0, -12), (-3, -8), (5, -2)):
                    pygame.draw.line(s, (110, 76, 56), (px, py + 8), (px + dx, py + dy))
                    s.fill((240, 244, 255), (px + dx - 1, py + dy - 1, 3, 1))
        elif name == "lamp":
            pygame.draw.line(s, (50, 46, 62), P(51, 7, 0), P(51, 7, 28), 2)
            px, py = P(51, 7, 30)
            s.fill((50, 46, 62), (px - 3, py - 2, 7, 2))
            s.fill((255, 220, 140), (px - 2, py, 5, 3))

    def draw(self, surf):
        w = self.w
        w.paint_sky(surf)
        if not self.cfg["wall"]:
            w.rain(surf)
        surf.blit(self.room, (0, 0))
        things = []
        for p in self.people:
            if p.visible:
                def fn(s, p=p):
                    if p.z < 1.5 and p.pose != "sit":
                        cast_shadow(s, P(p.x, p.y, 0), 3, -1, 11)
                    p.draw(s)
                things.append((p.x, p.y, p.z, fn))
        for b, c in self.loose:
            def fn(s, b=b, c=c):
                px, py = P(b.x, b.y, b.z)
                if b is getattr(self, "ball", None):
                    if not (self.dog and self.dog.has_ball):
                        bs = item_sprite("ball")
                        s.blit(bs, (px - 4, py - 7))
                else:
                    s.fill(OUTLINE, (px - 2, py - 5, 4, 6))
                    s.fill(c, (px - 1, py - 4, 2, 4))
            things.append((b.x, b.y, b.z, fn))
        if self.dog:
            things.append((self.dog.x, self.dog.y, self.dog.z, self.dog.draw))
        slots = [[] for _ in range(len(self.props) + 1)]
        for x, y, z, fn in things:
            for i, (n, b) in enumerate(self.props):
                if z < b[5] - 0.5 and (x < b[0] or y < b[1]) and x <= b[3] + 30 and y <= b[4] + 30:
                    slots[i].append((x + y, z, id(fn), fn))
                    break
            else:
                slots[-1].append((x + y, z, id(fn), fn))
        for i, (n, b) in enumerate(self.props):
            for e in sorted(slots[i], key=lambda e: e[:3]):
                e[3](surf)
            piece, at = w.sprite("place_%s_%s" % (self.kind, n), lambda s, n=n: self.paint(s, n))
            surf.blit(piece, at)
        for e in sorted(slots[-1], key=lambda e: e[:3]):
            e[3](surf)
        # light: daylight outdoors and through the windows, lamps after dark
        flick = bool(w.dark_now)
        key = (w.sky_q, flick)
        if key != self.lit_key:
            self.lit_key = key
            sun = w.sky_q[1] / 12.0
            indoors = bool(self.cfg["wall"])
            amb = mix((70, 76, 124), (240, 236, 228), sun if not indoors else max(sun, 0.25))
            if flick:
                amb = shade(amb, 0.7)
            self.mask.to_surface(self.lm, setcolor=amb, unsetcolor=WHITE)
            if indoors:
                pygame.draw.polygon(self.lm, amb, [P(0, RY, RH), P(0, 0, RH), P(RX, 0, RH), P(RX, 0, -5), P(RX, RY, -5), P(0, RY, -5)])
            if not flick and (indoors or sun < 0.6):
                for x, y in self.cfg["lights"]:
                    add_light(self.lm, P(x, y, 0), 70, 30, (150, 140, 110) if indoors else (170, 120, 60))
        surf.blit(self.lm, (0, 0), special_flags=pygame.BLEND_RGB_MULT)
        if self.dog and self.dog.speech_t > 0:
            draw_bubble(surf, self.dog.speech, *P(self.dog.x, self.dog.y, self.dog.z + 12))


# --------------------------------------------------------------------------
# tilt: IMU on the device, keys on a PC
# --------------------------------------------------------------------------
ELEV = math.radians(25)
SE, CE = math.sin(ELEV), math.cos(ELEV)


def world_from_screen(sr, sd, so):
    """(toward screen-right, toward screen-down, toward viewer) -> room x,y,z."""
    ga = -sd * SE - so * CE
    gz = -sd * CE + so * SE
    return ((sr - ga) / math.sqrt(2), (-sr - ga) / math.sqrt(2), gz)


def remap_pitch(sd, so):
    """Anything between 'standing upright' and 'flat on the desk' counts as level."""
    q = math.hypot(sd, so)
    if q < 1e-6:
        return 0.0, 0.0
    e = math.degrees(ELEV)
    phi = math.degrees(math.atan2(-so, sd))
    lo, hi = 0.85 * e, e + 0.15 * (90 - e)
    if 0 <= phi <= 90:
        p = e + 0.15 * (phi - e)
    elif phi > 90:
        p = hi + (phi - 90) / 90 * (180 - hi)
    elif phi >= -90:
        p = -90 + (phi + 90) / 90 * (lo + 90)
    else:
        p = phi
    p = math.radians(p)
    return q * math.cos(p), -q * math.sin(p)


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def norm(a):
    m = math.sqrt(dot(a, a)) or 1.0
    return (a[0] / m, a[1] / m, a[2] / m)


class IMU:
    """BMI270 accelerometer: kernel IIO if present, otherwise raw I2C."""

    def __init__(self, bus):
        self.kind = None
        self.note = ""
        for d in glob.glob("/sys/bus/iio/devices/iio:device*"):
            if os.path.exists(d + "/in_accel_x_raw"):
                try:
                    sc = d + "/in_accel_scale"
                    self.scale = float(open(sc).read()) / 9.80665 if os.path.exists(sc) else 1 / 4096.0
                    self.dir, self.kind = d, "iio"
                    return
                except OSError:
                    pass
        try:
            import ctypes
            import fcntl

            class Msg(ctypes.Structure):
                _fields_ = [("addr", ctypes.c_uint16), ("flags", ctypes.c_uint16), ("len", ctypes.c_uint16),
                            ("buf", ctypes.POINTER(ctypes.c_uint8))]

            class Rdwr(ctypes.Structure):
                _fields_ = [("msgs", ctypes.POINTER(Msg)), ("nmsgs", ctypes.c_uint32)]

            self._ct, self._fcntl, self._Msg, self._Rdwr = ctypes, fcntl, Msg, Rdwr
            self.fd = os.open(bus, os.O_RDWR)
            if self.reg(0x00, 1)[0] != 0x24:
                self.note = "no BMI270 at 0x68"
                return
            if self.reg(0x21, 1)[0] & 0x0F != 1:
                self.note = "BMI270 not initialised (open the Compass app once)"
                return
            pwr = self.reg(0x7D, 1)[0]
            if not pwr & 0x04:
                self.write(0x7C, 0x00)
                self.write(0x7D, pwr | 0x04)
                time.sleep(0.05)
            self.scale = (2 << (self.reg(0x41, 1)[0] & 3)) / 32768.0
            self.kind = "i2c"
        except Exception as e:      # no bus, no permission, not Linux...
            self.note = "IMU: %s" % e

    def _xfer(self, wr, n):
        ct = self._ct
        wb = (ct.c_uint8 * len(wr))(*wr)
        msgs = (self._Msg * 2)()
        msgs[0] = self._Msg(0x68, 0, len(wr), wb)
        cnt = 1
        rb = (ct.c_uint8 * max(n, 1))()
        if n:
            msgs[1] = self._Msg(0x68, 1, n, rb)
            cnt = 2
        self._fcntl.ioctl(self.fd, 0x0707, self._Rdwr(msgs, cnt))
        return bytes(rb[:n])

    def reg(self, r, n):
        return self._xfer([r], n)

    def write(self, r, v):
        self._xfer([r, v], 0)

    def read(self):
        try:
            if self.kind == "iio":
                return tuple(int(open("%s/in_accel_%s_raw" % (self.dir, a)).read()) * self.scale for a in "xyz")
            x, y, z = struct.unpack("<hhh", self.reg(0x0C, 6))
            return (x * self.scale, y * self.scale, z * self.scale)
        except Exception:
            return None


class Tilt:
    def __init__(self, imu, cal):
        self.imu = imu if imu and imu.kind else None
        self.f = tuple(cal.get("f", (0, 0, 1)))      # sensor reading when flat, screen up
        self.u = tuple(cal.get("u", (0, 1, 0)))      # sensor reading when upright
        self.lp = None
        self.raw = (0, 0, 1)
        self.roll = 0.0
        self.pitch = 0.0
        self.flip = False

    def read(self, dt, held):
        if self.imu:
            a = self.imu.read()
            if a:
                self.raw = a
                if self.lp is None:
                    self.lp = a
                k = min(1.0, dt / 0.12)
                self.lp = tuple(self.lp[i] + (a[i] - self.lp[i]) * k for i in range(3))
                r = norm(cross(self.u, self.f))
                gv = norm(self.lp)
                sd, so = remap_pitch(dot(gv, self.u), -dot(gv, self.f))
                g = world_from_screen(-dot(gv, r), sd, so)
                j = tuple(a[i] - self.lp[i] for i in range(3))
                jm = math.sqrt(j[0] ** 2 + j[1] ** 2 + j[2] ** 2)
                j = tuple(c * (jm - 0.35) / jm for c in j) if jm > 0.35 else (0.0, 0.0, 0.0)
                jerk = world_from_screen(-dot(j, r), dot(j, self.u), -dot(j, self.f))
                return g, jerk
        # simulated: J/L roll, I/M pitch, U flip, K shake
        tr = (-1 if "rl" in held else 0) + (1 if "rr" in held else 0)
        tp = (-1 if "pu" in held else 0) + (1 if "pd" in held else 0)
        if tr:
            self.roll += tr * 110 * dt
        else:
            self.roll = lerp(self.roll, 180.0 if self.flip else 0.0, min(1, dt * 5))
        if tp:
            self.pitch = clamp(self.pitch + tp * 110 * dt, -120, 120)
        else:
            self.pitch = lerp(self.pitch, 0.0, min(1, dt * 5))
        self.roll = clamp(self.roll, -200, 200)
        p = ELEV + math.radians(self.pitch)
        rr = math.radians(self.roll)
        sd0, so0 = math.cos(p), -math.sin(p)
        g = world_from_screen(sd0 * math.sin(rr), sd0 * math.cos(rr), so0)
        jerk = (0.0, 0.0, 0.0)
        if "shake" in held:
            v = norm((random.uniform(-1, 1), random.uniform(-1, 1), random.uniform(-0.6, 1)))
            jerk = tuple(c * 2.4 for c in v)
        return g, jerk


# --------------------------------------------------------------------------
# input and output back-ends
# --------------------------------------------------------------------------
EV_KEYS = {105: "left", 106: "right", 103: "up", 108: "down", 28: "ok", 57: "ok", 15: "tab", 1: "esc",
           36: "rl", 38: "rr", 23: "pu", 50: "pd", 37: "shake", 22: "flip", 46: "calib",
           17: "up", 30: "left", 31: "down", 32: "right"}


EV_CHARS = {14: "\b", 28: "\r", 57: " "}
for _row, _keys in ((16, "QWERTYUIOP"), (30, "ASDFGHJKL"), (44, "ZXCVBNM"), (2, "1234567890")):
    for _i, _c in enumerate(_keys):
        EV_CHARS[_row + _i] = _c


class EvdevKeys:
    def __init__(self, dev):
        self.fds = []
        paths = [dev] if dev else []
        if not paths:
            pref = "/dev/input/by-path/platform-3f804000.i2c-event"
            paths = [pref] if os.path.exists(pref) else sorted(glob.glob("/dev/input/event*"))
        for p in paths:
            try:
                self.fds.append(os.open(p, os.O_RDONLY | os.O_NONBLOCK))
            except OSError:
                pass
        self.fmt = "llHHi"
        self.size = struct.calcsize(self.fmt)
        self.held = set()
        self.chars = []

    def poll(self):
        pressed = []
        for fd in self.fds:
            while True:
                try:
                    data = os.read(fd, self.size * 32)
                except (BlockingIOError, OSError):
                    break
                if not data:
                    break
                for i in range(0, len(data) - self.size + 1, self.size):
                    _, _, typ, code, val = struct.unpack_from(self.fmt, data, i)
                    name = EV_KEYS.get(code)
                    if typ == 1 and val == 1 and code in EV_CHARS:
                        self.chars.append(EV_CHARS[code])
                    if typ == 1 and name:
                        if val == 1:
                            pressed.append(name)
                            self.held.add(name)
                        elif val == 0:
                            self.held.discard(name)
        return pressed, None, False


class PygameKeys:
    def __init__(self, scale):
        k = pygame
        self.map = {k.K_LEFT: "left", k.K_RIGHT: "right", k.K_UP: "up", k.K_DOWN: "down", k.K_RETURN: "ok",
                    k.K_SPACE: "ok", k.K_TAB: "tab", k.K_ESCAPE: "esc", k.K_j: "rl", k.K_l: "rr", k.K_i: "pu",
                    k.K_m: "pd", k.K_k: "shake", k.K_u: "flip", k.K_c: "calib", k.K_w: "up", k.K_a: "left",
                    k.K_s: "down", k.K_d: "right"}
        self.held = set()
        self.chars = []
        self.scale = scale

    def poll(self):
        pressed, mouse, clicked = [], None, False
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                pressed.append("esc")
            if e.type == pygame.KEYDOWN:
                ch = "\b" if e.key == pygame.K_BACKSPACE else ("\r" if e.key == pygame.K_RETURN else e.unicode.upper())
                if ch and (ch in "\b\r " or ch.isalnum() or ch in "?!'.,"):
                    self.chars.append(ch)
            if e.type == pygame.QUIT:
                pass
            elif e.type == pygame.KEYDOWN and e.key in self.map:
                pressed.append(self.map[e.key])
                self.held.add(self.map[e.key])
            elif e.type == pygame.KEYUP and e.key in self.map:
                self.held.discard(self.map[e.key])
            elif e.type == pygame.MOUSEMOTION:
                mouse = (e.pos[0] // self.scale, e.pos[1] // self.scale)
            elif e.type == pygame.MOUSEBUTTONDOWN:
                mouse = (e.pos[0] // self.scale, e.pos[1] // self.scale)
                clicked = True
        return pressed, mouse, clicked


class FbOut:
    def __init__(self, dev, rotate):
        name = os.path.basename(dev)
        sysd = "/sys/class/graphics/" + name
        try:
            self.w, self.h = [int(v) for v in open(sysd + "/virtual_size").read().strip().split(",")]
            self.bpp = int(open(sysd + "/bits_per_pixel").read())
            self.stride = int(open(sysd + "/stride").read())
        except (OSError, ValueError):
            self.w, self.h, self.bpp, self.stride = W, H, 16, W * 2
        if rotate is None:
            rotate = 90 if (self.w < self.h) else 0
        self.rotate = rotate
        self.f = open(dev, "r+b", buffering=0)
        if self.bpp == 16:
            self.buf = pygame.Surface((self.w, self.h), 0, 16, (0xF800, 0x07E0, 0x001F, 0))
        else:
            self.buf = pygame.Surface((self.w, self.h), 0, 32, (0xFF0000, 0x00FF00, 0x0000FF, 0))

    def show(self, surf):
        if self.rotate:
            surf = pygame.transform.rotate(surf, self.rotate)
        if surf.get_size() == (self.w, self.h):
            self.buf.blit(surf, (0, 0))
        else:
            sw, sh = surf.get_size()
            k = min(self.w / sw, self.h / sh)
            sc = pygame.transform.scale(surf, (max(1, int(sw * k)), max(1, int(sh * k))))
            self.buf.fill((0, 0, 0))
            self.buf.blit(sc, ((self.w - sc.get_width()) // 2, (self.h - sc.get_height()) // 2))
        data = self.buf.get_buffer().raw
        pitch = self.buf.get_pitch()
        self.f.seek(0)
        if pitch == self.stride:
            self.f.write(data)
        else:
            n = min(pitch, self.stride)
            pad = b"\0" * (self.stride - n)
            self.f.write(b"".join(data[r * pitch:r * pitch + n] + pad for r in range(self.h)))


class WindowOut:
    def __init__(self, scale):
        self.scale = scale
        self.win = pygame.display.set_mode((W * scale, H * scale))
        pygame.display.set_caption("Zero Life  -  J/L tilt  I/M pitch  U flip  K shake  mouse to click")

    def show(self, surf):
        pygame.transform.scale(surf, self.win.get_size(), self.win)
        pygame.display.flip()


# --------------------------------------------------------------------------
STATE_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config"),
                         "zerolife")
STATE = os.path.join(STATE_DIR, "state.json")


def load_state():
    # the game used to be called Tiny Loft: carry an old save over
    for path in (STATE, STATE.replace("zerolife", "tinyloft")):
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {}


def save_state(world, cal):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(STATE + ".tmp", "w") as f:
            json.dump({"world": world.dump(), "cal": cal, "version": VERSION}, f)
        os.replace(STATE + ".tmp", STATE)
    except OSError:
        pass


def main():
    if ARGS.seed is not None:
        random.seed(ARGS.seed)
    pygame.init()
    snd_init()
    if ARGS.weather and not HEADLESS:
        weather_thread(ARGS.weather)
    if USE_FB or HEADLESS:
        pygame.display.set_mode((1, 1))
    saved = {} if (ARGS.fresh or HEADLESS) else load_state()
    cal = saved.get("cal", {})

    if HEADLESS:
        out, keys = None, None
    elif USE_FB:
        out, keys = FbOut(ARGS.fb, ARGS.rotate), EvdevKeys(ARGS.kbd)
    else:
        out, keys = WindowOut(ARGS.scale), PygameKeys(ARGS.scale)

    imu = None
    if USE_FB and not ARGS.no_imu:
        imu = IMU(ARGS.i2c)
        if imu.note:
            sys.stderr.write(imu.note + " - using keys J/L/I/M/K for tilt\n")
    tilt = Tilt(imu, cal)
    world = World(saved.get("world"))
    screen = pygame.Surface((W, H))
    clock = pygame.time.Clock()

    def stop(*_):
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
    except (ValueError, OSError):
        pass

    cur = [160.0, 100.0]
    cur_t = 0.0
    calib = 0            # 0 off, 1 waiting for flat, 2 waiting for upright
    typing = None        # the line being typed to him, or None
    hint_t = 9.0
    saved_at = 0.0
    frame = 0
    busy = 0.0           # smoothed milliseconds of work per frame
    sim_roll, sim_pitch = [float(v) for v in ARGS.tilt.split(",")]
    ta, tb = [int(v) for v in ARGS.tiltat.split("-")]
    sa, sb = ([int(v) for v in ARGS.shakeat.split("-")] if ARGS.shakeat else (1, 0))
    if ARGS.act:
        m = world.man
        name, _, arg = ARGS.act.partition(":")
        m.script = getattr(m, "act_" + name)(arg) if arg else getattr(m, "act_" + name)()

    try:
        while True:
            dt = 1 / 30.0 if HEADLESS else min(0.05, clock.tick(30) / 1000.0)
            frame += 1
            busy = busy * 0.97 + clock.get_rawtime() * 0.03
            if busy > 36 and frame > 90:
                world.lite = True
            held = set()
            if keys:
                pressed, mouse, clicked = keys.poll()
                held = keys.held
                chars, keys.chars = keys.chars, []
                if typing is None:
                    if "T" in chars and not calib:
                        typing = ""
                        pressed = []
                else:                                   # everything typed goes into the line
                    for ch in chars:
                        typing = typing[:-1] if ch == "\b" else (typing + ch)[:28]
                    if "ok" in pressed and "\r" in chars:
                        world.man.hear(typing)
                        typing = None
                    elif "esc" in pressed:
                        typing = None
                    pressed, held, clicked = [], set(), False
                if mouse:
                    cur[0], cur[1] = mouse
                    cur_t = 4.0
                if clicked:
                    world.click(*cur)
                for k in pressed:
                    if k == "esc":
                        raise KeyboardInterrupt
                    elif k == "ok":
                        if calib == 1:
                            tilt.f = norm(tilt.raw)
                            calib = 2
                        elif calib == 2:
                            u = tilt.raw
                            d = dot(u, tilt.f)
                            tilt.u = norm(tuple(u[i] - d * tilt.f[i] for i in range(3)))
                            cal = {"f": list(tilt.f), "u": list(tilt.u)}
                            calib = 0
                            save_state(world, cal)
                        else:
                            cur_t = 4.0
                            world.click(*cur)
                    elif k == "tab":
                        ts = world.targets()
                        if ts:
                            i = int(cur_t > 0) and next((i for i, t in enumerate(ts)
                                                         if abs(t[2] - cur[0]) + abs(t[3] - cur[1]) < 3), -1) + 1
                            t = ts[i % len(ts)]
                            cur[0], cur[1] = t[2], t[3]
                            cur_t = 4.0
                    elif k == "flip":
                        tilt.flip = not tilt.flip
                    elif k == "calib" and tilt.imu:
                        calib = 1
                mv = 80 * dt
                for k, dx, dy in (("left", -1, 0), ("right", 1, 0), ("up", 0, -1), ("down", 0, 1)):
                    if k in held:
                        cur[0] = clamp(cur[0] + dx * mv, 0, W - 1)
                        cur[1] = clamp(cur[1] + dy * mv, 0, H - 1)
                        cur_t = 4.0
            else:
                if ta <= frame <= tb:
                    tilt.roll, tilt.pitch = sim_roll, sim_pitch
                    held = {"rl", "rr", "pu", "pd"}        # freeze the spring-back
                if sa <= frame <= sb:
                    held = held | {"shake"}
            g, jerk = tilt.read(dt, held)
            world.update(dt, g, jerk)
            world.draw(screen)

            cur_t -= dt
            hint_t -= dt
            if cur_t > 0:
                x, y = int(cur[0]), int(cur[1])
                for c, o in ((INK, 1), ((255, 255, 255), 0)):
                    pygame.draw.line(screen, c, (x - 4, y + o), (x - 2, y + o))
                    pygame.draw.line(screen, c, (x + 2, y + o), (x + 4, y + o))
                    pygame.draw.line(screen, c, (x + o, y - 4), (x + o, y - 2))
                    pygame.draw.line(screen, c, (x + o, y + 2), (x + o, y + 4))
            if calib:
                msg = ["LAY IT FLAT, SCREEN UP", "THEN PRESS ENTER"] if calib == 1 else \
                      ["HOLD IT UPRIGHT, FACING YOU", "THEN PRESS ENTER"]
                pygame.draw.rect(screen, (252, 250, 240), (86, 70, 148, 26), border_radius=3)
                pygame.draw.rect(screen, INK, (86, 70, 148, 26), 1, border_radius=3)
                for i, l in enumerate(msg):
                    text(screen, l, 160 - len(l) * 2, 76 + i * 8, INK)
            elif hint_t > 0 and not HEADLESS:
                text(screen, "ARROWS + ENTER: FIX", 4, 150, (150, 150, 180))
                text(screen, "TAB: NEXT  T: TALK  ESC: QUIT", 4, 158, (150, 150, 180))

            if typing is not None:
                pygame.draw.rect(screen, (20, 20, 44), (2, H - 12, W - 4, 10), border_radius=2)
                text(screen, "> " + typing + ("_" if int(world.t * 3) % 2 else ""), 5, H - 10, (250, 250, 240))
            if out:
                out.show(screen)
            if HEADLESS and frame >= ARGS.frames:
                big = pygame.transform.scale(screen, (W * ARGS.scale, H * ARGS.scale))
                pygame.image.save(big, ARGS.shot)
                break
            if not HEADLESS and world.t - saved_at > 30:
                saved_at = world.t
                save_state(world, cal)
    except KeyboardInterrupt:
        pass
    finally:
        if not HEADLESS:
            save_state(world, cal)
        pygame.quit()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # the launcher hides what an app prints, so leave the reason where it can be found
        import traceback
        try:
            with open("/tmp/zerolife.log", "a") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(sys.argv) + "\n" + traceback.format_exc() + "\n")
        except OSError:
            pass
        raise
