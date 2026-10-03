# FLOCK — Build plan, Saturday Oct 3 → Sunday Oct 4, 2026

Work stops Sunday 11:00 AM. Presentations noon. Status reports Sat 1:00 PM and 7:00 PM (mandatory).

## Phase 1 — Core avoidance ("ACAS core"). Target: green by Sat 7:00 PM
Definition of green: two judges' aircraft (controllers) + 6 AI aircraft fly the KDVT pattern; nodes exchange STATE over the emulated radio; a conflict is sequenced at ~90 s, warned at ~35 s, negotiated with complementary maneuvers at ~20 s, and if ignored the autopilot-equipped aircraft is taken over within printed bounds at ~8 s and released on stick input; every step shows in the comms log with a reason; the Monte Carlo chart exists.

| Time | Milestone | Lane A (Manas) | Lane B (Reya) | Lane C (Mansi) | Lane D (Manav) |
|---|---|---|---|---|---|
| 11:30 | Contract frozen | Agree INTERFACE.md + schemas.py together (15 min). Repo pushed. | | | |
| 1:00 PM status | Skeleton alive | World server runs physics for 8 aircraft; cockpit page shows own aircraft + controller input; god view draws aircraft on a KDVT map (terrain can be flat for now) | node.py connects, receives OWNSHIP, broadcasts STATE, classifies its own leg, emits a TRAFFIC advisory on straight-line CPA | channel.py relays STATE between nodes with range cutoff + loss; schema validation on every message | METAR cached and density altitude computed; runways_kdvt.json; log page shows LOG frames live; one-line pitch ready for status report |
| 4:00 PM | Loop closes | AI pattern traffic flies legs correctly; bank-to-turn + climb limits from density altitude; COMMAND applied only if ap_equipped and no stick | Turn-aware prediction (predict the next leg's turn); four layers with timings; negotiation (lower ID commits first, complementary sense); Escape Field scoring candidates against traffic + performance | Signed envelopes + seq/time window; HEARTBEAT and lost-link detection → fallback flag | Terrain + obstacle grids loaded into node constraints; explain panel renders ADVISORY.reason and COMMAND.reason; voice via speechSynthesis in cockpit page |
| 7:00 PM status | Phase 1 green | Cesium terrain or three.js fallback; three views stable on 3 laptops over the hotspot | Authority protocol + bounds monitor; NO_SOLUTION path; harness: FLOCK vs straight-line chart | Spoof injector exists (unsigned GHOST7); congestion knob | End-to-end run recorded on video; status-report demo driven by Manav |


## Dependency map — what each lane needs from whom, and what stands in until then
Nobody blocks on anybody. Each arrow has a stub in `stubs/` that speaks the frozen contract; build against the stub, swap when the real module is announced "REAL on main" in team chat and marked `[x]` in `BOARD.md`.

| Consumer | Needs | From | Stand-in until then | Real thing expected |
|---|---|---|---|---|
| B node | OWNSHIP in, COMMAND applied, STICK | A world | `stubs/fake_world.py` | A1/A7 by 4 PM |
| B node | `RadioClient.send/on_message` | C radio | `stubs/loopback_radio.py` (same API) | C1 by 12:30 |
| B node | `elev_at`, METAR DA, runway geometry | D data | `data/terrain.py` flat sample + `data/cache/*.sample.json` | D1 12:30, D4 3 PM |
| B node | trust evidence score | C evidence | hard-code TRUSTED for all peers until Phase 2 | C8 by 9 PM |
| A views | ADVISORY / TRUST / COMMAND / PREDICTION frames | B node | `stubs/fake_node.py --speed 4` | B3 1 PM, B6 4 PM |
| A world | nothing — A is the root; A tests with `fake_node` and `tail_log` | — | — | — |
| C channel | TRUTH positions (role `channel`) | A world | `stubs/fake_world.py` sends TRUTH | A1 by 12:30 |
| D log page | LOG frames | A world + any sender | `stubs/fake_world.py` + `stubs/fake_node.py` | A1 by 12:30 |
| D run_demo | all processes with CLI flags `--world`, `--id`, `--scenario` | A, B, C | start stubs in the same order | 6 PM |
| Everyone | `schemas.py` | repo | already there | frozen |

CLI convention (so `run_demo.sh` can start anything): every process accepts `--world ws://<ip>:8765`; nodes accept `--id`; world accepts `--scenario`; channel accepts `--loss --latency`.

## If a lane runs dry
Finish your lane list → take the next *Phase 2* row in your lane → take a **Pull Queue** item in `BOARD.md` → pair with the lane that is furthest behind its next ✔ status row. Say what you took in team chat; write your name in the table.

## Phase 2 — Radio trust ("radio spoofing & channel"). Sat 7:00 PM → ~11:00 PM
- Trust engine: kinematic plausibility (speed ≤ 200 kt, accel ≤ 0.5 g, turn rate ≤ 10°/s), Doppler/RSSI consistency (emulated from channel truth with noise), peer corroboration count (≥2 peers hearing it from different bearings) → TRUSTED / SUSPICIOUS / FAKE. Suspicious = warnings only; Fake = shown, never acted on.
- Spoof demo: GHOST7 appears on final; trust collapses; the judge's aircraft never maneuvers; log shows why. Sybil variant (3 fake IDs) shown as a known limit.
- AIS-style self-organizing slots (`radio/slots.py`): each node picks a 1-s slot by hashing its ID, moves on collision; show packet-collision rate before/after at 30 aircraft.
- Lost link after a MANEUVER_COMMIT: commits time out consistently; both default to right turn; show in log.
- Judge round 2 at ~midnight (blind, fresh judges) once Phase 2 is green.

## Phase 3 — Add-ons from FLOCK-X SENTINEL. One at a time, only if 1 and 2 are green

1. **Escape Crystal + MFI** (Manav): sample the reachable set (bank × vs × 10 s), subtract terrain/obstacles/traffic tubes; MFI = safe/reachable; render the crystal around the aircraft in the god view; MFI collapse as an extra trigger.

2. **Marana replay** (Reya): reconstruct WPR25FA097 geometry (stop-and-go C172 + go-around Lancair) as a scenario; run baseline vs FLOCK; report lead time honestly.

3. **Constraint Exchange / 4D contracts** (Mansi + Reya): SAFE_SET and CONTRACT messages; contract violation widens the target's uncertainty.

4. **RED FLOCK lite** (Reya): random/evolutionary search over scenario parameters to minimize separation; failures saved as regression scenarios.

5. **Threat Tubes** (Reya): widen predicted path uncertainty with staleness and non-compliance.

6. **Dragonfly attention** (Reya): full prediction only for the k riskiest tracks.

## Sunday
- 7:00–9:00 AM: freeze code; backup video final; slides final; Q&A drill, all four speak, 70 s each.
- 9:00–11:00 AM: two full rehearsals with the clock on the presentation machine and the hotspot; controllers tested; submit by 11:00.

## Cut (not planned)
Computer vision — camera node, Collective Parallax, LGMD — is out of the build (see BOARD.md Parking lot). Camera corroboration is not part of the trust engine; trust = plausibility + RF consistency + peer corroboration + signature.

## Go/no-go rules
- 1:00 PM: if a lane has no skeleton, pair the two nearest lanes on it for one hour.
- 7:00 PM: Phase 1 not green → cut Phase 3 entirely; Phase 2 reduced to trust engine + spoof demo only.
- Midnight: any add-on not demoable in 60 s is cut.
