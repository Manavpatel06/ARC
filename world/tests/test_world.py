"""
world/tests/test_world.py — Lane A regression suite (physics, pattern AI, autopilot, ground, TAWS, weather,
separation, scenarios). Pure in-process simulation, no network.  Run:  python -m pytest -q world/tests
"""
from __future__ import annotations
import collections, json, math, sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pattern as P                                         # noqa: E402
from world.flight_model import (Aircraft, Env, climb_capability_fpm, move, BANK_RATE_DPS,  # noqa: E402
                                CMD_MAX_BANK, CMD_MAX_HOLD_S)
from world.scenario import World                             # noqa: E402
from world.separation import SeparationMonitor               # noqa: E402
from world.taws import Taws                                  # noqa: E402

DT = 0.05
SCN = ROOT / "harness" / "scenarios"

def scn(name: str, **kw) -> World:
    """Scenario with the committed sample weather: results must not depend on whatever METAR
    `python data/metar.py` fetched last (judges.json says "live")."""
    raw = json.load(open(SCN / name))
    raw["weather"] = "cached"
    return World(raw, **kw)

def fly(w: World, ids, secs: float, t0: float = 0.0, each=None) -> float:
    t = t0
    for _ in range(int(secs / DT)):
        for i in ids:
            w.fleet[i].step(DT, w.env, t)
        t += DT
        if each:
            each(t)
    return t

def put_on_runway(w: World, ac: Aircraft, ias: float = 0.0, rwy: str = "25L") -> None:
    L = w.patterns[rwy].legs["RUNWAY"]
    ac.lat, ac.lon = P.from_enu(*L.a)
    ac.alt_msl_ft = w.env.terrain_ft(ac.lat, ac.lon)
    ac.agl_ft, ac.vs_fpm, ac.ias_kt, ac.hdg_deg = 0.0, 0.0, ias, L.brg
    ac.bank_deg = ac.bank_ctl_deg = 0.0
    ac.on_ground, ac._prev_tail_ms = True, None
    ac.events.clear()

# ---------------------------------------------------------------- physics
def test_climb_table_matches_spec():
    assert climb_capability_fpm(0) == pytest.approx(730)
    assert climb_capability_fpm(5000) == pytest.approx(500)
    assert climb_capability_fpm(8000) == pytest.approx(300)

def test_turn_rate_is_g_tan_bank_over_v():
    env = Env(field_elev_ft=0, da_field_ft=0, terrain_ft=lambda a, b: 0)
    ac = Aircraft("T", 33.7, -112.0, 0.0, 90.0, 0.0, bank_deg=30.0, ap_equipped=True)
    ac.bank_ctl_deg = 30.0
    ac.cmd, ac.cmd_until = {"mode": "TAKEOVER", "bank_cmd_deg": 30}, 1e9
    ac.alt_msl_ft = ac.agl_ft = 3000.0
    h0 = ac.hdg_deg
    for _ in range(20):
        ac.step(DT, env, 0.0)
    v = 90 * 0.514444 * (1 + 0.02 * 3.0)                      # TAS at DA 3,000
    expect = math.degrees(9.80665 * math.tan(math.radians(30)) / v)
    assert (ac.hdg_deg - h0) == pytest.approx(expect, rel=0.05)

def test_bank_rate_limited_to_15_deg_s():
    w = scn("judges.json"); a = w.fleet["N102"]
    banks = []
    def rec(t):
        a.apply_input(1.0, 0, 0.5, t); banks.append(a.bank_ctl_deg)
    fly(w, ["N102"], 1.0, each=rec)
    rates = [(b2 - b1) / DT for b1, b2 in zip(banks, banks[1:])]
    assert max(abs(r) for r in rates) <= BANK_RATE_DPS + 1e-6

