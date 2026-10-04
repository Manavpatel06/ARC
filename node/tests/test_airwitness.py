"""AirWitness anti-spoofing tests T1-T20 (python -m pytest node/tests/test_airwitness.py -q)."""
import random

import pytest

from harness.rf_sim import measure
from node.airwitness import (AirWitness, QUARANTINED, SUSPICIOUS, UNVERIFIED, VERIFIED)
from node.geometry import FT, KT, build_patterns, to_enu, to_latlon
from node.node import Node

ALT = 2500.0


def env(frm, seq, t, x, y, alt=ALT, gs=90.0, trk=90.0, vs=0.0, auth="ok", msg="STATE", body=None, rx=None, rf=None):
    lat, lon = to_latlon(x, y)
    if body is None:
        body = {"lat": lat, "lon": lon, "alt_press_ft": alt, "gs_kt": gs, "track_deg": trk, "vs_fpm": vs,
                "leg": "UNKNOWN", "intent": "", "ap_equipped": True}
    e = {"msg": msg, "from": frm, "seq": seq, "t": t, "sig": "", "body": body, "_rx_t": rx if rx is not None else t}
    if auth is not None:
        e["_auth"] = auth
    if rf is not None:
        e["_rf"] = rf
    return e


def own(aw, t, x=0.0, y=0.0, trk=90.0, gs=90.0, alt=ALT, press=None):
    lat, lon = to_latlon(x, y)
    aw.on_ownship({"t": t, "lat": lat, "lon": lon, "alt_msl_ft": alt, "alt_press_ft": press if press is not None else alt,
                   "gs_kt": gs, "track_deg": trk, "vs_fpm": 0.0})


def fly(aw, frm, t0, n, x0, y0, trk=90.0, gs=90.0, seq0=1, auth="ok", alt=ALT, turn_dps=0.0, vs=0.0):
    """n STATEs at 1 Hz along a (possibly turning) track; returns (next t, next seq, last x, last y)."""
    import math
    x, y, h = x0, y0, trk
    for k in range(n):
        t = t0 + k
        aw.on_message(env(frm, seq0 + k, t, x, y, alt=alt + vs * k / 60.0, gs=gs, trk=h % 360, vs=vs, auth=auth), t)
        r = math.radians(h)
        x, y = x + math.sin(r) * gs * KT, y + math.cos(r) * gs * KT
        h += turn_dps
    return t0 + n, seq0 + n, x, y


def challenge(aw, frm, now, auth="ok", seq=1000):
    out = aw.tick(now)
    nonce = out.get("c", {}).get(frm)
    assert nonce is not None
    aw.on_message(env(frm, seq, now + 0.5, 0, 0, msg="HEARTBEAT", body={"alive": True, "r": {aw.own_id: nonce}},
                      auth=auth), now + 0.5)


def mk(signed=True, witness=None):
    aw = AirWitness("N101", to_enu, signed_radio=signed, nonce_fn=lambda c=iter(range(100, 10000)): next(c))
    if witness is not None:
        aw.peer_evidence = lambda tid: (1.0, witness.get(tid, []))
    own(aw, 0.0)
    return aw


def settle(aw, tid, t):
    aw.assess(tid, t)
    return aw.assess(tid, t + 3.0)               # past the 2 s upgrade hysteresis


# T1
def test_t1_valid_signed_aircraft_is_verified():
    aw = mk()
    t, s, _, _ = fly(aw, "N204", 1.0, 5, 0, 2000, trk=270)
    challenge(aw, "N204", t, seq=s)
    r = settle(aw, "N204", t + 1)
    assert r.state == VERIFIED and r.may_coordinate and r.cap == "TAKEOVER", r.explain()


# T2
def test_t2_invalid_signature_never_verified():
    aw = mk(witness={"X1": ["corroborated:3"]})
    t, s, _, _ = fly(aw, "X1", 1.0, 6, 0, 2000, trk=270, auth="unknown_key")
    challenge(aw, "X1", t, auth="unknown_key", seq=s)
    r = settle(aw, "X1", t + 1)
    assert r.state != VERIFIED and r.checks["signature"].startswith("FAIL") and not r.may_coordinate


