"""Work out the 11 tile spots from where the colored sheets are, then check them one by one with the arm.

Layout: the 8 slots as a 4 x 2 block centered on the green sheet (overhanging its ends onto the table),
the exhibit as a row of 3 on blue. Follows each sheet's rotation; orange is left free. The sheets stay
where VeriArm measured them, so package sorting keeps working.
Writes art_curator/layout.json only; VeriArm's poses.json is read, never written.

  python -m art_curator.calibrate from-saved   no robot: sheets as VeriArm last measured them (poses.json)
  python -m art_curator.calibrate survey       MOVES THE ARM: find all 3 sheets with the camera (sheets EMPTY,
                                               moved at most ~10-20 cm from where VeriArm measured them)
  python -m art_curator.calibrate check        MOVES THE ARM: hover over each spot, nudge it, mark calibrated
  python -m art_curator.calibrate map          draw the layout (runtime/layout.png), no robot
  --reverse-exhibit: exhibit position 1 at the +y end of the row instead of the -y end.
"""
import argparse
import asyncio
import json
import math
import time

import numpy as np

from art_curator.exhibition import LAYOUT_FILE, RUNTIME, load_layout
from poses import load_poses

TILE_MM = 110.0
GRID_MM = 140.0     # slot rows: 3 cm gaps
COL_MM = 125.0      # slot columns: 1.5 cm gaps (4 x 125 is already wider than the sheet)
ROW_MM = 125.0      # exhibit row pitch on blue (1.5 cm gaps; the row may overhang the sheet a little)
EDGE_MM = 5.0       # keep grid tiles this far inside their sheet
CHECK_HOVER_MM = 20.0


# ---------- geometry (no robot) ----------

def sheet_frame(corners: list) -> tuple:
    """(center, unit axes u and v, side lengths) of the best-fit rectangle through the 4 corners.
    Fitted, not taken side by side: corners measured from angled views aren't a perfect rectangle
    (green's sides meet at ~80 deg), and skewed axes would skew the whole grid."""
    import cv2
    (cx, cy), (lu, lv), angle = cv2.minAreaRect(np.array(corners, np.float32))
    a = math.radians(angle)
    u = np.array([math.cos(a), math.sin(a)])
    return np.array([cx, cy]), u, np.array([-u[1], u[0]]), (lu, lv)


def grid_on_sheet(corners: list) -> list[tuple]:
    """4 x 2 tile centers: 4 along the sheet's long side (overhanging its ends: 8 tiles don't fit on a
    38 x 31 cm sheet), 2 rows across it, shifted toward the arm base as far as the short side allows.
    Order: the row nearer the arm first, each row by y."""
    center, u, v, (lu, lv) = sheet_frame(corners)
    if lv > lu:
        u, v, lu, lv = v, u, lv, lu  # u = long side
    to_base = -center / np.linalg.norm(center)
    if v @ to_base < 0:
        v = -v  # v points toward the arm
    room = lv / 2 - TILE_MM / 2 - EDGE_MM - GRID_MM / 2  # how far the rows can move toward the arm
    shift = max(0.0, room)
    cols = [(k - 1.5) * COL_MM for k in range(4)]
    rows = [shift + GRID_MM / 2, shift - GRID_MM / 2]  # nearer the arm first
    pts = [center + b * v + a * u for b in rows for a in cols]
    near, far = pts[:4], pts[4:]
    by_y = lambda p: p[1]
    return [tuple(np.round(p, 1)) for p in sorted(near, key=by_y) + sorted(far, key=by_y)]


def row_on_sheet(corners: list, reverse: bool = False) -> list[tuple]:
    """3 exhibit spots in a row through the sheet center, along the sheet side that's more across the
    arm's line of sight (so all three are about equally far). Position 1 at the -y end unless reverse."""
    center, u, v, _ = sheet_frame(corners)
    to_base = -center / np.linalg.norm(center)
    axis = u if abs(u @ to_base) < abs(v @ to_base) else v
    pts = sorted((tuple(np.round(center + k * ROW_MM * axis, 1)) for k in (-1, 0, 1)), key=lambda p: p[1])
    return pts[::-1] if reverse else pts


