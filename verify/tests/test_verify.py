"""
verify/tests/test_verify.py - ARC verification: checks, fusion, guardrails, and the full simulated loop
(world -> sensors -> onboard unit) with a ghost injected.   python -m pytest -q verify/tests
"""
from __future__ import annotations
import json, math, sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from verify import load_config                                   # noqa: E402
from verify.checks import CHECKS, CheckResult, Ctx               # noqa: E402
from verify.checks import kinematics, modes_presence, tcas_consistency  # noqa: E402
from verify.fusion import fuse                                   # noqa: E402
from verify.schema import Verify                                 # noqa: E402
from verify.tracks import Track, TrackManager, move              # noqa: E402
from verify.unit import VerifyUnit                               # noqa: E402

CFG = load_config()
NM = 1852.0
LAT0, LON0 = 33.69, -112.08

def own_at(tm: TrackManager, t: float, alt: float = 2500.0):
    tm.ingest({"kind": "OWNSHIP_STATE", "t_ns": int(t * 1e9), "lat": LAT0, "lon": LON0, "alt_ft": alt,
               "gs_kt": 0, "trk_deg": 0, "vs_fpm": 0})

def fly_target(tm, icao, t0, t1, brg=90.0, rng_nm=2.0, gs=100.0, trk=0.0, alt=2500.0, step=0.5, tcas=True,
               replies=True, tcas_offset_m=0.0):
    """A target at brg/rng from own-ship flying trk at gs; optional TCAS track and Mode S replies."""
    lat, lon = move(LAT0, LON0, rng_nm * NM * math.sin(math.radians(brg)), rng_nm * NM * math.cos(math.radians(brg)))
    t = t0
    while t <= t1 + 1e-9:
        d = gs * 0.514444 * (t - t0)
        la, lo = move(lat, lon, d * math.sin(math.radians(trk)), d * math.cos(math.radians(trk)))
        ns = int(t * 1e9)
        tm.ingest({"kind": "ADSB_POSITION", "t_ns": ns, "icao": icao, "callsign": icao.upper(), "lat": la, "lon": lo, "alt_ft": alt})
        tm.ingest({"kind": "ADSB_VELOCITY", "t_ns": ns, "icao": icao, "gs_kt": gs, "trk_deg": trk, "vs_fpm": 0})
        if tcas and abs(round(t) - t) < 1e-9:
            e = (lo - LON0) * 111_320 * math.cos(math.radians(LAT0)); n = (la - LAT0) * 111_320
            tm.ingest({"kind": "TCAS_TRACK", "t_ns": ns, "icao": icao, "range_m": math.hypot(e, n) + tcas_offset_m,
                       "brg_deg": math.degrees(math.atan2(e, n)) % 360, "alt_ft": alt})
        if replies and abs(round(t) - t) < 1e-9:
            tm.ingest({"kind": "MODES_REPLY", "t_ns": ns, "icao": icao, "df": 4 if int(t) % 5 == 0 else 11})
        t += step
    return tm.tracks[icao]

def ctx(tm, t):
    return Ctx(t=t, own=tm.own, radar_t=tm.radar_t, tcas_t=tm.tcas_t)

# ---------------------------------------------------------------- checks
def test_checks_return_none_without_data_never_a_penalty():
    tm = TrackManager(); own_at(tm, 0.0)
    tm.ingest({"kind": "ADSB_POSITION", "t_ns": int(1e9), "icao": "abc123", "lat": LAT0 + 0.01, "lon": LON0, "alt_ft": 2500})
    tr = tm.tracks["abc123"]
    assert kinematics.check(tr, ctx(tm, 1.0), CFG).score is None                 # 1 report
    assert tcas_consistency.check(tr, ctx(tm, 1.5), CFG).score is None           # TCAS grace period
    assert modes_presence.check(tr, ctx(tm, 1.5), CFG).score is None             # no radar heard
    far = fly_target(tm, "far001", 0, 10, rng_nm=30, tcas=False, replies=False)
    assert tcas_consistency.check(far, ctx(tm, 10), CFG).score is None           # beyond TCAS range
    out = fuse(tr, {n: fn(tr, ctx(tm, 1.5), CFG) for n, fn in CHECKS.items()}, CFG)
    assert out["state"] == "UNVERIFIED" and "not enough evidence" in out["reasons"][0]

