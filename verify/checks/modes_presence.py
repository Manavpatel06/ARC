"""
Mode S reply presence: a real transponder answers ground radar interrogations (DF4/5/20/21) and sends
acquisition squitters (DF11) - not only ADS-B extended squitters. A box that only broadcasts ADS-B never does.
Only held against a target if the radar was active and the target claims to be close enough for us to
have heard its replies; otherwise None.
"""
from __future__ import annotations

from verify.checks import CheckResult
from verify.tracks import rng_brg

NM = 1852.0

def check(track, ctx, cfg: dict) -> CheckResult:
    c = cfg["modes"]
    rec = [r for r in track.replies if ctx.t - r[0] <= c["window_s"]]
    if rec:
        dfs = sorted({r[1] for r in rec if r[1] is not None})
        radar = sum(1 for r in rec if r[1] in (4, 5, 20, 21))
        why = f"answers ground radar ({radar} replies)" if radar else f"sends Mode S squitters (DF{'/'.join(map(str, dfs))})"
        return CheckResult(c["present_score"], c["present_conf"], why, {"replies": len(rec), "dfs": dfs})
    if ctx.t - ctx.radar_t > c["radar_active_s"]:
        return CheckResult.none("no ground radar heard (nothing to compare)")
    if ctx.t - track.first_t < c["window_s"]:
        return CheckResult.none("listening for its Mode S replies")
    claim = track.claimed_at(ctx.t)
    if claim is None:
        return CheckResult.none("no position claim")
    own = ctx.own
    if own.t > -1e8 and rng_brg(own.lat, own.lon, claim[0], claim[1])[0] > c["reply_range_nm"] * NM:
        return CheckResult.none("too far to hear its replies")
    return CheckResult(c["absent_score"], c["absent_conf"],
                       f"never answers ground radar (0 Mode S replies in {c['window_s']:.0f} s)", {"replies": 0})