# ---------------------------------------------------------------- pattern AI
def test_ai_pattern_ten_minutes_laps_tracking_and_landings():
    w = scn("judges.json"); tw = Taws(w.airport)
    laps = collections.Counter(); prev = {}; worst = 0.0; touchdowns = []
    def rec(t):
        nonlocal worst
        for a in w.fleet.values():
            leg = a.autopilot.leg
            if prev.get(a.id) != leg and leg == "UPWIND":
                laps[a.id] += 1
            prev[a.id] = leg
            L = a.autopilot.p.legs[leg]; along, x = L.project(a.autopilot.p.to_enu(a.lat, a.lon))
            if 0.4 * L.length < along < 0.6 * L.length:
                worst = max(worst, abs(x))
            for e in a.events:
                if e[0] == "TOUCHDOWN":
                    touchdowns.append((a.id, e[1], tw.touchdown(a, e[1])))
            a.events.clear()
    fly(w, list(w.fleet), 600, each=rec)
    assert all(laps[i] >= 2 for i in w.fleet), laps
    assert worst < 80, f"mid-leg cross-track {worst:.0f} m"
    assert touchdowns and all(s == "RUNWAY" for _, _, s in touchdowns), touchdowns
    assert min(v for _, v, _ in touchdowns) > -400, "firm AI landing"

# ---------------------------------------------------------------- takeover / autopilot
def test_command_needs_ap_and_no_stick_and_is_bounded():
    w = scn("judges.json")
    n101, n102 = w.fleet["N101"], w.fleet["N102"]           # N101 ap_equipped, N102 not
    cmd = {"mode": "TAKEOVER", "bank_cmd_deg": -45, "hold_s": 30, "bounds": {"max_bank_deg": 30, "max_hold_s": 10}}
    assert n102.apply_command(cmd, 0.0) is False
    assert n101.apply_command(cmd, 0.0) is True
    assert n101.cmd_until == pytest.approx(CMD_MAX_HOLD_S)
    fly(w, ["N101"], 3.0)
    assert abs(n101.bank_ctl_deg) <= CMD_MAX_BANK + 1e-6
    n101.apply_input(0.8, 0, 0.5, 3.0)                      # stick active -> new takeover refused
    assert n101.apply_command(cmd, 3.0) is False

def test_ap_from_anywhere_lands_full_stop_then_takes_off_again():
    w = scn("judges.json"); a = w.fleet["N101"]; tw = Taws(w.airport)
    a.lat, a.lon = move(33.6883, -112.0826, 0, 4000)
    a.alt_msl_ft, a.hdg_deg, a.vs_fpm = 3400, 200, -400
    a.apply_input(0.5, -0.3, 0.6, 0.0)
    ok, phase = a.engage_ap(True)
    assert ok and phase == "LEVEL"
    t = fly(w, ["N101"], 420)
    assert a.ap_phase == "STOPPED" and a.ias_kt == 0 and tw.on_runway(a)
    al, x = a.autopilot.p.legs["RUNWAY"].project(a.autopilot.p.to_enu(a.lat, a.lon))
    assert abs(x) < 10, f"{x:.1f} m off the centreline"
    a.events.clear()
    assert a.engage_ap(True) == (True, "TAKEOFF")
    fly(w, ["N101"], 60, t0=t)
    assert any(e[0] == "LIFTOFF" for e in a.events)

def test_stick_disconnects_ap_and_ap_rules():
    w = scn("judges.json")
    assert w.fleet["N202"].engage_ap(True)[0] is False       # AI without an autopilot
    assert w.fleet["N102"].engage_ap(True)[0] is True        # judge aircraft always may
    a = w.fleet["N101"]; a.engage_ap(True)
    a.apply_input(0.05, 0.02, 0.5, 1.0)                      # stick noise below the deadzone
    assert a.ap_engaged
    a.apply_input(0.6, 0, 0.5, 1.1)
    assert not a.ap_engaged and a.mode == "HUMAN" and ("AP_DISCONNECT", "stick") in a.events

# ---------------------------------------------------------------- ground handling
def test_idle_on_runway_stops_and_names_the_runway():
    w = scn("judges.json"); a = w.fleet["N102"]
    put_on_runway(w, a, ias=70)
    fly(w, ["N102"], 25, each=lambda t: a.apply_input(0, 0, 0.0, t))
    assert a.ias_kt == 0 and ("STOPPED", "25L") in a.events

