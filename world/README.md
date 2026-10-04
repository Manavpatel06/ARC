Lane A (Manas). See `lanes/A-world-manas.md`. Files: world_server.py, flight_model.py, traffic.py, scenario.py. Views live in `web/index.html`.

**ARC is now advisory only** (`FLOCK_claude_code_prompt.md`): the world rejects every COMMAND
(`rejected_by_world: "advisory_only"`); `--allow-takeover` / `run_demo.ps1 -Legacy` keeps the old
collision-avoidance demo. `sensors.py` simulates what each judge aircraft's own equipment receives (ADS-B,
TCAS, Mode S, ground radar) for the onboard verification unit (`verify/`, role `avionics:<id>`);
`attacks.py` injects spoofing (god view **+ Ghost**, `SET_ATTACK`, or scenario `"attacks": [{"type":"ghost","at_s":60}]`).
Ground truth goes to god + log only (`GROUND_TRUTH`). See `verify/README.md`.

- `world_server.py` — physics + WebSocket hub (INTERFACE.md v1.2), serves `web/` on :8080. Scenario weather is
  pinned: `load_metar(scenario.weather)` (`cached` = committed sample, `live` = fetched METAR). ENV (density altitude)
  goes to god and every node; LIVE_TRAFFIC (real ADS-B, data/live_traffic.py) to god + log only.
- `separation.py` — ground-truth NMAC (< 500 ft H and < 100 ft V) / COLLISION (< 60 ft, < 30 ft) monitor. The world publishes `{"type":"WORLD_EVENT","event":"NMAC"|"COLLISION"|"NMAC_END",...}` to **god + log only** (truth; never to nodes). `NMAC_END` carries the closest approach of the encounter — use it for scoring runs.
- `flight_model.py` / `traffic.py` — ground physics (rollout, braking, rotate at 55 kt), flare, full-stop landings and
  takeoffs; the cockpit AP button (`{"type":"AP","ac_id","engage":true|false|null}` -> `AP_STATUS`, Lane A, proposed for
  INTERFACE v1.2) flies LEVEL -> JOIN -> PATTERN -> ROLLOUT -> STOPPED, or TAKEOFF from a stop. World also publishes
  `WORLD_EVENT` TOUCHDOWN (`vs_fpm`, `hard` < -800 fpm) / LIFTOFF / AP_DISCONNECT.
- Scenario start `{"leg":"RUNWAY","runway":"25L","offset_s":2}` (Lane A, proposed for v1.2): stopped on the centreline;
  AI aircraft depart at once, human aircraft wait for power or AP.
- `weather.py` — world weather acting on every aircraft (truth side; nodes still only know the METAR): wind that
  strengthens/veers with height, low-level shear, gusts, turbulence (none/light/moderate/severe), drifting thermals,
  visibility / cloud base (cockpit view only), QNH. Aircraft hold INDICATED altitude with their own altimeter
  setting, so a pressure drop they haven't dialled in puts them low; transponder pressure altitude stays true.
  Presets: `metar` (default, steady wind), `calm_morning`, `hot_gusty_afternoon`, `haboob`, `low_ceiling`,
  `pressure_drop`. Start with `--weather <preset>` or scenario key `"weather_preset"`; change live from the god view
  (`SET_WX` {preset | field: value | update_altimeters}). World sends `WX` (god full, cockpits pilot-level) and
  `WX_FIELD` (thermal positions, god only, every 2 s).
- `taws.py` — GPWS-style terrain alerts for judge aircraft from own data only (AGL, smoothed sink, position vs
  runways): PULL UP, SINK RATE, TERRAIN, TOO LOW TERRAIN; quiet in runway approach / climb-out corridors and
  (terrain modes) within 1.5 NM of the field. Touchdowns classified RUNWAY / OFF_RUNWAY / TERRAIN_IMPACT.
  World sends `WORLD_EVENT` TAWS {alert} on change; cockpit frame carries `taws`.
- Turbulence is smooth swell + occasional (1 - cos) jolts, not per-frame noise; the roll it causes sits on top of
  the bank the pilot / autopilot holds; the VSI lags ~1 s. AI finals aim ~200 m past the threshold.
