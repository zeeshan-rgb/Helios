"""Generate the Helios app icon (helios/ui/helios.ico + helios.png).

The mark is Helios's signature in icon form: a glowing cyan arc-reactor — concentric rings with a
bright core and a soft bloom — on a dark "quiet-luxe" tile, echoing the orb and the dashboard's
cyan accent. Rendered with numpy (gradient) + Pillow (shapes, Gaussian-blur glow) at high
resolution and downscaled, so it stays crisp from 256px down to 16px.

Reproducible: run `python tools/make_icon.py` to regenerate after a tweak.
"""

from __future__ import annotations

import os

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "helios", "ui")
M = 2048                       # master render size (downscaled to each icon size)

# Brand palette (matches the dashboard tokens / orb).
BG_IN = (18, 30, 44)           # tile centre (cool dark navy)
BG_OUT = (7, 9, 13)            # tile edge (near-black)
CYAN = (77, 208, 225)          # --accent
CYAN_HI = (150, 240, 255)      # brighter highlight
CYAN_DIM = (42, 111, 120)      # --accent-dim


def _radial_bg(size: int) -> Image.Image:
    """Dark tile with a soft radial gradient from BG_IN (centre) to BG_OUT (corners)."""
    y, x = np.mgrid[0:size, 0:size].astype("float32")
    cx = cy = (size - 1) / 2
    r = np.sqrt((x - cx) ** 2 + (y - cy) ** 2) / (size * 0.62)
    r = np.clip(r, 0, 1)
    t = (r ** 1.25)[..., None]
    a = np.array(BG_IN, dtype="float32")
    b = np.array(BG_OUT, dtype="float32")
    rgb = (a * (1 - t) + b * t).astype("uint8")
    alpha = np.full((size, size, 1), 255, dtype="uint8")
    return Image.fromarray(np.concatenate([rgb, alpha], axis=2), "RGBA")


def _rounded_mask(size: int, radius_frac: float = 0.235) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    rad = int(size * radius_frac)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=rad, fill=255)
    return m


def _ring(size, cx, cy, radius, width, color, alpha=255):
    """A stroked circle on its own RGBA layer (so it can be blurred for glow)."""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    bb = [cx - radius, cy - radius, cx + radius, cy + radius]
    d.ellipse(bb, outline=color + (alpha,), width=int(width))
    return layer


def _disc(size, cx, cy, radius, color, alpha=255):
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=color + (alpha,))
    return layer


def render_master() -> Image.Image:
    size = M
    base = _radial_bg(size)
    c = (size - 1) / 2

    # Hairline inner border (a faint "screen" edge), like the dashboard chrome.
    border = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    bd = ImageDraw.Draw(border)
    inset = int(size * 0.055)
    bd.rounded_rectangle([inset, inset, size - 1 - inset, size - 1 - inset],
                         radius=int(size * 0.19), outline=CYAN_DIM + (90,), width=max(2, size // 360))
    base = Image.alpha_composite(base, border)

    # ---- glow layer (everything bright, blurred for bloom) ----
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    glow = Image.alpha_composite(glow, _ring(size, c, c, size * 0.255, size * 0.052, CYAN, 255))
    glow = Image.alpha_composite(glow, _disc(size, c, c, size * 0.095, CYAN_HI, 255))
    glow = glow.filter(ImageFilter.GaussianBlur(size * 0.045))
    base = Image.alpha_composite(base, glow)
    base = Image.alpha_composite(base, glow)  # twice = stronger bloom

    # ---- crisp marks on top ----
    # Faint outer ring with tick marks (HUD detail; kept subtle so small sizes stay clean — the
    # ring + core carry the mark, this is just texture that emerges at larger sizes).
    base = Image.alpha_composite(base, _ring(size, c, c, size * 0.375, size * 0.009, CYAN, 70))
    ticks = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    td = ImageDraw.Draw(ticks)
    import math
    for k in range(12):
        ang = math.radians(k * 30)
        r0, r1 = size * 0.360, size * 0.390
        x0, y0 = c + r0 * math.cos(ang), c + r0 * math.sin(ang)
        x1, y1 = c + r1 * math.cos(ang), c + r1 * math.sin(ang)
        td.line([x0, y0, x1, y1], fill=CYAN + (95,), width=max(2, size // 340))
    base = Image.alpha_composite(base, ticks)

    # Main ring (bright) + a thin highlight ring just inside it.
    base = Image.alpha_composite(base, _ring(size, c, c, size * 0.255, size * 0.050, CYAN, 255))
    base = Image.alpha_composite(base, _ring(size, c, c, size * 0.222, size * 0.012, CYAN_HI, 200))

    # Core: bright disc + a soft 4-point glint.
    base = Image.alpha_composite(base, _disc(size, c, c, size * 0.092, CYAN_HI, 255))
    base = Image.alpha_composite(base, _disc(size, c, c, size * 0.060, (240, 253, 255), 255))
    glint = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glint)
    gl = size * 0.16
    gd.line([c - gl, c, c + gl, c], fill=(255, 255, 255, 120), width=max(2, size // 380))
    gd.line([c, c - gl, c, c + gl], fill=(255, 255, 255, 120), width=max(2, size // 380))
    glint = glint.filter(ImageFilter.GaussianBlur(size * 0.004))
    base = Image.alpha_composite(base, glint)

    # Clip to the rounded tile.
    base.putalpha(_rounded_mask(size))
    return base


def main():
    master = render_master()
    os.makedirs(OUT_DIR, exist_ok=True)
    png = master.resize((256, 256), Image.LANCZOS)
    png.save(os.path.join(OUT_DIR, "helios.png"))
    sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)]
    master.save(os.path.join(OUT_DIR, "helios.ico"), format="ICO", sizes=sizes)
    print("wrote", os.path.join(OUT_DIR, "helios.ico"), "and helios.png")


if __name__ == "__main__":
    main()
