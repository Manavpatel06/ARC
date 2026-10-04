"""
harness/redflock_spoof.py — RED ARC attacks AirWitness-Hybrid (passive) on the real node (Lane B).

Our aircraft N101 (autopilot-equipped) flies the 25L downwind.  N204 is a legitimate ARC aircraft (signed, its own
session id), well clear to the north.  The attacker's ground transmitter sits 3 km behind our start point.  Each
attack tries to put an aircraft head-on, 2.5 km ahead and closing, into our picture.  Nothing in ARC ever asks a
target to prove anything; every verdict below comes from passive evidence.

   1 invalid_signature   bad Ed25519 signature on a plausible track
   2 replay              N204's recorded packets re-sent later
   3 duplicate           N204's packets re-sent byte-for-byte straight away
   4 stale_session       packets in N204's old (expired) session id
   5 teleport            400 kt with position jumps
   6 smooth              physically perfect head-on track, unsigned, ARC band (RSSI)
   7 smooth_intent       as 6 plus INTENT and MANEUVER_COMMITs trying to enter a contract with us
   8 duplicate_identity  N204's identity with a stolen key, sequence numbers far ahead (hijack)
   9 sybil               three ghosts from the one ground transmitter
  10 tcas_conflict       a real legacy aircraft's ADS-B position spoofed; on-board TCAS sees where it really is
  11 adsb_only           plausible ADS-B-only ghost, no RF, no TCAS, no witnesses (the honest hard case)
  12 rf_trend            claims to close on us; the carrier Doppler says the transmitter is opening
  13 witness_disagree    a verified peer's passive digest and witness report contradict the claim
  14 intermittent        appears 4 s, vanishes 6 s, reappears elsewhere in a new session, repeatedly

Security objective (what RED ARC is rewarded for):  spoof VERIFIED, spoof gets negotiation authority, spoof causes
an automatic maneuver, spoof deforms the escape choice into one that is unsafe if the target is fake.
Pass = none of those four, and the real N204 keeps VERIFIED (except attack 8, where N204's identity is the victim).

    python harness/redflock_spoof.py
"""
from __future__ import annotations

import math
import os
import random
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from harness import surveillance_sim
from harness.rf_sim import measure
from node.airwitness import QUARANTINED, SUSPICIOUS, UNVERIFIED, VERIFIED
from node.geometry import FT, build_patterns, hvec, to_latlon
from node.node import Node

T0 = 1.0e9
ALT = 2500.0
V = 46.3                                       # 90 kt in m/s

RANK = {QUARANTINED: 0, SUSPICIOUS: 1, UNVERIFIED: 2, VERIFIED: 3}
ATTACKS = ["invalid_signature", "replay", "duplicate", "stale_session", "teleport", "smooth", "smooth_intent",
           "duplicate_identity", "sybil", "tcas_conflict", "adsb_only", "rf_trend", "witness_disagree", "intermittent"]


def _env(msg, frm, seq, t, body, auth="ok"):
    e = {"msg": msg, "from": frm, "seq": seq, "t": t, "sig": "", "_rx_t": t, "body": body}
    if auth is not None:
        e["_auth"] = auth
    return e


def _state(frm, seq, t, x, y, trk, gs=90.0, auth="ok", sid=None):
    lat, lon = to_latlon(x, y)
    b = {"lat": lat, "lon": lon, "alt_press_ft": ALT, "gs_kt": gs, "track_deg": trk, "vs_fpm": 0.0,
         "leg": "UNKNOWN", "intent": "", "ap_equipped": True}
    if sid:
        b["sid"] = sid
    return _env("STATE", frm, seq, t, b, auth)


def _radial_sign(obs, obs_v, tx, tx_v):
    d = [tx[i] - obs[i] for i in range(3)]
    r = math.sqrt(sum(x * x for x in d)) or 1.0
    rdot = sum(d[i] * (tx_v[i] - obs_v[i]) for i in range(3)) / r
    return 0 if abs(rdot) < 3.0 else (1 if rdot < 0 else -1)


