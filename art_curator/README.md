# AI Art Curator

A variation of VeriArm. Type a theme ("finding hope in uncertain times"); Claude picks 3 of 8 artwork tiles
and orders them as a story; the arm takes those tiles from their slots and lays them out as a small
exhibition, in narrative order.

Everything lives in this folder. It **imports** VeriArm's motion helpers (`find_pick`, `robot`, `poses`,
`inspection`, `zones`, `grip_check`) but **never changes** them, and never writes VeriArm's `poses.json`.
Package sorting keeps working as before.

Run every command from the repo root with the project venv: `.venv/bin/python -m art_curator.<module> ...`

## Quick start (no robot needed)

```bash
.venv/bin/python -m art_curator.app --offline        # web page, simulated arm: http://localhost:8090
.venv/bin/python -m art_curator.app                  # same, real Claude curator (needs ANTHROPIC_API_KEY)
```

On the page: type a theme, **Curate Exhibition**, then **Assemble exhibition**. Afterwards tick
"All 8 tiles are back..." and **Confirm reset** before the next one.

Claude needs `ANTHROPIC_API_KEY=...` in the repo's `.env` (gitignored, next to the Viam keys).
`--offline` swaps in a keyword matcher so everything else can be tested without network or a key.
It is a test stand-in, not a curator.

## The physical setup

| What | Where |
|---|---|
| 8 artwork tiles | 11 x 11 cm, 2 mm 3D-printed plastic back, 10 x 10 cm print taped on (clear tape helps the suction seal). **Print from `prints.py`**: camera mode recognizes the tiles by comparing against exactly these pictures |
| 8 pickup slots (S1-S8) | a 4 x 2 block centered on the **green** sheet, overhanging its short ends onto the table |
| 3 exhibit spots (E1-E3) | a row across the **blue** sheet; E1 is the story's start |
| orange sheet | not used |

The sheets stay where VeriArm measured them. Each artwork has a fixed slot (`catalog.json`): S1 Starry
Night, S2 Great Wave, S3 The Kiss, S4 The Scream, S5 Paris Street, S6 Dance, S7 Girl with a Pearl
Earring, S8 Woman with a Parasol. See `python -m art_curator.calibrate map` for a picture.

## Files

| File | What it does |
|---|---|
| `catalog.json` | the 8 artworks: id, title, artist, themes, description, pickup slot |
| `curator.py` | theme -> Claude -> 3 IDs in order + title, spoken preview, statement, closing reflection, reasons. `validate()` rejects anything off. The web page also tells Claude what the last 3 exhibitions showed, so a run of demos varies |
| `exhibition.py` | turns a validated exhibition into 3 moves; tracks where every tile is; runs them |
| `tile.py` | picks / places one 2 mm tile on the real arm; hover test for the height correction |
| `find_art.py` | camera mode: finds each tile near its slot and checks it's the right artwork (like `find_box`, for tiles) |
| `prints.py` | the 10 x 10 cm prints (PDF) and the same pictures `find_art` matches against |
| `calibrate.py` | works out the 11 spots from the sheets; hover-checks them on the arm |
| `layout.json` | the 11 spots (world mm) + `calibrated`, `slot_mode`, `region_mm` |
| `narrate.py` | spoken narration (ElevenLabs, Mac voice as fallback): preview, each artwork as it's carried, statement, reflection, sign-off |
| `app.py`, `page.html` | the web page |
| `images/1.jpg` ... `8.jpg` | public-domain pictures of the artworks, for the page |
| `test_curator.py` | validator tests (no robot, no network) |
| `test_find_art.py` | `find_art` on synthetic camera photos (no robot) |
| `runtime/` | gitignored: `state.json` (where tiles are), `run.lock`, `layout.png`, `prints.pdf`, `find_art_*` pictures |

## Commands

### Curate only (no motion)
```bash
.venv/bin/python -m art_curator.curator "what does it mean to be human"
.venv/bin/python -m art_curator.curator "joy" --offline
.venv/bin/python -m art_curator.curator --check answer.json      # validate a saved answer
.venv/bin/python -m art_curator.test_curator                     # validator tests
```

