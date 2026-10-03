"""
node/layers.py — Lane B (B6).

Four-layer escalation by time-to-conflict, with hysteresis so levels do not flap:
  SEQUENCE <= 90 s   TRAFFIC <= 35 s   RESOLVE <= 20 s   TAKEOVER <= 8 s
plus the advisory text/speech builders and the pattern sequencing plan
("NUMBER 2 - EXTEND DOWNWIND 15 S").  Pure logic; node.py decides what to send.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Optional

from node.geometry import FT, bearing_deg, wrap360

# Layer timings (seconds to predicted conflict).  "spec" is the contract in CONTEXT.md and the default.
# "early" warns the pilot 5 s sooner at the TRAFFIC and RESOLVE layers: in the Monte Carlo it cuts NMACs and
# NO_SOLUTION further (more chances for a pilot to respond) but raises nuisance alerts on benign traffic
# (about 3 % -> 7 %), so it is opt-in:  FLOCK_LAYERS=early python node/node.py ...
_PROFILES = {
    "spec": [("TAKEOVER", 8.0), ("RESOLVE", 20.0), ("TRAFFIC", 35.0), ("SEQUENCE", 90.0)],
    "early": [("TAKEOVER", 8.0), ("RESOLVE", 25.0), ("TRAFFIC", 40.0), ("SEQUENCE", 90.0)],
}
THRESHOLDS = _PROFILES.get(os.environ.get("FLOCK_LAYERS", "spec"), _PROFILES["spec"])
RANK = {"CLEAR": 0, "SEQUENCE": 1, "TRAFFIC": 2, "RESOLVE": 3, "TAKEOVER": 4}
LAYER_NUM = {"CLEAR": 0, "SEQUENCE": 1, "TRAFFIC": 2, "RESOLVE": 3, "TAKEOVER": 4, "RELEASE": 4, "NO_SOLUTION": 4}
_THR = dict(THRESHOLDS)
HYST_FRAC = 0.20          # a level is held until ttc exceeds its threshold by 20 % + 1 s
HYST_ABS_S = 1.0
CLEAR_HOLD_S = 4.0        # no conflict for this long before dropping to CLEAR
MIN_SPACING_M = 1200.0    # pattern spacing the sequencer tries to restore (~25 s at 90 kt)


def raw_level(ttc: Optional[float]) -> str:
    if ttc is None:
        return "CLEAR"
    for name, thr in THRESHOLDS:
        if ttc <= thr:
            return name
    return "CLEAR"


def cap_level(level: str, cap: str) -> str:
    return level if RANK[level] <= RANK[cap] else cap


@dataclass
class LayerTracker:
    """Per-target level with hysteresis."""
    level: str = "CLEAR"
    last_seen: float = -1e9
    since: float = 0.0
    history: list = field(default_factory=list)

    def update(self, now: float, ttc: Optional[float], cap: str = "TAKEOVER") -> str:
        want = cap_level(raw_level(ttc), cap)
        cur = self.level
        new = cur
        if ttc is not None:
            self.last_seen = now
        if RANK[want] > RANK[cur]:
            new = want                                   # escalate immediately, may skip levels
        elif RANK[want] < RANK[cur]:
            if ttc is None:
                if now - self.last_seen >= CLEAR_HOLD_S:
                    new = "CLEAR"
            else:
                held = _THR[cur] * (1.0 + HYST_FRAC) + HYST_ABS_S
                if ttc > held:
                    new = want
        if new != cur:
            self.level, self.since = new, now
            self.history.append((now, new))
        return new


# ---------- text ----------
_DIGITS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
_NATO_N = "november"


def spoken_callsign(cs: str) -> str:
    if cs.startswith("N") and cs[1:].isdigit():
        return _NATO_N + " " + " ".join(_DIGITS[int(c)] for c in cs[1:])
    return cs.lower()


def spoken_number(n: int) -> str:
    return " ".join(_DIGITS[int(c)] for c in str(abs(int(n))))


def clock_position(own_hdg: float, dx: float, dy: float) -> int:
    rel = wrap360(bearing_deg(dx, dy) - own_hdg)
    h = int(round(rel / 30.0)) % 12
    return 12 if h == 0 else h


def traffic_text(own_hdg: float, own_z: float, dx: float, dy: float, dz: float) -> tuple[str, str]:
    clock = clock_position(own_hdg, dx, dy)
    miles = math.hypot(dx, dy) / 1609.344
    if miles < 0.75:
        dist, dist_s = "LESS THAN 1 MILE", "less than one mile"
    else:
        n = max(1, int(round(miles)))
        dist, dist_s = f"{n} MILE{'S' if n > 1 else ''}", f"{_DIGITS[n] if n < 10 else n} mile{'s' if n > 1 else ''}"
    dz_ft = dz / FT
    if abs(dz_ft) < 300:
        alt, alt_s = "SAME ALTITUDE", "same altitude"
    else:
        hund = int(round(abs(dz_ft) / 100.0)) * 100
        rel = "ABOVE" if dz_ft > 0 else "BELOW"
        alt, alt_s = f"{hund} FT {rel}", f"{hund} feet {rel.lower()}"
    return (f"TRAFFIC - {clock} O'CLOCK - {dist} - {alt}",
            f"traffic, {_DIGITS[clock] if clock < 10 else clock} o'clock, {dist_s}, {alt_s}")


def maneuver_text(chosen: str, target: str, peer_sense: Optional[str], urgent: bool = False) -> tuple[str, str]:
    """RESOLVE text for candidate name like 'L30', 'R20', 'CLIMB', 'DESCEND'."""
    now_ = " NOW" if urgent else ""
    if chosen[0] in "LR" and chosen[1:].isdigit():
        side = "LEFT" if chosen[0] == "L" else "RIGHT"
        deg = chosen[1:]
        verb, verb_s = f"TURN {side} {deg}", f"turn {side.lower()} {spoken_number(int(deg))}"
    elif chosen == "CLIMB":
        verb, verb_s = "CLIMB", "climb"
    elif chosen == "DESCEND":
        verb, verb_s = "DESCEND", "descend"
    else:
        verb, verb_s = "CONTINUE", "continue"
    peer = {"L": "TURNING LEFT", "R": "TURNING RIGHT", "CLIMB": "CLIMBING", "DESCEND": "DESCENDING",
            "HOLD": "CONTINUING"}.get(peer_sense or "", "EXPECTED TO CONTINUE")
    return f"{verb}{now_} - {target} {peer}", f"{verb_s}{' now' if urgent else ''}, {spoken_callsign(target)} {peer.lower()}"


# ---------- sequencing ----------
@dataclass
class SeqPlan:
    runway: str
    order: list                    # callsigns, NUMBER 1 first
    extend_s: dict
    gap_m: float


def sequence_plan(runway: str, a_id: str, a_rem_m: float, a_v: float, b_id: str, b_rem_m: float, b_v: float,
                  min_spacing_m: float = MIN_SPACING_M) -> SeqPlan:
    """Order by path distance still to fly to the threshold; the follower extends to restore spacing."""
    if (a_rem_m, a_id) <= (b_rem_m, b_id):
        lead, lead_v, fol, fol_v, gap = a_id, a_v, b_id, b_v, b_rem_m - a_rem_m
    else:
        lead, lead_v, fol, fol_v, gap = b_id, b_v, a_id, a_v, a_rem_m - b_rem_m
    ext = {}
    if gap < min_spacing_m:
        need = (min_spacing_m - gap) / max(fol_v, 20.0)
        ext[fol] = float(min(60.0, max(10.0, math.ceil(need / 5.0) * 5.0)))
    return SeqPlan(runway, [lead, fol], ext, gap)


def sequence_text(me: str, plan: SeqPlan, own_leg: str, other: str) -> tuple[str, str]:
    num = plan.order.index(me) + 1
    if num == 1:
        return (f"NUMBER 1 - CONTINUE - {other} FOLLOWING", f"number one, continue, {spoken_callsign(other)} following")
    sec = int(plan.extend_s.get(me, 15))
    if own_leg in ("UPWIND", "CROSSWIND", "DOWNWIND", "BASE"):
        return (f"NUMBER 2 - EXTEND {own_leg} {sec} S", f"number two, extend {own_leg.lower()} {spoken_number(sec)} seconds")
    return (f"NUMBER 2 - SLOW AND SPACE {sec} S - {other} AHEAD",
            f"number two, slow and space {spoken_number(sec)} seconds, {spoken_callsign(other)} ahead")
