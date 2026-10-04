"""
verify/tests/test_attacks.py - every simulated attack end to end (world -> sensors -> onboard unit), the
whole-picture guardrails (jamming, own GPS), the ported checks, and ARC's own security (signed,
hash-chained event log; signed VERIFY frames).      python -m pytest -q verify/tests
"""
from __future__ import annotations
import json, sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from eval.sim import run                                         # noqa: E402
from verify import load_config                                   # noqa: E402
from verify.eventlog import EventLog, check_frame, load_key, sign_frame, verify_file   # noqa: E402

CFG = load_config()

def summary(r):
    rows = r["rows"]
    fake = [x for x in rows if x["label"] not in ("real", "gone")]
    real_suspect = [x for x in rows if x["label"] == "real" and x["state"] == "SUSPECT"]
    first = {}
    for x in fake:
        if x["state"] == "SUSPECT":
            first.setdefault(x["icao"], x["t"])
    return fake, real_suspect, first

@pytest.mark.parametrize("attack,within_s,n_fakes", [("ghost", 15, 1), ("swarm", 15, 4), ("masquerade", 15, 1),
                                                    ("replay", 40, 1), ("drift", 100, 1)])
def test_attack_goes_suspect_and_no_real_aircraft_does(attack, within_s, n_fakes):
    r = run(attack, seed=1, secs=150, attack_at=30)
    assert r["started"], r
    fake, real_suspect, first = summary(r)
    assert not real_suspect, sorted({(x["id"], x["reasons"][0]) for x in real_suspect})
    assert len(first) == n_fakes, (first, {x["id"] for x in fake})
    assert max(first.values()) - 30 <= within_s, first

def test_masquerade_splits_the_address_and_keeps_the_real_owner_clean():
    r = run("masquerade", seed=1, secs=90, attack_at=30)
    target = r["attack_info"]["target"]
    last = {}
    for x in r["rows"]:
        last[x["icao"]] = x
    fake = [x for x in last.values() if x["icao"].endswith("~2")]
    real = [x for x in last.values() if x["id"] == target]
    assert fake and fake[0]["state"] == "SUSPECT" and fake[0]["id"] == f"{target} (2)"
    assert real and real[0]["state"] == "VERIFIED", real                  # stolen address, real owner still green

def test_jamming_raises_the_degraded_banner_and_condemns_nobody():
    r = run("jamming", seed=1, secs=90, attack_at=30)
    _, real_suspect, _ = summary(r)
    assert not real_suspect
    on = [t for t, b in r["banners"] if any("TRAFFIC PICTURE DEGRADED" in x for x in b)]
    assert on and on[0] <= 33 and all(t >= 30 for t in on)

def test_own_gps_spoofing_raises_banner_and_widens_instead_of_condemning():
    r = run("gps", seed=1, secs=120, attack_at=30)
    _, real_suspect, _ = summary(r)
    assert not real_suspect, real_suspect[:3]
    on = [t for t, b in r["banners"] if any("OWN POSITION UNCERTAIN" in x for x in b)]
    assert on and on[0] - 30 < 40

def test_quiet_run_has_no_suspects_and_no_banners():
    r = run(None, seed=11, secs=150)
    assert not [x for x in r["rows"] if x["state"] == "SUSPECT"]
    assert not [b for _, b in r["banners"] if b]
    verified = {x["id"] for x in r["rows"] if x["state"] == "VERIFIED"}
    assert len(verified) >= 4

def test_ablation_without_tcas_still_catches_a_ghost_with_mode_s_and_timing():
    from verify.checks import CHECKS
    subset = {k: v for k, v in CHECKS.items() if k != "tcas_consistency"}
    r = run("ghost", seed=1, secs=90, attack_at=30, checks=subset)
    _, real_suspect, first = summary(r)
    assert first and not real_suspect

# ---------------------------------------------------------------- ARC's own security
def test_event_log_is_hash_chained_signed_and_tamper_evident(tmp_path):
    sk = load_key("TEST", key_dir=str(tmp_path / "keys"))
    p = tmp_path / "events.jsonl"
    log = EventLog(str(p), sk)
    for i in range(5):
        log.append("TARGET_STATE", {"icao": "abc123", "to": "SUSPECT" if i % 2 else "VERIFIED"}, float(i))
    ok, why = verify_file(str(p))
    assert ok and "5 entries" in why
    EventLog(str(p), sk).append("BANNER_ON", {"text": "x"}, 9.0)              # continues the same chain
    assert verify_file(str(p))[0]
    lines = p.read_text().splitlines()
    tampered = json.loads(lines[2]); tampered["data"]["to"] = "SUSPECT"        # rewrite history (was VERIFIED)
    p.write_text("\n".join(lines[:2] + [json.dumps(tampered)] + lines[3:]) + "\n")
    ok, why = verify_file(str(p))
    assert not ok and "entry 2" in why
    p.write_text("\n".join(lines[:2] + lines[3:]) + "\n")                      # delete an entry
    assert not verify_file(str(p))[0]

def test_verify_frames_are_signed_and_a_forgery_is_refused(tmp_path):
    sk = load_key("TESTA", key_dir=str(tmp_path))
    f = sign_frame({"type": "VERIFY", "ac_id": "N101", "t": 1.0, "targets": []}, sk)
    ok, _ = check_frame(f, None)
    assert ok
    forged = dict(f, targets=[{"icao": "abc", "state": "VERIFIED"}])
    assert check_frame(forged, f["pub"]) == (False, "bad signature")
    other = sign_frame({"type": "VERIFY", "ac_id": "N101", "t": 2.0, "targets": []}, load_key("TESTB", key_dir=str(tmp_path)))
    assert check_frame(other, f["pub"]) == (False, "key changed")
    assert check_frame({"type": "VERIFY"}, None) == (False, "unsigned")

# ---------------------------------------------------------------- real-data replay (milestone 5)
def _recording(n_frames=40, every=5.0):
    """A LIVE_TRAFFIC recording in data/live_traffic.py's format: three real aircraft around KDVT."""
    import math
    frames = []
    for k in range(n_frames):
        t = 1_790_000_000.0 + k * every
        ac = []
        for j, (lat, lon, trk, alt) in enumerate([(33.70, -112.10, 90, 2600), (33.67, -112.05, 270, 3100), (33.72, -112.06, 180, 2400)]):
            d = 50.0 * k * every                                   # ~97 kt
            ac.append({"id": f"a1b2c{j}", "callsign": f"REAL{j}", "lat": lat + d * math.cos(math.radians(trk)) / 111_320,
                       "lon": lon + d * math.sin(math.radians(trk)) / (111_320 * math.cos(math.radians(lat))),
                       "alt_msl_ft": alt, "gs_kt": 97, "track_deg": trk, "vs_fpm": 0, "on_ground": False, "dist_nm": 2.0})
        frames.append({"type": "LIVE_TRAFFIC", "t": t, "source": "test", "radius_nm": 12, "aircraft": ac})
    return frames

def test_replayed_real_traffic_is_heard_and_verified():
    from world.replay import ReplaySource
    rec = _recording()
    r = run(None, seed=1, secs=90, replay=ReplaySource(rec, start_t=1000.0))
    last = {}
    for x in r["rows"]:
        last[x["icao"]] = x
    replayed = {k: v for k, v in last.items() if k.startswith("a1b2c")}
    assert len(replayed) == 3, sorted(last)
    assert all(v["state"] == "VERIFIED" for v in replayed.values()), {k: (v["state"], v["reasons"]) for k, v in replayed.items()}
    assert {v["id"] for v in replayed.values()} == {"REAL0", "REAL1", "REAL2"}
