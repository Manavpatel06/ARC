"""
node/rightofway.py — 14 CFR 91.113 right-of-way, applied to an encounter's geometry (hardening, Sat Oct 3).

Pure, deterministic, no I/O. Used by node.py to decide WHO maneuvers first and which way, and by the log so every
decision cites the rule. It never overrides safety: escape.py still rejects anything unsafe, and right-of-way only
orders options that already passed every check.

  (e) head-on            each aircraft alters course to the RIGHT                      -> both "R", nobody stands on
  (f) overtaking         the overtaken aircraft has the right-of-way; the overtaking  -> overtaker gives way "R",
                         one alters course to the right to pass well clear               overtaken stands on "HOLD"
  (d) converging         the aircraft to the other's right has the right-of-way        -> give way "R" (pass behind),
                                                                                          the other stands on "HOLD"
  (g) landing            the aircraft at the lower altitude has the right-of-way       -> higher gives way, lower stands on
Ambiguous geometry (target almost dead ahead/behind in a crossing, or tracks near a category boundary) -> treated
like head-on: both alter right. That is the symmetric choice both nodes reach without talking.

Source: 14 CFR 91.113(d)-(g), eCFR.
"""
from __future__ import annotations
import math
from dataclasses import dataclass
from typing import Optional

APPROACH_LEGS = {"BASE", "FINAL", "STRAIGHT_IN"}
HEAD_ON_DTRK = 135.0       # tracks within 45 deg of reciprocal
OVERTAKE_DTRK = 45.0       # tracks within 45 deg of each other
AHEAD_HALF_ANGLE = 70.0    # "ahead" for overtaking (FAA: overtaken aircraft is within 70 deg of the other's tail)
AMBIG_BAND = 12.0          # deg either side of dead ahead / dead astern in a crossing -> ambiguous
LANDING_ALT_EPS_FT = 75.0  # within this, altitude does not decide the landing rule
FT = 0.3048

@dataclass(frozen=True)
class Role:
    kind: str              # HEAD_ON | OVERTAKING | OVERTAKEN | GIVE_WAY | STAND_ON | LANDING_GIVE_WAY | LANDING_STAND_ON | AMBIGUOUS
    prefer: str            # "R" or "HOLD"
    gives_way: bool        # True = this aircraft is expected to maneuver
    rule: str              # human-readable citation for the log

    @property
    def stands_on(self) -> bool:
        return not self.gives_way

def _wrap(a: float) -> float:
    return (a + 180.0) % 360.0 - 180.0

def _brg(dx: float, dy: float) -> float:
    return math.degrees(math.atan2(dx, dy)) % 360.0

def classify(own: dict, tgt: dict) -> Role:
    """own/tgt: {"x","y" (m, ENU), "trk" (deg true), "gs" (m/s), "alt_ft", "leg" (optional str)}."""
    dtrk = abs(_wrap(tgt["trk"] - own["trk"]))
    rel = _wrap(_brg(tgt["x"] - own["x"], tgt["y"] - own["y"]) - own["trk"])          # where the target is, from me
    rel_back = _wrap(_brg(own["x"] - tgt["x"], own["y"] - tgt["y"]) - tgt["trk"])     # where I am, from the target

    # (g) landing: both on an approach leg -> the lower one has the right-of-way
    if own.get("leg") in APPROACH_LEGS and tgt.get("leg") in APPROACH_LEGS:
        d = own["alt_ft"] - tgt["alt_ft"]
        if d > LANDING_ALT_EPS_FT:
            return Role("LANDING_GIVE_WAY", "R", True, "91.113(g) landing: lower aircraft has the right-of-way; I am higher")
        if d < -LANDING_ALT_EPS_FT:
            return Role("LANDING_STAND_ON", "HOLD", False, "91.113(g) landing: I am the lower aircraft, I have the right-of-way")

    # (e) head-on (and anything close to it)
    if dtrk >= HEAD_ON_DTRK:
        return Role("HEAD_ON", "R", True, "91.113(e) head-on: each aircraft alters course to the right")

    # (f) overtaking
    if dtrk <= OVERTAKE_DTRK:
        if abs(rel) <= AHEAD_HALF_ANGLE and own["gs"] > tgt["gs"]:
            return Role("OVERTAKING", "R", True, "91.113(f) overtaking: I am overtaking, I pass on the right")
        if abs(rel_back) <= AHEAD_HALF_ANGLE and tgt["gs"] > own["gs"]:
            return Role("OVERTAKEN", "HOLD", False, "91.113(f) overtaking: I am being overtaken, I have the right-of-way")
        return Role("AMBIGUOUS", "R", True, "91.113: parallel/same speed - both alter right (symmetric default)")

    # (d) converging
    if abs(rel) < AMBIG_BAND or abs(rel) > 180.0 - AMBIG_BAND:
        return Role("AMBIGUOUS", "R", True, "91.113: target dead ahead/astern - both alter right (symmetric default)")
    if rel > 0:
        return Role("GIVE_WAY", "R", True, "91.113(d) converging: traffic on my right has the right-of-way, I pass behind")
    return Role("STAND_ON", "HOLD", False, "91.113(d) converging: traffic on my left gives way, I have the right-of-way")

def pair(own: dict, tgt: dict) -> tuple[Role, Role, Optional[bool]]:
    """Both nodes run this on the same shared data. Returns (my role, the peer's role, i_go_first):
    i_go_first True/False when the rules give one clear give-way and one stand-on aircraft; None otherwise
    (head-on, ambiguous, or the two views disagree) -> the caller keeps the lower-ID-first ordering."""
    me, them = classify(own, tgt), classify(tgt, own)
    if me.gives_way and them.stands_on:
        return me, them, True
    if me.stands_on and them.gives_way:
        return me, them, False
    if me.stands_on and them.stands_on:
        # never let two aircraft both "stand on": both fall back to the head-on default
        amb = Role("AMBIGUOUS", "R", True, "91.113: both appear to have the right-of-way - both alter right")
        return amb, amb, None
    return me, them, None
