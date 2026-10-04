"""
radio/robustness.py — does the ARC core still keep aircraft apart when the radio is bad? (Lane C)

For each scenario x radio condition it starts a private world + channel + one node per ARC aircraft on its
own ports, applies the condition, and measures the run with harness/e2e_check.py (truth minimum separation,
NMAC count, link loss, negotiation). Runs several in parallel. Writes harness/out/robustness.json and
harness/out/robustness_table.md (raw table; radio/ROBUSTNESS.md is the curated slide summary).

Conditions: no ARC (world only, baseline) · loss 10 % · 30 % · 50 % · 2 s latency spike (+2 s for 60 s
from t = 10 s, on top of 10 % loss) · dropped MANEUVER_COMMIT (first commit dropped, 10 % loss).

    python radio/robustness.py                                  # both scenarios, all conditions, 120 s each
    python radio/robustness.py --scenarios head_on_judges --seconds 60 --seeds 1
"""
from __future__ import annotations
import argparse, asyncio, json, os, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
OUT = os.path.join(ROOT, "harness", "out")

CONDITIONS = [
    ("no_arc", "No ARC (baseline)", dict(loss=None)),
    ("loss0", "Perfect radio (0 % loss)", dict(loss=0.0)),
    ("loss10", "Loss 10 %", dict(loss=0.10)),
    ("loss30", "Loss 30 %", dict(loss=0.30)),
    ("loss50", "Loss 50 %", dict(loss=0.50)),
    ("latency2", "Latency spike 2 s (60 s)", dict(loss=0.10, fault=["latency", "2.0", "--for", "60"], fault_at=10)),
    ("dropcommit", "Dropped MANEUVER_COMMIT", dict(loss=0.10, fault=["drop-commit", "--count", "1"], fault_at=1)),
]


def arc_ids(scenario_path: str) -> list[str]:
    sc = json.load(open(scenario_path))
    return [a["id"] for a in sc["aircraft"] if a.get("arc", True)]


async def run_one(scn: str, key: str, cfg: dict, seed: int, slot: int, seconds: float) -> dict:
    wport, cport = 8800 + slot * 10, 8801 + slot * 10
    world = f"ws://localhost:{wport}"
    path = os.path.join(ROOT, "harness", "scenarios", f"{scn}.json")
    tag = f"{scn}_{key}_s{seed}"
    logs = open(os.path.join(OUT, f"rob_{tag}.log"), "w")
    procs = []

    def start(args):
        p = subprocess.Popen([PY] + args, cwd=ROOT, stdout=logs, stderr=subprocess.STDOUT)
        procs.append(p)
        return p

    try:
        start(["world/world_server.py", "--scenario", path, "--port", str(wport), "--http", "0"])
        await asyncio.sleep(3)
        if cfg["loss"] is not None:
            start(["radio/channel.py", "--world", world, "--ws-port", str(cport), "--loss", str(cfg["loss"]),
                   "--seed", str(seed), "--log", "all"])
            await asyncio.sleep(1.5)
            for ac in arc_ids(path):
                start(["node/node.py", "--id", ac, "--world", world, "--via-channel", f"ws://localhost:{cport}"])
        out_json = os.path.join(OUT, f"rob_{tag}.json")
        e2e = subprocess.Popen([PY, "harness/e2e_check.py", "--world", world, "--seconds", str(seconds), "--out", out_json],
                               cwd=ROOT, stdout=logs, stderr=subprocess.STDOUT)
        if cfg.get("fault"):
            await asyncio.sleep(cfg.get("fault_at", 1))
            subprocess.run([PY, "radio/faults.py", "--via-channel", f"ws://localhost:{cport}"] + cfg["fault"],
                           cwd=ROOT, stdout=logs, stderr=subprocess.STDOUT)
        while e2e.poll() is None:
            await asyncio.sleep(1)
        R = json.load(open(out_json))
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        logs.close()
    return summarize(scn, key, seed, R, tag)


