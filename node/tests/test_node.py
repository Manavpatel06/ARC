"""Lane B unit tests.  Run from the repo root:  python -m pytest node/tests -q"""
import math

import numpy as np
import pytest

import schemas
from harness.simworld import T0, Sim, load_scenario
from node import authority, conflict, escape, layers
from node.geometry import FT, KT, build_patterns, to_enu, to_latlon
from node.negotiate import Negotiator
from node.node import Node
from node.predict import KState, Predictor
from node.trust import TrustTable

PATS = build_patterns()
PRED = Predictor(PATS)
P25L = PATS["25L"]


def st_on(leg, along_m=None, kt=90.0):
    """State on `leg` along_m metres from its start (default: middle), at the circuit's own altitude (pattern.place)."""
    L = P25L.legs[leg]
    am = L.length / 2 if along_m is None else along_m
    pl = P25L.place(leg, am / (kt * KT), kt)
    return KState(pl["x_m"], pl["y_m"], P25L.elev_m + pl["agl_ft"] * FT, kt * KT, pl["hdg_deg"])


# ---- geometry / prediction
def test_enu_roundtrip():
    x, y = to_enu(33.69, -112.09)
    lat, lon = to_latlon(x, y)
    assert abs(lat - 33.69) < 1e-9 and abs(lon + 112.09) < 1e-9


@pytest.mark.parametrize("leg", ["UPWIND", "CROSSWIND", "DOWNWIND", "BASE", "FINAL", "STRAIGHT_IN"])
def test_leg_classification(leg):
    c = PRED.classify(st_on(leg))
    assert c.leg == leg and c.runway == "25L" and c.conf > 0.8


def test_low_confidence_falls_back_to_straight_line():
    x, y, _ = P25L.reference_point("DOWNWIND")
    st = KState(x + 9000, y + 9000, P25L.tpa_msl_m, 46.0, 10.0)        # nowhere near a pattern
    p = PRED.predict(st, 0.0)
    assert p.method == "straight-line" and p.confidence < 0.6
    assert np.allclose(np.diff(p.pts[:, 1]), np.diff(p.pts[:, 1])[0])    # constant velocity


def test_turn_aware_predicts_the_turn_with_tighter_sigma():
    st = st_on("DOWNWIND", along_m=P25L.legs["DOWNWIND"].length - 1200)   # close to the base corner
    p = PRED.predict(st, 0.0)
    assert p.method == "turn-aware" and p.next_turn and p.next_turn[0] == "BASE"
    line = PRED.straight_line(st, 0.0, 90.0, 1.0, 0.5, "DOWNWIND", "25L")
    assert p.pts[90, 4] < line.pts[90, 4]                      # turn-aware sigma < straight-line sigma
    assert math.hypot(*(p.pts[90, 1:3] - line.pts[90, 1:3])) > 500   # genuinely a different path


def test_intent_moves_the_turn():
    st = st_on("DOWNWIND")
    late = PRED.predict(st, 0.0)
    soon = PRED.predict(st, 0.0, intent="BASE_IN_5S")
    assert soon.next_turn[1] < late.next_turn[1] - 10


# ---- conflict
def test_head_on_conflict_and_parallel_miss():
    def straight(x, y, hdg, z=100.0):
        return PRED.straight_line(KState(x, y, z, 50.0, hdg), 0.0, 105.0, 1.0, 1.0, "UNKNOWN", None)
    a = straight(0, -3000, 0)
    b = straight(0, 3000, 180)
    c = conflict.assess("B", a, b, 0.0)
    assert c is not None and 25 < c.ttc_s < 60 and c.miss_h_m < 152
    off = straight(1500, 3000, 180)
    assert conflict.assess("B", a, off, 0.0) is None
    above = straight(0, 3000, 180, z=100 + 300 * FT)
    assert conflict.assess("B", a, above, 0.0) is None


