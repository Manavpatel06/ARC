"""AirWitness-Hybrid (passive) tests 1-27 from the spec, plus regression checks
(python -m pytest node/tests/test_airwitness.py -q).  Nothing here sends a challenge or waits for an answer."""
import math
import random
from types import SimpleNamespace

from harness.rf_sim import measure
from node.airwitness import (AirWitness, InterrogationReplyEvidence, ModeSObservation, QUARANTINED, RFMeasurement,
                             SIGMA_SCALE, SUSPICIOUS, TCASMeasurement, TrustResult, UNVERIFIED, VERIFIED)
from node.geometry import FT, KT, build_patterns, to_enu, to_latlon
from node.node import Node, trust_robust

ALT = 2500.0


def env(frm, seq, t, x, y, alt=ALT, gs=90.0, trk=90.0, vs=0.0, auth="ok", msg="STATE", body=None, rx=None, rf=None,
        sid=None):
    lat, lon = to_latlon(x, y)
    if body is None:
        body = {"lat": lat, "lon": lon, "alt_press_ft": alt, "gs_kt": gs, "track_deg": trk, "vs_fpm": vs,
                "leg": "UNKNOWN", "intent": "", "ap_equipped": True}
        if sid is not None:
            body["sid"] = sid
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


def fly(aw, frm, t0, n, x0, y0, trk=90.0, gs=90.0, seq0=1, auth="ok", alt=ALT, turn_dps=0.0, vs=0.0, sid=None):
    """n STATEs at 1 Hz along a (possibly turning) track; returns (next t, next seq, next x, next y)."""
    x, y, h = x0, y0, trk
    for k in range(n):
        t = t0 + k
        aw.on_message(env(frm, seq0 + k, t, x, y, alt=alt + vs * k / 60.0, gs=gs, trk=h % 360, vs=vs, auth=auth,
                          sid=sid), t)
        r = math.radians(h)
        x, y = x + math.sin(r) * gs * KT, y + math.cos(r) * gs * KT
        h += turn_dps
    return t0 + n, seq0 + n, x, y


def mk(signed=True, witness=None):
    aw = AirWitness("N101", to_enu, signed_radio=signed)
    if witness is not None:
        aw.peer_evidence = lambda tid: (1.0, witness.get(tid, []))
    own(aw, 0.0)
    return aw


def settle(aw, tid, t):
    aw.assess(tid, t)
    return aw.assess(tid, t + 3.0)               # past the 2 s upgrade hysteresis


def ok(r, src):
    return r.checks.get(src, "UNKNOWN").split(" ")[0]


def own_frame(t, x, y, trk=86.0, gs=90.0):
    lat, lon = to_latlon(x, y)
    return {"type": "OWNSHIP", "ac_id": "N101", "t": t, "lat": lat, "lon": lon, "alt_msl_ft": 2500.0,
            "alt_press_ft": 2500.0, "agl_ft": 1022.0, "gs_kt": gs, "track_deg": trk, "hdg_deg": trk, "bank_deg": 0.0,
            "vs_fpm": 0.0, "ias_kt": gs, "ap_equipped": True, "stick_active": False, "flaps": 0}