def test_brake_overrides_throttle_and_no_brake_keeps_rolling():
    w = scn("judges.json"); a = w.fleet["N102"]
    put_on_runway(w, a, ias=60)
    fly(w, ["N102"], 25, each=lambda t: a.apply_input(0, 0, 0.6, t, brake=True))
    assert a.ias_kt == 0
    put_on_runway(w, a, ias=60)
    fly(w, ["N102"], 8, each=lambda t: a.apply_input(0, 0, 0.6, t))
    assert a.ias_kt > 50

def test_off_runway_cannot_take_off():
    w = scn("judges.json"); a = w.fleet["N102"]
    a.lat, a.lon = move(33.6883, -112.0826, 0, -5000)
    a.alt_msl_ft = w.env.terrain_ft(a.lat, a.lon); a.agl_ft = 0; a.vs_fpm = 0; a.ias_kt = 90; a.on_ground = True
    fly(w, ["N102"], 30, each=lambda t: a.apply_input(0, 0.8, 1.0, t))
    assert a.agl_ft < 1 and a.ias_kt == 0 and a.surface == "OFF"

def test_manual_takeoff_from_runway():
    w = scn("judges.json"); a = w.fleet["N102"]
    put_on_runway(w, a)
    fly(w, ["N102"], 40, each=lambda t: a.apply_input(0, 0.6 if a.ias_kt > 58 else 0, 1.0, t))
    assert any(e[0] == "LIFTOFF" and e[1] >= 55 for e in a.events) and a.agl_ft > 50

# ---------------------------------------------------------------- terrain awareness
def test_taws_quiet_for_normal_traffic():
    w = scn("judges.json"); tw = Taws(w.airport); alerts = collections.Counter()
    def rec(t):
        if round(t / DT) % 2 == 0:
            for a in w.fleet.values():
                al = tw.alert(a, 0.1)
                if al:
                    alerts[(a.id, a.autopilot.leg, al)] += 1
    fly(w, list(w.fleet), 300, each=rec)
    assert not alerts, alerts

def test_taws_dive_far_from_field_warns_then_impact():
    w = scn("judges.json"); tw = Taws(w.airport); a = w.fleet["N102"]
    a.lat, a.lon = move(33.6883, -112.0826, 0, -5000)
    a.alt_msl_ft = w.env.terrain_ft(a.lat, a.lon) + 1200; a.hdg_deg = 180
    seen, surface = [], None
    t = 0.0
    for _ in range(int(120 / DT)):
        a.apply_input(0, -1.0, 0.9, t); a.step(DT, w.env, t); t += DT
        al = tw.alert(a, DT)
        if al and (not seen or seen[-1] != al):
            seen.append(al)
        td = [e for e in a.events if e[0] == "TOUCHDOWN"]
        if td:
            surface = tw.touchdown(a, td[0][1]); break
    assert "PULL UP" in seen and surface == "TERRAIN_IMPACT", (seen, surface)

# ---------------------------------------------------------------- weather
def test_pressure_drop_puts_stale_altimeters_low():
    w = scn("judges.json", weather="pressure_drop"); trues = []
    def rec(t):
        for a in w.fleet.values():
            L = a.autopilot.p.legs[a.autopilot.leg]
            if a.autopilot.leg == "DOWNWIND" and 0.4 * L.length < L.project(a.autopilot.p.to_enu(a.lat, a.lon))[0] < 0.6 * L.length:
                trues.append(a.alt_msl_ft)
    fly(w, list(w.fleet), 300, each=rec)
    assert w.stale_altimeters() == len(w.fleet)
    assert 1900 < sum(trues) / len(trues) < 2150, "0.5 inHg stale setting should fly ~500 ft low"
    assert w.update_altimeters() == len(w.fleet) and w.stale_altimeters() == 0

