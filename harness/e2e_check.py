"""
harness/e2e_check.py — Lane D. Live end-to-end check of the FLOCK core against the REAL running system.

Connects to a running world as `god` (truth + decisions) and `log` (every radio packet), watches for
--seconds, then answers the four core questions with numbers:
  1. Do aircraft talk continuously?   STATE delivery per pair in range, longest silence per pair.
  2. Do they negotiate?               SEQ_PROPOSE/ACCEPT and MANEUVER_COMMIT pairs, complementary senses.
  3. Do they predict the turn?        PREDICTION frames: method turn-aware vs straight-line, legs seen.
  4. Do they avoid in real time?      decision timeline per aircraft + truth minimum separation, NMACs.

    python harness/e2e_check.py --world ws://localhost:8765 --seconds 150 [--out harness/out/e2e_<name>.json]
Run once with nodes (FLOCK) and once with only the world (no nodes) for the same scenario to compare.
"""
from __future__ import annotations
import argparse, asyncio, collections, json, math, time
import websockets

NMAC_H_FT, NMAC_V_FT = 500.0, 100.0

def sep(a, b):
    dn = (b["lat"] - a["lat"]) * 111_320
    de = (b["lon"] - a["lon"]) * 111_320 * math.cos(math.radians(a["lat"]))
    return math.hypot(de, dn) / 0.3048, abs(b["alt_msl_ft"] - a["alt_msl_ft"])

async def watch(world: str, seconds: float):
    R = {"truth_min": {}, "nmac": {}, "decisions": [], "pred": collections.Counter(), "pred_legs": collections.Counter(),
         "state": collections.Counter(), "state_drop": collections.Counter(), "last_rx": {}, "gap": collections.defaultdict(float),
         "neg": [], "world_events": [], "aircraft": []}
    end = time.time() + seconds

    async def god():
        async with websockets.connect(f"{world}?role=god", max_size=None) as ws:
            async for raw in ws:
                m = json.loads(raw); t = m.get("type")
                if t == "TRUTH":
                    ac = [a for a in m["aircraft"] if a.get("agl_ft", 999) > 50]
                    R["aircraft"] = sorted({a["ac_id"] for a in m["aircraft"]} | set(R["aircraft"]))
                    for i, a in enumerate(ac):
                        for b in ac[i + 1:]:
                            k = "-".join(sorted((a["ac_id"], b["ac_id"])))
                            h, v = sep(a, b)
                            cur = R["truth_min"].get(k)
                            score = h if v < NMAC_V_FT else 1e9
                            if cur is None or score < cur["score"]:
                                R["truth_min"][k] = {"score": score, "h_ft": round(h), "v_ft": round(v), "t": m["t"]}
                            if h < NMAC_H_FT and v < NMAC_V_FT:
                                R["nmac"].setdefault(k, {"h_ft": round(h), "v_ft": round(v), "t": m["t"]})
                elif t in ("ADVISORY", "COMMAND", "STICK"):
                    R["decisions"].append({"t": m.get("t"), "ac": m.get("ac_id"), "type": t, "level": m.get("level") or m.get("mode"),
                                           "text": m.get("text") or (f"bank {m.get('bank_cmd_deg')}" if t == "COMMAND" else ""),
                                           "target": m.get("target_id"), "ttc": m.get("ttc_s"), "applied": m.get("applied")})
                elif t == "PREDICTION":
                    R["pred"][m.get("method", "?")] += 1
                    if m.get("leg"):
                        R["pred_legs"][m["leg"]] += 1
                elif t == "WORLD_EVENT":
                    R["world_events"].append(m)
                if time.time() > end:
                    return

    async def log():
        async with websockets.connect(f"{world}?role=log", max_size=None) as ws:
            async for raw in ws:
                f = json.loads(raw)
                p = f.get("payload", {})
                if f.get("kind") == "radio" and p.get("msg"):
                    frm, to = p.get("from"), p.get("to")
                    if p["msg"] == "STATE" and to and to != "*":
                        k = f"{frm}>{to}"
                        if p.get("delivered") is False:
                            if not str(p.get("reason", "")).startswith("range"):
                                R["state_drop"][k] += 1
                        else:
                            R["state"][k] += 1
                            now = time.time()
                            if k in R["last_rx"]:
                                R["gap"][k] = max(R["gap"][k], now - R["last_rx"][k])
                            R["last_rx"][k] = now
                    elif p["msg"] in ("SEQ_PROPOSE", "SEQ_ACCEPT", "MANEUVER_COMMIT", "INTENT") and p.get("delivered") is not False:
                        b = p.get("body", {})
                        R["neg"].append({"t": p.get("t"), "msg": p["msg"], "from": frm, "to": to,
                                         "sense": b.get("sense"), "target": b.get("target"), "order": b.get("order")})
                if time.time() > end:
                    return

    await asyncio.wait([asyncio.create_task(god()), asyncio.create_task(log())], timeout=seconds + 5)
    return R