# ---- layers
def test_layer_thresholds_and_hysteresis():
    assert [layers.raw_level(t) for t in (95, 90, 60, 35, 34, 20, 19, 8, 7)] == \
        ["CLEAR", "SEQUENCE", "SEQUENCE", "TRAFFIC", "TRAFFIC", "RESOLVE", "RESOLVE", "TAKEOVER", "TAKEOVER"]
    trk = layers.LayerTracker()
    seen = []
    for k, ttc in enumerate([80, 40, 34, 36, 34.5, 35.5, 20, 21, 19, 20.5, 8, 9, 7]):
        seen.append(trk.update(float(k), ttc))
    # 36 / 35.5 right after TRAFFIC must not flap back to SEQUENCE; 21 / 20.5 must not flap back to TRAFFIC
    assert seen == ["SEQUENCE", "SEQUENCE", "TRAFFIC", "TRAFFIC", "TRAFFIC", "TRAFFIC", "RESOLVE", "RESOLVE",
                    "RESOLVE", "RESOLVE", "TAKEOVER", "TAKEOVER", "TAKEOVER"]


def test_sequence_text():
    plan = layers.sequence_plan("25L", "N101", 4000, 46, "N204", 4300, 46)
    assert plan.order == ["N101", "N204"] and plan.extend_s["N204"] >= 10
    assert layers.sequence_text("N204", plan, "DOWNWIND", "N101")[0].startswith("NUMBER 2 - EXTEND DOWNWIND")


# ---- authority
CTX = {"ias_kt": 90, "agl_ft": 1000, "alt_msl_ft": 2500, "leg": "DOWNWIND", "tpa_msl_ft": 2478,
       "ap_equipped": True, "stick_active": False}


def cmd(bank=0.0, vs=0.0, hold=10.0):
    return {"type": "COMMAND", "ac_id": "N101", "t": 0.0, "mode": "TAKEOVER", "bank_cmd_deg": bank,
            "vs_cmd_fpm": vs, "hold_s": hold}


def test_authority_clips_45_to_30():
    v = authority.vet(cmd(bank=-45), CTX)
    assert v.ok and v.command["bank_cmd_deg"] == -30 and any("45" in c for c in v.clipped)
    schemas.Command.model_validate(v.command)


def test_authority_rejections():
    assert not authority.vet(cmd(30), dict(CTX, ap_equipped=False)).ok
    assert not authority.vet(cmd(30), dict(CTX, stick_active=True)).ok
    assert not authority.vet(cmd(30), dict(CTX, ias_kt=60)).ok
    assert not authority.vet(cmd(30), dict(CTX, leg="FINAL", agl_ft=250)).ok
    assert authority.vet(cmd(30), dict(CTX, leg="FINAL", agl_ft=350)).ok
    assert not authority.vet(cmd(0, vs=-500), dict(CTX, alt_msl_ft=2200)).ok      # would go below TPA - 300
    assert authority.vet(cmd(30, hold=25), CTX).command["hold_s"] == 10


def test_authority_release_on_stick_and_timeout():
    m = authority.AuthorityMonitor("N101")
    m.grant(cmd(30), 100.0)
    assert m.check(105.0, CTX, True) is None
    assert m.check(110.1, CTX, True)[1] == "hold timeout"
    m.grant(cmd(30), 200.0)
    cmd_, cause = m.on_stick(200.2)
    assert cause == "stick" and cmd_["mode"] == "RELEASE"
    schemas.Command.model_validate(cmd_)
    assert authority.release_text(-10)[0] == "YOUR AIRCRAFT - CONTINUE LEFT TURN"


def test_authority_imports_nothing_from_the_smart_logic():
    src = open(authority.__file__).read()
    assert "node.predict" not in src and "node.escape" not in src and "from node" not in src


# ---- escape
def test_climb_rate_by_density_altitude():
    assert escape.climb_rate_fpm(0) == 730 and abs(escape.climb_rate_fpm(5000) - 500) < 1
    assert abs(escape.climb_rate_fpm(8000) - 300) < 1