@pytest.mark.parametrize("preset,max_flips", [("hot_gusty_afternoon", 3.0), ("haboob", 4.0)])
def test_turbulence_is_smooth_not_jitter(preset, max_flips):
    w = scn("judges.json", weather=preset); a = w.fleet["N101"]
    fly(w, ["N101"], 5)
    banks = []
    fly(w, ["N101"], 60, t0=5, each=lambda t: banks.append(a.bank_deg))
    rates = [(b2 - b1) / DT for b1, b2 in zip(banks, banks[1:])]
    flips = sum(1 for x, y in zip(rates, rates[1:]) if x * y < 0) / 60
    assert flips < max_flips, f"{flips:.1f} bank reversals/s"

def test_every_preset_loads_and_flies():
    from world.weather import PRESETS
    for name in PRESETS:
        w = scn("judges.json", weather=name)
        fly(w, list(w.fleet), 20)
        assert all(math.isfinite(a.alt_msl_ft) and math.isfinite(a.ias_kt) for a in w.fleet.values()), name

def test_scenario_weather_pin():
    assert scn("three_on_final.json").metar["wind_kt"] == 8       # "cached" = committed sample

# ---------------------------------------------------------------- separation + scenarios
@pytest.mark.parametrize("name,expect", [("base_vs_straight_in", "NMAC"), ("three_on_final", "NMAC"), ("head_on_judges", "COLLISION")])
def test_conflict_scenarios_still_conflict_without_arc(name, expect):
    w = scn(f"{name}.json"); sep = SeparationMonitor(); kinds = set()
    def rec(t):
        if round(t / DT) % 2 == 0:
            kinds.update(e["event"] for e in sep.check(w.fleet, t))
    fly(w, list(w.fleet), 240, each=rec)
    assert expect in kinds, f"{name}: {kinds or 'no encounter'} - re-tune with world/find_conflict.py"

def test_three_on_final_all_pairs_in_the_box():
    w = scn("three_on_final.json"); sep = SeparationMonitor(); pairs = set()
    def rec(t):
        if round(t / DT) % 2 == 0:
            pairs.update((e["a"], e["b"]) for e in sep.check(w.fleet, t) if e["event"] == "NMAC")
    fly(w, list(w.fleet), 240, each=rec)
    assert len(pairs) == 3, pairs

def test_reset_returns_aircraft_to_spawn():
    w = scn("judges.json"); a = w.fleet["N102"]
    start = (a.lat, a.lon, a.alt_msl_ft)
    fly(w, ["N102"], 30, each=lambda t: a.apply_input(0.8, -1, 1, t))
    b = w.reset_aircraft("N102")
    assert (b.lat, b.lon, b.alt_msl_ft) == pytest.approx(start) and b.mode == "AUTOPILOT" and w.fleet["N102"] is b

def test_presets_restore_or_set_density_altitude():
    w = scn("judges.json")
    metar_da = w.env.da_field_ft
    w.set_preset("low_ceiling"); assert w.env.da_field_ft == 3500
    w.set_preset("pressure_drop"); assert w.env.da_field_ft == metar_da      # METAR weather + pressure drop
    w.set_preset("haboob"); w.set_preset("metar"); assert w.env.da_field_ft == metar_da
    pinned = scn("judges.json", da_override=5555); pinned.set_preset("haboob")
    assert pinned.env.da_field_ft == 5555                                    # CLI --da wins over presets

# ---------------------------------------------------------------- live traffic (live_kdvt.json)
from world.scenario import runway_flow                     # noqa: E402
from world.traffic import NM_M, TrafficGenerator          # noqa: E402
from world.flight_model import wrap180                     # noqa: E402

def live(**over) -> World:
    """live_kdvt with the sample weather (wind 250/8 -> runway 25 flow) unless over-ridden."""
    raw = json.load(open(SCN / "live_kdvt.json"))
    raw["weather"] = "cached"
    seed = over.pop("seed", None)
    raw.update(over)
    return World(raw, seed=seed)

