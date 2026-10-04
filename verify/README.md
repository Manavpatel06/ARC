FLOCK onboard traffic **verification** — advisory only, receive only (`FLOCK_claude_code_prompt.md`, milestones 1–2).

FLOCK checks whether every aircraft on the traffic display really exists where it claims to be, by
cross-checking its ADS-B claims against evidence the aircraft already receives. It produces a per-target
trust score (0–100), a state and plain-English reasons. It never commands a maneuver, never talks to the
autopilot, never transmits.

```
.\run_demo.ps1 -Scenario harness\scenarios\live_kdvt.json      # world + one unit per judge aircraft
python verify/unit.py --id N101 --world ws://localhost:8765    # one unit by hand
python -m pytest -q verify/tests
```
God view: **+ Ghost** injects a spoofed ADS-B aircraft near judge A (simulated radio only). The table shows
what A's unit concluded next to the ground truth. Cockpit: banner, coloured traffic, click a target chip
for its reasons and per-check breakdown. `-Legacy` runs the old collision-avoidance stack.

## Data flow
```
world (truth) --world/sensors.py--> SENSORS (role avionics:<id>, 10 Hz) --> verify/unit.py
   ADS-B it hears · own TCAS tracks · Mode S replies · 1030 interrogations · own-ship state · band stats
verify/unit.py --> VERIFY (1 Hz) --> own cockpit + god + log
world --> GROUND_TRUTH (real / ghost labels) --> god + log only, never units or cockpits
```

## States
| State | Meaning |
|---|---|
| VERIFIED (green) | trust ≥ 70 **and** independent physical confirmation (TCAS agrees or it answers ground radar) |
| UNVERIFIED (yellow) | not enough evidence either way — *not* the same as spoofed |
| SUSPECT (red) | trust ≤ 30 **and** at least one strong check says spoofed |

Guardrails (`guardrails.py`): a target our own TCAS tracks where it claims to be is **never** SUSPECT;
too little evidence is UNVERIFIED; plausible motion alone never verifies; weak doubts never make SUSPECT.

## Checks (milestone 2) — every number in `config.json`
| Check | Real | Spoof evidence | No data (None) |
|---|---|---|---|
| `tcas_consistency` (strongest) | TCAS range / bearing / altitude agree with the ADS-B claim | they disagree, or TCAS sees nothing where it claims to be | beyond TCAS range, TCAS off, still acquiring |
| `modes_presence` | answers ground radar (DF4/5/20/21) or squits DF11 | never answers while the radar is active | no radar heard, too far |
| `kinematics` (weak when OK) | plausible speed / turn / climb, positions follow its own velocity | teleports, impossible rates | < 3 reports |

Fusion (`fusion.py`): weighted log-odds from a neutral prior, smoothed over time but fast on strong evidence.

## Simulation (world side)
`world/sensors.py`: ADS-B position + velocity at 2 Hz each with free-space RSSI from the *transmitter's* true
position; own TCAS at 1 Hz (transponder-equipped aircraft only, 12 NM, range σ 15 m, bearing σ 5°); DF11 at
1 Hz; rotating Phoenix approach radar (4.8 s) → DF4/5 replies; own-ship state 5 Hz. `world/attacks.py`:
single ghost (ADS-B only, ground transmitter, plausible straight track). Values are estimates, not from a
standard. Next: ghost flock, position drift, replay, ICAO masquerade, jamming, own-ship GPS spoofing.
