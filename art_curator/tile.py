"""Pick and place one flat artwork tile (11 x 11 cm, 2 mm plastic back, taped print). MOVES THE ARM.

Same motion style as find_pick.pick_box / place.place_box, tuned for something 2 mm thick lying on the
hard table: VeriArm presses 10 mm into a box top (cardboard gives); here that would push the cup into
the table, so the press is a few mm. VeriArm's code is only imported, never changed.

First time with tiles: measure the height correction (see TILE_Z_CORRECTION_MM).
  python -m art_curator.tile hover              tip 30 mm above a tile at the blue zone center; measure the gap
  python -m art_curator.tile hover --x 350 --y 120
  python -m art_curator.tile pick-test          pick the tile, lift 6 cm, put it back, release
  python -m art_curator.tile move X1 Y1 X2 Y2   pick at (X1, Y1), place at (X2, Y2)
Every motion asks first unless --yes. Keep someone at the e-stop.
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
    await move_tip(machine, tile_tip(x, y, APPROACH_MM), "back off", straight_line=True)


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


async def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", choices=["hover", "pick-test", "move"])
    p.add_argument("coords", nargs="*", type=float, help="move: X1 Y1 X2 Y2 (world mm)")
    zone = get_pose("blue_zone")["center_world"]
    p.add_argument("--x", type=float, default=zone["x"], help="tile center x (default: blue zone center)")
    p.add_argument("--y", type=float, default=zone["y"], help="tile center y (default: blue zone center)")
    p.add_argument("--yes", action="store_true", help="don't ask before each motion")
    args = p.parse_args()
    find_pick.ASK_BEFORE_MOVING = not args.yes
    if args.cmd == "move" and len(args.coords) != 4:
        p.error("move needs X1 Y1 X2 Y2")
    async with await connect() as machine:
        if args.cmd == "hover":
            await hover(machine, args.x, args.y)
        elif args.cmd == "pick-test":
            await pick_test(machine, args.x, args.y)
        else:
            x1, y1, x2, y2 = args.coords
            await pick_tile(machine, x1, y1)
            await place_tile(machine, x2, y2)
            await go_to_overview(machine)


if __name__ == "__main__":
    asyncio.run(main())
