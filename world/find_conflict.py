"""
world/find_conflict.py — Lane A. Tune a scenario so chosen aircraft really conflict.

Flies the scenario offline (same physics + pattern autopilot as the world, no nodes, no
avoidance) for every combination of the --vary parameters, measures each pair's closest
approach while both are airborne and inside --window, and keeps the combination with the
tightest encounter. Score per pair = max(h/500 ft, v/100 ft) (< 1 = inside the NMAC box);
with 3+ aircraft the score is the WORST pair, so every pair must converge.

  python world/find_conflict.py --scenario harness/scenarios/base_cutoff.json --pair N101 N399 \\
      --vary N101.offset_s=0:120:2 --window 60:150 --legs DOWNWIND,BASE,FINAL,STRAIGHT_IN \\
      --out harness/scenarios/base_vs_straight_in.json --name base_vs_straight_in

  --vary ID.FIELD=START:STOP:STEP   FIELD = offset_s | agl_ft; repeat for a grid
  --window A:B      only count closest approach between A and B seconds (demo timing)
  --legs L1,L2      only count when BOTH aircraft are on one of these legs
  --min-agl FT      ignore moments when either aircraft is below this (runway overlaps), default 150
  --min-start-sep FT  reject settings where any pair spawns closer than this (default 2000), so
                      "conflicts" between aircraft stacked on the same spot don't win
  --only            write only the --pair aircraft to --out (default keeps the whole scenario)
"""
from __future__ import annotations
import argparse, copy, itertools, json, os, sys, time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from world.scenario import World
from world.separation import NMAC_H_FT, NMAC_V_FT, COLLISION_H_FT, COLLISION_V_FT, separation_ft, severity

def frange(spec: str) -> list[float]:
    a, b, c = (float(x) for x in spec.split(":"))
    n = int(round((b - a) / c)) + 1
    return [round(a + i * c, 6) for i in range(max(n, 1))]

def parse_vary(specs: list[str]) -> list[tuple[str, str, list[float]]]:
    out = []
    for sp in specs:
        lhs, rng = sp.split("=", 1)
        ac, fld = lhs.split(".", 1)
        if fld not in ("offset_s", "agl_ft"):
            sys.exit(f"--vary field must be offset_s or agl_ft, got {fld}")
        out.append((ac, fld, frange(rng)))
    return out

def apply(raw: dict, combo: dict) -> dict:
    scn = copy.deepcopy(raw)
    for spec in scn["aircraft"]:
        for (ac, fld), val in combo.items():
            if spec["id"] == ac:
                spec["start"][fld] = val
    return scn

def fly(scn: dict, pair: list[str], horizon: float, dt: float, window: tuple[float, float],
        legs: set | None, min_agl: float, min_start_sep: float = 0.0) -> dict:
    w = World(scn)
    fleet = {i: w.fleet[i] for i in pair}          # no nodes, no avoidance: others can't interact
    best = {p: (float("inf"), float("inf"), None, None) for p in itertools.combinations(pair, 2)}
    if any(separation_ft(fleet[p[0]], fleet[p[1]])[0] < min_start_sep for p in best):
        return {"score": float("inf"), "pairs": best}
    t = 0.0
    while t < horizon:
        for ac in fleet.values():
            ac.step(dt, w.env, t)
        t += dt
        if not (window[0] <= t <= window[1]):
            continue
        for p in best:
            a, b = fleet[p[0]], fleet[p[1]]
            if a.agl_ft < min_agl or b.agl_ft < min_agl:
                continue
            la, lb = a.autopilot.leg, b.autopilot.leg
            if legs and (la not in legs or lb not in legs):
                continue
            h, v = separation_ft(a, b)
            if severity(h, v) < severity(best[p][0], best[p][1]):
                best[p] = (h, v, round(t, 1), (la, lb))
    worst = max(severity(h, v) for h, v, _, _ in best.values())
    return {"score": worst, "pairs": best}

def describe(res: dict) -> str:
    parts = []
    for (a, b), (h, v, t, lg) in res["pairs"].items():
        if t is None:
            parts.append(f"{a}-{b}: no qualifying approach")
            continue
        tag = " COLLISION" if h < COLLISION_H_FT and v < COLLISION_V_FT else " NMAC" if h < NMAC_H_FT and v < NMAC_V_FT else ""
        parts.append(f"{a}-{b}: {h:.0f} ft / {v:.0f} ft at t={t:.0f} s ({lg[0]}/{lg[1]}){tag}")
    return "; ".join(parts)

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--pair", nargs="+", required=True, help="aircraft ids that must conflict (2 or more)")
    ap.add_argument("--vary", action="append", default=[], help="ID.FIELD=START:STOP:STEP")
    ap.add_argument("--window", default="0:300", help="A:B seconds")
    ap.add_argument("--horizon", type=float, default=None, help="seconds to fly (default: window end)")
    ap.add_argument("--legs", default="", help="comma list; both aircraft must be on one of these")
    ap.add_argument("--min-agl", type=float, default=150.0)
    ap.add_argument("--min-start-sep", type=float, default=2000.0)
    ap.add_argument("--dt", type=float, default=0.05, help="physics step; 0.05 = the world's 20 Hz")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--out")
    ap.add_argument("--name")
    ap.add_argument("--only", action="store_true")
    a = ap.parse_args()

    raw = json.load(open(a.scenario))
    ids = {s["id"] for s in raw["aircraft"]}
    for i in a.pair:
        if i not in ids:
            sys.exit(f"{i} not in scenario ({sorted(ids)})")
    win = tuple(float(x) for x in a.window.split(":"))
    horizon = a.horizon or win[1]
    legs = {x.strip().upper() for x in a.legs.split(",") if x.strip()} or None
    vary = parse_vary(a.vary)
    keys = [(ac, fld) for ac, fld, _ in vary]
    grid = list(itertools.product(*[vals for _, _, vals in vary])) or [()]

    t0 = time.time()
    results = []
    for vals in grid:
        combo = dict(zip(keys, vals))
        res = fly(apply(raw, combo), a.pair, horizon, a.dt, win, legs, a.min_agl, a.min_start_sep)
        results.append((res["score"], combo, res))
    results.sort(key=lambda r: r[0])
    print(f"{len(grid)} runs in {time.time() - t0:.1f} s | window {win[0]:.0f}-{win[1]:.0f} s"
          + (f" | legs {','.join(sorted(legs))}" if legs else "") + f" | min AGL {a.min_agl:.0f} ft")
    for score, combo, res in results[: a.top]:
        setting = ", ".join(f"{ac}.{fld}={v:g}" for (ac, fld), v in combo.items()) or "(as is)"
        print(f"  score {score:5.2f}  {setting}  ->  {describe(res)}")

    if a.out:
        score, combo, res = results[0]
        out = apply(raw, combo)
        if a.only:
            out["aircraft"] = [s for s in out["aircraft"] if s["id"] in a.pair]
        if a.name:
            out["name"] = a.name
        out["note"] = (f"tuned by world/find_conflict.py from {os.path.basename(a.scenario)}: {describe(res)} "
                       f"(no avoidance; window {win[0]:.0f}-{win[1]:.0f} s)")
        with open(a.out, "w") as f:
            json.dump(out, f, indent=1)
        print(f"wrote {a.out}")

if __name__ == "__main__":
    main()
