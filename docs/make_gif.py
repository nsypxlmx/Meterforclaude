#!/usr/bin/env python3
"""Draws docs/animations.gif: every icon shape playing every animation, as the
meter draws them for the top bar. Needs Pillow, PyGObject and GdkPixbuf.

    python3 docs/make_gif.py
"""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gi  # noqa: E402
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

import claude_usage_indicator as meter  # noqa: E402

ICON = 48               # px; the top bar is 16 (32 on HiDPI), larger reads better in a README
CELL_W, CELL_H = 96, 76
LEFT, TOP = 92, 34
BACKGROUND = (29, 29, 32)   # the top bar
TEXT, DIM = (235, 235, 235), (150, 150, 155)
FRAME_MS = 250          # the meter's own pace, PULSE_MS


def font(size):
    path = subprocess.run(["fc-match", "-f", "%{file}", "sans:bold"],
                          capture_output=True, text=True).stdout
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def render(svg_path, tmp):
    png = os.path.join(tmp, "frame.png")
    GdkPixbuf.Pixbuf.new_from_file_at_size(svg_path, ICON, ICON).savev(png, "png", [], [])
    return Image.open(png).convert("RGBA")


def main():
    shapes = meter.ICON_SHAPES
    animations = [(key, label) for key, label in meter.ANIMATIONS if key != "none"]
    width = LEFT + CELL_W * len(shapes)
    height = TOP + CELL_H * len(animations)
    heading, side = font(14), font(13)
    frames = []
    with tempfile.TemporaryDirectory() as tmp:
        meter.ICON_DIR = tmp
        for frame in range(meter.PULSE_FRAMES):
            image = Image.new("RGB", (width, height), BACKGROUND)
            draw = ImageDraw.Draw(image)
            for col, (_, label) in enumerate(shapes):
                draw.text((LEFT + col * CELL_W + CELL_W / 2, TOP / 2), label,
                          fill=TEXT, font=heading, anchor="mm")
            for row, (anim, label) in enumerate(animations):
                y = TOP + row * CELL_H
                draw.text((16, y + CELL_H / 2), label, fill=DIM, font=side, anchor="lm")
                for col, (shape, _) in enumerate(shapes):
                    icon = render(meter.icon_path(0.62, meter.COLORS["ok"], (0.35, "#6F8396"),
                                                  (anim, frame), shape), tmp)
                    image.paste(icon, (LEFT + col * CELL_W + (CELL_W - ICON) // 2,
                                       y + (CELL_H - ICON) // 2), icon)
            frames.append(image)
    out = os.path.join(ROOT, "docs", "animations.gif")
    frames[0].save(out, save_all=True, append_images=frames[1:], duration=FRAME_MS,
                   loop=0, optimize=True)
    print(out)


if __name__ == "__main__":
    main()
