Lane A (Manas). See `lanes/A-world-manas.md`. Files: world_server.py, flight_model.py, traffic.py, scenario.py. Views live in `web/index.html`.

- `world_server.py` — physics + WebSocket hub (INTERFACE.md v1.1), serves `web/` on :8080.
- `separation.py` — ground-truth NMAC (< 500 ft H and < 100 ft V) / COLLISION (< 60 ft, < 30 ft) monitor. The world publishes `{"type":"WORLD_EVENT","event":"NMAC"|"COLLISION"|"NMAC_END",...}` to **god + log only** (truth; never to nodes). `NMAC_END` carries the closest approach of the encounter — use it for scoring runs.
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
