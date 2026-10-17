"""find_art on synthetic photos: tiles drawn at known spots, seen through a fake straight-down camera with
the real camera's intrinsics, then blurred, dimmed, noised and JPEG-compressed. No robot.

  python -m art_curator.test_find_art
Real photos will differ (lighting, glare on the tape, print colors): re-tune MIN_SCORE on them.
"""
import asyncio
import random
import time

import cv2
import numpy as np

import zones
from art_curator import find_art as fa
from art_curator.exhibition import NotReadyError, load_layout
from art_curator.prints import print_image
from detect import pixel_to_table
from poses import get_pose

CANVAS_MM = (-250.0, 200.0, 500.0, 750.0)  # x0, y0, x1, y1 of the drawn table
CANVAS_PX_MM = 2.0
CAMERA_Z = 500.0


def draw_table(tiles: dict) -> np.ndarray:
    """Top-down table (same orientation as find_art.TopDown): beige table, green sheet, tiles.
    tiles: {art_id: (x, y, angle)}."""
    x0, y0, x1, y1 = CANVAS_MM
    w, h = int((x1 - x0) * CANVAS_PX_MM), int((y1 - y0) * CANVAS_PX_MM)
    rng = np.random.default_rng(1)
    img = (np.full((h, w, 3), (170, 185, 200), np.float32) + rng.normal(0, 4, (h, w, 3))).clip(0, 255).astype(np.uint8)
    to_px = lambda x, y: ((x - x0) * CANVAS_PX_MM, (y1 - y) * CANVAS_PX_MM)
    green = np.array([to_px(x, y) for x, y in get_pose("green_zone")["corners_world"]], np.int32)
    cv2.fillPoly(img, [green], (80, 165, 85))
    for art_id, (x, y, angle) in tiles.items():
        c = to_px(x, y)
        back = cv2.boxPoints((c, (110 * CANVAS_PX_MM, 110 * CANVAS_PX_MM), -angle)).astype(np.int32)
        cv2.fillPoly(img, [back], (215, 215, 215))  # the plastic back
        pr = print_image(art_id, CANVAS_PX_MM)
        s = pr.shape[0]
        m = cv2.getRotationMatrix2D((s / 2, s / 2), angle, 1.0)
        m[:, 2] += (c[0] - s / 2, c[1] - s / 2)
        warped = cv2.warpAffine(pr, m, (w, h))
        mask = cv2.warpAffine(np.full((s, s), 255, np.uint8), m, (w, h)) > 127
        img[mask] = warped[mask]
    return img


def camera_view(x: float, y: float) -> dict:
    """A straight-down camera at (x, y, CAMERA_Z) with the real camera's intrinsics and roll."""
    view = get_pose("overview-blue")
    axes = dict(view["camera_axes"], origin=[x, y, CAMERA_Z])
    return {"camera_intrinsics": view["camera_intrinsics"], "camera_axes": axes}


def photograph(table: np.ndarray, view: dict, seed: int) -> np.ndarray:
    k = view["camera_intrinsics"]
    w, h = k["width"], k["height"]
    x0, _, _, y1 = CANVAS_MM
    # per pixel: where its ray hits the tile plane, then that spot of the drawn table
    us, vs = np.meshgrid(np.arange(w), np.arange(h))
    xs, ys = np.vectorize(lambda u, v: pixel_to_table(u, v, view, fa.TILE_TOP_Z), otypes=[float, float])(
        us[::8, ::8], vs[::8, ::8])  # coarse grid, then interpolate (the mapping is smooth)
    mx = cv2.resize(((xs - x0) * CANVAS_PX_MM).astype(np.float32), (w, h), interpolation=cv2.INTER_LINEAR)
    my = cv2.resize(((y1 - ys) * CANVAS_PX_MM).astype(np.float32), (w, h), interpolation=cv2.INTER_LINEAR)
    img = cv2.remap(table, mx, my, cv2.INTER_LINEAR, borderValue=(60, 60, 60))
    rng = np.random.default_rng(seed)
    img = cv2.GaussianBlur(img, (3, 3), 0).astype(np.float32) * 0.85 + rng.normal(0, 6, img.shape)
    _, jpg = cv2.imencode(".jpg", img.clip(0, 255).astype(np.uint8), [cv2.IMWRITE_JPEG_QUALITY, 80])
    return cv2.imdecode(jpg, cv2.IMREAD_COLOR)


def scene(changes=None, seed=0) -> tuple:
    """Every tile near its slot (random offset up to 25 mm, random quarter turn +/- 8 deg)."""
    layout = load_layout()
    spots = {s: (p["x"], p["y"]) for s, p in layout["slots"].items()}
    expected = fa.expected_art(spots)
    rng = random.Random(seed)
    truth = {}
    for slot, (x, y) in spots.items():
        truth[slot] = (expected[slot], x + rng.uniform(-25, 25), y + rng.uniform(-25, 25),
                       rng.choice((0, 90, 180, 270)) + rng.uniform(-8, 8))
    if changes:
        changes(truth)
    tiles = {art: (x, y, a) for art, x, y, a in truth.values() if art is not None}
    return spots, truth, draw_table(tiles), layout.get("region_mm", 30.0)


def run(spots, table, region):
    shots = []
    async def fake_look_down_at(machine, x, y):  # stands in for zones.look_down_at
        view = camera_view(x, y)
        shots.append((x, y))
        return photograph(table, view, len(shots)), view
    zones.look_down_at = fake_look_down_at
    return asyncio.run(fa.find_art(None, spots, region)), shots


def main():
    t0 = time.time()
    spots, truth, table, region = scene(seed=3)
    found, shots = run(spots, table, region)
    errors = [np.hypot(found[s][0] - truth[s][1], found[s][1] - truth[s][2]) for s in spots]
    print(f"  ok  all 8 found from {len(shots)} photos in {time.time() - t0:.1f} s; "
          f"position error max {max(errors):.1f} mm, mean {np.mean(errors):.1f} mm")
    assert max(errors) < 5, errors

    def expect_refusal(name, change, words):
        spots, truth, table, region = scene(change, seed=3)
        try:
            run(spots, table, region)
        except NotReadyError as e:
            assert all(w in str(e) for w in words), str(e)
            print(f"  ok  {name}: {e}")
            return
        raise AssertionError(f"{name}: not refused")

    expect_refusal("empty slot", lambda t: t.update(slot_3=(None, 0, 0, 0)), ["slot_3", "no The Kiss found"])
    def swap(t):
        a, b = t["slot_2"], t["slot_6"]
        t["slot_2"], t["slot_6"] = (b[0],) + a[1:], (a[0],) + b[1:]
    expect_refusal("swapped tiles", swap, ["slot_2", "slot_6", "looks like"])
    def far(t):  # slot_5 is at the end of its row: push it outward, away from the other tiles
        art, x, y, a = t["slot_5"]
        t["slot_5"] = (art, x - 70, y, a)
    expect_refusal("tile 7 cm off its spot", far, ["slot_5", "mm from its spot"])
    print("all find_art checks passed; pictures in art_curator/runtime/find_art_*.jpg")


if __name__ == "__main__":
    main()
