"""
Kinematic plausibility of the ADS-B claims alone: no teleports, no impossible speed / climb / turn, and
each new position must follow from the previous one plus the velocity the target itself reported.
Plausible motion is only weak evidence (a careful spoofer moves plausibly); impossible motion is strong.
"""
from __future__ import annotations
import math

from verify.checks import CheckResult
from verify.tracks import KT, angdiff, enu

def check(track, ctx, cfg: dict) -> CheckResult:
    c = cfg["kinematics"]
    pts = [p for p in track.pos if ctx.t - p[0] <= c["window_s"]]
    if track.parent is not None and pts and pts[0][0] == track.pos[0][0] and track.parent.pos:
        # a branch of an address: it "comes from" where that address was just before - a jump shows up here
        par = [p for p in track.parent.pos if p[0] < pts[0][0]]
        if par and pts[0][0] - par[-1][0] <= 5.0:
            pts = [par[-1]] + pts
    if len(pts) < c["min_reports"]:
        return CheckResult.none(f"only {len(pts)} position reports so far")
    bad, worst = [], {}
    segs = []
    for (t0, la0, lo0, a0, _), (t1, la1, lo1, a1, _) in zip(pts, pts[1:]):
        dt = t1 - t0
        if dt <= 0.05:
            continue
        e, n = enu(la0, lo0, la1, lo1)
        d = math.hypot(e, n)
        gs = d / dt / KT
        segs.append((t0, t1, e, n, d, gs))
        if gs > c["max_gs_kt"]:
            bad.append(f"jumps {d:.0f} m in {dt:.1f} s ({gs:.0f} kt)")
            worst["gs_kt"] = round(gs)
        if a0 is not None and a1 is not None:
            vs = (a1 - a0) / dt * 60.0
            if abs(vs) > c["max_vs_fpm"]:
                bad.append(f"altitude changes {abs(vs):.0f} fpm")
                worst["vs_fpm"] = round(vs)
    for (a0, a1, ea, na, da, _), (b0, b1, eb, nb, db, _) in zip(segs, segs[1:]):
        if da > 30 and db > 30:
            mid_dt = max(0.1, ((b0 + b1) - (a0 + a1)) / 2.0)          # between segment midpoints (uneven gaps)
            rate = angdiff(math.degrees(math.atan2(ea, na)), math.degrees(math.atan2(eb, nb))) / mid_dt
            if rate > c["max_turn_dps"] * min(2.0, ctx.pos_scale):
                bad.append(f"turns {rate:.0f} deg/s")
                worst["turn_dps"] = round(rate, 1)
    # acceleration from its own velocity reports (Lane B AirWitness: 12 kt/s - a go-around still passes)
    vel_r = [v for v in track.vel if ctx.t - v[0] <= c["window_s"] and v[1] is not None]
    for (ta, ga, _, _), (tb, gb, _, _) in zip(vel_r, vel_r[1:]):
        if tb - ta >= 0.3 and abs(gb - ga) / (tb - ta) > c["max_accel_kt_s"]:
            bad.append(f"speed changes {abs(gb - ga) / (tb - ta):.0f} kt/s")
            worst["accel_kt_s"] = round(abs(gb - ga) / (tb - ta), 1)
            break
    # does each position follow from the previous one + the velocity it reported?
    vel = list(track.vel)
    for (t0, la0, lo0, _, _), (t1, la1, lo1, _, _) in zip(pts, pts[1:]):
        v = [x for x in vel if x[0] <= t0 + 0.6]
        if not v or t1 - t0 <= 0.05:
            continue
        _, gs, trk, _ = v[-1]
        if gs is None or trk is None:
            continue
        dt = t1 - t0
        pe, pn = gs * KT * math.sin(math.radians(trk)) * dt, gs * KT * math.cos(math.radians(trk)) * dt
        e, n = enu(la0, lo0, la1, lo1)
        res = math.hypot(e - pe, n - pn)
        if res > (c["max_pos_residual_m"] + c["residual_per_s_m"] * dt) * ctx.pos_scale:
            bad.append(f"position disagrees with its own velocity by {res:.0f} m")
            worst["residual_m"] = round(res)
    if bad:
        return CheckResult(c["bad_score"], c["bad_conf"], "impossible motion: " + bad[0], {"violations": bad, **worst})
    return CheckResult(c["ok_score"], c["ok_conf"], "moves like a real aircraft", {"reports": len(pts)})
