"""Pick and place one flat artwork tile (11 x 11 cm, 2 mm plastic back, taped print). MOVES THE ARM.

Same motion style as find_pick.pick_box / place.place_box, tuned for something 2 mm thick lying on the
hard table: VeriArm presses 10 mm into a box top (cardboard gives); here that would push the cup into
the table, so the press is a few mm. VeriArm's code is only imported, never changed.

First time with tiles: measure the height correction (see TILE_Z_CORRECTION_MM).
  python -m art_curator.tile hover                tip 30 mm above a tile at the blue zone center; measure the gap
  python -m art_curator.tile hover slot_8         ... above a layout spot (reach check)
  python -m art_curator.tile pick-test S3         pick the tile, lift 6 cm, put it back, release
  python -m art_curator.tile move slot_1 exhibit_1
Spots: slot_1..slot_8 / exhibit_1..exhibit_3 (or S1..S8 / E1..E3, from layout.json: always the current,
calibrated positions), "blue" (blue zone center), or two numbers X Y in world mm.
Every motion asks first unless --yes. Keep someone at the e-stop.
These are test moves: they don't update the exhibition's state. Afterwards put the tiles back and run
`exhibition reset` before a real run.
"""
import argparse
import asyncio

from viam.components.gripper import Gripper
from viam.proto.common import Pose
from viam.robot.client import RobotClient

import find_pick
from detect import TABLE_TOP_Z
from find_pick import move_tip, overview_tip_theta
from inspection import go_to_overview
from poses import get_pose
from robot import GRIPPER, connect

TILE_H_MM = 2.0
# The real suction tip sits lower than the config thinks (find_pick: 12 mm at a 61 mm box, 22 at 140).
# Not measured at table height yet. 17 errs HIGH (safe: worst case the cup stops short of the tile).
# Measure: `hover` commands 30 mm above the tile; ruler the real gap G; set this to 47 - G.
TILE_Z_CORRECTION_MM = 17.0
HOVER_MM = 30.0
APPROACH_MM = 50.0
PLACE_BACK_OFF_MM = 120.0  # after placing, rise this far so the artwork is visible while it's described
PRESS_MM = 3.0     # past the tile top: just enough to seal the cup, not enough to shove the table
LIFT_MM = 60.0     # tile bottom this far above the table while carrying
PUT_DOWN_MM = 3.0  # release with the tile this far above the table (a flat tile slides if dropped further)
GRIP_SETTLE_S = 2.0
SETTLE_S = 1.0


class TileGripError(RuntimeError):
    """The tile didn't come up with the cup. Suction is off; the tile's position is uncertain."""


def tile_tip(x: float, y: float, height_above_top: float) -> Pose:
    """Suction tip pointing down, `height_above_top` mm above the top of a tile lying at (x, y)."""
    return Pose(x=x, y=y, z=TABLE_TOP_Z + TILE_H_MM + height_above_top + TILE_Z_CORRECTION_MM,
                o_x=0, o_y=0, o_z=-1, theta=overview_tip_theta())


async def pick_tile(machine: RobotClient, x: float, y: float) -> None:
    """Approach, press PRESS_MM, suction on, lift. Raises TileGripError if the wrist camera says the
    tile didn't come up (then suction is turned off and the tip stays up)."""
    gripper = Gripper.from_robot(machine, GRIPPER)
    await gripper.open()
    await move_tip(machine, tile_tip(x, y, APPROACH_MM), "approach above the tile")
    await move_tip(machine, tile_tip(x, y, -PRESS_MM), "lower onto the tile", straight_line=True)
    find_pick.confirm("turn suction ON")
    print("[tile] grab() returned:", await gripper.grab())
    await asyncio.sleep(GRIP_SETTLE_S)
    from grip_check import judge, wrist_photo
    before = await wrist_photo(machine)
    await move_tip(machine, tile_tip(x, y, LIFT_MM), "lift the tile", straight_line=True)
    held, score = judge(before, await wrist_photo(machine), "tile_grip")
    print(f"[tile] grip check: {score:.2f} -> {'HELD' if held else 'NOT held'} (captures/tile_grip.jpg)")
    if not held:
        await gripper.open()
        raise TileGripError(f"tile at x={x:.0f} y={y:.0f} not held after lift (score {score:.2f})")


