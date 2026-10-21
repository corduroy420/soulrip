"""Draw the background for the Mac installer window: a 'drag to install' heading and an arrow
from the app to the Applications folder. Writes build_assets/dmg-background.png and @2x.

The icon positions here must match ICON_POSITIONS in scripts/build.py.
"""
import math
import os

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "build_assets")

WIDTH, HEIGHT = 660, 400
APP_POS, APPLICATIONS_POS = (170, 205), (490, 205)   # icon centres (points)
ICON_SIZE = 128
TOP, BOTTOM = (34, 36, 46), (22, 23, 30)
ARROW = (255, 140, 66)           # the orange of the logo
TEXT, SUBTLE = (240, 240, 245), (150, 152, 165)

FONT_CANDIDATES = ["/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/Helvetica.ttc",
                   "/Library/Fonts/Arial.ttf", "C:/Windows/Fonts/segoeui.ttf"]


def font(size: int, bold: bool = False):
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                f = ImageFont.truetype(path, size, index=1 if bold and path.endswith(".ttc") else 0)
                if bold and path.endswith("SFNS.ttf"):
                    try:
                        f.set_variation_by_name("Bold")  # SF is a variable font
                    except Exception:
                        pass
                return f
            except Exception:
                continue
    return ImageFont.load_default(size=size)


def bezier(p0, p1, p2, steps=60):
    return [((1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t ** 2 * p2[0],
             (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t ** 2 * p2[1])
            for t in (i / steps for i in range(steps + 1))]


def draw(scale: int) -> Image.Image:
    s = scale
    img = Image.new("RGB", (WIDTH * s, HEIGHT * s))
    d = ImageDraw.Draw(img)
    for y in range(HEIGHT * s):  # vertical gradient
        t = y / (HEIGHT * s)
        d.line([(0, y), (WIDTH * s, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(TOP, BOTTOM)))

    # Soft glow behind each icon so they sit on something
    glow = Image.new("L", img.size, 0)
    for cx, cy in (APP_POS, APPLICATIONS_POS):
        r = 70 * s
        ImageDraw.Draw(glow).ellipse([cx * s - r, (cy - 8) * s - r, cx * s + r, (cy - 8) * s + r], fill=20)
    img.paste((255, 255, 255), (0, 0), glow.filter(ImageFilter.GaussianBlur(28 * s)))

    # Heading
    title = font(22 * s, bold=True)
    text = "Drag Soulrip into Applications"
    w = d.textlength(text, font=title)
    d.text(((WIDTH * s - w) / 2, 52 * s), text, font=title, fill=TEXT)

    # Curved arrow from the app towards the Applications folder
    start = ((APP_POS[0] + ICON_SIZE / 2 + 14) * s, (APP_POS[1] - 6) * s)
    end = ((APPLICATIONS_POS[0] - ICON_SIZE / 2 - 22) * s, (APPLICATIONS_POS[1] - 6) * s)
    control = ((start[0] + end[0]) / 2, start[1] - 46 * s)
    points = bezier(start, control, end)
    width = 7 * s
    d.line(points, fill=ARROW, width=width, joint="curve")
    for x, y in (points[0], points[-1]):  # round caps
        d.ellipse([x - width / 2, y - width / 2, x + width / 2, y + width / 2], fill=ARROW)
    (x1, y1), (x2, y2) = points[-4], points[-1]
    angle = math.atan2(y2 - y1, x2 - x1)
    head, spread = 24 * s, math.radians(28)
    tip = (x2 + math.cos(angle) * 10 * s, y2 + math.sin(angle) * 10 * s)
    d.polygon([tip,
               (tip[0] - head * math.cos(angle - spread), tip[1] - head * math.sin(angle - spread)),
               (tip[0] - head * math.cos(angle + spread), tip[1] - head * math.sin(angle + spread))],
              fill=ARROW)

    # First-launch tip
    small = font(12 * s)
    for i, line in enumerate(["First time opening it? If macOS says it can't be verified,",
                              "go to System Settings → Privacy & Security → Open Anyway."]):
        w = d.textlength(line, font=small)
        d.text(((WIDTH * s - w) / 2, (338 + i * 18) * s), line, font=small, fill=SUBTLE)
    return img


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    art = draw(4)  # drawn large, then scaled down, for smooth edges
    art.resize((WIDTH * 2, HEIGHT * 2), Image.LANCZOS).save(os.path.join(OUT_DIR, "dmg-background@2x.png"), dpi=(144, 144))
    art.resize((WIDTH, HEIGHT), Image.LANCZOS).save(os.path.join(OUT_DIR, "dmg-background.png"), dpi=(72, 72))
    print("DMG background written to", OUT_DIR)


if __name__ == "__main__":
    main()
