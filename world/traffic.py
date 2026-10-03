"""
world/traffic.py — Lane A. Traffic-pattern geometry and the pattern autopilot.

Pattern for a landing runway (e.g. 25L, left traffic), corners in local ENU metres:
  P0 threshold (touchdown) -> P1 upwind turn (past the departure end) -> P2 downwind start
  -> P3 base turn (abeam a point FINAL_EXT_M before the threshold) -> P4 final turn -> P0
Legs: UPWIND P0-P1, CROSSWIND P1-P2, DOWNWIND P2-P3, BASE P3-P4, FINAL P4-P0, then
touch-and-go back onto UPWIND. STRAIGHT_IN flies the extended centreline to P0, then joins.

The autopilot steers a ground TRACK along each leg (cross-track correction, so wind cannot
make it drift), banks ~22 deg in pattern turns, leads each turn by r*tan(dpsi/2), holds
pattern altitude, and flies a 3 deg glide path on final. It only produces targets; the
limits (bank rate, climb vs density altitude, speed envelope) live in flight_model.
"""
from __future__ import annotations
import json, math, os, zlib
from dataclasses import dataclass

from world.flight_model import BANK_RATE_DPS, FT, G, KT, M_PER_DEG_LAT, Aircraft, Env, clamp, tas_kt, wrap180

DOWNWIND_OFFSET_M = 1_500.0     # ~0.8 nm abeam
UPWIND_EXT_M = 900.0            # turn crosswind this far past the departure end
FINAL_EXT_M = 2_400.0           # final ~1.3 nm -> ~410 ft AGL at the final turn on a 3 deg path
STRAIGHT_IN_M = 7_000.0         # straight-in starts ~3.8 nm out
GLIDE_DEG = 3.0
PATTERN_BANK = 22.0
XTRK_GAIN = 0.15               # deg of track correction per metre off course
DESCENT_START_AGL = 300.0      # downwind: descend this much after abeam the threshold
LEG_IAS = {"UPWIND": 75, "CROSSWIND": 85, "DOWNWIND": 95, "BASE": 85, "FINAL": 72, "STRAIGHT_IN": 80}
PATTERN_LEGS = ["UPWIND", "CROSSWIND", "DOWNWIND", "BASE", "FINAL"]

_HERE = os.path.dirname(os.path.abspath(__file__))
_CACHE = os.path.join(_HERE, "..", "data", "cache")

def load_airport() -> dict:
    """runways_kdvt.json from Lane D if present, else the committed sample."""
    for name in ("runways_kdvt.json", "runways_kdvt.sample.json"):
        p = os.path.join(_CACHE, name)
        if os.path.exists(p):
            with open(p) as f:
                return json.load(f)
    raise FileNotFoundError("data/cache/runways_kdvt*.json missing")

class Local:
    """Flat-earth ENU frame around the airport reference point."""
    def __init__(self, lat0: float, lon0: float):
        self.lat0, self.lon0 = lat0, lon0
        self.kx = M_PER_DEG_LAT * math.cos(math.radians(lat0))

    def to_enu(self, lat: float, lon: float) -> tuple[float, float]:
        return (lon - self.lon0) * self.kx, (lat - self.lat0) * M_PER_DEG_LAT

    def to_ll(self, e: float, n: float) -> tuple[float, float]:
        return self.lat0 + n / M_PER_DEG_LAT, self.lon0 + e / self.kx

def _unit(brg_deg: float) -> tuple[float, float]:
    return math.sin(math.radians(brg_deg)), math.cos(math.radians(brg_deg))

def _add(p, u, d):
    return (p[0] + u[0] * d, p[1] + u[1] * d)

def _brg(a, b) -> float:
    return math.degrees(math.atan2(b[0] - a[0], b[1] - a[1])) % 360.0

@dataclass
class Leg:
    name: str
    a: tuple[float, float]
    b: tuple[float, float]

    @property
    def brg(self) -> float:
        return _brg(self.a, self.b)

    @property
    def length(self) -> float:
        return math.hypot(self.b[0] - self.a[0], self.b[1] - self.a[1])

    def project(self, p) -> tuple[float, float]:
        """(along-track m from a, cross-track m; + = right of course)."""
        ue, un = _unit(self.brg)
        de, dn = p[0] - self.a[0], p[1] - self.a[1]
        return de * ue + dn * un, de * un - dn * ue

