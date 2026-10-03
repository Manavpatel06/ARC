"""
world/scenario.py — Lane A. Load harness/scenarios/*.json and spawn the fleet.

Aircraft start = {"leg", "runway", "offset_s", optional "agl_ft"}; offset_s walks along the
pattern from the start of the leg (negative = backwards). Optional top-level keys beyond
schemas.Scenario: "time_scale" (1 for judges, 10 for A/B runs) and
"density_altitude_override" (ft); CLI flags override both.
"""
from __future__ import annotations
import json, os

from schemas import Scenario
from world.flight_model import Aircraft, Env
from world.traffic import LEG_IAS, Pattern, PatternPilot, load_airport

_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "cache")

def load_metar() -> dict:
    for name in ("metar_kdvt.json", "metar_kdvt.sample.json"):
        p = os.path.join(_CACHE, name)
        if os.path.exists(p):
            with open(p) as f:
                return json.load(f)
    return {}

def make_env(airport: dict, metar: dict, da_override: float | None) -> Env:
    try:
        from data.terrain import elev_at_ft
        terrain = elev_at_ft
    except Exception:                     # numpy/cache missing: flat field
        elev = float(airport["elev_ft"])
        terrain = lambda lat, lon: elev
    return Env(field_elev_ft=float(airport["elev_ft"]),
               da_field_ft=float(da_override if da_override is not None else metar.get("density_altitude_ft", airport["elev_ft"])),
               wind_from_deg=float(metar.get("wind_dir_deg", 0) or 0),
               wind_kt=float(metar.get("wind_kt", 0) or 0),
               terrain_ft=terrain)

class World:
    """Everything the server needs from a scenario."""
    def __init__(self, path: str | None, da_override: float | None = None, time_scale: float | None = None):
        raw = {"name": "default", "aircraft": [
            {"id": "N101", "start": {"leg": "DOWNWIND", "runway": "25L", "offset_s": 0}, "ap": True, "human": True},
            {"id": "N204", "start": {"leg": "BASE", "runway": "25L", "offset_s": -10}, "ap": False}]}
        if path:
            with open(path) as f:
                raw = json.load(f)
        self.raw = raw
        self.scenario = Scenario.model_validate(raw)
        self.time_scale = float(time_scale if time_scale is not None else raw.get("time_scale", 1.0))
        if da_override is None:
            da_override = raw.get("density_altitude_override")
        self.airport = load_airport()
        self.metar = load_metar()
        self.env = make_env(self.airport, self.metar, da_override)
        self.patterns: dict[str, Pattern] = {}
        self.fleet: dict[str, Aircraft] = {}
        for spec in self.scenario.aircraft:
            st = spec.start
            rwy = st.get("runway", "25L")
            if rwy not in self.patterns:
                self.patterns[rwy] = Pattern(self.airport, rwy)
            pat = self.patterns[rwy]
            leg0 = st.get("leg", "DOWNWIND")
            leg, (e, n), along = pat.place(leg0, float(st.get("offset_s", 0)), LEG_IAS.get(leg0, 90))
            lat, lon = pat.local.to_ll(e, n)
            alt = pat.alt_profile_ft(leg, along)
            if "agl_ft" in st:
                alt = self.env.terrain_ft(lat, lon) + float(st["agl_ft"])
            pilot = PatternPilot(pat, leg, spec.id)
            ac = Aircraft(spec.id, lat, lon, alt, LEG_IAS[leg] + pilot.ias_bias, pat.legs[leg].brg,
                          ap_equipped=spec.ap, human=spec.human, camera=spec.camera, flock=spec.flock,
                          autopilot=pilot)
            self.fleet[spec.id] = ac
        self.humans = [a.id for a in self.fleet.values() if a.human]

    def cockpit_id(self, slot: str) -> str | None:
        """'A' -> first human aircraft, 'B' -> second; or an explicit aircraft id."""
        if slot in self.fleet:
            return slot
        idx = {"A": 0, "B": 1}.get(slot.upper())
        if idx is not None and idx < len(self.humans):
            return self.humans[idx]
        return None

    def static(self) -> dict:
        """Static picture for the god view: airport, pattern legs, weather."""
        return {"airport": self.airport, "patterns": {k: p.geometry() for k, p in self.patterns.items()},
                "metar": self.metar, "da_field_ft": self.env.da_field_ft, "scenario": self.scenario.name,
                "time_scale": self.time_scale}
