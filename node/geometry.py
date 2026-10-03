"""
node/geometry.py — Lane B (B2).  Thin wrapper over the shared pattern.py (contract v1.1).

pattern.py owns the circuit: ENU frame, leg segments, headings (KDVT is 086/266 TRUE; 25L left traffic is
south of the field, 25R right traffic is north).  This module adds what the classifier/predictor need on
top of those segments: a runway frame (s along the landing direction from the threshold, l toward the
pattern side), along/cross-track tests per leg, remaining-path length for sequencing, and scenario placement
via pattern.place().

Conventions: ENU metres, x east / y north; altitudes metres MSL inside node/; headings degrees TRUE.
"""
from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass, field
from typing import Optional

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import pattern as _pat                     # shared geometry (Lane D)
from data.runways import load as _load_runways
from data import metar as _metar

KDVT_LAT, KDVT_LON, KDVT_ELEV_FT = _pat.REF_LAT, _pat.REF_LON, _pat.ELEV_FT
FT = 0.3048
KT = 0.514444
NM = 1852.0
G = 9.80665
GLIDE_DEG = 3.0

LEG_ORDER = ["UPWIND", "CROSSWIND", "DOWNWIND", "BASE", "FINAL"]
DEFAULT_RUNWAYS = ("25L", "25R")


def to_enu(lat: float, lon: float) -> tuple[float, float]:
    return _pat.to_enu(lat, lon)


def to_latlon(x: float, y: float) -> tuple[float, float]:
    return _pat.from_enu(x, y)


def wrap180(a: float) -> float:
    return (a + 180.0) % 360.0 - 180.0


def wrap360(a: float) -> float:
    return a % 360.0


def hvec(heading_deg: float) -> tuple[float, float]:
    r = math.radians(heading_deg)
    return (math.sin(r), math.cos(r))


def bearing_deg(dx: float, dy: float) -> float:
    return math.degrees(math.atan2(dx, dy)) % 360.0


def turn_radius_m(v_ms: float, bank_deg: float = 20.0) -> float:
    return v_ms * v_ms / (G * math.tan(math.radians(bank_deg)))


@dataclass
class Leg:
    name: str
    a: tuple[float, float]
    b: tuple[float, float]
    heading: float
    length: float = 0.0
    d: tuple[float, float] = (0.0, 0.0)

    def __post_init__(self):
        dx, dy = self.b[0] - self.a[0], self.b[1] - self.a[1]
        self.length = math.hypot(dx, dy)
        self.d = (dx / self.length, dy / self.length) if self.length > 0 else hvec(self.heading)

    def along_cross(self, x: float, y: float) -> tuple[float, float, float]:
        """(along from a, signed cross-track (+ = left of travel), distance outside the segment ends)."""
        rx, ry = x - self.a[0], y - self.a[1]
        along = rx * self.d[0] + ry * self.d[1]
        cross = self.d[0] * ry - self.d[1] * rx
        return along, cross, max(0.0, -along, along - self.length)


@dataclass
class Pattern:
    runway: str                     # landing end, e.g. "25L"
    heading: float                  # final-approach heading (deg true)
    side: str                       # "left" | "right"
    thr: tuple[float, float]
    length_m: float
    elev_m: float
    tpa_agl_m: float
    base_dist: float                # length of FINAL: base-to-final corner to the threshold
    legs: dict = field(default_factory=dict)       # UPWIND..FINAL + STRAIGHT_IN
    f: tuple[float, float] = (0.0, 0.0)
    p: tuple[float, float] = (0.0, 0.0)

    @property
    def tpa_msl_m(self) -> float:
        return self.elev_m + self.tpa_agl_m

    def sl_to_xy(self, s: float, l: float) -> tuple[float, float]:
        return (self.thr[0] + self.f[0] * s + self.p[0] * l, self.thr[1] + self.f[1] * s + self.p[1] * l)

    def xy_to_sl(self, x: float, y: float) -> tuple[float, float]:
        dx, dy = x - self.thr[0], y - self.thr[1]
        return (dx * self.f[0] + dy * self.f[1], dx * self.p[0] + dy * self.p[1])

    def next_leg(self, name: str) -> Optional[str]:
        if name == "STRAIGHT_IN":
            return "FINAL"
        if name == "GO_AROUND":
            return "UPWIND"
        if name in LEG_ORDER:
            i = LEG_ORDER.index(name)
            return LEG_ORDER[i + 1] if i + 1 < len(LEG_ORDER) else None
        return None

    def remaining_path_m(self, x: float, y: float, leg: str) -> float:
        """Path length still to fly before crossing the threshold on final."""
        if leg in ("FINAL", "STRAIGHT_IN", "GO_AROUND"):
            s, _ = self.xy_to_sl(x, y)
            return max(0.0, -s)
        L = self.legs.get(leg)
        if L is None:
            return float("inf")
        along, _, _ = L.along_cross(x, y)
        rem = max(0.0, L.length - along)
        for nm in LEG_ORDER[LEG_ORDER.index(leg) + 1:]:
            rem += self.legs[nm].length
        return rem

    def place(self, leg: str, offset_s: float = 0.0, gs_kt: float = 90.0, agl_ft: Optional[float] = None) -> dict:
        """Scenario start semantics of INTERFACE.md section 5: delegates to pattern.place()."""
        return _pat.place(leg, self.runway, offset_s, gs_kt, agl_ft)

    def reference_point(self, leg: str, offset_m: float = 0.0, gs_kt: float = 90.0) -> tuple[float, float, float]:
        """(x, y, heading) at offset_m metres along the circuit from the START of `leg` (negative = before it)."""
        p = _pat.place(leg, self.runway, offset_m / (gs_kt * KT), gs_kt)
        return (p["x_m"], p["y_m"], p["hdg_deg"])


def build_pattern(runway: str) -> Pattern:
    """One Pattern from pattern.legs(runway); everything geometric comes from the shared definition."""
    segs = _pat.legs(runway)
    by = {s["name"]: s for s in segs}
    fin = by["FINAL"]
    thr = fin["end"]
    heading = fin["hdg_deg"]
    side = _load_runways()["ends"][runway]["pattern"]
    f = hvec(heading)
    p = hvec(heading - 90.0 if side == "left" else heading + 90.0)
    length = math.hypot(by["UPWIND"]["start"][0] - thr[0], by["UPWIND"]["start"][1] - thr[1])
    pat = Pattern(runway, heading, side, thr, length, _pat.ELEV_FT * FT, _pat.TPA_AGL_FT * FT, fin["length_m"], f=f, p=p)
    for name in LEG_ORDER:
        s = by[name]
        pat.legs[name] = Leg(name, s["start"], s["end"], s["hdg_deg"])
    si = _pat.straight_in(runway)
    pat.legs["STRAIGHT_IN"] = Leg("STRAIGHT_IN", si["start"], fin["start"], si["hdg_deg"])
    return pat


def load_metar() -> dict:
    return _metar.load()


def build_patterns(runways=DEFAULT_RUNWAYS) -> dict[str, Pattern]:
    return {r: build_pattern(r) for r in runways}
