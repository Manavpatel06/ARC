# Lane B numbers (simulation) — what we can quote

Source: `python harness/montecarlo.py --n 60` — 420 KDVT 25L pattern encounters, 154 of which are NMACs with no avoidance (the scored set) and 183 benign (nuisance set). 10 % packet loss, 0.3 s latency, real terrain and FAA obstacles, pilots react in 5 s and follow an advisory 70 % of the time, half the aircraft AP-equipped. Simulation under the published geometry: it shows a timely intervention, it does not claim a real accident would have been prevented.

| | NMAC rate | median min separation | warning lead (median) | nuisance alerts | NO_SOLUTION |
|---|---|---|---|---|---|
| No avoidance | 100 % | – | – | – | – |
| Straight-line + fixed right 30° | 18 % (28/154) | 903 ft | 41 s | 1 % | 0 |
| ARC, default layers (90/35/20/8 s), required margin 2.0 (rerun 4 Oct, same numbers) | 3 % (5/154) | 1,169 ft | 55 s | 3 % | 4 |
| ARC, SEQUENCE moved to 120 s (120/35/20/8 s), tried 4 Oct, not adopted | 5 % (8/154) | 1,159 ft | 56 s | 3 % | 3 |
| ARC, earlier alerts and takeover (90/60/35/20 s), tried 4 Oct, not adopted | 16 % (25/154) | 1,227 ft | 55 s | 34 % | 10 |
| ARC, `ARC_LAYERS=early` (90/40/25/8 s) | 3 % (4/154) | 1,224 ft | 55 s | 7 % | 6 |

Run-to-run spread on one version is a few percentage points on NMAC (we saw 3–6 %), so quote "about 5 %" for the default and "about 3 %" for `early`, not a decimal.