# T3
def test_t3_recorded_packet_replay_detected():
    aw = mk()
    t, s, x, y = fly(aw, "N204", 1.0, 40, 0, 2000, trk=270)
    old = env("N204", 2, 2.0, 0, 2000, trk=270, rx=t)        # packet seq 2 recorded at t=2, replayed at t=41
    old["t"] = t - 0.5                                           # attacker re-stamps it to look fresh
    assert aw.on_message(old, t) is False
    r = aw.assess("N204", t)
    assert r.checks["replay"] == "DETECTED" and r.state in (SUSPICIOUS, QUARANTINED)


# T4
def test_t4_duplicate_packet_ignored():
    aw = mk()
    t, s, x, y = fly(aw, "N204", 1.0, 3, 0, 2000)
    dup = env("N204", s - 1, t - 1, x, y)
    assert aw.on_message(dup, t) is False
    assert aw.assess("N204", t).checks["replay"] == "DETECTED"


# T5
def test_t5_stale_timestamp_rejected():
    aw = mk()
    t, s, x, y = fly(aw, "N204", 1.0, 3, 0, 2000)
    assert aw.on_message(env("N204", s, t - 5.0, x, y, rx=t), t) is False
    assert aw.assess("N204", t).checks["freshness"].startswith("FAIL")


# T6
def test_t6_impossible_jump():
    aw = mk()
    t, s, x, y = fly(aw, "X2", 1.0, 3, 0, 2000)
    aw.on_message(env("X2", s, t, x + 3000, y), t)
    r = aw.assess("X2", t)
    assert "position_jump" in r.checks["kinematics"] and r.state in (SUSPICIOUS, QUARANTINED)


# T7
def test_t7_impossible_turn_rate():
    aw = mk()
    t, s, x, y = fly(aw, "X3", 1.0, 3, 0, 2000, trk=90)
    aw.on_message(env("X3", s, t, x, y, trk=270), t)
    assert "turn_rate" in aw.assess("X3", t).checks["kinematics"]


# T8
def test_t8_intent_right_but_track_left_widens_tube_only():
    aw = mk()
    t, s, x, y = fly(aw, "N204", 1.0, 3, 0, 2000, trk=270)
    challenge(aw, "N204", t, seq=s)
    settle(aw, "N204", t)
    aw.on_message(env("N204", s + 1, t + 0.6, x, y, msg="MANEUVER_COMMIT", rx=t + 0.6,
                      body={"target": "N101", "sense": "R", "bank_deg": 30, "vs_fpm": 0, "start_t": t, "hold_s": 10}), t + 0.6)
    t2, s2, _, _ = fly(aw, "N204", t + 1, 14, x, y, trk=270, turn_dps=-4.0, seq0=s + 2)   # flies LEFT instead
    r = aw.assess("N204", t2)
    assert r.checks["intent"].startswith("INCONSISTENT") and r.sigma_extra > 1.0
    assert r.state == VERIFIED                         # a pilot ignoring advice is not a spoofer


# T9
def test_t9_legacy_unsigned_aircraft_still_tracked_and_warned():
    aw = mk()
    t, s, _, _ = fly(aw, "ADSB1", 1.0, 6, 0, 3000, trk=270, auth="unsigned")
    r = settle(aw, "ADSB1", t)
    assert r.state == UNVERIFIED and r.cap is not None and not r.may_coordinate


# T10
def test_t10_single_uncorroborated_ghost():
    aw = mk(witness={"GHOST7": ["not_heard_by:N204,N311", "no_corroboration"]})
    t, s, _, _ = fly(aw, "GHOST7", 1.0, 6, 0, 120, trk=270, auth="unsigned")
    for k in range(5):                                   # it really transmits from a site 2.5 km south
        aw.ingest_rf(measure("N101", (0, 0, ALT * FT), "GHOST7", (0, -2500, 0), t - 5 + k, kinds=("rssi",),
                             rng=random.Random(k)))
    r = aw.assess("GHOST7", t)
    assert r.state in (SUSPICIOUS, QUARANTINED) and r.cap in ("TRAFFIC", None)


