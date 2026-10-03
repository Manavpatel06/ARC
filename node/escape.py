"""
node/escape.py — Lane B (B8).  The Escape Field.

Candidates (bank L/R 20/30/45, climb, descend 500 fpm, hold) are each flown forward 30 s with this
aircraft's own performance (climb rate from density altitude, 15 deg/s roll rate, turn rate g*tan(bank)/V)
and scored against every peer's predicted path (or the peer's committed maneuver, if it sent one),
the terrain floor, obstacles and the performance/airspace limits.  The least severe candidate that clears
the NMAC box with margin is chosen; every rejected candidate carries a one-line reason for the log.

Pure functions, no I/O.  Authority limits are enforced separately in node/authority.py; escape only knows
the physical limits, so a candidate that is physically fine but out of bounds is rejected there.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from node.geometry import FT, G, KT, hvec

HORIZON_S = 30.0
DT = 0.5
ROLL_RATE_DPS = 15.0
VS_RATE_MS2 = 1.0                # ~200 fpm per second
HOLD_S = 10.0
NMAC_H_M = 500 * FT
NMAC_V_M = 100 * FT
MARGIN_OK = 1.5                  # NMAC-box multiples considered "clear with margin"
MARGIN_MIN = 1.0                 # inside this the maneuver does not resolve the conflict
TERRAIN_CLEARANCE_M = 300 * FT
MAX_AUTH_BANK = 30.0
DESCEND_FPM = 500.0
MIN_IAS_KT = 62.0
SIMILAR_MARGIN = 0.3             # margins this close count as "similar" for the tie-breakers
SIMILAR_SEVERITY = 0.25          # ... and so do maneuvers within one bank step of the least severe one
PATTERN_FLOOR_BELOW_TPA_M = 300 * FT


def climb_rate_fpm(da_ft: float) -> float:
    """C172S-class climb from density altitude: the shared curve in data/metar.py (730 @ 0, 500 @ 5,000, 300 @ 8,000)."""
    from data.metar import climb_fpm
    return climb_fpm(da_ft)


@dataclass
class Candidate:
    name: str
    kind: str                       # turn | climb | descend | hold
    bank: float = 0.0               # signed deg, + = right
    vs_fpm: float = 0.0
    severity: float = 0.0

    @property
    def sense(self) -> str:
        if self.kind == "turn":
            return "R" if self.bank > 0 else "L"
        return {"climb": "CLIMB", "descend": "DESCEND", "hold": "HOLD"}[self.kind]


def _cost(c: "Candidate", own: "OwnState", hold_s: float) -> dict:
    """Shared fuel/time cost (cost.py, Lane D) of flying this candidate; zero-cost fallback if cost.py is absent."""
    try:
        from cost import candidate_cost
        return candidate_cost(c.name, bank_deg=abs(c.bank), vs_fpm=c.vs_fpm, hold_s=hold_s, kt=max(own.gs / KT, 40.0))
    except Exception:
        return {"extra_s": 0.0, "fuel_gal": 0.0, "usd": 0.0, "co2_lb": 0.0}


def _hits_obstacle(path, xs, ys, own: "OwnState", obstacle_fn) -> bool:
    """Below top + 300 ft inside an obstacle's protection cylinder, but only if the maneuver makes things worse:
    it descends more than 12 m, or runs into an obstacle taller than the one we are already passing."""
    here = obstacle_fn(own.x, own.y)
    here = -1e9 if here is None else here
    for i in range(len(xs)):
        top = obstacle_fn(float(xs[i]), float(ys[i]))
        if top is None:
            continue
        z = path[i * 4, 2]
        if z < top + TERRAIN_CLEARANCE_M and (top > here + 1.0 or z < own.z - 12.0):
            return True
    return False


def candidates(climb_fpm: float, cur_bank: float = 0.0) -> list[Candidate]:
    """Climb/descend keep the bank the aircraft already has (a turn in progress continues), so the command is
    never 'wings level' by accident."""
    keep = max(-MAX_AUTH_BANK, min(MAX_AUTH_BANK, cur_bank))
    out = [Candidate("HOLD", "hold", severity=0.0)]
    for b in (20, 30, 45):
        out.append(Candidate(f"L{b}", "turn", -float(b), severity=b / 45.0))
        out.append(Candidate(f"R{b}", "turn", float(b), severity=b / 45.0))
    out.append(Candidate("CLIMB", "climb", keep, climb_fpm, 0.35))
    out.append(Candidate("DESCEND", "descend", keep, -DESCEND_FPM, 0.30))
    return out


@dataclass
class OwnState:
    x: float
    y: float
    z: float
    hdg: float
    gs: float                       # m/s
    bank: float = 0.0
    vs: float = 0.0                 # m/s
    ias_kt: float = 90.0
    agl: float = 300.0
    da_ft: float = 4980.0
    tpa_msl_m: Optional[float] = None


def simulate_maneuver(x: float, y: float, z: float, hdg: float, v: float, bank0: float, vs0: float,
                      bank_cmd: float, vs_cmd: float, hold_s: float = HOLD_S, delay_s: float = 0.0,
                      horizon: float = HORIZON_S, dt: float = DT) -> np.ndarray:
    """(N, 3) x, y, z at t = 0, dt, ... horizon.  Bank/vs ramp to the command during [delay, delay+hold], then level."""
    n = int(round(horizon / dt)) + 1
    out = np.empty((n, 3))
    bank, vs, h = bank0, vs0, hdg
    out[0] = (x, y, z)
    for i in range(1, n):
        t = i * dt
        active = delay_s <= t <= delay_s + hold_s
        tb = bank_cmd if active else 0.0
        tv = vs_cmd * FT / 60.0 if active else 0.0
        bank += max(-ROLL_RATE_DPS * dt, min(ROLL_RATE_DPS * dt, tb - bank))
        vs += max(-VS_RATE_MS2 * dt, min(VS_RATE_MS2 * dt, tv - vs))
        h += math.degrees(G * math.tan(math.radians(bank)) / v) * dt if v > 1 else 0.0
        hx, hy = hvec(h)
        x += hx * v * dt
        y += hy * v * dt
        z += vs * dt
        out[i] = (x, y, z)
    return out


def peer_commit_path(peer_state: dict, commit: dict, now: float, fallback: np.ndarray) -> np.ndarray:
    """Forward-simulate a peer's committed maneuver (MANEUVER_COMMIT body); HOLD means follow its predicted path."""
    sense = commit.get("sense", "HOLD")
    if sense == "HOLD":
        return fallback
    bank = float(commit.get("bank_deg", 30.0)) * (1.0 if sense == "R" else -1.0 if sense == "L" else 0.0)
    vs = {"CLIMB": 500.0, "DESCEND": -500.0}.get(sense, float(commit.get("vs_fpm", 0.0)))
    if sense in ("L", "R"):
        vs = 0.0
    delay = max(0.0, float(commit.get("start_t", now)) - now)
    return simulate_maneuver(peer_state["x"], peer_state["y"], peer_state["z"], peer_state["hdg"], peer_state["gs"],
                             0.0, peer_state.get("vs", 0.0), bank, vs, float(commit.get("hold_s", HOLD_S)), delay)


