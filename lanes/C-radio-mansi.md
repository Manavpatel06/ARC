# Lane C — Radio channel, protocol, security, trust evidence (Mansi)

Paste CONTEXT.md and INTERFACE.md first, then this file. You are the agent for Lane C.

## Goal
Build the emulated peer-to-peer radio and everything that makes it trustworthy: schema-validated signed messages, a channel that drops and delays like a real 3-mile link, self-organizing time slots so 30 aircraft do not collide on the air, a spoofer, and the evidence that lets nodes tell real aircraft from fake ones (from the Onboard RF Consistency Monitor idea).

## Deliverables (in `/radio`)
1. `radio/client.py` — library used by nodes: `send(msg_type, body)` wraps the envelope, signs, numbers `seq`, timestamps; `on_message(callback)` verifies signature, seq monotonicity, time window ±2 s, schema, then delivers. Transport: UDP multicast `239.1.1.1:5005` by default; `--via-channel` mode sends to the channel process instead (needed when Wi-Fi blocks multicast; test both on the hotspot early).
2. `radio/channel.py` — the air. Connects to the world as role `channel` to learn true positions (nodes never see this). For each packet: drop if sender–receiver range > 4,828 m; drop with probability `loss` (default 0.10; CLI flag); delay `latency` 0.3 ± 0.1 s; emulate slot collisions when >1 sender in the same 1-s slot (configurable aircraft count); mirror every delivered and dropped packet to the world `log` role with `delivered:true|false, reason`.
3. `radio/crypto.py` — Ed25519 (PyNaCl). Keypair per node at startup; public keys exchanged at boot via a `KEYS` message to the world (hackathon simplification, say so in the pitch). Replay protection: per-sender last `seq` and time window.
4. `radio/slots.py` — AIS-style self-organizing TDMA: 1-s frame, 20 slots; node picks `hash(id) % 20`, listens one frame, moves to a free slot on collision; event-driven INTENT/COMMIT may use a reserved burst slot. Metric: collision rate at 8 / 30 aircraft with and without slots → one number for the slide.
5. `radio/evidence.py` — **trust evidence** per target, consumed by Lane B's `trust.py`:
   - kinematic plausibility: speed ≤ 200 kt, acceleration ≤ 0.5 g, turn rate ≤ 10°/s, altitude rate ≤ 2,000 fpm;
   - RF consistency (emulated): the channel attaches a noisy RSSI and Doppler to each delivered packet derived from true geometry; evidence compares them with the *claimed* position/velocity; disagreement lowers trust;
   - peer corroboration: count of distinct peers (by bearing diversity) that also hear this target; ≥2 raises trust;
   - signature validity (unsigned = immediate SUSPICIOUS);
   - camera corroboration flag from SIGHTING bodies.
   Output a score 0–1 and an evidence list of strings.
6. `radio/spoofer.py` — rogue node: broadcasts `GHOST7` on final (unsigned, or signed with an unknown key, or kinematically impossible); CLI flags for each attack; `--sybil 3` broadcasts three mutually consistent fakes to show the known limit.
7. `radio/faults.py` — scripted faults for the demo and for RED FLOCK later: drop a specific MANEUVER_COMMIT, kill a node's heartbeat for 5 s, spike latency to 2 s.

## Acceptance tests
- 1:00 PM: two nodes exchange STATE through `channel.py` with range cutoff; a third node 4 miles away hears nothing; every packet appears in the log.
- 4:00 PM: signature failure, old seq, and out-of-window time are each rejected with a logged reason; HEARTBEAT loss flagged within 3 s.
- 7:00 PM: spoofer running: GHOST7 shows as SUSPICIOUS on one node and FAKE once two peers fail to corroborate; slot collision rate before/after measured at 30 simulated senders; `faults.py` can drop a commit and the nodes' fallback is visible in the log.

## Constraints
- Nodes must never read channel truth. The channel is the only process that knows true positions, and only to decide delivery and to attach emulated RSSI/Doppler.
- Keep message bodies small (< 200 bytes) to stay honest about a low-bandwidth link.
- All drops and rejections are logged with a reason; the log laptop is a judging exhibit.

## Optional, only after acceptance and the 7 PM go/no-go
ESP32-C6 node port: C++ (Arduino/ESP-IDF) implementation of `client.py` envelope + a minimal node for one aircraft over Wi-Fi UDP, marked "over the air" in the log. Proves the node runs on target-class hardware with real loss; do not start before Phase 1 and 2 are green.