def ghost_run(steps=450, refute=True, ghost_commit=False, verified_peer=False):
    """Our aircraft on the 25L downwind, an unsigned ghost head-on 2.5 km ahead closing (optionally refuted by a peer
    witness so it is SUSPICIOUS), optionally a real far-away ARC peer."""
    pats = build_patterns()
    node = Node("N101", patterns=pats)
    node.aw.signed_radio = True
    if refute:
        node.aw.peer_evidence = lambda tid: (0.0, ["refuted_by:N204"] if tid == "GHOST7" else ["corroborated:2"])
    pl = pats["25L"].place("DOWNWIND", 20.0, 90.0)
    out = {"cmds": [], "levels": set(), "commits": [], "frames": [], "node": node, "adv": []}
    for k in range(steps):
        t = 1.0e9 + k * 0.1
        o = own_frame(t, pl["x_m"] + 46.3 * k * 0.1, pl["y_m"])
        if k % 10 == 0:
            gx = pl["x_m"] + 2500.0 - 46.3 * k * 0.1
            node.on_radio(env("GHOST7", k + 1, t, gx, pl["y_m"], trk=266.0, auth="unsigned", rx=t))
            if ghost_commit:
                node.on_radio(env("GHOST7", k + 2, t, gx, pl["y_m"], msg="MANEUVER_COMMIT", auth="unsigned", rx=t,
                                  body={"target": "N101", "sense": "L", "bank_deg": 30, "vs_fpm": 0, "start_t": t,
                                        "hold_s": 10}))
            if verified_peer:
                node.on_radio(env("N204", k + 1, t, pl["x_m"] - 9000 + 46.3 * k * 0.1, pl["y_m"] + 6000, trk=90.0,
                                  auth="ok", rx=t))
        frames, radios = node.tick(o)
        for f in frames:
            out["frames"].append(f)
            if f["type"] == "COMMAND":
                out["cmds"].append(f)
            if f["type"] == "ADVISORY":
                out["levels"].add(f["level"])
                out["adv"].append(f)
        out["commits"] += [b for m, b in radios if m == "MANEUVER_COMMIT"]
    return out


# 1
def test_01_valid_signed_peer_becomes_verified():
    aw = mk()
    t, s, _, _ = fly(aw, "N204", 1.0, 5, 0, 2000, trk=270)
    r = settle(aw, "N204", t)
    assert r.state == VERIFIED and r.may_coordinate and r.cap == "TAKEOVER", r.explain()
    assert r.probs["real"] > 0.85


# 2
def test_02_invalid_signature_cannot_become_verified():
    aw = mk(witness={"X1": ["corroborated:3"]})
    t, s, _, _ = fly(aw, "X1", 1.0, 8, 0, 2000, trk=270, auth="unknown_key")
    r = settle(aw, "X1", t)
    assert r.state != VERIFIED and ok(r, "signature") == "CONFLICT" and not r.may_coordinate
    assert r.cap is not None                                     # still tracked, never deleted


# 3
def test_03_replayed_packet_rejected():
    aw = mk()
    t, s, x, y = fly(aw, "N204", 1.0, 40, 0, 2000, trk=270)
    old = env("N204", 2, t - 0.5, 0, 2000, trk=270, rx=t)          # seq 2 recorded at t=2, re-stamped, replayed
    assert aw.on_message(old, t) is False
    assert "replay" in aw.tracks["N204"].flags


# 4
def test_04_duplicate_packet_ignored():
    aw = mk()
    t, s, x, y = fly(aw, "N204", 1.0, 3, 0, 2000)
    n = len(aw.tracks["N204"].states)
    assert aw.on_message(env("N204", s - 1, t - 1, x - 46.3, y), t) is False
    assert len(aw.tracks["N204"].states) == n


def test_04b_expired_session_and_new_session():
    aw = mk()
    t, s, x, y = fly(aw, "N204", 1.0, 5, 0, 2000, sid="aaaa")
    t, s, x, y = fly(aw, "N204", t, 3, x, y, sid="bbbb", seq0=1)            # restart: new session, seq starts over
    assert aw.tracks["N204"].sid == "bbbb"
    assert aw.on_message(env("N204", 50, t, x, y, sid="aaaa"), t) is False   # packet from the old session
    assert "expired_session" in aw.tracks["N204"].flags


def test_04c_stale_timestamp_dropped():
    aw = mk()
    t, s, x, y = fly(aw, "N204", 1.0, 3, 0, 2000)
    assert aw.on_message(env("N204", s, t - 5.0, x, y, rx=t), t) is False
    assert ok(aw.assess("N204", t), "packet") == "CONFLICT"


# 5
def test_05_legacy_adsb_visible_but_unverified():
    aw = mk()
    t, s, _, _ = fly(aw, "ADSB1", 1.0, 8, 0, 3000, trk=270, auth="unsigned")
    r = settle(aw, "ADSB1", t)
    assert r.state == UNVERIFIED and r.cap is not None and not r.may_coordinate and r.clof_layer == "shadow"


