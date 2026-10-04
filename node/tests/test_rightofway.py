"""14 CFR 91.113 right-of-way classification (node/rightofway.py)."""
import math
from node import rightofway as R

def ac(x, y, trk, kt=90, alt=2500, leg=None):
    return {"x": x, "y": y, "trk": trk, "gs": kt * 0.514444, "alt_ft": alt, "leg": leg}

def test_head_on_both_turn_right():
    a, b = ac(0, 0, 0), ac(0, 3000, 180)
    me, them, first = R.pair(a, b)
    assert me.kind == them.kind == "HEAD_ON" and me.prefer == them.prefer == "R" and first is None

def test_converging_traffic_on_right_gives_way():
    a = ac(0, 0, 0)                 # heading north
    b = ac(2000, 2000, 270)         # to a's right, heading west across a's path
    me, them, first = R.pair(a, b)
    assert me.kind == "GIVE_WAY" and me.prefer == "R"
    assert them.kind == "STAND_ON" and them.prefer == "HOLD"
    assert first is True
    _, _, first_b = R.pair(b, a)
    assert first_b is False

def test_overtaking_passes_right_overtaken_stands_on():
    fast, slow = ac(0, 0, 90, kt=120), ac(1500, 0, 90, kt=85)
    me, them, first = R.pair(fast, slow)
    assert me.kind == "OVERTAKING" and them.kind == "OVERTAKEN" and first is True and them.prefer == "HOLD"

def test_landing_lower_aircraft_has_right_of_way():
    hi = ac(0, 0, 266, alt=2100, leg="FINAL")
    lo = ac(-800, 300, 266, alt=1800, leg="STRAIGHT_IN")
    me, them, first = R.pair(hi, lo)
    assert me.kind == "LANDING_GIVE_WAY" and them.kind == "LANDING_STAND_ON" and first is True

def test_never_both_stand_on():
    for ang in range(0, 360, 7):
        for brg in range(0, 360, 11):
            a = ac(0, 0, 0)
            b = ac(3000 * math.sin(math.radians(brg)), 3000 * math.cos(math.radians(brg)), ang)
            me, them, _ = R.pair(a, b)
            assert not (me.stands_on and them.stands_on), (ang, brg)
