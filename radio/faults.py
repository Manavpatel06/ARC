"""
radio/faults.py — scripted radio faults for the demo and RED FLOCK (Lane C, task C9).
Sends FAULT control frames to radio/channel.py, which applies them and logs every affected packet
with reason "fault:...".

  python radio/faults.py drop-commit [--from N101] [--count 1]      drop the next MANEUVER_COMMIT (lost-link fallback demo)
  python radio/faults.py kill-link N204 [--for 5] [--msg HEARTBEAT] silence a node (all msgs by default) for 5 s
  python radio/faults.py latency 2.0 [--for 10]                     spike link latency to 2 s for 10 s
  python radio/faults.py script faults.json                         [{"at_s":5,"kind":"drop","msg":"MANEUVER_COMMIT"}, ...]
Add --via-channel ws://<channel-ip>:8766 when multicast is blocked, --iface <my-ip> on multi-NIC laptops.
"""
from __future__ import annotations
import argparse, asyncio, json, os, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from radio import wire     # noqa: E402


async def send_faults(rules: list[dict], via: str | None, iface: str | None):
    if via:
        import websockets
        async with websockets.connect(via.rstrip("/") + "/?id=FAULTS") as ws:
            t0 = time.time()
            for r in rules:
                await asyncio.sleep(max(0.0, r.pop("at_s", 0) - (time.time() - t0)))
                await ws.send(json.dumps(dict(r, ctl="FAULT")))
                print(f"[faults] sent {r}")
        return
    sock, dest, mode = wire.open_udp(0, wire.GROUP, iface, listen=False)
    t0 = time.time()
    for r in rules:
        await asyncio.sleep(max(0.0, r.pop("at_s", 0) - (time.time() - t0)))
        frame = wire.dumps(dict(r, ctl="FAULT"))
        for _ in range(3):                                   # UDP: send 3 copies; the channel de-duplicates by id
            sock.sendto(frame, (dest, wire.UPLINK_PORT))
            await asyncio.sleep(0.05)
        print(f"[faults] sent {r} ({mode})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--via-channel")
    ap.add_argument("--iface")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("drop-commit"); p.add_argument("--from", dest="frm"); p.add_argument("--count", type=int, default=1)
    p = sub.add_parser("kill-link"); p.add_argument("node"); p.add_argument("--for", dest="dur", type=float, default=5.0)
    p.add_argument("--msg", default="*")
    p = sub.add_parser("latency"); p.add_argument("value", type=float); p.add_argument("--for", dest="dur", type=float, default=10.0)
    p = sub.add_parser("script"); p.add_argument("file")
    a = ap.parse_args()
    fid = f"{int(time.time()*1000)}"
    if a.cmd == "drop-commit":
        rules = [{"kind": "drop", "msg": "MANEUVER_COMMIT", "target": a.frm, "count": a.count, "duration_s": 120}]
    elif a.cmd == "kill-link":
        rules = [{"kind": "silence", "target": a.node, "msg": a.msg, "duration_s": a.dur}]
    elif a.cmd == "latency":
        rules = [{"kind": "latency", "value_s": a.value, "duration_s": a.dur}]
    else:
        rules = json.load(open(a.file))
    for i, r in enumerate(rules):
        r["id"] = f"{fid}-{i}"
    asyncio.run(send_faults(rules, a.via_channel, a.iface))


if __name__ == "__main__":
    main()