# 6
def test_06_impossible_motion_ghost_suspicious():
    aw = mk()
    t, s, x, y = fly(aw, "X2", 1.0, 3, 0, 2500)
    aw.on_message(env("X2", s, t, x + 3000, y), t)
    r = aw.assess("X2", t)
    assert "position_jump" in r.checks["motion"] and r.state in (SUSPICIOUS, QUARANTINED)
    aw = mk()
    t, s, x, y = fly(aw, "X3", 1.0, 3, 0, 2500, trk=90)
    aw.on_message(env("X3", s, t, x, y, trk=270), t)
    assert "turn_rate" in aw.assess("X3", t).checks["motion"]


# 7
def test_07_smooth_legitimate_motion_not_quarantined():
    aw = mk()
    t, _, _, _ = fly(aw, "N204", 1.0, 12, 0, 2500, trk=270, turn_dps=9.0, vs=1500, gs=95)   # steep turn + climb
    r = aw.assess("N204", t)
    assert ok(r, "motion") == "PASS" and r.state != QUARANTINED
    aw = mk()                                                                                # packet loss: 4 s gap
    t, s, x, y = fly(aw, "N204", 1.0, 3, 0, 2500, trk=270)
    t2, _, _, _ = fly(aw, "N204", t + 4, 5, x - 46.3 * 4, y, trk=270, seq0=s + 6)
    r = settle(aw, "N204", t2)
    assert r.state == VERIFIED, r.explain()


# 8
def test_08_missing_tcas_is_not_fake():
    aw = mk()
    t, _, _, _ = fly(aw, "ADSB1", 1.0, 8, 0, 3000, trk=270, auth="unsigned")
    r = settle(aw, "ADSB1", t)
    assert ok(r, "tcas") == "UNKNOWN" and r.state not in (SUSPICIOUS, QUARANTINED)


def _tcas_for(aw, tid, t, err_m=0.0):
    c = aw._claimed_at(aw.tracks[tid], t)
    dx, dy, dz = c[0] - aw.own_pos[0], c[1] - aw.own_pos[1], c[2] - aw.own_pos[2]
    rng = math.sqrt(dx * dx + dy * dy + dz * dz)
    return TCASMeasurement(t=t, range_m=rng + err_m, bearing_deg=math.degrees(math.atan2(dx, dy)) % 360,
                           relative_altitude_m=dz, target_id=tid)


# 9
def test_09_tcas_agreement_increases_confidence():
    aw = mk()
    t, _, _, _ = fly(aw, "ADSB1", 1.0, 8, 0, 3000, trk=270, auth="unsigned")
    before = aw.assess("ADSB1", t).probs["real"]
    for k in range(3):
        aw.add_tcas(_tcas_for(aw, "ADSB1", t - 1 + k * 0.5))
    r = aw.assess("ADSB1", t + 0.1)
    assert ok(r, "tcas") == "PASS" and r.probs["real"] > before and r.physical_support


# 10
def test_10_tcas_contradiction_decreases_confidence():
    aw = mk()
    t, _, _, _ = fly(aw, "GHOST7", 1.0, 8, 0, 14800, trk=180, auth="unsigned")      # claims ~8 NM
    before = aw.assess("GHOST7", t).probs["real"]
    for k in range(3):                                                             # TCAS says ~2 NM
        aw.add_tcas(TCASMeasurement(t=t - 1 + k * 0.5, range_m=3700.0, bearing_deg=0.0, relative_altitude_m=0.0,
                                    target_id="GHOST7"))
    r = aw.assess("GHOST7", t + 0.1)
    assert ok(r, "tcas") == "CONFLICT" and r.probs["real"] < before and r.state in (SUSPICIOUS, QUARANTINED)


# 11
def test_11_mode_s_support_contributes():
    aw = mk()
    t, _, _, _ = fly(aw, "ADSB1", 1.0, 8, 0, 3000, trk=270, auth="unsigned")
    before = aw.assess("ADSB1", t).probs["real"]
    for k in range(3):
        aw.add_mode_s(ModeSObservation(t=t - k, target_id="ADSB1", rssi_dbm=-70.0))
    aw.add_interrogation_reply(InterrogationReplyEvidence(t - 1, t - 1 + 0.000128, "ADSB1", 0.9))
    r = aw.assess("ADSB1", t)
    assert ok(r, "mode_s") == "PASS" and ok(r, "interrogation") == "PASS" and r.probs["real"] > before


