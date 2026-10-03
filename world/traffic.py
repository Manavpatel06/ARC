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

Phases (PatternPilot.phase). AI traffic stays in PATTERN and does touch-and-goes. The cockpit AP
button (engage()) flies ONE circuit to a full stop:
  in the air:   LEVEL (wings level, hold altitude) -> JOIN (take the leg it is already lined up
                with, else fly direct to a downwind entry point at TPA) -> PATTERN -> after the
                threshold ROLLOUT (brake on the centreline) -> STOPPED
  on the ground: TAKEOFF (full power on the centreline, rotate at 55 kt) -> PATTERN -> ... -> STOPPED
"""
from __future__ import annotations
import math, zlib
from dataclasses import dataclass

import pattern as P
from world.flight_model import BANK_RATE_DPS, FT, G, KT, Aircraft, Env, clamp, tas_kt, wrap180

PATTERN_BANK = 22.0
XTRK_GAIN = 0.15               # deg of track correction per metre off course
GROUND_ROLL_M = 300.0          # touch-and-go: roll this far before rotating
AIM_POINT_M = 200.0            # final approach aiming point past the threshold
JOIN_ENTRY_FRAC = 0.35         # JOIN aims at this fraction along the downwind (midfield-ish)
JOIN_LEG_XTRK_M = 450.0        # already this close to a leg and roughly aligned -> join it directly
JOIN_LEG_HDG_DEG = 50.0
LEVEL_IAS = (80.0, 100.0)
TAKEOFF_IAS, ROTATE_IAS = 75.0, 55.0
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
        self.legs["RUNWAY"] = Leg("RUNWAY", thr, far, 0.0, 0.0)       # centreline: rollout / takeoff

    @staticmethod
    def to_enu(lat: float, lon: float) -> tuple[float, float]:
        return P.to_enu(lat, lon)

    def next_leg(self, name: str) -> str:
        if name in ("FINAL", "STRAIGHT_IN", "RUNWAY"):
            return "UPWIND"
        return CIRCUIT[CIRCUIT.index(name) + 1]

    def geometry(self) -> dict:
        """Lat/lon leg segments for the god view."""
        ll = lambda p: [round(v, 6) for v in P.from_enu(*p)]
        return {"runway": self.ident, "side": self.side, "tpa_ft": self.tpa_ft,
                "legs": {n: [ll(L.a), ll(L.b)] for n, L in self.legs.items() if n != "RUNWAY"}}

class PatternPilot:
    """Autopilot that flies the circuit forever (touch-and-goes)."""
    def __init__(self, pattern: Pattern, leg: str, ac_id: str):
        self.p = pattern
        self.leg = "UPWIND" if leg == "GO_AROUND" else leg
        h = zlib.crc32(ac_id.encode())
        self.ias_bias = (h % 9) - 4.0                 # +-4 kt per aircraft
        self.bank = PATTERN_BANK + ((h >> 8) % 5) - 2  # 20-24 deg
        self.phase = "STOPPED" if leg == "RUNWAY" else "PATTERN"
        self.full_stop = False                         # True once the AP button engaged it
        self.touched = True                            # wheels have touched since the last final

    # ---------- AP button ----------
    def engage(self, ac: Aircraft) -> str:
        """Cockpit AP button: fly one circuit to a full stop (or take off first if on the ground)."""
        self.full_stop = True
        if ac.on_ground:
            self.leg, self.phase = "RUNWAY", ("TAKEOFF" if ac.ias_kt < ROTATE_IAS else "ROLLOUT")
        else:
            self.phase = "LEVEL"
        return self.phase

    def _choose_join(self, ac: Aircraft, pos) -> None:
        """Join the leg we are already lined up with, else head for the downwind entry point."""
        for name in ("FINAL", "STRAIGHT_IN", "BASE", "DOWNWIND"):
            L = self.p.legs[name]
            along, xtrk = L.project(pos)
            if 0 < along < L.length * 0.8 and abs(xtrk) < JOIN_LEG_XTRK_M and abs(wrap180(ac.track_deg - L.brg)) < JOIN_LEG_HDG_DEG:
                self.leg, self.phase = name, "PATTERN"
                return
        self.phase = "JOIN"

    def _steer_to(self, ac: Aircraft, brg: float) -> float:
        return clamp(1.6 * wrap180(brg - ac.track_deg), -self.bank, self.bank)

    def _ground(self, ac: Aircraft, pos) -> tuple[float, float, float]:
        """ROLLOUT / STOPPED / TAKEOFF on the runway centreline (bank target = nosewheel steering)."""
        L = self.p.legs["RUNWAY"]
        along, xtrk = L.project(pos)
        steer = clamp(1.6 * wrap180(L.brg - clamp(xtrk * 0.5, -20, 20) - ac.hdg_deg), -30, 30)
        if self.phase == "TAKEOFF":
            if ac.agl_ft > 100:
                self.leg, self.phase, self.touched = "UPWIND", "PATTERN", True
                return 0.0, 9_999, LEG_IAS["UPWIND"] + self.ias_bias
            return steer if ac.on_ground else 0.0, (9_999 if ac.ias_kt >= ROTATE_IAS else 0.0), TAKEOFF_IAS
        if self.phase == "ROLLOUT" and not ac.on_ground:
            # crossed the threshold still airborne: flare and settle onto the centreline
            return clamp(2.5 * wrap180(L.brg - clamp(xtrk * 0.45, -20, 20) - ac.track_deg), -15, 15), -250.0, 60.0
        if self.phase == "ROLLOUT" and ac.ias_kt < 1.0:
            self.phase = "STOPPED"
        return steer, 0.0, 0.0                                   # brake to a stop, hold

    def targets(self, ac: Aircraft, env: Env, now: float) -> tuple[float, float, float]:
        p = self.p
        pos = p.to_enu(ac.lat, ac.lon)
        if self.phase in ("ROLLOUT", "STOPPED", "TAKEOFF"):
            return self._ground(ac, pos)
        if self.phase == "LEVEL":
            if abs(ac.bank_ctl_deg) < 3 and abs(ac.vs_fpm) < 150:      # held bank, not turbulence roll
                self._choose_join(ac, pos)
            else:
                return 0.0, 0.0, clamp(ac.ias_kt, *LEVEL_IAS)
        if self.phase == "JOIN":
            D = p.legs["DOWNWIND"]
            ue, un = math.sin(math.radians(D.brg)), math.cos(math.radians(D.brg))
            entry = (D.a[0] + ue * D.length * JOIN_ENTRY_FRAC, D.a[1] + un * D.length * JOIN_ENTRY_FRAC)
            de, dn = entry[0] - pos[0], entry[1] - pos[1]
            v = max(ac.gs_kt, 40) * KT
            r = v * v / (G * math.tan(math.radians(self.bank)))
            if math.hypot(de, dn) < 1.5 * r:                     # close enough: roll onto downwind
                self.leg, self.phase = "DOWNWIND", "PATTERN"
            else:
                vs = clamp(6.0 * (p.tpa_ft - ac.indicated_ft), -800, 9_999)      # TPA on the altimeter
                return self._steer_to(ac, math.degrees(math.atan2(de, dn)) % 360), vs, LEG_IAS["DOWNWIND"] + self.ias_bias

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
            if self.full_stop and self.leg in ("FINAL", "STRAIGHT_IN"):
                self.leg, self.phase = "RUNWAY", "ROLLOUT"        # full stop instead of touch-and-go
                return self._ground(ac, pos)
            self.leg = nxt
            if nxt in ("FINAL", "STRAIGHT_IN"):
                self.touched = False
            L = p.legs[self.leg]
            along, xtrk = L.project(pos)

        # lateral: desired ground track = leg bearing corrected for cross-track error
        short_final = (self.leg in ("FINAL", "STRAIGHT_IN") and ac.agl_ft < 400) or (self.leg == "UPWIND" and along < 0)
        gain = XTRK_GAIN * (3.0 if short_final else 1.0)           # tighter on short final: land on the centreline
        want_trk = L.brg - clamp(xtrk * gain, -35, 35)
        bank = clamp((2.5 if short_final else 1.6) * wrap180(want_trk - ac.track_deg), -self.bank, self.bank)

        # vertical: follow the leg's AGL profile, feed-forward the descent on descending legs
        ground = ac.alt_msl_ft - ac.agl_ft
        if self.leg in ("CROSSWIND", "DOWNWIND", "BASE"):
            # flown by the altimeter: published altitudes, read with this aircraft's setting
            tgt_alt, cur = p.elev_ft + L.agl_at(along), ac.indicated_ft
        else:
            # upwind / final are flown visually against the ground
            # final aims ~200 m past the threshold (crosses it at ~50 ft), like a real approach
            aim = AIM_POINT_M if self.leg in ("FINAL", "STRAIGHT_IN") else 0.0
            tgt_alt, cur = ground + L.agl_at(along - aim), ac.alt_msl_ft
        vs = 6.0 * (tgt_alt - cur) - ac.gs_kt * 101.27 * L.slope
        if self.leg == "UPWIND" and along < 0:
            # over the runway after final: flare onto the wheels, roll, then climb (touch-and-go)
            on_runway_m = along + p.runway_len_m
            self.touched = self.touched or ac.on_ground
            if not self.touched:
                vs = -150.0 - 5.0 * ac.agl_ft
            else:
                vs = 0.0 if ac.agl_ft < 5 and on_runway_m < GROUND_ROLL_M else 9_999
        elif self.leg in ("UPWIND", "CROSSWIND") and cur < tgt_alt - 50:
            vs = 9_999                                             # best climb (capped by DA)
        if self.leg in ("FINAL", "STRAIGHT_IN") and ac.agl_ft < 40:
            vs = max(vs, -150.0 - 5.0 * ac.agl_ft)                # flare: ~-350 fpm at 40 ft to -150 at the wheels

        ias = LEG_IAS[self.leg] + self.ias_bias
        return bank, vs, ias
