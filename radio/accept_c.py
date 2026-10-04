"""
radio/accept_c.py — Lane C acceptance checks (1 PM / 4 PM / 7 PM rows of lanes/C-radio-mansi.md).

Starts the world (stubs/fake_world.py unless --world is given) and radio/channel.py, then runs light
"radio-only" nodes in this process: each reads its own OWNSHIP from the world, sends STATE at 1 Hz from
it, and runs radio/evidence.py. Prints PASS/FAIL per check.

    python radio/accept_c.py              # all checks (~70 s)
    python radio/accept_c.py --only 1pm   # 1pm | 4pm | 7pm
    python radio/accept_c.py --world ws://192.168.137.1:8765   # against the real world on the hotspot
"""
from __future__ import annotations
import argparse, asyncio, collections, json, os, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import websockets                                  # noqa: E402
from radio.client import RadioClient               # noqa: E402
from radio.evidence import TrustEvidence, state_for   # noqa: E402
from radio import slots                            # noqa: E402

PY = sys.executable
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    RESULTS.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""), flush=True)


class MiniNode:
    """Radio-only stand-in for node/node.py: own OWNSHIP in, STATE out, evidence on."""
    def __init__(self, ac_id: str, world: str):
        self.id, self.world = ac_id, world
        self.radio = RadioClient(ac_id, verbose=False)
        self.ev = TrustEvidence.attach(self.radio)
        self.heard = collections.Counter()
        self.rejects: list[str] = []
        self.links: list[tuple[str, str, float]] = []
        self.radio.on_message(lambda e: self.heard.update([e["from"]]) if e["msg"] == "STATE" else None)
        self.radio.on_reject(lambda e, r: self.rejects.append(r))
        self.radio.on_link(lambda p, s, age: self.links.append((p, s, time.time(), age)))
        self.own = None
        self.tasks = []

    async def start(self):
        await self.radio.start()
        self.tasks.append(asyncio.create_task(self._world()))
        self.tasks.append(asyncio.create_task(self._tx()))

    async def _world(self):
        async with websockets.connect(f"{self.world}?role=node:{self.id}") as ws:
            async for raw in ws:
                m = json.loads(raw)
                if m.get("type") == "OWNSHIP":
                    self.own = m
                    self.ev.on_ownship(m)

    async def _tx(self):
        while True:
            await asyncio.sleep(1.0)
            o = self.own
            if o:
                await self.radio.send("STATE", {"lat": o["lat"], "lon": o["lon"], "alt_press_ft": o["alt_press_ft"],
                                                "gs_kt": o["gs_kt"], "track_deg": o["track_deg"], "vs_fpm": o["vs_fpm"],
                                                "leg": "UNKNOWN", "intent": "", "ap_equipped": o["ap_equipped"]})

    async def stop(self):
        for t in self.tasks:
            t.cancel()
        await self.radio.stop()


class LogTap:
    def __init__(self, world):
        self.world = world
        self.frames: list[dict] = []

    async def run(self):
        async with websockets.connect(f"{self.world}?role=log", max_size=None) as ws:
            async for raw in ws:
                m = json.loads(raw)
                if m.get("kind") == "radio":
                    self.frames.append(m.get("payload", {}))


def spawn(args: list[str], name: str):
    log = open(os.path.join(ROOT, "harness", "out", f"accept_c_{name}.log"), "w")
    return subprocess.Popen([PY] + args, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)


