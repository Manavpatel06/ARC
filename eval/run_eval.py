"""
eval/run_eval.py - FLOCK verification evaluation against ground truth (what judges will ask).

    python eval/run_eval.py                 # 4 seeds per attack, ablation on 2 seeds -> eval/out/report.md + charts
    python eval/run_eval.py --seeds 8

Every run is the full simulated loop (eval/sim.py): world physics + live traffic + world/sensors.py + one
onboard unit, an attack started at t = 30 s on judge A, 150 s total, verdicts once a second.
  detection rate     attack targets that reached SUSPECT at least once
  time to detect     first SUSPECT - attack start (per attack target)
  false-alarm rate   real aircraft that were ever SUSPECT; and SUSPECT share of real target-seconds
  ROC                spoof score = 100 - trust, per target-second after the attack starts, threshold swept
  ablation           the same with each check removed: why fusion beats any single check
Banners: jamming -> "traffic picture degraded", own GPS spoofing -> "own position uncertain" (time to raise,
and false banners in every other run).
"""
from __future__ import annotations
import argparse, json, os, sys, time
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from eval.sim import run                      # noqa: E402
from verify.checks import CHECKS              # noqa: E402

OUT = os.path.join(ROOT, "eval", "out")
TARGET_ATTACKS = ["ghost", "flock", "drift", "replay", "masquerade"]
# the hard cases: no TCAS help - far beyond TCAS range, or our own TCAS unavailable
HARD = {"ghost beyond TCAS (14 NM)": ("ghost", {"ahead_nm": 14.0}, False),
        "flock beyond TCAS (14 NM)": ("flock", {"ahead_nm": 14.0}, False),
        "ghost, own TCAS off": ("ghost", None, True),
        "masquerade, own TCAS off": ("masquerade", None, True),
        "quiet, own TCAS off": (None, None, True)}
BANNER_ATTACKS = {"jamming": "TRAFFIC PICTURE DEGRADED", "gps": "OWN POSITION UNCERTAIN"}
ATTACK_AT, SECS = 30.0, 150.0

def score_run(r: dict) -> dict:
    rows = [x for x in r["rows"] if x["label"] != "gone"]
    fake = [x for x in rows if x["label"] != "real"]
    real = [x for x in rows if x["label"] == "real"]
    first = {}
    for x in fake:
        if x["state"] == "SUSPECT":
            first.setdefault(x["icao"], x["t"] - ATTACK_AT)
    fake_ids = {x["icao"] for x in fake}
    real_ids = {x["icao"] for x in real}
    real_suspect_ids = {x["icao"] for x in real if x["state"] == "SUSPECT"}
    after = [x for x in rows if x["t"] >= ATTACK_AT]
    banner_t = {}
    for t, bn in r["banners"]:
        for key in BANNER_ATTACKS.values():
            if any(key in b for b in bn):
                banner_t.setdefault(key, t)
    return {"attack": r["attack"], "seed": r["seed"], "fake_ids": len(fake_ids), "detected": len(first),
            "ttd": sorted(first.values()), "real_ids": len(real_ids), "real_suspect_ids": len(real_suspect_ids),
            "real_rows": len(real), "real_suspect_rows": sum(1 for x in real if x["state"] == "SUSPECT"),
            "real_verified_rows": sum(1 for x in real if x["state"] == "VERIFIED"),
            "roc": [(100 - x["trust"], x["label"] != "real") for x in after], "banner_t": banner_t}

