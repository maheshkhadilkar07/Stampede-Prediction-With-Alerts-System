"""
scripts/generate_officer_icons.py
===================================
Generates the PWA icon set for the officer app.

Kept as a script rather than committing only the PNGs so the icons can be
regenerated if the palette changes, and so it is obvious how they were made.

Run from the project root:
    python scripts/generate_officer_icons.py

Needs Pillow (already in requirements.txt for the report generator).
"""

import os
import sys

from PIL import Image, ImageDraw

# Palette lifted from static/css/officer.css so the home-screen icon and the
# app it opens are visibly the same thing.
INK = (7, 12, 22, 255)        # --ink
TEAL = (45, 212, 191, 255)    # --go
RED = (255, 59, 48, 255)      # --critical

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICON_DIR = os.path.join(PROJECT_ROOT, "static", "icons")

# Drawn at 8x and downsampled with LANCZOS. PIL has no antialiased drawing
# primitives, so supersampling is the only way to get clean curves.
SUPERSAMPLE = 8


def draw_icon(size, content_scale, maskable):
    """
    A location pin inside a pulse ring: 'someone is needed here'.

    content_scale shrinks the artwork for maskable icons. Android may crop a
    maskable icon to a circle, a squircle or a rounded square at its own
    discretion, and anything outside the central 80% can be cut. Drawing the
    pin at 55% of the canvas keeps it whole under every mask.
    """
    canvas = size * SUPERSAMPLE
    image = Image.new("RGBA", (canvas, canvas), INK if maskable else (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    if not maskable:
        # Rounded-square plate for the 'any' purpose, matching the app's
        # 20px card radius scaled to icon size.
        radius = int(canvas * 0.22)
        draw.rounded_rectangle([0, 0, canvas - 1, canvas - 1], radius=radius, fill=INK)

    centre = canvas / 2.0
    unit = canvas * content_scale  # the pin's overall height

    # --- pulse ring: the alert half of the idea ---------------------------
    ring_r = unit * 0.60
    ring_w = max(1, int(unit * 0.055))
    draw.ellipse(
        [centre - ring_r, centre - ring_r, centre + ring_r, centre + ring_r],
        outline=RED, width=ring_w,
    )

    # --- pin head --------------------------------------------------------
    head_r = unit * 0.26
    head_cy = centre - unit * 0.10
    draw.ellipse(
        [centre - head_r, head_cy - head_r, centre + head_r, head_cy + head_r],
        fill=TEAL,
    )

    # --- pin tail: a triangle whose top edge is hidden inside the head ----
    tail_half = head_r * 0.72
    tip_y = head_cy + unit * 0.46
    draw.polygon(
        [
            (centre - tail_half, head_cy + head_r * 0.52),
            (centre + tail_half, head_cy + head_r * 0.52),
            (centre, tip_y),
        ],
        fill=TEAL,
    )

    # --- hole in the pin head, punched to the plate colour ---------------
    hole_r = head_r * 0.40
    draw.ellipse(
        [centre - hole_r, head_cy - hole_r, centre + hole_r, head_cy + hole_r],
        fill=INK,
    )

    return image.resize((size, size), Image.LANCZOS)


def main():
    os.makedirs(ICON_DIR, exist_ok=True)

    targets = [
        ("officer-96.png", 96, 0.62, False),
        ("officer-192.png", 192, 0.62, False),
        ("officer-512.png", 512, 0.62, False),
        ("officer-maskable-192.png", 192, 0.45, True),
        ("officer-maskable-512.png", 512, 0.45, True),
    ]

    for filename, size, scale, maskable in targets:
        path = os.path.join(ICON_DIR, filename)
        draw_icon(size, scale, maskable).save(path, "PNG", optimize=True)
        print(f"  wrote {filename} ({size}x{size})")

    # Browsers still request /favicon.ico from the root of a scope.
    draw_icon(64, 0.62, False).save(
        os.path.join(ICON_DIR, "officer-favicon.png"), "PNG", optimize=True,
    )
    print("  wrote officer-favicon.png (64x64)")
    print(f"\nIcons written to {ICON_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
