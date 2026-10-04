"""
Same-emitter detection (spec check 5; Lane B's AirWitness _sybil, adapted to a single passive receiver).

Several "different" aircraft sent by one transmitter arrive with the same signal strength at the same
moments, even though they claim to be kilometres apart - and their signal does not fit where they claim to
be. (Transponder-equipped aircraft also give TCAS bearings: several targets on one TCAS bearing with
identical range would be the same evidence; ghosts have no TCAS track, so RSSI carries it here.)
Only ever spoof evidence; never a positive.
"""
from __future__ import annotations
import math

from verify.checks import CheckResult
from verify.checks.rssi import expected_dbm
from verify.tracks import FT, rng_brg

def _series(tr, t, window):
    return [(p[0], p[1], p[2], p[3], p[4]) for p in tr.pos if t - p[0] <= window and p[4] is not None]

def _residual(own, p, cfg):
    h, _ = rng_brg(own.lat, own.lon, p[1], p[2])
    return p[4] - expected_dbm(math.hypot(h, ((p[3] or own.alt_ft) - own.alt_ft) * FT), cfg)

def check(track, ctx, cfg: dict) -> CheckResult:
    c = cfg["emitter_cluster"]
    own, tm = ctx.own, ctx.tm
    if tm is None or own.t < -1e8:
        return CheckResult.none("no other targets to compare")
    mine = _series(track, ctx.t, c["window_s"])
    if len(mine) < c["min_pairs"]:
        return CheckResult.none("too few signal samples")
    res_mine = sorted(_residual(own, p, cfg) for p in mine)
    if abs(res_mine[len(res_mine) // 2]) < c["misfit_db"]:
        return CheckResult.none("its signal fits its claim")            # nothing odd about this one
    twins = []
    for icao, other in tm.tracks.items():
        if other is track:
            continue
        theirs = _series(other, ctx.t, c["window_s"])
        pairs = [(a, b) for a in mine for b in theirs if abs(a[0] - b[0]) <= c["pair_dt_s"]]
        if len(pairs) < c["min_pairs"]:
            continue
        d = sorted(abs(a[4] - b[4]) for a, b in pairs)
        apart, _ = rng_brg(pairs[-1][0][1], pairs[-1][0][2], pairs[-1][1][1], pairs[-1][1][2])
        if d[len(d) // 2] <= c["same_db"] and apart >= c["min_apart_m"]:
            twins.append(other.callsign or icao.upper())
    if twins:
        return CheckResult(c["bad_score"], c["bad_conf"],
                           f"same transmitter as {', '.join(sorted(twins)[:3])} (identical signal, km apart)",
                           {"twins": sorted(twins)})
    return CheckResult.none("no look-alike signals")
