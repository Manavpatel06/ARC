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

Live traffic (harness/scenarios/live_kdvt.json):
  "runway_flow": "auto" | "07" | "25"   auto = the end the METAR wind favours (calm -> 25)
  runway names "NORTH" / "SOUTH" / "ACTIVE" resolve against the flow: NORTH = 07L|25R, SOUTH and
  ACTIVE = 07R|25L
  start {"leg": "INBOUND", "from_deg": 0, "dist_nm": 4, "runway": "NORTH"[, "alt_msl_ft"]}: placed
  dist_nm out on that bearing from the field, flying at it straight and level (TRANSIT) at pattern
  altitude until a pilot or the AP button takes it; it joins that runway's pattern 2 NM past the field
  "traffic": {"seed": 7, "airborne": [5, 8], "runways": {"SOUTH": 0.75, "NORTH": 0.25}, "live_seed": false,
              "mix": {...}}   -> world/traffic.py TrafficGenerator (CLI --seed overrides the seed)
"""
from __future__ import annotations
import json, math

import pattern as P
from data.metar import load as load_metar, wind_vector_ms
from data.runways import load as load_runways
from schemas import Scenario
from world.flight_model import Aircraft, Env
from world.traffic import LEG_IAS, NM_M, Pattern, PatternPilot, TrafficGenerator
from world.weather import PRESETS, Weather, preset as weather_preset

STALE_BARO_PRESETS = {"pressure_drop"}
FLOW_RUNWAYS = {"07": {"NORTH": "07L", "SOUTH": "07R", "ACTIVE": "07R"},
                "25": {"NORTH": "25R", "SOUTH": "25L", "ACTIVE": "25L"}}
RESET_CLEAR_NM, RESET_CLEAR_FT = 2.0, 1500.0     # demo reset removes AI traffic this close to a judge's start
RECIP = {"07R": "25L", "25L": "07R", "07L": "25R", "25R": "07L"}   # the two ends of one strip
OCCUPIED_HALF_W_M = 40.0          # on the strip: within this of the centreline (runways 75-100 ft wide)
LANDING_CHECK_FT = 400.0          # autopilot landers decide here whether the runway is clear
DEPART_WATCH_NM = 1.5             # takeoff holds while someone lands inside this, on this strip

def scenario_flow(raw: dict, wx: dict) -> str:
    """Landing direction for the whole airport: scenario "runway_flow" ("auto" = METAR wind), else the
    direction most of the scenario's aircraft are set up for (tuned scenarios), else the wind."""
    want = str(raw.get("runway_flow", "")).strip()
    if want:
        return runway_flow(wx, want)
    idents = [str((a.get("start") or {}).get("runway", "")) for a in raw.get("aircraft", [])]
    n07 = sum(i.startswith("07") for i in idents)
    n25 = sum(i.startswith("25") for i in idents)
    if n07 or n25:
        return "07" if n07 > n25 else "25"
    return runway_flow(wx)

