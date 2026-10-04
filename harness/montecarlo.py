"""
harness/montecarlo.py — Lane B (B10).  ARC vs "straight-line + fixed maneuver", same encounters.

Encounter set (KDVT 25L left traffic, seeded, randomised pilot behaviour):
  conflict-prone  : overtake on downwind, base-to-final cut-off, base vs straight-in, head-on crosswind
  non-conflict    : spaced pattern traffic, vertical crossing (+500..800 ft), lateral pass (900..1800 ft)

Each encounter is flown three times from the same seed:
  none      nobody runs anything (labels the encounter: true NMAC / close / benign)
  baseline  harness/baseline.py  - straight-line prediction, fixed R30 turn, no sequencing/negotiation
  flock     node/node.py         - turn-aware prediction, Escape Field, negotiation, sequencing

Both logics share thresholds (90/35/20/8 s), sigma growth, the bounds monitor, radio loss/latency and the
same pilot model (5 s reaction, 70 % comply with an advisory; AP-equipped aircraft take over at 8 s).
Metrics: NMAC rate, warning lead time before the closest approach, maneuver severity (bank), nuisance alerts.

Run:  python harness/montecarlo.py [--n 30] [--workers 4] [--loss 0.1] [--latency 0.3]
Out:  harness/out/flock_vs_baseline.png (+ .json)

Everything here is simulation under the published geometry.  It shows a timely intervention under these
assumptions; it does not claim that any real accident would have been prevented.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys
import time
import zlib
from concurrent.futures import ProcessPoolExecutor

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from harness.baseline import BaselineNode
from harness.simworld import NMAC_H_M, NMAC_V_M, T0, Sim, SimAircraft
from node.geometry import FT, KT, build_patterns, hvec
from node.node import Node
from node.predict import PatternFollower

KINDS = {
    "overtake_downwind": True, "base_cutoff": True, "base_vs_straight_in": True, "head_on_crosswind": True,
    "spaced_pattern": False, "vertical_cross": False, "lateral_pass": False,
}
DURATION_S = 150.0
DT = 0.2


def _pilot(rng: random.Random) -> dict:
    return dict(pilot_bank=rng.uniform(15.0, 25.0), lead_scale=rng.uniform(0.85, 1.15),
                comply=0.7, react_s=5.0, ap=rng.random() < 0.5)


def _on_leg(pat, leg, rng, offset_m, kt, jitter_m=12.0):
    """Position/heading/altitude at offset_m along the circuit from the START of `leg` (pattern.place semantics)."""
    pl = pat.place(leg, offset_m / (kt * KT), kt)
    z = pat.elev_m + pl["agl_ft"] * FT + rng.uniform(-jitter_m, jitter_m)
    return pl["x_m"], pl["y_m"], z, pl["hdg_deg"]


def _eta_to_final_entry(pat, leg, x, y, z, v_ms, hdg, bank, lead) -> float:
    f = PatternFollower(pat, leg, x, y, z, v_ms, hdg, bank_deg=bank, lead_scale=lead)
    t = 0.0
    while t < 300.0:
        f.step(0.5)
        t += 0.5
        if f.leg == "FINAL" and not f.turning:
            return t
    return t


def make_encounter(kind: str, seed: int, pats: dict) -> list[SimAircraft]:
    rng = random.Random(seed * 31 + zlib.crc32(kind.encode()) % 1000)
    pat = pats["25L"]
    pa, pb = _pilot(rng), _pilot(rng)
    ka = rng.uniform(80, 95)
    out: list[SimAircraft] = []
    dw = pat.legs["DOWNWIND"]
    bs = pat.legs["BASE"]

    def ac(ac_id, leg, x, y, z, h, kt, p, flock=True):
        return SimAircraft(ac_id, pat, leg, x, y, z, h, kt, ap=p["ap"], flock=flock, human=True,
                           pilot_bank=p["pilot_bank"], lead_scale=p["lead_scale"], comply=p["comply"], react_s=p["react_s"])

    if kind == "overtake_downwind":
        off = rng.uniform(1800, 3200)
        gap = rng.uniform(450, 1000)
        kb = ka + rng.uniform(14, 24)
        xa, ya, za, ha = _on_leg(pat, "DOWNWIND", rng, off, ka)
        xb, yb, zb, hb = _on_leg(pat, "DOWNWIND", rng, off - gap, kb)         # B starts behind and is faster
        out = [ac("N101", "DOWNWIND", xa, ya, za, ha, ka, pa), ac("N204", "DOWNWIND", xb, yb, zb, hb, kb, pb)]
    elif kind in ("base_cutoff", "base_vs_straight_in"):
        leg_a = "DOWNWIND" if kind == "base_cutoff" else "BASE"
        off_a = (dw.length - rng.uniform(2200, 4200)) if leg_a == "DOWNWIND" else (bs.length - rng.uniform(600, 1200))
        xa, ya, za, ha = _on_leg(pat, leg_a, rng, off_a, ka)
        eta = _eta_to_final_entry(pat, leg_a, xa, ya, za, ka * KT, ha, pa["pilot_bank"], pa["lead_scale"])
        kb = rng.uniform(88, 104)
        jit = rng.uniform(-9, 9)
        s_entry = -(pat.base_dist - 593.0)                                # where a nominal 20 deg turn rolls out
        sb = s_entry - kb * KT * (eta + jit)
        xb, yb = pat.sl_to_xy(sb, rng.uniform(-40, 40))
        zb = pat.elev_m + 15.0 + math.tan(math.radians(3.0)) * (-sb) + rng.uniform(-15, 15)
        zb = min(zb, pat.tpa_msl_m + 15.0)
        out = [ac("N101", leg_a, xa, ya, za, ha, ka, pa), ac("N204", "STRAIGHT_IN", xb, yb, zb, pat.heading, kb, pb)]
    elif kind == "head_on_crosswind":
        L = pat.legs["CROSSWIND"]
        U = pat.legs["UPWIND"]
        kb = rng.uniform(85, 110)
        meet = rng.uniform(0.35, 0.65)                                   # fraction along the crosswind leg
        mx, my = L.a[0] + L.d[0] * L.length * meet, L.a[1] + L.d[1] * L.length * meet
        tmeet = rng.uniform(40, 60)
        t_cw = L.length * meet / (ka * KT)
        back = min(U.length, max(0.0, ka * KT * (tmeet - t_cw)))
        ax, ay = U.b[0] - U.d[0] * back, U.b[1] - U.d[1] * back
        tmeet = back / (ka * KT) + t_cw
        bx, by = mx + L.d[0] * kb * KT * tmeet, my + L.d[1] * kb * KT * tmeet
        za = pat.tpa_msl_m + rng.uniform(-10, 10)
        out = [ac("N101", "UPWIND", ax, ay, za, U.heading, ka, pa),
               ac("N204", None, bx, by, za + rng.uniform(-18, 18), (L.heading + 180.0) % 360.0, kb, pb)]
    elif kind == "spaced_pattern":
        off = rng.uniform(2500, 3600)
        space = rng.uniform(35, 50) * ka * KT
        xa, ya, za, ha = _on_leg(pat, "DOWNWIND", rng, off, ka)
        xb, yb, zb, hb = _on_leg(pat, "DOWNWIND", rng, off - space, ka + rng.uniform(-4, 4))
        out = [ac("N101", "DOWNWIND", xa, ya, za, ha, ka, pa), ac("N204", "DOWNWIND", xb, yb, zb, hb, ka, pb)]
    elif kind in ("vertical_cross", "lateral_pass"):
        L = dw
        kb = rng.uniform(85, 110)
        tmeet = rng.uniform(55, 85)
        along_meet = ka * KT * tmeet + rng.uniform(50, 200)
        mx, my = L.a[0] + L.d[0] * along_meet, L.a[1] + L.d[1] * along_meet
        xa, ya = mx - L.d[0] * ka * KT * tmeet, my - L.d[1] * ka * KT * tmeet
        perp = (-L.d[1], L.d[0])
        if kind == "vertical_cross":
            dz, lat = rng.uniform(500, 800) * FT * rng.choice([-1, 1]), 0.0
            hb = (L.heading + 90.0) % 360.0
        else:
            dz, lat = rng.uniform(-20, 20) * FT, rng.uniform(2000, 4000) * FT * rng.choice([-1, 1])
            hb = (L.heading + 180.0) % 360.0
        hv = hvec(hb)
        bx = mx + perp[0] * lat - hv[0] * kb * KT * tmeet
        by = my + perp[1] * lat - hv[1] * kb * KT * tmeet
        za = pat.tpa_msl_m + rng.uniform(-15, 15)
        out = [ac("N101", "DOWNWIND", xa, ya, za, L.heading, ka, pa), ac("N204", None, bx, by, za + dz, hb, kb, pb)]
    return out


def run_one(args) -> dict:
    kind, seed, loss, latency, modes = args
    pats = build_patterns()
    res: dict = {"kind": kind, "seed": seed}
    factories = {"none": None,
                 "baseline": lambda i: BaselineNode(i, patterns=pats, record=False),
                 "flock_nosq": lambda i: Node(i, patterns=pats, record=False),
                 "flock": lambda i: Node(i, patterns=pats, record=False)}
    for mode in modes:
        ac = make_encounter(kind, seed, pats)
        sim = Sim(pats, ac, factories[mode], loss=loss, latency_s=latency, dt=DT, seed=seed,
                  follow_sequence=(mode != "flock_nosq"))
        r = sim.run(DURATION_S)
        margin = max(r.min_h_m / NMAC_H_M, r.min_v_at_min_h_m / NMAC_V_M)
        alerts = {lv for (_, lv) in r.first_level_t if lv in ("TRAFFIC", "RESOLVE", "TAKEOVER")}
        pilot_alert_t = [r.first_level_t[k] - T0 for k in r.first_level_t if k[1] in ("TRAFFIC", "RESOLVE", "TAKEOVER")]
        res[mode] = {
            "margin": margin, "min_h_ft": r.min_h_m / FT, "min_v_ft": r.min_v_at_min_h_m / FT, "nmac": r.nmac,
            "t_cpa": r.t_cpa,
            "t_detect": min(r.first_conflict_t.values()) if r.first_conflict_t else None,
            "t_alert": min(pilot_alert_t) if pilot_alert_t else None,
            "alerted": bool(alerts), "maneuvered": sorted(r.maneuvered), "max_bank": r.max_bank,
            "tick_ms_mean": r.tick_ms_mean, "tick_ms_max": r.tick_ms_max,
            "no_solution": any(f["level"] == "NO_SOLUTION" for _, _, f in r.advisories),
        }
        if mode == "none":
            res["label"] = "conflict" if r.nmac else ("close" if margin < 3.0 else "benign")
            if res["label"] == "close":
                break                                           # ambiguous: do not score it
    return res


MODES = ("none", "baseline", "flock_nosq", "flock")


def summarise(rows: list[dict]) -> dict:
    conf = [r for r in rows if r.get("label") == "conflict" and "flock" in r]
    ben = [r for r in rows if r.get("label") == "benign" and "flock" in r]

    def lead(r, mode, key):
        m = r[mode]
        return None if m[key] is None else r["none"]["t_cpa"] - m[key]

    def bank(r, mode):
        b = list(r[mode]["max_bank"].values())
        return max(b) if b else 0.0

    out = {"n_conflict": len(conf), "n_benign": len(ben), "modes": {}}
    for mode in MODES:
        real = mode != "none"
        leads = [lead(r, mode, "t_detect") for r in conf] if real else []
        alert_leads = [lead(r, mode, "t_alert") for r in conf] if real else []
        ticks = [r[mode]["tick_ms_mean"] for r in rows if mode in r and r[mode]["tick_ms_mean"]] if real else []
        out["modes"][mode] = {
            "nmac": sum(r[mode]["nmac"] for r in conf),
            "nmac_rate": sum(r[mode]["nmac"] for r in conf) / max(len(conf), 1),
            "median_min_h_ft": statistics.median(r[mode]["min_h_ft"] for r in conf) if conf else None,
            "detect_leads": [x for x in leads if x is not None],
            "alert_leads": [x for x in alert_leads if x is not None],
            "banks": [bank(r, mode) for r in conf if r[mode]["max_bank"]] if real else [],
            "missed_detect": sum(1 for x in leads if x is None),
            "nuisance_alert_rate": (sum(r[mode]["alerted"] for r in ben) / max(len(ben), 1)) if real else 0.0,
            "nuisance_maneuver_rate": (sum(bool(r[mode]["maneuvered"]) for r in ben) / max(len(ben), 1)) if real else 0.0,
            "no_solution": sum(r[mode]["no_solution"] for r in conf) if real else 0,
            "tick_ms_mean": statistics.mean(ticks) if ticks else 0.0,
            "tick_ms_max": max((r[mode]["tick_ms_max"] for r in rows if mode in r), default=0.0) if real else 0.0,
        }
    return out


def plot(summary: dict, path: str, n_per_kind: int, loss: float, latency: float) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    C = {"none": "#8a8a8a", "baseline": "#c0504d", "flock_nosq": "#7fa8d6", "flock": "#2f6fb5"}
    L = {"none": "no avoidance", "baseline": "straight-line\n+ fixed R30", "flock_nosq": "ARC\n(pilots ignore\nsequencing)", "flock": "ARC"}
    m = summary["modes"]
    sel = [k for k in MODES if k != "none"]
    fig, ax = plt.subplots(1, 4, figsize=(18, 4.9))

    ax[0].bar([L[k] for k in MODES], [m[k]["nmac_rate"] * 100 for k in MODES], color=[C[k] for k in MODES])
    for i, k in enumerate(MODES):
        ax[0].text(i, m[k]["nmac_rate"] * 100 + 1.5, f"{m[k]['nmac']}/{summary['n_conflict']}", ha="center", fontsize=9)
    ax[0].set_ylabel("NMAC rate (%)")
    ax[0].set_title("Near mid-air collisions\n(encounters that are NMACs with no avoidance)", fontsize=10)
    ax[0].set_ylim(0, 112)

    data = [m[k]["detect_leads"] or [0.0] for k in sel]
    bp = ax[1].boxplot(data, patch_artist=True, tick_labels=[L[k] for k in sel], widths=0.55)
    for patch, k in zip(bp["boxes"], sel):
        patch.set_facecolor(C[k])
        patch.set_alpha(0.8)
    ax[1].set_ylabel("seconds before closest approach")
    ax[1].set_title("Warning lead time\n(conflict first detected)", fontsize=10)

    bank_means = [statistics.mean(m[k]["banks"]) if m[k]["banks"] else 0.0 for k in sel]
    ax[2].bar([L[k] for k in sel], bank_means, color=[C[k] for k in sel])
    for i, v in enumerate(bank_means):
        ax[2].text(i, v + 0.5, f"{v:.0f}�", ha="center", fontsize=9)
    ax[2].set_ylabel("mean max bank used (deg)")
    ax[2].set_title("Maneuver severity\n(over aircraft that maneuvered)", fontsize=10)

    x = list(range(len(sel)))
    ax[3].bar([i - 0.2 for i in x], [m[k]["nuisance_alert_rate"] * 100 for k in sel], 0.4, color=[C[k] for k in sel],
              label="alert (>= TRAFFIC)")
    ax[3].bar([i + 0.2 for i in x], [m[k]["nuisance_maneuver_rate"] * 100 for k in sel], 0.4, color=[C[k] for k in sel],
              alpha=0.45, hatch="//", label="maneuver")
    ax[3].set_xticks(x)
    ax[3].set_xticklabels([L[k] for k in sel])
    ax[3].set_ylabel("% of benign encounters")
    ax[3].set_title(f"Nuisance (n={summary['n_benign']} benign)\nsolid = alert, hatched = maneuver", fontsize=10)

    for a in ax:
        a.spines[["top", "right"]].set_visible(False)
        a.tick_params(axis="x", labelsize=7.5)
    fig.suptitle(f"ARC vs straight-line baseline - KDVT 25L pattern encounters: {summary['n_conflict']} conflict / "
                 f"{summary['n_benign']} benign, loss {int(loss*100)}%, latency {latency}s, pilots: 5 s reaction, 70% comply (simulation)",
                 fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fig.savefig(path, dpi=140)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30, help="encounters per kind")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--loss", type=float, default=0.1)
    ap.add_argument("--latency", type=float, default=0.3)
    ap.add_argument("--kinds", default=",".join(KINDS))
    ap.add_argument("--out", default=os.path.join(_REPO, "harness", "out", "flock_vs_baseline.png"))
    a = ap.parse_args()
    kinds = a.kinds.split(",")
    jobs = [(k, 1000 * i + s, a.loss, a.latency, ("none", "baseline", "flock_nosq", "flock")) for i, k in enumerate(kinds) for s in range(a.n)]
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        rows = list(ex.map(run_one, jobs, chunksize=2))
    s = summarise(rows)
    print(f"{len(jobs)} encounters in {time.time() - t0:.0f}s: {s['n_conflict']} true-conflict, {s['n_benign']} benign "
          f"({len(jobs) - s['n_conflict'] - s['n_benign']} ambiguous, not scored)")
    by_kind: dict = {}
    for r in rows:
        by_kind.setdefault(r["kind"], {}).setdefault(r.get("label", "?"), 0)
        by_kind[r["kind"]][r.get("label", "?")] += 1
    for k, v in by_kind.items():
        print(f"  {k:22s} {v}")
    for mode, d in s["modes"].items():
        if mode == "none":
            print(f"none      NMAC {d['nmac']}/{s['n_conflict']} ({d['nmac_rate']*100:.0f}%)")
            continue
        dl = d["detect_leads"]
        print(f"{mode:9s} NMAC {d['nmac']}/{s['n_conflict']} ({d['nmac_rate']*100:.0f}%)  median min-sep {d['median_min_h_ft']:.0f} ft  "
              f"detect-lead median {statistics.median(dl) if dl else float('nan'):.0f}s (missed {d['missed_detect']})  "
              f"bank mean {statistics.mean(d['banks']) if d['banks'] else 0:.0f} deg  nuisance alerts {d['nuisance_alert_rate']*100:.0f}% "
              f"maneuvers {d['nuisance_maneuver_rate']*100:.0f}%  NO_SOLUTION {d['no_solution']}  tick {d['tick_ms_mean']:.2f}/{d['tick_ms_max']:.1f} ms")
    plot(s, a.out, a.n, a.loss, a.latency)
    with open(a.out.replace(".png", ".json"), "w") as fh:
        json.dump({"summary": s, "rows": rows}, fh, default=float)
    print("chart ->", a.out)


if __name__ == "__main__":
    main()
