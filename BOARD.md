# BOARD — who is doing what, right now

**This file is the single source of truth for status.** Update your row in the same commit as the work (`[ ]` → `[~]` in progress → `[x]` done, or `[!]` blocked + one line why). Push at least hourly and before 1 PM / 7 PM status reports. If you finish your list, take the next item from your lane's queue, then from **Pull Queue** at the bottom. Nobody sits idle; nobody waits on another lane — every task below names the stub that stands in until the real thing lands.

Legend: `[ ]` todo · `[~]` in progress · `[x]` done · `[!]` blocked (say why, ping the owner, pick the next task)

## The rule that keeps four people parallel
Every lane develops against `stubs/` until the real module exists, then swaps in one line:

| You need | Until it exists, use | Real thing (owner) |
|---|---|---|
| A world sending OWNSHIP / applying COMMAND / mirroring LOG | `python stubs/fake_world.py` | `world/world_server.py` (Manas) |
| A node emitting ADVISORY / COMMAND / TRUST / PREDICTION | `python stubs/fake_node.py --id N101 --speed 4` | `node/node.py` (Reya) |
| A radio client with `send()` / `on_message()` | `from stubs.loopback_radio import RadioClient` | `radio/client.py` same API (Mansi) |
| A log view | `python stubs/tail_log.py` | `web/log.html` (Manav) |
| Terrain / METAR / runways files | `data/cache/*.sample.json` committed in repo (flat terrain, DA 4,980 ft) | `data/*.py` fetchers (Manav) |

Swap rule: when a real module passes its acceptance test, its owner posts "REAL: <module> on main" in the team chat and marks the row `[x]` here. Everyone else pulls and switches off the stub. If the real one breaks, switch back to the stub and keep moving; don't wait for the fix.

---

## Lane A — Sim world & views · Manas · branch `lane-a-world`
Stubs to run while building: `stubs/fake_node.py` (gives views real ADVISORY/TRUST/COMMAND traffic), `stubs/tail_log.py`.

| # | Task | Target | Status |
|---|---|---|---|
| A1 | `world/world_server.py`: WebSocket hub with `?role=`, OWNSHIP to own node only, mirror ADVISORY/COMMAND/TRUST to cockpit/god/log, TRUTH to god+channel. Start by copying `stubs/fake_world.py`. | 12:30 | [ ] |
| A2 | `world/flight_model.py`: 3-DOF, bank-to-turn, bank rate 15°/s, climb vs density altitude, speed envelope. | 1:00 PM | [ ] |
| A3 | `web/index.html?role=cockpitA`: own aircraft moving, Gamepad API → INPUT at 30 Hz, keyboard fallback. Test with `fake_node`. | 1:00 PM ✔ status | [ ] |
| A4 | `web/index.html?role=god`: KDVT map (flat image OK), all aircraft, labels. | 1:00 PM ✔ status | [ ] |
| A5 | `world/traffic.py`: AI pattern traffic on 25L, correct legs, 90–100 kt, `flock=False` and `camera=True` flags. | 2:30 | [ ] |
| A6 | Cesium terrain or three.js fallback — **decide by 2 PM, max 90 min**. | 3:30 | [ ] |
| A7 | COMMAND applied only if `ap_equipped` and no stick; STICK event during takeover; RELEASE handled. | 4:00 PM | [ ] |
| A8 | Cockpit: big advisory text, voice (`speechSynthesis`), trust badges, radar display from node TRUST only. | 4:00 PM ✔ status | [ ] |
| A9 | God view: predicted paths from PREDICTION frames, conflict markers, layer rings, METAR text, DA slider → `SET_DA`. | 5:30 | [ ] |
| A10 | `world/scenario.py`: load `harness/scenarios/*.json`, `time_scale`, `density_altitude_override`. | 6:00 | [ ] |
| A11 | Three views stable on three laptops over the hotspot. | 7:00 PM ✔ status | [ ] |
| A12 | *Phase 2:* god-view overlay for trust state (FAKE drawn hollow red, CAMERA_ONLY as bearing wedge, not a dot). | 9:00 PM | [ ] |
| A13 | *Phase 3 support:* render CRYSTAL frames (Escape Crystal) around own aircraft in god view. | after go/no-go | [ ] |
| A14 | *Queue:* replay mode — play a `harness/out/*.jsonl` log back into the views for the backup video. | queue | [ ] |

