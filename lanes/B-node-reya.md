# Lane B — ARC node logic and evidence (Reya)

Paste CONTEXT.md and INTERFACE.md first, then this file. You are the agent for Lane B.

## Start against stubs (no waiting)
Start now, without Lane A, C or D: run `python stubs/fake_world.py` for OWNSHIP/COMMAND/STICK, import `from stubs.loopback_radio import RadioClient` (identical API to the coming `radio/client.py`), use `data/terrain.elev_at` (flat sample) and `data/cache/*.sample.json`. Hard-code every peer TRUSTED until Phase 2. Your task rows with times: `BOARD.md` → Lane B.

## Contract v1.1 (Sat 12:20 PM) — what changes for you
- **TRUST targets must carry `rel`** `{brg_deg (TRUE own→target), rng_m, dalt_ft (target−own), trk_deg, vs_fpm}` computed from the peer's last STATE — the cockpit radar draws only this. Send TRUST at ≥ 1 Hz.
- **Geometry is shared:** `pattern.py` gives `to_enu/from_enu`, `legs(runway)` (ENU segments + headings + altitudes) and `place()`. B2 `node/geometry.py` should wrap it, not redefine the pattern. KDVT headings are **086°/266° TRUE**; 25L left traffic is south of the field (downwind 086, base 356, final 266).
- **Weather/terrain:** `data.metar.load()` (DA 4,083 ft sample), `data.metar.climb_fpm(da)`, `data.terrain.elev_at_ft()`. OWNSHIP `gs_kt/track_deg` now include wind; predict with ground velocity, decide maneuvers with air data.
- Optional `PREDICTION` frames now have a model (`schemas.Prediction`) — the god view draws them.

## Goal
Build the node: one Python process per aircraft that reads only OWNSHIP, hears peers over the radio, predicts turns, detects conflicts, responds in four layers, picks maneuvers with the Escape Field, takes and releases control within printed bounds, and explains everything. Then prove it with a Monte Carlo chart against a straight-line baseline.

## Deliverables (in `/node` and `/harness`)
1. `node/node.py` — asyncio main loop at 10 Hz. Connects to world as `node:<id>` and to the radio (`radio/client.py` from Lane C). Maintains tracks for peers (last STATE/INTENT, age, trust from `trust.py`). Emits ADVISORY, COMMAND, TRUST, and optional PREDICTION frames.
2. `node/geometry.py` — thin wrapper over `pattern.py` (ENU, `legs()`); leg polygons for 25L (left traffic) and 25R (right traffic) built from those segments ± a corridor width.
3. `node/predict.py` — **turn-aware prediction**. Classify leg from position, track and altitude relative to the runway (UPWIND/CROSSWIND/DOWNWIND/BASE/FINAL/STRAIGHT_IN/GO_AROUND/UNKNOWN) with a confidence. Predict the next 90 s *including the turn to the next leg* (turn point from pattern geometry; turn radius from speed and 20° bank). Use the peer's declared INTENT when present. **If confidence < 0.6, fall back to straight-line prediction with wider uncertainty.** Output: list of (t, x, y, z, sigma).
4. `node/conflict.py` — pairwise predicted closest approach for own vs each peer (k nearest, k=6). Conflict = predicted miss < 500 ft horizontal and < 100 ft vertical within 90 s, accounting for sigma. Returns ttc and miss.
5. `node/layers.py` — escalation by ttc: ≤90 s SEQUENCE, ≤35 s TRAFFIC, ≤20 s RESOLVE, ≤8 s TAKEOVER. Sequencing: for same-runway pattern conflicts, propose order by distance-to-threshold; lower ID proposes (SEQ_PROPOSE), peer accepts; advisory text "NUMBER 2 - EXTEND DOWNWIND 15 S". Hysteresis so levels do not flap.
6. `node/escape.py` — **Escape Field**. Candidates: bank L/R 20/30/45°, climb at available rate, descend 500 fpm, hold. Forward-simulate 30 s with own performance at current density altitude (from `data/cache/metar_kdvt.json`). Score each: traffic (min miss vs all peers' predicted paths and committed maneuvers), terrain floor (from `data/cache/terrain_kdvt.npy`), obstacles, performance feasibility, airspace floor. Pick the best feasible; return reason with rejected candidates and why.
7. `node/negotiate.py` — lower ID commits first (MANEUVER_COMMIT) with the best sense; higher ID picks the best sense given the peer's commit. Event-driven: send immediately on decision. Lost link (no HEARTBEAT 3 s or no ACK) → fallback sense R for both.
8. `node/authority.py` — **bounds monitor**, separate module, no imports from predict/escape. Checks every COMMAND against Bounds (max bank 30°, min IAS 1.3·Vs, min AGL 300 ft on final, no descent below pattern altitude − 300 ft, hold ≤ 10 s). Rejects or clips. If no candidate passes → ADVISORY level NO_SOLUTION ("NO SAFE MANEUVER - YOUR AIRCRAFT"). Issues RELEASE on STICK, on conflict clear, or at hold timeout; announces state at release ("your aircraft, continue right turn").
9. `node/trust.py` — interface to Lane C's trust evidence; default thresholds TRUSTED ≥ 0.7, SUSPICIOUS 0.4–0.7, FAKE < 0.4. Only TRUSTED targets may trigger RESOLVE or TAKEOVER; SUSPICIOUS → TRAFFIC only; CAMERA_ONLY → not produced (CV cut); keep the branch as TRAFFIC-only if it ever appears.
10. `harness/montecarlo.py` — extends `harness/tcas_ga_sim.py`. Encounter set: pattern geometries (overtake on downwind/final, base-to-final cut-off, straight-in vs pattern, head-on crosswind) plus non-conflict encounters for nuisance measurement. Compare straight-line+fixed-maneuver vs ARC: warning lead time, maneuver severity (bank needed), nuisance alerts, NMAC count. One matplotlib chart saved to `harness/out/arc_vs_baseline.png`. **If recorded KDVT ADS-B tracks are available (OpenSky account), replay them as an out-of-sample test and report separately.**

## Acceptance tests
- 1:00 PM: node connects, logs its leg classification every second, broadcasts STATE, raises TRAFFIC on a straight-line CPA against one scripted intruder.
- 4:00 PM: turn-aware prediction shows a base-to-final conflict ≥ 60 s before straight-line logic does on scenario `base_cutoff.json`; four layers escalate in order with correct text; two nodes commit complementary senses within 300 ms in the log.
- 7:00 PM: authority monitor rejects a 45° command and clips to 30°; NO_SOLUTION path demonstrated on `boxed_in.json`; RELEASE on STICK within one tick; chart produced.

## Constraints
- Deterministic. No randomness in decisions (randomness lives in the harness only).
- Every ADVISORY and COMMAND carries a `reason` dict a human can read on the log laptop.
- Keep per-tick compute < 10 ms per node with 8 peers.

## Phase 3 hooks (do not build before acceptance)
Threat Tubes: multiply sigma by staleness and non-compliance. RED ARC lite: `harness/redarc.py` random search over scenario parameters minimizing separation; failures saved to `harness/scenarios/regression/`. Marana replay: `harness/scenarios/marana_2025.json`.