# T11
def test_t11_three_peers_corroborate_real_target():
    aw = mk(witness={"N204": ["signed", "corroborated:3"]})
    t, s, _, _ = fly(aw, "N204", 1.0, 5, 0, 2000, trk=270)
    r = settle(aw, "N204", t)
    assert r.state == VERIFIED and "3 corroborating" in r.checks["witnesses"]


# T12
def test_t12_same_identity_in_two_places():
    aw = mk()
    for k in range(8):                                   # two transmitters share one id, 2 km apart
        x = 0.0 if k % 2 == 0 else 2000.0
        aw.on_message(env("N399", k + 1, 1.0 + k * 0.5, x + 23 * k, 3000), 1.0 + k * 0.5)
    r = aw.assess("N399", 5.0)
    assert r.checks["duplicate_id"] == "DETECTED" and r.state in (SUSPICIOUS, QUARANTINED)


# T13
def test_t13_five_identities_one_transmitter():
    aw = mk()
    for i in range(5):
        tid = f"GHOST{i}"
        fly(aw, tid, 1.0, 4, 300.0 * i, 1500, auth="unsigned", seq0=1)
        for k in range(4):
            aw.ingest_rf(measure("N101", (0, 0, ALT * FT), tid, (0, -2500, 0), 2.0 + k, kinds=("rssi",),
                                 rng=random.Random(10 * i + k), source_id="TX-1"))
    for i in range(5):
        r = aw.assess(f"GHOST{i}", 5.0)
        assert r.checks.get("sybil", "").startswith("SAME TRANSMITTER") and r.state != VERIFIED


# T14
def test_t14_claimed_position_vs_rf_range():
    aw = mk()
    t, s, _, _ = fly(aw, "N204", 1.0, 6, 0, 120, trk=270)          # claims to be 120 m away
    for k in range(5):
        aw.ingest_rf(measure("N101", (0, 0, ALT * FT), "N204", (0, -3000, ALT * FT), t - 5 + k, kinds=("range",),
                             rng=random.Random(k)))
    r = aw.assess("N204", t)
    assert r.checks["rf_range"].startswith("CONFLICT") and r.state in (SUSPICIOUS, QUARANTINED)


# T15
def test_t15_claimed_position_vs_tdoa():
    aw = mk()
    t, s, _, _ = fly(aw, "N204", 1.0, 6, 0, 1500, trk=270)
    for k in range(5):
        aw.ingest_rf(measure("N311", (-3000, 0, ALT * FT), "N204", (2500, -2500, ALT * FT), t - 5 + k, kinds=("tdoa",),
                             rng=random.Random(k), ref_pos=(3000, 0, ALT * FT)))
    r = aw.assess("N204", t)
    assert r.checks["tdoa"].startswith("CONFLICT") and r.state != VERIFIED


# T16
def test_t16_gnss_jump_inconsistent_with_motion():
    aw = mk()
    for k in range(5):
        own(aw, 1.0 + k * 0.1, x=46.3 * 0.1 * k)
    own(aw, 1.5, x=46.3 * 0.5 + 500.0)                  # 500 m east in 0.1 s, no matching motion
    assert aw.pnt.state == "DEGRADED" and aw.pnt.extra_sigma_m > 500


# T17
def test_t17_packet_loss_is_not_spoofing():
    aw = mk()
    t, s, x, y = fly(aw, "N204", 1.0, 3, 0, 2000, trk=270)
    t2, s2, _, _ = fly(aw, "N204", t + 4, 5, x - 46.3 * 4, y, trk=270, seq0=s + 6)      # 4 s silence, seqs skipped
    for k in range(3):                                   # challenges go unanswered (lost)
        aw.tick(t2 + 10 * k)
        aw.tick(t2 + 10 * k + 6)
    r = aw.assess("N204", t2 + 30)
    assert r.checks["replay"] == "PASS" and r.state not in (SUSPICIOUS, QUARANTINED)


# T18
def test_t18_legitimate_unusual_maneuver():
    aw = mk()
    fly(aw, "N204", 1.0, 12, 0, 2000, trk=270, turn_dps=9.0, vs=1500, gs=95)        # 45-60 deg bank turn + climb
    r = aw.assess("N204", 13.0)
    assert r.checks["kinematics"] == "PASS"


