# ARC — limitations (said out loud)

1. **Simulated aircraft interfaces.** TCAS, Mode S replies, 1030 interrogations, own-ship GPS integrity and
   RSSI come from `world/sensors.py`, not from a real aircraft. Their noise and range numbers (TCAS range σ 15 m,
   bearing σ 5°, 12 NM; radar 4.8 s; free-space RSSI ±2 dB) are reasonable estimates, **not** taken from TCAS
   MOPS or radar specifications. A real installation reads TCAS / GPS integrity through an Aircraft Interface
   Device; that path is not built.
2. **Passive checks can be beaten.** An attacker with a real transponder near the claimed position, or one who
   controls signal strength and timing per receiver, defeats TCAS / Mode S / RSSI checks. ARC raises the cost
   of spoofing; it does not make it impossible.
3. **Single-aircraft geometry.** One receiver cannot locate a transmitter; RSSI ranging is coarse (3 dB ≈ ×1.4
   distance, transponder power varies ±3 dB). Multi-receiver checks (Lane B's multi-observer / witnesses) need a
   data link, which breaks "receive only" — roadmap.
4. **No TCAS coverage → slower.** Beyond TCAS range or with TCAS off, a lone ghost needs ~20 s of radar silence
   before it is SUSPECT; with no ground radar either it stays UNVERIFIED (warned, honest) — see eval hard cases.
5. **Slow drift is caught late by design.** "Frog-boiling" at 12 m/s is flagged once the offset exceeds what
   TCAS noise can explain (~70 s, ~850 m); it shows yellow first.
6. **Address split heuristic.** The first established branch is assumed to be the real transponder; an attacker
   who starts transmitting an address *before* the real aircraft comes into range takes the first branch (TCAS
   and Mode S evidence still go against it, but its name is the plain callsign).
7. **Public feeds lack raw signal data** (no RSSI, no frames): recorded real traffic is replayed through the
   simulated physical layer.
8. **Security of ARC itself is a demo version.** The unit's Ed25519 key is generated on first use and pinned
   by the world on first contact (trust on first use); the display end is not authenticated. Production needs
   keys provisioned at installation, an authenticated display link and protected key storage.
9. **Not certified, not certifiable as-is.** An EFB app reading avionics data read-only is the deployment story;
   real use needs DO-178C / DO-200B-style assurance and an operational approval.
10. **Evaluation numbers describe this simulation** (`eval/out/report.md`): attacks and traffic come from the
    same models the checks were tuned on. Field data (recorded ADS-B with a real SDR) is the next step.