## Lane B — Node logic & evidence · Reya · branch `lane-b-node`
Stubs to run while building: `stubs/fake_world.py` (OWNSHIP in, COMMAND applied), `stubs.loopback_radio.RadioClient`, `stubs/tail_log.py`, `data/cache/*.sample.json`.

| # | Task | Target | Status |
|---|---|---|---|
| B1 | `node/node.py`: 10 Hz loop, connect `node:<id>`, read OWNSHIP, broadcast STATE via RadioClient, track table of peers. | 12:30 | [ ] |
| B2 | `node/geometry.py`: lat/lon ↔ ENU around KDVT; pattern leg polygons for 25L from `runways_kdvt.sample.json`. | 1:00 PM | [ ] |
| B3 | Straight-line CPA vs one peer → TRAFFIC advisory (text + reason). | 1:00 PM ✔ status | [ ] |
| B4 | `node/predict.py`: leg classifier with confidence; turn-aware 90 s prediction; `< 0.6` → straight-line fallback. | 3:00 | [ ] |
| B5 | `node/conflict.py`: predicted miss < 500 ft H & < 100 ft V within 90 s, with sigma; ttc. | 3:00 | [ ] |
| B6 | `node/layers.py`: SEQUENCE ≤90 / TRAFFIC ≤35 / RESOLVE ≤20 / TAKEOVER ≤8 with hysteresis; SEQ_PROPOSE/ACCEPT. | 4:00 PM ✔ status | [ ] |
| B7 | `node/negotiate.py`: lower ID commits first, complementary sense; event-driven MANEUVER_COMMIT. | 4:00 PM ✔ status | [ ] |
| B8 | `node/escape.py`: Escape Field — candidates × 30 s forward sim × traffic/terrain/obstacles/performance; reason with rejected list. | 5:00 | [ ] |
| B9 | `node/authority.py`: bounds monitor (separate module), NO_SOLUTION path, RELEASE on STICK within one tick. | 6:00 | [ ] |
| B10 | `harness/montecarlo.py`: FLOCK vs straight-line chart → `harness/out/flock_vs_baseline.png`. | 7:00 PM ✔ status | [ ] |
| B11 | *Phase 2:* `node/trust.py` consuming Lane C evidence; TRUSTED-only may RESOLVE/TAKEOVER; CAMERA_ONLY = right-of-way only. | 8:30 PM | [ ] |
| B12 | *Phase 2:* lost link after commit → consistent timeout, both default right; three-on-final scenario. | 10:00 PM | [ ] |
| B13 | *Phase 3:* Marana replay scenario `harness/scenarios/marana_2025.json`. | after go/no-go | [ ] |
| B14 | *Queue:* out-of-sample validation on recorded KDVT ADS-B (OpenSky) if account arrives. | queue | [ ] |

## Lane C — Radio, protocol, security · Mansi · branch `lane-c-radio`
Stubs to run while building: `stubs/fake_world.py` (TRUTH feed for the channel), `stubs/loopback_radio.py` as the reference API, `stubs/tail_log.py`.