@dataclass
class Evaluation:
    cand: Candidate
    feasible: bool
    reason: Optional[str]
    margin: float
    min_h_m: float
    min_v_m: float
    path: np.ndarray = field(repr=False, default=None)

    def summary(self) -> dict:
        return {"cand": self.cand.name, "margin": round(self.margin, 2), "min_h_ft": round(self.min_h_m / FT),
                "min_v_ft": round(self.min_v_m / FT), "ok": self.feasible, "why": self.reason}


@dataclass
class EscapeResult:
    ranked: list                    # feasible Evaluations, preferred first
    evals: list                     # every Evaluation
    rejected: dict                  # name -> reason
    base_margin: float              # margin of HOLD (nothing changes)
    costs: dict = field(default_factory=dict)       # candidate name -> shared cost.py numbers
    env: dict = field(default_factory=dict)         # density altitude / climb capability the decision used

    @property
    def chosen(self) -> Optional[Evaluation]:
        return self.ranked[0] if self.ranked else None

    def reason(self, ttc_s: Optional[float] = None) -> dict:
        ch = self.chosen
        r = {"chosen": ch.cand.name if ch else None, "rejected": dict(self.rejected),
             "margin": round(ch.margin, 2) if ch else None, "hold_margin": round(self.base_margin, 2)}
        if ttc_s is not None:
            r["ttc_s"] = round(ttc_s, 1)
        if self.env:
            r["env"] = dict(self.env)
        if self.costs:
            def brief(c):
                return {"extra_s": c["extra_s"], "fuel_gal": c["fuel_gal"], "usd": c["usd"]}
            cc = self.costs.get(ch.cand.name) if ch else None
            r["cost"] = {"chosen": brief(cc) if cc else None,
                         "rejected": {n: brief(c) for n, c in self.costs.items() if ch is None or n != ch.cand.name},
                         "note": (f"extra {cc['extra_s']:.0f} s, {cc['fuel_gal']:.2f} gal" if cc else "")}
        return r


def _margin(path: np.ndarray, peers: dict[str, np.ndarray], sigmas: Optional[dict[str, tuple]] = None):
    best_m, best_h, best_v = math.inf, math.inf, math.inf
    for pid, pp in peers.items():
        n = min(len(path), len(pp))
        dh = np.hypot(path[:n, 0] - pp[:n, 0], path[:n, 1] - pp[:n, 1])
        dz = np.abs(path[:n, 2] - pp[:n, 2])
        if sigmas and pid in sigmas:
            sh, sv = sigmas[pid]
            dh_e = np.maximum(0.0, dh - 0.5 * sh[:n])
            dz_e = np.maximum(0.0, dz - 0.5 * sv[:n])
        else:
            dh_e, dz_e = dh, dz
        m = np.maximum(dh_e / NMAC_H_M, dz_e / NMAC_V_M)
        i = int(np.argmin(m))
        if m[i] < best_m:
            best_m, best_h, best_v = float(m[i]), float(dh[i]), float(dz[i])
    return best_m, best_h, best_v


