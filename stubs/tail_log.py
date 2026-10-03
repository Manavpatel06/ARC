"""
stubs/tail_log.py — console tail of the world's `log` role, for anyone who needs to see
frames before Lane D's web/log.html exists. Also writes harness/out/log_<ts>.jsonl.

Run:  python stubs/tail_log.py --world ws://localhost:8765 [--filter N101]
"""
from __future__ import annotations
import argparse, asyncio, json, os, time
import websockets

async def run(world: str, flt: str | None):
    os.makedirs("harness/out", exist_ok=True)
    path = f"harness/out/log_{int(time.time())}.jsonl"
    f = open(path, "a")
    async with websockets.connect(f"{world}?role=log") as ws:
        print(f"[log] connected; writing {path}")
        async for raw in ws:
            m = json.loads(raw)
            f.write(raw + "\n")
            p = m.get("payload", {})
            src = m.get("src", "?")
            if flt and flt not in json.dumps(m):
                continue
            kind = m.get("kind")
            if kind == "decision":
                desc = f"{p.get('type')} {p.get('level') or p.get('mode') or ''} {p.get('text') or ''}".strip()
                if p.get("type") == "TRUST":
                    desc = "TRUST " + ", ".join(f"{t['id']}={t['state']}" for t in p.get("targets", []))
            elif kind == "radio":
                desc = f"radio {p.get('msg')} from {p.get('from')} seq {p.get('seq')} " + ("" if p.get("delivered", True) else f"DROPPED({p.get('reason')})")
            else:
                desc = json.dumps(p)[:100]
            print(f"{time.strftime('%H:%M:%S')} {kind:8s} {src:7s} {desc}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", default="ws://localhost:8765")
    ap.add_argument("--filter")
    a = ap.parse_args()
    asyncio.run(run(a.world, a.filter))
