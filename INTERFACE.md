# FLOCK — Interface contract (v1.1; v1 frozen Sat 11:30 AM, v1.1 additive changes Sat 12:20 PM; change only by team agreement)

> **v1.1 changes (additive — nothing in v1 changed meaning):** `rel` on each TRUST target (radar positions) · `HELLO` + cockpit A/B resolution · `TRUTH`, `PREDICTION`, `SET_DA`, `CRYSTAL` formalised in `schemas.py` · INPUT semantics pinned · OWNSHIP ground speed/track include wind · scenario `start` semantics pinned (`pattern.place`). See §6.

All messages are JSON objects, one per WebSocket frame or UDP datagram. Units: SI internally except where the field name says otherwise (`_kt`, `_ft`, `_fpm`, `_deg`). Times are Unix seconds (float) from the world clock. `schemas.py` is the executable version of this file; import it everywhere.

## 1. World server (Lane A) — `ws://<host>:8765`
One process owns physics at 20 Hz and acts as a hub. Clients connect with `?role=` in the URL: `node:<ac_id>`, `cockpit:<ac_id>`, `god`, `log`, `channel`, `camera`, `data`.

### World → node (10 Hz, only the node's own aircraft)
```json
{"type":"OWNSHIP","ac_id":"N101","t":1759520000.1,
 "lat":33.688,"lon":-112.082,"alt_msl_ft":2480,"alt_press_ft":2470,"agl_ft":1002,
 "gs_kt":92,"track_deg":251,"hdg_deg":249,"bank_deg":0,"vs_fpm":0,"ias_kt":90,
 "ap_equipped":true,"stick_active":false,"flaps":0}
```
### Node → world
Advisory (display + voice):
```json
{"type":"ADVISORY","ac_id":"N101","t":...,"layer":1,"level":"SEQUENCE",
 "text":"NUMBER 2 - EXTEND DOWNWIND 15 S","speak":"number two, extend downwind fifteen seconds",
 "target_id":"N204","ttc_s":84,"reason":{"predicted_miss_ft":310,"method":"turn-aware","confidence":0.82}}
```
`level` ∈ SEQUENCE | TRAFFIC | RESOLVE | TAKEOVER | RELEASE | NO_SOLUTION | CLEAR.

Command (only when the node has authority):
```json
{"type":"COMMAND","ac_id":"N101","t":...,"mode":"TAKEOVER",
 "bank_cmd_deg":-28,"vs_cmd_fpm":0,"hold_s":10,
 "bounds":{"max_bank_deg":30,"min_ias_kt":62,"min_agl_ft":300,"max_hold_s":10},
 "reason":{"chosen":"L30","rejected":{"R30":"traffic","CLIMB":"performance 350fpm","DESCEND":"terrain floor"},"ttc_s":7.6}}
```
`mode` ∈ TAKEOVER | RELEASE. The world applies the command only if `ap_equipped` and `stick_active` is false; any stick input makes the world send `{"type":"STICK","ac_id":..}` and the node must issue RELEASE within one tick.

Trust summary (for the cockpit badges **and the cockpit radar** — the radar draws ONLY these targets, at `rel`):
```json
{"type":"TRUST","ac_id":"N101","t":...,"targets":[{"id":"N204","score":0.93,"state":"TRUSTED","evidence":["plausible","corroborated:2"],"rel":{"brg_deg":131,"rng_m":1620,"dalt_ft":-40,"trk_deg":356,"vs_fpm":-400}},{"id":"GHOST7","score":0.12,"state":"FAKE","evidence":["no_corroboration","kinematics_violation"]}]}
```
`rel` (v1.1) is computed by the node from the peer's last STATE: TRUE bearing own→target, horizontal range in m, altitude difference in ft (target − own), optional target track and vertical speed. Nodes send TRUST at ≥ 1 Hz so the radar stays current. FAKE targets still carry `rel` (drawn hollow red, never acted on).