def test_tcas_confirms_real_and_catches_range_lie_and_absence():
    tm = TrackManager(); own_at(tm, 0.0)
    real = fly_target(tm, "aaa111", 0, 10)
    r = tcas_consistency.check(real, ctx(tm, 10), CFG)
    assert r.score > 0.9 and r.evidence.get("tcas_confirmed") and "TCAS confirms" in r.reason
    liar = fly_target(tm, "bbb222", 0, 10, brg=200, tcas_offset_m=1500)             # TCAS measures 0.8 NM further
    r = tcas_consistency.check(liar, ctx(tm, 10), CFG)
    assert r.score < 0.1 and "TCAS measures" in r.reason
    ghost = fly_target(tm, "ccc333", 0, 10, brg=300, tcas=False, replies=False)
    r = tcas_consistency.check(ghost, ctx(tm, 10), CFG)
    assert r.score < 0.1 and "TCAS sees nothing" in r.reason

def test_kinematics_flags_teleport_but_not_normal_flight():
    tm = TrackManager(); own_at(tm, 0.0)
    ok = fly_target(tm, "aaa111", 0, 8)
    assert kinematics.check(ok, ctx(tm, 8), CFG).score >= 0.5
    tr = fly_target(tm, "ddd444", 0, 4)
    lat, lon = move(tr.pos[-1][1], tr.pos[-1][2], 5000, 0)                       # 5 km jump in 0.5 s, then on from there
    for i in range(5):
        la, lo = move(lat, lon, 0, 25.7 * i)
        tm.ingest({"kind": "ADSB_POSITION", "t_ns": int((4.5 + 0.5 * i) * 1e9), "icao": "ddd444", "lat": la, "lon": lo, "alt_ft": 2500})
    jumped = tm.tracks["ddd444~2"]                                                 # the address split at the jump
    r = kinematics.check(jumped, ctx(tm, 6.5), CFG)
    assert r.score < 0.1 and "jumps" in r.reason
    assert kinematics.check(tr, ctx(tm, 6.5), CFG).score >= 0.5                     # the original track is not blamed

def test_modes_presence_only_counts_absence_when_radar_is_active():
    tm = TrackManager(); own_at(tm, 0.0)
    real = fly_target(tm, "aaa111", 0, 25)
    ghost = fly_target(tm, "ccc333", 0, 25, brg=300, tcas=False, replies=False)
    assert modes_presence.check(real, ctx(tm, 25), CFG).score > 0.5
    assert modes_presence.check(ghost, ctx(tm, 25), CFG).score < 0.5            # radar active (real's DF4s)
    quiet = TrackManager(); own_at(quiet, 0.0)
    g2 = fly_target(quiet, "ccc333", 0, 25, tcas=False, replies=False)
    assert modes_presence.check(g2, ctx(quiet, 25), CFG).score is None           # no radar anywhere: no data

# ---------------------------------------------------------------- fusion + guardrails
def test_tcas_confirmed_target_is_never_suspect():
    tr = Track("eee555", first_t=0.0)
    results = {"tcas_consistency": CheckResult(0.97, 0.9, "TCAS confirms it", {"tcas_confirmed": True}),
               "modes_presence": CheckResult(0.02, 1.0, "never answers ground radar"),
               "kinematics": CheckResult(0.02, 1.0, "impossible motion")}
    for _ in range(10):
        out = fuse(tr, results, CFG)
    assert out["trust"] <= CFG["fusion"]["suspect_max"]                          # the numbers say spoof ...
    assert out["state"] == "UNVERIFIED" and "never marked spoofed" in out["reasons"][0]   # ... the guardrail says no

