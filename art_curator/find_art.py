"""find_art(): where each artwork tile really is, near its slot (camera slot mode). Like find_box, for tiles.

  1. look straight down at the slots (zones.look_down_at); more photos if one doesn't show every region
  2. turn each photo into a top-down picture of the table at 1 px/mm (the camera pose is known), so a tile
     always looks the same size
  3. in each slot's region (layout spot +/- region_mm), search for that slot's artwork (the same picture as
     prints.py prints) at 12 rotations: the best fit of its color LAYOUT is the tile's center
  4. score = layout match x PALETTE match (hue/saturation histogram) there. The palette is what tells an
     empty slot apart: the green sheet's edge against the table can mimic a painting's layout (The
     Scream: light top, dark bottom) but not its colors
  5. also score the other 7 artworks: if one clearly wins, the wrong tile is in that slot

Raises NotReadyError (nothing has moved) if a slot's tile isn't found or is the wrong artwork.
Saves runtime/find_art_<n>.jpg: the top-down view with every match drawn.

  python -m art_curator.find_art              MOVES THE ARM (camera only, no picking): find all 8, print offsets
  python -m art_curator.find_art --image runtime/find_art_photo_0.jpg --view runtime/find_art_photo_0.json
                                              offline, from a photo the robot run saved
"""
import argparse
import asyncio
import json
from functools import lru_cache

import cv2
import numpy as np

from art_curator.curator import load_catalog
from art_curator.exhibition import RUNTIME, NotReadyError, load_layout
from art_curator.prints import PRINT_MM, print_image
from detect import TABLE_TOP_Z

PX_PER_MM = 1.0           # top-down picture scale; the camera sees ~1.7 px/mm from ~0.5 m, so little is lost
TILE_TOP_Z = TABLE_TOP_Z + 2.0
MATCH_MM = 80.0           # match the central 80 mm of the 100 mm print: stays inside the print at +/-10 deg
ANGLES = [quarter + tilt for quarter in (0, 90, 180, 270) for tilt in (-10, 0, 10)]
# Synthetic photos (test_find_art): real tiles score >= ~0.48, empty slots <= ~0.07; on a real tile the
# right artwork beats every other by >= ~0.3. Re-check on real photos (find_art prints every score).
MIN_SCORE = 0.25          # layout x palette below this: that artwork isn't in the region
ID_MARGIN = 0.10          # another artwork scoring this much more (and >= MIN_SCORE) = wrong tile
SEARCH_EXTRA_MM = 40.0    # look this far beyond the region, so a tile just outside it is measured (and
                          # refused as too far), not half-matched at the region's edge
MAX_PHOTOS = 5


# ---------- geometry ----------

def world_to_pixel(xs: np.ndarray, ys: np.ndarray, z: float, view: dict) -> tuple:
    """Camera pixel (u, v) of world points (xs, ys, z). Inverse of detect.pixel_to_table."""
    k, axes = view["camera_intrinsics"], view["camera_axes"]
    rot = np.column_stack([axes["x"], axes["y"], axes["z"]])  # camera -> world
    pts = np.stack([xs - axes["origin"][0], ys - axes["origin"][1], np.full_like(xs, z - axes["origin"][2])], -1)
    cam = pts @ rot  # = (rot.T @ p) for every point
    with np.errstate(divide="ignore", invalid="ignore"):
        u = k["fx"] * cam[..., 0] / cam[..., 2] + k["cx"]
        v = k["fy"] * cam[..., 1] / cam[..., 2] + k["cy"]
    behind = cam[..., 2] <= 0
    return np.where(behind, -1, u), np.where(behind, -1, v)