# 12
def test_12_missing_mode_s_alone_not_fake():
    aw = mk()
    t, _, _, _ = fly(aw, "ADSB1", 1.0, 8, 0, 3000, trk=270, auth="unsigned")
    r = settle(aw, "ADSB1", t)
    assert ok(r, "mode_s") == "UNKNOWN" and ok(r, "interrogation") == "UNKNOWN" and r.state == UNVERIFIED


def _doppler_track(tx_vel_x):
    """Target claims to fly head-on toward us from 3 km east at 90 kt; its transmitter really moves at tx_vel_x."""
    aw = mk()
    t, _, _, _ = fly(aw, "T1", 1.0, 3, 3000, 0, trk=270)
    for k in range(8):
        tt = t + k
        aw.on_message(env("T1", 10 + k, tt, 3000 - 46.3 * (3 + k), 0, trk=270), tt)
        tx = (3000 - 46.3 * (3 + k), 0.0, ALT * FT)
        aw.ingest_rf(measure("N101", aw.own_pos, "T1", tx, tt, kinds=("doppler",), rng=random.Random(k),
                             tx_vel=(tx_vel_x, 0.0, 0.0)))
    return aw, t + 8


# 13
def test_13_doppler_agreement_increases_confidence():
    aw, t = _doppler_track(-46.3)
    r = aw.assess("T1", t)
    assert ok(r, "doppler_trend") == "PASS"
    base = mk()
    tb, _, _, _ = fly(base, "T1", 1.0, 11, 3000, 0, trk=270)
    assert r.probs["real"] >= base.assess("T1", tb).probs["real"]


# 14
def test_14_doppler_contradiction_decreases_confidence():
    aw, t = _doppler_track(+46.3)                         # claims closing, carrier says opening
    r = aw.assess("T1", t)
    assert ok(r, "doppler_trend") == "CONFLICT"
    good, tg = _doppler_track(-46.3)
    assert r.probs["real"] < good.assess("T1", tg).probs["real"]


# 15
def test_15_rssi_alone_cannot_decide():
    aw = mk()
    v = 150 * KT
    t, _, _, _ = fly(aw, "N204", 1.0, 3, 2500, 0, trk=270, gs=150)
    for k in range(12):                                  # claims closing, RSSI gets weaker (contradiction)
        tt = t + k
        aw.on_message(env("N204", 10 + k, tt, 2500 - v * (3 + k), 0, trk=270, gs=150), tt)
        aw.ingest_rf(RFMeasurement("N101", "N204", tt, aw.own_pos, rssi_dbm=-60.0 - 2.0 * k))
    r = settle(aw, "N204", t + 12)
    assert ok(r, "rssi_trend") == "CONFLICT" and r.state not in (SUSPICIOUS, QUARANTINED) and not r.gates


# 16
def test_16_independent_witnesses_increase_confidence():
    aw = mk(witness={"N204": ["corroborated:3"]})
    t, _, _, _ = fly(aw, "N204", 1.0, 5, 0, 2000, trk=270)
    with_w = aw.assess("N204", t).probs["real"]
    aw2 = mk()
    fly(aw2, "N204", 1.0, 5, 0, 2000, trk=270)
    assert ok(aw.assess("N204", t), "witnesses") == "PASS" and with_w > aw2.assess("N204", t).probs["real"]


def test_16b_passive_digest_multi_observer():
    """Verified peers' piggybacked WITNESS_DIGESTs: a claim whose radial-motion sign disagrees at every observer."""
    aw = mk(signed=False)
    peers = {"N204": (-6000.0, 0.0), "N311": (0.0, -6000.0)}
    for p, (px, py) in peers.items():
        fly(aw, p, 1.0, 5, px, py, trk=90)
        settle(aw, p, 5.0)
    t, s, _, _ = fly(aw, "GHOST7", 1.0, 5, 3000, 3000, trk=225, gs=120)    # claims flying toward both peers
    for p, (px, py) in peers.items():                    # both peers observe it OPENING (doppler trend -1)
        aw.on_message(env(p, 50, t, px + 46.3 * 5, py, msg="HEARTBEAT", body={"alive": True, "w": {"GHOST7": [0.4, 0, -1, 0]}}), t)
    r = aw.assess("GHOST7", t)
    assert ok(r, "multi_observer") == "CONFLICT" and r.state in (SUSPICIOUS, QUARANTINED)
    for p, (px, py) in peers.items():                    # and when they agree it is PASS
        aw.on_message(env(p, 60, t + 1, px + 46.3 * 6, py, msg="HEARTBEAT", body={"alive": True, "w": {"GHOST7": [0.4, 0, 1, 0]}}), t + 1)
    assert ok(aw.assess("GHOST7", t + 1), "multi_observer") == "PASS"


