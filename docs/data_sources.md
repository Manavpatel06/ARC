# ARC — data sources

Check each source's current terms, rate limits and authentication before relying on it; the notes below are
what our code assumes, not legal advice. **Recordings of real traffic are not committed to the repo.**

| Source | Used for | How | Notes |
|---|---|---|---|
| Simulation (`world/sensors.py`) | everything physical: ADS-B with RSSI, own TCAS, Mode S replies, 1030 interrogations, own GPS, band stats, attacks with ground truth | built in | the only source with ground-truth labels; drives the demo and `eval/` |
| airplanes.live API | real ADS-B state vectors around KDVT (display, replay) | `data/live_traffic.py` (tried first) | free community API; rate-limited — we poll every 5 s |
| adsb.lol API | same, fallback | `data/live_traffic.py` | open-data API (check its licence before redistributing data) |
| OpenSky Network REST | same, fallback | `data/live_traffic.py` | anonymous access is rate-limited; historical data needs a research account |
| ADS-B Exchange | not used | — | historical samples free, live API paid |
| RTL-SDR + readsb / dump1090-fa (Beast, TCP 30005) | raw Mode S frames with 12 MHz timestamps and signal level | `sources/sdr_beast.py` | receive only; positions need `pip install pyModeS` (local CPR decoding against our position) |
| KDVT METAR (aviationweather.gov) | wind / runway flow, QNH, density altitude | `data/metar.py` | |
| KDVT runways (FAA 5010 via AirNav), terrain | pattern geometry, TAWS | `data/runways.py`, `data/terrain.py` | |

Public feeds give **decoded state vectors**, not raw frames or signal strength: use them for realistic
traffic and trajectories (`world/replay.py` plays a recording back as real, transponder-equipped aircraft),
and use simulation or our own SDR for the physical-layer checks (TCAS, Mode S timing, RSSI).

Record on the venue Wi-Fi: `python data/live_traffic.py --world ws://localhost:8765 --record`
(→ `harness/out/live_<ts>.jsonl`); replay: `world_server.py --replay <file>` or scenario `"replay": "<file>"`.
