"""
Replay / duplicate detection (spec check 7; Lane B's AirWitness packet guard + duplicate identity, adapted to
unsigned ADS-B).

  one ICAO in two places   reports for one address that, moved on to the same instant, fall into two clusters
                           far apart (a masquerading or replaying transmitter next to the real aircraft)
  stale replay             the same (lat, lon, alt) report arriving again after other positions in between
                           (old recorded messages re-broadcast)
  going backwards          a new report that places it where it was a while ago against its own velocity
Only ever spoof evidence; None when nothing is wrong.
"""
from __future__ import annotations
import math

from verify.checks import CheckResult
from verify.tracks import KT, enu

def _retrace_lag(track, other):
    """If this track's recent positions retrace where `other` (same address) was earlier: the delay, s."""
    mine = list(track.pos)[-6:]
    if len(mine) < 4 or not other.pos:
        return None
    lags = []
    for t, lat, lon, _, _ in mine:
        best = None
        for ot, olat, olon, _, _ in other.pos:
            if t - ot < 5.0:
                continue
            d = math.hypot(*enu(lat, lon, olat, olon))
            if d < 80.0 and (best is None or d < best[0]):
                best = (d, t - ot)
        if best:
            lags.append(best[1])
    if len(lags) >= 4 and max(lags) - min(lags) < 3.0:
        return sum(lags) / len(lags)
    return None

def check(track, ctx, cfg: dict) -> CheckResult:
    c = cfg["replay"]
    twin = track.parent or track.branch                  # the same ICAO address heard from a second place
    if twin is not None and twin.pos and track.pos:
        lag = _retrace_lag(track, twin)
        if lag is not None:
            return CheckResult(c["bad_score"], c["bad_conf"],
                               f"replays positions its address reported {lag:.0f} s ago", {"lag_s": round(lag, 1)})
        a, b = track.claimed_at(ctx.t), twin.claimed_at(ctx.t)
        sep = math.hypot(*enu(a[0], a[1], b[0], b[1])) if a and b else 0.0
        return CheckResult(c["shared_score"], c["shared_conf"],
                           f"its ICAO address is also in use {sep / 1852:.1f} NM away", {"sep_m": round(sep)})
    pts = [p for p in track.pos if ctx.t - p[0] <= c["window_s"]]
    if len(pts) < 4:
        return CheckResult.none("too few reports")
    # project every recent report to "now" with the latest claimed velocity
    vel = track.vel[-1] if track.vel else None
    gs, trk = (vel[1] or 0.0, vel[2] or 0.0) if vel else (0.0, 0.0)
    lat0, lon0 = pts[-1][1], pts[-1][2]
    proj = []
    for t, lat, lon, _, _ in pts:
        e, n = enu(lat0, lon0, lat, lon)
        dt = ctx.t - t
        proj.append((e + gs * KT * dt * math.sin(math.radians(trk)), n + gs * KT * dt * math.cos(math.radians(trk))))
    ref = proj[-1]
    far = [p for p in proj if math.hypot(p[0] - ref[0], p[1] - ref[1]) > c["dup_id_m"] * ctx.pos_scale]
    near = len(proj) - len(far)
    if len(far) >= 2 and near >= 2:
        d = max(math.hypot(p[0] - ref[0], p[1] - ref[1]) for p in far)
        return CheckResult(c["bad_score"], c["bad_conf"], f"one ICAO address in two places {d / 1852:.1f} NM apart",
                           {"far": len(far), "near": near, "sep_m": round(d)})
    seen = {}
    for i, (t, lat, lon, alt, _) in enumerate(pts):
        key = (round(lat, 5), round(lon, 5), alt)
        if key in seen and i - seen[key] >= 2 and gs > 30:
            return CheckResult(c["bad_score"], c["bad_conf"], "old position reports re-broadcast (replay)",
                               {"repeat_after": i - seen[key]})
        seen.setdefault(key, i)
    return CheckResult.none("no duplicates")
