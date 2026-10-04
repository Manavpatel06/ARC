"""
1030/1090 timing correlation (spec check 3).

A rotating ground radar interrogates on 1030 MHz along a narrow beam; a transponder replies on 1090 MHz
only while the beam points at it. We hear the beam ourselves when it sweeps us (INTERROGATION_1030), which
gives the radar's phase; the radar site and rotation period are public (config). So for every Mode S reply
(DF4/5/20/21) we can say where the beam was pointing when it was sent - and that must be the bearing, from
the radar, of where the target CLAIMS to be. A target whose ADS-B position has been moved (drift / offset
spoofing, or a masquerader with a real aircraft's address) replies from the wrong bearing.
Agreement is physical evidence (like TCAS); None without a radar fix or replies.
"""
from __future__ import annotations

from verify.checks import CheckResult
from verify.tracks import angdiff, rng_brg

NM = 1852.0

def beam_azimuth(t: float, ctx, cfg: dict):
    """Where the radar beam pointed at time t, from the last time it swept us. None without a recent fix."""
    c = cfg["timing_1030"]
    tm, own = ctx.tm, ctx.own
    if tm is None or not tm.interrogations or own.t < -1e8:
        return None
    t_hit = tm.interrogations[-1][0]
    if abs(t - t_hit) > c["fix_valid_rotations"] * c["period_s"]:
        return None
    _, az_own = rng_brg(c["site_lat"], c["site_lon"], own.lat, own.lon)
    return (az_own + 360.0 * (t - t_hit) / c["period_s"]) % 360.0

def check(track, ctx, cfg: dict) -> CheckResult:
    c = cfg["timing_1030"]
    replies = [r for r in track.replies if ctx.t - r[0] <= c["window_s"] and r[1] in (4, 5, 20, 21)]
    if not replies:
        return CheckResult.none("no ground-radar replies")
    errs = []
    for t, _, _ in replies:
        az = beam_azimuth(t, ctx, cfg)
        claim = track.claimed_at(t)
        if az is None or claim is None:
            continue
        rng, brg = rng_brg(c["site_lat"], c["site_lon"], claim[0], claim[1])
        if rng < c["min_site_range_nm"] * NM:
            continue                                       # too close to the radar: bearing ill-defined
        errs.append(angdiff(az, brg))
    if len(errs) < c["min_replies"]:
        return CheckResult.none("waiting for a radar fix" if replies else "no replies")
    errs.sort()
    med = errs[len(errs) // 2]
    tol = c["tol_deg"] * (2.0 if ctx.own_uncertain else 1.0)
    ev = {"median_err_deg": round(med, 1), "replies": len(errs)}
    if med > tol:
        return CheckResult(c["bad_score"], c["bad_conf"],
                           f"its radar replies come from {med:.0f} deg away from where its ADS-B says it is", ev)
    return CheckResult(c["ok_score"], c["ok_conf"], "radar replies time-match its claimed position", ev)