### Run an exhibition from the terminal
```bash
.venv/bin/python -m art_curator.exhibition status
.venv/bin/python -m art_curator.exhibition reset                         # after putting the tiles back by hand
.venv/bin/python -m art_curator.exhibition plan "joy and freedom"        # curate + print the 3 moves, no motion
.venv/bin/python -m art_curator.exhibition run "joy and freedom" --sim   # simulated arm
.venv/bin/python -m art_curator.exhibition run "joy and freedom"         # REAL ARM, asks before every motion
.venv/bin/python -m art_curator.exhibition run "joy and freedom" --yes   # REAL ARM, no questions
```
Test flags for `--sim`: `--fail-at 2` (move 2 loses its grip), `--slots camera --missing slot_3` (the
camera can't see slot 3).

### Prints and tests
```bash
.venv/bin/python -m art_curator.prints --out ~/Desktop/art_prints.pdf   # print at 100%; check the 10 cm line
.venv/bin/python -m art_curator.test_find_art                            # camera matching on synthetic photos
```

### Web page
```bash
.venv/bin/python -m art_curator.app                    # simulated arm (default, safe)
.venv/bin/python -m art_curator.app --robot            # REAL ARM: no per-motion questions, keep someone at the e-stop
.venv/bin/python -m art_curator.app --voice            # narrate out loud (see below)
.venv/bin/python -m art_curator.app --voice-wait       # same, but the arm waits for each sentence
.venv/bin/python -m art_curator.app --host 0.0.0.0     # open it from another laptop
```
Port 8090, so it can run next to VeriArm's `ui.py` (8080). Only one of them may drive the arm at a time.
**Stop arm** (robot mode) stops the arm where it is and leaves the suction as is; the e-stop is the real
safety.

## Narration (`--voice`)

On the page (`app --voice`) or the terminal (`exhibition run ... --voice`):
1. after curating: the title and Claude's short spoken preview of the journey and its high point (the
   `preview` field: under 30 words, e.g. "When the ground shifts beneath you, where does light come from?
   Follow a path from dread to a night sky that refuses to stop shining.")
2. on Assemble: "Now assembling *title*."
3. as each tile is carried: "First / Next / And finally, *artwork*, by *artist*." + why it's there
4. once all 3 are placed: "*Title*: from *A*, through *B*, to *C*.", the full curatorial statement over
   the finished table, Claude's closing reflection (the `closing` field: one sentence about the theme, e.g.
   "Perhaps hope isn't the absence of uncertainty, but the courage to find beauty within it."), and always
   the same sign-off: "Thank you for curating with me. Until our next exhibition, stay curious!"
   (`SIGN_OFF` in `narrate.py`). The page says "All 3 placed. The curator is speaking…" until the
   closing line has been spoken, then "Exhibition assembled".
   (on a failure: "I couldn't place *artwork*. Please check the table.")

Pacing: each artwork's description plays while its tile is carried, and the next tile doesn't start until
that description has finished (the first move also waits for the preview; the closing words wait for the
last description). Each tile takes its move time or its description time (~10-15 s), whichever is longer, so
voice and arm stay together.

Voice: **ElevenLabs** when `ELEVENLABS_API_KEY` is in `.env` (default voice "George", change with
`ELEVENLABS_VOICE_ID`; list voices with `.venv/bin/python -m art_curator.narrate voices`). Right after
curating, every line of the exhibition is generated in parallel (preview first), and clips are cached in
`runtime/voice_cache/`. Without a key, or if a line can't be generated in time (no wifi), that line uses
the Mac's `say`; a bad key or used-up credits switch to `say` for the rest of the session. A better Mac
voice: download e.g. "Ava (Premium)" in System Settings > Accessibility > Spoken Content, then
`ART_MAC_VOICE=Ava (Premium)` in `.env`. Test a line: `.venv/bin/python -m art_curator.narrate say "Hello"`.

Speech never changes where or how the arm moves, only when the next tile starts. If the audio stutters
while the arm moves, use `--voice-wait` (fully sequential: the arm pauses for every sentence, then moves,
like VeriArm). `VERIARM_VOICE=0` mutes it (sentences are
still printed). The voice reads English only: other languages fall back to the artwork names.
Hear a sample: `.venv/bin/python -m art_curator.narrate`.

## Slot mode: fixed or camera

Where the arm picks each tile:

- **fixed**: at the slot's coordinates in `layout.json`. Tiles must sit on their taped outlines.
- **camera**: before any tile moves, the camera finds every tile within `region_mm` (default 30 mm) of its
  slot and the arm picks where the tile really is. A missing or doubled tile stops the run before
  anything moves, and names the slot.

The default is `"slot_mode"` in `layout.json`. `--slots fixed` / `--slots camera` overrides it for one
command (`exhibition run ...`) or for as long as the page's server runs (`app ...`). If camera mode
misbehaves at the demo: restart the page with `--slots fixed`.

