# ARC — Status report, Sat 1:00 PM

**One line:** ARC is a peer-to-peer collision-avoidance node for general aviation that predicts where traffic is *turning* in the airport pattern, negotiates complementary maneuvers aircraft-to-aircraft, and only touches the controls inside printed safety bounds.

## Working right now (demo on request)
- **Simulation world** (Manas): authoritative world server, 3-DOF flight model with density-altitude climb limits and live wind, 8 aircraft flying the real Deer Valley (KDVT) traffic pattern on both parallel runways, cockpit view with PlayStation controller + voice, god view of the whole pattern.
- **Shared contract v1.1 + real airport data** (Manav): all message types frozen in code; surveyed KDVT runway geometry (true headings 086°/266°, 25L left traffic south, 25R right traffic north); shared pattern geometry so world and avoidance logic agree; weather → density altitude → climb capability.
- **Comms log + explain panel** (Manav): every radio packet and every decision live, colour-coded; click any decision to see predicted miss, time to conflict, method, confidence, maneuvers chosen and rejected with reasons, the printed authority bounds, and the negotiation transcript. Spoofed packets show in red with the rejection reason.
- **Node logic** (Reya): <fill in — e.g. node connects, reads own state, classifies leg, broadcasts STATE, raises TRAFFIC on straight-line CPA>
- **Radio** (Mansi): <fill in — e.g. channel relays STATE with 3-mile range cutoff and loss; schema validation>

## Next gate — 4:00 PM: the loop closes
Turn-aware prediction raises a base-to-final conflict ≥ 60 s before straight-line logic; four layers escalate (sequence → warn → negotiate → bounded takeover); two nodes commit complementary turns within 300 ms; signed messages with replay protection.

## Risks we are managing
- Event Wi-Fi isolates clients → we run on a phone hotspot (tested <time>).
- Scope: camera/computer vision deliberately cut to keep the core and the radio-trust story solid.