What is left, and why:
- Most residual NMACs are two pilot-only aircraft where one or both pilots do not follow the advice (the sim draws 30 % non-compliance). No node logic can fix a pilot who ignores the advisory; only an AP-equipped aircraft can be taken over, and only inside the printed bounds.
- `NO_SOLUTION` is mostly late base-to-final merges where a 10 s bounded maneuver cannot open enough separation, plus low final where automatic action is forbidden below 300 ft AGL. Many of those encounters still end safely.
- Why SEQUENCE starts at 90 s, not 120 s (tested 4 Oct, deterministic seeds, run twice): at 120 s, NMAC 8 vs 5 of 154 with the same nuisance rate (3 %) and only 1 s more warning, and accept_b three_on_final fails (362 ft vs 1,196 ft): a landing-order plan made on a 2-minute prediction gets cancelled after a pilot has already extended downwind. A 120 s "pattern traffic - monitor" stage with planning kept at 90 s fixed that seed but broke another (170 ft). Two-minute predictions of GA pilots are too uncertain to move anyone (see harness/out/learned_report.md: real 60 s error is already ~700 m).
- Why not alert at 60 s and take over at 20 s (90/60/35/20, tested 4 Oct, same seeds): NMAC 16 % vs 3 %, nuisance traffic alerts 34 % vs 3 %, NO_SOLUTION 10 vs 4; accept_b 12/16. At 60 s most pattern conflicts have not formed yet (pilots have not turned), so alerts fire on traffic that would have been fine, and a takeover at 20 s commits to a 10 s bounded maneuver on a prediction that is still changing. TCAS uses 15-35 s for the same reason.
- Warning lead is bounded by the 90 s horizon: straight-line only sees a base-to-final merge about 43 s before it starts because it cannot know about the turn; ARC sees it at the horizon edge (the brief's 60 s target is not reachable without a longer horizon, which also raised nuisance to 17 % when tried with 120/45/30/8).

Per-tick compute: about 0.5 ms mean (peaks of 15–20 ms under the parallel Monte Carlo load).

Head-on (`head_on_judges.json`, both AP-equipped, pilots ignoring advice, 10 % loss, 40 seeds): 0 of 40 end inside 700 ft, and all 40 take over right/right. Before the commit-ordering fix 2 of 40 ended at about 250 ft because one aircraft flipped to a left turn at takeover.

Required margin (`ARC_MARGIN_OK`, default 2.0 NMAC boxes): 1.5 gave 7 % NMAC after the right-of-way merge, 2.0 gives 3 % (mean bank 22 deg), 2.5 gives 2 % but mean bank 27 deg, close to the 30 deg cap, so 2.0 is the default.

Live end-to-end on one laptop (real `world/world_server.py`, `radio/channel.py`, one `node/node.py` per aircraft, `harness/e2e_check.py`):
- `head_on_judges.json`, nobody on the sticks: SEQUENCE -> TRAFFIC -> RESOLVE -> TAKEOVER (both) -> RELEASE -> CLEAR, 954 ft, 0 NMAC.
- `three_on_final.json`: without ARC 3 NMAC pairs (48 ft / 262 ft / 388 ft); with ARC 4 runs gave 0, 0, 0 and 1 NMAC pair (463 ft, N204-N399).

## Intent without a link: ADS-B Target State & Status (`ARC_INTENT`)

Question: how much does ARC lose if the peer's declared leg and `BASE_IN_12S` intent (ARC-link only fields) are not
available, and does the intent real ADS-B v2 already carries help? `ARC_INTENT=link` (default, unchanged) uses
the link fields; `adsb` ignores them and reads Target State & Status selected heading / selected altitude
(AP-equipped aircraft only, ~half; the heading bug moves to the next leg 6 s before the turn, `ARC_TSS_LEAD_S`);
`none` uses position / velocity only. Intent is never believed from a SUSPICIOUS / QUARANTINED target.
Run: `ARC_INTENT=<mode> python harness/montecarlo.py --n 60 [--kinds ...]`.

Standard pattern (the 7 kinds above, 154 NMAC / 183 benign): identical in all three modes - 3 % NMAC, 1,169-1,177 ft
median min separation, 55 s detect lead, 3 % nuisance. The sim's pilots fly the predictor's own pattern model, so
being told the turn adds nothing; dropping the link costs nothing here.

Pattern-breaking pilots (new kinds, 60 each: `early_base_cutoff` base 0.9-1.7 km early, `extended_downwind_cutoff`
20-40 s past the turn point, `extended_downwind_spaced` the extension puts A behind the straight-in):

| | early base: detect lead / NMAC | extended downwind: detect lead | spaced extension: nuisance alerts |
|---|---|---|---|
| baseline (straight-line) | 62 s / 2 of 26 | 37 s | 0 of 60 |
| ARC, link intent | 64 s / 3 of 26 | 77 s | 6 of 60 |
| ARC, ADS-B target state | **67 s** / 3 of 26 | 77 s | 6 of 60 |
| ARC, no intent | 64 s / 3 of 26 | 77 s | 6 of 60 |

- ADS-B target state helps only the early base: +3 s median detection (paired: earlier in 9 of 35 conflicts, later in
  none), NMAC unchanged. A selected heading cannot say "I will turn later", so extensions are invisible to it.
- Pattern breakers do not hurt ARC: it detects the early base at 64 s vs baseline 62 s and the extended downwind at
  77 s vs 37 s. Its smaller early-base min separation (1,180 vs 1,730 ft) is by design (2-box margin, 21 deg mean bank
  vs the baseline's fixed 30 deg turn), and its alert comes after a SEQUENCE layer instead of at detection.
- The 6 of 60 nuisance alerts on spaced extensions are not mispredicted turns: 5 fire at 135-149 s, when A is on
  final while the straight-in ahead lands (runway occupancy); 1 fires early (43 s), in line with the 3 % elsewhere.
- Tried and rejected: falling back to straight-line once an aircraft is 250 m past the model's turn point
  (nuisance 12 % -> 10 %, but extended-downwind detection 77 s -> 54 s, because it stops predicting the late base).
