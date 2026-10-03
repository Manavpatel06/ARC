"""
world/taws.py — Lane A. Simplified terrain awareness (GPWS-style) for the cockpits.

Uses only own-aircraft data (radio altitude = agl_ft, sink rate, own position against the
published runway layout), like a real GPWS / TAWS box — never other traffic.

Alerts (highest wins), sink rate smoothed over ~2 s like a real box (turbulence bumps don't trip it):
  PULL UP          sink > 1,600 + 1.8 x AGL fpm, or < 10 s to ground at > 500 fpm outside a runway corridor
  SINK RATE        sink > 1,000 + 1.6 x AGL fpm (GPWS mode-1 style envelope), below 2,500 ft AGL
  TERRAIN          below 500 ft AGL, descending > 300 fpm, outside a runway corridor, away from the airport
  TOO LOW TERRAIN  below 300 ft AGL away from the airport, or below 150 ft AGL near it, outside a runway
                   corridor and not climbing
Runway corridor = lined up (within 40 deg) with any runway: on approach from 6 km out (150 m + 10 % of the
distance wide) through the runway, or on climb-out up to 4 km past its far end. "Near the airport" = within
1.5 NM of a runway (terrain modes inhibited there, as real TAWS does for the destination field).

Touchdown classification: on a runway surface (+15 m margin) or OFF_RUNWAY; TERRAIN_IMPACT if
the sink was harder than 1,000 fpm or the speed above 80 kt away from a runway.
"""
from __future__ import annotations
import math
from dataclasses import dataclass

M_PER_DEG = 111_320.0
FT = 0.3048

@dataclass
class RunwayEnd:
    ident: str
    e: float          # threshold, local metres
    n: float
    ue: float         # unit vector along the landing direction
    un: float
    length_m: float
    half_width_m: float
    hdg: float

AIRPORT_RADIUS_M = 1.5 * 1852
SINK_TAU_S = 2.0

class Taws:
    def __init__(self, airport: dict):
        self._sink: dict[str, float] = {}           # smoothed sink per aircraft
        self.lat0, self.lon0 = float(airport["lat"]), float(airport["lon"])
        self.kx = M_PER_DEG * math.cos(math.radians(self.lat0))
        self.ends: list[RunwayEnd] = []
        for ident, e in (airport.get("ends") or {}).items():
            x0, y0 = self._enu(e["lat"], e["lon"]); x1, y1 = self._enu(e["far_lat"], e["far_lon"])
            L = math.hypot(x1 - x0, y1 - y0) or 1.0
            self.ends.append(RunwayEnd(ident, x0, y0, (x1 - x0) / L, (y1 - y0) / L, L,
                                       float(e.get("width_ft", 75)) * FT / 2, math.degrees(math.atan2(x1 - x0, y1 - y0)) % 360))

    def _enu(self, lat: float, lon: float) -> tuple[float, float]:
        return (lon - self.lon0) * self.kx, (lat - self.lat0) * M_PER_DEG

    def _frame(self, ac, r: RunwayEnd) -> tuple[float, float]:
        x, y = self._enu(ac.lat, ac.lon)
        dx, dy = x - r.e, y - r.n
        return dx * r.ue + dy * r.un, dx * r.un - dy * r.ue       # along, right-of-centreline

    def on_runway(self, ac) -> str | None:
        """Runway end ident under the aircraft (the end it is rolling toward, e.g. 25L not 07R), or None."""
        best, best_d = None, 999.0
        for r in self.ends:
            u, v = self._frame(ac, r)
            if -15 <= u <= r.length_m + 15 and abs(v) <= r.half_width_m + 15:
                d = abs((getattr(ac, "hdg_deg", r.hdg) - r.hdg + 180) % 360 - 180)
                if d < best_d:
                    best, best_d = r.ident, d
        return best

    def in_corridor(self, ac) -> bool:
        for r in self.ends:
            u, v = self._frame(ac, r)
            hd = abs((ac.track_deg - r.hdg + 180) % 360 - 180)
            if hd >= 40:
                continue
            if -6000 <= u <= r.length_m and abs(v) < 150 + 0.1 * max(0.0, -u):          # approach
                return True
            if 0 <= u <= r.length_m + 4000 and abs(v) < 150 + 0.1 * max(0.0, u - r.length_m):   # climb-out
                return True
        return False

    def near_airport(self, ac) -> bool:
        for r in self.ends:
            u, v = self._frame(ac, r)
            du = 0.0 if 0 <= u <= r.length_m else min(abs(u), abs(u - r.length_m))
            if math.hypot(du, v) < AIRPORT_RADIUS_M:
                return True
        return False

    def forget(self, ac_id: str) -> None:
        self._sink.pop(ac_id, None)

    def sink_fpm(self, ac, dt: float) -> float:
        """Smoothed sink rate (positive = descending); call once per tick per aircraft."""
        raw = -min(0.0, ac.vs_fpm + getattr(ac, "vs_air_fpm", 0.0))
        s = self._sink.get(ac.id, raw)
        s += (raw - s) * min(1.0, dt / SINK_TAU_S)
        self._sink[ac.id] = s
        return s

    def alert(self, ac, dt: float = 0.1) -> str | None:
        sink = self.sink_fpm(ac, dt)
        if ac.on_ground or ac.agl_ft > 2500:
            return None
        agl = max(0.0, ac.agl_ft)
        corridor = self.in_corridor(ac)
        near = self.near_airport(ac)
        tti = agl / (sink / 60.0) if sink > 1 else 1e9
        if sink > 1600 + 1.8 * agl or (sink > 500 and tti < 10 and not corridor):
            return "PULL UP"
        if sink > 1000 + 1.6 * agl:
            return "SINK RATE"
        if not corridor and not near and agl < 500 and sink > 300:
            return "TERRAIN"
        if not corridor and sink > -100 and (agl < 150 or (agl < 300 and not near)):
            return "TOO LOW TERRAIN"
        return None

    def touchdown(self, ac, sink_fpm: float) -> str:
        if self.on_runway(ac):
            return "RUNWAY"
        return "TERRAIN_IMPACT" if sink_fpm < -1000 or ac.ias_kt > 80 else "OFF_RUNWAY"
