"""AI Art Curator web page: type a theme, see the exhibition, let the arm assemble it, reset.

  python -m art_curator.app                 http://localhost:8090  simulated arm (safe to play with)
  python -m art_curator.app --robot         REAL ARM: no per-motion questions, keep someone at the e-stop
  python -m art_curator.app --offline       keyword curator instead of Claude (no network / no API key)
  python -m art_curator.app --voice         read the title and statement aloud (VeriArm's announce.py)
  python -m art_curator.app --slots camera  find the tiles with the camera (default: layout.json slot_mode)
  --host 0.0.0.0 to open it from another laptop.

Separate from VeriArm's ui.py (port 8080); both can run at once, but only one may drive the arm.
Stop (robot mode) stops the arm where it is and leaves the suction as is, like ui.py.
"""
import argparse
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from art_curator import exhibition as ex
from art_curator.curator import CurationError, curate, load_catalog

HERE = Path(__file__).parent
PAGE = HERE / "page.html"
IMAGES = HERE / "images"


class App:
    """What the page shows. The physical truth lives in exhibition's state file; this adds the
    curated-but-not-yet-assembled exhibition and what the server is busy with."""

    def __init__(self, robot: bool, offline: bool, voice: bool, slots: str | None = None):
        self.robot, self.offline, self.voice, self.slots = robot, offline, voice, slots
        self.lock = threading.Lock()
        self.pending: dict | None = None  # last validated curation, not yet assembled
        self.busy: str | None = None      # "curating" | "assembling"
        self.error: str | None = None

    def status(self) -> dict:
        state = ex.check_stale(ex.load_state())
        catalog = load_catalog()
        with self.lock:
            return {"state": state, "pending": self.pending, "busy": self.busy, "error": self.error,
                    "mode": "robot" if self.robot else "simulated", "curator": "offline" if self.offline else "claude",
                    "calibrated": bool(ex.load_layout().get("calibrated")),
                    "slot_mode": ex.slot_mode(ex.load_layout(), self.slots),
                    "catalog": [{"id": i, "title": a["title"], "artist": a["artist"]} for i, a in catalog.items()]}

    def _claim(self, what: str) -> None:
        with self.lock:
            if self.busy:
                raise ex.NotReadyError(f"busy {self.busy}; wait for it to finish")
            if ex.run_active():
                raise ex.NotReadyError("an exhibition is being assembled right now")
            self.busy, self.error = what, None

    def curate(self, theme: str) -> dict:
        self._claim("curating")
        try:
            exhibition = curate(theme, offline=self.offline)
        except CurationError as e:
            with self.lock:
                self.error = f"Curation rejected, nothing will move: {e}"
            raise
        finally:
            with self.lock:
                self.busy = None
        with self.lock:
            self.pending = exhibition
        if self.voice:
            import announce
            announce.speak(f"{exhibition['exhibition_title']}. {exhibition['curatorial_statement']}")
        return exhibition

    def assemble(self) -> None:
        """Check everything now (so the page gets the refusal at once), then move in the background."""
        with self.lock:
            exhibition = self.pending
        if exhibition is None:
            raise ex.NotReadyError("curate an exhibition first")
        ex.plan_moves(exhibition, ex.check_stale(ex.load_state()), load_catalog(), ex.load_layout(),
                      need_calibrated=self.robot)
        self._claim("assembling")
        threading.Thread(target=self._assemble, args=(exhibition,), daemon=True).start()

    def _assemble(self, exhibition: dict) -> None:
        if self.robot:
            import find_pick
            find_pick.ASK_BEFORE_MOVING = False  # nobody at a terminal to answer
            robot = ex.RealRobot()
        else:
            robot = ex.SimRobot(seconds=1.5)
        try:
            state = asyncio.run(ex.execute(exhibition, robot, need_calibrated=self.robot, slots=self.slots))
            if state["status"] == "failed":
                with self.lock:
                    self.error = state["note"]
        except Exception as e:  # NotReadyError raced in, robot unreachable, ...
            with self.lock:
                self.error = f"Assembly didn't run: {e}"
        finally:
            with self.lock:
                self.busy = None

    def reset(self) -> None:
        with self.lock:
            if self.busy == "assembling":
                raise ex.NotReadyError("can't reset while the arm is assembling")
            self.error = None
        ex.reset()

    def stop(self) -> str:
        if not self.robot:
            return "simulated: nothing to stop"
        async def go():
            from viam.components.arm import Arm
            from robot import ARM, connect
            async with await connect() as machine:
                await Arm.from_robot(machine, ARM).stop()
        try:
            asyncio.run(go())
            return "arm stopped (suction left as is)"
        except Exception as e:
            return f"couldn't send arm stop ({e}): use the e-stop"


APP: App | None = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # the page polls every second; keep the terminal readable

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        if self.path == "/":
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/status":
            self._json(200, APP.status())
        elif self.path.startswith("/images/"):
            name = Path(self.path).name
            f = IMAGES / name
            if f.parent == IMAGES and f.suffix == ".jpg" and f.exists():
                self._send(200, f.read_bytes(), "image/jpeg")
            else:
                self._send(404, b"no image", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        try:
            if self.path == "/api/curate":
                self._json(200, APP.curate(str(body.get("theme", ""))))
            elif self.path == "/api/assemble":
                APP.assemble()
                self._json(200, {"ok": True})
            elif self.path == "/api/reset":
                if body.get("confirm") is not True:
                    raise ex.NotReadyError("confirm that all 8 tiles are back in their slots")
                APP.reset()
                self._json(200, {"ok": True})
            elif self.path == "/api/stop":
                self._json(200, {"message": APP.stop()})
            else:
                self._send(404, b"not found", "text/plain")
        except (CurationError, ex.NotReadyError) as e:
            self._json(409, {"error": str(e)})


def main():
    global APP
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--robot", action="store_true", help="drive the REAL arm (default: simulated)")
    p.add_argument("--offline", action="store_true", help="keyword curator, no Claude call")
    p.add_argument("--voice", action="store_true", help="speak the title and statement")
    p.add_argument("--slots", choices=ex.SLOT_MODES, help="fixed or camera (default: layout.json slot_mode)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8090)
    args = p.parse_args()
    APP = App(robot=args.robot, offline=args.offline, voice=args.voice, slots=args.slots)
    mode = "REAL ARM" if args.robot else "simulated arm"
    print(f"AI Art Curator ({mode}, {'offline' if args.offline else 'Claude'} curator): "
          f"http://{'localhost' if args.host == '127.0.0.1' else args.host}:{args.port}")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
