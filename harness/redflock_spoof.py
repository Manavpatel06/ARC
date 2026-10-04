"""
harness/redflock_spoof.py — RED FLOCK spoof ladder against the real node + AirWitness (Lane B).

Our aircraft N101 (autopilot-equipped) flies the 25L downwind.  N204 is a legitimate FLOCK aircraft (signed,
answers liveness challenges).  An attacker transmits from the ground 2.5 km south of the field and tries
progressively better spoofs of an aircraft head-on, 2.5 km ahead and closing:

  1  impossible motion   unsigned GHOST7 at 400 kt with position jumps          -> kinematics
  2  replay              records N204's real signed packets, re-sends them later -> replay window (N204 stays VERIFIED)
  3  fresh + plausible   unsigned GHOST7 flying a perfect head-on track          -> no signature, RF range 2.5 km off,
                                                                                    no peer witnesses
  4  hardest             signed with its OWN key (not registered), plausible     -> unknown key, no witnesses: can be
                         track, transmitter actually near the claimed position      tracked and warned about, never
                                                                                    coordinated with or acted on
  5  sybil               three ghosts from the one ground transmitter            -> same RF source

Pass = no MANEUVER_COMMIT to a ghost, no automatic TAKEOVER because of a ghost, and the ghost never VERIFIED.

    python harness/redflock_spoof.py            # table + the explanation the log panel shows for attempt 3
"""
from __future__ import annotations

import math
import os
import random
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from harness.rf_sim import measure
from node.airwitness import VERIFIED
from node.geometry import FT, KT, build_patterns, hvec, to_latlon
from node.node import Node

T0 = 1.0e9
SITE = (0.0, -2500.0, 0.0)        # attacker's antenna on the ground south of KDVT
ALT = 2500.0


def _state_env(frm, seq, t, x, y, trk, gs=90.0, auth="ok"):
    lat, lon = to_latlon(x, y)
    return {"msg": "STATE", "from": frm, "seq": seq, "t": t, "sig": "", "_rx_t": t, "_auth": auth,
            "body": {"lat": lat, "lon": lon, "alt_press_ft": ALT, "gs_kt": gs, "track_deg": trk, "vs_fpm": 0.0,
                     "leg": "UNKNOWN", "intent": "", "ap_equipped": True}}