class TopDown:
    """The table seen from straight above: column = x (mm), row = y (mm, +y up), 1 px = 1/PX_PER_MM mm."""

    def __init__(self, bgr: np.ndarray, view: dict, x0: float, y0: float, x1: float, y1: float):
        self.x0, self.y1 = x0, y1
        cols, rows = int((x1 - x0) * PX_PER_MM), int((y1 - y0) * PX_PER_MM)
        xs, ys = np.meshgrid(x0 + (np.arange(cols) + 0.5) / PX_PER_MM, y1 - (np.arange(rows) + 0.5) / PX_PER_MM)
        u, v = world_to_pixel(xs, ys, TILE_TOP_Z, view)
        h, w = bgr.shape[:2]
        self.valid = (u >= 0) & (u < w - 1) & (v >= 0) & (v < h - 1)
        self.img = cv2.remap(bgr, u.astype(np.float32), v.astype(np.float32), cv2.INTER_LINEAR,
                             borderMode=cv2.BORDER_CONSTANT, borderValue=0)

    def px(self, x: float, y: float) -> tuple:
        return int(round((x - self.x0) * PX_PER_MM)), int(round((self.y1 - y) * PX_PER_MM))

    def world(self, c: float, r: float) -> tuple:
        return self.x0 + (c + 0.5) / PX_PER_MM, self.y1 - (r + 0.5) / PX_PER_MM

    def crop(self, x: float, y: float, half_mm: float):
        """(image, top-left col, row) of the square around (x, y), or None unless the camera saw all of it."""
        c, r = self.px(x, y)
        h = int(round(half_mm * PX_PER_MM))
        if c - h < 0 or r - h < 0 or c + h > self.img.shape[1] or r + h > self.img.shape[0]:
            return None
        if not self.valid[r - h:r + h, c - h:c + h].all():
            return None
        return self.img[r - h:r + h, c - h:c + h], c - h, r - h


# ---------- matching ----------

def _soften(img: np.ndarray) -> np.ndarray:
    """Blur a little: prints and the camera never match pixel for pixel."""
    return cv2.GaussianBlur(img, (5, 5), 0)


def _palette(img: np.ndarray) -> np.ndarray:
    """Hue x saturation histogram: which colors, regardless of where or how bright."""
    hist = cv2.calcHist([cv2.cvtColor(img, cv2.COLOR_BGR2HSV)], [0, 1], None, [30, 16], [0, 180, 0, 256])
    return cv2.normalize(hist, hist).flatten()


@lru_cache(maxsize=None)
def templates(art_id: int) -> tuple:
    """(angle, image, palette) of the central MATCH_MM of the print rotated by angle, for every angle."""
    full = print_image(art_id, PX_PER_MM)
    side = full.shape[0]
    keep = int(round(MATCH_MM * PX_PER_MM))
    out = []
    for angle in ANGLES:
        m = cv2.getRotationMatrix2D((side / 2, side / 2), angle, 1.0)
        rot = cv2.warpAffine(full, m, (side, side), borderValue=(255, 255, 255))
        a = (side - keep) // 2
        part = _soften(rot[a:a + keep, a:a + keep])
        out.append((angle, part, _palette(part)))
    return tuple(out)


def best_match(region: np.ndarray, art_id: int) -> tuple:
    """(score, col, row of the template center within region, angle) of art_id's best fit.
    Position and angle from the color layout; score = layout x palette there (0..1)."""
    best = (-1.0, 0, 0, 0, None)
    for angle, tmpl, pal in templates(art_id):
        _, layout, _, (c, r) = cv2.minMaxLoc(cv2.matchTemplate(region, tmpl, cv2.TM_CCOEFF_NORMED))
        if layout > best[0]:
            best = (layout, c, r, angle, pal)
    layout, c, r, angle, pal = best
    k = int(round(MATCH_MM * PX_PER_MM))
    palette = cv2.compareHist(_palette(region[r:r + k, c:c + k]), pal, cv2.HISTCMP_CORREL)
    return max(0.0, layout) * max(0.0, palette), c + k / 2, r + k / 2, angle


