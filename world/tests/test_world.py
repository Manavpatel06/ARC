"""
world/tests/test_world.py — Lane A regression suite (physics, pattern AI, autopilot, ground, TAWS, weather,
separation, scenarios). Pure in-process simulation, no network.  Run:  python -m pytest -q world/tests
"""
from __future__ import annotations
import collections, math, sys
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
    w = World(str(SCN / "judges.json")); a = w.fleet["N102"]
    banks = []
    def rec(t):
        a.apply_input(1.0, 0, 0.5, t); banks.append(a.bank_ctl_deg)
    fly(w, ["N102"], 1.0, each=rec)
    rates = [(b2 - b1) / DT for b1, b2 in zip(banks, banks[1:])]
    assert max(abs(r) for r in rates) <= BANK_RATE_DPS + 1e-6

# ---------------------------------------------------------------- pattern AI
def test_ai_pattern_ten_minutes_laps_tracking_and_landings():
    w = World(str(SCN / "judges.json")); tw = Taws(w.airport)
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
    w = World(str(SCN / "judges.json"))
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
    w = World(str(SCN / "judges.json")); a = w.fleet["N101"]; tw = Taws(w.airport)
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
    w = World(str(SCN / "judges.json"))
    assert w.fleet["N202"].engage_ap(True)[0] is False       # AI without an autopilot
    assert w.fleet["N102"].engage_ap(True)[0] is True        # judge aircraft always may
    a = w.fleet["N101"]; a.engage_ap(True)
    a.apply_input(0.05, 0.02, 0.5, 1.0)                      # stick noise below the deadzone
    assert a.ap_engaged
    a.apply_input(0.6, 0, 0.5, 1.1)
    assert not a.ap_engaged and a.mode == "HUMAN" and ("AP_DISCONNECT", "stick") in a.events

# ---------------------------------------------------------------- ground handling
def test_idle_on_runway_stops_and_names_the_runway():
    w = World(str(SCN / "judges.json")); a = w.fleet["N102"]
    put_on_runway(w, a, ias=70)
    fly(w, ["N102"], 25, each=lambda t: a.apply_input(0, 0, 0.0, t))
    assert a.ias_kt == 0 and ("STOPPED", "25L") in a.events

def test_brake_overrides_throttle_and_no_brake_keeps_rolling():
    w = World(str(SCN / "judges.json")); a = w.fleet["N102"]
    put_on_runway(w, a, ias=60)
    fly(w, ["N102"], 25, each=lambda t: a.apply_input(0, 0, 0.6, t, brake=True))
    assert a.ias_kt == 0
    put_on_runway(w, a, ias=60)
    fly(w, ["N102"], 8, each=lambda t: a.apply_input(0, 0, 0.6, t))
    assert a.ias_kt > 50

def test_off_runway_cannot_take_off():
    w = World(str(SCN / "judges.json")); a = w.fleet["N102"]
    a.lat, a.lon = move(33.6883, -112.0826, 0, -5000)
    a.alt_msl_ft = w.env.terrain_ft(a.lat, a.lon); a.agl_ft = 0; a.vs_fpm = 0; a.ias_kt = 90; a.on_ground = True
    fly(w, ["N102"], 30, each=lambda t: a.apply_input(0, 0.8, 1.0, t))
    assert a.agl_ft < 1 and a.ias_kt == 0 and a.surface == "OFF"

def test_manual_takeoff_from_runway():
    w = World(str(SCN / "judges.json")); a = w.fleet["N102"]
    put_on_runway(w, a)
    fly(w, ["N102"], 40, each=lambda t: a.apply_input(0, 0.6 if a.ias_kt > 58 else 0, 1.0, t))
    assert any(e[0] == "LIFTOFF" and e[1] >= 55 for e in a.events) and a.agl_ft > 50

