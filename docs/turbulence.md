# ARC in turbulence — Dryden add-on experiment (branch `lane-b-turbulence`)

Question: is ARC strong enough in turbulent air, and does anything need to change?  Everything here is an
**add-on**: the world's existing weather (`world/weather.py`, `world/flight_model.py`) is not modified, and every
existing run (`harness/montecarlo.py` without `--turb`, `run_demo.ps1`) behaves exactly as before.

## What was added

| File | What |
|---|---|
| `harness/dryden.py` | Dryden gust model, MIL-F-8785C / MIL-HDBK-1797 low-altitude form (u, v, w; W20 = 15 / 30 / 45 kt for light / moderate / severe). Verified: output standard deviation within 1 % of the spec on all three axes. |
| `harness/simworld.py`, `harness/montecarlo.py` | opt-in `turbulence=` / `--turb 1\|2\|3`: gusts push each aircraft off the path it flies, the pilot works it back (20 s horizontal, 12 s vertical), the airframe smooths horizontal gusts (1.5 s). Default 0 = unchanged. |
| `harness/dryden_world.py` | live launcher: the normal world server with Dryden layered in at run time (continuous vertical / along / side gusts); the world's own bank swell, bumps, METAR gusts, shear and thermals stay. Passes the world's own smoothness and preset tests. |

## What turbulence broke, and what changed in ARC

Monte Carlo, 60 encounters per kind (420 per level), 10 % radio loss, 0.3 s latency.  "Before" = `lane-b-node`
(eba9cf5 / 47f7a54) flown in the same Dryden air; "after" = this branch.  These numbers were measured on that
pre-rename base; the branch has since been rebuilt on the ARC-named `main` (tests 165/165, `accept_b` 16/16, RED ARC
14/14 pass there) and the Monte Carlo comparison should be re-run on it before merging.

| Turbulence | NMAC | median min-sep | nuisance alerts (benign) | NO_SOLUTION |
|---|---|---|---|---|
| calm | 5 -> **5** / 154 | 1169 -> **1177** ft | ~3 % -> **0 %** | 4 -> **3** |
| light | 4 -> **4** / 164 | 1137 -> **1172** ft | 3.3 % -> **0 %** | 8 -> **7** |
| moderate | 2 -> **2** / 165 | 1129 -> **1139** ft | 7 % -> **3.8 %** | 14 -> **11** |
| severe | 3 -> **3** / 164 | 1092 -> **1110** ft | 16 % -> **12.5 %** | 13 -> **10** |

Real aircraft wrongly flagged as spoofed (AirWitness), same 420 legitimate encounters per level: moderate 88 -> **0**;
after: calm 0, light 0, moderate 0, severe 1 (an acceleration limit tripped once in severe gusts).
Also after: 133 tests pass, `accept_b` 16/16, RED ARC 14/14 attacks blocked.

**Collision avoidance itself was already turbulence-robust** (NMAC did not rise with turbulence).  What turbulence
broke was false alarms.  Three causes, three changes:

1. **AirWitness flagged landing aircraft as spoofed** (88 of 420 at moderate): on the rollout a crosswind gust swings
   the ground track of a 5-10 kt aircraft by tens of degrees.  The turn-rate limit is a flight limit: it no longer
   applies below 40 kt ground speed (`TAXI_KT`, `node/airwitness.py`).  Real aircraft taxi and turn off fast too.
2. **Two landing rolls overlapping on the runway counted as a conflict** (calm air too; turbulence shifts more
   encounters into it).  Such a conflict is now `ground_only` (`node/conflict.py`) and capped at SEQUENCE: the
   aircraft are still spaced early (that spacing helps; dropping those conflicts entirely cost two NMACs in calm
   air), but there is no TRAFFIC alarm and no maneuver for it.
3. **One gust bump extrapolated for a minute**: a straight-line prediction of crossing traffic used the raw reported
   vertical speed.  Peers' turn rate and the vertical speed used for the *predicted path* now go through an adaptive
   (steady-state Kalman) smoother that measures each aircraft's own noise: calm air keeps the raw values, gusty air is
   filtered (`AdaptiveSmoother`, `node/node.py`).  Escape planning and negotiation keep the raw vertical speed.
   Tuning was by Monte Carlo: heavier smoothing removed more nuisance alerts but delayed a real climb in severe air
   and cost an NMAC, so the light setting (q 25 / 128) was kept.

Tests that changed: `accept_b` "NO_SOLUTION on boxed_in" used a scenario whose only conflict was the runway-roll
overlap (the aircraft never came within 956 ft in the air); it now uses the airborne base-to-final merge walled in by
terrain.  `test_sequencing_is_kept_after_a_maneuver_cleared_the_conflict` now uses encounters with a real airborne
maneuver.

### Is it strong enough?

Yes for light and moderate turbulence: no NMAC change, fewer alarms than calm air had before.  Severe turbulence is
still the hard case (12.5 % nuisance alerts on benign traffic, 10 NO_SOLUTION); ARC stays safe there (NMAC unchanged)
but noisy.  Limits: Dryden is statistical turbulence; wind shear, microbursts, wake turbulence and mountain wave are
not modelled, and real recorded tracks (OpenSky KDVT) are the next validation step.

## Run it

```
python harness/montecarlo.py --n 60 --turb 2                 # moderate; chart -> harness/out/arc_vs_baseline_turb2.png
python harness/dryden_world.py --scenario harness/scenarios/judges.json --weather hot_gusty_afternoon
```

The live launcher takes the same arguments as `world/world_server.py`; start the channel and nodes as usual
(`run_demo.ps1` steps 2-3), or change the god view's Turbulence selector while it runs.
