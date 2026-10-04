"""
TCAS range / bearing consistency - the strongest check.

Our own TCAS measures range from the round-trip time of the target's transponder replies (independent of
anything the target claims) and a rough bearing. A ghost that only broadcasts ADS-B never answers TCAS.
  ADS-B claim and TCAS track agree              -> strong evidence it is real   ("tcas_confirmed")
  they disagree (range / bearing / altitude)    -> strong evidence of spoofing
  no TCAS track where it claims to be, in range -> spoofing evidence (it should have answered)
  claims to be beyond TCAS range / TCAS off     -> None (no data, never held against it)
Bearing tolerance is wide on purpose: TCAS bearing is the weak measurement.
"""
from __future__ import annotations

from verify.checks import CheckResult
from verify.tracks import angdiff, rng_brg

NM = 1852.0

def check(track, ctx, cfg: dict) -> CheckResult:
    c = cfg["tcas"]
    own = ctx.own
    if own.t < -1e8:
        return CheckResult.none("own position unknown")
    if not own.tcas_ok:
        return CheckResult.none("own TCAS not available")
    tc = track.last_tcas(ctx.t, c["fresh_s"])
    if not track.has_adsb:
        if tc is not None:                                  # TCAS-only target (no ADS-B): physically there
            return CheckResult(c["match_score"], c["match_conf"], f"TCAS tracks it at {tc[1] / NM:.1f} NM (no ADS-B)",
                               {"tcas_only": True, "tcas_range_m": round(tc[1])})
        return CheckResult.none("no ADS-B claim to compare")
    at = tc[0] if tc is not None else ctx.t
    claim = track.claimed_at(at)
    rng, brg = rng_brg(own.lat, own.lon, claim[0], claim[1])
    dalt = (claim[2] - own.alt_ft) if claim[2] is not None else 0.0
    ev = {"claimed_range_m": round(rng), "claimed_brg_deg": round(brg), "claimed_dalt_ft": round(dalt)}
    if tc is not None:
        _, t_rng, t_brg, t_alt = tc
        ev.update(tcas_range_m=round(t_rng), tcas_brg_deg=round(t_brg))
        r_tol = c["range_tol_m"] + c["range_tol_frac"] * rng
        if abs(t_rng - rng) > r_tol:
            return CheckResult(c["mismatch_score"], c["mismatch_conf"],
                               f"ADS-B says {rng / NM:.1f} NM, TCAS measures {t_rng / NM:.1f} NM", ev)
        if rng > 300 and angdiff(t_brg, brg) > c["bearing_tol_deg"]:
            return CheckResult(c["mismatch_score"], c["mismatch_conf"],
                               f"ADS-B bearing {brg:03.0f}, TCAS sees it at {t_brg:03.0f}", ev)
        if t_alt is not None and claim[2] is not None and abs(t_alt - claim[2]) > c["alt_tol_ft"]:
            return CheckResult(c["mismatch_score"], c["mismatch_conf"],
                               f"ADS-B altitude {claim[2]:.0f} ft, transponder reports {t_alt:.0f} ft", ev)
        return CheckResult(c["match_score"], c["match_conf"], f"TCAS confirms it: {t_rng / NM:.1f} NM, bearing {t_brg:03.0f}",
                           dict(ev, tcas_confirmed=True))
    inside = rng <= c["expect_inside_frac"] * c["range_nm"] * NM and abs(dalt) <= c["alt_window_ft"]
    if not inside:
        return CheckResult.none(f"beyond TCAS range ({rng / NM:.1f} NM)", **ev)
    if ctx.t - track.first_t < c["grace_s"]:
        return CheckResult.none("waiting for TCAS to acquire it", **ev)
    return CheckResult(c["absent_score"], c["absent_conf"], f"TCAS sees nothing where it claims to be ({rng / NM:.1f} NM)", ev)