| # | Task | Target | Status |
|---|---|---|---|
| C1 | `radio/client.py`: same API as `stubs/loopback_radio.RadioClient` + `--via-channel` transport. Validate every message with `schemas.RadioMsg`. | 12:30 | [ ] |
| C2 | `radio/channel.py`: connect as `channel`, read TRUTH, range cutoff 4,828 m, loss, latency; mirror delivered/dropped + reason to log. | 1:00 PM ✔ status | [ ] |
| C3 | Test both transports on the hotspot (multicast vs via-channel). **Report which works by 1 PM.** | 1:00 PM ✔ status | [ ] |
| C4 | `radio/crypto.py`: Ed25519 keypair, sign/verify, KEYS exchange at boot, seq monotonic, ±2 s window; rejections logged. | 3:00 | [ ] |
| C5 | HEARTBEAT 0.5 Hz; lost-link flag within 3 s. | 4:00 PM ✔ status | [ ] |
| C6 | `radio/spoofer.py`: GHOST7 unsigned / unknown key / impossible kinematics; `--sybil 3`. | 5:30 | [ ] |
| C7 | `radio/slots.py`: AIS-style self-organizing slots; collision-rate number at 8 and 30 senders, with/without. | 7:00 PM ✔ status | [ ] |
| C8 | *Phase 2:* `radio/evidence.py`: plausibility, emulated RSSI/Doppler consistency, peer corroboration, signature, camera flag → score + evidence strings. | 9:00 PM | [ ] |
| C9 | *Phase 2:* `radio/faults.py`: drop a specific COMMIT, kill heartbeat 5 s, latency spike 2 s. | 10:00 PM | [ ] |
| C10 | *After 7 PM go/no-go only:* ESP32-C6 node over Wi-Fi UDP speaking the envelope; "over the air" tag in log. | optional | [ ] |
| C11 | *Queue:* one-page BOM + band/link budget for the pitch (judge ask #9). | queue | [ ] |

## Lane D — Integration, data, log, camera, pitch · Manav · branch `lane-d-integration`
Stubs to run while building: `stubs/fake_world.py` + `stubs/fake_node.py --speed 4` (gives the log page real frames).

| # | Task | Target | Status |
|---|---|---|---|
| D1 | `data/metar.py` → `metar_kdvt.json` + density altitude; `data/runways.py` → `runways_kdvt.json`. Replace the `.sample` files. | 12:30 | [ ] |
| D2 | `web/log.html`: live LOG table with colors by type; filters per aircraft; runs against stubs. | 1:00 PM ✔ status | [ ] |
| D3 | Hotspot test: four laptops reach the world server; write the IP in team chat. | 12:00 | [ ] |
| D4 | `data/terrain.py` + `data/obstacles.py` → grid + CSV; `elev_at(lat, lon)`. | 3:00 | [ ] |
| D5 | Explain panel: click a decision → `reason` rendered (miss, ttc, method, confidence, chosen/rejected, trust evidence, negotiation transcript). | 4:00 PM ✔ status | [ ] |
| D6 | `data/camera.py`: ArUco toy plane → SIGHTING at ≥ 5 Hz; test under room lights. | 4:30 | [ ] |
| D7 | `run_demo.sh` / `.ps1`: start everything in order with the hotspot IP. | 6:00 | [ ] |
| D8 | Full run on the hotspot; **backup video recorded**; status-report demo. | 7:00 PM ✔ status | [ ] |
| D9 | *Phase 2:* spoof demo script + log highlights; judge round 2 at ~midnight. | 11:00 PM | [ ] |
| D10 | *Phase 3:* `node/crystal.py` Escape Crystal + MFI → CRYSTAL frames. | after go/no-go | [ ] |
| D11 | *Phase 3:* `node/parallax.py` Collective Parallax (two SIGHTING bearings → position). | after go/no-go | [ ] |
| D12 | Pitch deck + 5-min script, four speakers × 70 s; wording fixes from `docs/judge-round-1.md`. | Sat night | [ ] |

---

## Pull Queue — unowned tasks; take one if your lane list is empty (write your name)
| Task | Taken by | Status |
|---|---|---|
| `harness/scenarios/three_on_final.json` (three aircraft converging on 25L final) | | [ ] |
| `harness/scenarios/straight_in_misclassified.json` (straight-in that looks like base) | | [ ] |
| `harness/scenarios/spoof_on_final.json` (GHOST7 appears at 1 mi final) | | [ ] |
| Airport diagram check: KDVT runway ends/headings/TPA vs OurAirports (write result in CONTEXT.md) | | [ ] |
| Q&A sheet: 15 likely judge questions with 2-line answers (`docs/qa.md`) | | [ ] |
| Status-report one-pager template for 1 PM and 7 PM (`docs/status.md`) | | [ ] |
| Glossary for the slide footer (GA, TCAS, RA/TA, ADS-B, NMAC, CPA, DA) | | [ ] |
| OBS set up on the recording laptop; test 30 s capture of all four windows | | [ ] |

## Blocked right now
(Write here: who, on what, since when, who can unblock. Delete when cleared.)