async def place_tile(machine: RobotClient, x: float, y: float) -> None:
    """Carry the held tile to (x, y), lower to PUT_DOWN_MM, release, back off."""
    await move_tip(machine, tile_tip(x, y, LIFT_MM), "carry the tile")
    await move_tip(machine, tile_tip(x, y, PUT_DOWN_MM), "lower the tile", straight_line=True)
    find_pick.confirm("turn suction OFF (tile is just above the table)")
    await Gripper.from_robot(machine, GRIPPER).open()
    await asyncio.sleep(SETTLE_S)
    await move_tip(machine, tile_tip(x, y, PLACE_BACK_OFF_MM), "back off", straight_line=True)


async def hover(machine: RobotClient, x: float, y: float) -> None:
    await move_tip(machine, tile_tip(x, y, HOVER_MM), f"hover {HOVER_MM:.0f} mm above the tile")
    print(f"[tile] commanded: suction tip {HOVER_MM:.0f} mm above the tile top "
          f"(correction {TILE_Z_CORRECTION_MM:.0f} mm).\n"
          f"       Measure the real gap G (cup bottom to tile top) with a ruler.\n"
          f"       Then set TILE_Z_CORRECTION_MM = {HOVER_MM + TILE_Z_CORRECTION_MM:.0f} - G in art_curator/tile.py")


async def pick_test(machine: RobotClient, x: float, y: float) -> None:
    try:
        await pick_tile(machine, x, y)
    except TileGripError as e:
        print(f"[tile] PICK FAILED: {e}")
        await go_to_overview(machine)
        return
    await place_tile(machine, x, y)
    await go_to_overview(machine)
    print("[tile] pick test OK: picked, lifted, put back")


def spot_xy(name: str) -> tuple[float, float]:
    """slot_3 / S3 / exhibit_1 / E1 / blue -> (x, y) from layout.json (or the blue zone center)."""
    from art_curator.exhibition import load_layout
    key = name.strip().lower()
    if key == "blue":
        zone = get_pose("blue_zone")["center_world"]
        return zone["x"], zone["y"]
    layout = load_layout()
    if key[:1] == "s" and key[1:].isdigit():
        key = f"slot_{key[1:]}"
    if key[:1] == "e" and key[1:].isdigit():
        key = f"exhibit_{key[1:]}"
    if key.startswith("slot_") and key in layout["slots"]:
        p = layout["slots"][key]
    elif key.startswith("exhibit_") and key[8:] in layout["exhibit"]:
        p = layout["exhibit"][key[8:]]
    else:
        raise SystemExit(f"[tile] unknown spot '{name}': use slot_1..slot_{len(layout['slots'])}, "
                         f"exhibit_1..exhibit_{len(layout['exhibit'])} (or S1 / E1), blue, or X Y")
    return float(p["x"]), float(p["y"])


def parse_spots(tokens: list[str]) -> list[tuple[str, float, float]]:
    """Spot names and/or X Y number pairs, in order -> [(label, x, y)]."""
    out, i = [], 0
    while i < len(tokens):
        try:
            x = float(tokens[i])
        except ValueError:
            out.append((tokens[i], *spot_xy(tokens[i])))
            i += 1
            continue
        if i + 1 >= len(tokens):
            raise SystemExit(f"[tile] '{tokens[i]}' needs a Y after it")
        out.append((f"({tokens[i]}, {tokens[i + 1]})", x, float(tokens[i + 1])))
        i += 2
    return out


async def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", choices=["hover", "pick-test", "move"])
    p.add_argument("where", nargs="*", help="hover / pick-test: one spot; move: two (from, to). Default: blue")
    p.add_argument("--yes", action="store_true", help="don't ask before each motion")
    args = p.parse_args()
    spots = parse_spots(args.where) or parse_spots(["blue"])
    need = 2 if args.cmd == "move" else 1
    if len(spots) != need:
        p.error(f"{args.cmd} needs {need} spot{'s' if need > 1 else ''}, got {len(spots)}")
    for label, x, y in spots:
        print(f"[tile] {label}: x={x:.1f} y={y:.1f}")
    find_pick.ASK_BEFORE_MOVING = not args.yes
    async with await connect() as machine:
        if args.cmd == "hover":
            await hover(machine, spots[0][1], spots[0][2])
        elif args.cmd == "pick-test":
            await pick_test(machine, spots[0][1], spots[0][2])
        else:
            (_, x1, y1), (_, x2, y2) = spots
            await pick_tile(machine, x1, y1)
            await place_tile(machine, x2, y2)
            await go_to_overview(machine)


if __name__ == "__main__":
    asyncio.run(main())
