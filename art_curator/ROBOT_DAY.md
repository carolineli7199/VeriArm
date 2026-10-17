# Robot day: AI Art Curator checklist

Everything below runs from the repo root (`~/Documents/robotics-hackathon`, branch `feature/art-curator`).
`py` = `.venv/bin/python`. Every motion asks first unless `--yes`. **Someone at the e-stop, always.**
Package sorting (VeriArm) is untouched: the sheets stay where they are, `poses.json` is never written.

## Before the robot (no arm needed)

- [ ] Print the tiles: `py -m art_curator.prints --out ~/Desktop/art_prints.pdf`, **100% / actual size**;
      the ruler line on each page must measure exactly 10 cm
- [ ] Cut on the grey lines, center each print on its 11 x 11 cm plastic back, clear tape over the top
      (smooth where the suction cup lands: the center)
- [ ] Label stickers / tape: S1-S8 for the slots, E1-E3 for the exhibit row
- [ ] `.env` has `VIAM_*`, `ANTHROPIC_API_KEY`, `ELEVENLABS_API_KEY`
- [ ] Sanity check, simulated arm: `py -m art_curator.app --voice` -> http://localhost:8090, one exhibition
      by voice, end to end. Then Ctrl-C it (only one program may drive the arm)
- [ ] Laptop charger, volume up, (optional) external speaker

## On the robot, in order

| # | Step | Command | Done when |
|---|---|---|---|
| 1 | Connection | `py connect_test.py` | arm, gripper, camera listed |
| 2 | **Height correction** (one tile at the blue zone center) | `py -m art_curator.tile hover` | gap G measured with a ruler (cup bottom to tile top); set `TILE_Z_CORRECTION_MM = 47 - G` in `tile.py` |
| 3 | Verify the correction (optional, 2 min) | `py -m art_curator.tile hover` again | gap now ~30 mm |
| 4 | **Pick test** | `py -m art_curator.tile pick-test` | tile lifts 6 cm, goes back down, released; console says `HELD` |
| 5 | **Reach + calibration**: a tile (or outline) on every spot | `py -m art_curator.calibrate check` | all 11 hovered, nudged (`dx dy` mm), marked calibrated. **Do S8 (furthest, ~63 cm) and S1/S5 (beside the base) first** |
| 6 | Tape the 11 outlines on the table, labeled | | resets are "tile on its label" |
| 7 | One full move, slot to exhibit | `py -m art_curator.tile move X1 Y1 X2 Y2` (S1 -> E1 coords from `layout.json`) | tile lands inside E1's outline |
| 8 | **First exhibition, step by step** | `py -m art_curator.exhibition reset` then `py -m art_curator.exhibition run "joy"` | 3 tiles in E1-E3 in order; **time one tile** (stopwatch: pick start to arm risen) |
| 9 | Full narrated run | `py -m art_curator.exhibition reset`, then `py -m art_curator.exhibition run "<theme>" --yes --voice` | voice and arm in step; tile visible while its detail is spoken |
| 10 | The page, real arm | `py -m art_curator.app --robot --voice` | theme by voice -> curate -> Assemble -> sign-off; Stop button works |
| 11 | **Camera, look only** (tiles on slots, some a bit off / turned) | `py -m art_curator.find_art` | all 8 found; scores printed; check `art_curator/runtime/find_art_*.jpg` and the offsets vs reality |
| 12 | Camera, full run | `py -m art_curator.exhibition run "<theme>" --slots camera` | picks land on the tile centers even when tiles are off by 2-3 cm |
| 13 | Failure drills | empty a slot (camera mode) / press Stop mid-move / tile swapped | clear message, nothing moves after it, reset works |
| 14 | Decide the demo setup | set `"slot_mode"` in `layout.json`, voice on | 2-3 full rehearsals incl. resets |
| 15 | Package-sorting fallback still works | `py main.py` (VeriArm) on a box | sorts as before |

**Don't** run `calibrate from-saved` / `calibrate survey` after step 5: they rebuild `layout.json` and
drop the calibration and every nudge.

