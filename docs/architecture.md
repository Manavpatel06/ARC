# ARC — architecture (traffic verification)

ARC is an **onboard, advisory-only traffic verification system**: for every aircraft on the traffic
display it says whether that aircraft really exists where it claims to be, with a trust score and reasons.
It never commands a maneuver, never talks to the autopilot, never transmits, and never tells a pilot to
ignore TCAS. (The earlier collision-avoidance demo still runs with `run_demo.ps1 -Legacy`.)

```
 world (truth: physics, live traffic, weather)          data/live_traffic.py --record   sources/sdr_beast.py
   │  world/attacks.py  (ghost, flock, drift, replay,          │ (real ADS-B, recorded)          │ (RTL-SDR, Beast,
   │                     masquerade, jamming, own-GPS)         ▼                                 │  receive only)
   ▼                                                    world/replay.py                         ▼
 world/sensors.py — what each judge aircraft's OWN equipment receives        normalized SensorMsg
   ADS-B 2 Hz (RSSI from the transmitter's true position) · own TCAS 1 Hz · DF11 squitters ·
   rotating ground radar → DF4/5 replies + the 1030 interrogation when the beam hits us · own GPS · band stats
   │  SENSORS frames, role avionics:<id>, 10 Hz              (ground truth never on this path)
   ▼
 verify/unit.py — the onboard unit (one per judge aircraft)
   tracks.py      per-ICAO tracks; an address heard from two places is split into two branches
   checks/        tcas_consistency · modes_presence · timing_1030 · rssi · emitter_cluster · kinematics ·
                  replay_detect · popin        → each {score | None, confidence, reason}
   fusion.py      weighted log-odds, smoothed, fast on strong evidence → trust 0–100
   guardrails.py  TCAS-confirmed never SUSPECT · thin evidence = UNVERIFIED · VERIFIED needs physical proof ·
                  SUSPECT needs a strong check · jamming / own-GPS: absence not counted, tolerances widened
   eventlog.py    signed (Ed25519), hash-chained event log; signed VERIFY frames
   │  VERIFY 1 Hz (signed; world pins the unit's key, drops forgeries)
   ▼
 world → own cockpit (traffic colours, banner, reasons, Report to ATC) · god view (truth vs verdict) · log
```

| Spec item | Where |
|---|---|
| Normalized schema | `verify/schema.py` |
| Simulator: own-ship, TCAS, Mode S, 1030, RSSI, ground radar | `world/sensors.py` (+ physics in `world/`) |
| Attack injectors, live toggles | `world/attacks.py`, god view attack row, `SET_ATTACK` |
| Track manager | `verify/tracks.py` |
| Checks 1–7 | `verify/checks/` |
| Fusion + guardrails | `verify/fusion.py`, `verify/guardrails.py`, `verify/config.json` |
| Signed event log, signed link | `verify/eventlog.py`, `world/world_server.py` (`unit_keys`) |
| Display | `web/cockpit.js` (pilot), `web/god.js` (demo control panel) |
| Evaluation | `eval/run_eval.py` → `eval/out/report.md` |
| Real data | `world/replay.py` (recorded ADS-B), `sources/sdr_beast.py` (live SDR) |

Stack: Python 3.12 + websockets + pydantic + numpy + PyNaCl; plain ES-module web pages (no build step);
JSON config. The spec suggested FastAPI + React + YAML; the existing world/web stack already does the job,
is tested, and keeps the demo laptop dependency-light.
