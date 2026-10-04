# ARC — pitch notes

**One-liner:** ARC verifies that every aircraft on your traffic display actually exists, by checking that
its signals agree with each other, using equipment the plane already has.

## What already exists
- **Ground-based validation**: ATC multilateration / radar cross-checks of ADS-B — on the ground, not in the cockpit.
- **TCAS hybrid surveillance validation**: TCAS checks ADS-B against active interrogation for the targets it
  tracks — one source, inside TCAS range.
- **Academic ML detectors**: mostly offline, ground-station data, black boxes.
- **Cryptographic ADS-B proposals**: need a protocol change on every aircraft — decades away.

## What ARC adds
- **Onboard and continuous**: verdicts in the cockpit, every second, for every target.
- **Multi-signal fusion with explanations**: TCAS range/bearing, Mode S presence, 1030/1090 timing, RSSI,
  one-transmitter clusters, kinematics, replay/duplicate detection → one trust score and the top reasons in plain English.
- **No avionics changes**: an EFB app reading data read-only + an optional portable SDR receiver.
- **Safe by construction**: advisory only; never suppresses a TCAS-confirmed target; "unverified" is not
  "spoofed"; banners when the whole picture is degraded (jamming) or our own position is doubtful (GPS spoofing).
- **Accountable**: signed, hash-chained detection log; signed link to the display; "Report to ATC" pre-filled.
- **Future**: designed to feed ACAS X's surveillance and tracking stage; complements GPS-spoofing protection.

## Numbers (simulation, `eval/out/report.md`)
4 seeds × 13 cases, live KDVT traffic: **every attack aircraft detected, 0 real aircraft ever SUSPECT, real
traffic VERIFIED 98–100 % of the time, 0 false banners**, ROC AUC 0.986.
Time to SUSPECT (median): ghost 5 s, flock 3 s, ICAO masquerade 1 s, replay ~2 s after its first message,
slow drift 73 s (yellow first). No TCAS (beyond range or switched off): lone ghost 21 s (ground-radar
silence), flock 3 s (one transmitter). Jamming banner 1 s, own-GPS banner 27 s, nobody condemned.
Ablation: TCAS alone catches 87 %; all checks together 100 % — and dropping Mode S presence or radar
timing lets false alarms in. That is why fusion beats any single check.
