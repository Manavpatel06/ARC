"""
stubs/fake_channel.py — stand-in for Lane C's radio/channel.py so the log page (Lane D) and god
view (Lane A) show realistic radio traffic before the real channel exists.

Connects to the world as role `channel`, reads TRUTH, and posts one LOG-worthy frame per
radio event back to the world (the world mirrors channel frames to the `log` role as kind=radio):
  * STATE 1 Hz per ARC aircraft, delivered to each peer within 4,828 m, dropped otherwise
    (reason "range"), plus random loss (reason "loss") — one frame per (sender, receiver) pair
  * HEARTBEAT every 2 s per aircraft (one frame, broadcast)
  * every ~25 s a scripted negotiation between the two closest aircraft:
    INTENT -> MANEUVER_COMMIT (L) -> MANEUVER_COMMIT (R)
  * --spoof: GHOST7 broadcasts an unsigned STATE on final every 2 s (reason "bad_signature")
Frame posted (payload of the LOG): radio envelope + {"to","delivered","reason","rng_m","latency_s"}

Run:  python stubs/fake_channel.py --world ws://localhost:8765 [--loss 0.1] [--spoof]
"""
from __future__ import annotations
import argparse, asyncio, json, math, random, time
import websockets

RANGE_M = 4828.0

def rng_m(a, b):
    dn = (b["lat"] - a["lat"]) * 111_320
    de = (b["lon"] - a["lon"]) * 111_320 * math.cos(math.radians(a["lat"]))
    return math.hypot(de, dn)

async def run(world: str, loss: float, spoof: bool):
    seq: dict[str, int] = {}
    def env(msg, frm, body):
        seq[frm] = seq.get(frm, 0) + 1
        return {"msg": msg, "from": frm, "seq": seq[frm], "t": time.time(), "sig": "stub", "body": body}
    async with websockets.connect(f"{world}?role=channel") as ws:
        print("[fake-channel] connected")
        last_state = last_hb = last_neg = last_spoof = 0.0
        truth = None
        async for raw in ws:
            m = json.loads(raw)
            if m.get("type") != "TRUTH":
                continue
            truth = [a for a in m["aircraft"]]
            now = time.time()
            out = []
            if now - last_state >= 1.0:
                last_state = now
                for a in truth:
                    e = env("STATE", a["ac_id"], {"lat": a["lat"], "lon": a["lon"], "alt_press_ft": a.get("alt_press_ft", a["alt_msl_ft"]),
                                                  "gs_kt": a["gs_kt"], "track_deg": a["track_deg"], "vs_fpm": a["vs_fpm"],
                                                  "leg": a.get("leg", "UNKNOWN"), "intent": "", "ap_equipped": a.get("ap_equipped", False)})
                    for b in truth:
                        if b is a:
                            continue
                        r = rng_m(a, b)
                        if r > RANGE_M:
                            ok, why = False, "range"
                        elif random.random() < loss:
                            ok, why = False, "loss"
                        else:
                            ok, why = True, None
                        out.append(dict(e, to=b["ac_id"], delivered=ok, reason=why, rng_m=round(r), latency_s=round(random.uniform(0.2, 0.4), 2)))
            if now - last_hb >= 2.0:
                last_hb = now
                for a in truth:
                    out.append(dict(env("HEARTBEAT", a["ac_id"], {"alive": True}), to="*", delivered=True, reason=None))
            if now - last_neg >= 25 and len(truth) >= 2:
                last_neg = now
                pairs = sorted(((rng_m(a, b), a["ac_id"], b["ac_id"]) for i, a in enumerate(truth) for b in truth[i + 1:]))
                _, lo, hi = pairs[0]
                lo, hi = sorted([lo, hi])
                out.append(dict(env("INTENT", lo, {"leg": "DOWNWIND", "intent": "BASE_IN_20S", "valid_for_s": 20}), to=hi, delivered=True, reason=None))
                out.append(dict(env("MANEUVER_COMMIT", lo, {"target": hi, "sense": "L", "bank_deg": 30, "vs_fpm": 0, "start_t": now, "hold_s": 10}), to=hi, delivered=True, reason=None))
                out.append(dict(env("MANEUVER_COMMIT", hi, {"target": lo, "sense": "R", "bank_deg": 30, "vs_fpm": 0, "start_t": now + 0.2, "hold_s": 10}), to=lo, delivered=True, reason=None))
            if spoof and now - last_spoof >= 2.0:
                last_spoof = now
                out.append(dict(env("STATE", "GHOST7", {"lat": 33.6886, "lon": -112.055, "alt_press_ft": 1900, "gs_kt": 260, "track_deg": 266,
                                                        "vs_fpm": -500, "leg": "FINAL", "intent": "", "ap_equipped": False}),
                                sig="", to="*", delivered=False, reason="bad_signature"))
            for f in out:
                await ws.send(json.dumps(f))

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", default="ws://localhost:8765")
    ap.add_argument("--loss", type=float, default=0.1)
    ap.add_argument("--spoof", action="store_true")
    a = ap.parse_args()
    asyncio.run(run(a.world, a.loss, a.spoof))
