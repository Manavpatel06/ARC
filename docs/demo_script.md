# ARC — final live demo (both layers in one run)

Setup: `.\run_demo.ps1 -Arc` (scenario `harness/scenarios/arc_head_on.json`: N101 and N102 5 NM apart, nose to nose over
KDVT at 2,500 ft MSL, ~100 kt each; without ARC they collide after about 83 s). World with takeover allowed, radio
channel, one ARC node per aircraft, one verification unit per judge, live sky on both cockpit radars.
Laptops: cockpit A, cockpit B, god view. Restart the encounter: god view **Reset demo** (and **Stop all** for attacks).

| t after Reset (s) | Distance | Do | Judges see (cloud test, 4 Oct) |
|---|---|---|---|
| 0 | 5 NM | Hands off. | Cockpit: N102 at 12 o'clock, same altitude, **TRAFFIC VERIFIED · 1 of 1**. God view: two aircraft on one line. |
| ~10 | 4.4 NM | God: **Ghost swarm**. | Four red SUSPECT fakes within 2-5 s; the other judge stays VERIFIED 100. |
| ~37 | 3 NM | Nothing. | **POSSIBLE CONFLICT - MONITOR**: ARC has seen the head-on. |
| ~44 | 2 NM | Nothing. | **TRAFFIC - 12 O'CLOCK** in both cockpits. |
| ~59 | 1.2 NM | Nothing. | **RESOLVE**: both told to turn right (FAA head-on rule). |
| ~71 | 0.5 NM | Nothing. | **ARC HAS THE AIRCRAFT - RIGHT 20** on both, bounds panel. |
| ~81 | | Touch a stick (optional). | **YOUR AIRCRAFT**. Closest approach 860-1,150 ft (3 runs); without ARC 3 ft. Fakes never targeted. |

# ARC — demo script (verification)

Setup: `.\run_demo.ps1 -Scenario harness\scenarios\live_kdvt.json` (world + one ARC onboard unit per judge;
live sky on unless `-NoLive`). Laptops: cockpit A, cockpit B, god view. Optional: add `"replay":
"harness/out/live_<ts>.jsonl"` to the scenario (or `--replay`) to fly with recorded real traffic.
Reset between runs: god view **⟲ Reset demo** (twice). Attack row: one click on, one click off.

| # | Do | Judges see |
|---|---|---|
| 1 | Nothing — fly. | Cockpit banner **TRAFFIC VERIFIED · n of n**; every target green; click one: "TCAS confirms it", "answers ground radar", "radar replies time-match its claimed position". God view: Truth `real` = ARC `VERIFIED` ✓. |
| 2 | God: **Ghost swarm**. | Within ~5 s four red targets: **SUSPECT TRAFFIC · …**; reasons "TCAS sees nothing where it claims to be", "never answers ground radar", "same transmitter as …". God view draws the ghosts and the one spoofer transmitter they really come from. |
| 3 | God: **Drift**. | One real aircraft's ADS-B starts walking away: yellow "ADS-B and TCAS starting to disagree" → red "ADS-B says 1.5 NM, TCAS measures 0.6 NM" while TCAS keeps seeing it where it really is. |
| 4 | God: **ICAO masquerade** (or Replay). | A second "N229 (2)" appears and goes red ("its ICAO address is also in use … away"); the real N229 stays green — a stolen address does not take the real owner down. |
| 5 | God: **Jamming**. | Amber banner **TRAFFIC PICTURE DEGRADED – increase lookout, notify ATC**; nothing is marked spoofed just because it went quiet. |
| 6 | God: **Own GPS spoof**. | Amber **OWN POSITION UNCERTAIN – position checks widened**; real traffic is not condemned even though every ADS-B position now looks "off". |
| 7 | Cockpit: click a red target → **Report to ATC…** | Pre-filled report with position, claimed position, evidence and phraseology (nothing is sent). |
| 8 | Show `eval/out/report.md`. | Detection / false-alarm table, time to detect, ROC, ablation (TCAS-only fails the hard cases). |
| 9 | `python -m verify.eventlog harness\out\verify\N101_events.jsonl` | "OK – n entries, chain and signatures OK"; edit one line → "FAIL entry k: content changed". |

Talking points: advisory only (ARC never flies the aircraft or contradicts TCAS); receive only; unverified ≠ spoofed.
