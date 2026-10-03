"""
world/scenario.py — Lane A. Load harness/scenarios/*.json and spawn the fleet.

Aircraft start = {"leg", "runway", "offset_s", optional "agl_ft"} -> pattern.place() (shared with
Lane B). Lane A extension (proposed for INTERFACE v1.2): {"leg": "RUNWAY", "runway": "25L",
"offset_s": s} starts the aircraft stopped on the centreline, s seconds of 60 kt roll past the
threshold (default 2 s, ~60 m). AI aircraft take off at once; human aircraft hold until the pilot
adds power or presses AP. Optional top-level keys beyond schemas.Scenario: "time_scale" (1 for judges, 10 for A/B
runs), "density_altitude_override" (ft) and "weather_preset" (world/weather.py PRESETS, default
"metar"); CLI flags override all three. Density altitude: CLI/scenario override > preset > METAR.
Aircraft spawn with their altimeter set to the current QNH, except in "pressure_drop", where they
still have the METAR setting (stale) to show the altimeter trap.
"""
from __future__ import annotations
import json, math

import pattern as P
from data.metar import load as load_metar, wind_vector_ms
from data.runways import load as load_runways
from schemas import Scenario
from world.flight_model import Aircraft, Env
from world.traffic import LEG_IAS, Pattern, PatternPilot
from world.weather import PRESETS, Weather, preset as weather_preset

STALE_BARO_PRESETS = {"pressure_drop"}

def make_env(airport: dict, wx: dict, da_override: float | None) -> Env:
    try:
        from data.terrain import elev_at_ft
        terrain = elev_at_ft
    except Exception:                     # numpy/cache missing: flat field
        elev = float(airport["elev_ft"])
        terrain = lambda lat, lon: elev
    we, wn = wind_vector_ms(wx)
    da = da_override if da_override is not None else wx.get("density_altitude_ft", airport["elev_ft"])
    return Env(field_elev_ft=float(airport["elev_ft"]), da_field_ft=float(da),
               wind_e_ms=we, wind_n_ms=wn, terrain_ft=terrain)

