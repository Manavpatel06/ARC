# FLOCK — Interface contract (v1, frozen Sat 11:30 AM; change only by team agreement)

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

Trust summary (for the cockpit badges):
```json
{"type":"TRUST","ac_id":"N101","t":...,"targets":[{"id":"N204","score":0.93,"state":"TRUSTED","evidence":["plausible","corroborated:2","camera"]},{"id":"GHOST7","score":0.12,"state":"FAKE","evidence":["no_corroboration","kinematics_violation"]}]}
```
### World → cockpit (20 Hz) — own aircraft state plus the latest ADVISORY/TRUST from its node. Nothing else.
### World → god (10 Hz) — all truth states, all predicted paths (nodes publish `PREDICTION` optionally), conflict markers.
### Anything → log — every radio message and every ADVISORY/COMMAND/TRUST is mirrored to role `log` as `{"type":"LOG","src":"N101","kind":"radio|decision","payload":{...}}`.
### Cockpit → world — `{"type":"INPUT","ac_id":"N101","roll":-0.3,"pitch":0.1,"throttle":0.7}` from the Gamepad API at 30 Hz.

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
```json
{"name":"judges_converging","aircraft":[{"id":"N101","start":{"leg":"DOWNWIND","runway":"25L","offset_s":0},"ap":true,"human":true}, ...],
 "channel":{"loss":0.1,"latency_s":0.3},"weather":"live|cached:2026-10-04T18Z","spoofer":false,"camera_target":true}
```