def run_live(w: World, secs: float, t0: float = 0.0, sep: SeparationMonitor | None = None, each=None):
    """Everything flies; the generator runs once a second like the server."""
    t, n, air, events = t0, 0, [], []
    for _ in range(int(secs / DT)):
        for ac in list(w.fleet.values()):
            ac.step(DT, w.env, t)
            ac.events.clear()
        t += DT; n += 1
        if n % 4 == 0:
            w.runway_watch(t)                                 # 5 Hz, like the server
        if n % 20 == 0:
            s, r = w.traffic.step(t)
            events += [("+", i, w.traffic.describe(i)["mission"]) for i in s] + [("-", i, None) for i in r]
            air.append(w.traffic.airborne())
            if sep:
                for i in r:
                    sep.forget(i)
        if sep and n % 2 == 0:
            events += [(e["event"], e["a"], e["b"]) for e in sep.check(w.fleet, t) if e["event"] != "NMAC_END"]
        if each:
            each(t)
    return t, air, events

def test_runway_flow_follows_the_wind():
    assert runway_flow({"wind_dir_deg": 120, "wind_kt": 9}) == "07"          # today's KDVT METAR
    assert runway_flow({"wind_dir_deg": 250, "wind_kt": 8}) == "25"
    assert runway_flow({"wind_dir_deg": 100, "wind_kt": 2}) == "25"          # calm: 25
    assert runway_flow({"wind_dir_deg": 250, "wind_kt": 8}, "07") == "07"    # scenario override
    w = live(runway_flow="07")
    assert w.flow == "07" and w.traffic_runways == {"07R": 0.75, "07L": 0.25} and set(w.patterns) == {"07R", "07L"}
    assert live().traffic_runways == {"25L": 0.75, "25R": 0.25}

def test_judges_start_4nm_north_and_south_inbound_at_pattern_altitude():
    w = live(runway_flow="07")
    a, b = w.fleet["N101"], w.fleet["N102"]
    for ac, brg in ((a, 0.0), (b, 180.0)):
        x, y = P.to_enu(ac.lat, ac.lon)
        assert math.hypot(x, y) / NM_M == pytest.approx(4.0, abs=0.01)
        assert math.degrees(math.atan2(x, y)) % 360 == pytest.approx(brg, abs=0.5)
        assert ac.hdg_deg == pytest.approx((brg + 180) % 360) and ac.alt_msl_ft == pytest.approx(P.ELEV_FT + P.TPA_AGL_FT, abs=1)
        assert ac.human and ac.mode == "AUTOPILOT" and ac.autopilot.phase == "TRANSIT"
    assert a.autopilot.p.ident == "07L" and b.autopilot.p.ident == "07R"      # north / south runway
    # untouched they hold course and altitude at each other ...
    fly(w, ["N101", "N102"], 60)
    assert abs(wrap180(a.track_deg - 180)) < 3 and abs(wrap180(b.track_deg)) < 3 and abs(a.alt_msl_ft - 2500) < 60
    # ... and join their own pattern 2 NM past the field
    fly(w, ["N101"], 240, t0=60)
    assert a.autopilot.phase in ("JOIN", "PATTERN")

def test_generator_keeps_traffic_flowing_and_repeats_with_the_seed():
    w1, w2 = live(runway_flow="07", seed=11), live(runway_flow="07", seed=11)
    sep = SeparationMonitor()
    _, air, ev1 = run_live(w1, 600, sep=sep)
    _, _, ev2 = run_live(w2, 600)
    spawns = [e for e in ev1 if e[0] == "+"]
    assert [e for e in ev1 if e[0] in "+-"] == [e for e in ev2 if e[0] in "+-"]      # same seed, same traffic
    assert min(air[60:]) >= 4 and max(air) <= 8 and sum(air[60:]) / len(air[60:]) >= 5.5, air
    assert len(spawns) >= 10 and any(e[0] == "-" for e in ev1)                     # comes and goes
    assert {"circuits", "arrival_45"} <= {e[2] for e in spawns}
    ai_hits = [e for e in ev1 if e[0] in ("NMAC", "COLLISION") and "N101" not in e[1:] and "N102" not in e[1:]]
    assert not ai_hits, ai_hits                                                       # AI never hit each other
    assert live(runway_flow="07", seed=12).traffic.seed == 12
    w3 = live(runway_flow="07", seed=12); run_live(w3, 120)
    assert sorted(w3.traffic.used) != sorted(w1.traffic.used)                        # another seed, other traffic

