"""Turn a validated exhibition into exactly three moves, run them, and keep track of where every tile is.

All motion comes from local data only: artwork ID -> catalog pickup slot -> layout.json (x, y), and
narrative position -> layout.json exhibit spot. The curator's text is never used for motion.

Slot mode (layout.json "slot_mode", or --slots): where the pick coordinates come from.
  fixed   layout.json as-is: tiles must sit on their taped outlines
  camera  before moving, the camera finds each tile within region_mm of its layout spot and the arm
          picks where the tile really is; a missing or doubled tile stops the run before any tile moves

State (art_curator/runtime/state.json) knows where each artwork is. Rules:
  - a run needs status "ready", which only `reset` sets (after a person checks the table)
  - one run at a time (a lock file, released even if the process dies)
  - a move counts as done only after the pick (with grip check) and place both returned
  - a failed move stops the run; that tile's location becomes "unknown"; a reset is required
  - after a finished exhibition, a reset is required before the next one

  python -m art_curator.exhibition status
  python -m art_curator.exhibition reset                         # after putting all 8 tiles back by hand
  python -m art_curator.exhibition plan "joy and freedom" --offline   # curate + print the 3 moves, no motion
  python -m art_curator.exhibition run "joy and freedom" --sim         # simulated robot
  python -m art_curator.exhibition run "joy and freedom"               # REAL ROBOT (asks before each motion)
  ... run "joy and freedom" --sim --slots camera                        # simulated camera (tiles a bit off)
"""
import argparse
import asyncio
import fcntl
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from art_curator.curator import CurationError, curate, load_catalog, show

HERE = Path(__file__).parent
LAYOUT_FILE = HERE / "layout.json"
RUNTIME = HERE / "runtime"
STATE_FILE = RUNTIME / "state.json"
LOCK_FILE = RUNTIME / "run.lock"
UNKNOWN = "unknown"
SLOT_MODES = ("fixed", "camera")


class NotReadyError(RuntimeError):
    """The physical state or the config doesn't allow a run. Nothing moved."""


@dataclass
class Move:
    step: int        # 1..3 = exhibit position
    art_id: int
    title: str
    source: str      # "slot_N"
    dest: str        # "exhibit_K"
    source_xy: tuple  # where the arm picks: the layout spot, or where the camera found the tile
    dest_xy: tuple
    status: str = "pending"  # pending | in_progress | done | failed
    layout_xy: tuple = ()     # the layout spot (kept to compare with the camera)


# ---------- state ----------

def home_locations(catalog: dict[int, dict]) -> dict[str, str]:
    return {str(i): a["pickup_position"] for i, a in catalog.items()}


def load_state() -> dict:
    """Missing or unreadable state = we don't know where anything is: needs a reset."""
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"status": "needs_reset", "locations": {}, "run": None,
                "note": "no saved state: check the table, then reset"}