# T19
def test_t19_suspicious_target_cannot_force_takeover():
    pats = build_patterns()
    node = Node("N101", patterns=pats)
    node.aw.signed_radio = True
    pl = pats["25L"].place("DOWNWIND", 20.0, 90.0)
    hx = 1.0
    cmds, levels, commits = [], set(), []
    for k in range(450):
        t = 1.0e9 + k * 0.1
        x = pl["x_m"] + 46.3 * k * 0.1 * hx
        lat, lon = to_latlon(x, pl["y_m"])
        o = {"type": "OWNSHIP", "ac_id": "N101", "t": t, "lat": lat, "lon": lon, "alt_msl_ft": 2500.0,
             "alt_press_ft": 2500.0, "agl_ft": 1022.0, "gs_kt": 90.0, "track_deg": 86.0, "hdg_deg": 86.0, "bank_deg": 0.0,
             "vs_fpm": 0.0, "ias_kt": 90.0, "ap_equipped": True, "stick_active": False, "flaps": 0}
        if k % 10 == 0:                                  # unsigned GHOST7, head-on, 2.5 km ahead closing
            gx = pl["x_m"] + 2500.0 - 46.3 * k * 0.1
            g = env("GHOST7", k + 1, t, gx, pl["y_m"], trk=266.0, auth="unsigned", rx=t)
            node.on_radio(g)
        frames, radios = node.tick(o)
        for f in frames:
            if f["type"] == "COMMAND":
                cmds.append(f)
            if f["type"] == "ADVISORY":
                levels.add(f["level"])
        commits += [b for m, b in radios if m == "MANEUVER_COMMIT"]
    assert not cmds and not commits and "TAKEOVER" not in levels
    assert node.trust.aw_state("GHOST7") != VERIFIED


# T20
def test_t20_verified_target_can_coordinate():
    aw = mk(witness={"N204": ["signed", "corroborated:2"]})
    t, s, _, _ = fly(aw, "N204", 1.0, 5, 0, 2000, trk=270)
    challenge(aw, "N204", t, seq=s)
    r = settle(aw, "N204", t)
    assert r.state == VERIFIED and r.may_coordinate and r.use_claimed_intent and r.sigma_scale == 1.0


def test_explanation_is_human_readable():
    aw = mk(witness={"GHOST7": ["not_heard_by:N204"]})
    t, s, _, _ = fly(aw, "GHOST7", 1.0, 5, 0, 120, auth="unsigned")
    text = aw.assess("GHOST7", t).explain()
    assert "TARGET: GHOST7" in text and "signature" in text and "Action:" in text


# ---- software-only additions
def test_pop_in_target_near_us_is_flagged():
    aw = mk()
    t, s, _, _ = fly(aw, "GHOST7", 1.0, 4, 0, 600, auth="unsigned")
    r = aw.assess("GHOST7", t)
    assert "pop_in" in r.checks and r.state in (SUSPICIOUS, QUARANTINED)


def test_altitude_claim_inconsistent_with_local_atmosphere():
    aw = mk()
    own(aw, 0.5, alt=2500, press=2480)                   # our GNSS-baro offset: +20 ft
    for k in range(4):
        e = env("X9", k + 1, 1.0 + k, 0, 3000 - 46 * k, trk=180)
        e["body"]["alt_geo_ft"] = ALT + 900               # claims a 900 ft offset: not the same air mass
        aw.on_message(e, 1.0 + k)
    r = aw.assess("X9", 5.0)
    assert r.checks["baro_geo"].startswith("FAIL") and r.state in (SUSPICIOUS, QUARANTINED)
    aw2 = mk()
    own(aw2, 0.5, alt=2500, press=2480)
    for k in range(4):
        e = env("N204", k + 1, 1.0 + k, 0, 3000 - 46 * k, trk=180)
        e["body"]["alt_geo_ft"] = ALT + 30
        aw2.on_message(e, 1.0 + k)
    assert aw2.assess("N204", 5.0).checks["baro_geo"] == "PASS"


