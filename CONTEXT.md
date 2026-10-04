# ARC — Shared context for every AI agent and every teammate

Paste this file at the top of every AI coding session, then paste your lane file from `lanes/`.

## What we are building (one paragraph)
ARC is a peer-to-peer collision-avoidance node for general aviation (GA), built for Devils Invent "Future-Ready Avionics" (Honeywell Aerospace, ASU, Oct 2–4 2026), Problem Statement 2: "Automated TCAS for General Aviation — peer-to-peer, cost a priority, ranges up to 3 miles, warn if the pilot ignores, automate avoid via aircraft-to-aircraft negotiation of flight paths." Every aircraft runs the same node. The node reads only its own aircraft's state; everything about other aircraft arrives over a radio link. It predicts where other aircraft are *turning* (traffic-pattern intent), responds in four layers (sequence → warn → negotiate → act), chooses maneuvers that fit this airplane in this world (density altitude, terrain, obstacles, traffic), trusts targets by evidence rather than by broadcast, and hands control back the instant the pilot touches the stick. A Honeywell mentor called it "the idea in his dreams" this morning.

## Hard rules (judges will probe these)
1. **Nodes see only what a real node would see.** Own GPS/baro/heading from the world; peers only via radio messages; no god-view leaks.
2. **Deterministic logic decides. No LLMs in the control loop.** AI/ML is allowed for perception and prediction only; every alert and action must be explainable from numbers in the log.
3. **The takeover is bounded and printed.** Max bank 30°, speed floor 1.3 × Vs, no automatic action below 300 ft AGL on final, no automatic descent below pattern altitude − 300 ft, max 10 s authority, release on any stick input, and if no maneuver fits → warn-only and say so. These bounds are enforced by a small separate monitor, not by the smart logic.
4. **Honest words.** Say "RAs inhibited below 1,000 ft AGL (TAs continue)", not "TCAS goes silent". Don't claim nanosecond latency. Don't say "would have prevented" a real crash; say "produces a timely intervention under the published geometry".
5. **Scope discipline.** Must-haves first (Phase 1), then Phase 2, then one Phase 3 add-on at a time. Anything not green at the 7 PM status report is cut, not improvised. Record a backup video Saturday night.
6. **Use the shared tools, don't re-derive.** Runways `data.runways.load()`, weather/DA/wind `data.metar.load()` + `climb_fpm()` + `wind_vector_ms()`, terrain `data.terrain.elev_at_ft()`, pattern `pattern.py`, messages `schemas.py`.
7. **Nobody waits.** Every cross-lane dependency has a stand-in in `stubs/` that speaks the contract. Build against the stub; swap when the real module is marked `[x]` in `BOARD.md`.
8. **Everything speaks the same schema.** `schemas.py` is the contract. Change it only by agreement in the team chat.

## Phases
- **Phase 1 — core avoidance ("ACAS core")**, target green by Sat 7 PM: world + controllers + AI pattern traffic, nodes with turn-aware prediction, four layers, Escape Field (performance + terrain), authority protocol with bounds, comms log + explain panel, Monte Carlo chart.
- **Phase 2 — radio trust ("radio spoofing & channel")**, Sat evening: channel emulator with range/loss/latency/congestion, signed messages + replay protection, trust engine (plausibility, Doppler consistency, peer corroboration), spoof injection demo, lost-link fallback, AIS-style self-organizing slots for "avoiding clash of signals".
- **Phase 3 — add-ons from ARC-X SENTINEL**, one at a time, only when 1 and 2 are green. Priority order: (1) Escape Crystal + Maneuver Freedom Index, (2) Marana historical replay, (3) Constraint Exchange / 4D contracts, (4) RED ARC simple adversarial search + regression library, (5) Threat Tubes with simple uncertainty inflation, (6) dragonfly threat attention. Camera/CV items are cut.

