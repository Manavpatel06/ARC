"""
stubs/fake_node.py — stand-in for Lane B's node so Lane A (views) and Lane D (log page)
can build against real ADVISORY / TRUST / COMMAND traffic NOW.

Connects to the world as node:<id>, reads OWNSHIP, and replays a scripted escalation
ladder on a timer (no real conflict logic):
  t+5   SEQUENCE  "NUMBER 2 - EXTEND DOWNWIND 15 S"      ttc 84
  t+20  TRAFFIC   "TRAFFIC - 2 O'CLOCK - 1 MILE - SAME ALT" ttc 33
  t+32  RESOLVE   "TURN LEFT 30 - N204 TURNING RIGHT"      ttc 19
  t+42  TAKEOVER  COMMAND bank -28 for 8 s + ADVISORY "ARC HAS THE AIRCRAFT"
  t+50  RELEASE   "YOUR AIRCRAFT - CONTINUE LEFT TURN"
  t+58  CLEAR     "CLEAR OF CONFLICT"
  then loops. TRUST frame every 1 s with N204 TRUSTED and GHOST7 FAKE, each with a v1.1 `rel` radar position.
Also broadcasts a PREDICTION frame (60 s straight-line path) at 1 Hz for the god view.

Run:  python stubs/fake_node.py --id N101 --world ws://localhost:8765 [--speed 4]
"""
from __future__ import annotations
import argparse, asyncio, json, math, time
import websockets

LADDER = [
    (5,  "SEQUENCE", 1, "NUMBER 2 - EXTEND DOWNWIND 15 S", "number two, extend downwind fifteen seconds", 84),
    (20, "TRAFFIC",  2, "TRAFFIC - 2 O'CLOCK - 1 MILE - SAME ALTITUDE", "traffic, two o'clock, one mile, same altitude", 33),
    (32, "RESOLVE",  3, "TURN LEFT 30 - N204 TURNING RIGHT", "turn left three zero, november two zero four turning right", 19),
    (42, "TAKEOVER", 4, "ARC HAS THE AIRCRAFT - LEFT 28", "arc has the aircraft", 7.6),
    (50, "RELEASE",  4, "YOUR AIRCRAFT - CONTINUE LEFT TURN", "your aircraft, continue left turn", None),
    (58, "CLEAR",    0, "CLEAR OF CONFLICT", "clear of conflict", None),
]
PERIOD = 70

def predict(own: dict, horizon_s=60, step_s=5):
    pts = []
    v = own["gs_kt"] * 0.514444
    for k in range(0, horizon_s + 1, step_s):
        d = v * k
        dlat = d * math.cos(math.radians(own["track_deg"])) / 111_320
        dlon = d * math.sin(math.radians(own["track_deg"])) / (111_320 * math.cos(math.radians(own["lat"])))
        pts.append({"t": k, "lat": own["lat"] + dlat, "lon": own["lon"] + dlon, "alt_msl_ft": own["alt_msl_ft"], "sigma_m": 30 + 4 * k})
    return {"type": "PREDICTION", "ac_id": own["ac_id"], "t": own["t"], "method": "straight-line(stub)", "confidence": 0.5, "path": pts}

async def run(ac_id: str, world: str, speed: float = 1.0):
    url = f"{world}?role=node:{ac_id}"
    async with websockets.connect(url) as ws:
        print(f"[fake-node {ac_id}] connected {url}")
        own = None
        t0 = time.time()
        fired = set()
        last_trust = last_pred = 0.0
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.2)
                m = json.loads(raw)
                if m.get("type") == "OWNSHIP":
                    own = m
                elif m.get("type") == "STICK":
                    print(f"[fake-node {ac_id}] STICK -> RELEASE")
                    await ws.send(json.dumps({"type": "COMMAND", "ac_id": ac_id, "t": time.time(), "mode": "RELEASE", "reason": {"cause": "stick"}}))
            except asyncio.TimeoutError:
                pass
            if own is None:
                continue
            now = time.time()
            el = ((now - t0) * speed) % PERIOD
            cycle = int(((now - t0) * speed) // PERIOD)
            for (at, level, layer, text, speak, ttc) in LADDER:
                key = (cycle, level)
                if el >= at and key not in fired:
                    fired.add(key)
                    adv = {"type": "ADVISORY", "ac_id": ac_id, "t": now, "layer": layer, "level": level,
                           "text": text, "speak": speak, "target_id": "N204", "ttc_s": ttc,
                           "reason": {"predicted_miss_ft": 310, "method": "stub-ladder", "confidence": 0.82}}
                    await ws.send(json.dumps(adv))
                    print(f"[fake-node {ac_id}] {level}: {text}")
                    if level == "TAKEOVER":
                        cmd = {"type": "COMMAND", "ac_id": ac_id, "t": now, "mode": "TAKEOVER",
                               "bank_cmd_deg": -28, "vs_cmd_fpm": 0, "hold_s": 8,
                               "bounds": {"max_bank_deg": 30, "min_ias_kt": 62, "min_agl_ft": 300, "max_hold_s": 10},
                               "reason": {"chosen": "L30", "rejected": {"R30": "traffic", "CLIMB": "performance 350fpm", "DESCEND": "terrain floor"}, "ttc_s": 7.6}}
                        await ws.send(json.dumps(cmd))
                    if level == "RELEASE":
                        await ws.send(json.dumps({"type": "COMMAND", "ac_id": ac_id, "t": now, "mode": "RELEASE", "reason": {"cause": "conflict clear"}}))
            if now - last_trust > 1:
                last_trust = now
                # v1.1 rel: scripted relative positions (a real node computes these from peer STATE)
                trk = own["track_deg"]
                closing = max(200.0, 2600.0 - 40.0 * el)            # N204 closes from ~1.4 NM as the ladder runs
                await ws.send(json.dumps({"type": "TRUST", "ac_id": ac_id, "t": now, "targets": [
                    {"id": "N204", "score": 0.93, "state": "TRUSTED", "evidence": ["plausible", "corroborated:2", "signed"],
                     "rel": {"brg_deg": round((trk + 45) % 360, 1), "rng_m": round(closing), "dalt_ft": -30, "trk_deg": 41, "vs_fpm": 0}},
                    {"id": "GHOST7", "score": 0.12, "state": "FAKE", "evidence": ["no_corroboration", "kinematics_violation", "unsigned"],
                     "rel": {"brg_deg": round((trk - 100) % 360, 1), "rng_m": 2100, "dalt_ft": 0, "trk_deg": 266, "vs_fpm": -500}}]}))
            if now - last_pred > 1:
                last_pred = now
                await ws.send(json.dumps(predict(own)))

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", default="N101")
    ap.add_argument("--world", default="ws://localhost:8765")
    ap.add_argument("--speed", type=float, default=1.0, help="ladder time scale (4 = full cycle in ~17 s)")
    a = ap.parse_args()
    asyncio.run(run(a.id, a.world, a.speed))