def runway_flow(wx: dict, want: str = "auto") -> str:
    """'07' or '25': the end with a headwind (true wind vs the 086/266 true runways); calm -> 25."""
    if want in ("07", "25"):
        return want
    wkt, wdir = float(wx.get("wind_kt", 0) or 0), float(wx.get("wind_dir_deg", 0) or 0)
    if wkt < 3:
        return "25"
    return "07" if math.cos(math.radians(wdir - 86.0)) >= 0 else "25"

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
                 weather: str | None = None, seed: int | None = None):
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
        self.flow = scenario_flow(raw, self.metar)
        self.patterns: dict[str, Pattern] = {}
        self.fleet: dict[str, Aircraft] = {}
        self.specs = {spec.id: spec for spec in self.scenario.aircraft}
        for spec in self.scenario.aircraft:
            self.fleet[spec.id] = self._spawn(spec, initial=True)
        self.humans = [a.id for a in self.fleet.values() if a.human]
        self.traffic: TrafficGenerator | None = None
        if raw.get("traffic"):
            cfg = raw["traffic"]
            self.traffic_runways = {self.runway(k): float(v) for k, v in
                                    (cfg.get("runways") or {"ACTIVE": 1.0}).items()}
            for r in self.traffic_runways:
                self.pattern(r)                            # the god view draws every active pattern
            self.traffic = TrafficGenerator(self, cfg, seed)

    # ---------- runways ----------
    def runway(self, name: str) -> str:
        """'NORTH' / 'SOUTH' / 'ACTIVE' -> the runway end in use today; real idents pass through."""
        return FLOW_RUNWAYS[self.flow].get(str(name).upper(), name)

    def pattern(self, rwy: str) -> Pattern:
        if rwy not in self.patterns:
            self.patterns[rwy] = Pattern(rwy)
        return self.patterns[rwy]

    def with_flow(self, rwy: str) -> str:
        """The end of this strip that is in use: everyone lands the same way on one runway."""
        return rwy if rwy[:2] == self.flow else RECIP.get(rwy, rwy)

    # ---------- autopilot: one landing direction, runway must be clear ----------
    def engage_ap(self, ac_id: str, on: bool) -> tuple[bool, str]:
        """Cockpit AP button. The autopilot always flies the strip's active end (never lands against the
        flow, whatever runway the scenario started the aircraft on)."""
        ac = self.fleet[ac_id]
        pl = ac.autopilot
        if on and pl is not None and hasattr(pl, "p") and pl.p.ident[:2] != self.flow:
            pl.p = self.pattern(self.with_flow(pl.p.ident))
            pl.over.clear()
        return ac.engage_ap(on)

    def _on_strip(self, R, o) -> tuple[float, float] | None:
        """(along, xtrk) if aircraft o is on the runway strip R (either direction), else None."""
        along, xtrk = R.project(P.to_enu(o.lat, o.lon))
        if -30.0 < along < R.length + 30.0 and abs(xtrk) < OCCUPIED_HALF_W_M:
            return along, xtrk
        return None

    def runway_watch(self, now: float) -> list[dict]:
        """Every aircraft the autopilot is flying (AI, judges on AP or untouched):
        - landing below 400 ft with anything on the strip - stopped, taxiing, rolling out, lifting off - goes around
        - waiting to take off holds short while the strip ahead is occupied or someone is on short final.
        Returns GO_AROUND / HOLD_SHORT world events."""
        events = []
        for ac in list(self.fleet.values()):
            pl = ac.autopilot
            if pl is None or not hasattr(pl, "p") or ac.mode != "AUTOPILOT":
                continue
            R = pl.p.legs["RUNWAY"]
            if not ac.on_ground and ac.agl_ft < LANDING_CHECK_FT and (
                    (pl.phase == "PATTERN" and (pl.leg in ("FINAL", "STRAIGHT_IN") or (pl.leg == "UPWIND" and not pl.touched)))
                    or pl.phase == "ROLLOUT"):
                for o in self.fleet.values():
                    if o is ac or not (o.on_ground or (o.agl_ft < 50 and o.vs_fpm < 100)):
                        continue
                    if self._on_strip(R, o):
                        pl.go_around()
                        events.append({"event": "GO_AROUND", "a": ac.id, "b": o.id, "runway": pl.p.ident,
                                       "reason": f"runway occupied by {o.id}", "agl_ft": round(ac.agl_ft)})
                        break
            elif pl.phase == "TAKEOFF" and ac.on_ground:
                me = R.project(P.to_enu(ac.lat, ac.lon))[0]
                why = None
                for o in self.fleet.values():
                    if o is ac:
                        continue
                    on = self._on_strip(R, o)
                    if on and on[0] > me - 30 and (o.on_ground or o.agl_ft < 50):
                        why = f"{o.id} on the runway ahead"
                    else:
                        along, xtrk = R.project(P.to_enu(o.lat, o.lon))
                        if (not o.on_ground and o.agl_ft < 600 and abs(xtrk) < 150 and o.vs_fpm < 0
                                and -DEPART_WATCH_NM * NM_M < along < R.length + DEPART_WATCH_NM * NM_M):
                            why = f"{o.id} landing"
                    if why:
                        break
                if why and ac.ias_kt < 5.0:                        # never abort a takeoff already rolling
                    if not pl.hold_short:
                        events.append({"event": "HOLD_SHORT", "a": ac.id, "runway": pl.p.ident, "reason": why})
                    pl.hold_short = True
                elif not why:
                    pl.hold_short = False
        for e in events:
            e.update(type="WORLD_EVENT", t=round(now, 3))
        return events

    # ---------- live traffic (world/traffic.py TrafficGenerator) ----------
    def add_traffic(self, ac_id: str, lat: float, lon: float, alt_msl_ft: float, hdg: float, ias: float,
                    pilot: PatternPilot, ap_equipped: bool = False) -> Aircraft:
        ac = Aircraft(ac_id, lat, lon, alt_msl_ft, ias, hdg, ap_equipped=ap_equipped, autopilot=pilot)
        self._settle(ac, stale=False)
        self.fleet[ac_id] = ac
        return ac

    def remove_traffic(self, ac_id: str) -> None:
        if ac_id not in self.specs:                        # scenario aircraft are never removed
            self.fleet.pop(ac_id, None)

    def _settle(self, ac: Aircraft, stale: bool) -> None:
        ac.agl_ft = ac.alt_msl_ft - self.env.terrain_ft(ac.lat, ac.lon)
        ac.qnh_inhg = self.env.qnh_inhg
        ac.baro_set_inhg = float(self.metar.get("altimeter_inhg", 29.92) or 29.92) if stale else self.env.qnh_inhg
        ac.on_ground = ac.agl_ft <= 0.5

    def reset_demo(self) -> tuple[list[Aircraft], list[str]]:
        """God-view RESET DEMO: every judge aircraft back to its scenario start, and AI traffic sitting on
        those starts removed. Returns (reset aircraft, removed traffic ids)."""
        reset = [self.reset_aircraft(i) for i in self.humans]
        removed = []
        if self.traffic is not None:
            for ac in reset:
                x, y = P.to_enu(ac.lat, ac.lon)
                removed += self.traffic.clear_near(x, y, ac.alt_msl_ft, RESET_CLEAR_NM * NM_M, RESET_CLEAR_FT)
        return reset, removed

    def _spawn(self, spec, initial: bool = False) -> Aircraft:
        """Build an aircraft at its scenario start (also used by reset_aircraft)."""
        st = spec.start
        rwy = self.runway(st.get("runway", "25L"))
        self.pattern(rwy)
        leg0 = st.get("leg", "DOWNWIND").upper()
        if leg0 == "INBOUND":
            return self._spawn_inbound(spec, rwy, initial)
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
        self._settle(ac, stale=initial and self.env.wx.name in STALE_BARO_PRESETS)
        return ac

    def _spawn_inbound(self, spec, rwy: str, initial: bool) -> Aircraft:
        """Free start: dist_nm out on bearing from_deg from the field, flying at the field, straight and
        level at pattern altitude (TRANSIT) until someone flies it."""
        st = spec.start
        brg, d = float(st.get("from_deg", 0.0)), float(st.get("dist_nm", 4.0)) * NM_M
        pat = self.patterns[rwy]
        alt = float(st.get("alt_msl_ft", pat.tpa_ft))
        hdg = (brg + 180.0) % 360.0
        lat, lon = P.from_enu(d * math.sin(math.radians(brg)), d * math.cos(math.radians(brg)))
        pilot = PatternPilot(pat, "DOWNWIND", spec.id)
        pilot.phase, pilot.leg, pilot.transit = "TRANSIT", "INBOUND", (hdg, alt)
        ac = Aircraft(spec.id, lat, lon, alt, 95.0, hdg, ap_equipped=spec.ap, human=spec.human, camera=spec.camera,
                      flock=spec.flock, autopilot=pilot)
        self._settle(ac, stale=initial and self.env.wx.name in STALE_BARO_PRESETS)
        return ac

    def runway_start(self, pat: Pattern, offset_s: float) -> dict:
        return self._runway_start(pat, offset_s)

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
                "time_scale": self.time_scale, "wx": self.wx_state(), "presets": sorted(PRESETS),
                "flow": self.flow, "live_traffic": self.traffic is not None, "judges": list(self.humans)}

    def wx_state(self) -> dict:
        from world.weather import PRESET_NOTES
        d = self.env.wx.public()
        d.update(da_field_ft=self.env.da_field_ft, stale_altimeters=self.stale_altimeters(),
                 note=PRESET_NOTES.get(self.env.wx.name, ""))      # thermal positions: WX_FIELD frames (world clock)
        return d