### World → cockpit (20 Hz) — own aircraft state plus the latest ADVISORY/TRUST from its node. Nothing else.
### World → god (10 Hz) — all truth states, all predicted paths (nodes publish `PREDICTION` optionally), conflict markers.
Frames the god view receives (v1.1, models in `schemas.py`):
```json
{"type":"TRUTH","t":...,"aircraft":[<OWNSHIP>, ...]}                       // world -> god + channel ONLY, 10 Hz
{"type":"PREDICTION","ac_id":"N101","target_id":null,"t":...,"method":"turn-aware","confidence":0.82,"leg":"DOWNWIND",
 "path":[{"t":5,"lat":..,"lon":..,"alt_msl_ft":2500,"sigma_m":40}, ...]}      // node -> world -> god, <= 1 Hz
{"type":"CRYSTAL","ac_id":"N101","t":...,"mfi":0.38,
 "points":[{"bank_deg":-30,"vs_fpm":0,"lat":..,"lon":..,"alt_msl_ft":2500,"safe":false,"why":"traffic"}, ...]}  // Phase 3
```
Live sky (v1.2, Lane D `data/live_traffic.py` → world as role `data` → **god + log only**): `{"type":"LIVE_TRAFFIC","t","source","radius_nm","aircraft":[{"id","callsign","type","lat","lon","alt_msl_ft","gs_kt","track_deg","vs_fpm","on_ground","leg","runway","leg_conf","next_leg","in_pattern"}]}` every ~5 s. Real ADS-B aircraft around KDVT with their pattern leg classified by `pattern.py`. **Display and prediction only — never forwarded to nodes, never acted on.** World: `if t == "LIVE_TRAFFIC": send("god", m); log(...)`.

God → world: `{"type":"SET_DA","ft":6500}` overrides density altitude for climb capability (world logs it).
**TRUTH never goes to a node or cockpit.**
### Anything → log — every radio message and every ADVISORY/COMMAND/TRUST is mirrored to role `log` as `{"type":"LOG","src":"N101","kind":"radio|decision","payload":{...}}`.
### Cockpit → world — `{"type":"INPUT","ac_id":"N101","roll":-0.3,"pitch":0.1,"throttle":0.7}` from the Gamepad API at 30 Hz.
INPUT meaning (v1.1; Lane A may tune the constants, not the meaning):
- `roll` ∈ [−1, 1] → **target bank** = roll × 45°, approached at ≤ 15°/s. Stick centred → wings return level. (Chosen over a bank-rate stick because judges are not pilots; a rate stick spirals them in.)
- `pitch` ∈ [−1, 1] → **target vertical speed**: up = pitch × climb capability at current density altitude; down = pitch × 1,000 fpm.
- `throttle` ∈ [0, 1] → **target IAS** 60 → 120 kt, approached at ≤ 2 kt/s.
- Any INPUT with |roll| or |pitch| > 0.1 sets `stick_active=true`; it clears after 1 s without such input. During a TAKEOVER the first such INPUT makes the world send STICK.

### Cockpit identity (v1.1)
`web/index.html?role=cockpitA` connects as `role=cockpit:A`; `cockpitB` → `cockpit:B`. The world resolves `A` = first aircraft with `"human":true` in the scenario, `B` = second (in `judges.json`: A = N101, B = N102). Override with `&ac=N102` → page connects as `cockpit:N102`. On connect the world replies `HELLO {"role":"cockpit:N101","ac_id":"N101","scenario":"judges","aircraft":[{"id","human","ap","flock"}...]}` (ids and flags only, no positions). Every role gets HELLO.

### OWNSHIP air vs ground (v1.1)
`ias_kt`/`hdg_deg` are air data; `gs_kt`/`track_deg` are ground data = air velocity + wind from `data.metar.load()` (`data.metar.wind_vector_ms`). `agl_ft` = `alt_msl_ft − data.terrain.elev_at_ft(lat, lon)` (flat field elevation until the real grid lands).

## 2. Radio (Lane C) — UDP multicast `239.1.1.1:5005` (or relayed through the channel process)
Envelope for every radio message:
```json
{"msg":"STATE","from":"N101","seq":4821,"t":...,"sig":"<base64 ed25519>","body":{...}}
```
Bodies:
- `STATE` (1 Hz): `{"lat","lon","alt_press_ft","gs_kt","track_deg","vs_fpm","leg":"DOWNWIND","intent":"BASE_IN_20S","ap_equipped":true}`
- `INTENT` (event-driven): `{"leg","intent","valid_for_s":20}`
- `SEQ_PROPOSE`: `{"runway":"25L","order":["N101","N204"],"extend_s":{"N204":15}}` · `SEQ_ACCEPT`: `{"proposal_seq":4821}`
- `MANEUVER_COMMIT`: `{"target":"N204","sense":"R","bank_deg":30,"vs_fpm":0,"start_t":...,"hold_s":10}`
- `SIGHTING` (camera-only target shared to peers): `{"observer":"N311","az_deg":42,"el_deg":1.5,"size_px":18,"growth_px_s":2.1,"ttc_s":31,"conf":0.7}`
- `HEARTBEAT` (2 s): `{"alive":true}`