# ---------------------------------------------------------------- terrain awareness
def test_taws_quiet_for_normal_traffic():
    w = World(str(SCN / "judges.json")); tw = Taws(w.airport); alerts = collections.Counter()
    def rec(t):
        if round(t / DT) % 2 == 0:
            for a in w.fleet.values():
                al = tw.alert(a, 0.1)
                if al:
                    alerts[(a.id, a.autopilot.leg, al)] += 1
    fly(w, list(w.fleet), 300, each=rec)
    assert not alerts, alerts

def test_taws_dive_far_from_field_warns_then_impact():
    w = World(str(SCN / "judges.json")); tw = Taws(w.airport); a = w.fleet["N102"]
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
    w = World(str(SCN / "judges.json"), weather="pressure_drop"); trues = []
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
    w = World(str(SCN / "judges.json"), weather=preset); a = w.fleet["N101"]
    fly(w, ["N101"], 5)
    banks = []
    fly(w, ["N101"], 60, t0=5, each=lambda t: banks.append(a.bank_deg))
    rates = [(b2 - b1) / DT for b1, b2 in zip(banks, banks[1:])]
    flips = sum(1 for x, y in zip(rates, rates[1:]) if x * y < 0) / 60
    assert flips < max_flips, f"{flips:.1f} bank reversals/s"

def test_every_preset_loads_and_flies():
    from world.weather import PRESETS
    for name in PRESETS:
        w = World(str(SCN / "judges.json"), weather=name)
        fly(w, list(w.fleet), 20)
        assert all(math.isfinite(a.alt_msl_ft) and math.isfinite(a.ias_kt) for a in w.fleet.values()), name

def test_scenario_weather_pin():
    assert World(str(SCN / "three_on_final.json")).metar["wind_kt"] == 8       # "cached" = committed sample

# ---------------------------------------------------------------- separation + scenarios
@pytest.mark.parametrize("name,expect", [("base_vs_straight_in", "NMAC"), ("three_on_final", "NMAC"), ("head_on_judges", "COLLISION")])
def test_conflict_scenarios_still_conflict_without_flock(name, expect):
    w = World(str(SCN / f"{name}.json")); sep = SeparationMonitor(); kinds = set()
    def rec(t):
        if round(t / DT) % 2 == 0:
            kinds.update(e["event"] for e in sep.check(w.fleet, t))
    fly(w, list(w.fleet), 240, each=rec)
    assert expect in kinds, f"{name}: {kinds or 'no encounter'} - re-tune with world/find_conflict.py"

def test_three_on_final_all_pairs_in_the_box():
    w = World(str(SCN / "three_on_final.json")); sep = SeparationMonitor(); pairs = set()
    def rec(t):
        if round(t / DT) % 2 == 0:
            pairs.update((e["a"], e["b"]) for e in sep.check(w.fleet, t) if e["event"] == "NMAC")
    fly(w, list(w.fleet), 240, each=rec)
    assert len(pairs) == 3, pairs

def test_reset_returns_aircraft_to_spawn():
    w = World(str(SCN / "judges.json")); a = w.fleet["N102"]
    start = (a.lat, a.lon, a.alt_msl_ft)
    fly(w, ["N102"], 30, each=lambda t: a.apply_input(0.8, -1, 1, t))
    b = w.reset_aircraft("N102")
    assert (b.lat, b.lon, b.alt_msl_ft) == pytest.approx(start) and b.mode == "AUTOPILOT" and w.fleet["N102"] is b

def test_presets_restore_or_set_density_altitude():
    w = World(str(SCN / "judges.json"))
    metar_da = w.env.da_field_ft
    w.set_preset("low_ceiling"); assert w.env.da_field_ft == 3500
    w.set_preset("pressure_drop"); assert w.env.da_field_ft == metar_da      # METAR weather + pressure drop
    w.set_preset("haboob"); w.set_preset("metar"); assert w.env.da_field_ft == metar_da
    pinned = World(str(SCN / "judges.json"), da_override=5555); pinned.set_preset("haboob")
    assert pinned.env.da_field_ft == 5555                                    # CLI --da wins over presets
