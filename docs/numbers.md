# Lane B numbers (simulation) — what we can quote

Source: `python harness/montecarlo.py --n 60` — 420 KDVT 25L pattern encounters, 154 of which are NMACs with no avoidance (the scored set) and 183 benign (nuisance set). 10 % packet loss, 0.3 s latency, real terrain and FAA obstacles, pilots react in 5 s and follow an advisory 70 % of the time, half the aircraft AP-equipped. Simulation under the published geometry: it shows a timely intervention, it does not claim a real accident would have been prevented.

| | NMAC rate | median min separation | warning lead (median) | nuisance alerts | NO_SOLUTION |
|---|---|---|---|---|---|
| No avoidance | 100 % | – | – | – | – |
| Straight-line + fixed right 30° | 18 % (28/154) | 903 ft | 41 s | 1 % | 0 |
| FLOCK, default layers (90/35/20/8 s) | 6 % (9/154) | 1,153 ft | 55 s | 3 % | 12 |
| FLOCK, `FLOCK_LAYERS=early` (90/40/25/8 s) | 3 % (4/154) | 1,224 ft | 55 s | 7 % | 6 |

Run-to-run spread on one version is a few percentage points on NMAC (we saw 3–6 %), so quote "about 5 %" for the default and "about 3 %" for `early`, not a decimal.

What is left, and why:
- Most residual NMACs are two pilot-only aircraft where one or both pilots do not follow the advice (the sim draws 30 % non-compliance). No node logic can fix a pilot who ignores the advisory; only an AP-equipped aircraft can be taken over, and only inside the printed bounds.
- `NO_SOLUTION` is mostly late base-to-final merges where a 10 s bounded maneuver cannot open enough separation, plus low final where automatic action is forbidden below 300 ft AGL. Many of those encounters still end safely.
- Warning lead is bounded by the 90 s horizon: straight-line only sees a base-to-final merge about 43 s before it starts because it cannot know about the turn; FLOCK sees it at the horizon edge (the brief's 60 s target is not reachable without a longer horizon, which also raised nuisance to 17 % when tried with 120/45/30/8).

Per-tick compute: about 0.5 ms mean (peaks of 15–20 ms under the parallel Monte Carlo load).