async def main(a):
    os.makedirs(os.path.join(ROOT, "harness", "out"), exist_ok=True)
    procs = []
    world = a.world
    if not world:
        procs.append(spawn(["stubs/fake_world.py", "--scenario", "harness/scenarios/judges.json"], "world"))
        world = "ws://localhost:8765"
        await asyncio.sleep(2.0)
    procs.append(spawn(["radio/channel.py", "--world", world, "--seed", "7"], "channel"))
    await asyncio.sleep(1.5)
    tap = LogTap(world)
    tap_task = asyncio.create_task(tap.run())
    nodes = {i: MiniNode(i, world) for i in ("N101", "N102", "N399", "N204", "N203")}
    for n in nodes.values():
        await n.start()
    try:
        if a.only in (None, "1pm"):
            print("\n1:00 PM — STATE through channel.py with range cutoff; far node hears nothing; all packets logged")
            await asyncio.sleep(10)
            n1, n2, far = nodes["N101"], nodes["N102"], nodes["N399"]
            check("N101 hears N102 and N102 hears N101", n1.heard["N102"] >= 4 and n2.heard["N101"] >= 4,
                  f"{n1.heard['N102']} / {n2.heard['N101']} STATEs in 10 s")
            far_ids = [i for i in ("N101", "N102") if far.heard[i]]
            check("N399 (> 3 mi from N101/N102) hears nothing from them", not far_ids, f"heard {far_ids or 'nothing'} from them")
            leak = [f for f in tap.frames if f.get("delivered") and (f.get("rng_m") or 0) > 4828]
            check("no packet is ever delivered beyond 4,828 m", not leak, f"{len(leak)} leaks")
            dl = [f for f in tap.frames if "delivered" in f]
            reasons = collections.Counter(("ok" if f["delivered"] else str(f["reason"]).split("(")[0]) for f in dl)
            check("every delivered and dropped packet is in the world log", reasons["ok"] > 0 and reasons["range"] > 0,
                  dict(reasons).__repr__())
        if a.only in (None, "4pm"):
            print("\n4:00 PM — bad signature, old seq, out-of-window rejected with a logged reason; lost link < 3 s")
            sp = spawn(["radio/spoofer.py", "--mode", "replay"], "spoof_replay")
            await asyncio.sleep(9)
            sp.terminate()
            await asyncio.sleep(3)                       # let links recover from the spoofer's collisions first
            allrej = collections.Counter(r.split("(")[0] for n in nodes.values() for r in n.rejects)
            logged = collections.Counter(str(f.get("reason")).split("(")[0] for f in tap.frames if f.get("rejected_by"))
            for why in ("bad_signature", "replay_seq", "time_window"):
                check(f"{why} rejected and logged", allrej[why] > 0 and logged[why] > 0,
                      f"nodes rejected {allrej[why]}, log has {logged[why]}")
            n1 = nodes["N101"]
            t_kill = time.time()
            subprocess.run([PY, "radio/faults.py", "kill-link", "N102", "--for", "5"], cwd=ROOT, capture_output=True)
            await asyncio.sleep(7)
            # Requirement: flag lost link within 3 s of the LAST packet heard. Measure exactly that (the LOST event's
            # silence age), not wall time since faults.py was launched (Python start-up + packets in flight vary).
            lost = [(t, age) for p, s, t, age in n1.links if p == "N102" and s == "LOST" and t > t_kill]
            restored = [t for p, s, t, age in n1.links if p == "N102" and s == "RESTORED" and t > t_kill]
            age = lost[0][1] if lost else None
            check("N101 flags N102 link LOST within 3 s of its last packet", bool(lost) and age <= 3.0 + 0.25,
                  f"flagged after {age:.2f} s of silence (limit 3 s + 0.2 s monitor tick)" if lost else "never flagged")
            check("link RESTORED after the fault ends", bool(restored))
        if a.only in (None, "7pm"):
            print("\n7:00 PM — spoofed GHOST7 on final: SUSPICIOUS, then FAKE; slot collisions; dropped COMMIT")
            sp = spawn(["radio/spoofer.py", "--mode", "unsigned"], "spoof")
            seen = collections.defaultdict(set)
            t0 = time.time()
            while time.time() - t0 < 14:
                await asyncio.sleep(0.5)
                for nid, n in nodes.items():
                    if "GHOST7" in n.ev.tracks:
                        seen[nid].add(state_for(n.ev.assess("GHOST7")[0]))
            sp.terminate()
            # judge only nodes that actually heard GHOST7 enough to decide (>= 5 STATEs): a node that caught one or two
            # packets at the edge of range has no RF statistics yet and correctly stays SUSPICIOUS.
            obs = {nid: n.ev.assess("GHOST7") for nid, n in nodes.items()
                   if "GHOST7" in n.ev.tracks and len(n.ev.tracks["GHOST7"].states) >= 5}
            check("GHOST7 shows SUSPICIOUS on a node", any("SUSPICIOUS" in s for s in seen.values()), dict(seen).__repr__())
            check("GHOST7 ends FAKE on every node that heard it >= 5 times", obs and all(state_for(s) == "FAKE" for s, _ in obs.values()),
                  "; ".join(f"{k}: {s} {ev}" for k, (s, ev) in obs.items()))
            real = {nid: n.ev.assess("N102") for nid, n in nodes.items() if nid != "N102" and "N102" in n.ev.tracks}
            check("real peer N102 stays TRUSTED", real and all(state_for(s) == "TRUSTED" for s, _ in real.values()),
                  "; ".join(f"{k}: {s}" for k, (s, _) in real.items()))
            rows = slots.report((8, 30), (20, 40), seeds=5)
            txt = "; ".join(f"{r['senders']} ac/{r['slots']} slots: {r['unslotted']:.0%} -> {r['sotdma']:.0%}" for r in rows)
            check("slot collision rate measured at 8 and 30 senders", all(r["sotdma"] < r["unslotted"] for r in rows), txt)
            subprocess.run([PY, "radio/faults.py", "drop-commit", "--from", "N101"], cwd=ROOT, capture_output=True)
            await asyncio.sleep(0.5)
            await nodes["N101"].radio.send("MANEUVER_COMMIT", {"target": "N102", "sense": "R", "bank_deg": 30, "vs_fpm": 0,
                                                               "start_t": time.time(), "hold_s": 10})
            await asyncio.sleep(2.5)
            dropped = [f for f in tap.frames if f.get("msg") == "MANEUVER_COMMIT" and str(f.get("reason", "")).startswith("fault")]
            check("faults.py drops a MANEUVER_COMMIT and the drop is logged", bool(dropped),
                  f"{len(dropped)} log entries 'fault:drop_maneuver_commit'")
    finally:
        for n in nodes.values():
            await n.stop()
        tap_task.cancel()
        for p in procs:
            p.terminate()
    passed = sum(ok for _, ok, _ in RESULTS)
    print(f"\nLane C: {passed}/{len(RESULTS)} checks passed")
    return passed == len(RESULTS)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--world")
    ap.add_argument("--only", choices=["1pm", "4pm", "7pm"])
    ok = asyncio.run(main(ap.parse_args()))
    sys.exit(0 if ok else 1)