def find_in_photos(photos: list, spots: dict, region_mm: float, expected: dict) -> dict:
    """photos: [(bgr, view)]; spots: {slot: (x, y)}; expected: {slot: art_id}.
    Returns {slot: {"xy", "off_mm", "score", "angle", "photo", "looks_like", "other_score"}} for every slot
    whose whole search area some photo saw; the caller decides what's missing / wrong / too far."""
    half = region_mm + MATCH_MM / 2           # where a tile counts as "in its slot"
    wide = half + SEARCH_EXTRA_MM             # where we look for it
    xs = [p[0] for p in spots.values()]
    ys = [p[1] for p in spots.values()]
    pad = wide + 10
    results = {}
    for n, (bgr, view) in enumerate(photos):
        td = TopDown(bgr, view, min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)
        shown = td.img.copy()
        for slot, (x, y) in spots.items():
            got, near = td.crop(x, y, wide), td.crop(x, y, half)
            if got is None or near is None:
                continue
            region, c0, r0 = got
            score, c, r, angle = best_match(_soften(region), expected[slot])
            # wrong-tile check only in the slot's own region: the wider view may show a neighbour's tile
            near_region = _soften(near[0])
            others = {a: best_match(near_region, a)[0] for a in expected.values() if a != expected[slot]}
            rival = max(others, key=others.get)
            if slot in results and results[slot]["score"] >= score:
                continue
            wx, wy = td.world(c0 + c, r0 + r)
            # the region is a square: +/- region_mm in x and in y
            results[slot] = {"xy": (wx, wy), "off_mm": round(float(max(abs(wx - x), abs(wy - y))), 1),
                             "score": round(score, 3), "angle": angle, "photo": n,
                             # another artwork only counts if it's itself a real match (not empty-slot noise)
                             "looks_like": rival if others[rival] >= max(MIN_SCORE, score + ID_MARGIN) else expected[slot],
                             "other_score": round(others[rival], 3)}
            ok = (score >= MIN_SCORE and results[slot]["looks_like"] == expected[slot]
                  and results[slot]["off_mm"] <= region_mm)
            color = (0, 200, 0) if ok else (0, 0, 255)
            cv2.rectangle(shown, td.px(x - region_mm - 55, y + region_mm + 55),
                          td.px(x + region_mm + 55, y - region_mm - 55), (200, 200, 200), 1)
            cv2.circle(shown, td.px(wx, wy), 4, color, -1)
            box = cv2.boxPoints(((c0 + c, r0 + r), (110 * PX_PER_MM, 110 * PX_PER_MM), -angle)).astype(np.int32)
            cv2.drawContours(shown, [box], -1, color, 2)
            cv2.putText(shown, f"{slot} {score:.2f}", (int(c0 + c) - 40, int(r0 + r) - 60),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)
        RUNTIME.mkdir(exist_ok=True)
        cv2.imwrite(str(RUNTIME / f"find_art_{n}.jpg"), shown)
    return results


def judge(results: dict, spots: dict, expected: dict, region_mm: float) -> dict:
    """{slot: (x, y)} if every slot has its own artwork; else NotReadyError naming each problem."""
    catalog = load_catalog()
    problems = []
    for slot in spots:
        r = results.get(slot)
        if r is None:
            problems.append(f"{slot}: the camera couldn't see the whole slot")
        elif r["looks_like"] != expected[slot]:  # before the score check: a wrong tile also scores low
            problems.append(f"{slot}: looks like {catalog[r['looks_like']]['title']}, "
                            f"not {catalog[expected[slot]]['title']}")
        elif r["score"] < MIN_SCORE:
            problems.append(f"{slot}: no {catalog[expected[slot]]['title']} found (match {r['score']:.2f})")
        elif r["off_mm"] > region_mm:
            problems.append(f"{slot}: {catalog[expected[slot]]['title']} is {r['off_mm']:.0f} mm from its spot "
                            f"(max {region_mm:.0f}): push it back")
    if problems:
        raise NotReadyError("; ".join(problems))
    return {slot: results[slot]["xy"] for slot in spots}


def expected_art(spots: dict) -> dict:
    by_slot = {a["pickup_position"]: i for i, a in load_catalog().items()}
    return {slot: by_slot[slot] for slot in spots}


# ---------- robot ----------