def run_attempt(kind: str, seconds: float = 45.0) -> dict:
    pats = build_patterns()
    node = Node("N101", patterns=pats, record=False)
    node.aw.signed_radio = True
    node.aw.peer_evidence = lambda tid: (1.0, ["signed", "corroborated:2"] if tid == "N204"
                                         else ["not_heard_by:N204,N311", "no_corroboration"])
    pl = pats["25L"].place("DOWNWIND", 20.0, 90.0)
    hx, hy = hvec(pl["hdg_deg"])
    rng = random.Random(7)
    ghosts = ["GHOST7"] if kind != "sybil" else ["GHOST7", "GHOST8", "GHOST9"]
    recorded, seq = [], {g: 0 for g in ghosts + ["N204"]}
    out = {"commits_to_ghost": 0, "takeovers": 0, "levels": set(), "states": {}}
    for k in range(int(seconds * 10)):
        t = T0 + k * 0.1
        ox, oy = pl["x_m"] + hx * 46.3 * k * 0.1, pl["y_m"] + hy * 46.3 * k * 0.1
        lat, lon = to_latlon(ox, oy)
        own = {"type": "OWNSHIP", "ac_id": "N101", "t": t, "lat": lat, "lon": lon, "alt_msl_ft": ALT, "alt_press_ft": ALT,
               "agl_ft": 1022.0, "gs_kt": 90.0, "track_deg": pl["hdg_deg"], "hdg_deg": pl["hdg_deg"], "bank_deg": 0.0,
               "vs_fpm": 0.0, "ias_kt": 90.0, "ap_equipped": True, "stick_active": False, "flaps": 0}
        if k % 10 == 0:
            # the legitimate peer, well clear to the north, signed, answering challenges
            seq["N204"] += 1
            e = _state_env("N204", seq["N204"], t, ox - 3000.0, oy + 2200.0, (pl["hdg_deg"] + 180) % 360)
            recorded.append(dict(e))
            node.on_radio(e)
            for i, g in enumerate(ghosts):
                d = 2500.0 - 46.3 * 2 * k * 0.1
                gx, gy = ox + hx * d + 300.0 * i, oy + hy * d
                seq[g] += 1
                if kind == "impossible":
                    gx += rng.choice([-1, 1]) * 1500.0 * (k // 10 % 2)
                    e = _state_env(g, seq[g], t, gx, gy, (pl["hdg_deg"] + 180) % 360, gs=400.0, auth="unsigned")
                elif kind == "replay":
                    continue
                elif kind == "hardest":
                    e = _state_env(g, seq[g], t, gx, gy, (pl["hdg_deg"] + 180) % 360, auth="unknown_key")
                else:
                    e = _state_env(g, seq[g], t, gx, gy, (pl["hdg_deg"] + 180) % 360, auth="unsigned")
                node.on_radio(e)
                tx = (gx, gy, ALT * FT) if kind == "hardest" else SITE
                node.aw.ingest_rf(measure("N101", (ox, oy, ALT * FT), g, tx, t, kinds=("rssi", "bearing"), rng=rng,
                                          source_id="GROUND-TX" if kind == "sybil" else None))
            if kind == "replay" and k >= 150 and recorded:
                old = dict(recorded[rng.randrange(0, 10)])           # an old N204 packet, re-stamped to look fresh
                old["t"], old["_rx_t"] = t, t
                node.on_radio(old)
        frames, radios = node.tick(own)
        # answer N204's liveness challenges as the real N204 would
        for msg, body in radios:
            if msg == "HEARTBEAT" and "N204" in body.get("c", {}):
                seq["N204"] += 1
                node.on_radio({"msg": "HEARTBEAT", "from": "N204", "seq": seq["N204"], "t": t + 0.05, "sig": "",
                               "_rx_t": t + 0.05, "_auth": "ok", "body": {"alive": True, "r": {"N101": body["c"]["N204"]}}})
            if msg == "MANEUVER_COMMIT" and body.get("target", "").startswith("GHOST"):
                out["commits_to_ghost"] += 1
        for f in frames:
            if f["type"] == "COMMAND" and f["mode"] == "TAKEOVER":
                out["takeovers"] += 1
            if f["type"] == "ADVISORY" and str(f.get("target_id", "")).startswith("GHOST"):
                out["levels"].add(f["level"])
    now = T0 + seconds
    for g in ghosts + ["N204"]:
        out["states"][g] = node.aw.assess(g, now)
    return out


def main() -> None:
    attempts = [("impossible", "1 impossible motion"), ("replay", "2 replay of N204"), ("fresh", "3 fresh + plausible"),
                ("hardest", "4 own key, true position"), ("sybil", "5 sybil x3")]
    print(f"{'attempt':26s} {'ghost state':12s} {'score':>5s}  {'ghost advisories':22s} {'commits':>7s} {'takeover':>8s}  N204")
    ok_all = True
    explain = None
    for kind, label in attempts:
        r = run_attempt(kind)
        ghost = "N204" if kind == "replay" else "GHOST7"
        g = r["states"][ghost]
        n204 = r["states"]["N204"]
        blocked = r["commits_to_ghost"] == 0 and r["takeovers"] == 0 and (kind == "replay" or g.state != VERIFIED)
        ok = blocked and n204.state == VERIFIED
        ok_all &= ok
        lv = ",".join(sorted(r["levels"])) or "-"
        shown = f"{g.state} ({'replays dropped' if kind == 'replay' else ''})" if kind == "replay" else g.state
        print(f"{label:26s} {shown:12s} {g.score:5.2f}  {lv:22s} {r['commits_to_ghost']:7d} {r['takeovers']:8d}  "
              f"{n204.state}  {'PASS' if ok else 'FAIL'}")
        if kind == "fresh":
            explain = g.explain()
        if kind == "replay":
            print(f"{'':26s} N204 replay check: {n204.checks.get('replay')}")
    print("\n" + (explain or ""))
    print("\nRED FLOCK spoof ladder: " + ("all attempts blocked from automatic control" if ok_all else "FAILURES above"))
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
