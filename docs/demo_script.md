# FLOCK — demo script (verification)

Setup: `.\run_demo.ps1 -Scenario harness\scenarios\live_kdvt.json` (world + one FLOCK onboard unit per judge;
live sky on unless `-NoLive`). Laptops: cockpit A, cockpit B, god view. Optional: add `"replay":
"harness/out/live_<ts>.jsonl"` to the scenario (or `--replay`) to fly with recorded real traffic.
Reset between runs: god view **⟲ Reset demo** (twice). Attack row: one click on, one click off.

| # | Do | Judges see |
|---|---|---|
| 1 | Nothing — fly. | Cockpit banner **TRAFFIC VERIFIED · n of n**; every target green; click one: "TCAS confirms it", "answers ground radar", "radar replies time-match its claimed position". God view: Truth `real` = FLOCK `VERIFIED` ✓. |
| 2 | God: **Ghost flock**. | Within ~5 s four red targets: **SUSPECT TRAFFIC · …**; reasons "TCAS sees nothing where it claims to be", "never answers ground radar", "same transmitter as …". God view draws the ghosts and the one spoofer transmitter they really come from. |
| 3 | God: **Drift**. | One real aircraft's ADS-B starts walking away: yellow "ADS-B and TCAS starting to disagree" → red "ADS-B says 1.5 NM, TCAS measures 0.6 NM" while TCAS keeps seeing it where it really is. |
| 4 | God: **ICAO masquerade** (or Replay). | A second "N229 (2)" appears and goes red ("its ICAO address is also in use … away"); the real N229 stays green — a stolen address does not take the real owner down. |
| 5 | God: **Jamming**. | Amber banner **TRAFFIC PICTURE DEGRADED – increase lookout, notify ATC**; nothing is marked spoofed just because it went quiet. |
| 6 | God: **Own GPS spoof**. | Amber **OWN POSITION UNCERTAIN – position checks widened**; real traffic is not condemned even though every ADS-B position now looks "off". |
| 7 | Cockpit: click a red target → **Report to ATC…** | Pre-filled report with position, claimed position, evidence and phraseology (nothing is sent). |
| 8 | Show `eval/out/report.md`. | Detection / false-alarm table, time to detect, ROC, ablation (TCAS-only fails the hard cases). |
| 9 | `python -m verify.eventlog harness\out\verify\N101_events.jsonl` | "OK – n entries, chain and signatures OK"; edit one line → "FAIL entry k: content changed". |

Talking points: advisory only (FLOCK never flies the aircraft or contradicts TCAS); receive only; unverified ≠ spoofed.
