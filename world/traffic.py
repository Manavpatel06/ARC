"""
world/traffic.py — Lane A. Pattern autopilot for AI aircraft.

Geometry comes from the shared /pattern.py (one definition for world, nodes and god view):
UPWIND -> CROSSWIND -> DOWNWIND -> BASE -> FINAL -> touch-and-go -> UPWIND, plus STRAIGHT_IN,
each leg a straight ENU segment with AGL at both ends. 25L = left traffic (south), 25R = right
traffic (north); headings 086/266 true.

The autopilot steers a ground TRACK along each leg (cross-track correction, so wind cannot make
it drift), banks 20-24 deg in turns, leads each turn by r*tan(dpsi/2) + roll-in distance, and
follows the leg's altitude profile with a feed-forward descent rate on descending legs. It only
produces targets; the limits (bank rate, climb vs density altitude, speed envelope) live in
world/flight_model.py.
"""
from __future__ import annotations
import math, zlib
from dataclasses import dataclass

import pattern as P
from world.flight_model import BANK_RATE_DPS, FT, G, KT, Aircraft, Env, clamp, tas_kt, wrap180

PATTERN_BANK = 22.0
XTRK_GAIN = 0.15               # deg of track correction per metre off course
GROUND_ROLL_M = 300.0          # touch-and-go: roll this far before rotating
LEG_IAS = {"UPWIND": 75, "CROSSWIND": 85, "DOWNWIND": 95, "BASE": 85, "FINAL": 72, "STRAIGHT_IN": 80}
CIRCUIT = ["UPWIND", "CROSSWIND", "DOWNWIND", "BASE", "FINAL"]

@dataclass
class Leg:
    name: str
    a: tuple[float, float]
    b: tuple[float, float]
    agl0_ft: float
    agl1_ft: float

    @property
    def brg(self) -> float:
        return math.degrees(math.atan2(self.b[0] - self.a[0], self.b[1] - self.a[1])) % 360.0

    @property
    def length(self) -> float:
        return math.hypot(self.b[0] - self.a[0], self.b[1] - self.a[1])

    def project(self, p) -> tuple[float, float]:
        """(along-track m from a, cross-track m; + = right of course)."""
        ue, un = math.sin(math.radians(self.brg)), math.cos(math.radians(self.brg))
        de, dn = p[0] - self.a[0], p[1] - self.a[1]
        return de * ue + dn * un, de * un - dn * ue

    def agl_at(self, along: float) -> float:
        f = clamp(along / self.length, 0.0, 1.0) if self.length else 1.0
        return self.agl0_ft + f * (self.agl1_ft - self.agl0_ft)

    @property
    def slope(self) -> float:
        """Descent gradient (ft of AGL lost per ft flown), >= 0."""
        return max(0.0, (self.agl0_ft - self.agl1_ft) / (self.length / FT)) if self.length else 0.0

def _leg(seg: dict) -> Leg:
    return Leg(seg["name"], tuple(seg["start"]), tuple(seg["end"]), seg["agl0_ft"], seg["agl1_ft"])

class Pattern:
    """Pattern geometry for one landing runway ident (e.g. '25L'), from pattern.py."""
    def __init__(self, ident: str):
        self.ident = ident
        self.side = P._RW["ends"][ident]["pattern"]
        self.elev_ft = P.ELEV_FT
        self.tpa_ft = P.ELEV_FT + P.TPA_AGL_FT
        self.legs = {s["name"]: _leg(s) for s in P.legs(ident)}
        self.legs["STRAIGHT_IN"] = _leg(P.straight_in(ident))
        thr, far = self.legs["FINAL"].b, self.legs["UPWIND"].a
        self.runway_len_m = math.hypot(far[0] - thr[0], far[1] - thr[1])

    @staticmethod
    def to_enu(lat: float, lon: float) -> tuple[float, float]:
        return P.to_enu(lat, lon)

    def next_leg(self, name: str) -> str:
        if name in ("FINAL", "STRAIGHT_IN"):
            return "UPWIND"
        return CIRCUIT[CIRCUIT.index(name) + 1]

    def geometry(self) -> dict:
        """Lat/lon leg segments for the god view."""
        ll = lambda p: [round(v, 6) for v in P.from_enu(*p)]
        return {"runway": self.ident, "side": self.side, "tpa_ft": self.tpa_ft,
                "legs": {n: [ll(L.a), ll(L.b)] for n, L in self.legs.items()}}

class PatternPilot:
    """Autopilot that flies the circuit forever (touch-and-goes)."""
    def __init__(self, pattern: Pattern, leg: str, ac_id: str):
        self.p = pattern
        self.leg = "UPWIND" if leg == "GO_AROUND" else leg
        h = zlib.crc32(ac_id.encode())
        self.ias_bias = (h % 9) - 4.0                 # +-4 kt per aircraft
        self.bank = PATTERN_BANK + ((h >> 8) % 5) - 2  # 20-24 deg

    def targets(self, ac: Aircraft, env: Env, now: float) -> tuple[float, float, float]:
        p = self.p
        pos = p.to_enu(ac.lat, ac.lon)
        L = p.legs[self.leg]
        along, xtrk = L.project(pos)

        # leg sequencing: lead the turn by r*tan(dpsi/2) plus the distance flown while rolling in
        # (radius in the air mass uses true airspeed)
        nxt = p.next_leg(self.leg)
        dpsi = abs(wrap180(p.legs[nxt].brg - L.brg))
        v = max(tas_kt(ac.ias_kt, env.da_at(ac.alt_msl_ft)), 40) * KT
        r = v * v / (G * math.tan(math.radians(self.bank)))
        lead = r * math.tan(math.radians(min(dpsi, 150) / 2))
        if dpsi > 5:
            lead += v * (self.bank / BANK_RATE_DPS) / 2
        if L.length - along <= lead:
            self.leg = nxt
            L = p.legs[self.leg]
            along, xtrk = L.project(pos)

        # lateral: desired ground track = leg bearing corrected for cross-track error
        want_trk = L.brg - clamp(xtrk * XTRK_GAIN, -35, 35)
        bank = clamp(1.6 * wrap180(want_trk - ac.track_deg), -self.bank, self.bank)

        # vertical: follow the leg's AGL profile, feed-forward the descent on descending legs
        ground = ac.alt_msl_ft - ac.agl_ft
        tgt_alt = ground + L.agl_at(along)
        vs = 6.0 * (tgt_alt - ac.alt_msl_ft) - ac.gs_kt * 101.27 * L.slope
        if self.leg == "UPWIND" and along < 0:
            # still over the runway after a touch-and-go: roll, then climb at best rate
            on_runway_m = along + p.runway_len_m
            vs = 0.0 if ac.agl_ft < 5 and on_runway_m < GROUND_ROLL_M else 9_999
        elif self.leg in ("UPWIND", "CROSSWIND") and ac.alt_msl_ft < tgt_alt - 50:
            vs = 9_999                                             # best climb (capped by DA)

        ias = LEG_IAS[self.leg] + self.ias_bias
        return bank, vs, ias
