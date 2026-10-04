# ARC under a bad radio: robustness table

## Latest: `integration` 022f36d (Reya's R/R preference + commit at RESOLVE), radio ccc5c9c, Sat 3:45 PM

### head_on_judges (120 s, 4 runs per row)

| Radio condition | Runs with NMAC | Worst min separation | Median min separation | STATE loss measured |
|---|---|---|---|---|
| **No ARC** | **1 of 1** | **12 ft** | 12 ft | – |
| Perfect radio (0 % loss) | **0 of 4** | 805 ft | 829 ft | 0–3 % |
| Loss 10 % | **0 of 4** | 800 ft | 814 ft | 9–13 % |
| Loss 30 % | 2 of 4 | 161 ft | 547 ft | 27–36 % |

**Slide line that is true now:** "ARC turns a 12 ft head-on into 800+ ft of separation, with 0 NMACs in 8 of 8 runs from a perfect radio to 10 % packet loss."
**Not yet true:** "0 NMAC at 30 % loss." In both failures one aircraft committed a maneuver (N102 DESCEND), then at takeover switched to a different sense (LEFT 30) without re-committing, while the peer kept its compatible turn (RIGHT 20). With 30 % loss its picture of the peer is staler. Fix for Lane B: at takeover keep the committed sense, or re-commit and pick the side compatible with the peer's last commit (head-on: both RIGHT).

### Update 5:50 PM: Reya's `lane-b-node` 4a53fc9 ("agreed turn sense kept through takeover")

| Radio condition | Runs with NMAC | Min separation |
|---|---|---|
| Loss 30 % | **0 of 4** | 831–850 ft |

The 30 % failure above is fixed on Lane B's branch. Not yet in `integration`: merging it hits a conflict in `node/node.py` with Manav's right-of-way change (151c951), which Reya and Manav need to resolve. With it, the slide line becomes "0 NMAC in 12 of 12 head-on runs from a perfect radio to 30 % loss."

### three_on_final, retuned by Lane A (180 s, 2 runs per row)

| Radio condition | Runs with NMAC | Min separation |
|---|---|---|
| No ARC | 0 of 1 | 701 ft (N101-N204); N101-N399 2,262 ft |
| Loss 10 % | 0 of 2 | 688 ft |
| Loss 30 % | 0 of 2 | 688 ft |

The retune predicts N101-N399 at 179 ft offline (`find_conflict.py`), but the live world flies them 2,262 ft apart. Offline and live disagree, so this scenario still doesn't produce a conflict to avoid. Lane A to check.

---

## Earlier run (before Reya's fix), `integration` 91725c5, 41 runs

Measured with `harness/e2e_check.py` (truth from the world) on `integration` 91725c5 plus the latest `radio/`, one node per ARC aircraft over `radio/channel.py`. Run it again with `python radio/robustness.py` (use `--scenarios three_on_final --seconds 180`, because that conflict happens at t ≈ 125 s). Min separation = closest pair that came within 100 ft vertically. NMAC = < 500 ft horizontal and < 100 ft vertical.

## head_on_judges (two judge aircraft head-on on opposite downwinds, 120 s, 2–6 runs per row)

| Radio condition | Runs | Runs with NMAC | Worst min separation | Median min separation | STATE loss measured |
|---|---|---|---|---|---|
| **No ARC** (baseline) | 1 | **1** | **4 ft** | 4 ft | – |
| Perfect radio (0 % loss) | 4 | 1 | 264 ft | 848 ft | 0–7 % |
| Loss 10 % | 6 | 2 | 195 ft | 853 ft | 6–12 % |
| Loss 30 % | 6 | 1 | 227 ft | 852 ft | 25–42 % |
| Loss 50 % | 2 | 1 | 189 ft | 856 ft | 44–57 % |
| Latency spike 2 s for 60 s | 2 | 1 | 46 ft | 926 ft | 26–31 % (stale packets rejected) |
| Dropped MANEUVER_COMMIT | 2 | 0 | 813 ft | 848 ft | 10–11 % |

**What the numbers say.** ARC turns a 4 ft head-on collision into about 850 ft of separation in most runs, and that median holds from 0 % to 50 % loss. **The radio is not what decides the outcome.** About 1 run in 4 ends in an NMAC at *every* radio condition, **including a perfect radio**. The cause is in the takeover logic (Lane B). Both nodes choose their escape on their own at ttc ≈ 8 s and send MANEUVER_COMMIT only *after* acting (+33.4 s, after both takeovers at +32.9 s and +33.0 s). When the two independent choices disagree (N101 R30, N102 L20 head-on), they turn into each other. When they agree (both L), separation is about 850 ft. Fix (Lane B): commit at RESOLVE (ttc ≤ 20 s) with the lower id first, take over only in the agreed sense, and default both to RIGHT (14 CFR 91.113) when no commit was heard.

## three_on_final (180 s, 2 runs per row)

| Radio condition | Runs with NMAC | Min separation | STATE loss measured | RESOLVE advisories |
|---|---|---|---|---|
| **No ARC** (baseline) | 0 | 787 ft (N101-N204) | – | – |
| Perfect radio | 0 | 774 ft | 2–3 % | 11 |
| Loss 10 % | 0 | 773 ft | 12–16 % | 12–15 |
| Loss 30 % | 0 | 771 ft | 33–40 % | 10–15 |
| Loss 50 % | 0 | 772 ft | 52–53 % | 11–12 |
| Latency spike 2 s | 0 | 771 ft | 22–24 % | 11–16 |
| Dropped MANEUVER_COMMIT | 0 | 773 ft | 10–13 % | 9–14 |

**Not a valid test yet.** Even without ARC this scenario no longer conflicts: N101-N399 is 2,399 ft apart, against the 38 ft NMAC it was tuned for. Pinning the weather to `cached` did not restore it (these runs use the cached sample: DA 4,083 ft, wind 250/8), so world changes since tuning detuned it. It needs `world/find_conflict.py` again. ARC still produces 9–16 RESOLVE advisories and keeps every pair above 770 ft at every loss level.

## Radio-side facts behind the table
- Measured STATE loss tracks the configured loss (10 % → 6–16 %, 30 % → 25–42 %, 50 % → 44–57 %). Small samples, plus slot collisions at start-up.
- **2 s latency costs packets on purpose:** receivers reject anything older than ±2 s as a possible replay (115 `time_window` rejections in one run). That is the security/latency trade-off of replay protection.
- One dropped MANEUVER_COMMIT changed nothing: nodes re-send a commit about every 2 s while the conflict lasts (seen in the run logs).

## Slide wording (honest)
- OK to say: "ARC turns a 4 ft head-on into ~850 ft median separation, and that median holds up to 50 % packet loss."
- Not yet OK to say: "still 0 NMAC at 30 % loss." 1 of 6 runs at 30 % had an NMAC, and so did 1 of 4 with a perfect radio. Fix the takeover/commit ordering first, then re-run `python radio/robustness.py`.
