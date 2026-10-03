Lane A (Manas). See `lanes/A-world-manas.md`. Files: world_server.py, flight_model.py, traffic.py, scenario.py. Views live in `web/index.html`.

- `world_server.py` — physics + WebSocket hub (INTERFACE.md v1.1), serves `web/` on :8080.
- `separation.py` — ground-truth NMAC (< 500 ft H and < 100 ft V) / COLLISION (< 60 ft, < 30 ft) monitor. The world publishes `{"type":"WORLD_EVENT","event":"NMAC"|"COLLISION"|"NMAC_END",...}` to **god + log only** (truth; never to nodes). `NMAC_END` carries the closest approach of the encounter — use it for scoring runs.
- `flight_model.py` / `traffic.py` — ground physics (rollout, braking, rotate at 55 kt), flare, full-stop landings and
  takeoffs; the cockpit AP button (`{"type":"AP","ac_id","engage":true|false|null}` -> `AP_STATUS`, Lane A, proposed for
  INTERFACE v1.2) flies LEVEL -> JOIN -> PATTERN -> ROLLOUT -> STOPPED, or TAKEOFF from a stop. World also publishes
  `WORLD_EVENT` TOUCHDOWN (`vs_fpm`, `hard` < -800 fpm) / LIFTOFF / AP_DISCONNECT.
- Scenario start `{"leg":"RUNWAY","runway":"25L","offset_s":2}` (Lane A, proposed for v1.2): stopped on the centreline;
  AI aircraft depart at once, human aircraft wait for power or AP.
- `find_conflict.py` — offline scenario tuner: sweeps `offset_s` / `agl_ft` of chosen aircraft, flies the same physics without nodes, keeps the tightest encounter, writes the scenario.

```
python world/find_conflict.py --scenario harness/scenarios/base_cutoff.json --pair N101 N399 \
    --vary N101.offset_s=0:120:2 --window 60:150 --legs BASE,FINAL,STRAIGHT_IN \
    --out harness/scenarios/base_vs_straight_in.json --name base_vs_straight_in
```

Conflict scenarios (no avoidance; times at x1):

| Scenario | What happens without FLOCK |
|---|---|
| `harness/scenarios/base_vs_straight_in.json` | N101 turns base/final into straight-in N399: NMAC on final ~124 s |
| `harness/scenarios/three_on_final.json` | N101 (judge) + straight-in N399 NMAC on final ~124 s, N204 ~700 ft in trail |
| `harness/scenarios/head_on_judges.json` | both judges (N101 25L downwind 086°, N102 07R downwind 266°) head-on, collision ~74 s |
