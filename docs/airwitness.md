# ARC AirWitness-Hybrid — passive, camera-free anti-spoofing (Lane B)

> Every received broadcast is a claim. ARC starts tracking, predicting and warning on it immediately; AirWitness
> evaluates the claim in parallel and only ever changes what it is allowed to do. Nothing asks another aircraft to
> prove anything, nothing waits for an answer, and low trust never deletes traffic.

Code: `node/airwitness.py` (engine), wired in `node/node.py` and `node/trust.py`. Tests: `node/tests/test_airwitness.py`
(spec tests 1–27 plus regression checks). Attacks: `python harness/redflock_spoof.py` (14 attacks). Simulated
surveillance: `harness/surveillance_sim.py`. Simulated RF: `harness/rf_sim.py`.

## What was removed (challenge-response)

The previous version proved liveness by putting a nonce in our HEARTBEAT (`"c": {peer: nonce}`) and waiting for the
peer to echo it (`"r"`), and it held "no answer to our negotiation" against a peer. Both are gone:

| Removed | Where |
|---|---|
| `CHALLENGE_EVERY_S / TIMEOUT_S / VALID_S`, `import secrets`, `nonce_fn` | `node/airwitness.py` constants / `__init__` |
| `_Track.chal_pending / chal_ok_t / chal_misses / chal_last`, `neg_miss` | `node/airwitness.py` |
| HEARTBEAT `"c"` handling (queue an echo) and `"r"` handling (match our nonce) | `AirWitness.on_message` |
| `AirWitness.tick()` (issue nonces, time them out, count misses, count negotiation misses) | whole method |
| replay exemption "real owner answered a fresh challenge"; `challenge: PASS / NO RESPONSE / PENDING`; `negotiation: NO ANSWER` penalty | `AirWitness.assess` |
| `extra = self.aw.tick(now)` and the extra challenge/echo HEARTBEATs | `Node._periodic_radio` |
| N204 answering challenges | `harness/redflock_spoof.py` |

`git diff d266114 -- node/airwitness.py node/node.py harness/redflock_spoof.py` shows every removed line;
`test_no_challenge_path_left` fails if a `tick`, `nonce` or `chal_` path comes back or a HEARTBEAT carries anything
but `alive` and the passive digest `w`.

## Pipeline

```
TARGET BROADCAST -> FAST PASSIVE PACKET GUARD (signature / session / sequence / replay / duplicate / freshness)
     |                                   |
COLLISION ENGINE runs on the claim   AIRWITNESS-HYBRID (2 Hz): physics | independent surveillance | RF + peers
     |                                   |
     |                    P(REAL) / P(SPOOF) / P(FAULTY) + hard gates -> VERIFIED / UNVERIFIED / SUSPICIOUS / QUARANTINED
     |                                   |
     +---- Verified CLOF / Shadow CLOF -> Threat Tube width -> trust-robust Escape -> negotiation (verified only)
                                                           -> authority.py bounds monitor (formal safety kernel) -> action
```

## Evidence sources (each returns PASS / WEAK / CONFLICT / UNKNOWN; unavailable = UNKNOWN, never FAIL)

| Source | How | Hard gate |
|---|---|---|
| signature | Ed25519 `_auth` from `radio/client.py` | invalid/absent: never VERIFIED (still tracked) |
| packet guard | SHA-1 packet hash (duplicates), session id `sid` (new = restart, older = expired), IPsec-style sequence window, sequence jump > 100, ≥ 3 sessions in 60 s, ±2 s freshness, local monotonic arrival | dropped packets never update the track; a strike unless the identity's own live signed track is clean (attack on it, not by it) |
| motion (`MotionEvidence`) | position / velocity / turn / vertical residuals; GA limits 250 kt, 12 kt/s, 15°/s, 3000 fpm, 1500 fpm/s (legit emergency maneuvers pass) | conflict = strike |
| trajectory | constant-turn-rate prediction vs received state, rolling RMS | soft |
| intent | a MANEUVER_COMMIT flown the opposite way | widens the tube only |
| identity | one id in two places | strike |
| pop-in / baro-geo | first heard < 2 km and airborne; GNSS-baro offset vs ours | baro-geo conflict = strike |
| TCAS (`TCASMeasurement`) | read-only range / bearing / relative altitude vs the claim | contradiction = strike; agreement = physical support |
| Mode-S (`ModeSObservation`), 1030/1090 (`InterrogationReplyEvidence`) | passive; support only | – |
| RF location | RSSI / ranging / bearing / TDOA vs the claimed position | conflict = strike |
| Doppler trend (`RFWaveEvidence`) | sign + trend of carrier shift vs the claimed radial motion | counts only together with another failure |
| RSSI trend | correlation with claimed range; low weight | never decides alone |
| witnesses | Lane C reports (corroborated / refuted) | – |
| multi-observer (`MultiObserverEvidence`) | verified peers' piggybacked WITNESS_DIGEST: radial-motion sign at each observer vs the claim | conflict = strike |
| sybil | several ids from one transmitter | strike |
| negotiation | a peer's ordinary answer to our own commit / proposal | positive only; silence is not held against anyone |

Inference: one likelihood factor per source over REAL / SPOOF / FAULTY (prior 0.90 / 0.05 / 0.05), then the gates.
QUARANTINED: two independent strikes or P(spoof) ≥ 0.85. SUSPICIOUS: one strike or P(spoof) ≥ 0.45. VERIFIED:
valid signature, clean packets, ≥ 3 s of track and P(real) ≥ 0.85, held 2 s (downgrades are immediate). All constants
are at the top of `node/airwitness.py`.

## What each state may do