class Pattern:
    """Pattern geometry for one landing runway ident (e.g. '25L')."""
    def __init__(self, airport: dict, ident: str):
        self.ident = ident
        self.local = Local(airport["lat"], airport["lon"])
        self.elev_ft = float(airport["elev_ft"])
        self.tpa_ft = self.elev_ft + float(airport.get("tpa_agl_ft", 1_000))
        rwy = next(r for r in airport["runways"] if ident in r["ends"])
        other = next(k for k in rwy["ends"] if k != ident)
        thr = self.local.to_enu(rwy["ends"][ident]["lat"], rwy["ends"][ident]["lon"])
        dep = self.local.to_enu(rwy["ends"][other]["lat"], rwy["ends"][other]["lon"])
        self.landing_brg = _brg(thr, dep)
        side = rwy.get("pattern", {}).get(ident, "left")
        self.side = side
        u = _unit(self.landing_brg)
        s = _unit(self.landing_brg + (-90 if side == "left" else 90))
        p0 = thr
        p1 = _add(dep, u, UPWIND_EXT_M)
        p2 = _add(p1, s, DOWNWIND_OFFSET_M)
        p4 = _add(thr, u, -FINAL_EXT_M)
        p3 = _add(p4, s, DOWNWIND_OFFSET_M)
        self.thr, self.dep = thr, dep
        self.corners = [p0, p1, p2, p3, p4]
        self.legs = {n: Leg(n, self.corners[i], self.corners[(i + 1) % 5]) for i, n in enumerate(PATTERN_LEGS)}
        self.legs["STRAIGHT_IN"] = Leg("STRAIGHT_IN", _add(thr, u, -STRAIGHT_IN_M), thr)

    def next_leg(self, name: str) -> str:
        if name in ("FINAL", "STRAIGHT_IN"):
            return "UPWIND"
        return PATTERN_LEGS[PATTERN_LEGS.index(name) + 1]

    def prev_leg(self, name: str) -> str:
        return "FINAL" if name in ("UPWIND", "STRAIGHT_IN") else PATTERN_LEGS[PATTERN_LEGS.index(name) - 1]

    def glide_alt_ft(self, dist_to_thr_m: float) -> float:
        return self.elev_ft + max(0.0, dist_to_thr_m) * math.tan(math.radians(GLIDE_DEG)) / FT

    def alt_profile_ft(self, leg: str, along_m: float) -> float:
        """Nominal altitude on a leg at along-track distance (used for spawn and as AP target)."""
        L = self.legs[leg]
        if leg == "UPWIND":
            return min(self.tpa_ft - 200, self.elev_ft + max(0.0, along_m - 600) * 0.12 / FT)
        if leg == "CROSSWIND":
            return self.tpa_ft - 100
        if leg == "DOWNWIND":
            # level until abeam the threshold, then start down (power back, ~300 ft by the base turn)
            abeam = L.length - FINAL_EXT_M
            frac = clamp((along_m - abeam) / FINAL_EXT_M, 0, 1)
            return self.tpa_ft - DESCENT_START_AGL * frac
        if leg == "BASE":
            frac = clamp(along_m / L.length, 0, 1)
            top = self.tpa_ft - DESCENT_START_AGL
            return top + (self.glide_alt_ft(FINAL_EXT_M) - top) * frac
        return min(self.tpa_ft, self.glide_alt_ft(L.length - along_m))   # FINAL / STRAIGHT_IN

    def place(self, leg: str, offset_s: float, ias_kt: float) -> tuple[str, tuple[float, float], float]:
        """Walk offset_s * ias along the pattern from the start of `leg` (negative = backwards).
        Returns (leg, ENU point, along-track m on that leg)."""
        d = offset_s * ias_kt * KT
        name = leg
        for _ in range(20):
            L = self.legs[name]
            if d < 0 and name != "STRAIGHT_IN":
                name = self.prev_leg(name)
                d += self.legs[name].length
                continue
            if d > L.length:
                d -= L.length
                name = self.next_leg(name)
                continue
            break
        L = self.legs[name]
        return name, _add(L.a, _unit(L.brg), d), d

    def geometry(self) -> dict:
        """Lat/lon corners for the god view."""
        ll = lambda p: [round(v, 6) for v in self.local.to_ll(*p)]
        return {"runway": self.ident, "side": self.side, "tpa_ft": self.tpa_ft,
                "legs": {n: [ll(L.a), ll(L.b)] for n, L in self.legs.items()}}

class PatternPilot:
    """Autopilot that flies the pattern legs forever (touch-and-goes)."""
    def __init__(self, pattern: Pattern, leg: str, ac_id: str):
        self.p = pattern
        self.leg = leg
        h = zlib.crc32(ac_id.encode())
        self.ias_bias = (h % 9) - 4.0                 # +-4 kt per aircraft
        self.bank = PATTERN_BANK + ((h >> 8) % 5) - 2  # 20-24 deg

    def targets(self, ac: Aircraft, env: Env, now: float) -> tuple[float, float, float]:
        p = self.p
        pos = p.local.to_enu(ac.lat, ac.lon)
        L = p.legs[self.leg]
        along, xtrk = L.project(pos)

        # leg sequencing: lead the turn by r * tan(dpsi/2) plus the distance flown while rolling in
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

        # vertical
        tgt_alt = p.alt_profile_ft(self.leg, along)
        vs = 6.0 * (tgt_alt - ac.alt_msl_ft)
        if self.leg in ("FINAL", "STRAIGHT_IN", "BASE") and ac.alt_msl_ft < p.tpa_ft + 50:
            # feed-forward the descent rate of the glide path at current ground speed
            vs -= ac.gs_kt * 101.27 * math.tan(math.radians(GLIDE_DEG))
        if self.leg == "UPWIND" and along < 600:
            vs = 0.0 if ac.agl_ft < 5 and along < 300 else 9_999   # roll, then full climb
        elif self.leg in ("UPWIND", "CROSSWIND") and ac.alt_msl_ft < tgt_alt - 50:
            vs = 9_999                                                # best climb (capped by DA)

        ias = LEG_IAS[self.leg] + self.ias_bias
        return bank, vs, ias