def test_plausible_motion_alone_never_verifies_and_weak_doubt_never_suspects():
    tr = Track("fff666", first_t=0.0)
    for _ in range(10):
        out = fuse(tr, {"kinematics": CheckResult(0.98, 0.9, "moves like a real aircraft")}, CFG)
    assert out["state"] == "UNVERIFIED"
    tr2 = Track("fff777", first_t=0.0)
    for _ in range(10):
        out = fuse(tr2, {"modes_presence": CheckResult(0.2, 0.55, "few replies"),
                         "kinematics": CheckResult(0.2, 0.55, "odd")}, CFG)
    assert out["state"] == "UNVERIFIED"

def test_fusion_smooths_but_reacts_fast_to_strong_evidence():
    tr = Track("ggg888", first_t=0.0)
    good = {"tcas_consistency": CheckResult(0.97, 0.9, "TCAS confirms it", {"tcas_confirmed": True}),
            "modes_presence": CheckResult(0.85, 0.6, "answers ground radar")}
    for _ in range(5):
        a = fuse(tr, good, CFG)
    assert a["state"] == "VERIFIED"
    bad = {"tcas_consistency": CheckResult(0.03, 0.9, "ADS-B says 2.0 NM, TCAS measures 0.4 NM"),
           "modes_presence": CheckResult(0.1, 0.6, "never answers")}
    b = fuse(tr, bad, CFG)
    assert b["state"] == "SUSPECT" and b["reasons"][0].startswith("ADS-B says")   # one update, not ten

# ---------------------------------------------------------------- the whole loop, simulated
def _world():
    from world.scenario import World
    raw = json.load(open(ROOT / "harness" / "scenarios" / "live_kdvt.json"))
    raw["weather"] = "cached"
    return World(raw, seed=7)

def test_simulated_ghost_goes_suspect_and_real_traffic_never_does():
    from world.sensors import SensorSim
    w = _world(); ss = SensorSim(w)
    unit = VerifyUnit("N101", CFG)
    dt, t, n, ghost_at, first_suspect = 0.05, 1000.0, 0, None, None
    wrong = []
    while t < 1000 + 120:
        for ac in list(w.fleet.values()):
            ac.step(dt, w.env, t); ac.events.clear()
        t += dt; n += 1
        if n % 20 == 0:
            w.traffic.step(t)
        if n == 600:                                                             # ghost at 30 s
            g = ss.attacks.start("ghost", w.fleet["N101"], t); ghost_at = t
        msgs = ss.step(t, own_ids=["N101"])["N101"]
        assert all("label" not in m and "truth" not in m for m in msgs)          # no ground truth on the wire
        unit.ingest({"t": t, "msgs": msgs})
        if unit.due():
            v = unit.evaluate()
            Verify.model_validate(v)
            truth = {e["icao"]: e["label"] for e in ss.truth(t)}
            for x in v["targets"]:
                if truth.get(x["icao"]) == "real" and x["state"] == "SUSPECT":
                    wrong.append((round(t - 1000), x["id"], x["reasons"]))
                if truth.get(x["icao"]) == "ghost" and x["state"] == "SUSPECT" and first_suspect is None:
                    first_suspect = t
    assert not wrong, wrong
    assert first_suspect is not None and first_suspect - ghost_at <= 15.0, first_suspect
    final = {x["icao"]: x for x in unit.evaluate()["targets"]}
    gx = final[g.icao]
    assert gx["state"] == "SUSPECT" and any("TCAS" in r for r in gx["reasons"])
    tcas_range = CFG["tcas"]["range_nm"] * NM * 0.8
    for x in final.values():
        if x["icao"] != g.icao and x["rel"] and x["rel"]["rng_m"] < tcas_range and abs(x["rel"]["dalt_ft"]) < 3000:
            assert x["state"] == "VERIFIED", x                                   # real and close: confirmed
