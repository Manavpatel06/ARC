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

## Round 2 — software-only additions (no hardware)

| Idea | Plausible with software only? | What FLOCK does |
|---|---|---|
| RF fingerprinting (transmitter imperfections) | **No.** Needs raw IQ samples from an SDR front end; the radio we have only gives decoded packets + RSSI/Doppler. | Not built for real. The sybil test uses a simulated emitter id (`source_id`) through the same interface, so a future SDR plug-in needs no engine change. Would only ever be weak evidence. |
| Direction of arrival with two antennas | **No** (needs a second antenna and phase/time measurement). | Replaced by a software equivalent: **collective radio location**. Every FLOCK node shares its RSSI-derived range to each target in its signed HEARTBEAT (`"w"`); each receiver checks the claimed position against the ranges measured by several VERIFIED peers at known places. Several aircraft act as the "two ears", spread kilometres apart. |
| Kinematic / plausibility filters (incl. "pops into existence") | **Yes.** | Speed / acceleration / turn / climb / teleport limits, track continuity, plus **pop-in**: a target first heard within 2 km and already airborne (real traffic arrives from the edge of radio range). |
| Barometric vs geometric altitude | **Yes** (needs both altitudes in the message). | Our STATE now carries `alt_geo_ft` next to `alt_press_ft` (additive field). Every aircraft in the same air mass has nearly the same GNSS-minus-baro offset; a target whose offset differs from ours by > 400 ft fails (> 250 ft costs confidence). Legacy traffic without the field: n/a. |
| ACAS X-style probabilistic reasoning | **Yes** (software). | Trust becomes uncertainty: the trust state scales each target's Threat Tube (x1 / x1.5 / x2.5), so doubtful data widens the protected volume instead of being believed or deleted. Full ACAS X tables are out of scope. |
| Negotiation as evidence (new) | **Yes.** | A live FLOCK peer answers our MANEUVER_COMMIT / SEQ_PROPOSE within 6 s (`negotiation: PASS (answered in 1.0 s)`), which counts as independent proof of life; a target that keeps talking but never answers loses confidence; a peer that commits one way and flies the opposite way is "intent inconsistent" (tube widened). Commits from anything that is not VERIFIED are never used for planning, and we never send commits to it. |

Pilot alert: when a target becomes SUSPICIOUS or QUARANTINED, or our own GNSS becomes inconsistent, the node adds an `alerts` list to its TRUST frame (additive) — the cockpit shows a warning banner and speaks it once ("caution, spoofed traffic, G H O S T 7, ignored"). Live it reached the pilot 1–5 s after the ghost's first packet; full quarantine took 2–17 s depending on geometry.

Measured detection (in-process ladder, `harness/redflock_spoof.py`): impossible motion, fresh unsigned ghost and sybil flagged + quarantined + alerted within 1 s of the first packet; a replay is dropped on the first replayed packet; the "own key, true position" attacker is SUSPICIOUS + alerted within 1 s (warning only, never quarantined — it is the honest hard case).

## Limitations that remain (and why they cannot be fixed in software alone)

1. **An attacker with a valid registered key** (stolen key or insider) passes the signature gate; only physics, RF location and peer witnesses can catch it.
2. **RSSI-only ranging is coarse** (3 dB ≈ a factor of 1.4 in distance): it catches gross position lies, not a transmitter a few hundred metres from its claim. Fine location needs ranging/TDOA hardware (simulated today).
3. **Doppler** depends on a stable oscillator in real radios; here it comes from the channel emulator, and it never decides alone.
4. **Isolated aircraft** (no FLOCK peers in range) get no witnesses and no collective radio location; they still have signature, replay, physics and pop-in checks.
5. **Legacy (unsigned) traffic that pops up close** (e.g. after radio shadowing) is treated as SUSPICIOUS: it is still shown and warned about, but FLOCK will not act on it automatically.
6. **Contract**: the frozen TRUST states have no UNVERIFIED (sent as SUSPICIOUS + `aw:UNVERIFIED`); `alerts` and `alt_geo_ft` are additive fields; a v1.2 should make them official.
7. **Real RF hardware** (fingerprinting, DoA, ranging) is out of scope by choice (software only); the `RFMeasurement` interface is where it plugs in.
