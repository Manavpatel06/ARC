ARC onboard traffic **verification** — advisory only, receive only (`FLOCK_claude_code_prompt.md`).

ARC checks whether every aircraft on the traffic display really exists where it claims to be, by
cross-checking its ADS-B claims against evidence the aircraft already receives. Per target: trust 0–100,
VERIFIED / UNVERIFIED / SUSPECT and plain-English reasons. It never commands a maneuver, never talks to the
autopilot, never transmits.

```
.\run_demo.ps1 -Scenario harness\scenarios\live_kdvt.json      # world + one unit per judge aircraft
python verify/unit.py --id N101 --world ws://localhost:8765    # one unit by hand (signed log in harness/out/verify/)
python -m verify.eventlog harness/out/verify/N101_events.jsonl # check the signed, hash-chained event log
python eval/run_eval.py                                        # detection / false alarms / ROC / ablation
python -m pytest -q verify/tests
```

| File | |
|---|---|
| `schema.py` | normalized sensor message + VERIFY output |
| `tracks.py` | per-ICAO tracks; an address heard from two places splits into two branches |
| `checks/` | `tcas_consistency`, `modes_presence`, `timing_1030`, `rssi`, `emitter_cluster`, `kinematics`, `replay_detect`, `popin` |
| `fusion.py`, `guardrails.py` | weighted log-odds → trust; the rules that may never be broken |
| `unit.py` | the onboard unit: jamming / own-GPS guards, banners, alerts, signed frames, event log |
| `eventlog.py` | Ed25519-signed, hash-chained log; signed VERIFY frames |
| `config.json` | every threshold and weight |

Docs: `docs/architecture.md`, `docs/checks.md` (every check, fusion, guardrails), `docs/data_sources.md`,
`docs/limitations.md`, `docs/pitch_notes.md`, `docs/demo_script.md`. Results: `eval/out/report.md`.