def save_state(state: dict) -> None:
    RUNTIME.mkdir(exist_ok=True)
    state["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE_FILE)  # atomic: a crash never leaves half a file


def occupant(state: dict, place: str) -> str | None:
    return next((art for art, where in state["locations"].items() if where == place), None)


class RunLock:
    """Held for the whole run. flock is released by the OS if the process dies, so a crash can't
    leave the system locked forever (the state file then still says "running": see check_stale)."""

    def __enter__(self):
        RUNTIME.mkdir(exist_ok=True)
        self.f = open(LOCK_FILE, "w")
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.f.close()
            raise NotReadyError("another exhibition is running right now")
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def run_active() -> bool:
    try:
        with RunLock():
            return False
    except NotReadyError:
        return True


def mark_interrupted(state: dict) -> dict:
    """A run that died mid-way (crash, Ctrl-C, laptop asleep). The tile it was moving may be anywhere."""
    state["status"] = "failed"
    state["note"] = "the last run stopped unexpectedly: check the table, then reset"
    for m in (state.get("run") or {}).get("moves", []):
        if m["status"] == "in_progress":
            m["status"] = "failed"
            state["locations"][str(m["art_id"])] = UNKNOWN
    save_state(state)
    return state


def check_stale(state: dict) -> dict:
    """State says "running" but nobody holds the lock: that run died."""
    if state["status"] == "running" and not run_active():
        mark_interrupted(state)
    return state


def reset(catalog: dict[int, dict] | None = None) -> dict:
    """A person has put all 8 tiles back in their slots and cleared the exhibit. Record that."""
    if run_active():
        raise NotReadyError("can't reset while a run is moving the arm")
    catalog = catalog or load_catalog()
    state = {"status": "ready", "locations": home_locations(catalog), "run": None,
             "note": "reset: all 8 tiles confirmed in their slots, exhibit empty"}
    save_state(state)
    return state


# ---------- planning ----------

def load_layout(path: Path = LAYOUT_FILE) -> dict:
    return json.loads(path.read_text())


def slot_mode(layout: dict, override: str | None = None) -> str:
    mode = override or layout.get("slot_mode", "fixed")
    if mode not in SLOT_MODES:
        raise NotReadyError(f"slot mode '{mode}' is not one of {SLOT_MODES}")
    return mode


async def locate_sources(robot, moves: list[Move], state: dict, layout: dict) -> dict:
    """Camera mode: find every tile that should be in a slot (all of them, not just the 3 to move: that's
    the readiness check), then aim each move at its tile. Raises NotReadyError; nothing has moved."""
    home = {where: art for art, where in state["locations"].items() if where.startswith("slot_")}
    spots = {slot: (layout["slots"][slot]["x"], layout["slots"][slot]["y"]) for slot in home}
    found = await robot.find_art(spots, layout.get("region_mm", 30.0))
    missing = sorted(set(spots) - set(found))
    if missing:
        raise NotReadyError(f"camera can't find a tile in {', '.join(missing)}: check those slots")
    for m in moves:
        m.source_xy = found[m.source]
    return {slot: [round(v, 1) for v in xy] for slot, xy in found.items()}


def plan_moves(exhibition: dict, state: dict, catalog: dict[int, dict], layout: dict,
               need_calibrated: bool = True) -> list[Move]:
    """The three moves, in exhibit order. Raises NotReadyError (nothing moves) unless every check passes."""
    if state["status"] != "ready":
        raise NotReadyError(f"status is '{state['status']}', not 'ready': {state.get('note', '')} -> reset first")
    if need_calibrated and not layout.get("calibrated"):
        raise NotReadyError("layout.json is not calibrated on the robot yet (\"calibrated\": false)")
    moves = []
    for pick in exhibition["selected_artworks"]:  # validated: positions 1, 2, 3 in order
        art_id, pos = pick["id"], pick["position"]
        source, dest = catalog[art_id]["pickup_position"], f"exhibit_{pos}"
        src_xy, dst_xy = layout["slots"].get(source), layout["exhibit"].get(str(pos))
        if src_xy is None or dst_xy is None:
            raise NotReadyError(f"layout.json has no position for {source if src_xy is None else dest}")
        where = state["locations"].get(str(art_id))
        if where != source:
            raise NotReadyError(f"artwork {art_id} is at '{where}', not its slot {source}")
        if occupant(state, dest) is not None:
            raise NotReadyError(f"{dest} is already occupied by artwork {occupant(state, dest)}")
        moves.append(Move(pos, art_id, pick["title"], source, dest, (src_xy["x"], src_xy["y"]),
                          (dst_xy["x"], dst_xy["y"]), layout_xy=(src_xy["x"], src_xy["y"])))
    return moves


def print_plan(moves: list[Move]) -> None:
    for m in moves:
        print(f"  move {m.step}: [{m.art_id}] {m.title:30s} {m.source} (x={m.source_xy[0]:.0f} y={m.source_xy[1]:.0f})"
              f" -> {m.dest} (x={m.dest_xy[0]:.0f} y={m.dest_xy[1]:.0f})")


def print_located(moves: list[Move]) -> None:
    for m in moves:
        dx, dy = m.source_xy[0] - m.layout_xy[0], m.source_xy[1] - m.layout_xy[1]
        print(f"  [camera] {m.title} found at x={m.source_xy[0]:.0f} y={m.source_xy[1]:.0f}"
              f" ({dx:+.0f}, {dy:+.0f} mm from its layout spot)")


# ---------- running ----------

class SimRobot:
    """Stands in for the arm: prints and waits. fail_at=N makes move N fail like a lost grip.
    Its camera reports each tile up to 15 mm off its spot; missing=["slot_3"] hides that tile."""

    def __init__(self, seconds: float = 1.0, fail_at: int | None = None, missing: list[str] = ()):
        self.seconds, self.fail_at, self.missing = seconds, fail_at, list(missing)

    async def find_art(self, spots: dict, region_mm: float) -> dict:
        import random
        print(f"[sim] camera looking for {len(spots)} tiles")
        await asyncio.sleep(self.seconds)
        return {s: (x + random.uniform(-15, 15), y + random.uniform(-15, 15))
                for s, (x, y) in spots.items() if s not in self.missing}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def move(self, m: Move) -> None:
        print(f"[sim] picking {m.title} from {m.source}")
        await asyncio.sleep(self.seconds)
        if m.step == self.fail_at:
            raise RuntimeError(f"simulated grip failure on move {m.step}")
        print(f"[sim] placed it at {m.dest}")
        await asyncio.sleep(self.seconds)

    async def finish(self) -> None:
        print("[sim] back to overview")


class RealRobot:
    """The arm, through art_curator.tile (which uses VeriArm's move_tip and grip check)."""

    async def __aenter__(self):
        from robot import connect
        self.machine = await connect()
        return self

    async def __aexit__(self, *exc):
        await self.machine.close()

    async def find_art(self, spots: dict, region_mm: float) -> dict:
        from art_curator.find_art import find_art  # raises NotReadyError: missing or wrong tile
        return await find_art(self.machine, spots, region_mm)

    async def move(self, m: Move) -> None:
        from art_curator.tile import pick_tile, place_tile
        await pick_tile(self.machine, *m.source_xy)  # raises TileGripError if it didn't come up
        await place_tile(self.machine, *m.dest_xy)

    async def finish(self) -> None:
        from inspection import go_to_overview
        await go_to_overview(self.machine)


async def execute(exhibition: dict, robot, need_calibrated: bool = True, on_change=None,
                  slots: str | None = None, narrator=None) -> dict:
    """Plan, locate (camera mode), then do the moves one by one, saving state after every step.
    Returns the final state. Raises NotReadyError before any tile moves if a check fails.
    on_change(state) is called after each save. slots overrides layout.json's slot_mode.
    narrator (narrate.Narrator): speaks as it goes; never changes what the arm does."""
    picks = {p["position"]: p for p in exhibition["selected_artworks"]}
    catalog = load_catalog()
    def save(state):
        save_state(state)
        if on_change:
            on_change(state)

    with RunLock():
        state = load_state()
        if state["status"] == "running":  # we hold the lock, so whoever wrote "running" is gone
            mark_interrupted(state)
        layout = load_layout()
        mode = slot_mode(layout, slots)
        moves = plan_moves(exhibition, state, catalog, layout, need_calibrated)
        async with robot:
            located = None
            if mode == "camera":
                try:
                    located = await locate_sources(robot, moves, state, layout)
                except NotReadyError:
                    await robot.finish()  # camera pose -> back to overview; no tile has moved
                    raise
                print_located(moves)
            state.update(status="running", note=f"assembling '{exhibition['exhibition_title']}'",
                         run={"exhibition": exhibition, "moves": [asdict(m) for m in moves], "slot_mode": mode,
                              "located": located, "started": time.strftime("%Y-%m-%dT%H:%M:%S")})
            save(state)
            if narrator:
                await narrator.quiet()  # let the preview finish if Assemble was pressed during it
                narrator.starting(exhibition)
            for i, m in enumerate(moves):
                if narrator and i:
                    await narrator.quiet()  # the previous tile's description finishes before the next tile
                state["run"]["moves"][i]["status"] = "in_progress"
                save(state)
                if narrator:
                    narrator.placing(picks[m.step])  # plays while this tile is carried
                try:
                    await robot.move(m)
                except Exception as e:  # grip lost, motion error, connection drop: stop, don't guess
                    state["run"]["moves"][i]["status"] = "failed"
                    state["locations"][str(m.art_id)] = UNKNOWN
                    state.update(status="failed",
                                 note=f"move {m.step} ({m.title}) failed: {e}. Check the table, then reset")
                    save(state)
                    if narrator:
                        narrator.failed(picks[m.step])
                    return state
                state["run"]["moves"][i]["status"] = "done"
                state["locations"][str(m.art_id)] = m.dest
                save(state)
            await robot.finish()
        if narrator:  # still "running" (page: "All 3 placed") until the closing words have been spoken
            await narrator.quiet()
            narrator.complete(exhibition)
            await narrator.quiet()
        state.update(status="complete", note="exhibition assembled; reset before the next one")
        save(state)
        return state


# ---------- command line ----------

def print_status(state: dict) -> None:
    print(f"status: {state['status']}  {state.get('note', '')}")
    catalog = load_catalog()
    for art, where in sorted(state["locations"].items(), key=lambda kv: int(kv[0])):
        print(f"  [{art}] {catalog[int(art)]['title']:30s} {where}")
    for m in (state.get("run") or {}).get("moves", []):
        print(f"  move {m['step']}: [{m['art_id']}] {m['title']} -> {m['dest']}: {m['status']}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", choices=["status", "reset", "plan", "run"])
    p.add_argument("theme", nargs="?")
    p.add_argument("--offline", action="store_true", help="keyword curator, no Claude call")
    p.add_argument("--sim", action="store_true", help="simulated robot (no arm)")
    p.add_argument("--fail-at", type=int, help="--sim: make move N fail")
    p.add_argument("--slots", choices=SLOT_MODES, help="fixed or camera (default: layout.json slot_mode)")
    p.add_argument("--missing", nargs="*", default=[], help="--sim --slots camera: slots the camera can't see")
    p.add_argument("--voice", action="store_true", help="narrate out loud while building")
    p.add_argument("--voice-wait", action="store_true", help="--voice, but the arm waits for each sentence")
    p.add_argument("--yes", action="store_true", help="reset: skip the question; run: don't ask before each motion")
    args = p.parse_args()

    if args.cmd == "status":
        print_status(check_stale(load_state()))
        return
    if args.cmd == "reset":
        if not args.yes and input("Are all 8 tiles in their slots and the 3 exhibit spots empty? (yes/no) ") != "yes":
            sys.exit("Not reset.")
        print_status(reset())
        return
    if not args.theme:
        p.error("give a theme")
    try:
        exhibition = curate(args.theme, offline=args.offline)
        show(exhibition)
        if args.cmd == "plan":
            state, layout = check_stale(load_state()), load_layout()
            print(f"\nPlan (no motion, slot mode: {slot_mode(layout, args.slots)}):")
            print_plan(plan_moves(exhibition, state, load_catalog(), layout, need_calibrated=False))
            if not layout.get("calibrated"):
                print("  (layout.json is a DRAFT: a real run will refuse until it's calibrated)")
            return
        import find_pick
        find_pick.ASK_BEFORE_MOVING = not args.yes
        robot = SimRobot(fail_at=args.fail_at, missing=args.missing) if args.sim else RealRobot()
        narrator = None
        if args.voice or args.voice_wait:
            from art_curator.narrate import Narrator
            narrator = Narrator(wait=args.voice_wait)
            narrator.curated(exhibition)  # title + statement, like the page does after curating
        state = asyncio.run(execute(exhibition, robot, need_calibrated=not args.sim, slots=args.slots,
                                    narrator=narrator))
    except (CurationError, NotReadyError) as e:
        sys.exit(f"[exhibition] NOT STARTED, nothing moved: {e}")
    print()
    print_status(state)


if __name__ == "__main__":
    main()
