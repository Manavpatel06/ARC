# ARC: Autonomous Resolution & Coordination

**Coordinated, spoof-checked collision avoidance for small aircraft, in software.**
Devils Invent 2026 (Honeywell Aerospace × ASU), Problem Statement 2: *Automated TCAS for General Aviation*.
Team: Manav Patel · Mansi · Manas · Reya (Arizona State University).

Small aircraft can see each other on ADS-B, but they cannot agree on what to do. In the Marana midair
(19 Feb 2025) two pilots converged over a runway, each expecting the other to turn. ARC runs on
every aircraft. It predicts where traffic is turning, gets both aircraft to agree who moves by FAA
right-of-way rules, warns both pilots, and only if nobody acts flies a bounded maneuver through the
existing autopilot. It also refuses to act on aircraft that are not real: every target is
cross-checked for ADS-B spoofing before it may trigger anything.

## How it works

| Step | What ARC does | Technique | Code |
|---|---|---|---|
| Predict | Where will each aircraft be in the next 90 s? | Traffic-pattern leg classifier (Gaussian likelihoods on position, heading, altitude) + kinematic turn model; uncertainty grows with time | `node/predict.py` |
| Detect | Is there a conflict, and how soon? | Closest approach on a 0.25 s grid; a near miss (500 ft / 100 ft box) must survive after subtracting 1σ; 4 layers with hysteresis | `node/conflict.py`, `node/layers.py` |
| Agree | Who moves, which way? | 14 CFR 91.113 right-of-way + landing-order sequencing; same deterministic rules on both aircraft; signed commits when a link exists; lost link → both turn right | `node/rightofway.py`, `node/negotiate.py` |
| Command | Which maneuver, and may ARC fly it? | Simulates 9 candidate maneuvers 30 s ahead against terrain (USGS), obstacles (FAA), density-altitude climb; least severe with ≥ 2× NMAC margin; separate bounds monitor | `node/escape.py`, `node/authority.py` |
| Trust | Is the target real? | Ed25519-signed radio + replay guard; Bayesian evidence fusion (AirWitness); 8-check verification unit with log-odds fusion, guardrails and a hash-chained signed event log | `radio/crypto.py`, `node/airwitness.py`, `verify/` |

**Timeline (seconds to conflict):** landing order at 90 s, traffic alert at 35 s, agreed maneuver at 20 s, takeover only if
nobody acted by 8 s. **Bounds:** max 30° bank, speed ≥ 62 kt (1.3 Vs), no automatic action below 300 ft AGL on final,
≤ 10 s authority, any stick input returns control immediately.

Full write-up: [`docs/architecture.md`](docs/architecture.md) · checks: [`docs/checks.md`](docs/checks.md) ·
message contract: [`docs/interface.md`](docs/interface.md) + [`schemas.py`](schemas.py).

## Run the demo

Requirements: Python 3.11+, a browser with GPU acceleration (3D view), laptops on one network.

```
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: . .venv/bin/activate
pip install -r requirements.txt

.\run_demo.ps1 -Arc               # Windows   (macOS/Linux: ./run_demo.sh --arc)
.\run_demo.ps1 -Stop              # stop everything
```

`-Arc` starts the world simulator, the radio channel, one ARC node per aircraft, one verification unit per pilot
aircraft, and a live feed of real ADS-B traffic around Deer Valley (KDVT). Scenario: `harness/scenarios/arc_head_on.json`.
Two aircraft start 5 NM apart, nose to nose. Without ARC they collide after about 83 s.

