# Lane B numbers (simulation) — what we can quote

Source: `python harness/montecarlo.py --n 60` — 420 KDVT 25L pattern encounters, 154 of which are NMACs with no avoidance (the scored set) and 183 benign (nuisance set). 10 % packet loss, 0.3 s latency, real terrain and FAA obstacles, pilots react in 5 s and follow an advisory 70 % of the time, half the aircraft AP-equipped. Simulation under the published geometry: it shows a timely intervention, it does not claim a real accident would have been prevented.

| | NMAC rate | median min separation | warning lead (median) | nuisance alerts | NO_SOLUTION |
|---|---|---|---|---|---|
| No avoidance | 100 % | – | – | – | – |
| Straight-line + fixed right 30° | 18 % (28/154) | 903 ft | 41 s | 1 % | 0 |
| ARC, default layers (90/35/20/8 s), required margin 2.0 | 3 % (5/154) | 1,157 ft | 55 s | 3 % | 5 |
| ARC, `FLOCK_LAYERS=early` (90/40/25/8 s) | 3 % (4/154) | 1,224 ft | 55 s | 7 % | 6 |

Run-to-run spread on one version is a few percentage points on NMAC (we saw 3–6 %), so quote "about 5 %" for the default and "about 3 %" for `early`, not a decimal.

What is left, and why:
- Most residual NMACs are two pilot-only aircraft where one or both pilots do not follow the advice (the sim draws 30 % non-compliance). No node logic can fix a pilot who ignores the advisory; only an AP-equipped aircraft can be taken over, and only inside the printed bounds.
- `NO_SOLUTION` is mostly late base-to-final merges where a 10 s bounded maneuver cannot open enough separation, plus low final where automatic action is forbidden below 300 ft AGL. Many of those encounters still end safely.
- Warning lead is bounded by the 90 s horizon: straight-line only sees a base-to-final merge about 43 s before it starts because it cannot know about the turn; ARC sees it at the horizon edge (the brief's 60 s target is not reachable without a longer horizon, which also raised nuisance to 17 % when tried with 120/45/30/8).

Per-tick compute: about 0.5 ms mean (peaks of 15–20 ms under the parallel Monte Carlo load).

Head-on (`head_on_judges.json`, both AP-equipped, pilots ignoring advice, 10 % loss, 40 seeds): 0 of 40 end inside 700 ft, and all 40 take over right/right. Before the commit-ordering fix 2 of 40 ended at about 250 ft because one aircraft flipped to a left turn at takeover.

Required margin (`FLOCK_MARGIN_OK`, default 2.0 NMAC boxes): 1.5 gave 7 % NMAC after the right-of-way merge, 2.0 gives 3 % (mean bank 22 deg), 2.5 gives 2 % but mean bank 27 deg, close to the 30 deg cap, so 2.0 is the default.

Live end-to-end on one laptop (real `world/world_server.py`, `radio/channel.py`, one `node/node.py` per aircraft, `harness/e2e_check.py`):
- `head_on_judges.json`, nobody on the sticks: SEQUENCE -> TRAFFIC -> RESOLVE -> TAKEOVER (both) -> RELEASE -> CLEAR, 954 ft, 0 NMAC.
- `three_on_final.json`: without ARC 3 NMAC pairs (48 ft / 262 ft / 388 ft); with ARC 4 runs gave 0, 0, 0 and 1 NMAC pair (463 ft, N204-N399).