def covered(photos: list, spots: dict, region_mm: float) -> set:
    """Slots whose whole search region is inside at least one photo."""
    half = region_mm + MATCH_MM / 2 + SEARCH_EXTRA_MM
    seen = set()
    for bgr, view in photos:
        h, w = bgr.shape[:2]
        for slot, (x, y) in spots.items():
            cx = np.array([x - half, x + half, x - half, x + half], float)
            cy = np.array([y - half, y - half, y + half, y + half], float)
            u, v = world_to_pixel(cx, cy, TILE_TOP_Z, view)
            if ((u >= 0) & (u < w - 1) & (v >= 0) & (v < h - 1)).all():
                seen.add(slot)
    return seen


async def take_photos(machine, spots: dict, region_mm: float) -> list:
    """Straight-down photos until every slot's region is in one of them (or MAX_PHOTOS)."""
    from zones import look_down_at
    photos = []
    todo = dict(spots)
    while todo and len(photos) < MAX_PHOTOS:
        first = next(iter(todo.values()))  # aim at the first uncovered slot and its close neighbours
        near = [p for p in todo.values() if np.hypot(p[0] - first[0], p[1] - first[1]) < 200]
        x, y = np.mean([p[0] for p in near]), np.mean([p[1] for p in near])
        if not photos:  # first try: the middle of everything
            x, y = np.mean([p[0] for p in spots.values()]), np.mean([p[1] for p in spots.values()])
        print(f"[find_art] photo looking down at x={x:.0f} y={y:.0f}")
        shot = await look_down_at(machine, float(x), float(y))
        if shot is None:
            print("[find_art]   can't get the camera above that point")
            todo.pop(next(iter(todo)))  # give up on that slot; judge() will name it
            continue
        photos.append(shot)
        RUNTIME.mkdir(exist_ok=True)  # raw photo + camera pose: re-run offline with --image/--view
        cv2.imwrite(str(RUNTIME / f"find_art_photo_{len(photos) - 1}.jpg"), shot[0])
        (RUNTIME / f"find_art_photo_{len(photos) - 1}.json").write_text(json.dumps(shot[1], indent=1))
        seen = covered(photos, spots, region_mm)
        todo = {s: p for s, p in spots.items() if s not in seen}
    return photos


async def find_art(machine, spots: dict, region_mm: float) -> dict:
    """{slot: (x, y) of the tile's center} for every slot in spots. Raises NotReadyError otherwise."""
    expected = expected_art(spots)
    photos = await take_photos(machine, spots, region_mm)
    results = find_in_photos(photos, spots, region_mm, expected)
    for slot, r in sorted(results.items()):
        x, y = spots[slot]
        print(f"[find_art]   {slot}: match {r['score']:.2f} at x={r['xy'][0]:.0f} y={r['xy'][1]:.0f} "
              f"({r['xy'][0] - x:+.0f}, {r['xy'][1] - y:+.0f} mm), turned {r['angle']} deg"
              f"{'' if r['looks_like'] == expected[slot] else '  WRONG TILE?'}")
    return judge(results, spots, expected, region_mm)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--image", help="offline: a saved photo")
    p.add_argument("--view", help="offline: the camera pose saved with it (poses.capture_current_pose format)")
    args = p.parse_args()
    layout = load_layout()
    spots = {s: (p["x"], p["y"]) for s, p in layout["slots"].items()}
    region = layout.get("region_mm", 30.0)
    try:
        if args.image:
            photos = [(cv2.imread(args.image), json.loads(open(args.view).read()))]
            found = judge(find_in_photos(photos, spots, region, expected_art(spots)), spots, expected_art(spots), region)
        else:
            from inspection import go_to_overview
            from robot import connect
            async def go():
                async with await connect() as machine:
                    try:
                        return await find_art(machine, spots, region)
                    finally:
                        await go_to_overview(machine)
            found = asyncio.run(go())
    except NotReadyError as e:
        raise SystemExit(f"[find_art] NOT READY: {e}\n  pictures: {RUNTIME}/find_art_*.jpg")
    print(f"[find_art] all {len(found)} tiles found. pictures: {RUNTIME}/find_art_*.jpg")


if __name__ == "__main__":
    main()
