"""
RSSI vs claimed distance (spec check 4; method from Lane B's AirWitness _rssi_trend / _rf_location).

Received power should follow free-space loss from where the target CLAIMS to be. Two views:
  level  median(measured - expected) over the window; a ghost transmitted from close by but claiming to be
         far away arrives far too strong (or vice versa). Transponder power varies (~125-500 W), so the
         tolerance is wide.
  trend  as the claimed range changes, measured power should move with it (correlation); a ghost whose
         claim closes while its transmitter stays put does not.
Signal strength is noisy (multipath, antenna shadowing): low weight, and agreement is only weak support.
"""
from __future__ import annotations
import math

from verify.checks import CheckResult
from verify.tracks import FT, rng_brg

def expected_dbm(rng_m: float, cfg: dict) -> float:
    c = cfg["rssi"]
    d_km = max(rng_m, 30.0) / 1000.0
    return c["tx_dbm"] - c["cable_db"] - (20 * math.log10(d_km) + 20 * math.log10(1090.0) + 32.44)

def check(track, ctx, cfg: dict) -> CheckResult:
    c = cfg["rssi"]
    own = ctx.own
    if own.t < -1e8:
        return CheckResult.none("own position unknown")
    pts = [p for p in track.pos if ctx.t - p[0] <= c["window_s"] and p[4] is not None and p[3] is not None]
    if len(pts) < c["min_samples"]:
        return CheckResult.none(f"only {len(pts)} signal samples")
    exp, obs = [], []
    for t, lat, lon, alt, rssi in pts:
        h, _ = rng_brg(own.lat, own.lon, lat, lon)
        d = math.hypot(h, (alt - own.alt_ft) * FT)
        exp.append(expected_dbm(d, cfg))
        obs.append(rssi)
    res = sorted(o - e for o, e in zip(obs, exp))
    med = res[len(res) // 2]
    tol = c["level_tol_db"] * (1.5 if ctx.own_uncertain else 1.0)
    ev = {"median_residual_db": round(med, 1), "samples": len(res)}
    if abs(med) > tol:
        word = "stronger" if med > 0 else "weaker"
        return CheckResult(c["bad_score"], c["bad_conf"],
                           f"signal {abs(med):.0f} dB {word} than a transmitter where it claims to be", ev)
    if max(exp) - min(exp) >= c["trend_min_db"]:
        me, mo = sum(exp) / len(exp), sum(obs) / len(obs)
        cov = sum((a - me) * (b - mo) for a, b in zip(exp, obs))
        sd = math.sqrt(sum((a - me) ** 2 for a in exp) * sum((b - mo) ** 2 for b in obs)) or 1.0
        corr = cov / sd
        ev["trend_corr"] = round(corr, 2)
        if corr < c["trend_bad_corr"]:
            return CheckResult(c["trend_bad_score"], c["trend_bad_conf"],
                               f"signal does not follow its claimed range (corr {corr:.2f})", ev)
    return CheckResult(c["ok_score"], c["ok_conf"], f"signal strength fits its claimed range ({med:+.0f} dB)", ev)