def summarize(scores: list[dict]) -> dict:
    s = defaultdict(float)
    ttd = []
    for x in scores:
        for k in ("fake_ids", "detected", "real_ids", "real_suspect_ids", "real_rows", "real_suspect_rows", "real_verified_rows"):
            s[k] += x[k]
        ttd += x["ttd"]
    ttd.sort()
    return {"detection": s["detected"] / s["fake_ids"] if s["fake_ids"] else None,
            "false_alarm_aircraft": s["real_suspect_ids"] / s["real_ids"] if s["real_ids"] else 0.0,
            "false_alarm_time": s["real_suspect_rows"] / s["real_rows"] if s["real_rows"] else 0.0,
            "real_verified_time": s["real_verified_rows"] / s["real_rows"] if s["real_rows"] else 0.0,
            "ttd_median": ttd[len(ttd) // 2] if ttd else None, "ttd_max": ttd[-1] if ttd else None,
            "n_fake": int(s["fake_ids"]), "n_real": int(s["real_ids"])}

def roc(points: list[tuple]) -> list[tuple]:
    pos = sum(1 for _, f in points if f)
    neg = len(points) - pos
    out = []
    for thr in range(0, 102, 2):
        tp = sum(1 for sc, f in points if f and sc >= thr)
        fp = sum(1 for sc, f in points if not f and sc >= thr)
        out.append((thr, tp / pos if pos else 0.0, fp / neg if neg else 0.0))
    return out

def auc(curve):
    pts = sorted((fpr, tpr) for _, tpr, fpr in curve)
    return sum((b[0] - a[0]) * (a[1] + b[1]) / 2 for a, b in zip(pts, pts[1:]))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=4)
    ap.add_argument("--ablation-seeds", type=int, default=2)
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    t0 = time.time()
    seeds = list(range(1, a.seeds + 1))
    per_attack, all_scores = {}, []
    for atk in [None] + TARGET_ATTACKS + list(BANNER_ATTACKS):
        sc = [score_run(run(atk, seed=s, secs=SECS, attack_at=ATTACK_AT)) for s in seeds]
        per_attack[atk or "none"] = sc
        all_scores += sc
        print(f"  {atk or 'none':11s} {json.dumps({k: (round(v, 3) if isinstance(v, float) else v) for k, v in summarize(sc).items()})}", flush=True)
    for name, (atk, params, toff) in HARD.items():
        sc = [score_run(run(atk, seed=s, secs=SECS, attack_at=ATTACK_AT, params=params, tcas_off=toff)) for s in seeds]
        for x in sc:
            x["attack"] = atk
        per_attack[name] = sc
        all_scores += sc
    curve = roc([p for x in all_scores if x["attack"] in TARGET_ATTACKS or x["attack"] is None for p in x["roc"]])
    for name in HARD:
        print(f"  {name:26s} {json.dumps({k: (round(v, 3) if isinstance(v, float) else v) for k, v in summarize(per_attack[name]).items()})}", flush=True)

    # ablation: drop one check at a time (and keep only TCAS), target attacks + quiet runs
    abl = {}
    configs = {"all checks": CHECKS}
    for name in CHECKS:
        configs[f"without {name}"] = {k: v for k, v in CHECKS.items() if k != name}
    configs["TCAS only"] = {"tcas_consistency": CHECKS["tcas_consistency"]}
    configs["no TCAS"] = {k: v for k, v in CHECKS.items() if k != "tcas_consistency"}
    cases = [(atk, None, False) for atk in TARGET_ATTACKS + [None]] + list(HARD.values())
    for label, checks in configs.items():
        sc = [score_run(run(atk, seed=s, secs=SECS, attack_at=ATTACK_AT, checks=checks, params=p, tcas_off=toff))
              for atk, p, toff in cases for s in range(1, a.ablation_seeds + 1)]
        abl[label] = summarize(sc)
        print(f"  ablation {label:28s} det {abl[label]['detection']:.2f}  fa {abl[label]['false_alarm_aircraft']:.3f}  "
              f"ttd {abl[label]['ttd_median']}", flush=True)
    write_report(per_attack, curve, abl, seeds, time.time() - t0)

