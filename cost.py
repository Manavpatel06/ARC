"""
cost.py — shared FUEL / TIME / CO2 cost of avoidance actions (Lane D; used by node/escape.py as a
TIE-BREAKER and by the log panel + pitch numbers).

Rule (do not change): SAFETY FIRST, COST SECOND. A maneuver is only ever compared on cost against
other maneuvers that pass every safety check (traffic margin, terrain, obstacles, performance, bounds).
Cost never makes FLOCK pick a less safe maneuver.

Why it matters: today's tools resolve pattern conflicts late, so the usual fix is big — a go-around
(a whole extra circuit) or a 360° turn for spacing. FLOCK sees the conflict ~60-90 s early, so the
fix is small: "extend downwind 15 s". This module puts numbers on that difference.

    from cost import action_cost, compare_table
    action_cost("EXTEND", seconds=15)          # -> {"extra_s": 30, "extra_nm": 0.75, "fuel_gal": 0.07, "usd": 0.47, "co2_lb": 1.3}
    action_cost("GO_AROUND")                   # full extra KDVT circuit from pattern.py geometry
    action_cost("TURN_360", bank_deg=20)
    action_cost("RESOLVE_TURN", bank_deg=30, hold_s=10)
    action_cost("CLIMB", vs_fpm=500, hold_s=10)

Sources / assumptions (say these out loud if asked):
  * Fuel: 8.5 US gal/h planning rate for a C172S (range 7-10 gal/h)  — flyawaysimulation.com C172 fuel planning.
    Climb at full power assumed 11 gal/h (ASSUMPTION; POH climb tables vary with DA).
  * Price: 100LL national average July 2026: $6.46 self-serve, $7.51 full-serve  — iFlightPlanner fuel trends.
  * CO2: 18.32 lb CO2 per gallon of aviation gasoline  — U.S. EIA CO2 emissions coefficients.
  * Speeds/geometry: pattern.py (KDVT legs, 90 kt pattern speed).
"""
from __future__ import annotations
import math

GPH_CRUISE = 8.5          # US gal/h, C172S planning rate
GPH_CLIMB = 11.0          # US gal/h, full-power climb (assumption)
USD_PER_GAL = 6.46        # 100LL self-serve national avg, Jul 2026
CO2_LB_PER_GAL = 18.32    # EIA, aviation gasoline
PATTERN_KT = 90.0
G = 9.80665
KT = 0.514444

def _money(extra_s: float, extra_climb_s: float = 0.0) -> dict:
    gal = GPH_CRUISE * extra_s / 3600.0 + (GPH_CLIMB - GPH_CRUISE) * extra_climb_s / 3600.0
    return {"fuel_gal": round(gal, 3), "usd": round(gal * USD_PER_GAL, 2), "co2_lb": round(gal * CO2_LB_PER_GAL, 2)}

def _circuit_s(runway: str = "25L", kt: float = PATTERN_KT) -> tuple[float, float]:
    """Seconds and NM for one full KDVT circuit (runway + all legs), from the shared pattern geometry."""
    from pattern import legs, _RW
    rw_len_m = _RW["ends"][runway]["length_ft"] * 0.3048
    m = sum(l["length_m"] for l in legs(runway)) + rw_len_m
    return m / (kt * KT), m / 1852.0

def _turn_and_rejoin_s(bank_deg: float, hold_s: float, kt: float, intercept_deg: float = 30.0) -> tuple[float, float]:
    """Bank for hold_s, roll out on the new heading's reverse turn back to the original heading over the
    same time, then rejoin the original track with a 30° intercept. Returns (extra seconds, lateral offset m)."""
    v = kt * KT
    w = G * math.tan(math.radians(abs(bank_deg))) / v           # rad/s
    dpsi = w * hold_s                                           # heading change after the first turn
    # integrate turn-out then turn-back (symmetric): along-track a, cross-track y
    n = 200; dt = hold_s / n; psi = 0.0; x = y = 0.0
    for i in range(2 * n):
        psi += (w if i < n else -w) * dt
        x += v * math.cos(psi) * dt; y += v * math.sin(psi) * dt
    t_flown = 2 * hold_s
    shortfall = v * t_flown - x                                 # metres of progress lost along track
    ic = math.radians(intercept_deg)
    rejoin_extra = abs(y) / math.sin(ic) - abs(y) / math.tan(ic)  # extra path to fly back to the line
    return (shortfall + rejoin_extra) / v, abs(y)