def build_layout(sheets: dict, reverse: bool, source: str) -> dict:
    """sheets: {"green": corners, "blue": corners, ...} in world mm."""
    slots = grid_on_sheet(sheets["green"])
    row = row_on_sheet(sheets["blue"], reverse)
    return {
        "calibrated": False,
        "note": (f"From {source} at {time.strftime('%Y-%m-%d %H:%M')}. Slots 1-8: 4 x 2 block on green "
                 f"({COL_MM:.0f} x {GRID_MM:.0f} mm pitch, overhanging its ends, rows pulled toward the arm); exhibit row of 3 on blue ({ROW_MM:.0f} mm pitch, "
                 f"position 1 at the {'+y' if reverse else '-y'} end). Run `calibrate check` on the robot "
                 "to confirm every spot."),
        "slots": {f"slot_{i}": {"x": float(x), "y": float(y)} for i, (x, y) in enumerate(slots, 1)},
        "exhibit": {str(i): {"x": float(x), "y": float(y)} for i, (x, y) in enumerate(row, 1)},
    }


def save_layout(layout: dict) -> None:
    LAYOUT_FILE.write_text(json.dumps(layout, indent=2) + "\n")


def print_layout(layout: dict) -> None:
    spots = {**layout["slots"], **{f"exhibit_{k}": v for k, v in layout["exhibit"].items()}}
    for name, p in spots.items():
        r = math.hypot(p["x"], p["y"])
        tag = "  <-- further than the arm has gone (~630)" if r > 630 else ""
        print(f"  {name:10s} x={p['x']:6.1f} y={p['y']:6.1f}  {r:4.0f} mm from the base"
              f"{'  checked' if p.get('checked') else ''}{tag}")
    print(f"  calibrated: {layout.get('calibrated')}")