### Python API for the radio client (Lane C ships it; Lane B codes against it from minute one)
`stubs/loopback_radio.py` is the reference implementation of this API; `radio/client.py` must keep the same names and types.
```python
radio = RadioClient(ac_id="N101", via_channel=None)   # via_channel="ws://<ip>:8765" when multicast is blocked
await radio.start()
radio.on_message(lambda env: ...)                      # env = validated envelope dict (schemas.RadioMsg); own messages never echoed
env = await radio.send("STATE", body_dict)             # wraps envelope, assigns seq/t, signs (stub: sig="")
radio.stats                                            # {"tx","rx","rejected"}
await radio.stop()
```

### Channel emulator rules (Lane C)
The channel knows true positions (from the world, role `channel`) and decides what arrives: drop if range > 4,828 m; drop with probability `loss` (default 0.10); delay by `latency` (default 0.3 s ± 0.1); when more than N senders share a slot, drop colliding packets (AIS-style slot map reduces this). Nodes never read the channel's truth.

### Security (Lane C)
Ed25519 key per node generated at startup; public keys exchanged once at boot (hackathon simplification). Reject: bad signature, `seq` not increasing, `t` outside ±2 s window. The spoofer sends unsigned or kinematically impossible STATE for `GHOST7`.

## 3. Data (Lane D) — files in `/data/cache/`, loaded at node start
- `metar_kdvt.json` (latest + cached fallback): temp_c, altimeter_inhg, wind_dir_deg, wind_kt → `density_altitude_ft`.
- `terrain_kdvt.npy` + `terrain_meta.json`: elevation grid (m) ~15 mi around KDVT.
- `obstacles_kdvt.csv`: lat, lon, top_msl_ft from FAA DOF.
- `runways_kdvt.json`: runway ends, headings, elevation, pattern side.
- Camera publishes `SIGHTING` bodies to the world as role `camera`, attributed to a designated camera-equipped aircraft.

## 4. Timing
World physics 20 Hz; node loop 10 Hz; STATE 1 Hz; HEARTBEAT 0.5 Hz; cockpit input 30 Hz; log unbuffered.

## 5. Scenario files (`harness/scenarios/*.json`)
`start` = `{"leg","runway","offset_s"[, "agl_ft"]}` → position via **`pattern.place(leg, runway, offset_s, gs_kt, agl_ft)`** (one shared definition in `pattern.py`). `offset_s` = seconds already flown along the circuit from the start of that leg at the aircraft's speed; negative = before the leg starts (e.g. `BASE, -5` is 5 s before the base turn, still on downwind). `leg` ∈ UPWIND | CROSSWIND | DOWNWIND | BASE | FINAL | STRAIGHT_IN | GO_AROUND. Headings are TRUE: KDVT 25L/25R final 266°T (254°M), downwind 086°T; 25L left traffic (south side), 25R right traffic (north side).
```json
{"name":"judges_converging","aircraft":[{"id":"N101","start":{"leg":"DOWNWIND","runway":"25L","offset_s":0},"ap":true,"human":true}, ...],
 "channel":{"loss":0.1,"latency_s":0.3},"weather":"live|cached:2026-10-04T18Z","spoofer":false,"camera_target":true}
```

## 6. Changelog
- **v1.2 (Sat 1:30 PM)** — additive: `LIVE_TRAFFIC` (real ADS-B overlay for the god view; model `schemas.LiveTraffic`).
- **v1.1 (Sat 12:20 PM)** — from Lane A review. Additive: `TrustTarget.rel` (radar positions); `HELLO` + `cockpit:A|B` resolution + `&ac=` override; `TRUTH`/`PREDICTION`/`SET_DA`/`CRYSTAL` models; INPUT semantics; OWNSHIP gs/track include wind, agl from terrain; scenario start semantics via `pattern.py`. Corrected KDVT geometry (true headings 086/266; 25R is the north runway). Agreed by: Manav (lead), Manas (requested) — Reya/Mansi please ack in chat.
- **v1 (Sat 11:30 AM)** — initial freeze.