def test_departures_and_arrivals_finish_and_leave():
    w = live(runway_flow="07", seed=23)
    gen = w.traffic
    dep = gen._departure("07R", 0.0)
    assert dep and w.fleet[dep].autopilot.phase == "TAKEOFF" and w.fleet[dep].on_ground
    sti = gen._straight_in("07R", 0.0)
    gone = {}
    def watch(t):
        for i in (dep, sti):
            if i not in w.fleet and i not in gone:
                gone[i] = t
    run_live(w, 720, each=watch)                                      # a go-around (runway busy) costs a circuit
    assert dep in gone and sti in gone, (gone, w.fleet.get(dep) and w.fleet[dep].autopilot.phase)

def test_ai_pilot_follows_node_advice_after_about_3_s():
    w = live(runway_flow="07", seed=5)
    gen = w.traffic
    i = gen._circuits("07R", 0.0)
    ac, pl = w.fleet[i], w.fleet[i].autopilot
    pl.phase, pl.leg = "PATTERN", "DOWNWIND"
    pl.follow_p = 1.0
    e = pl.advise({"level": "RESOLVE", "text": "TURN LEFT 30 - N101 TURNING RIGHT", "reason": {"chosen": "L30"}}, 100.0)
    assert e and e["follow"] and e["action"] == ["TURN", -30.0]
    assert pl.advise({"level": "RESOLVE", "text": "TURN LEFT 30 - N101 TURNING RIGHT", "reason": {"chosen": "L30"}}, 101.0) is None  # repeat
    assert pl.targets(ac, w.env, 101.0)[0] != -30.0                  # not yet: pilot reaction time
    assert pl.targets(ac, w.env, 103.6)[0] == -30.0                  # ~3 s later: banking left 30
    pl.man = None
    pl.advise({"level": "SEQUENCE", "text": "NUMBER 2 - EXTEND DOWNWIND 20 S", "reason": {}}, 110.0)
    pl.targets(ac, w.env, 114.0)
    assert pl.extend_s == 20.0
    pl.phase, pl.leg = "PATTERN", "FINAL"
    pl.advise({"level": "TAKEOVER", "text": "CLIMB NOW - N204 DESCENDING", "reason": {"chosen": "CLIMB"}}, 120.0)
    ac.agl_ft, ac.on_ground = 500, False
    pl.targets(ac, w.env, 124.0)
    assert pl.phase == "GO_AROUND"                                    # advice on final -> go around
    pl.follow_p = 0.0
    e = pl.advise({"level": "RESOLVE", "text": "TURN RIGHT 20 - N9 CONTINUING", "reason": {"chosen": "R20"}}, 130.0)
    assert e and not e["follow"] and not pl.pending
    pl.follow_p = 0.7
    pl.rng.seed(1)
    picks = [pl.rng.random() < pl.follow_p for _ in range(1000)]
    assert 0.65 < sum(picks) / 1000 < 0.75

def test_extended_downwind_moves_the_base_turn_out():
    def base_turn_along(ext: float) -> float:
        w = live(runway_flow="07", seed=5)
        i = w.traffic._circuits("07R", 0.0)
        ac, pl = w.fleet[i], w.fleet[i].autopilot
        pos = P.place("DOWNWIND", "07R", 10, 95.0)
        ac.lat, ac.lon, ac.alt_msl_ft, ac.hdg_deg, ac.ias_kt = pos["lat"], pos["lon"], pos["alt_msl_ft"], pos["hdg_deg"], 95.0
        ac.agl_ft, ac.on_ground = pos["agl_ft"], False
        pl.phase, pl.leg, pl.extend_s = "PATTERN", "DOWNWIND", ext
        D = pl.p.legs["DOWNWIND"]
        t = 0.0
        while pl.leg == "DOWNWIND" and t < 300:
            ac.step(DT, w.env, t); t += DT
        return D.project(P.to_enu(ac.lat, ac.lon))[0]
    plain, extended = base_turn_along(0.0), base_turn_along(30.0)
    assert 1200 < extended - plain < 1800                             # ~30 s at ~95 kt further out