def write_report(per_attack, curve, abl, seeds, secs):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    summ = {k: summarize(v) for k, v in per_attack.items()}
    # ROC
    fig, ax = plt.subplots(figsize=(4.6, 4.2))
    ax.plot([c[2] for c in curve], [c[1] for c in curve], marker=".", color="#d33")
    ax.plot([0, 1], [0, 1], ls="--", color="#999", lw=0.8)
    for thr, tpr, fpr in curve:
        if thr == 70:
            ax.annotate("SUSPECT (trust <= 30)", (fpr, tpr), textcoords="offset points", xytext=(10, -14), fontsize=8)
            ax.plot([fpr], [tpr], "o", color="#111")
    ax.set_xlabel("false positive rate (real target-seconds)"); ax.set_ylabel("true positive rate (attack target-seconds)")
    ax.set_title(f"ROC - spoof score = 100 - trust (AUC {auc(curve):.3f})", fontsize=9)
    fig.tight_layout(); fig.savefig(os.path.join(OUT, "roc.png"), dpi=130); plt.close(fig)
    # time to detect
    fig, ax = plt.subplots(figsize=(8.0, 3.4))
    names = [k for k in summ if (k in TARGET_ATTACKS or k in HARD) and summ[k]["ttd_median"] is not None]
    short = [n.replace(" beyond TCAS (14 NM)", "\n>TCAS range").replace(", own TCAS off", "\nno TCAS") for n in names]
    ax.bar(short, [summ[k]["ttd_median"] or 0 for k in names], color="#d33", label="median")
    ax.scatter(short, [summ[k]["ttd_max"] or 0 for k in names], color="#111", zorder=3, label="worst")
    ax.tick_params(axis="x", labelsize=7)
    ax.set_ylabel("seconds after attack start"); ax.set_title("Time to SUSPECT", fontsize=9); ax.legend(fontsize=8)
    fig.tight_layout(); fig.savefig(os.path.join(OUT, "time_to_detect.png"), dpi=130); plt.close(fig)
    # ablation
    fig, ax = plt.subplots(figsize=(7.5, 3.6))
    labels = list(abl)
    ax.barh(labels, [abl[k]["detection"] for k in labels], color="#36c")
    for i, k in enumerate(labels):
        ax.text(abl[k]["detection"] + 0.01, i, f"fa {abl[k]['false_alarm_aircraft']:.2f}", va="center", fontsize=7)
    ax.set_xlim(0, 1.15); ax.invert_yaxis(); ax.set_xlabel("detection rate (attack aircraft reaching SUSPECT)")
    ax.set_title("Ablation - remove one check at a time", fontsize=9)
    fig.tight_layout(); fig.savefig(os.path.join(OUT, "ablation.png"), dpi=130); plt.close(fig)

    def pct(v):
        return "–" if v is None else f"{100 * v:.0f} %"
    L = ["# FLOCK verification - evaluation", "",
         f"`python eval/run_eval.py --seeds {len(seeds)}` · {len(seeds)} seeds per attack · attack at t = {ATTACK_AT:.0f} s · "
         f"{SECS:.0f} s per run · live KDVT traffic (5-8 AI aircraft, runway 07) · judge A's onboard unit · {secs:.0f} s wall time", "",
         "Simulated sensors (world/sensors.py) and attacks (world/attacks.py); ground truth never reaches the unit. "
         "Numbers describe this simulation, not certified performance - see docs/limitations.md.", "",
         "## Per attack", "",
         "| attack | attack aircraft | detected | median / worst time to SUSPECT | real aircraft ever SUSPECT | real target-time SUSPECT | real target-time VERIFIED |",
         "|---|---|---|---|---|---|---|"]
    for k, v in summ.items():
        if k in BANNER_ATTACKS:
            continue
        ttd = "–" if v["ttd_median"] is None else f"{v['ttd_median']:.0f} s / {v['ttd_max']:.0f} s"
        L.append(f"| {k} | {v['n_fake']} | {pct(v['detection'])} | {ttd} | {pct(v['false_alarm_aircraft'])} of {v['n_real']} | "
                 f"{pct(v['false_alarm_time'])} | {pct(v['real_verified_time'])} |")
    L += ["", "Replay re-broadcasts positions 60 s old, so it only starts transmitting ~30 s after the attack is switched "
          "on (t = 60 s): it is SUSPECT ~2 s after its first message. Without TCAS a lone ghost waits out the 20 s "
          "Mode S listening window; a flock is caught sooner by its shared transmitter."]
    L += ["", "## Whole-picture attacks (banners)", "", "| attack | banner | raised (median after start) | real aircraft ever SUSPECT |", "|---|---|---|---|"]
    for k, key in BANNER_ATTACKS.items():
        ts = [x["banner_t"].get(key) for x in per_attack[k]]
        got = [t - ATTACK_AT for t in ts if t is not None]
        L.append(f"| {k} | {key} | {len(got)}/{len(ts)} runs, {sorted(got)[len(got) // 2]:.0f} s | {pct(summarize(per_attack[k])['false_alarm_aircraft'])} |"
                 if got else f"| {k} | {key} | 0/{len(ts)} runs | – |")
    false_banners = sum(1 for k, v in per_attack.items() if k not in BANNER_ATTACKS for x in v if x["banner_t"])
    L += ["", f"False banners in the {sum(len(v) for k, v in per_attack.items() if k not in BANNER_ATTACKS)} other runs: {false_banners}.", "",
          "## ROC", "", f"![ROC](roc.png)", "", f"AUC {auc(curve):.3f} over every target-second after the attack starts "
          "(attack aircraft vs real aircraft). The operating point is SUSPECT = trust ≤ 30, plus the guardrails "
          "(a TCAS-confirmed target is never SUSPECT; SUSPECT needs one strong check).", "",
          "## Time to detect", "", "![time to detect](time_to_detect.png)", "",
          "Drift is slow by design: the attack walks the position 12 m/s, and FLOCK flags it once the offset exceeds "
          "what TCAS noise can explain (yellow first, then red).", "",
          "## Ablation - why fusion beats any single check", "", "![ablation](ablation.png)", "",
          "| configuration | detection | real aircraft ever SUSPECT | median time to detect |", "|---|---|---|---|"]
    for k, v in abl.items():
        ttd = "–" if v["ttd_median"] is None else f"{v['ttd_median']:.0f} s"
        L.append(f"| {k} | {pct(v['detection'])} | {pct(v['false_alarm_aircraft'])} | {ttd} |")
    L += ["", "Ablation runs: ghost, flock, drift, replay, masquerade, a quiet run and the five hard cases "
          "(beyond TCAS range / own TCAS off), fewer seeds than above. 'TCAS only' does well inside TCAS range and "
          "fails the hard cases; the other checks carry those."]
    open(os.path.join(OUT, "report.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")
    json.dump({"per_attack": summ, "ablation": abl, "roc": curve}, open(os.path.join(OUT, "results.json"), "w"), indent=1)
    print(f"wrote {os.path.join(OUT, 'report.md')}")

if __name__ == "__main__":
    main()