def report(R: dict) -> str:
    out = []
    pairs = sorted(set(k for k in R["state"]) | set(R["state_drop"]))
    if pairs:
        tot_ok = sum(R["state"].values()); tot_drop = sum(R["state_drop"].values())
        worst = max(pairs, key=lambda k: R["gap"].get(k, 0))
        out.append(f"1. CONTINUOUS LINK: {tot_ok} STATE delivered, {tot_drop} lost in range ({100*tot_drop/max(1,tot_ok+tot_drop):.1f}% loss) "
                   f"across {len(pairs)} directed pairs; longest silence {R['gap'].get(worst,0):.1f} s ({worst})")
    else:
        out.append("1. CONTINUOUS LINK: no radio traffic seen (no nodes / no channel)")
    commits = [n for n in R["neg"] if n["msg"] == "MANEUVER_COMMIT"]
    seqs = [n for n in R["neg"] if n["msg"] in ("SEQ_PROPOSE", "SEQ_ACCEPT")]
    uniq = {}
    for c in commits:
        uniq.setdefault((c["from"], c["target"]), c["sense"])
    pairs_c = []
    for (a, b), s in uniq.items():
        if (b, a) in uniq and a < b:
            pairs_c.append(f"{a} {s} / {b} {uniq[(b, a)]}")
    out.append(f"2. NEGOTIATION: {len(seqs)} sequencing messages, {len(uniq)} maneuver commits; paired decisions: "
               + (", ".join(pairs_c) if pairs_c else "none paired") +
               ("" if not uniq else "  [" + ", ".join(f"{a}->{b}:{s}" for (a, b), s in uniq.items()) + "]"))
    if R["pred"]:
        out.append(f"3. PREDICTION: {dict(R['pred'])} frames; legs predicted {dict(R['pred_legs'])}")
    else:
        out.append("3. PREDICTION: no PREDICTION frames published (nodes may predict internally without publishing)")
    first = {}
    for d in R["decisions"]:
        first.setdefault((d["ac"], d["level"]), d)
    tl = sorted(first.values(), key=lambda d: d["t"] or 0)
    t0 = tl[0]["t"] if tl else 0
    out.append(f"4. AVOIDANCE: {len(R['decisions'])} decisions; first of each kind:")
    for d in tl[:20]:
        out.append(f"     +{(d['t'] or 0) - t0:6.1f}s {d['ac']:5s} {d['type']:8s} {str(d['level']):11s} {d['text'] or ''}"
                   + (f" vs {d['target']}" if d.get("target") else "") + (f" ttc {d['ttc']:.0f}s" if isinstance(d.get("ttc"), (int, float)) else ""))
    out.append("   truth minimum separation per pair (closest point with < 100 ft vertical, else closest overall):")
    for k, v in sorted(R["truth_min"].items(), key=lambda kv: kv[1]["score"]):
        out.append(f"     {k:11s} {v['h_ft']:6d} ft horizontal / {v['v_ft']:4d} ft vertical")
    out.append(f"   NMAC (< 500 ft and < 100 ft): {len(R['nmac'])} pair(s) {list(R['nmac'])}")
    return "\n".join(out)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", default="ws://localhost:8765")
    ap.add_argument("--seconds", type=float, default=150)
    ap.add_argument("--out")
    a = ap.parse_args()
    R = asyncio.run(watch(a.world, a.seconds))
    txt = report(R)
    print(txt)
    if a.out:
        R2 = {k: (dict(v) if isinstance(v, collections.Counter) else v) for k, v in R.items() if k != "last_rx"}
        R2["gap"] = dict(R["gap"])
        json.dump(R2, open(a.out, "w"), default=str, indent=1)
