# Lane D — Integration, world data, hard algorithms, pitch (Manav)

Paste CONTEXT.md and docs/interface.md first, then this file. You are the agent for Lane D.

## Start against stubs (no waiting)
Start now, without Lane A or B: `python stubs/fake_world.py` + `python stubs/fake_node.py --id N101 --speed 4` feed the log page a full advisory ladder every 17 s. Your task rows with times: `BOARD.md` → Lane D.

## Goal
Make the pieces one system and give it a world to live in: live weather, terrain and obstacles, the comms log and explain panel the judges read, and in Phase 3 the Escape Crystal / MFI. Own the demo run and the pitch. **No camera / computer-vision work** (cut Sat 11:50 AM — see BOARD.md Parking lot).

## Deliverables
### Data (`/data`)
1. `data/metar.py` — fetch KDVT METAR from aviationweather.gov data API; compute pressure altitude, density altitude, wind; write `data/cache/metar_kdvt.json`; keep the last good file as fallback; CLI prints "KDVT 34°C 29.92 → DA 4,083 ft → C172 climb ≈ 542 fpm" — **written (Sat 12:20): `load()`, `climb_fpm()`, `wind_vector_ms()`, sample committed; live fetch untested (aviationweather.gov blocked from the build sandbox — run it on the hotspot)**.
2. `data/runways.py` — **done (Sat 12:20)** from surveyed AirNav/FAA 5010 ends; `load()`; sample committed. Shared pattern geometry in `/pattern.py`.
3. `data/terrain.py` — elevation grid ~15 mi around KDVT (USGS 3DEP/SRTM via a one-time download or Open-Meteo elevation sampling on a grid); save `terrain_kdvt.npy` + meta; `elev_at(lat, lon)`.
4. `data/obstacles.py` — FAA Digital Obstacle File filtered to ~15 mi; `obstacles_kdvt.csv`.
5. `data/opensky.py` (nice-to-have) — live `/states/all` in a Phoenix bbox every 10 s → `LIVE_TRAFFIC` frames to the world for an optional god-view overlay; recorded snapshot fallback.

### Log and explain panel (`/web/log`)
6. `web/log.html` + `web/log.js` — `?role=log`. Live table of LOG frames: time, source, kind, type, one-line summary; color by message type (STATE grey, INTENT blue, SEQ_* teal, MANEUVER_COMMIT orange, SIGHTING purple, decisions bold, drops red strikethrough with reason). Click a decision → side panel renders `reason` (predicted miss, ttc, method, confidence, chosen/rejected maneuvers, trust evidence, negotiation transcript). Top bar: live METAR, density altitude, channel loss/latency, aircraft count, slot-collision rate. Filters per aircraft. Export JSONL for the backup.

### Integration
8. `run_demo.sh` / `run_demo.ps1` — starts world, channel, N nodes, data fetchers in the right order with the hotspot IP; `scenarios/judges.json` is the Sunday scenario.
9. Record the backup video (OBS) Saturday night; keep `harness/out/arc_vs_baseline.png` and the pitch deck in `/docs`.

### Phase 3 (only after Phase 1 and 2 are green)
10. `node/crystal.py` — **Escape Crystal + MFI**: sample reachable set over bank ∈ [−30°, 30°] × vs ∈ [−500, climb_avail] × t ≤ 10 s; mark points unsafe if below terrain + 300 ft, inside an obstacle cylinder, or inside a peer's predicted tube; MFI = safe/total; send `CRYSTAL` frames to the god view; MFI collapse rate as an extra escalation trigger.

## Acceptance tests
- 1:00 PM: METAR cached and DA printed; runways JSON built; log page shows live frames from world + channel.
- 4:00 PM: terrain and obstacles loaded by nodes (Lane B imports `data/terrain.elev_at`); explain panel renders reasons.
- 7:00 PM: full demo run from `run_demo.sh` on the hotspot; backup video recorded; status report delivered.

## Pitch (owner: Manav)
Five minutes, four speakers, 70 s each, order: problem → how it works → judges fly (narrated) → feasibility and scale → why ours and what next. Lead with the shippable layers; takeover is phase three with printed bounds. Say the nuisance number out loud. Name prior art (TCAS II, FLARM, ADS-B In) and the delta. Quote the mentor and Zhong Chen where they gave numbers.
