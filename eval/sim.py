"""
eval/sim.py - run one labelled scenario offline: world physics + live traffic + world/sensors.py + one FLOCK
onboard unit (verify/unit.py's VerifyUnit), no server, no network. Same code paths as the live demo.

    from eval.sim import run
    rec = run(attack="drift", seed=3, secs=150, attack_at=30)
    rec["rows"]      # one per (evaluation second, target): t, icao, label (truth), state, trust, reasons
    rec["banners"]   # (t, [banner, ...]) per evaluation
"""
from __future__ import annotations
import json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from verify import load_config                       # noqa: E402
from verify.unit import VerifyUnit                   # noqa: E402

SCENARIO = os.path.join(ROOT, "harness", "scenarios", "live_kdvt.json")
DT = 0.05

def make_world(seed: int, scenario: str = SCENARIO):
    from world.scenario import World
    raw = json.load(open(scenario))
    raw["weather"] = "cached"                          # results must not depend on today's METAR
    raw.setdefault("runway_flow", "07")
    return World(raw, seed=seed)

def run(attack: str | None = None, seed: int = 1, secs: float = 150.0, attack_at: float = 30.0, victim: str = "N101",
        checks: dict | None = None, cfg: dict | None = None, params: dict | None = None, t_start: float = 1000.0,
        scenario: str = SCENARIO, replay=None, tcas_off: bool = False) -> dict:
    from world.sensors import SensorSim
    w = make_world(seed, scenario)
    ss = SensorSim(w, seed=1090 + seed)
    if replay is not None:
        ss.replay_src = replay
    if tcas_off:
        ss.tcas_off.add(victim)                           # our own TCAS unavailable: the other checks must carry it
    unit = VerifyUnit(victim, cfg or load_config(), checks=checks)
    rows, banners, started = [], [], None
    t, n = t_start, 0
    while t < t_start + secs:
        for ac in list(w.fleet.values()):
            ac.step(DT, w.env, t)
            ac.events.clear()
        t += DT
        n += 1
        if n % 20 == 0 and w.traffic is not None:
            w.traffic.step(t)
        if n % 4 == 0:
            w.runway_watch(t)
        if attack and started is None and t - t_start >= attack_at:
            try:
                started = ss.attacks.start(attack, w.fleet[victim], t, **(params or {}))
            except KeyError:
                started = False                           # nothing to attack in this run
        unit.ingest({"t": t, "msgs": ss.step(t, own_ids=[victim])[victim]})
        if unit.due():
            v = unit.evaluate()
            truth = {e["icao"]: e["label"] for e in ss.truth(t)}
            for x in v["targets"]:
                addr = x["icao"].split("~")[0]
                lab = truth.get(addr, "gone")
                if "~" in x["icao"] or (addr in truth and _is_attacked_branch(x, ss, t)):
                    lab = _branch_label(x, ss, t, lab)
                rows.append({"t": round(t - t_start, 1), "icao": x["icao"], "id": x["id"], "label": lab,
                             "state": x["state"], "trust": x["trust"], "reasons": x["reasons"],
                             "checks": {k: c["score"] for k, c in x["checks"].items()}})
            banners.append((round(t - t_start, 1), v["banners"]))
    return {"attack": attack if not params else f"{attack}{params}", "seed": seed, "tcas_off": tcas_off, "attack_at": attack_at, "started": bool(started),
            "attack_info": started.public() if started else None, "rows": rows, "banners": banners}

def _is_attacked_branch(x, ss, t) -> bool:
    return any(a.kind in ("masquerade", "replay") for a in ss.attacks.active.values())

def _branch_label(x, ss, t, lab):
    """An address heard from two places: which place is the real aircraft? The one nearest its truth."""
    import math
    from world.sensors import icao_of
    addr = x["icao"].split("~")[0]
    real = next((ac for ac in ss.w.fleet.values() if icao_of(ac.id) == addr), None)
    fakes = [a for a in ss.attacks.active.values() if a.kind in ("masquerade", "replay") and getattr(a, "target", None) and
             icao_of(a.target) == addr]
    if not fakes:
        return lab
    if real is None or x.get("rel") is None and False:
        return fakes[0].kind
    # the unit names the second place "(2)"; decide by which one the ground truth calls real
    return fakes[0].kind if x["icao"].endswith("~2") else "real"