def summarize(scn, key, seed, R, tag) -> dict:
    pairs = R.get("truth_min", {})
    # closest approach: smallest horizontal among pairs that came within 100 ft vertically, else overall score
    close = [(k, v) for k, v in pairs.items() if v.get("v_ft", 999) < 100] or list(pairs.items())
    k, v = min(close, key=lambda kv: kv[1]["h_ft"]) if close else ("-", {"h_ft": None, "v_ft": None})
    st_ok, st_drop = sum(R.get("state", {}).values()), sum(R.get("state_drop", {}).values())
    decisions = R.get("decisions", [])
    levels = {}
    for d in decisions:
        levels[d.get("level")] = levels.get(d.get("level"), 0) + 1
    commits = [n for n in R.get("neg", []) if n.get("msg") == "MANEUVER_COMMIT"]
    log_txt = open(os.path.join(OUT, f"rob_{tag}.log")).read()
    return {"scenario": scn, "condition": key, "seed": seed, "min_pair": k, "min_h_ft": v["h_ft"], "min_v_ft": v["v_ft"],
            "nmac": len(R.get("nmac", {})), "nmac_pairs": list(R.get("nmac", {})),
            "state_loss_pct": round(100 * st_drop / max(1, st_ok + st_drop), 1) if (st_ok + st_drop) else None,
            "longest_gap_s": round(max(R.get("gap", {}).values() or [0]), 1),
            "resolve": levels.get("RESOLVE", 0), "takeover": levels.get("TAKEOVER", 0),
            "commits": len(commits), "commit_dropped": log_txt.count("FAULT armed") > 0 and key == "dropcommit"}


async def main(a):
    os.makedirs(OUT, exist_ok=True)
    jobs = []
    for scn in a.scenarios:
        for key, label, cfg in CONDITIONS:
            if a.only and key not in a.only:
                continue
            for seed in (range(1, a.seeds + 1) if cfg["loss"] is not None else [1]):
                jobs.append((scn, key, cfg, seed))
    print(f"{len(jobs)} runs x {a.seconds:.0f} s, {a.parallel} at a time", flush=True)
    results, sem = [], asyncio.Semaphore(a.parallel)
    free = list(range(a.parallel))

    async def worker(job):
        async with sem:
            slot = free.pop()
            try:
                r = await run_one(*job, slot=slot, seconds=a.seconds)
                results.append(r)
                print(f"  {r['scenario']:15s} {r['condition']:11s} s{r['seed']}: min {r['min_h_ft']} ft / {r['min_v_ft']} ft "
                      f"({r['min_pair']})  NMAC {r['nmac']}  loss {r['state_loss_pct']}%  RA {r['resolve']}  TO {r['takeover']}", flush=True)
            finally:
                free.append(slot)
    await asyncio.gather(*(worker(j) for j in jobs))
    json.dump(results, open(os.path.join(OUT, f"robustness{a.tag}.json"), "w"), indent=1)
    write_md(results, a)


def write_md(results, a):
    label = {k: l for k, l, _ in CONDITIONS}
    lines = ["# ARC robustness under a bad radio", "",
             f"Each cell: worst run over {a.seeds} seed(s), {a.seconds:.0f} s, truth from the world (`harness/e2e_check.py`). "
             "Min separation = closest pair that came within 100 ft vertically (else closest overall). "
             "NMAC = < 500 ft horizontal and < 100 ft vertical.", ""]
    for scn in a.scenarios:
        lines += [f"## {scn}", "", "| Radio condition | Min separation | NMAC | STATE loss measured | RESOLVE / TAKEOVER | Commits |",
                  "|---|---|---|---|---|---|"]
        for key, _, _ in CONDITIONS:
            rs = [r for r in results if r["scenario"] == scn and r["condition"] == key]
            if not rs:
                continue
            w = min(rs, key=lambda r: (r["min_h_ft"] if r["min_h_ft"] is not None else 1e9))
            nm = max(r["nmac"] for r in rs)
            loss = "–" if w["state_loss_pct"] is None else f"{w['state_loss_pct']:.0f} %"
            ra = "–" if key == "no_arc" else f"{max(r['resolve'] for r in rs)} / {max(r['takeover'] for r in rs)}"
            cm = "–" if key == "no_arc" else str(max(r["commits"] for r in rs)) + (" (1st dropped)" if key == "dropcommit" else "")
            lines.append(f"| {label[key]} | {w['min_h_ft']:,} ft / {w['min_v_ft']} ft ({w['min_pair']}) | **{nm}** | {loss} | {ra} | {cm} |")
        lines.append("")
    open(os.path.join(OUT, f"robustness_table{a.tag}.md"), "w").write("\n".join(lines))   # radio/ROBUSTNESS.md is the curated summary
    print("\n".join(lines))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", nargs="+", default=["head_on_judges", "three_on_final"])
    ap.add_argument("--only", nargs="*", help="condition keys: " + " ".join(k for k, _, _ in CONDITIONS))
    ap.add_argument("--seconds", type=float, default=120)
    ap.add_argument("--seeds", type=int, default=2)
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--tag", default="", help="suffix for harness/out/robustness<tag>.json")
    asyncio.run(main(ap.parse_args()))