def evaluate(own: OwnState, hold_path: np.ndarray, peers: dict[str, np.ndarray],
             terrain_fn: Callable[[float, float], float], obstacle_fn: Optional[Callable] = None,
             sigmas: Optional[dict[str, tuple]] = None, max_bank: float = MAX_AUTH_BANK,
             ceiling_msl_m: Optional[float] = None, hold_s: float = HOLD_S,
             exclude: tuple = (), require_maneuver: bool = False) -> EscapeResult:
    """require_maneuver: the peer is already maneuvering; prefer a complementary maneuver of our own (TCAS-style)
    that adds separation on top of the peer's, and fall back to holding only if nothing adds any."""
    climb = climb_rate_fpm(own.da_ft)
    evals: list[Evaluation] = []
    rejected: dict[str, str] = {}
    pattern_floor = (own.tpa_msl_m - PATTERN_FLOOR_BELOW_TPA_M) if own.tpa_msl_m is not None else None
    for c in candidates(climb, own.bank):
        if c.name in exclude:
            continue
        if c.kind == "hold":
            path = hold_path
        else:
            path = simulate_maneuver(own.x, own.y, own.z, own.hdg, own.gs, own.bank, own.vs,
                                     c.bank, c.vs_fpm, hold_s)
        margin, mh, mv = _margin(path, peers, sigmas)
        reason = None
        if c.kind == "turn" and abs(c.bank) > max_bank:
            reason = f"exceeds {max_bank:.0f} deg authority bound"
        elif c.kind == "turn" and own.ias_kt < MIN_IAS_KT + 3.0:
            reason = f"speed margin {own.ias_kt:.0f} kt"
        elif c.kind == "climb" and (own.ias_kt < MIN_IAS_KT + 3.0 or climb < 250.0):
            reason = f"performance {climb:.0f} fpm at DA {own.da_ft:,.0f} ft"
        elif c.kind != "hold":
            zs = path[:, 2]
            xs, ys = path[::4, 0], path[::4, 1]
            # keep >= 300 ft over terrain; an aircraft already lower than that (short final) only has to not lose height
            clear = min(TERRAIN_CLEARANCE_M, max(0.0, own.agl - 12.0))
            if any(path[i * 4, 2] - terrain_fn(float(xs[i]), float(ys[i])) < clear for i in range(len(xs))):
                reason = "terrain floor"
            elif obstacle_fn and _hits_obstacle(path, xs, ys, own, obstacle_fn):
                reason = "obstacle"
            elif c.kind == "descend" and pattern_floor is not None and zs.min() < pattern_floor:
                reason = "pattern altitude floor"
            elif ceiling_msl_m is not None and zs.max() > ceiling_msl_m:
                reason = "airspace ceiling"
        if reason is None and c.kind != "hold" and margin < MARGIN_MIN:
            reason = f"performance {climb:.0f} fpm at DA {own.da_ft:,.0f} ft" if c.kind == "climb" else "traffic"
        ev = Evaluation(c, reason is None, reason, margin, mh, mv, path)
        evals.append(ev)
        if reason is not None:
            rejected[c.name] = reason
    base = next((e.margin for e in evals if e.cand.kind == "hold"), math.inf)
    good = [e for e in evals if e.feasible and e.margin >= MARGIN_OK]
    marginal = [e for e in evals if e.feasible and MARGIN_MIN <= e.margin < MARGIN_OK and e.cand.kind != "hold"]
    good.sort(key=lambda e: (e.cand.severity, -e.margin))
    marginal.sort(key=lambda e: -e.margin)
    costs = {e.cand.name: _cost(e.cand, own, hold_s) for e in evals}
    # Safety decides who is allowed (every check passed, margin >= MARGIN_OK).  Among those that are about as safe
    # as the least severe one, break ties: (1) turn right rather than left (14 CFR 91.113: head-on, both right),
    # (2) lower fuel from the shared cost model, (3) lower severity.  Cost never promotes a less safe maneuver.
    if good:
        c0 = good[0]
        group = [e for e in good if e.cand.severity <= c0.cand.severity + SIMILAR_SEVERITY
                 and e.margin >= c0.margin - SIMILAR_MARGIN]
        group.sort(key=lambda e: (1 if (e.cand.kind == "turn" and e.cand.bank < 0) else 0,
                                  round(costs[e.cand.name]["fuel_gal"], 3), e.cand.severity, -e.margin))
        good = group + [e for e in good if e not in group]
    ranked = good + marginal
    if require_maneuver:
        # complementary = our maneuver must add separation on top of what the peer's maneuver already gives
        adds = [e for e in ranked if e.cand.kind != "hold" and e.margin >= base + 0.15]
        ranked = adds or ranked
    for e in evals:
        if e.feasible and e not in ranked:
            rejected[e.cand.name] = "traffic" if e.cand.kind != "hold" else "conflict persists"
    return EscapeResult(ranked, evals, rejected, base, costs, {"da_ft": round(own.da_ft), "climb_fpm": round(climb)})
