"""
world/separation.py — Lane A. Ground-truth separation monitor (world side only).

Checks every aircraft pair against two boxes, from TRUTH positions:
  NMAC       horizontal < 500 ft AND vertical < 100 ft   (the FLOCK nuisance/conflict box)
  COLLISION  horizontal < 60 ft  AND vertical < 30 ft    (~ C172 wingspan / height)
One event per encounter: an encounter opens when a pair enters the NMAC box and closes when
it leaves with hysteresis (h > 700 ft or v > 200 ft); the close event carries the minimum
separation seen. These are truth-derived, so they go to god + log only — never to nodes.

Used by world/world_server.py (live) and world/find_conflict.py (offline tuning).
"""
from __future__ import annotations
import itertools, math
from dataclasses import dataclass, field

FT = 0.3048
NMAC_H_FT, NMAC_V_FT = 500.0, 100.0
COLLISION_H_FT, COLLISION_V_FT = 60.0, 30.0
EXIT_H_FT, EXIT_V_FT = 700.0, 200.0

def separation_ft(a, b) -> tuple[float, float]:
    """(horizontal ft, vertical ft) between two aircraft-like objects with lat/lon/alt_msl_ft."""
    kx = 111_320.0 * math.cos(math.radians((a.lat + b.lat) / 2))
    h_m = math.hypot((a.lon - b.lon) * kx, (a.lat - b.lat) * 111_320.0)
    return h_m / FT, abs(a.alt_msl_ft - b.alt_msl_ft)

def severity(h_ft: float, v_ft: float) -> float:
    """< 1 means inside the NMAC box; smaller is closer. Used to rank encounters."""
    return max(h_ft / NMAC_H_FT, v_ft / NMAC_V_FT)

@dataclass
class Encounter:
    a: str
    b: str
    t0: float
    min_h_ft: float = float("inf")
    min_v_ft: float = float("inf")
    min_t: float = 0.0
    collided: bool = False
    legs: tuple = ()
    where: dict = field(default_factory=dict)

class SeparationMonitor:
    def __init__(self):
        self.open: dict[tuple[str, str], Encounter] = {}
        self.counts = {"NMAC": 0, "COLLISION": 0}

    def check(self, fleet: dict, now: float) -> list[dict]:
        """Run once per tick over {id: Aircraft}. Returns WORLD_EVENT frames to publish."""
        events = []
        for a, b in itertools.combinations(sorted(fleet.values(), key=lambda x: x.id), 2):
            key = (a.id, b.id)
            h, v = separation_ft(a, b)
            enc = self.open.get(key)
            if enc is None:
                if h < NMAC_H_FT and v < NMAC_V_FT:
                    enc = self.open[key] = Encounter(a.id, b.id, now)
                    self.counts["NMAC"] += 1
                    events.append(self._event("NMAC", a, b, h, v, now))
                else:
                    continue
            if severity(h, v) < severity(enc.min_h_ft, enc.min_v_ft):
                enc.min_h_ft, enc.min_v_ft, enc.min_t = h, v, now
                enc.legs = (self._leg(a), self._leg(b))
                enc.where = {"lat": round((a.lat + b.lat) / 2, 6), "lon": round((a.lon + b.lon) / 2, 6),
                             "alt_msl_ft": round((a.alt_msl_ft + b.alt_msl_ft) / 2)}
            if not enc.collided and h < COLLISION_H_FT and v < COLLISION_V_FT:
                enc.collided = True
                self.counts["COLLISION"] += 1
                events.append(self._event("COLLISION", a, b, h, v, now))
            if h > EXIT_H_FT or v > EXIT_V_FT:
                del self.open[key]
                events.append({"type": "WORLD_EVENT", "event": "NMAC_END", "a": enc.a, "b": enc.b, "t": round(now, 3),
                               "min_h_ft": round(enc.min_h_ft), "min_v_ft": round(enc.min_v_ft),
                               "at_t": round(enc.min_t, 3), "duration_s": round(now - enc.t0, 1),
                               "collided": enc.collided, "legs": list(enc.legs), **enc.where})
        return events

    @staticmethod
    def _leg(ac):
        return getattr(ac.autopilot, "leg", None) if ac.autopilot else None

    def _event(self, kind, a, b, h, v, now) -> dict:
        return {"type": "WORLD_EVENT", "event": kind, "a": a.id, "b": b.id, "t": round(now, 3),
                "h_ft": round(h), "v_ft": round(v), "legs": [self._leg(a), self._leg(b)],
                "modes": [a.mode, b.mode],
                "lat": round((a.lat + b.lat) / 2, 6), "lon": round((a.lon + b.lon) / 2, 6),
                "alt_msl_ft": round((a.alt_msl_ft + b.alt_msl_ft) / 2), "counts": dict(self.counts)}
