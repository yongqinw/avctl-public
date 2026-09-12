"""What makes the remote installable: manifest, icon, splash screens.

The icon and launch images are RENDERED here, in pure Python, rather than
checked in as binaries: the design is a power glyph in the app's own
phosphor green on its own charcoal, and code that draws it is diffable,
tweakable, and can emit every size Apple wants without a graphics
dependency the mini would otherwise never need. Everything is rendered at
most once per size and cached for the life of the process.

Colors come from app.css's dark theme (--page, --dial-a, --screen-ink):
the home screen icon should look like the app it opens.
"""

from __future__ import annotations

import math
import struct
import zlib

# app.css dark theme, hex-for-hex.
PAGE = (8, 9, 10)          # --page: the splash and manifest background
BODY_A = (31, 34, 37)      # --body-a: the icon plate's top
BODY_B = (19, 21, 23)      # --body-b: the icon plate's bottom
GREEN = (47, 163, 101)     # --dial-a: the glyph
GREEN_HI = (108, 240, 164)  # --screen-ink: the glyph's bright core

THEME_COLOR = "#08090a"

# The devices a splash can be served for, as CSS points (width, height,
# ratio). Physical pixels are points x ratio; the head links and the route
# whitelist are BOTH generated from this list, so they cannot drift.
SPLASH_POINTS = [
    (320, 568, 2),   # SE (1st gen)
    (375, 667, 2),   # 6s / 7 / 8 / SE 2-3
    (375, 812, 3),   # X / XS / 11 Pro / 12-13 mini
    (390, 844, 3),   # 12 / 13 / 14
    (393, 852, 3),   # 14 Pro / 15 / 16
    (402, 874, 3),   # 16 Pro
    (414, 896, 2),   # XR / 11
    (414, 896, 3),   # XS Max / 11 Pro Max
    (428, 926, 3),   # 12-13 Pro Max / 14 Plus
    (430, 932, 3),   # 14 Pro Max / 15-16 Plus
    (440, 956, 3),   # 16 Pro Max
]

ICON_SIZES = (180, 192, 512)   # apple-touch-icon and the manifest pair

_cache: dict[str, bytes] = {}


# --- a PNG, from nothing --------------------------------------------------


def _chunk(tag: bytes, data: bytes) -> bytes:
    body = tag + data
    return struct.pack(">I", len(data)) + body + struct.pack(
        ">I", zlib.crc32(body) & 0xFFFFFFFF)


def _png(width: int, height: int, rows) -> bytes:
    """Truecolor 8-bit PNG from an iterable of raw RGB scanlines."""
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    press = zlib.compressobj(9)
    idat = b""
    for row in rows:
        idat += press.compress(b"\x00" + row)   # filter 0 per scanline
    idat += press.flush()
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header)
            + _chunk(b"IDAT", idat) + _chunk(b"IEND", b""))


# --- the glyph ------------------------------------------------------------
#
# The universal power symbol: an open ring with a bar through the gap.
# Drawn with distance functions and a one-pixel smoothstep, so every size
# comes out anti-aliased without any raster library.


def _glyph_coverage(px: float, py: float, s: float) -> float:
    cx = cy = s / 2.0
    ring_r = 0.26 * s
    half_t = 0.032 * s
    gap = 0.62            # radians of arc left open either side of straight up
    bar_top, bar_bot = 0.16 * s, 0.46 * s
    aa = 1.0

    def stroke(distance: float) -> float:
        return min(1.0, max(0.0, (half_t - distance) / aa + 0.5))

    dx, dy = px - cx, py - cy
    cov = 0.0
    # The arc, with the gap cut out of the top...
    if abs(math.atan2(dx, -dy)) > gap:
        cov = stroke(abs(math.hypot(dx, dy) - ring_r))
    # ...its two rounded ends...
    for side in (-1.0, 1.0):
        ex = cx + ring_r * math.sin(side * gap)
        ey = cy - ring_r * math.cos(gap)
        cov = max(cov, stroke(math.hypot(px - ex, py - ey)))
    # ...and the bar through the gap, rounded caps for free.
    clamped = min(max(py, bar_top), bar_bot)
    return max(cov, stroke(math.hypot(px - cx, py - clamped)))


def _glyph_color(py: float, s: float) -> tuple[int, int, int]:
    """Bright at the top, settling toward the base green -- the same
    top-lit look every key in app.css has."""
    t = min(1.0, max(0.0, py / s))
    return tuple(round(GREEN_HI[i] + (GREEN[i] - GREEN_HI[i]) * t)
                 for i in range(3))


def _blend(base: tuple[int, int, int], top: tuple[int, int, int],
           cov: float) -> bytes:
    return bytes(round(base[i] + (top[i] - base[i]) * cov) for i in range(3))


def icon_png(size: int) -> bytes:
    """The home-screen icon: glyph on the app's charcoal plate.

    The glyph stays inside the maskable safe zone (a 40%-radius circle), so
    the same bitmap serves as `purpose: maskable` -- Android can round it,
    iOS applies its own corner mask to the square.
    """
    key = f"icon-{size}"
    if key not in _cache:
        rows = []
        for y in range(size):
            plate = tuple(round(BODY_A[i] + (BODY_B[i] - BODY_A[i]) * y / size)
                          for i in range(3))
            row = bytearray()
            for x in range(size):
                cov = _glyph_coverage(x + 0.5, y + 0.5, size)
                if cov > 0.0:
                    row += _blend(plate, _glyph_color(y + 0.5, size), cov)
                else:
                    row += bytes(plate)
            rows.append(bytes(row))
        _cache[key] = _png(size, size, rows)
    return _cache[key]


def splash_png(width: int, height: int) -> bytes:
    """A launch image: the page background with a dim glyph at the center.

    iOS shows this for the beat between tap and first paint; matching
    --page means the app appears to fade in rather than flash white. Only
    the glyph's bounding box is actually computed -- the rest of the image
    is one repeated scanline.
    """
    key = f"splash-{width}x{height}"
    if key not in _cache:
        plain = bytes(PAGE) * width
        # Dim: a third of the way from background to green -- a mark, not
        # a light show. Rendered small, the size of the icon on screen.
        dim = tuple(round(PAGE[i] + (GREEN[i] - PAGE[i]) * 0.55)
                    for i in range(3))
        s = round(min(width, height) * 0.30)
        x0 = (width - s) // 2
        y0 = (height - s) // 2
        rows = []
        for y in range(height):
            if y < y0 or y >= y0 + s:
                rows.append(plain)
                continue
            row = bytearray(plain)
            for x in range(x0, x0 + s):
                cov = _glyph_coverage(x - x0 + 0.5, y - y0 + 0.5, s)
                if cov > 0.0:
                    row[x * 3:x * 3 + 3] = _blend(PAGE, dim, cov)
            rows.append(bytes(row))
        _cache[key] = _png(width, height, rows)
    return _cache[key]


def splash_sizes() -> set[tuple[int, int]]:
    """Physical-pixel whitelist for the splash route."""
    return {(w * r, h * r) for w, h, r in SPLASH_POINTS}


def manifest() -> dict:
    """The web app manifest. `scope: /` keeps every in-app link inside the
    standalone window; anything off-origin opens in the browser sheet."""
    return {
        "id": "/",
        "name": "avctl",
        "short_name": "avctl",
        "description": "The rack's remote: TV, amp, DAC and Music in one place.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": THEME_COLOR,
        "theme_color": THEME_COLOR,
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192",
             "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512",
             "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512",
             "type": "image/png", "purpose": "maskable"},
        ],
    }
