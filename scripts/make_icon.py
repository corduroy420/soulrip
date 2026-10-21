"""Turn assets/logo.png into the app icons (build_assets/icon.png, .icns, .ico).

assets/logo.png is pixel art (a 100×100 Pixilart export with a transparent background). It's enlarged by
a whole-number factor with nearest-neighbour scaling, so every art pixel becomes an exact square block
with no blur, then centred on a white rounded square. Smaller icon sizes are made by averaging whole
blocks (box filter), and the smallest ones get slightly thicker strokes so the 1-pixel lines stay visible
in the Dock, taskbar and title bar. assets/icon.png (shown in the README) is a 256 px copy of the result.
"""
import os

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGO = os.path.join(ROOT, "assets", "logo.png")
OUT_DIR = os.path.join(ROOT, "build_assets")
SIZE = 1024
MARGIN = 60          # transparent edge around the rounded square (macOS icon grid)
PADDING = 120        # space between the square's edge and the drawing
SMALL_SIZES = [16, 32, 48, 64, 128, 256]


def trimmed_logo() -> Image.Image:
    logo = Image.open(LOGO).convert("RGBA")
    box = logo.getchannel("A").point(lambda p: 255 if p > 16 else 0).getbbox()
    return logo.crop(box) if box else logo


def compose(logo: Image.Image, thicken: int = 0) -> Image.Image:
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle([MARGIN, MARGIN, SIZE - MARGIN, SIZE - MARGIN], radius=200, fill=255)
    canvas.paste(Image.new("RGBA", (SIZE, SIZE), (255, 255, 255, 255)), (0, 0), mask)
    ImageDraw.Draw(canvas).rounded_rectangle([MARGIN, MARGIN, SIZE - MARGIN, SIZE - MARGIN], radius=200,
                                             outline=(0, 0, 0, 40), width=4)  # faint edge on white backgrounds
    room = SIZE - 2 * (MARGIN + PADDING)
    scale = max(1, room // max(logo.size))        # whole number: each art pixel → an exact scale×scale block
    art = logo.resize((logo.width * scale, logo.height * scale), Image.NEAREST)
    if thicken:  # stamp the drawing in a small circle of offsets: thicker strokes, same colours
        r = thicken
        bold = Image.new("RGBA", (art.width + 2 * r, art.height + 2 * r), (0, 0, 0, 0))
        for dx in range(-r, r + 1, 2):
            for dy in range(-r, r + 1, 2):
                if dx * dx + dy * dy <= r * r:
                    bold.alpha_composite(art, (r + dx, r + dy))
        art = bold
    canvas.alpha_composite(art, ((SIZE - art.width) // 2, (SIZE - art.height) // 2))
    return canvas


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    logo = trimmed_logo()
    full = compose(logo)
    bold = compose(logo, thicken=10)  # used for the small sizes only
    small = [(bold if s <= 64 else full).resize((s, s), Image.BOX) for s in SMALL_SIZES]
    full.save(os.path.join(OUT_DIR, "icon.png"))
    full.save(os.path.join(OUT_DIR, "icon.icns"), append_images=small)
    small[-1].save(os.path.join(OUT_DIR, "icon.ico"), sizes=[(s, s) for s in SMALL_SIZES], append_images=small[:-1])
    for s in (32, 128):
        (bold if s <= 64 else full).resize((s, s), Image.BOX).save(os.path.join(OUT_DIR, f"preview-{s}.png"))
    print("Icons written to", OUT_DIR)


if __name__ == "__main__":
    main()