## Parameters to tune

### Motion (`art_curator/tile.py`)

| Parameter | Now | What it does | Change it when |
|---|---|---|---|
| `TILE_Z_CORRECTION_MM` | 17 (guess, errs high) | real suction tip vs. what the config thinks | **set in step 2**: `HOVER_MM + current value - G` |
| `PRESS_MM` | 3 | how far past the tile top the cup pushes | no seal / `NOT held` -> 5; arm faults or the tile skids -> 2 |
| `GRIP_SETTLE_S` | 2.0 | wait after suction on, before lifting | tile drops on lift -> 3 |
| `LIFT_MM` | 60 | tile height while carried | tile brushes other tiles / the arm swings low -> 80-100 |
| `PUT_DOWN_MM` | 3 | release height above the table | tile slides on release -> 1-2; cup hits the table -> 5 |
| `PLACE_BACK_OFF_MM` | 120 | how high the arm rises after placing (so the art is visible while described) | still blocks the view -> 150-200; slow -> 80 |
| `HOVER_MM` | 30 | hover-test height | (leave) |

### Layout (`art_curator/layout.json`, edited by `calibrate check`)

| Parameter | Now | What it does | Change it when |
|---|---|---|---|
| slot / exhibit `x`, `y` | draft from the sheets | where every tile sits | nudged in step 5 |
| `calibrated` | false | real-arm runs refuse until true | set by `calibrate check` |
| `slot_mode` | fixed | `fixed` or `camera` default for runs | step 14 |
| `region_mm` | 40 | camera mode: how far a tile may sit from its spot (x and y) | tiles placed sloppier -> 50; neighbours get confused -> 30 |
| `COL_MM` / `GRID_MM` (in `calibrate.py`) | 125 / 140 | slot spacing on green | S8 out of reach -> tighten to ~120 / 130 and re-check |

### Camera (`art_curator/find_art.py`; read the scores it prints in step 11)

| Parameter | Now | What it does | Change it when |
|---|---|---|---|
| `MIN_SCORE` | 0.25 | below this: "no *artwork* found" | real tiles score < 0.4 -> 0.15-0.2; an empty slot scores > 0.15 -> 0.3 |
| `ID_MARGIN` | 0.10 | how clearly another artwork must win to call a tile "wrong" | false "looks like X" -> 0.15 |
| `SEARCH_EXTRA_MM` | 40 | looks this far past the region | (leave) |
| `MAX_PHOTOS` | 5 | photos to cover all slots | "couldn't see the whole slot" -> 6-7 |
| glare | | clear tape reflects lights | if scores are low on shiny tiles: tilt / move the lamp, matte tape |

### Voice and pacing

| Parameter | Where | Now | Change it when |
|---|---|---|---|
| pacing | `--voice` / `--voice-wait` | voice during moves, next tile waits | audio stutters while moving -> `--voice-wait` |
| voice | `.env` `ELEVENLABS_VOICE_ID` | George | prefer another: `py -m art_curator.narrate voices` |
| clip timeout | `narrate.py` `SYNTH_TIMEOUT_S` | 20 s | slow venue wifi: lower to 8 so the Mac voice steps in sooner |
| description length | `curator.py` prompt ("under 25 / 20 words") | ~15 s per artwork | exhibition too long -> "under 15 words" |
| curator speed | `curator.py` `EFFORT` | low (~7 s) | (leave) |
| mic pause | `page.html` `SILENCE_MS` | 1.5 s | cuts people off -> 2000; waits too long -> 1200 |
| mic loudness | `page.html` `SPEECH_RMS` | 0.02 | "Didn't hear anything" in a quiet room -> 0.01 |

## Numbers to write down tomorrow

- Height correction G: ____ mm -> `TILE_Z_CORRECTION_MM` = ____
- One tile, pick to risen: ____ s (estimate was ~25 s) -> whole exhibition ~____ min
- Camera scores, real tiles: lowest ____ ; empty slot: highest ____
- Camera offsets vs reality: worst ____ mm
- Furthest spot reached: S8 yes / no
