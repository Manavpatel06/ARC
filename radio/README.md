# Lane C — Radio, protocol, security (Mansi)

Emulated peer-to-peer radio for ARC: signed, schema-validated messages; a channel that drops and delays like a
3-mile link; AIS-style self-organizing slots; a spoofer; and trust evidence so nodes tell real aircraft from fake.
Brief: `lanes/C-radio-mansi.md`. Acceptance: `python radio/accept_c.py` → **14/14** (1 PM, 4 PM, 7 PM rows).

| File | What it does |
|---|---|
| `client.py` | `RadioClient` — same API as `stubs/loopback_radio.py`. Signs (Ed25519), numbers `seq`, timestamps, validates every message; rejects bad signature / old seq / ±2 s window / schema with a logged reason; HEARTBEAT 0.5 Hz; link LOST after 3 s silence. |
| `channel.py` | The air. Reads TRUTH (role `channel`); range 4,828 m, loss 10 %, latency 0.3 ± 0.1 s, slot collisions, faults; attaches emulated RSSI/Doppler; mirrors every delivered and dropped packet to the world log with a reason. Pins public keys (registrar). |
| `crypto.py` | Ed25519 sign/verify, key ring, replay guard; per-aircraft key kept in `~/.arc/keys/`. |
| `slots.py` | AIS-style self-organizing TDMA (1 s frame, 20 slots, slot 19 = burst). `python radio/slots.py` prints the collision-rate table. |
| `evidence.py` | Trust evidence per target: signature, kinematic plausibility, RF consistency, peer corroboration → score 0–1 + evidence strings for `node/trust.py`. |
| `spoofer.py` | Rogue radio: GHOST7 on final — unsigned / unknown key / impossible kinematics / impersonation / replay; `--sybil 3`. |
| `faults.py` | Drop a MANEUVER_COMMIT, silence a node for 5 s, latency spike to 2 s. |
| `rf.py`, `wire.py` | Shared RF model (915 MHz, 20 dBm, free space) and transport plumbing. |
| `accept_c.py` | Lane C acceptance checks. |
| `watch.py` | Live colour-coded terminal view of all communication (packets, drops, rejections, links, trust, advisories). |

## Run
```bash
python radio/channel.py --world ws://<WORLD_IP>:8765 --loss 0.1 --latency 0.3     # one per demo, any laptop
python node/node.py --id N101 --world ws://<WORLD_IP>:8765                          # nodes pick up radio/client.py automatically
python radio/spoofer.py                       # GHOST7 unsigned on final  (--mode impossible | unknown-key | impersonate --as N204 | replay; --sybil 3)
python radio/faults.py drop-commit --from N101    # or: kill-link N204 --for 5   |   latency 2.0 --for 10
python radio/accept_c.py                      # all Lane C checks locally (~70 s)
python radio/slots.py                         # collision-rate table for the slide
```

## Watch all communication live (terminal)
```bash
python radio/watch.py --world ws://<WORLD_IP>:8765               # every packet, drop, rejection, link, trust change, advisory
python radio/watch.py --world ws://<WORLD_IP>:8765 --no-state    # hide routine STATE/HEARTBEAT — negotiation, trust, advisories stand out
python radio/watch.py --world ws://<WORLD_IP>:8765 --only GHOST7 # one aircraft
python radio/watch.py --world ws://<WORLD_IP>:8765 --drops       # only drops, rejections, faults
```
Radio lines appear only while `radio/channel.py` runs against that world. Reconnects by itself; summary line every 10 s.

## Transports (board task C3: test on the hotspot)
- **Default — UDP multicast** through the channel: uplink `239.1.1.1:5005`, downlink `:5006`. Needs multicast to pass the hotspot.
- **Multicast blocked → WebSocket relay**: `RadioClient(ac_id, via_channel="ws://<CHANNEL_IP>:8766")` — note **8766** (the channel), not 8765 (the world). Node CLI: `--via-channel ws://<CHANNEL_IP>:8766`.
- **No channel at all**: `RadioClient(ac_id, direct=True)` (like the loopback stub, no loss, no RF).
- Windows laptop with several network cards: add `--iface <its hotspot IP>` to channel/spoofer/client.
- Windows firewall (PowerShell as admin): `New-NetFirewallRule -DisplayName "ARC UDP" -Direction Inbound -Protocol UDP -LocalPort 5005,5006 -Action Allow` and `... -Protocol TCP -LocalPort 8765,8766,8000 ...`.

## For Lane B (Reya): turn trust evidence on — 6 lines in `node/node.py` `run()`
Without this every peer, including GHOST7, stays TRUSTED. Tested against `lane-b-node` @297100f: GHOST7 → FAKE 0.0 on every node, real peers TRUSTED 1.0.
```python
    radio.on_message(node.on_radio)
    try:                                                   # Lane C trust evidence (radio/evidence.py)
        from radio.evidence import TrustEvidence
        ev = TrustEvidence.attach(radio)
        radio.on_message(lambda e: node.trust.update(e["from"], *ev.assess(e["from"])))
    except Exception:
        ev = None
    ...
                if m.get("type") == "OWNSHIP":
                    latest["own"] = m
                    if ev:
                        ev.on_ownship(m)
```
Delivered envelopes also carry `_auth` ("ok" | "unsigned" | "unknown_key"), `_rf` and `_rx_t`; extra fields, ignored by `schemas.RadioMsg`.

## Numbers for the slide (`python radio/slots.py`, all senders in mutual range, 1 Hz)
| senders | slots | unslotted | SOTDMA |
|---|---|---|---|
| 8 | 20 | 35 % | 6 % |
| 30 | 20 | 83 % | 62 % |
| 8 | 40 | 19 % | 3 % |
| 30 | 40 | 58 % | 23 % |
Honest capacity: 19 STATE slots at 1 Hz = 19 aircraft in mutual range. 30 aircraft need 40 slots (25 ms) or a lower STATE rate for far aircraft.

## Honest words (pitch)
- Keys are made per device and registered at boot — a hackathon stand-in for registration-bound certificates.
- A signature proves who sent a message, not that it is true; that is what RF consistency and peer corroboration are for.
- RSSI/Doppler are emulated from true geometry plus noise; real Doppler needs a TCXO and frequency-offset calibration.
- Sybil with several valid keys and well-placed transmitters is a known limit — expensive, not impossible.
- Hobby-band emulation (915 MHz) is not an aviation communications approval.

## Proposed contract addition (needs a "yes" in team chat; no schema change made)
HEARTBEAT body carries an optional `"nb": {"N204": 1, "GHOST7": 0}` (≤ 12 entries) — what this node heard in the
last 3 s and whether its RF/kinematics agreed. `HeartbeatBody` ignores unknown fields, so nothing breaks today.