# 17
def test_17_no_witnesses_is_not_spoofing():
    aw = mk()
    t, _, _, _ = fly(aw, "N204", 1.0, 5, 0, 2000, trk=270)
    r = settle(aw, "N204", t)
    assert ok(r, "witnesses") == "UNKNOWN" and r.state == VERIFIED


# 18
def test_18_duplicate_identity_suspicious():
    aw = mk()
    for k in range(8):                                   # two transmitters share one id, 2 km apart
        x = 0.0 if k % 2 == 0 else 2000.0
        aw.on_message(env("N399", k + 1, 1.0 + k * 0.5, x + 23 * k, 3000), 1.0 + k * 0.5)
    r = aw.assess("N399", 5.0)
    assert ok(r, "identity") == "CONFLICT" and r.state in (SUSPICIOUS, QUARANTINED)


# 19, 20, 22
def test_19_20_22_suspicious_ghost_warns_but_never_negotiates_or_takes_over():
    out = ghost_run()
    node = out["node"]
    assert node.trust.aw_state("GHOST7") in (SUSPICIOUS, QUARANTINED)
    assert "TRAFFIC" in out["levels"]                                     # 19: still a warning when threatening
    assert not out["commits"] and not node.trust.may_negotiate("GHOST7")  # 20: no negotiation
    assert not out["cmds"] and "TAKEOVER" not in out["levels"]            # 22: no takeover from its data


# 21
def test_21_suspicious_track_cannot_enter_4d_contract():
    out = ghost_run(steps=200, ghost_commit=True)
    node = out["node"]
    pair = node.negotiator.pairs.get("GHOST7")
    assert pair is None or pair.peer_commit is None
    assert node.trust.result("GHOST7").authority["4D_contract"] == "BLOCKED"


# 23, 24
def test_23_24_verified_and_shadow_clof():
    out = ghost_run(steps=120, verified_peer=True)
    clof = [f["clof"] for f in out["frames"] if f["type"] == "TRUST" and "clof" in f][-1]
    assert clof["GHOST7"] == "shadow"                                    # 23
    assert clof["N204"] == "verified"                                    # 24 (sim transport: signatures n/a)


# 25
def test_25_trust_widens_threat_tube():
    assert SIGMA_SCALE[VERIFIED] < SIGMA_SCALE[UNVERIFIED] < SIGMA_SCALE[SUSPICIOUS]
    node = ghost_run(steps=60, verified_peer=True)["node"]
    g, v = node._peer_pred(node.peers["GHOST7"]), node._peer_pred(node.peers["N204"])
    assert g.pts[5, 4] > v.pts[5, 4] * 2.0


# 26
def test_26_trust_robust_escape_prefers_safe_under_both():
    E = lambda n: SimpleNamespace(cand=SimpleNamespace(name=n))
    h1 = SimpleNamespace(ranked=[E("L30"), E("CLIMB"), E("R30"), E("DESCEND")])     # target real: all four safe-ish
    h2 = SimpleNamespace(ranked=[E("R30"), E("DESCEND"), E("HOLD")])                  # target spoofed: L / CLIMB hit
    r = trust_robust(h1, h2)                                                          # real traffic or terrain
    assert r["chosen"] == "R30" and r["safe_if_spoofed"] and [e.cand.name for e in h1.ranked][:2] == ["R30", "DESCEND"]
    h1 = SimpleNamespace(ranked=[E("L30")])                                           # nothing unsafe is promoted
    assert trust_robust(h1, SimpleNamespace(ranked=[E("R30")]))["chosen"] == "L30"
    out = ghost_run(refute=False)                                                     # UNVERIFIED ghost: RESOLVE says why
    rs = [a["reason"] for a in out["adv"] if a["level"] == "RESOLVE"]
    assert rs and "trust_robust" in rs[-1] and rs[-1]["clof"] == "shadow"


