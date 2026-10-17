"""Turn a validated exhibition into exactly three moves, run them, and keep track of where every tile is.

All motion comes from local data only: artwork ID -> catalog pickup slot -> layout.json (x, y), and
narrative position -> layout.json exhibit spot. The curator's text is never used for motion.

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


class NotReadyError(RuntimeError):
    """The physical state or the config doesn't allow a run. Nothing moved."""


@dataclass
class Move:
    step: int        # 1..3 = exhibit position
    art_id: int
    title: str
    source: str      # "slot_N"
    dest: str        # "exhibit_K"
    source_xy: tuple
    dest_xy: tuple
    status: str = "pending"  # pending | in_progress | done | failed


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
        moves.append(Move(pos, art_id, pick["title"], source, dest,
                          (src_xy["x"], src_xy["y"]), (dst_xy["x"], dst_xy["y"])))
    return moves


def print_plan(moves: list[Move]) -> None:
    for m in moves:
        print(f"  move {m.step}: [{m.art_id}] {m.title:30s} {m.source} (x={m.source_xy[0]:.0f} y={m.source_xy[1]:.0f})"
              f" -> {m.dest} (x={m.dest_xy[0]:.0f} y={m.dest_xy[1]:.0f})")


# ---------- running ----------

class SimRobot:
    """Stands in for the arm: prints and waits. fail_at=N makes move N fail like a lost grip."""

    def __init__(self, seconds: float = 1.0, fail_at: int | None = None):
        self.seconds, self.fail_at = seconds, fail_at

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

    async def move(self, m: Move) -> None:
        from art_curator.tile import pick_tile, place_tile
        await pick_tile(self.machine, *m.source_xy)  # raises TileGripError if it didn't come up
        await place_tile(self.machine, *m.dest_xy)

    async def finish(self) -> None:
        from inspection import go_to_overview
        await go_to_overview(self.machine)


async def execute(exhibition: dict, robot, need_calibrated: bool = True, on_change=None) -> dict:
    """Plan, then do the moves one by one, saving state after every step. Returns the final state.
    Raises NotReadyError before any motion if a check fails. on_change(state) is called after each save."""
    catalog = load_catalog()
    def save(state):
        save_state(state)
        if on_change:
            on_change(state)

    with RunLock():
        state = load_state()
        if state["status"] == "running":  # we hold the lock, so whoever wrote "running" is gone
            mark_interrupted(state)
        moves = plan_moves(exhibition, state, catalog, load_layout(), need_calibrated)
        state.update(status="running", note=f"assembling '{exhibition['exhibition_title']}'",
                     run={"exhibition": exhibition, "moves": [asdict(m) for m in moves],
                          "started": time.strftime("%Y-%m-%dT%H:%M:%S")})
        save(state)
        async with robot:
            for i, m in enumerate(moves):
                state["run"]["moves"][i]["status"] = "in_progress"
                save(state)
                try:
                    await robot.move(m)
                except Exception as e:  # grip lost, motion error, connection drop: stop, don't guess
                    state["run"]["moves"][i]["status"] = "failed"
                    state["locations"][str(m.art_id)] = UNKNOWN
                    state.update(status="failed",
                                 note=f"move {m.step} ({m.title}) failed: {e}. Check the table, then reset")
                    save(state)
                    return state
                state["run"]["moves"][i]["status"] = "done"
                state["locations"][str(m.art_id)] = m.dest
                save(state)
            await robot.finish()
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
            print("\nPlan (no motion):")
            print_plan(plan_moves(exhibition, state, load_catalog(), layout, need_calibrated=False))
            if not layout.get("calibrated"):
                print("  (layout.json is a DRAFT: a real run will refuse until it's calibrated)")
            return
        import find_pick
        find_pick.ASK_BEFORE_MOVING = not args.yes
        robot = SimRobot(fail_at=args.fail_at) if args.sim else RealRobot()
        state = asyncio.run(execute(exhibition, robot, need_calibrated=not args.sim))
    except (CurationError, NotReadyError) as e:
        sys.exit(f"[exhibition] NOT STARTED, nothing moved: {e}")
    print()
    print_status(state)


if __name__ == "__main__":
    main()
