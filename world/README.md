Lane A (Manas). See `lanes/A-world-manas.md`. Files: world_server.py, flight_model.py, traffic.py, scenario.py. Views live in `web/index.html`.

- `world_server.py` — physics + WebSocket hub (INTERFACE.md v1.1), serves `web/` on :8080.
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

Conflict scenarios (no avoidance; times at x1):

| Scenario | What happens without FLOCK |
|---|---|
| `harness/scenarios/base_vs_straight_in.json` | N101 turns base/final into straight-in N399: NMAC on final ~124 s |
| `harness/scenarios/three_on_final.json` | N101 (judge) + straight-in N399 NMAC on final ~124 s, N204 ~700 ft in trail |
| `harness/scenarios/head_on_judges.json` | both judges (N101 25L downwind 086°, N102 07R downwind 266°) head-on, collision ~74 s |
