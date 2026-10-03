# Lane A — Sim world and views (Manas)

Paste CONTEXT.md and INTERFACE.md first, then this file. You are the agent for Lane A.

## Start against stubs (no waiting)
Start now, without Lane B or D: run `python stubs/fake_node.py --id N101 --speed 4` against your world to get real ADVISORY/TRUST/COMMAND/PREDICTION frames for the views, and `python stubs/tail_log.py` to see what you mirror. `stubs/fake_world.py` is a minimal reference of the hub protocol — copy it as your starting point. Your task rows with times: `BOARD.md` → Lane A.

## Goal
Build the authoritative simulation world and the three browser views so two judges can fly two aircraft with PlayStation controllers inside a traffic pattern at Deer Valley (KDVT) with 6 AI aircraft, while FLOCK nodes (Lane B) connect over WebSocket and receive only their own aircraft's state.

## Deliverables (in `/world` and `/web`)
1. `world/world_server.py` — FastAPI + websockets. Runs physics at 20 Hz for all aircraft. Hub for roles `node:<id>`, `cockpit:<id>`, `god`, `log`, `channel`, `camera`, `data`. Sends OWNSHIP only to the matching node; applies COMMAND only if `ap_equipped` and `stick_active` is false; emits STICK when a controlled aircraft receives INPUT during a TAKEOVER; mirrors every ADVISORY/COMMAND/TRUST and every radio frame it is handed to role `log`.
2. `world/flight_model.py` — 3-DOF point mass. State: lat, lon, alt, ias, hdg, bank, vs. Bank-to-turn (turn rate = g·tan(bank)/V). Bank rate limit 15°/s. Climb capability = f(density altitude): 730 fpm at 0, 500 at 5,000, 300 at 8,000 ft DA (linear interpolate). Speed envelope 48–140 kt. Pressure altitude = true altitude ± a per-aircraft altimeter-setting error (±50 ft) to be realistic.
3. `world/traffic.py` — AI aircraft flying a standard left pattern on 25L: upwind → crosswind → downwind (1,000 ft AGL) → base → final → touch-and-go. Legs turn at realistic points; speeds 90–100 kt; small per-aircraft variation. Some AI aircraft marked `flock=False` (no node).
4. `world/scenario.py` — load `harness/scenarios/*.json`; spawn aircraft; a `density_altitude_override` and a `time_scale` (1× for judges, 10× for A/B runs).
5. `web/index.html` (+ `web/app.js`, `web/style.css`) — one page, role from `?role=`:
   - `cockpitA|cockpitB`: first-person chase or cockpit view, radar-style traffic display showing only what the node reports (ADVISORY/TRUST), big advisory text, trust badges (TRUSTED green, SUSPICIOUS amber, FAKE red, CAMERA_ONLY blue wedge), voice via `speechSynthesis`, Gamepad API input at 30 Hz → INPUT messages, keyboard fallback (arrows + W/S).
   - `god`: third-person view of the whole pattern: runways, all aircraft with labels, predicted paths with turns (from node PREDICTION frames if provided), conflict markers, layer ring colors (sequence/warn/negotiate/act), live METAR text, density-altitude slider that sends `{"type":"SET_DA","ft":...}` to the world.
   - `log`: Lane D owns the styling; expose the WebSocket feed and a minimal table so D can build on it.
   - Terrain: CesiumJS with a free ion token (World Terrain + OSM Buildings) centered on KDVT. Fallback: three.js with a pre-downloaded satellite image as a plane. Decide by 2 PM; do not spend more than 90 minutes on Cesium.

## Acceptance tests
- 1:00 PM: `python world/world_server.py` runs; `?role=cockpitA` shows own aircraft moving with controller input; `?role=god` shows 8 aircraft on a KDVT map.
- 4:00 PM: AI aircraft fly the full pattern repeatedly without drifting; a COMMAND with bank −28° banks the aircraft and a stick input triggers STICK; a node receives OWNSHIP at 10 Hz for its aircraft only.
- 7:00 PM: all three views run on three laptops over the phone hotspot for 10 minutes without disconnects; voice works; density-altitude slider changes climb capability visibly.

## Constraints
- No god-view data to cockpit pages. The cockpit knows only what its node told it.
- Keep the page framework-free (ES modules) unless a library saves real time; pin any CDN versions.
- Log frame rate problems early; 8 aircraft at 20 Hz physics must stay under 30% CPU.

## Nice-to-have only after acceptance
Obstacle towers and weather cells rendered in god view; Escape Crystal rendering hook (Lane D will send `{"type":"CRYSTAL","ac_id","points":[...]}`); replay mode from a recorded run.