# 27
def test_27_monte_carlo_does_not_regress():
    from harness.montecarlo import run_one
    rows = [run_one((k, s, 0.1, 0.3, ("none", "arc"))) for k, s in (("head_on_crosswind", 3), ("overtake_downwind", 5),
                                                                          ("spaced_pattern", 2))]
    for r in rows:
        if "arc" in r and r.get("label") == "conflict":
            assert r["arc"]["margin"] >= r["none"]["margin"]
        if r.get("label") == "benign":
            assert not r["arc"]["maneuvered"]


# ---- regression / passive-design checks
def test_no_challenge_path_left():
    import inspect
    import node.airwitness as m
    src = inspect.getsource(m)
    assert not hasattr(AirWitness, "tick") and "nonce" not in src and "chal_" not in src
    out = ghost_run(steps=40, verified_peer=True)
    hb = [b for _, _, f in out["node"].sent if f.get("msg") == "HEARTBEAT" for b in [f["body"]]]
    assert hb and all(set(b) <= {"alive", "w"} for b in hb)


def test_unavailable_sources_are_unknown_not_fail():
    aw = mk()
    t, _, _, _ = fly(aw, "N204", 1.0, 5, 0, 2000, trk=270)
    r = aw.assess("N204", t)
    for src in ("tcas", "mode_s", "interrogation", "rf_location", "doppler_trend", "rssi_trend", "witnesses",
                "multi_observer", "negotiation"):
        assert ok(r, src) == "UNKNOWN", (src, r.checks[src])


def test_explain_panel():
    aw = mk(witness={"GHOST7": ["refuted_by:N204"]})
    t, _, _, _ = fly(aw, "GHOST7", 1.0, 5, 0, 2500, auth="unsigned")
    text = aw.assess("GHOST7", t).explain()
    for s in ("TARGET: GHOST7", "P(real):", "P(spoof):", "P(faulty):", "EVIDENCE", "AUTHORITY", "negotiation:",
              "4D_contract:", "auto_command_from_target:"):
        assert s in text


def test_negotiation_answer_is_positive_only():
    aw = mk()
    t, s, x, y = fly(aw, "N204", 1.0, 4, 0, 2000, trk=270)
    aw.note_sent("MANEUVER_COMMIT", {"target": "N204", "sense": "R"}, t)
    aw.on_message(env("N204", s, t + 0.4, x, y, msg="MANEUVER_COMMIT", rx=t + 0.4,
                      body={"target": "N101", "sense": "R", "bank_deg": 20, "vs_fpm": 0, "start_t": t, "hold_s": 10}), t + 0.4)
    assert ok(aw.assess("N204", t + 1), "negotiation") == "PASS"
    aw2 = mk()
    t, s, x, y = fly(aw2, "N205", 1.0, 4, 0, 2000, trk=270)
    aw2.note_sent("MANEUVER_COMMIT", {"target": "N205", "sense": "R"}, t)
    t2, _, _, _ = fly(aw2, "N205", t, 8, x, y, trk=270, seq0=s)              # never answers: no penalty at all
    r = settle(aw2, "N205", t2)
    assert ok(r, "negotiation") == "UNKNOWN" and r.state == VERIFIED


def test_intent_opposite_widens_tube_only():
    aw = mk()
    t, s, x, y = fly(aw, "N204", 1.0, 3, 0, 2000, trk=270)
    settle(aw, "N204", t)
    aw.on_message(env("N204", s + 1, t + 0.6, x, y, msg="MANEUVER_COMMIT", rx=t + 0.6,
                      body={"target": "N101", "sense": "R", "bank_deg": 30, "vs_fpm": 0, "start_t": t, "hold_s": 10}), t + 0.6)
    t2, _, _, _ = fly(aw, "N204", t + 1, 14, x, y, trk=270, turn_dps=-4.0, seq0=s + 2)   # flies LEFT instead
    r = settle(aw, "N204", t2)
    assert ok(r, "intent") == "CONFLICT" and r.sigma_extra > 1.0 and r.state == VERIFIED


