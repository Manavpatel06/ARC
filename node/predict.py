"""
node/predict.py — Lane B (B4).

Turn-aware prediction.  Two jobs:
  1. classify(): which traffic-pattern leg is this aircraft on, with a confidence in [0, 1]
  2. predict(): next ~90 s of (t, x, y, z, sigma), *including the turn to the next leg*.
     Turn points come from the pattern geometry, turn radius from speed and a 20 deg bank.
     If confidence < CONF_MIN the prediction falls back to straight-line with wider sigma.

PatternFollower is the single kinematic model of "an aircraft flying the pattern".  The
predictor runs it forward nominally; the harness reuses it (with randomised pilot behaviour)
to fly the AI traffic, so prediction error in the Monte Carlo is real model error.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from node.geometry import (FT, KT, G, LEG_ORDER, GLIDE_DEG, Pattern, hvec, turn_radius_m, wrap180, wrap360)

CONF_MIN = 0.6
PATTERN_BANK_DEG = 20.0
SIGMA0_M = 15.0
SIGMA_V0_M = 3.0
CLIMB_MS = 500 * FT / 60.0
DESC_BASE_MS = 500 * FT / 60.0
MAX_DESC_MS = 1000 * FT / 60.0

_INTENT_RE = re.compile(r"^([A-Z_]+?)_IN_(\d+(?:\.\d+)?)S$")


# ---------- data ----------
@dataclass
class KState:
    """Kinematic state used by classifier/predictor. SI units, altitude MSL metres."""
    x: float
    y: float
    z: float
    gs: float                  # m/s
    track: float               # deg true
    vs: float = 0.0            # m/s
    turn_rate: float = 0.0     # deg/s, + = right turn
    agl: Optional[float] = None


@dataclass
class Classification:
    runway: Optional[str]
    leg: str
    conf: float
    candidates: list = field(default_factory=list)    # [(conf, runway, leg)] best first

    @property
    def pattern_leg(self) -> bool:
        return self.leg not in ("UNKNOWN",)


@dataclass
class Prediction:
    method: str                        # "turn-aware" | "straight-line"
    confidence: float
    leg: str
    runway: Optional[str]
    t0: float                          # absolute time of pts[0]
    pts: np.ndarray                    # (N, 5): t_rel, x, y, z, sigma_h
    sigma_v: np.ndarray                # (N,)
    next_turn: Optional[tuple] = None  # (next_leg, t_rel) of the first predicted turn

    def at(self, t_abs: np.ndarray) -> np.ndarray:
        """Interpolate (x, y, z, sigma_h, sigma_v) at absolute times; clamps at the ends."""
        tt = self.pts[:, 0] + self.t0
        out = np.empty((len(t_abs), 5))
        for i in range(4):
            out[:, i] = np.interp(t_abs, tt, self.pts[:, i + 1])
        out[:, 4] = np.interp(t_abs, tt, self.sigma_v)
        return out

    def to_frame(self, ac_id: str, to_latlon) -> dict:
        path = []
        for (t, x, y, z, s) in self.pts[::5]:
            lat, lon = to_latlon(float(x), float(y))
            path.append({"t": round(float(t), 1), "lat": lat, "lon": lon, "alt_msl_ft": round(float(z) / FT, 0),
                         "sigma_m": round(float(s), 0)})
        return {"type": "PREDICTION", "ac_id": ac_id, "t": self.t0, "method": self.method,
                "confidence": round(self.confidence, 2), "leg": self.leg, "path": path}


# ---------- kinematic model ----------
class PatternFollower:
    """Flies a pattern leg-to-leg: lead-turn at each corner, line tracking on the legs, 3 deg glide on final."""

    def __init__(self, pat: Pattern, leg: str, x: float, y: float, z: float, gs: float, hdg: float,
                 bank_deg: float = PATTERN_BANK_DEG, lead_scale: float = 1.0, vz: float = 0.0,
                 turn_at: Optional[tuple] = None, extend_s: float = 0.0):
        self.pat, self.leg = pat, leg
        self.extend_s = extend_s        # seconds of extra straight flight to add once, at the next turn point
        self.x, self.y, self.z, self.v, self.hdg, self.vz = x, y, z, gs, hdg, vz
        self.bank, self.lead_scale = bank_deg, lead_scale
        self.turning = False
        self.turn_target = hdg
        self.turn_at = turn_at          # (next_leg_name, t_rel)
        self.t = 0.0
        self.landed = False
        self.first_turn: Optional[tuple] = None

    # --- guidance ---
    def _vertical(self) -> float:
        pat, leg = self.pat, self.leg
        if leg in ("FINAL", "STRAIGHT_IN"):
            s, _ = pat.xy_to_sl(self.x, self.y)
            zt = pat.elev_m + 15.0 + math.tan(math.radians(GLIDE_DEG)) * max(-s, 0.0)
            if self.z <= zt + 5.0:
                return 0.0 if self.z >= pat.elev_m + 12.0 else max(0.0, self.vz)
            return -min(MAX_DESC_MS, max(math.tan(math.radians(GLIDE_DEG)) * self.v, (self.z - zt) / 10.0))
        if leg == "BASE":
            return -DESC_BASE_MS if self.z > pat.elev_m + 70.0 else 0.0
        if leg == "UPWIND":
            return CLIMB_MS if self.z < pat.tpa_msl_m - 15.0 else 0.0
        if leg == "DOWNWIND" and pat.xy_to_sl(self.x, self.y)[0] < 0.0:
            return -DESC_BASE_MS if self.z > pat.elev_m + 70.0 else 0.0      # past the numbers: start down
        dz = pat.tpa_msl_m - self.z                      # crosswind / downwind: hold unless far from TPA
        if abs(dz) <= 300 * FT:
            return 0.0
        return math.copysign(CLIMB_MS, dz)

    def step(self, dt: float) -> None:
        pat = self.pat
        if self.landed:
            self.v = max(0.0, self.v - 3.0 * dt)
            self.x += hvec(self.hdg)[0] * self.v * dt
            self.y += hvec(self.hdg)[1] * self.v * dt
            self.z = pat.elev_m
            self.t += dt
            return
        L = pat.legs[self.leg]
        nxt = pat.next_leg(self.leg)
        along, cross, _ = L.along_cross(self.x, self.y)
        omega = math.degrees(self.v / turn_radius_m(self.v, self.bank)) if self.v > 1 else 0.0   # deg/s
        if not self.turning and nxt is not None:
            dpsi = abs(wrap180(pat.legs[nxt].heading - L.heading))
            lead = turn_radius_m(self.v, self.bank) * math.tan(math.radians(dpsi) / 2.0) * self.lead_scale
            by_intent = self.turn_at is not None and self.turn_at[0] == nxt and self.t >= self.turn_at[1]
            if (L.length - along) <= lead or by_intent:
                if self.extend_s > 0.0:
                    self.extend_s -= dt                          # sequencing: fly on past the turn point
                else:
                    self.turning, self.turn_target = True, pat.legs[nxt].heading
                    if self.first_turn is None:
                        self.first_turn = (nxt, self.t)
        if self.turning:
            dh = wrap180(self.turn_target - self.hdg)
            if abs(dh) <= omega * dt:
                self.hdg, self.leg, self.turning = self.turn_target, nxt, False
            else:
                self.hdg = wrap360(self.hdg + math.copysign(omega * dt, dh))
        else:
            look = max(400.0, 10.0 * self.v)
            desired = L.heading + max(-25.0, min(25.0, math.degrees(math.atan2(cross, look))))
            dh = wrap180(desired - self.hdg)
            self.hdg = wrap360(self.hdg + max(-omega * dt, min(omega * dt, dh)))
        self.vz = self._vertical()
        hx, hy = hvec(self.hdg)
        self.x += hx * self.v * dt
        self.y += hy * self.v * dt
        self.z = max(pat.elev_m, self.z + self.vz * dt)
        self.t += dt
        if self.leg == "FINAL" and not self.turning:
            a2, _, _ = pat.legs["FINAL"].along_cross(self.x, self.y)
            if a2 >= pat.legs["FINAL"].length:
                self.landed = True
                self.z = pat.elev_m


# ---------- classifier + predictor ----------
class Predictor:
    def __init__(self, patterns: dict[str, Pattern], conf_min: float = CONF_MIN, line_sigma_k: float = 2.5):
        self.patterns = patterns
        self.conf_min = conf_min
        self.line_sigma_k = line_sigma_k       # sigma growth (m/s) of the straight-line fallback

    # --- classification ---
    @staticmethod
    def _alt_term(leg: str, agl_ft: float, tpa_agl_ft: float) -> float:
        if leg in ("CROSSWIND", "DOWNWIND"):
            d = abs(agl_ft - tpa_agl_ft)
            lo, hi = 400.0, 800.0
        elif leg == "UPWIND":
            d = max(0.0, agl_ft - (tpa_agl_ft + 300.0))
            lo, hi = 0.0, 600.0
        elif leg == "BASE":
            d = max(0.0, 150.0 - agl_ft, agl_ft - (tpa_agl_ft + 300.0))
            lo, hi = 0.0, 500.0
        elif leg == "FINAL":
            d = max(0.0, agl_ft - 1500.0, -50.0 - agl_ft)
            lo, hi = 0.0, 700.0
        else:  # STRAIGHT_IN
            d = max(0.0, agl_ft - 3000.0, -50.0 - agl_ft)
            lo, hi = 0.0, 1000.0
        if d <= lo:
            return 1.0
        return math.exp(-(((d - lo) / hi) ** 2))

    def classify(self, st: KState, declared_leg: Optional[str] = None) -> Classification:
        cands: list[tuple[float, str, str]] = []
        for rwy, pat in self.patterns.items():
            agl_ft = ((st.agl if st.agl is not None else st.z - pat.elev_m)) / FT
            tpa_ft = pat.tpa_agl_m / FT
            for name in LEG_ORDER + ["STRAIGHT_IN"]:
                L = pat.legs[name]
                along, cross, out = L.along_cross(st.x, st.y)
                e = math.hypot(cross, out)
                pos = math.exp(-((e / 450.0) ** 2))
                dth = abs(wrap180(st.track - L.heading))
                head = math.exp(-((dth / 28.0) ** 2))
                diff = wrap180(L.heading - st.track)
                if abs(st.turn_rate) > 1.0 and diff * st.turn_rate > 0 and abs(diff) <= 135.0:
                    # mid-turn toward this leg: judge where the arc will leave us, not where we are now
                    r = st.gs / math.radians(abs(st.turn_rate))
                    d_rad = math.radians(abs(diff))
                    sgn = 1.0 if st.turn_rate > 0 else -1.0
                    cross_end = cross + sgn * r * (1.0 - math.cos(d_rad))
                    along_end = along + r * math.sin(d_rad)
                    out_end = max(0.0, -along_end, along_end - L.length)
                    pos_t = math.exp(-((math.hypot(cross_end, out_end) / 300.0) ** 2))
                    if pos_t * 1.0 > pos * head:
                        pos, head = pos_t, 1.0
                conf = pos * head * self._alt_term(name, agl_ft, tpa_ft)
                if name == "FINAL" and agl_ft < 500.0 and st.vs > 300 * FT / 60.0 and                         abs(wrap180(st.track - pat.heading)) < 25.0:
                    cands.append((conf, rwy, "GO_AROUND"))
                cands.append((conf, rwy, name))
        cands.sort(key=lambda c: -c[0])
        best = cands[0]
        if declared_leg:
            for c in cands:
                if c[2] == declared_leg and c[0] >= 0.3:
                    best = (max(c[0], 0.8), c[1], c[2])
                    break
        if best[0] < 0.15:
            return Classification(None, "UNKNOWN", best[0], cands[:3])
        return Classification(best[1], best[2], best[0], cands[:3])

    # --- prediction ---
    @staticmethod
    def parse_intent(intent: str) -> Optional[tuple[str, float]]:
        m = _INTENT_RE.match(intent or "")
        return (m.group(1), float(m.group(2))) if m else None

    def straight_line(self, st: KState, t0: float, horizon: float, dt: float, conf: float, leg: str,
                      runway: Optional[str], age_s: float = 0.0) -> Prediction:
        n = int(horizon / dt) + 1
        t = np.arange(n) * dt
        hx, hy = hvec(st.track)
        sigma = SIGMA0_M + 5.0 * age_s + self.line_sigma_k * t
        pts = np.column_stack([t, st.x + hx * st.gs * t, st.y + hy * st.gs * t, st.z + st.vs * t, sigma])
        return Prediction("straight-line", conf, leg, runway, t0, pts, SIGMA_V0_M + 0.6 * t)

    def predict(self, st: KState, t0: float, cls: Optional[Classification] = None, horizon: float = 90.0,
                dt: float = 1.0, intent: Optional[str] = None, intent_age_s: float = 0.0, age_s: float = 0.0,
                declared_leg: Optional[str] = None, extend_s: float = 0.0) -> Prediction:
        cls = cls or self.classify(st, declared_leg)
        if cls.conf < self.conf_min or cls.runway is None or cls.leg == "UNKNOWN":
            return self.straight_line(st, t0, horizon, dt, cls.conf, cls.leg, cls.runway, age_s)
        pat = self.patterns[cls.runway]
        leg = "UPWIND" if cls.leg == "GO_AROUND" else cls.leg
        turn_at = None
        pi = self.parse_intent(intent) if intent else None
        if pi and intent_age_s < 25.0:
            turn_at = (pi[0], max(0.0, pi[1] - intent_age_s))
        fol = PatternFollower(pat, leg, st.x, st.y, st.z, st.gs, st.track, vz=st.vs, turn_at=turn_at)
        if extend_s > 0.0:
            nxt = pat.next_leg(leg)
            if nxt is not None:
                L = pat.legs[leg]
                along, _, _ = L.along_cross(st.x, st.y)
                lead = turn_radius_m(st.gs, PATTERN_BANK_DEG) * math.tan(math.radians(abs(wrap180(pat.legs[nxt].heading - L.heading))) / 2.0)
                already = max(0.0, (along - (L.length - lead)) / max(st.gs, 1.0))      # time already spent past the turn point
                # only believe the extension once we can see it being flown (already past the turn point):
                # assuming compliance up front would hide the conflict the advice is meant to fix
                fol.extend_s = max(0.0, extend_s - already) if already > 0.5 else 0.0
        if abs(st.turn_rate) > 1.0:
            fol.turn_target = fol.hdg
        n = int(horizon / dt) + 1
        pts = np.empty((n, 5))
        pts[0] = (0.0, st.x, st.y, st.z, 0.0)
        for i in range(1, n):
            fol.step(dt)
            pts[i] = (i * dt, fol.x, fol.y, fol.z, 0.0)
        t = pts[:, 0]
        k = 0.8 + 1.5 * (1.0 - cls.conf)
        pts[:, 4] = SIGMA0_M + 5.0 * age_s + k * t
        return Prediction("turn-aware", cls.conf, cls.leg, cls.runway, t0, pts, SIGMA_V0_M + 0.3 * t, fol.first_turn)