Open on the other laptops (replace the IP with the world laptop's):

| View | URL |
|---|---|
| Cockpit A / B | `http://<ip>:8080/index.html?role=cockpitA` / `...?role=cockpitB` (game controller supported) |
| God view | `http://<ip>:8080/index.html?role=god`, with **Reset demo** and attack buttons (Ghost swarm, Drift, Replay, ...) |
| Comms log | `http://<ip>:8080/log.html` |

3D terrain: append `&ion=<Cesium ion token>` once per browser. Without it the view uses flat terrain with OpenStreetMap imagery.
Other modes: `.\run_demo.ps1` (verification only, advisory) and `-Scenario harness\scenarios\live_kdvt.json` (free flight with traffic).
Step-by-step: [`docs/demo_script.md`](docs/demo_script.md), [`docs/RUNBOOK.md`](docs/RUNBOOK.md).

## Results

All simulation results are reproducible with the command shown. Fleet and accident numbers are sourced in the pitch deck.

| What | Result | Command |
|---|---|---|
| 154 simulated KDVT near-miss encounters (10% radio loss, pilots react in 5 s, follow advice 70% of the time) | Near misses: 18% for a straight-line baseline that always turns right, 3% with ARC. Warning lead 55 s vs 41 s. False traffic alerts 3% | `python harness/montecarlo.py --n 60` |
| Live head-on, 5 NM nose to nose, hands off, 4 fake aircraft injected | 3 ft apart without ARC; 859–1,149 ft with ARC (3 runs); fakes never targeted | `.\run_demo.ps1 -Arc` + `harness/e2e_check.py` |
| Spoof attacks, 7 types × 4 seeds, live KDVT traffic | Where TCAS is fitted: all caught, 1–5 s, 0 of 31 real aircraft flagged, ROC AUC 0.986. ADS-B In only: 4 of 5 fake-aircraft attacks caught; slow drift needs TCAS | `python eval/run_eval.py --seeds 4`, `python eval/ga_only.py` |
| Turn prediction on 116 real KDVT aircraft (14 h of recorded ADS-B) | Turn-aware beats straight-line on 4 of 5 pattern legs (26–42% lower error) | `python harness/fit_learned.py` |
| Timing choices | 120 s landing order and 60/20 s alert/takeover tested and rejected (more near misses, 34% false alarms) | [`docs/numbers.md`](docs/numbers.md) |
| Tests | 162 unit tests, 16/16 acceptance checks (bounds, release within one tick, radio blackout, three on final, no-solution) | `pytest node world verify`, `python harness/accept_b.py`, `node web/tests/run.mjs` |

Details: [`docs/numbers.md`](docs/numbers.md), [`eval/out/report.md`](eval/out/report.md), judge Q&A [`docs/qa.md`](docs/qa.md).

## Limitations (said out loud)

Flight dynamics, the radio link, and the TCAS / Mode S / signal-strength sensors are simulated. Terrain, obstacles, weather
and background ADS-B traffic are real data. No over-the-air test, no pilot-in-the-loop evaluation, not certified.
Takeover is not yet gated on own-GPS integrity, and there is no TCAS-style sense reversal. Passive spoof checks raise the cost
of spoofing; they do not make it impossible. Full list: [`docs/limitations.md`](docs/limitations.md).

## Repository map

| Folder | Contents |
|---|---|
| `node/` | ARC onboard logic: prediction, conflict detection, layers, right-of-way, negotiation, escape search, bounds monitor, AirWitness |
| `verify/` | Onboard traffic verification unit: 8 checks, fusion, guardrails, signed event log |
| `world/` | Simulation world: flight model, AI traffic, sensors, attack models, WebSocket hub |
| `radio/` | Air-to-air channel emulator (loss, latency, range), Ed25519 signing, spoofer, robustness tests |
| `web/` | Cockpit (2D/3D), god view, comms log; vanilla JS, Canvas, CesiumJS |
| `data/` | Live ADS-B feed, METAR, terrain (USGS 3DEP), obstacles (FAA DOF), runways |
| `harness/` | Scenarios, Monte Carlo, acceptance checks, end-to-end checker, real-data fit |
| `eval/` | Spoof-detection evaluation, ROC and ablation |
| `docs/` | Architecture, checks, numbers, Q&A, limitations, demo script; `docs/process/` = hackathon planning notes |