def _head_on(own_z=300.0, peer_z=300.0, terrain=450.0):
    own = escape.OwnState(0, -1250, own_z, 0.0, 46.0, 0.0, 0.0, 90.0, 300.0, 4980.0, own_z)
    grid = np.arange(0, 30.01, 0.5)
    hold = np.column_stack([np.zeros_like(grid), -1250 + 46 * grid, np.full_like(grid, own_z)])
    peer = np.column_stack([np.zeros_like(grid), 1250 - 46 * grid, np.full_like(grid, peer_z)])
    return own, hold, {"B": peer}, (lambda x, y: terrain)


def test_escape_head_on_picks_a_turn_and_explains():
    own, hold, peers, terr = _head_on(terrain=100.0)
    r = escape.evaluate(own, hold, peers, terr)
    assert r.base_margin < 1.0 and r.chosen is not None and r.chosen.margin >= escape.MARGIN_OK
    assert "L45" in r.rejected and "authority" in r.rejected["L45"]


def test_escape_terrain_floor_blocks_everything_low():
    # a ridge 120 m high ahead (y > -800): holding course meets it too, so nothing straight or lateral is acceptable
    own, hold, peers, _ = _head_on(own_z=450 + 80, peer_z=450 + 80)
    r = escape.evaluate(own, hold, peers, lambda x, y: 450.0 + (120.0 if y > -800.0 else 0.0))
    assert r.chosen is None and r.rejected["L30"] == "terrain floor"


def test_low_aircraft_may_climb_but_not_descend_toward_the_ground():
    own, hold, peers, terr = _head_on(own_z=450 + 80, peer_z=450 + 80)
    r = escape.evaluate(own, hold, peers, lambda x, y: 450.0)
    assert r.rejected["DESCEND"] == "terrain floor" and r.rejected.get("CLIMB") != "terrain floor"


# ---- negotiate
def _ev(peer_commit):
    own, hold, peers, terr = _head_on(terrain=100.0)
    if peer_commit:
        peers = {"B": escape.peer_commit_path({"x": 0, "y": 1250, "z": 300.0, "hdg": 180.0, "gs": 46.0},
                                              peer_commit, 0.0, peers["B"])}
    return escape.evaluate(own, hold, peers, terr, require_maneuver=bool(peer_commit))


def test_negotiation_lower_first_higher_responds_and_link_loss_falls_back():
    lo, hi = Negotiator("A"), Negotiator("B")
    lo.note_rx("B", 0.0)
    hi.note_rx("A", 0.0)
    d1 = lo.decide(1.0, "B", _ev, lambda: None)
    assert d1.basis == "lower-first" and d1.commit is not None
    hi.on_commit("A", d1.commit, 1.3)
    d2 = hi.decide(1.3, "A", _ev, lambda: None)
    assert d2.basis == "responding" and d2.commit is not None
    assert lo.decide(1.2, "B", _ev, lambda: None).commit is None          # sticky: same inputs, no new commit
    lost = Negotiator("A")
    lost.note_rx("B", 0.0)
    d3 = lost.decide(10.0, "B", _ev, lambda: None)
    assert d3.basis == "fallback-link-lost" and d3.sense == "R" and d3.cand == "R30"


# ---- trust
def test_trust_states_and_caps():
    t = TrustTable()
    t.update("A", 0.93)
    t.update("B", 0.55)
    t.update("G", 0.12)
    t.update("C", 0.5, camera_only=True)
    assert [t.state(p) for p in "ABGC"] == ["TRUSTED", "SUSPICIOUS", "FAKE", "CAMERA_ONLY"]
    assert t.cap("A") == "TAKEOVER" and t.cap("B") == "TRAFFIC" and t.cap("G") is None and t.cap("C") == "TRAFFIC"
    assert not t.may_negotiate("B") and t.may_negotiate("A")
    schemas.Trust.model_validate(t.frame("N101", 0.0, ["A", "B", "G"]))