## Team and lanes
- **Manas — Lane A, Sim world** (`/world`, `/web` views): CesiumJS/three.js Deer Valley, 3-DOF flight model, controllers, AI pattern traffic, cockpit/god/log views, world server.
- **Reya — Lane B, Node logic + evidence** (`/node`, `/harness`): trust thresholds, pattern-leg classifier, turn-aware prediction, four layers, Escape Field, authority protocol + bounds monitor, Monte Carlo chart.
- **Mansi — Lane C, Radio, protocol, security** (`/radio`): channel emulator, schemas enforcement, Ed25519 signing, replay protection, trust evidence (RF consistency), spoof injector, AIS-style slots, lost-link fallback; optional ESP32-C6 node port.
- **Manav — Lane D, Integration, data, hard algorithms, pitch** (`/data`, `/web/log`, integration): METAR/terrain/obstacles, comms log + explain panel, run_demo, Escape Crystal/MFI in Phase 3, pitch, mentors.

## Repo layout
```
arc/
  CONTEXT.md  INTERFACE.md  PLAN.md  schemas.py  requirements.txt
  world/      # Lane A: world_server.py (physics + WebSocket hub), flight_model.py, traffic.py
  web/        # Lane A (+D for log): index.html?role=cockpitA|cockpitB|god|log
  node/       # Lane B: node.py, predict.py, layers.py, escape.py, authority.py, trust.py
  radio/      # Lane C: channel.py, crypto.py, spoofer.py, slots.py
  data/       # Lane D: metar.py, terrain.py, obstacles.py, opensky.py
  harness/    # Lane B: montecarlo.py (extends tcas_ga_sim.py), scenarios/
  lanes/      # one brief per lane for AI agents
  stubs/      # stand-ins for every lane (fake_world, fake_node, loopback_radio, tail_log) so no lane waits
  BOARD.md    # live task board: rows per lane with times + status; Pull Queue for idle hands
```

## Fixed facts to use (verify against the airport diagram before the pitch)
- Airport: Deer Valley (KDVT), Phoenix. Field elevation 1,478 ft MSL. **07L/25R = north runway** (4,500 ft, 7L left traffic, 25R right traffic); **07R/25L = south runway** (8,196 ft, 7R right traffic, 25L left traffic). **Headings TRUE 086°/266°** (magnetic 074°/254°, variation ~12°E) — the sim works in lat/lon so use true. TPA 2,500 ft MSL piston (~1,020 AGL). Displaced thresholds 7R 898 ft, 25L 916 ft. Surveyed runway ends in `data/runways.py` (AirNav/FAA 5010). 25L left pattern: downwind 086° south of the field, base 356°, final 266°.
- **Pattern geometry lives in ONE place: `pattern.py`** (`place()`, `legs()`, `to_enu()`). World spawn, AI traffic, node leg classifier and god view all import it.
- Aircraft model: Cessna 172S-class. Vs1 ≈ 48 kt clean, pattern speed ≈ 90 kt, cruise 110–120 kt, sea-level standard climb ≈ 730 fpm; use ≈ 500 fpm at 5,000 ft density altitude and ≈ 300 fpm at 8,000 ft. Bank-to-turn: turn rate = g·tan(bank)/V.
- Layer timings (time to predicted conflict): sequence ≤ 90 s, warn ≤ 35 s, negotiate ≤ 20 s, act ≤ 8 s. Nuisance control: a conflict needs predicted miss < 500 ft horizontal AND < 100 ft vertical (NMAC box) within the horizon, with uncertainty.
- Radio: STATE at 1 Hz + event-driven INTENT/COMMIT; emulated range 3 statute miles (4,828 m); default loss 10%, latency 300 ms; lost link → both turn right (14 CFR 91.113).
- Physics facts for the pitch (from Friday's sim): head-on 120+120 kt closes 3 mi in 39 s; 30° bank at 120 kt gains ~700 ft lateral in 10 s vs ~150 ft from a 500 fpm climb/descend pair; naive left/right rule left 23% of crossings unresolved, CPA-maximizing negotiation 0%.

## Demo (Sunday noon, 5 min + 3 min Q&A)
Four laptops: cockpit A (PS controller 1), cockpit B (PS controller 2), god view on projector, comms log + explain panel. Judges fly; ARC sequences them; they try to crash; warn → negotiate → bounded takeover → handback on stick input; spoofed aircraft is flagged and never acted on. (Camera/CV is cut from the build — see BOARD.md.) All four teammates speak.