- `find_conflict.py` — offline scenario tuner: sweeps `offset_s` / `agl_ft` of chosen aircraft, flies the same physics without nodes, keeps the tightest encounter, writes the scenario.

```
python world/find_conflict.py --scenario harness/scenarios/base_cutoff.json --pair N101 N399 [--weather metar] \
    --vary N101.offset_s=0:120:2 --window 60:150 --legs BASE,FINAL,STRAIGHT_IN \
    --out harness/scenarios/base_vs_straight_in.json --name base_vs_straight_in
```

Conflict scenarios (no avoidance; times at x1; weather pinned with `"weather": "cached"`, real KDVT terrain;
re-check with find_conflict.py after any change to physics, terrain or pattern geometry):

| Scenario | What happens without ARC (world separation monitor) |
|---|---|
| `harness/scenarios/base_vs_straight_in.json` | N101 turns base/final into straight-in N399: NMAC on final ~149 s |
| `harness/scenarios/three_on_final.json` | N101 (judge), N204, straight-in N399 converge on final: all three pairs NMAC, 155-160 s |
| `harness/scenarios/head_on_judges.json` | both judges (N101 25L downwind 086°, N102 07R downwind 266°) head-on, collision ~76 s |

Live traffic — `harness/scenarios/live_kdvt.json` (free flight, no tuned conflict):

```
.\run_demo.ps1 -Scenario harness\scenarios\live_kdvt.json [-Seed 11]
```

- Judges: N101 (cockpit A) 4 NM north and N102 (cockpit B) 4 NM south, inbound at pattern altitude (2,500 ft), free to
  fly anywhere. Untouched they fly straight at each other through the field (ARC sequences / warns / resolves),
  then join their own pattern 2 NM past it. Start `{"leg":"INBOUND","from_deg","dist_nm","runway":"NORTH"|"SOUTH"}`.
- Runway flow from the METAR wind (`"runway_flow": "auto"`; 120/9 -> runway 07: 07R south, right traffic; 07L north,
  left traffic; calm -> 25). `NORTH` / `SOUTH` / `ACTIVE` runway names follow the flow.
- `traffic.py` TrafficGenerator keeps 5-8 AI airborne: 45-degree-entry arrivals from random directions on the pattern
  side, straight-ins, departures (straight out or 45-degree turn out, gone at 5.5 NM), touch-and-go circuits (1-3,
  then full stop). Landers taxi off (despawn) below 15 kt. Departures hold short while the runway or a 1.5 NM final is
  occupied. Fixed seed (`traffic.seed`, or `--seed` / `-Seed`) = the same spawns every run.
- AI pilots act on their own node's advice 70 % of the time, ~3 s after it arrives: EXTEND (base turn moves out),
  SLOW AND SPACE, TURN LEFT/RIGHT n, CLIMB, DESCEND; advice that reaches them on final -> go-around. They also go around
  when the runway is still occupied below 300 ft, and do last-resort see-and-avoid (turn right; go around on final)
  when traffic ahead will pass within 150 m / 300 ft in 30 s. Decisions go to the log (`kind: decision`, `ai_pilot`).
- With `--traffic-nodes ws://<channel>` (run_demo does this) the world starts a node per AI aircraft and stops it when
  the aircraft leaves; logs in `harness/out/nodes/<id>.log`, PIDs appended to `--pidfile` for `run_demo.ps1 -Stop`.
- `WORLD_EVENT` SPAWN {mission, runway, live} / DESPAWN {why} -> log; god drops aircraft that leave TRUTH.
- Demo reset: god view **⟲ Reset demo** (click twice, or Shift+R twice) sends `{"type":"RESET_DEMO"}`: both judges back at
  their start points (pattern-altitude, inbound, autopilot until a pilot touches the stick) and AI traffic within
  2 NM / 1,500 ft of those points removed. Cockpit L2+R2 reset still resets just that aircraft.
- `"live_seed": true` (optional): some arrivals spawn where real aircraft are flying right now (LIVE_TRAFFIC from
  `data/live_traffic.py`, started by run_demo); breaks exact seed replay.

Tests (Lane A):
```
python -m pytest -q world/tests      # physics, pattern AI, autopilot, ground, TAWS, weather, scenarios + server contract
node web/tests/run.mjs               # rumble patterns, reset hold, controller buttons, brakes (no browser needed)
```