# ---- node integration: frames honour the contract, layers escalate in order
def test_scenario_base_cutoff_end_to_end():
    ac = load_scenario("harness/scenarios/base_cutoff_conflict.json", PATS, comply=0.0)
    sim = Sim(PATS, ac, lambda i: Node(i, patterns=PATS), loss=0.0, latency_s=0.3, dt=0.1, follow_sequence=False)
    res = sim.run(175)
    order = ["SEQUENCE", "TRAFFIC", "RESOLVE", "TAKEOVER"]
    t = [res.first_level_t[("N101", lv)] for lv in order]
    assert t == sorted(t) and t[0] < T0 + 60
    for _, _, f in res.advisories:
        schemas.Advisory.model_validate(f)
    for _, _, f in res.commands:
        schemas.Command.model_validate(f)
    assert all(f["reason"] for _, _, f in res.commands)
    assert res.tick_ms_mean < 10.0


# ---- pull-queue scenarios: straight-in that looks like base, spoofed GHOST7 on final
def test_straight_in_crabbing_is_not_read_as_base():
    for crab in (-25.0, 25.0):
        st = st_on("STRAIGHT_IN", along_m=P25L.legs["STRAIGHT_IN"].length - 900)
        st.track = (st.track + crab) % 360.0
        c = PRED.classify(st)
        assert c.leg != "BASE", (crab, c)
        p = PRED.predict(st, 0.0, c)
        if p.method == "turn-aware":                     # confident -> it must not invent a base turn
            assert p.next_turn is None or p.next_turn[0] != "BASE"


def test_fake_target_is_never_acted_on():
    node = Node("N101", patterns=PATS)
    node.trust.update("GHOST7", 0.12, ["no_corroboration", "kinematics_violation"])
    pl = P25L.place("FINAL", 0.0, 90.0, 800)
    ox, oy = pl["x_m"], pl["y_m"]
    from node.geometry import to_latlon, hvec
    hx, hy = hvec(pl["hdg_deg"])
    levels, cmds = set(), []
    for k in range(0, 400):                                  # 40 s at 10 Hz, GHOST7 head-on 1.2 km ahead
        t = 1.0e9 + k * 0.1
        x, y = ox + hx * 46.3 * k * 0.1, oy + hy * 46.3 * k * 0.1
        lat, lon = to_latlon(x, y)
        own = {"type": "OWNSHIP", "ac_id": "N101", "t": t, "lat": lat, "lon": lon, "alt_msl_ft": 1830.0, "alt_press_ft": 1820.0,
               "agl_ft": 352.0, "gs_kt": 90.0, "track_deg": pl["hdg_deg"], "hdg_deg": pl["hdg_deg"], "bank_deg": 0.0, "vs_fpm": 0.0,
               "ias_kt": 90.0, "ap_equipped": True, "stick_active": False, "flaps": 0}
        if k % 10 == 0:
            gl, gn = to_latlon(x + hx * 1200 - hx * 46.3 * 0.0, y + hy * 1200)
            node.on_radio({"msg": "STATE", "from": "GHOST7", "seq": k + 1, "t": t, "sig": "",
                           "body": {"lat": gl, "lon": gn, "alt_press_ft": 1820.0, "gs_kt": 90.0,
                                    "track_deg": (pl["hdg_deg"] + 180.0) % 360.0, "vs_fpm": 0.0, "leg": "UNKNOWN",
                                    "intent": "", "ap_equipped": False}})
        frames, _ = node.tick(own)
        for f in frames:
            if f["type"] == "ADVISORY":
                levels.add(f["level"])
            if f["type"] == "COMMAND":
                cmds.append(f)
    assert not cmds and not (levels & {"RESOLVE", "TAKEOVER", "SEQUENCE", "TRAFFIC"}), levels
    tr = [f for f in frames if f["type"] == "TRUST"] or [node.trust.frame("N101", t, ["GHOST7"], node._rels(t))]
    assert tr[-1]["targets"][0]["state"] == "FAKE" and "rel" in tr[-1]["targets"][0]


# ---- Phase 1 hardening: R/R head-on, peer PREDICTION, live DA, obstacles, cost tie-breaker
def test_head_on_prefers_right_turns_and_reports_cost():
    own, hold, peers, terr = _head_on(terrain=100.0)
    r = escape.evaluate(own, hold, peers, terr)
    assert r.chosen.cand.name.startswith("R"), [e.cand.name for e in r.ranked[:4]]
    why = r.reason()
    assert why["cost"]["chosen"]["fuel_gal"] >= 0 and "extra" in why["cost"]["note"]
    assert set(why["cost"]["rejected"]) >= {"L20", "R30", "CLIMB"}