def test_pop_in_and_baro_geo():
    aw = mk()
    t, _, _, _ = fly(aw, "GHOST7", 1.0, 4, 0, 600, auth="unsigned")
    assert ok(aw.assess("GHOST7", t), "pop_in") == "CONFLICT"
    aw = mk()
    own(aw, 0.5, alt=2500, press=2480)
    for k in range(4):
        e = env("X9", k + 1, 1.0 + k, 0, 3000 - 46 * k, trk=180)
        e["body"]["alt_geo_ft"] = ALT + 900
        aw.on_message(e, 1.0 + k)
    r = aw.assess("X9", 5.0)
    assert ok(r, "baro_geo") == "CONFLICT" and r.state in (SUSPICIOUS, QUARANTINED)


def test_sybil_one_transmitter_many_ids():
    aw = mk()
    for i in range(5):
        tid = f"GHOST{i}"
        fly(aw, tid, 1.0, 4, 300.0 * i, 2500, auth="unsigned", seq0=1)
        for k in range(4):
            aw.ingest_rf(measure("N101", (0, 0, ALT * FT), tid, (0, -2500, 0), 2.0 + k, kinds=("rssi",),
                                 rng=random.Random(10 * i + k), source_id="TX-1"))
    for i in range(5):
        r = aw.assess(f"GHOST{i}", 5.0)
        assert ok(r, "sybil") == "CONFLICT" and r.state != VERIFIED


def test_rf_location_conflict():
    aw = mk()
    t, s, _, _ = fly(aw, "N204", 1.0, 6, 0, 2500, trk=270)
    for k in range(5):
        aw.ingest_rf(measure("N101", (0, 0, ALT * FT), "N204", (0, -3000, ALT * FT), t - 5 + k, kinds=("range",),
                             rng=random.Random(k)))
    r = aw.assess("N204", t)
    assert ok(r, "rf_location") == "CONFLICT" and r.state in (SUSPICIOUS, QUARANTINED)


def test_gnss_jump_inconsistent_with_motion():
    aw = mk()
    for k in range(5):
        own(aw, 1.0 + k * 0.1, x=46.3 * 0.1 * k)
    own(aw, 1.5, x=46.3 * 0.5 + 500.0)
    assert aw.pnt.state == "DEGRADED" and aw.pnt.extra_sigma_m > 500


def test_quarantined_with_independent_sensor_keeps_hazard():
    r = TrustResult("X", 0.05, QUARANTINED, physical_support=True)
    assert r.cap == "TRAFFIC" and r.clof_layer == "shadow"
    assert TrustResult("X", 0.05, QUARANTINED).cap is None


def test_latency_is_logged():
    aw = mk()
    t, _, _, _ = fly(aw, "N204", 1.0, 5, 0, 2000, trk=270)
    aw.assess("N204", t)
    assert aw.stats["guard_us"] and aw.stats["assess_us"]


def test_node_trust_frame_carries_a_spoof_alert_once():
    pats = build_patterns()
    node = Node("N101", patterns=pats)
    node.aw.signed_radio = True
    pl = pats["25L"].place("DOWNWIND", 20.0, 90.0)
    alerts = []
    for k in range(80):
        t = 1.0e9 + k * 0.1
        if k % 10 == 0:                                   # 400 kt ghost with jumps
            node.on_radio(env("GHOST7", k + 1, t, pl["x_m"] + 2000 + (1500 if k % 20 else 0), pl["y_m"], gs=400,
                              trk=266.0, auth="unsigned", rx=t))
        frames, _ = node.tick(own_frame(t, pl["x_m"] + 4.6 * k, pl["y_m"]))
        alerts += [a for f in frames if f["type"] == "TRUST" for a in f.get("alerts", [])]
    ghost = [a for a in alerts if a["id"] == "GHOST7"]
    assert ghost and ghost[-1]["text"].startswith(("SPOOFED", "UNCONFIRMED")) and len(ghost) <= 2