def test_reset_demo_puts_both_judges_back_and_clears_their_starts():
    w = live(runway_flow="07", seed=7)
    run_live(w, 90)
    a = w.fleet["N101"]
    a.apply_input(0.6, 0.4, 1.0, 90.0)
    fly(w, ["N101"], 20, t0=90)
    gen: TrafficGenerator = w.traffic
    near = gen._new_id()                                              # park an AI aircraft on N102's start
    pl = gen._pilot(near, w.pattern("07R"), "DOWNWIND"); pl.phase = "JOIN"
    s = w.specs["N102"].start
    lat, lon = P.from_enu(0, -4 * NM_M + 500)
    w.add_traffic(near, lat, lon, 2400, 0.0, 95.0, pl); gen.active[near] = {"mission": "arrival_45", "born": 0, "rwy": "07R", "live": None}
    reset, removed = w.reset_demo()
    assert [r.id for r in reset] == ["N101", "N102"] and near in removed and near not in w.fleet
    for ac, brg in ((w.fleet["N101"], 0.0), (w.fleet["N102"], 180.0)):
        x, y = P.to_enu(ac.lat, ac.lon)
        assert math.hypot(x, y) / NM_M == pytest.approx(4.0, abs=0.01) and ac.mode == "AUTOPILOT"
        assert ac.autopilot.phase == "TRANSIT" and not ac.has_pilot

# ---------------------------------------------------------------- runway occupancy + one landing direction
def fly_all(w: World, secs: float, t0: float = 0.0, sep: SeparationMonitor | None = None):
    """Everyone flies; World.runway_watch at 5 Hz like the server. Returns (t, world events, separation events)."""
    t, n, evs, seps = t0, 0, [], []
    for _ in range(int(secs / DT)):
        for ac in list(w.fleet.values()):
            ac.step(DT, w.env, t)
            ac.events.clear()
        t += DT; n += 1
        if n % 4 == 0:
            evs += w.runway_watch(t)
        if sep and n % 2 == 0:
            seps += [e for e in sep.check(w.fleet, t) if e["event"] in ("NMAC", "COLLISION")]
    return t, evs, seps

def park_on_runway(w: World, ac: Aircraft, rwy: str, along_m: float, ias: float = 0.0) -> None:
    """A judge sitting (or rolling slowly) on the runway: pilot has it, no AP."""
    L = w.pattern(rwy).legs["RUNWAY"]
    ue, un = math.sin(math.radians(L.brg)), math.cos(math.radians(L.brg))
    ac.lat, ac.lon = P.from_enu(L.a[0] + ue * along_m, L.a[1] + un * along_m)
    ac.alt_msl_ft = w.env.terrain_ft(ac.lat, ac.lon)
    ac.agl_ft, ac.vs_fpm, ac.ias_kt, ac.hdg_deg, ac.on_ground = 0.0, 0.0, ias, L.brg, True
    ac.apply_input(0, 0, 0.0 if ias == 0 else 0.3, 0.0, brake=ias == 0)

def on_final(w: World, ac: Aircraft, rwy: str, secs_out: float) -> None:
    pos = P.place("FINAL", rwy, 0, 72.0)
    L = w.pattern(rwy).legs["FINAL"]
    p = P.place("FINAL", rwy, max(0.0, L.length / (72 * 0.514444) - secs_out), 72.0)
    ac.lat, ac.lon, ac.alt_msl_ft, ac.hdg_deg, ac.ias_kt = p["lat"], p["lon"], p["alt_msl_ft"], p["hdg_deg"], 72.0
    ac.agl_ft, ac.on_ground = p["agl_ft"], False
    ac.autopilot.leg, ac.autopilot.phase, ac.autopilot.touched = "FINAL", "PATTERN", False

