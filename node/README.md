Lane B (Reya). See `lanes/B-node-reya.md`. Phase 3: crystal.py, parallax.py (Manav).

| File | What it does |
|---|---|
| `node.py` | The node. `Node` is a clock-free class (time = `OWNSHIP.t`) so the same code runs against the world, the stubs and the harness; `run()` is the asyncio shell (`python node/node.py --id N101 -v`). |
| `geometry.py` | Thin wrapper over the shared `pattern.py` (086/266 true): runway frame, along/cross-track per leg, remaining path, `place()`. |
| `predict.py` | Leg classifier with confidence (mid-turn aware), turn-aware 90 s prediction (`PatternFollower`), INTENT timing, straight-line fallback below 0.6. |
| `conflict.py` | Pairwise NMAC-box test (500 ft / 100 ft) on a 0.25 s grid with sigma taken off; k = 6 nearest. |
| `layers.py` | SEQUENCE 90 / TRAFFIC 35 / RESOLVE 20 / TAKEOVER 8 s with hysteresis; advisory text; sequencing plan. |
| `escape.py` | Escape Field: bank L/R 20/30/45, climb (density altitude), descend, hold; 30 s forward sim; terrain/performance/bounds; rejected list with reasons. |
| `negotiate.py` | Lower ID commits first, higher ID pre-computes and answers; lost link falls back to R30 on both sides. |
| `authority.py` | The bounds monitor. Imports nothing from predict/escape. Clips to 30 deg, 10 s, 62 kt, 300 ft AGL on final, TPA-300; releases on stick. |
| `trust.py` | TRUSTED >= 0.7 / SUSPICIOUS / FAKE; `rel` for the cockpit radar. Peers are TRUSTED until Lane C's evidence lands. |
| `tests/` | `python -m pytest node/tests -q` |

Evidence: `python harness/accept_b.py` (1 PM / 4 PM / 7 PM checks), `python harness/montecarlo.py` (chart).
