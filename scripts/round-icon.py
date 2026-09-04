#!/usr/bin/env python3
"""Mask square artwork into the macOS Big Sur app-icon grid (Commission #4).

Modern macOS icons are not full-bleed squares: the Dock expects a 1024x1024
canvas whose visible tile is a centred 824x824 rounded rectangle (corner
radius ~185) with TRANSPARENT margins and corners. Full-bleed opaque artwork
renders as a hard-edged square that ignores the system icon shape.

    python3 scripts/round-icon.py <in.png> <out.png> \
        [--size 1024] [--content 824] [--radius 185]

Pure Pillow; corners are supersampled 4x for clean anti-aliasing.
"""
import argparse
import sys

try:
    from PIL import Image, ImageDraw
except ImportError:
    sys.exit("round-icon: Pillow is required (python3 -m pip install pillow)")


def round_into_grid(src_path, out_path, size=1024, content=824, radius=185):
    art = Image.open(src_path).convert("RGBA")
    art = art.resize((content, content), Image.LANCZOS)

    ss = 4                                   # supersample for smooth corners
    mask = Image.new("L", (content * ss, content * ss), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, content * ss - 1, content * ss - 1),
        radius=radius * ss, fill=255)
    mask = mask.resize((content, content), Image.LANCZOS)

    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    off = (size - content) // 2
    canvas.paste(art, (off, off), mask)
    canvas.save(out_path, "PNG")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--size", type=int, default=1024)
    ap.add_argument("--content", type=int, default=824)
    ap.add_argument("--radius", type=int, default=185)
    a = ap.parse_args()
    round_into_grid(a.src, a.out, a.size, a.content, a.radius)
    print(f"rounded {a.src} -> {a.out} "
          f"({a.size}px canvas, {a.content}px tile, r={a.radius})")


if __name__ == "__main__":
    main()
