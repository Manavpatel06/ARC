# Handoff: AirWitness-Hybrid (Lane B, Reya -> Manas)

State of `lane-b-node` at the handoff: the passive AirWitness-Hybrid engine is in (commit `8634fdf`), tests 133 pass,
`harness/accept_b.py` 16/16, RED FLOCK 14/14 attacks blocked, Monte Carlo FLOCK 5/154 NMAC (3 %), 0 false spoof
alerts in 420 legitimate encounters. Full design and numbers: `docs/airwitness.md`.

Re-run everything:

```
python -m pytest -q
python harness/accept_b.py
python harness/redflock_spoof.py
python harness/montecarlo.py --n 60
```

## 1. What was removed (challenge-response) — review before merging to integration

See every removed line with:

```
git diff d266114 8634fdf -- node/airwitness.py node/node.py harness/redflock_spoof.py
```

| Removed | File / function | What it did |
|---|---|---|
| `import secrets`, `nonce_fn`, `self._nonce`, `self._respond` | `node/airwitness.py` `AirWitness.__init__` | random nonces to challenge peers; queue of echoes we owed |
| `CHALLENGE_EVERY_S`, `CHALLENGE_TIMEOUT_S`, `CHALLENGE_VALID_S` | `node/airwitness.py` constants | challenge every 10 s, wait 5 s, valid 25 s |
| `chal_pending`, `chal_ok_t`, `chal_misses`, `chal_last`, `neg_miss` | `node/airwitness.py` `_Track` | per-peer challenge / no-answer state |
| HEARTBEAT `"c"` (challenge to us -> echo) and `"r"` (echo of ours -> PASS) | `AirWitness.on_message` | the request/response itself |
| `AirWitness.tick()` | whole method | issued nonces, timed them out, counted misses and negotiation misses |
| replay exemption "owner answered a fresh signed challenge" | `AirWitness.assess` | now: exemption only if the identity's own live signed track is clean (`_clean_live_track`) |
| `challenge: PASS / NO RESPONSE / PENDING` check and its score penalty | `AirWitness.assess` | — |
| `negotiation: NO ANSWER xN` penalty | `AirWitness.assess` | negotiation is now positive-only evidence; silence costs nothing |
| `extra = self.aw.tick(now)` + challenge/echo HEARTBEATs | `node/node.py` `Node._periodic_radio` | HEARTBEAT is now only `{"alive": true, "w": <passive witness digest>}` |
| N204 answering our challenges | `harness/redflock_spoof.py` | — |

Guard against it coming back: `node/tests/test_airwitness.py::test_no_challenge_path_left` fails if `AirWitness.tick`,
`nonce` or `chal_` reappear, or if a HEARTBEAT carries anything except `alive` and `w`.

Also changed in `node/node.py` (not challenge code, but review it):
- `on_radio`: a new peer is assessed on its first STATE (was: TRUSTED by default until the first 2 Hz assessment).
- `on_radio`: `MANEUVER_COMMIT` from a peer that is not VERIFIED is ignored (no 4D contract with shadow traffic).
- `_do_unverified_resolve`: trust-robust escape (`trust_robust`), RESOLVE reason carries `trust_robust`, `clof`,
  `takeover_inhibited`.
- STATE body: new `sid` (session id); TRUST frame: new `clof`; `Node.on_surveillance`, `Node.latency_summary`.

## 2. Limitations to fix (prioritized)

| # | Limitation | Where | Suggested fix |
|---|---|---|---|
| 1 | **5 s trust start-up.** A new peer needs `VERIFY_MIN_TRACK_S` 3 s + `UPGRADE_HOLD_S` 2 s to become VERIFIED; until then no negotiation / takeover with it. Matters only when aircraft first hear each other already in conflict (`accept_b` "below 300 ft" scenario). | `node/airwitness.py` constants, `_assess` | shorten for signed peers with clean packets + physics (e.g. 1.5 s + 1 s), or let a strong independent source (TCAS PASS, witnesses PASS) skip the track-length wait; re-run RED FLOCK + false-positive check after |
| 2 | **Stolen-key identity hijack quarantines the real owner too** (attack 8). Safe, but a denial of service on that aircraft's coordination. | `_guard_and_ingest` (`seq_jump`), `_assess` gates | split the track by position cluster: keep the branch consistent with the old track / RF / witnesses, quarantine the other; needs key revocation from Lane C for a real fix |
| 3 | **Plausible ADS-B-only ghost** with no RF, TCAS or peers stays UNVERIFIED (warned, never acted on automatically). | inherent | only more independent sensors help: wire a real 1090 receiver / TCAS feed into `Node.on_surveillance` |
| 4 | **TCAS-seen aircraft not drawn.** When TCAS contradicts a spoofed ADS-B position, the aircraft TCAS really sees is not created as its own track (QUARANTINED claim keeps cap only if TCAS *agrees*). | `AirWitness.add_tcas`, `Node` peers | create a TCAS-only shadow track (range/bearing/rel-alt -> position, wide tube, TRAFFIC cap) for unassociated or contradicting TCAS returns |
| 5 | **Surveillance and fine RF are simulated.** TCAS / Mode-S / 1030-1090 come from `harness/surveillance_sim.py`; ranging / bearing / TDOA from `harness/rf_sim.py`. | interfaces in `node/airwitness.py` | plug real feeds into `TCASMeasurement`, `ModeSObservation`, `InterrogationReplyEvidence`, `RFMeasurement` (no engine change); live node `run()` does not read any surveillance source yet |
| 6 | **RSSI ranging is coarse** (3 dB ≈ ×1.4 distance) — FLOCK-band ghosts flag in ~3 s only because Doppler helps; Doppler on real radios needs stable oscillators. | `_rf_location`, `_doppler_trend` | calibrate sigmas against Mansi's real radios; keep Doppler as "confirms another failure" only |
| 7 | **Likelihood table is hand-set**, not fitted. | `LIK` in `node/airwitness.py` | fit on recorded / replayed traffic (OpenSky KDVT) + RED FLOCK runs; report false-alert rate per flight hour |
| 8 | **Contract**: UNVERIFIED goes on the wire as SUSPICIOUS + `aw:UNVERIFIED`; `clof`, `alerts`, `alt_geo_ft`, `sid`, HEARTBEAT `w` are additive fields. | `schemas.py`, `web/cockpit.js` | propose contract v1.2 with the four AirWitness states and these fields; show CLOF layer + P(real/spoof/faulty) in the cockpit panel |
| 9 | **Live test on the real stack not re-run** after this change (world + Lane C signed radio + channel + `radio/spoofer.py`). The in-process harnesses pass. | — | run two nodes + spoofer against the world, confirm real peers VERIFY in ~5 s and the ghost is alerted |