How `find_art.py` works: straight-down photos over the slots (more than one if needed) are turned into a
top-down picture of the table at 1 px/mm. In each slot it searches for that slot's artwork at 12
rotations (tiles may sit sideways or upside down): the color layout gives the position, and the color
palette there confirms it (a sheet edge can mimic a painting's layout, not its colors). It also scores
the other 7 artworks, so a swapped tile is caught. It looks 4 cm beyond the region so a tile just outside
is measured and refused ("is 50 mm from its spot"), not half-matched.

Messages: `no The Kiss found` (empty slot), `looks like Dance, not The Great Wave` (wrong tile),
`is 50 mm from its spot (max 30): push it back`, `the camera couldn't see the whole slot`.

Try it on the robot without picking anything: `.venv/bin/python -m art_curator.find_art` (moves the camera,
prints every slot's score and offset). Pictures: `runtime/find_art_<n>.jpg` (what it found) and
`runtime/find_art_photo_<n>.jpg/.json` (raw photo + camera pose; re-run offline with
`find_art --image ... --view ...`).

Tested on synthetic photos only (80 tiles: all found, position error under 5 mm; empty, swapped and
too-far tiles refused). Real photos differ (glare on the tape, print colors, light): on the robot, check the
scores it prints. Real tiles should be well above `MIN_SCORE` (0.25) in `find_art.py`; empty slots near 0.

## Rules the code enforces

- Claude only chooses artwork IDs and their order. Every coordinate comes from `catalog.json` and
  `layout.json`. A curator answer that fails validation (wrong count, repeated or unknown IDs, positions not
  exactly 1/2/3, blank text, extra fields) moves nothing.
- A run needs status **ready**, and only a reset sets it. A reset means a person checked the table: all
  8 tiles in their slots, the exhibit empty.
- One run at a time (a lock file the OS releases if the process dies).
- A move counts as done only after the pick (with the wrist-camera grip check) and the place both
  succeeded. A failed move stops the run; that tile becomes "unknown"; a reset is needed.
- After a finished exhibition, a reset is needed before the next one. If a run died mid-way (crash,
  Ctrl-C), the next command sees it and requires a reset.
- Real-arm runs refuse while `layout.json` has `"calibrated": false`. Simulated runs ignore it.

## First time on the robot (in this order)

1. **Height correction.** One tile at the blue zone center:
   `.venv/bin/python -m art_curator.tile hover`. Measure the gap G (cup bottom to tile top). Set
   `TILE_Z_CORRECTION_MM = 47 - G` in `tile.py` (in general: `HOVER_MM + current value - G`).
2. **Pick test.** `.venv/bin/python -m art_curator.tile pick-test`: pick, lift 6 cm, put back.
3. **Check the 11 spots.** Tiles (or taped outlines) on every spot, then
   `.venv/bin/python -m art_curator.calibrate check`. At each hover: Enter = good, `dx dy` = nudge in mm
   (+x away from the arm base, +y to the left when standing at the base looking out), `s` skip, `q`
   stop. At the end it asks to mark the layout calibrated. Check S8 (furthest, ~63 cm) and S1/S5 (beside
   the base) first.
4. **Tape the outlines** and label them S1-S8, E1-E3, so resets are quick.
5. **Full run:** `exhibition reset`, then `exhibition run "<theme>"` (asks before each motion), then the
   web page with `--robot`.
6. **Camera mode (after fixed works):** `find_art` alone first (camera only, no picking), check the
   scores and `runtime/find_art_*.jpg`, then `exhibition run "<theme>" --slots camera`.

**Don't re-run `calibrate from-saved` or `calibrate survey` after step 3**: both rebuild `layout.json`
from scratch and drop `calibrated` and every nudge.

`calibrate survey` finds the sheets with the camera, for when they've moved (by up to ~10-20 cm; the
sheets must be empty). Then repeat step 3.

## Troubleshooting

| Message | Meaning / fix |
|---|---|
| `status is 'needs_reset'` / `'complete'` / `'failed'` | put all 8 tiles in their slots, clear the exhibit, then reset |
| `layout.json is not calibrated` | do "First time on the robot" step 3, or use `--sim` |
| `another exhibition is running` | wait, or check for another terminal / page server running a run |
| `no ANTHROPIC_API_KEY` | add it to `.env`, or use `--offline` |
| `artwork N is at 'unknown'` | a run failed while moving it: find the tile, put everything back, reset |
| `camera can't find a tile in slot_N` | camera mode: that slot is empty, the tile is too far off, or two tiles overlap the region |
| tile not picked (grip check: NOT held) | re-measure the height correction; check the tape is smooth where the cup lands |

## For agents changing this code

- Don't edit VeriArm files outside `art_curator/`; import them. Don't write `poses.json`.
- Keep the LLM out of motion: it may only change which IDs are picked and in what order. New fields from
  the model must go through `validate()`.
- Every new way to move a tile must update `runtime/state.json` through `exhibition.execute` (state is
  saved after every step).
- Test without the arm first: `test_curator`, `test_find_art`, `exhibition ... --sim`, `app` (simulated
  by default).