def run_attack(kind: str, seconds: float = 40.0) -> dict:
    pats = build_patterns()
    node = Node("N101", patterns=pats, record=False)
    node.aw.signed_radio = True
    witness = {"N204": ["corroborated:2"]}
    if kind == "witness_disagree":
        witness["GHOST7"] = ["refuted_by:N204"]
    node.aw.peer_evidence = lambda tid: (1.0, witness.get(tid, []))
    pl = pats["25L"].place("DOWNWIND", 20.0, 90.0)
    hdg = pl["hdg_deg"]
    hx, hy = hvec(hdg)
    site = (pl["x_m"] - hx * 3000.0, pl["y_m"] - hy * 3000.0, 1478.0 * FT)    # attacker antenna, on the ground
    rng = random.Random(11)
    victim = kind in ("replay", "duplicate", "stale_session", "duplicate_identity")
    target = "N204" if victim else "GHOST7"
    ghosts = ["GHOST7", "GHOST8", "GHOST9"] if kind == "sybil" else ([] if victim else ["GHOST7"])
    if kind == "tcas_conflict":
        ghosts = ["N777"]
        target = "N777"
    seq = {g: 0 for g in ghosts + ["N204"]}
    recorded = []
    out = {"verified": False, "negotiation": 0, "takeovers": 0, "deformed": 0, "levels": set(), "t_flag": None,
           "t_quar": None, "t_alert": None, "dropped": 0, "result": None, "n204": None, "worst": VERIFIED}
    attack_t0 = T0 + (15.0 if victim else 0.0)
    for k in range(int(seconds * 10)):
        t = T0 + k * 0.1
        ox, oy = pl["x_m"] + hx * V * k * 0.1, pl["y_m"] + hy * V * k * 0.1
        own_pos, own_v = (ox, oy, ALT * FT), (hx * V, hy * V, 0.0)
        lat, lon = to_latlon(ox, oy)
        own = {"type": "OWNSHIP", "ac_id": "N101", "t": t, "lat": lat, "lon": lon, "alt_msl_ft": ALT, "alt_press_ft": ALT,
               "agl_ft": 1022.0, "gs_kt": 90.0, "track_deg": hdg, "hdg_deg": hdg, "bank_deg": 0.0,
               "vs_fpm": 0.0, "ias_kt": 90.0, "ap_equipped": True, "stick_active": False, "flaps": 0}
        if k % 10 == 0:
            # ---- the legitimate N204, clear to the north-west, flying the opposite way
            n204 = (ox - 3000.0 - hx * V * k * 0.2, oy + 2600.0, ALT * FT)
            seq["N204"] += 1
            e = _state("N204", seq["N204"], t, n204[0], n204[1], (hdg + 180) % 360, sid="n204-s2")
            recorded.append(dict(e, body=dict(e["body"])))
            node.on_radio(e)
            if kind == "witness_disagree" and k >= 60:
                # N204's passive digest: what IT really observes of "GHOST7" (the ground transmitter)
                sgn = _radial_sign(n204, (-hx * V, -hy * V, 0.0), site, (0, 0, 0))
                seq["N204"] += 1
                node.on_radio(_env("HEARTBEAT", "N204", seq["N204"], t, {"alive": True, "w": {"GHOST7": [0.5, 0, sgn or -1, 0]}}))
            # ---- attacks on N204's own traffic
            if t >= attack_t0 and kind == "replay":
                old = dict(recorded[rng.randrange(0, 10)])
                old["t"], old["_rx_t"] = t, t                      # re-stamped to look fresh
                node.on_radio(old)
            if t >= attack_t0 and kind == "duplicate":
                node.on_radio(dict(recorded[-1]))
            if t >= attack_t0 and kind == "stale_session":
                b = dict(recorded[-1]["body"], sid="n204-s1")
                node.on_radio(_env("STATE", "N204", seq["N204"] + 1, t, b))
            if t >= attack_t0 and kind == "duplicate_identity":
                d = 2500.0 - 2 * V * (t - attack_t0)
                b = _state("N204", seq["N204"] + 1000, t, ox + hx * d, oy + hy * d, (hdg + 180) % 360, sid="n204-s2")
                node.on_radio(b)
            # ---- ghosts
            for i, g in enumerate(ghosts):
                d = 2500.0 - 2 * V * k * 0.1
                gx, gy = ox + hx * d + 300.0 * i, oy + hy * d
                gtrk = (hdg + 180) % 360
                seq[g] += 1
                auth, gs, sid = "unsigned", 90.0, None
                if kind == "invalid_signature":
                    auth = "bad_signature"
                if kind == "teleport":
                    gx += rng.choice([-1, 1]) * 1500.0 * (k // 10 % 2)
                    gs = 400.0
                if kind == "intermittent":
                    cyc = int(k * 0.1) % 10
                    if cyc >= 4:
                        continue
                    sid = f"g{int(k * 0.1) // 10}"
                    gx += 600.0 * (int(k * 0.1) // 10)
                if kind == "tcas_conflict":
                    auth = None                                      # legacy ADS-B: no ARC auth field at all
                e = _state(g, seq[g], t, gx, gy, gtrk, gs=gs, auth=auth if auth else "unsigned", sid=sid)
                node.on_radio(e)
                if kind == "smooth_intent":
                    seq[g] += 1
                    node.on_radio(_env("MANEUVER_COMMIT", g, seq[g], t, {"target": "N101", "sense": "L", "bank_deg": 30,
                                                                         "vs_fpm": 0, "start_t": t, "hold_s": 10}, "unsigned"))
                    seq[g] += 1
                    node.on_radio(_env("INTENT", g, seq[g], t, {"leg": "UNKNOWN", "intent": "BASE_IN_10S", "valid_for_s": 20}, "unsigned"))
                # ---- what the physics really shows
                if kind in ("smooth", "smooth_intent", "invalid_signature", "teleport", "sybil", "intermittent",
                            "witness_disagree"):
                    # the ARC-band channel gives RSSI and carrier Doppler for every received packet
                    node.aw.ingest_rf(measure("N101", own_pos, g, site, t, kinds=("rssi", "doppler"), rng=rng,
                                              obs_vel=own_v, source_id="GROUND-TX" if kind == "sybil" else None))
                if kind == "rf_trend":
                    node.aw.ingest_rf(measure("N101", own_pos, g, site, t, kinds=("doppler",), rng=rng, obs_vel=own_v))
                if kind == "tcas_conflict":                          # the real N777 is 9 km north; TCAS sees it there
                    real = (ox + 1000.0, oy + 9000.0, ALT * FT)
                    m = surveillance_sim.tcas(own_pos, real, t, rng, target_id="N777")
                    if m:
                        node.on_surveillance(m)
                    node.on_surveillance(surveillance_sim.mode_s("N777", t))
        frames, radios = node.tick(own)
        for msg, body in radios:
            if msg == "MANEUVER_COMMIT" and body.get("target") in ghosts + ([target] if victim else []):
                out["negotiation"] += 1
        pair = node.negotiator.pairs.get(target)
        if pair is not None and pair.peer_commit is not None and target != "N204":
            out["negotiation"] += 1
        r = node.trust.result(target) if target in node.peers else None
        if r is not None and t >= attack_t0:
            if r.state == VERIFIED and target != "N204":
                out["verified"] = True
            if kind == "duplicate_identity" and t - attack_t0 > 6.0 and r.state == VERIFIED:
                out["verified"] = True                               # a hijacked identity must not stay VERIFIED
            if RANK[r.state] < RANK[out["worst"]]:
                out["worst"] = r.state
            if r.state in (SUSPICIOUS, QUARANTINED) and out["t_flag"] is None:
                out["t_flag"] = t - attack_t0
            if r.state == QUARANTINED and out["t_quar"] is None:
                out["t_quar"] = t - attack_t0
        for f in frames:
            if f["type"] == "TRUST" and any(a["id"] == target for a in f.get("alerts", [])) and out["t_alert"] is None:
                out["t_alert"] = t - attack_t0
            if f["type"] == "COMMAND" and f["mode"] == "TAKEOVER":
                out["takeovers"] += 1
            if f["type"] == "ADVISORY" and f.get("target_id") in ghosts + [target]:
                out["levels"].add(f["level"])
                tr = f["reason"].get("trust_robust")
                if tr and f["reason"].get("chosen") not in (None, "HOLD") and not tr["safe_if_spoofed"]:
                    out["deformed"] += 1
    now = T0 + seconds
    if target in node.aw.tracks:
        out["result"] = node.aw.assess(target, now)
    out["n204"] = node.aw.assess("N204", now)
    out["latency"] = node.latency_summary()
    if victim:
        out["dropped"] = sum(1 for f in ("replay", "duplicate", "expired_session", "seq_jump")
                             if f in node.aw.tracks["N204"].flags)
    return out


def victim_kind(kind: str) -> bool:
    return kind in ("replay", "duplicate", "stale_session")


def main() -> None:
    fmt = lambda v: "-" if v is None else f"{v:.1f}s"
    print(f"{'attack':20s} {'worst state':13s} {'P(spoof)':>8s} {'flag':>6s} {'quar':>6s} {'alert':>6s} "
          f"{'VERIF':>5s} {'negot':>5s} {'auto':>4s} {'deform':>6s}  {'N204':10s} result")
    ok_all, n_ok, lat = True, 0, []
    for kind in ATTACKS:
        r = run_attack(kind)
        res, n204 = r["result"], r["n204"]
        secure = not r["verified"] and r["negotiation"] == 0 and r["takeovers"] == 0 and r["deformed"] == 0
        if kind == "duplicate_identity":
            ok = secure and n204.state != VERIFIED
        else:
            ok = secure and n204.state == VERIFIED
        ok_all &= ok
        n_ok += ok
        lat.append(r["latency"])
        st = r["worst"] if not victim_kind(kind) else (res.state if res else "-")
        ps = f"{res.probs['spoof']:.2f}" if res else "-"
        print(f"{kind:20s} {st:13s} {ps:>8s} {fmt(r['t_flag']):>6s} {fmt(r['t_quar']):>6s} {fmt(r['t_alert']):>6s} "
              f"{'YES' if r['verified'] else 'no':>5s} {r['negotiation']:5d} {r['takeovers']:4d} {r['deformed']:6d}  "
              f"{n204.state:10s} {'PASS' if ok else 'FAIL'}")
        if kind == "tcas_conflict" and res:
            print("\n" + res.explain() + "\n")
    g = [x["packet_guard_ms"]["max"] for x in lat if x["packet_guard_ms"]]
    a = [x["trust_update_ms"]["max"] for x in lat if x["trust_update_ms"]]
    c = [x["collision_loop_ms"]["mean"] for x in lat if x["collision_loop_ms"]]
    print(f"latency: packet guard peak {max(g):.3f} ms, trust update peak {max(a):.2f} ms, collision loop mean "
          f"{sum(c) / len(c):.2f} ms")
    print(f"\nRED ARC vs AirWitness-Hybrid: {n_ok}/{len(ATTACKS)} attacks blocked "
          + ("- no spoof gained trusted maneuver authority" if ok_all else "- FAILURES above"))
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
