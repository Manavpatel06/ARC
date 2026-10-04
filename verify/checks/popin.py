"""
Pop-in (Lane B's AirWitness): real aircraft come into receiver range from far away (1090 is heard 100+ NM);
one that is first heard already close and airborne is odd. Weak, and ignored while our own receiver has just
started listening (everything "appears" at power-on). Only ever spoof evidence.
"""
from __future__ import annotations

from verify.checks import CheckResult
from verify.tracks import rng_brg

def check(track, ctx, cfg: dict) -> CheckResult:
    c = cfg["popin"]
    if ctx.own.t < -1e8 or not track.pos:
        return CheckResult.none("no position")
    if track.first_t - ctx.t0 < c["warmup_s"]:
        return CheckResult.none("heard since our receiver started")
    t, lat, lon, alt, _ = track.pos[0]
    rng, _ = rng_brg(ctx.own.lat, ctx.own.lon, lat, lon)
    if rng < c["near_nm"] * 1852 and (alt or 0) > c["min_alt_ft"]:
        return CheckResult(c["score"], c["conf"], f"appeared {rng / 1852:.1f} NM away, already airborne",
                           {"first_range_m": round(rng)})
    return CheckResult.none("came into range normally")