def action_cost(kind: str, seconds: float = 0.0, bank_deg: float = 20.0, hold_s: float = 10.0,
                vs_fpm: float = 500.0, kt: float = PATTERN_KT, runway: str = "25L") -> dict:
    """Extra time, distance, fuel, money and CO2 versus continuing the normal pattern."""
    kind = kind.upper()
    climb_s = 0.0
    if kind in ("EXTEND", "SEQUENCE", "SLOW_AND_SPACE"):
        extra = 2.0 * seconds                     # out on downwind/base and the same distance back
    elif kind == "GO_AROUND":
        extra, _ = _circuit_s(runway, kt)
        climb_s = (1000.0 / 600.0) * 60.0          # ~1,000 ft back to pattern altitude at ~600 fpm
    elif kind == "TURN_360":
        extra = 2 * math.pi * (kt * KT) / (G * math.tan(math.radians(bank_deg)))
    elif kind in ("RESOLVE_TURN", "TURN", "L", "R"):
        extra, _ = _turn_and_rejoin_s(bank_deg, hold_s, kt)
    elif kind in ("CLIMB", "DESCEND"):
        extra = 0.0                                # vertical step: no lateral detour
        climb_s = hold_s if kind == "CLIMB" else 0.0
    elif kind == "HOLD":
        extra = 0.0
    else:
        raise ValueError(kind)
    out = {"action": kind, "extra_s": round(extra, 1), "extra_nm": round(extra * kt / 3600.0, 2)}
    out.update(_money(extra, climb_s))
    return out

def candidate_cost(name: str, bank_deg: float = 0.0, vs_fpm: float = 0.0, hold_s: float = 10.0, kt: float = PATTERN_KT) -> dict:
    """Cost for an Escape Field candidate name like 'L30', 'R20', 'CLIMB', 'DESCEND', 'HOLD'."""
    n = name.upper()
    if n[:1] in "LR" and n[1:].isdigit():
        return action_cost("RESOLVE_TURN", bank_deg=float(n[1:]), hold_s=hold_s, kt=kt)
    if n in ("CLIMB", "DESCEND", "HOLD"):
        return action_cost(n, hold_s=hold_s, vs_fpm=vs_fpm, kt=kt)
    return action_cost("HOLD")

def compare_table() -> list[dict]:
    """The pitch slide: what each way of resolving the same pattern conflict costs one aircraft."""
    rows = [("FLOCK early sequencing: extend downwind 15 s", action_cost("EXTEND", seconds=15)),
            ("FLOCK resolve: 20° turn for 10 s, then rejoin", action_cost("RESOLVE_TURN", bank_deg=20, hold_s=10)),
            ("FLOCK resolve: 30° turn for 10 s, then rejoin", action_cost("RESOLVE_TURN", bank_deg=30, hold_s=10)),
            ("Late fix: 360° turn for spacing (20° bank)", action_cost("TURN_360", bank_deg=20)),
            ("Late fix: go-around, one more KDVT circuit", action_cost("GO_AROUND"))]
    return [dict(label=l, **c) for l, c in rows]

if __name__ == "__main__":
    print(f"{'action':48s} {'extra':>8s} {'NM':>6s} {'fuel gal':>9s} {'USD':>7s} {'CO2 lb':>7s}")
    for r in compare_table():
        print(f"{r['label']:48s} {r['extra_s']:7.0f}s {r['extra_nm']:6.2f} {r['fuel_gal']:9.3f} {r['usd']:7.2f} {r['co2_lb']:7.2f}")
    ga, ext = action_cost("GO_AROUND"), action_cost("EXTEND", seconds=15)
    print(f"\nOne go-around avoided by early sequencing saves ~{ga['extra_s']-ext['extra_s']:.0f} s, "
          f"{ga['fuel_gal']-ext['fuel_gal']:.2f} gal, ${ga['usd']-ext['usd']:.2f}, {ga['co2_lb']-ext['co2_lb']:.1f} lb CO2 per aircraft.")
