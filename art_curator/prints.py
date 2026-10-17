"""The printed squares: each artwork fitted into 10 x 10 cm, centered, white padding, never stretched.

find_art.py matches the camera against these same pictures, so print from here (not from another copy
of the painting) and the matching works best.

  python -m art_curator.prints                       art_curator/runtime/prints.pdf: 2 per page, letter
  python -m art_curator.prints --out ~/Desktop/art_prints.pdf

Print at 100% / "actual size" (not "fit to page"). Each page has a 10 cm ruler line to check.
"""
import argparse
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).parent
IMAGES = HERE / "images"
PRINT_MM = 100.0


def print_image(art_id: int, px_per_mm: float) -> np.ndarray:
    """The 10 x 10 cm print as a BGR image at px_per_mm: the artwork fitted inside, white around it."""
    side = int(round(PRINT_MM * px_per_mm))
    art = cv2.imread(str(IMAGES / f"{art_id}.jpg"))
    if art is None:
        raise FileNotFoundError(IMAGES / f"{art_id}.jpg")
    scale = side / max(art.shape[:2])
    w, h = max(1, round(art.shape[1] * scale)), max(1, round(art.shape[0] * scale))
    art = cv2.resize(art, (w, h), interpolation=cv2.INTER_AREA)
    canvas = np.full((side, side, 3), 255, np.uint8)
    y, x = (side - h) // 2, (side - w) // 2
    canvas[y:y + h, x:x + w] = art
    return canvas


def build_pdf(out: Path, dpi: int = 300) -> None:
    from PIL import Image, ImageDraw, ImageFont
    from art_curator.curator import load_catalog
    catalog = load_catalog()
    px_mm = dpi / 25.4
    font = ImageFont.load_default(size=round(3.5 * px_mm))  # ~3.5 mm tall text
    page_w, page_h = round(215.9 * px_mm), round(279.4 * px_mm)  # US letter
    pages = []
    ids = sorted(catalog)
    for first in range(0, len(ids), 2):
        page = Image.new("RGB", (page_w, page_h), "white")
        draw = ImageDraw.Draw(page)
        for slot, art_id in enumerate(ids[first:first + 2]):
            sq = cv2.cvtColor(print_image(art_id, px_mm), cv2.COLOR_BGR2RGB)
            x0 = (page_w - sq.shape[1]) // 2
            y0 = round((25 + slot * 125) * px_mm)
            page.paste(Image.fromarray(sq), (x0, y0))
            # thin grey cut line around the 10 cm square, and the label outside it
            draw.rectangle([x0 - 1, y0 - 1, x0 + sq.shape[1], y0 + sq.shape[0]], outline=(170, 170, 170), width=2)
            a = catalog[art_id]
            draw.text((x0, y0 + sq.shape[0] + round(3 * px_mm)),
                      f"S{art_id}  {a['title']} - {a['artist']}   (cut on the grey line)", fill="black", font=font)
        ruler_y = page_h - round(12 * px_mm)
        x0 = (page_w - round(100 * px_mm)) // 2
        draw.line([x0, ruler_y, x0 + round(100 * px_mm), ruler_y], fill="black", width=3)
        draw.text((x0, ruler_y + 10), "this line must measure exactly 10 cm (print at 100%)", fill="black", font=font)
        pages.append(page)
    out.parent.mkdir(parents=True, exist_ok=True)
    pages[0].save(out, save_all=True, append_images=pages[1:], resolution=dpi)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", type=Path, default=HERE / "runtime" / "prints.pdf")
    args = p.parse_args()
    build_pdf(args.out.expanduser())
    print(args.out.expanduser())


if __name__ == "__main__":
    main()
