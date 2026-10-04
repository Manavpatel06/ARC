# FLOCK — verification checks, fusion and guardrails

Every check: `check(track, ctx, cfg) -> {score 0..1 | None, confidence, reason}`. 0 = evidence of spoofing,
1 = evidence it is real, **None = not applicable / not enough data — never held against a target**.
Every number is in `verify/config.json`.

| # | Check | Real | Spoof evidence | None when |
|---|---|---|---|---|
| 1 | `tcas_consistency` (strongest, weight 1.5) | median of the last 5 TCAS samples agrees with the claim (range ±300 m + 3 %, sideways ±max(300 m, 15°), altitude ±400 ft) → `tcas_confirmed` | they disagree (strong); "starting to disagree" at half the tolerance (weak → yellow first); no TCAS track where it claims to be, in range, while it is broadcasting | beyond 0.85 × TCAS range, own TCAS off, < 3 samples, band jammed, it stopped broadcasting |
| 2 | `modes_presence` | answers ground radar (DF4/5/20/21) or squits DF11 | silent for 20 s while the radar is active and it claims to be close (conf 0.7) | no radar heard, too far, band jammed, still listening |
| 3 | `timing_1030` | its radar replies arrive when the beam (phase from the beam hitting us; public site + 4.8 s period) points at its *claimed* bearing | replies from a bearing > 8° away from its claim | no radar fix, < 2 replies, too close to the radar |
| 4 | `rssi` (weight 0.6) | power fits free-space loss from the claimed position (±8 dB; trend follows claimed range) | > 8 dB off (a nearby transmitter claiming to be far) or anti-correlated trend | < 6 samples |
| 5 | `emitter_cluster` (Lane B's sybil) | — | same signal (±1.5 dB, ≥ 4 paired samples) as another target ≥ 1 km away, and its own signal misfits its claim | nothing odd |
| 6 | `kinematics` (weak when OK) | plausible speed / climb / turn / acceleration; positions follow its own velocity | teleports, > 600 kt, > 6000 fpm, > 8°/s, > 12 kt/s, residual > 250 m + 15 m/s | < 3 reports |
| 7 | `replay_detect` | — | one address in two places; a branch retracing positions its address reported earlier (replay); repeated old reports | no duplicates |
| – | `popin` (Lane B, weak) | — | first heard < 1.1 NM away, already airborne (not in the first 30 s after power-on) | came into range normally |

**Address split** (`tracks.py`): an established address whose new position is > 800 m from where it
should be opens a second branch (`<icao>~2`, shown as `N229 (2)`). TCAS and Mode S replies belong to the
transponder, i.e. the first branch; so a masquerader or replay gets its own track and its own verdict while
the real owner stays VERIFIED — the fix for AirWitness limitation 2 (stolen identity quarantining the owner).

## Fusion
`L = logit(prior 0.5) + Σ wᵢ · confᵢ · logit(clip(scoreᵢ))`, smoothed `L ← L + α(L_obs − L)` with α = 0.35, or
0.8 when the evidence jumps by ≥ 3 (fast on strong evidence, steady otherwise). Trust = 100 · σ(L).
VERIFIED ≥ 70, SUSPECT ≤ 30, else UNVERIFIED; reasons = the checks that pushed hardest toward the verdict.

## Guardrails (`guardrails.py`, unit-level in `unit.py`)
1. A target our own TCAS tracks where it claims to be is **never SUSPECT** (floor UNVERIFIED).
2. No check with confidence ≥ 0.5 → UNVERIFIED ("not enough evidence yet").
3. VERIFIED needs physical evidence: TCAS agreement, Mode S replies or radar timing — not just plausible motion.
4. SUSPECT needs at least one strong (≥ 0.7) negative check, not an accumulation of weak doubts.
5. **Band degraded** (noise floor > −90 dBm or message rate < 50 % of normal): banner "TRAFFIC PICTURE
   DEGRADED – increase lookout, notify ATC"; missing TCAS / Mode S replies prove nothing; tolerances ×2.
6. **Own position uncertain** (GPS integrity flag, or ≥ 2 and ≥ 60 % of TCAS-tracked aircraft disagree with
   their ADS-B): banner "OWN POSITION UNCERTAIN"; position tolerances ×4 and TCAS mismatches never decisive.

## Ported from Lane B (AirWitness-Hybrid)
Duplicate identity → `replay_detect` + address split; sybil → `emitter_cluster`; RF location / RSSI trend →
`rssi`; pop-in → `popin`; GA kinematic limits incl. acceleration → `kinematics`. Not ported (break
advisory-only / receive-only): signatures (ADS-B is unsigned), challenge / negotiation, witness digests,
multi-observer evidence; Doppler needs a coherent receiver we do not model.