def draw_map(layout: dict, sheets: dict | None = None) -> str:
    """Top-down picture: +x (away from the base) up, +y left. Returns the file path."""
    import cv2
    spots = list(layout["slots"].values()) + list(layout["exhibit"].values())
    xy = [(0.0, 0.0)] + [(p["x"] + s, p["y"] + s) for p in spots for s in (-TILE_MM / 2, TILE_MM / 2)]
    xy += [tuple(c) for corners in (sheets or {}).values() for c in corners]
    xs, ys = [p[0] for p in xy], [p[1] for p in xy]
    S, M = 0.8, 40  # px per mm, margin
    W, H = int((max(ys) - min(ys)) * S) + 2 * M, int((max(xs) - min(xs)) * S) + 2 * M + 20
    img = np.full((H, W, 3), 255, np.uint8)
    def px(x, y):
        return int(M + (max(ys) - y) * S), int(H - M - (x - min(xs)) * S)
    colors = {"blue": (230, 160, 90), "green": (120, 200, 120), "orange": (80, 160, 240)}
    for name, corners in (sheets or {}).items():
        cv2.fillPoly(img, [np.array([px(x, y) for x, y in corners], np.int32)], colors[name])
    def tile(p, label, col):
        h = TILE_MM / 2
        cv2.rectangle(img, px(p["x"] + h, p["y"] + h), px(p["x"] - h, p["y"] - h), col, 2)
        cx, cy = px(p["x"], p["y"])
        cv2.putText(img, label, (cx - 14, cy + 6), cv2.FONT_HERSHEY_SIMPLEX, 0.55, col, 2)
    for k, p in layout["slots"].items():
        tile(p, "S" + k.split("_")[1], (40, 40, 40))
    for k, p in layout["exhibit"].items():
        tile(p, "E" + k, (20, 20, 170))
    cv2.circle(img, px(0, 0), 12, (0, 0, 0), -1)
    cv2.putText(img, "arm base", (px(0, 0)[0] + 16, px(0, 0)[1] + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1)
    cv2.putText(img, "+x up, +y left (seen from above)", (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1)
    RUNTIME.mkdir(exist_ok=True)
    out = RUNTIME / "layout.png"
    cv2.imwrite(str(out), img)
    return str(out)


def saved_sheets() -> dict:
    """The sheets as VeriArm last measured them (poses.json)."""
    poses = load_poses()
    return {c: poses[f"{c}_zone"]["corners_world"] for c in ("green", "blue")}


# ---------- robot ----------

async def survey_sheets(machine) -> dict:
    """Find each sheet with VeriArm's zones.best_view_of, starting from where it was last measured."""
    import zones
    from detect import BLUE_HSV_HIGH, BLUE_HSV_LOW
    zones.COLORS.setdefault("blue", (BLUE_HSV_LOW, BLUE_HSV_HIGH))  # in memory only, zones.py is unchanged
    found = {}
    for color, corners in saved_sheets().items():
        target = tuple(np.mean(np.array(corners, float), axis=0))
        print(f"[calibrate] looking for the {color} sheet near x={target[0]:.0f} y={target[1]:.0f}")
        best = await zones.best_view_of(machine, color, target)
        if best is None:
            raise RuntimeError(f"can't see the {color} sheet: is it empty, and within ~10-20 cm of before?")
        rec, _ = best
        print(f"[calibrate]   {color}: center x={rec['center_world']['x']} y={rec['center_world']['y']}, "
              f"{rec['size_mm'][0]} x {rec['size_mm'][1]} mm{' (PARTIAL view)' if rec.get('partial') else ''}")
        found[color] = rec["corners_world"]
    return found


async def check_spots(machine) -> None:
    """Hover over each spot; Enter = good, 'dx dy' = nudge (mm), s = skip, q = stop. Saves as it goes."""
    import find_pick
    from art_curator.tile import tile_tip
    from find_pick import move_tip
    from inspection import go_to_overview
    find_pick.ASK_BEFORE_MOVING = False  # this loop asks at every spot anyway
    layout = load_layout()
    spots = [("slots", k) for k in layout["slots"]] + [("exhibit", k) for k in layout["exhibit"]]
    print("Put a tile (or a taped outline) at each spot. At each hover: is the cup over the tile center?\n"
          "  Enter = yes   'dx dy' = move the spot by dx, dy mm (+x = away from the arm base, +y = to the\n"
          "  left when standing at the base looking out)   s = skip   q = stop")
    for group, key in spots:
        p = layout[group][key]
        name = key if group == "slots" else f"exhibit_{key}"
        while True:
            await move_tip(machine, tile_tip(p["x"], p["y"], CHECK_HOVER_MM), f"hover over {name}")
            answer = input(f"{name} (x={p['x']:.0f} y={p['y']:.0f}): ").strip().lower()
            if answer == "q":
                save_layout(layout)
                await go_to_overview(machine)
                return
            if answer in ("", "s"):
                p["checked"] = answer == ""
                save_layout(layout)
                break
            try:
                dx, dy = (float(v) for v in answer.split())
            except ValueError:
                print("  type Enter, s, q, or two numbers like '5 -10'")
                continue
            p["x"], p["y"] = round(p["x"] + dx, 1), round(p["y"] + dy, 1)
    await go_to_overview(machine)
    every = all(p.get("checked") for g in ("slots", "exhibit") for p in layout[g].values())
    if every and input("All 11 spots checked. Mark the layout calibrated? (yes/no) ").strip() == "yes":
        layout["calibrated"] = True
    save_layout(layout)
    print_layout(layout)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", choices=["from-saved", "survey", "check", "map"])
    p.add_argument("--reverse-exhibit", action="store_true", help="exhibit position 1 at the +y end")
    args = p.parse_args()
    if args.cmd == "map":
        print(draw_map(load_layout()))
        return
    if args.cmd == "from-saved":
        sheets = saved_sheets()
        layout = build_layout(sheets, args.reverse_exhibit, "VeriArm's saved zones (poses.json)")
    else:
        from robot import connect
        async def go():
            async with await connect() as machine:
                if args.cmd == "check":
                    await check_spots(machine)
                    return None
                from inspection import go_to_overview
                found = await survey_sheets(machine)
                await go_to_overview(machine)
                return found
        sheets = asyncio.run(go())
        if sheets is None:
            return
        RUNTIME.mkdir(exist_ok=True)
        (RUNTIME / "sheets.json").write_text(json.dumps(sheets, indent=1))
        layout = build_layout(sheets, args.reverse_exhibit, "camera survey")
    save_layout(layout)
    print_layout(layout)
    print("map:", draw_map(layout, sheets))


if __name__ == "__main__":
    main()
