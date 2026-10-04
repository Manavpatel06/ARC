# FLOCK AirWitness — camera-free anti-spoofing (Lane B)

> FLOCK does not trust an aircraft because it broadcasts a GPS coordinate. Every transmission is treated as a claim. Cryptography checks who sent it, physics checks whether its motion is possible, nearby aircraft act as independent witnesses, and RF ranging or multilateration checks whether the transmitter is physically where it claims to be. Unverified traffic can still warn the pilot, but it can never by itself command the airplane.

Code: `node/airwitness.py` (engine), wired in `node/node.py` and `node/trust.py`. Tests: `node/tests/test_airwitness.py` (T1–T20). Demo: `python harness/redflock_spoof.py`. RF simulator: `harness/rf_sim.py`. Lane C supplies signatures, replay rejection on the wire, RSSI/Doppler and peer witness reports (`radio/client.py`, `radio/evidence.py`); AirWitness turns all of it into one decision.

## Pipeline

```
radio envelope (claim) -> AirWitness evidence -> trust state + uncertainty -> Threat Tube (prediction sigma)
                      -> conflict / layers -> Escape Field -> bounds monitor (authority.py) -> allowed action
```
Cyber-security never chooses the maneuver; it only decides what a target's claims are allowed to influence.

## Evidence (all built, no camera)

| Check | How | Effect |
|---|---|---|
| Signature | Ed25519 from `radio/client.py` (`_auth`) | unsigned / unknown key: hard gate, never VERIFIED (still tracked) |
| Freshness | claimed time vs receive time, ±2 s | stale packet dropped |
| Replay | sliding window (as in IPsec): duplicate sequence number, or > 32 behind / > 2 s older than newest | packet dropped; SUSPICIOUS minimum, unless the real owner still answers a fresh signed challenge (then the attacker cannot demote it) |
| Challenge-response | nonce in our HEARTBEAT (`"c": {peer: nonce}`), echoed in theirs (`"r"`), signed | proves the peer is live now; a miss only costs confidence (loss happens) |
| Kinematics | speed ≤ 250 kt, accel ≤ 12 kt/s, turn ≤ 15 °/s, climb ≤ 3,000 fpm, teleport vs dead-reckoning | SUSPICIOUS minimum |
| Continuity | residual of each position vs the track's own dead-reckoning | soft |
| Intent consistency | a MANEUVER_COMMIT must show up in the following STATEs | widens the tube only (a pilot ignoring advice is not a spoofer) |
| Peer witnesses | signed neighbour reports (Lane C) | corroboration raises, refutation / silence lowers |
| RF location | `RFMeasurement`: range (RSSI or ranging), bearing, TDOA, Doppler vs the CLAIMED position | conflict: SUSPICIOUS minimum (Doppler alone never decides) |
| Duplicate identity | one id at two places at once | SUSPICIOUS minimum |
| Sybil | several ids that RF says come from one transmitter | SUSPICIOUS minimum |
| Ownship GNSS | GNSS vs heading/speed dead-reckoning and baro altitude | GPS kept, own uncertainty grows, reported DEGRADED |

Two independent hard failures make a target QUARANTINED. Soft evidence moves the score; an upgrade to VERIFIED has to hold for 2 s, a downgrade is immediate.

## States and what they may do

| AirWitness state | TRUST wire state (contract v1.1) | May drive | Tube |
|---|---|---|---|
| VERIFIED | TRUSTED | negotiation, RESOLVE, automatic TAKEOVER | normal |
| UNVERIFIED (e.g. legacy ADS-B, new peer) | SUSPICIOUS + `aw:UNVERIFIED` | warnings and advice to our pilot; no commit to it, no takeover | x1.5 |
| SUSPICIOUS | SUSPICIOUS | TRAFFIC warning only; claimed leg/intent ignored | straight-line physical envelope x2.5 |
| QUARANTINED | FAKE | display only (radar draws it hollow) | – |

Low trust never deletes a target: it removes what its claims are allowed to do and widens its uncertainty. The TRUST frame carries every check (`aw:STATE`, `signature:…`, `rf_bearing:CONFLICT…`, `action:…`); ADVISORY reasons carry `trust` with the state, score and reasons.

**Contract note (team decision):** the schema's TRUST states are TRUSTED / SUSPICIOUS / FAKE / CAMERA_ONLY, so UNVERIFIED travels as SUSPICIOUS plus `aw:UNVERIFIED` in the evidence list. An additive v1.2 adding the four AirWitness states would let the cockpit badge show it directly.

## Results

- Tests T1–T20 (plus an explanation test) pass: `python -m pytest node/tests/test_airwitness.py -q`.
- RED FLOCK ladder (`harness/redflock_spoof.py`), our aircraft head-on with the ghost, autopilot-equipped:

| Attempt | Ghost ends | Ghost may drive | Commits to ghost | Takeovers |
|---|---|---|---|---|
| 1 impossible motion (400 kt, jumps) | QUARANTINED | nothing | 0 | 0 |
| 2 replay of the real N204's packets | replays dropped, N204 stays VERIFIED | – | 0 | 0 |
| 3 fresh, plausible, unsigned, 2.5 km ahead | QUARANTINED (signature + RF bearing + no witnesses) | nothing | 0 | 0 |
| 4 hardest: own key, transmitter at the claimed position | SUSPICIOUS | TRAFFIC warning only | 0 | 0 |
| 5 sybil x3 from one ground site | QUARANTINED | nothing | 0 | 0 |

- Live on the real stack (world + Lane C signed radio + channel + two nodes + `radio/spoofer.py`): the two real aircraft VERIFIED each other (signed challenge-response PASS); GHOST7 unsigned → SUSPICIOUS, GHOST7 impossible → QUARANTINED; no advisory or command about the ghost in either run.

## What is simulated, what needs hardware

- Built and real today: signatures, replay window, challenge-response, kinematics, continuity, intent, peer witnesses, duplicate id, trust states, tube inflation, explanations, ownship GNSS check.
- Simulated (V2, `harness/rf_sim.py`): two-way ranging, angle of arrival, TDOA, Doppler, emitter fingerprint ids for the sybil test. The RSSI range from the channel emulator is used live. Laptops and ESP32s do not give aviation-grade multilateration; real synchronized RF hardware fills the same `RFMeasurement` fields later without changing the engine.
- Research only: RF fingerprinting (would be weak supporting evidence, never safety-critical).

Known limit, said out loud: an attacker with several valid, registered keys and transmitters placed to fake geometry could corroborate itself (Sybil with real keys). Registration-bound keys plus RF location from several receivers make that expensive, not impossible.