class World:
    """Everything the server needs from a scenario."""
    def __init__(self, path: str | dict | None, da_override: float | None = None, time_scale: float | None = None,
                 weather: str | None = None):
        """path: scenario file, or an already-loaded scenario dict (world/find_conflict.py)."""
        raw = {"name": "default", "aircraft": [
            {"id": "N101", "start": {"leg": "DOWNWIND", "runway": "25L", "offset_s": 0}, "ap": True, "human": True},
            {"id": "N204", "start": {"leg": "BASE", "runway": "25L", "offset_s": -10}, "ap": False}]}
        if isinstance(path, dict):
            raw = path
        elif path:
            with open(path) as f:
                raw = json.load(f)
        self.raw = raw
        self.scenario = Scenario.model_validate(raw)
        self.time_scale = float(time_scale if time_scale is not None else raw.get("time_scale", 1.0))
        if da_override is None:
            da_override = raw.get("density_altitude_override")
        self.airport = load_runways()
        self.metar = load_metar(self.scenario.weather)            # "cached" pins the committed sample (tuned scenarios), "live" = fetched METAR
        self.env = make_env(self.airport, self.metar, da_override)
        self.env.ref_lat, self.env.ref_lon = float(self.airport["lat"]), float(self.airport["lon"])
        from world.taws import Taws
        self.runways = Taws(self.airport)                  # runway surfaces (brakes only work on a runway)
        self.env.runway_at = self.runways.on_runway
        self.da_pinned = da_override is not None
        self.set_preset(weather or raw.get("weather_preset", "metar"))
        self.patterns: dict[str, Pattern] = {}
        self.fleet: dict[str, Aircraft] = {}
        self.specs = {spec.id: spec for spec in self.scenario.aircraft}
        for spec in self.scenario.aircraft:
            self.fleet[spec.id] = self._spawn(spec, initial=True)
        self.humans = [a.id for a in self.fleet.values() if a.human]

    def _spawn(self, spec, initial: bool = False) -> Aircraft:
        """Build an aircraft at its scenario start (also used by reset_aircraft)."""
        st = spec.start
        rwy = st.get("runway", "25L")
        if rwy not in self.patterns:
            self.patterns[rwy] = Pattern(rwy)
        leg0 = st.get("leg", "DOWNWIND").upper()
        if leg0 == "RUNWAY":
            pos = self._runway_start(self.patterns[rwy], float(st.get("offset_s", 2.0)))
        else:
            ias0 = LEG_IAS.get(leg0, 90)
            pos = P.place(leg0, rwy, float(st.get("offset_s", 0)), ias0, st.get("agl_ft"))
        pilot = PatternPilot(self.patterns[rwy], pos["leg"], spec.id)
        ias = 0.0 if pos["leg"] == "RUNWAY" else LEG_IAS[pos["leg"]] + pilot.ias_bias
        ac = Aircraft(spec.id, pos["lat"], pos["lon"], pos["alt_msl_ft"], ias,
                      pos["hdg_deg"], ap_equipped=spec.ap, human=spec.human, camera=spec.camera,
                      flock=spec.flock, autopilot=pilot)
        if pos["leg"] == "RUNWAY" and not spec.human:
            pilot.phase = "TAKEOFF"                        # AI departs straight away
        ac.agl_ft = ac.alt_msl_ft - self.env.terrain_ft(ac.lat, ac.lon)
        ac.qnh_inhg = self.env.qnh_inhg
        stale = initial and self.env.wx.name in STALE_BARO_PRESETS
        ac.baro_set_inhg = float(self.metar.get("altimeter_inhg", 29.92) or 29.92) if stale else self.env.qnh_inhg
        ac.on_ground = ac.agl_ft <= 0.5
        return ac

    def reset_aircraft(self, ac_id: str) -> Aircraft:
        """Put one aircraft back at its scenario start: fresh autopilot, no AP / takeover / pilot input.
        It flies the pattern on its autopilot until the pilot touches the controls again."""
        ac = self._spawn(self.specs[ac_id])
        self.fleet[ac_id] = ac
        return ac

    def _runway_start(self, pat: Pattern, offset_s: float) -> dict:
        L = pat.legs["RUNWAY"]
        d = max(0.0, offset_s) * 60 * 0.514444
        ue, un = math.sin(math.radians(L.brg)), math.cos(math.radians(L.brg))
        lat, lon = P.from_enu(L.a[0] + ue * d, L.a[1] + un * d)
        return {"lat": lat, "lon": lon, "alt_msl_ft": self.env.terrain_ft(lat, lon), "hdg_deg": L.brg, "leg": "RUNWAY"}

    # ---------- weather ----------
    def set_preset(self, name: str) -> Weather:
        self.env.wx = weather_preset(name, self.metar)
        self._apply_da()
        return self.env.wx

    def set_weather(self, **kw) -> list[str]:
        """Partial weather change (god view SET_WX). Aircraft keep their altimeter settings."""
        changed = self.env.wx.update(**kw)
        if changed:
            self.env.wx.name = "custom"
        if "da_ft" in changed:
            self.da_pinned = False
            self._apply_da()
        return changed

    def _apply_da(self):
        if not self.da_pinned and self.env.wx.da_ft is not None:
            self.env.da_field_ft = float(self.env.wx.da_ft)

    def update_altimeters(self) -> int:
        """Everyone dials in the current QNH (ATIS update). Returns how many were off."""
        n = 0
        for ac in self.fleet.values():
            if abs(ac.baro_set_inhg - self.env.qnh_inhg) > 0.005:
                n += 1
            ac.baro_set_inhg = self.env.qnh_inhg
        return n

    def stale_altimeters(self) -> int:
        return sum(abs(ac.baro_set_inhg - self.env.qnh_inhg) > 0.005 for ac in self.fleet.values())

    def cockpit_id(self, slot: str) -> str | None:
        """'A' -> first human aircraft, 'B' -> second; or an explicit aircraft id (INTERFACE v1.1)."""
        if slot in self.fleet:
            return slot
        idx = {"A": 0, "B": 1}.get(slot.upper())
        if idx is not None and idx < len(self.humans):
            return self.humans[idx]
        return None

    def roster(self) -> list[dict]:
        """HELLO aircraft list: ids and flags only, no positions."""
        return [{"id": a.id, "human": a.human, "ap": a.ap_equipped, "flock": a.flock} for a in self.fleet.values()]

    def static(self) -> dict:
        """Static picture for the god view and cockpit charts: airport, pattern legs, weather."""
        return {"airport": self.airport, "patterns": {k: p.geometry() for k, p in self.patterns.items()},
                "metar": self.metar, "da_field_ft": self.env.da_field_ft, "scenario": self.scenario.name,
                "time_scale": self.time_scale, "wx": self.wx_state(), "presets": sorted(PRESETS)}

    def wx_state(self) -> dict:
        from world.weather import PRESET_NOTES
        d = self.env.wx.public()
        d.update(da_field_ft=self.env.da_field_ft, stale_altimeters=self.stale_altimeters(),
                 note=PRESET_NOTES.get(self.env.wx.name, ""))      # thermal positions: WX_FIELD frames (world clock)
        return d