def test_both_aircraft_of_a_head_on_choose_right():
    own, hold, peers, terr = _head_on(terrain=100.0)
    mine = escape.evaluate(own, hold, peers, terr).chosen.cand
    peer_view = escape.OwnState(0, 1250, 300.0, 180.0, 46.0, 0.0, 0.0, 90.0, 300.0, 4980.0, 300.0)
    their_hold = peers["B"]
    their_peers = {"A": hold}
    theirs = escape.evaluate(peer_view, their_hold, their_peers, terr).chosen.cand
    assert mine.sense == "R" and theirs.sense == "R"


def test_cost_never_beats_safety():
    own, hold, peers, terr = _head_on(terrain=100.0)
    r = escape.evaluate(own, hold, peers, terr)
    cheap_unsafe = [e for e in r.evals if e.cand.kind == "descend"]
    assert r.chosen.margin >= escape.MARGIN_OK
    assert all(e.cand.name != "DESCEND" or e.margin >= escape.MARGIN_OK for e in [r.chosen])
    assert cheap_unsafe and cheap_unsafe[0].cand.name in r.rejected or cheap_unsafe[0].margin >= escape.MARGIN_OK


def test_live_density_altitude_changes_climb_and_reason_text():
    own, hold, peers, terr = _head_on(terrain=100.0)
    own.da_ft = 8000.0
    r = escape.evaluate(own, hold, peers, terr)
    assert r.env["da_ft"] == 8000 and abs(r.env["climb_fpm"] - 300) <= 1
    if "CLIMB" in r.rejected and "performance" in r.rejected["CLIMB"]:
        assert "at DA 8,000 ft" in r.rejected["CLIMB"]
    n = Node("N101", patterns=PATS)
    n.on_env({"type": "ENV", "da_field_ft": 8000.0})
    assert n.da_ft == 8000.0


def test_performance_rejection_text_names_the_density_altitude():
    own, hold, peers, terr = _head_on(own_z=300.0, peer_z=300.0, terrain=100.0)
    own.da_ft = 12500.0                                        # 80 fpm: below the 250 fpm floor
    r = escape.evaluate(own, hold, peers, terr)
    assert r.rejected["CLIMB"].startswith("performance") and "at DA 12,500 ft" in r.rejected["CLIMB"]


def test_obstacle_fn_is_used_when_data_obstacles_exists(monkeypatch):
    import sys
    import types
    mod = types.ModuleType("data.obstacles")
    mod.obstacle_fn_enu = lambda to_latlon: (lambda x, y: 2743.0 if abs(x) > 150.0 else None)   # tall obstacles either side
    monkeypatch.setitem(sys.modules, "data.obstacles", mod)
    n = Node("N101", patterns=PATS)
    assert n.obstacle_fn is not None and n.obstacle_fn(500.0, 0.0) == 2743.0 and n.obstacle_fn(0.0, 0.0) is None
    own, hold, peers, terr = _head_on(terrain=100.0)
    r = escape.evaluate(own, hold, peers, terr, obstacle_fn=n.obstacle_fn)
    assert r.rejected.get("L30") == "obstacle" and r.rejected.get("R30") == "obstacle"


def test_peer_prediction_frames_are_published_for_the_god_view():
    ac = load_scenario("harness/scenarios/base_cutoff_conflict.json", PATS, comply=0.0)
    sim = Sim(PATS, ac, lambda i: Node(i, patterns=PATS), loss=0.0, latency_s=0.3, dt=0.1, follow_sequence=False)
    sim.run(70)
    node = sim.aircraft["N101"].node
    preds = [f for k, _, f in node.sent if k == "world" and f["type"] == "PREDICTION"]
    peer = [f for f in preds if f["target_id"] == "N399"]
    own = [f for f in preds if f["target_id"] is None]
    assert own and peer
    for f in peer[-3:]:
        schemas.Prediction.model_validate(f)
    assert peer[-1]["ac_id"] == "N101" and peer[-1]["path"][0]["t"] == 0.0 and len(peer[-1]["path"]) == 19