def test_negotiation_is_liveness_evidence():
    aw = mk()
    t, s, x, y = fly(aw, "N204", 1.0, 4, 0, 2000, trk=270)
    aw.note_sent("MANEUVER_COMMIT", {"target": "N204", "sense": "R"}, t)
    aw.on_message(env("N204", s, t + 0.4, x, y, msg="MANEUVER_COMMIT", rx=t + 0.4,
                      body={"target": "N101", "sense": "R", "bank_deg": 20, "vs_fpm": 0, "start_t": t, "hold_s": 10}), t + 0.4)
    r = settle(aw, "N204", t + 1)
    assert r.checks["negotiation"].startswith("PASS") and r.state == VERIFIED
    aw2 = mk()
    t, s, x, y = fly(aw2, "N205", 1.0, 4, 0, 2000, trk=270)
    aw2.note_sent("MANEUVER_COMMIT", {"target": "N205", "sense": "R"}, t)
    t2, _, _, _ = fly(aw2, "N205", t, 8, x, y, trk=270, seq0=s)                       # keeps talking, never answers
    aw2.tick(t2)
    assert aw2.assess("N205", t2).checks["negotiation"].startswith("NO ANSWER")


def test_verified_peers_rssi_ranges_locate_a_transmitter():
    aw = mk(signed=False)
    t, s, _, _ = fly(aw, "N204", 1.0, 5, -6000, 0, trk=90)        # a verified peer 6 km west
    settle(aw, "N204", t)
    t2, _, _, _ = fly(aw, "GHOST7", 1.0, 5, 0, 900, trk=270)       # claims 900 m north of us
    peer_pos = aw._claimed_at(aw.tracks["N204"], t)[:3]
    for k in range(4):                                             # N204 hears it from a transmitter right next to N204
        tx = (peer_pos[0] + 200.0, peer_pos[1] + 100.0, peer_pos[2])      # RSSI only catches gross lies (factor ~1.4)
        rng = measure("N204", peer_pos, "GHOST7", tx, t + k, kinds=("rssi",), rng=random.Random(k))
        aw.on_message(env("N204", 100 + k, t + k, -6000 + 46 * (5 + k), 0, msg="HEARTBEAT",
                          body={"alive": True, "w": {"GHOST7": rng.estimated_range_m}}), t + k)
    r = aw.assess("GHOST7", t + 4)
    assert any(m.observer_id == "N204" for m, _ in aw.tracks["GHOST7"].rf)
    assert r.checks.get("rf_range", "").startswith("CONFLICT")


def test_node_trust_frame_carries_a_spoof_alert_once():
    pats = build_patterns()
    node = Node("N101", patterns=pats)
    node.aw.signed_radio = True
    pl = pats["25L"].place("DOWNWIND", 20.0, 90.0)
    alerts = []
    for k in range(80):
        t = 1.0e9 + k * 0.1
        lat, lon = to_latlon(pl["x_m"] + 4.6 * k, pl["y_m"])
        o = {"type": "OWNSHIP", "ac_id": "N101", "t": t, "lat": lat, "lon": lon, "alt_msl_ft": 2500.0,
             "alt_press_ft": 2500.0, "agl_ft": 1022.0, "gs_kt": 90.0, "track_deg": 86.0, "hdg_deg": 86.0, "bank_deg": 0.0,
             "vs_fpm": 0.0, "ias_kt": 90.0, "ap_equipped": True, "stick_active": False, "flaps": 0}
        if k % 10 == 0:                                   # 400 kt ghost with jumps
            node.on_radio(env("GHOST7", k + 1, t, pl["x_m"] + 2000 + (1500 if k % 20 else 0), pl["y_m"], gs=400,
                              trk=266.0, auth="unsigned", rx=t))
        frames, _ = node.tick(o)
        alerts += [a for f in frames if f["type"] == "TRUST" for a in f.get("alerts", [])]
    ghost = [a for a in alerts if a["id"] == "GHOST7"]
    assert ghost and ghost[-1]["text"].startswith(("SPOOFED", "UNCONFIRMED")) and len(ghost) <= 2