| State | TRUST wire | CLOF | Tube | Negotiation / 4D contract | Automatic command from its data |
|---|---|---|---|---|---|
| VERIFIED | TRUSTED | verified | ×1 | allowed | allowed |
| UNVERIFIED | SUSPICIOUS + `aw:UNVERIFIED` | shadow | ×1.5 | blocked | blocked (RESOLVE advice only) |
| SUSPICIOUS | SUSPICIOUS | shadow | ×2.5, claimed intent ignored | blocked | blocked (TRAFFIC) |
| QUARANTINED | FAKE | – (shadow + TRAFFIC if TCAS / 1030-1090 independently sees it) | ×4 | blocked | blocked |

A new target is assessed on its first packet: it is never TRUSTED by default. MANEUVER_COMMITs from a target that is
not VERIFIED are ignored. Shadow targets are resolved with the **trust-robust escape**: candidates are scored with the
target real (H1) and spoofed (H2), and maneuvers safe under both come first (`trust_robust` in `node/node.py`; the
RESOLVE reason carries `trust_robust` and `takeover_inhibited`). The TRUST frame carries `clof` per target and every
evidence line, P(real/spoof/faulty) and the authority list; `TrustResult.explain()` prints the panel.

## Results

- Tests: 133 pass (`python -m pytest -q`), including spec tests 1–27 and `test_no_challenge_path_left`. accept_b 16/16.
- Monte Carlo (`python harness/montecarlo.py --n 60`, 420 encounters, 154 true conflicts, 10 % loss, 0.3 s latency):

| | NMAC | median min-sep | nuisance alerts / maneuvers | NO_SOLUTION | tick mean |
|---|---|---|---|---|---|
| no logic | 154/154 | – | – | – | – |
| baseline | 28/154 (18 %) | 913 ft | 1 % / 1 % | 0 | 2.0 ms |
| ARC (before, challenge-response) | 11/154 (7 %) | 1138 ft | 3 % | 4 | – |
| **ARC (AirWitness-Hybrid)** | **5/154 (3 %)** | **1169 ft** | 3 % / 0 % | 4 | 3.9 ms |

- False positives: 0 spoof alerts and 0 SUSPICIOUS/QUARANTINED real peers in the same 420 legitimate encounters.
  (The first run found one, a vertical-speed change of > 1500 fpm/s when a simulated pilot ended a maneuver; the
  limit is now 3000 fpm/s, ~1.5 g, so an abrupt push-over or go-around passes.)
- RED ARC (`python harness/redflock_spoof.py`): 14/14 attacks blocked. No spoof was ever VERIFIED, got negotiation or
  a 4D contract, caused an automatic maneuver, or pushed the escape choice to one unsafe if it is fake.

| attack | worst state | flagged | quarantined |
|---|---|---|---|
| invalid signature, smooth, smooth + intent | QUARANTINED | 3 s | 3 s |
| replay / duplicate / stale session of N204 | packets dropped, N204 stays VERIFIED | – | – |
| teleport, sybil ×3, TCAS conflict | QUARANTINED | 1 s | 1 s |
| duplicate identity (stolen key, sequence hijack) | N204 identity QUARANTINED (see limits) | 0 s | 1 s |
| plausible ADS-B-only ghost | UNVERIFIED: warned, no authority (honest hard case) | – | – |
| RF trend, witness disagreement, intermittent | QUARANTINED | 1–3 s | 3 s |

- Latency (attack runs): packet guard peak 1.0 ms, trust update peak 0.4 ms, collision loop mean 1.0 ms. The Monte
  Carlo's tick peaks (hundreds of ms) were measured with two heavy jobs sharing the CPU.

## Inputs: real, simulated, interface-only

| | Inputs |
|---|---|
| **Real from the current stack** | Ed25519 signatures (Lane C `radio/client.py`), session id, sequence numbers, packet hashes, freshness, local monotonic arrival, motion / trajectory / intent checks, GNSS-vs-baro altitude, RSSI and carrier Doppler from the channel emulator (`_rf`), Lane C witness reports, the passive WITNESS_DIGEST, negotiation answers |
| **Simulated** | TCAS, passive Mode-S, 1030/1090 reply pairs (`harness/surveillance_sim.py`); two-way ranging, bearing, TDOA, transmitter ids for the sybil test (`harness/rf_sim.py`) |
| **Interface-only / future hardware** | a real TCAS traffic file, a 1090 MHz receiver, SDR / synchronized RF. They plug into `TCASMeasurement`, `ModeSObservation`, `InterrogationReplyEvidence`, `RFMeasurement` and `Node.on_surveillance` without engine changes |

## Limits (said out loud)

1. A brand-new target needs ~5 s (3 s of track + 2 s hold) to become VERIFIED; until then it is a shadow target
   (warning and unilateral advice, no negotiation, no takeover).
2. An attacker with a stolen, registered key who hijacks an identity gets that identity QUARANTINED — including the
   real owner. Safe, but it is a denial of service on that aircraft's coordination.
3. A plausible ADS-B-only ghost with no RF, TCAS or peers around it stays UNVERIFIED: it is warned about, never acted on
   automatically. Only independent sensors can say more.
4. When TCAS contradicts a spoofed ADS-B position, the aircraft TCAS really sees is not drawn as its own ARC track
   (the aircraft's TCAS display shows it).
5. RSSI ranging is coarse (3 dB ≈ ×1.4 distance); Doppler depends on stable oscillators on real radios.
6. Contract: UNVERIFIED travels as SUSPICIOUS + `aw:UNVERIFIED`; `clof`, `alerts`, `alt_geo_ft` and `sid` are additive
   fields — a v1.2 should make them official.