def test_resolve_advice_matches_what_takeover_flies():
    """AP-equipped: a maneuver the bounds monitor would refuse (descent below TPA-300) is never advised or committed."""
    ac = load_scenario("harness/scenarios/base_cutoff_conflict.json", PATS, comply=0.0)
    sim = Sim(PATS, ac, lambda i: Node(i, patterns=PATS), loss=0.0, latency_s=0.3, dt=0.1, follow_sequence=False)
    res = sim.run(175)
    for aid in ("N101",):                                       # the AP-equipped aircraft
        advised = [f["reason"].get("chosen") for _, i, f in res.advisories if i == aid and f["level"] == "RESOLVE"]
        flown = [f["reason"].get("chosen") for _, i, f in res.commands if i == aid and f["mode"] == "TAKEOVER"]
        assert advised and flown
        cmd = next(f for _, i, f in res.commands if i == aid and f["mode"] == "TAKEOVER")
        # same maneuver, or the log says exactly what changed between the advice and the takeover
        assert flown[0] in advised or "changed_from" in cmd["reason"], (advised, flown)
        if "changed_from" in cmd["reason"]:
            assert cmd["reason"]["changed_from"]["advised"] in advised and cmd["reason"]["changed_from"]["why"]


def test_bounds_blocked_candidate_is_rejected_with_reason():
    own, hold, peers, terr = _head_on(terrain=100.0)
    r = escape.evaluate(own, hold, peers, terr, blocked={"R20": "bounds: test", "L20": "bounds: test"})
    assert r.rejected["R20"] == "bounds: test" and r.chosen.cand.name not in ("R20", "L20")


# ---- go-around, kept sequencing, head-on ordering
def test_go_around_candidate_only_on_the_approach():
    assert "GO_AROUND" not in [c.name for c in escape.candidates(500.0)]
    ga = [c for c in escape.candidates(500.0, 0.0, go_around=True) if c.name == "GO_AROUND"][0]
    assert ga.bank == 0.0 and ga.vs_fpm == 500.0 and ga.sense == "CLIMB"
    own, hold, peers, terr = _head_on(terrain=100.0)
    r = escape.evaluate(own, hold, peers, terr, go_around=True)
    assert "GO_AROUND" in [e.cand.name for e in r.evals]
    assert layers.maneuver_text("GO_AROUND", "N204", None)[0].startswith("GO AROUND - CLIMB STRAIGHT AHEAD")


def test_head_on_both_aircraft_take_over_in_the_same_sense():
    import json
    for seed in (15, 20):                              # two seeds where the takeover used to flip one aircraft to the left
        ac = load_scenario("harness/scenarios/head_on_judges.json", PATS, comply=0.0)
        res = Sim(PATS, ac, lambda i: Node(i, patterns=PATS, record=False), loss=0.1, latency_s=0.3, dt=0.1, seed=seed,
                  follow_sequence=False).run(130)
        banks = {i: f["bank_cmd_deg"] for _, i, f in res.commands if f["mode"] == "TAKEOVER"}
        assert len(banks) == 2 and all(b > 0 for b in banks.values()), (seed, banks)
        assert res.min_h_m / FT > 700, (seed, res.min_h_m / FT)


def test_sequencing_is_kept_after_a_maneuver_cleared_the_conflict():
    from harness.montecarlo import make_encounter, DT, DURATION_S
    kept = 0
    for seed in (2009, 2011, 2015, 2022):
        ac = make_encounter("base_vs_straight_in", seed, PATS)
        res = Sim(PATS, ac, lambda i: Node(i, patterns=PATS, record=False), loss=0.1, latency_s=0.3, dt=DT, seed=seed).run(DURATION_S)
        kept += sum(1 for _, _, f in res.advisories if f["reason"].get("kept_after") == "maneuver")
    assert kept > 0
