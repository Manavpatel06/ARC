"""eval/ga_only.py - spoof detection with only what a plain ADS-B In receiver provides (no TCAS, no 1030 MHz). python eval/ga_only.py"""
import sys, json; import os; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))); sys.argv=["x"]
import eval.run_eval as E
GA = {k: v for k, v in E.CHECKS.items() if k not in ("tcas_consistency", "timing_1030")}
print("checks:", sorted(GA))
res = {}
for atk in E.TARGET_ATTACKS + [None]:
    sc = [E.score_run(E.run(atk, seed=s, secs=E.SECS, attack_at=E.ATTACK_AT, checks=GA, tcas_off=True)) for s in (1, 2, 3, 4)]
    res[atk or "none"] = E.summarize(sc)
    print(atk or "none", json.dumps({k: (round(v, 3) if isinstance(v, float) else v) for k, v in res[atk or "none"].items()}), flush=True)
allsc = []
json.dump(res, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "out", "ga_only_results.json"), "w"), indent=1)
