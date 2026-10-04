# ARC basic FAA check (Lane C, Sat Oct 3 2026)

This is a first-pass check of ARC as built (`main` f1ae914) against the FAA and FCC rules that apply to a GA collision-avoidance add-on at KDVT. It is a team checklist, not a legal opinion. Each row cites the code that supplies the evidence.

✅ meets it today · ⚠️ gap we can close this weekend · ⏳ real-product step, outside hackathon scope

## Operating rules (14 CFR Part 91)

| Rule | What it asks | ARC today | |
|---|---|---|---|
| **91.3** PIC final authority | The pilot is always in command | Takeover releases on any stick input (`node/authority.py`). After a stick release, automatic action is inhibited for 5 s (`STICK_INHIBIT_S`, `node/node.py`). Takeover only happens on autopilot-equipped aircraft. | ✅ |
| 91.3 (continued) | The pilot can turn the automation off | No master **TAKEOVER ARM / OFF** switch. The pilot can only override each event as it happens. | ⚠️ add an arm switch to the cockpit page, default ARMED, logged |
| **91.111** operating near other aircraft | Don't create a collision hazard | Each escape is scored against every known aircraft (smallest margin over all peers), plus terrain and obstacle floors (`node/escape.py`). | ✅ |
| **91.113(d)–(g)** right-of-way | Head-on: both turn right; converging: the aircraft on the right has the right-of-way; overtaking; landing | Implemented in `node/rightofway.py`, and every RESOLVE reason cites the rule | ✅ |
| **91.225 / 91.227** ADS-B Out | Required around PHX Class B / Mode C veil (KDVT); the system must not degrade it | ARC uses its own 915 MHz link and never transmits on 1090/978. Live ADS-B is display-only and never sent to nodes (`web/livesky.js`). ARC is in addition to ADS-B Out, never a replacement. | ✅ |
| 91.21 portable electronic devices | The operator has to judge that the device doesn't interfere with nav/comm | No EMI test yet | ⏳ |

## Equipment approval (certified Part 23 aircraft)

| Path | What it asks | ARC today | |
|---|---|---|---|
| **NORSEE** (PS-AIR-21.8-1602), alerts and radio only | Failure is no worse than *minor*; no operational credit; independent of primary systems; "supplemental only" notice and placard | Alerts and radio are advisory and separate from aircraft systems. The robustness table shows radio loss degrades toward the no-ARC baseline, not below it. **The supplemental notice is missing.** | ⚠️ add "SUPPLEMENTAL ONLY — not a primary traffic system" to the cockpit page and README |
| NORSEE, automatic takeover | — | Commanding bank and vertical speed is worse than a *minor* failure, so **the takeover cannot use NORSEE** | ⏳ STC path |
| **STC** for takeover | System safety assessment (AC 23.1309-1E); DO-178C software and DO-254 hardware assurance; DO-160 environmental testing | The design already follows the right pattern: a small independent bounds monitor is the only path to the controls (`authority.py` imports nothing from the smart logic). Bounds: 30° bank, 62 kt floor, no action below 300 ft AGL on final, 10 s max hold. No formal assurance yet. | ⏳ |
| Experimental aircraft | No equipment approval needed | This is the realistic flight-test path | ✅ |
| Closest TSOs | TSO-C199 (traffic awareness beacon), TSO-C195 (ADS-B In applications) | For reference when picking the standard | ⏳ |

## Security / anti-spoofing

| Item | Requirement / guidance | ARC today | |
|---|---|---|---|
| ADS-B authenticity | 91.227 has none; this is the known gap | Every message is Ed25519-signed with a replay window and seq checks; RF, kinematic and peer evidence; GHOST7 → FAKE, with 0 RA/takeover against it (`radio/`) | ✅ |
| Cybersecurity rule | 2024 NPRM covers Parts 25/33/35 only (not final); Part 23 uses existing rules. The industry process is DO-326A / DO-356A. | No formal security risk assessment document | ⏳ write a short DO-326A-style threat list from `radio/README.md` |
| GNSS spoofing | FAA GNSS Interference Resource Guide (Mar 2026): cross-check, then report to ATC | Ownship GNSS consistency check exists in Reya's AirWitness (`lane-b-node` c5aa5d7, not in `main` yet) | ⚠️ pending merge |
| Privacy (PAPA, in ALERT Act) | ADS-B data used only for safety | ARC broadcasts tail ID and position unencrypted (signed, not hidden) | ⏳ note on the slide; a rotating ID is possible later |

## Spectrum (FCC)

| Item | Requirement | ARC today | |
|---|---|---|---|
| 902–928 MHz, 47 CFR 15.247 | FCC equipment authorization for the final device; Part 15 must accept interference | Pre-certified E22 module in the BOM (`radio/LINK_BUDGET.md`); graceful degradation measured (`radio/ROBUSTNESS.md`) | ⏳ |
| Spoofer | Willful interference is prohibited (47 U.S.C. 333) | `radio/spoofer.py` runs only inside the emulated channel, never on air | ✅ |

## Three fixes for this weekend
1. **Supplemental-only label** on the cockpit page and in the READMEs (Lane A / D, 5 min).
2. **TAKEOVER ARM / OFF switch** in the cockpit, sent to the node, logged (Lane A + B).
3. **Merge AirWitness** once its open issues are fixed (real peers stuck UNVERIFIED; SEQUENCE advisories against GHOST7).

## Slide line
"ARC's alerts and radio fit the FAA's NORSEE path for certified GA today, and any experimental aircraft can fly it now. The automatic takeover is built the way certification expects: a small independent monitor with printed bounds, pilot override on stick, right-of-way per 91.113. Taking it to an STC is the next step."