@pytest.mark.parametrize("parked_ias", [0.0, 12.0])          # stopped, and slowly rolling / taxiing on the runway
def test_ap_lander_goes_around_when_someone_is_on_the_runway(parked_ias):
    w = scn("judges.json")
    for i in [i for i in w.fleet if i not in ("N101", "N102")]:
        del w.fleet[i]
    park_on_runway(w, w.fleet["N101"], "25L", 500.0, parked_ias)
    b = w.fleet["N102"]
    on_final(w, b, "25L", 40)
    ok, _ = w.engage_ap("N102", True)                          # judge B on AP, full stop
    assert ok
    b.autopilot.leg, b.autopilot.phase, b.autopilot.touched = "FINAL", "PATTERN", False
    sep = SeparationMonitor()
    _, evs, seps = fly_all(w, 90, sep=sep)
    ga = [e for e in evs if e["event"] == "GO_AROUND" and e["a"] == "N102"]
    assert ga and ga[0]["b"] == "N101" and ga[0]["agl_ft"] <= 400, evs
    assert not [e for e in seps if e["event"] == "COLLISION"], seps
    assert b.alt_msl_ft - b.autopilot.p.elev_ft > 300 and not b.on_ground     # climbing away, not on top of N101

def test_ai_traffic_goes_around_a_stopped_judge_and_lands_when_clear():
    w = scn("judges.json")
    keep = ("N101", "N204")                                   # N204: AI on final, touch-and-goes
    for i in [i for i in w.fleet if i not in keep]:
        del w.fleet[i]
    park_on_runway(w, w.fleet["N101"], "25L", 900.0)
    ai = w.fleet["N204"]
    on_final(w, ai, "25L", 35)
    sep = SeparationMonitor()
    _, evs, seps = fly_all(w, 60, sep=sep)
    assert any(e["event"] == "GO_AROUND" and e["a"] == "N204" for e in evs), evs
    assert not [e for e in seps if e["event"] == "COLLISION"]
    w.fleet["N101"].lat, w.fleet["N101"].lon = P.from_enu(0, -3000)     # judge clears the runway
    w.fleet["N101"].alt_msl_ft = w.env.terrain_ft(w.fleet["N101"].lat, w.fleet["N101"].lon)
    _, evs2, _ = fly_all(w, 480, t0=60)
    assert not [e for e in evs2 if e["event"] == "GO_AROUND"]             # clear runway: no more go-arounds

def test_ap_takeoff_holds_short_while_runway_or_short_final_busy():
    w = scn("judges.json")
    for i in [i for i in w.fleet if i not in ("N101", "N102", "N204")]:
        del w.fleet[i]
    a, b = w.fleet["N101"], w.fleet["N102"]
    park_on_runway(w, b, "25L", 60.0)                          # B at the start of the runway, wants to go
    park_on_runway(w, a, "25L", 1200.0)                        # A stopped further down
    w.fleet.pop("N204")
    w.engage_ap("N102", True)
    assert b.autopilot.phase == "TAKEOFF"
    _, evs, _ = fly_all(w, 20)
    assert b.ias_kt < 1 and b.on_ground and any(e["event"] == "HOLD_SHORT" and e["a"] == "N102" for e in evs)
    a.lat, a.lon = P.from_enu(0, -3000)                        # A taxis off
    a.alt_msl_ft = w.env.terrain_ft(a.lat, a.lon)
    fly_all(w, 60, t0=20)
    assert not b.on_ground and b.agl_ft > 100                  # then it goes

def test_ap_always_lands_with_the_runway_flow():
    w = scn("head_on_judges.json")                              # N102 was set up on 07R, N101 on 25L (same strip)
    assert w.flow == "25"
    b = w.fleet["N102"]
    assert b.autopilot.p.ident == "07R"
    ok, _ = w.engage_ap("N102", True)
    assert ok and b.autopilot.p.ident == "25L"                 # AP flies the active end, like everyone else
    t, _, _ = fly_all(w, 600)
    R = b.autopilot.p.legs["RUNWAY"]
    assert b.ap_phase == "STOPPED" and abs(wrap180(b.hdg_deg - R.brg)) < 10   # landed heading 266, not 086
    assert scn("judges.json").flow == "25" and live(runway_flow="07").flow == "07"
