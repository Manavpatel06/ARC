# Blind judging round 1 (Fri night) — what to fix before Sunday

Two simulated Honeywell judges scored the pitch + demo vision blind against the rubric: **52 / 65 from both**, first among PS 2 teams by ~16 points. Points lost on *Can be implemented* (3), *Technically sound* (3), *Scalable* (3).

## Fixes, in priority order (each maps to a lane)
1. **Lead with what ships** (Lane D, pitch). Predictor + sequencing + trust engine run on existing ADS-B In hardware; takeover is phase three, autopilot-equipped aircraft only. Never headline "takes the airplane".
2. **Validate out of sample** (Lane B). The 71 s vs 24 s chart was measured on our own pattern generator. Replay recorded KDVT ADS-B tracks (OpenSky) and report lead time *and* false alerts per flight hour. Say the nuisance number out loud.
3. **Printed takeover bounds on screen** (Lane B `authority.py`, Lane A view). Max bank 30°, speed floor 1.3·Vs, altitude floor, max 10 s authority; no admissible maneuver → NO_SOLUTION warn-only. Demo the no-solution case (`boxed_in.json`) and the handback.
4. **Name the predictor's failure mode** (Lane B `predict.py`). Straight-ins, 45° entries, go-arounds → confidence per leg; < 0.6 falls back to straight-line; show confidence; demo one misclassification.
5. **Lost link after commit; three aircraft** (Lane B `negotiate.py`, Lane C `faults.py`). Commits time out consistently; both default right; show it in the log; demo three-on-final.
6. **Tight trust wording** (Lane C). State exactly what is measured (RSSI bound, Doppler, peer corroboration, camera) and what it does *not* catch (one spoofer with three radios). Camera sightings drawn as bearing wedges, not positioned dots.
7. **Tablet ≠ negotiating member.** ADS-B In is receive-only; a tablet is advisory.
8. **Wording.** "RAs inhibited below 1,000 ft; TAs continue to 500 ft" (not "goes silent"). Cut "what Xp should have been". Cite the NTSB preliminary for Marana.
9. **Something must actually transmit** (Lane C, after 7 PM go/no-go). One real link on the table (ESP32-C6 node exchanging STATE with measured loss) + one-page BOM + band/link budget.

Judge round 2 (blind, fresh judges) runs once Phase 2 is green, ~midnight Saturday.
