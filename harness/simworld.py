"""
harness/simworld.py — Lane B.

A small closed-loop world for evidence, no sockets: AI pattern pilots (node.predict.PatternFollower with
randomised pilot behaviour), a lossy/laggy radio, the 3-DOF-ish bank/climb dynamics the world applies to
COMMANDs, and real Node instances talking to each other.  Used by montecarlo.py and accept_b.py.

Everything random lives here (and in montecarlo.py): the nodes themselves are deterministic.
"""
from __future__ import annotations

import copy
import heapq
import json
import math
import os
import random
import sys
from dataclasses import dataclass, field
from typing import Callable, Optional

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from node.geometry import FT, G, KT, Pattern, build_patterns, hvec, to_latlon, wrap180, wrap360
from node.node import Node
from node.predict import KState, PatternFollower, Predictor

T0 = 1_759_500_000.0
RADIO_RANGE_M = 4828.0
NMAC_H_M, NMAC_V_M = 500 * FT, 100 * FT
ROLL_RATE = 15.0
TSS_BUG_LEAD_S = float(os.environ.get("FLOCK_TSS_LEAD_S", "6.0"))   # AP heading bug moves this long before a turn


def apply_deviation(f: PatternFollower, dev: Optional[dict]) -> bool:
    """A pilot breaking the standard pattern (the predictor's model does not know):
         {"kind": "extend", "leg": "DOWNWIND", "s": 30}   fly on s seconds past the normal turn point (spacing)
         {"kind": "early",  "leg": "DOWNWIND", "frac": .5} turn to the next leg once this far along the leg
    Returns True once applied (call every step until then)."""
    if dev is None or f.leg != dev["leg"] or f.turning:
        return False
    if dev["kind"] == "extend":
        f.extend_s = float(dev["s"])
        return True
    L = f.pat.legs[f.leg]
    along, _, _ = L.along_cross(f.x, f.y)
    if along >= dev["frac"] * L.length:
        f.turn_at = (f.pat.next_leg(f.leg), f.t)
        return True
    return False


def _ground_m(x: float, y: float, fallback: float) -> float:
    """Terrain under the aircraft from the real grid (the world reports AGL against terrain, contract v1.1)."""
    try:
        from data.terrain import elev_at
        return elev_at(*to_latlon(x, y))
    except Exception:
        return fallback


@dataclass
class Maneuver:
    bank: float
    vs_fpm: float
    until: float
    src: str                           # "cmd" | "pilot"
    go_around: bool = False            # after it the pilot is on the upwind leg, not back on the same final


class SimAircraft:
    def __init__(self, ac_id: str, pat: Pattern, leg: Optional[str], x: float, y: float, z: float, hdg: float,
                 v_kt: float, ap: bool = False, flock: bool = True, human: bool = True, pilot_bank: float = 20.0,
                 lead_scale: float = 1.0, comply: float = 0.7, react_s: float = 5.0, gps_sigma_m: float = 3.0,
                 deviation: Optional[dict] = None):
        self.id, self.pat = ac_id, pat
        self.deviation = deviation                   # apply_deviation(); dropped once flown or a maneuver replaces the plan
        self.x, self.y, self.z, self.hdg, self.v = x, y, z, hdg, v_kt * KT
        self.bank, self.vs = 0.0, 0.0
        self.ap, self.flock, self.human = ap, flock, human
        self.pilot_bank, self.lead_scale = pilot_bank, lead_scale
        self.comply, self.react_s = comply, react_s
        self.gps_sigma = gps_sigma_m
        self.follower: Optional[PatternFollower] = None
        if leg is not None:
            self.follower = PatternFollower(pat, leg, x, y, z, self.v, hdg, bank_deg=pilot_bank, lead_scale=lead_scale)
        self.maneuver: Optional[Maneuver] = None
        self.stay_straight = leg is None                 # transit aircraft never join the pattern
        self.stick_until = -1.0
        self.pending: Optional[tuple[float, Maneuver]] = None        # pilot reaction scheduled
        self.responded_to: dict = {}                    # (target, kind) -> last time the pilot acted
        self.v_cmd: Optional[float] = None               # slowed speed while "slow and space" is flown
        self.slow_until = -1.0
        self.v_nominal = self.v
        self.node: Optional[Node] = None

    @property
    def stick_active(self) -> bool:
        return self.stick_until > 0

    def ownship(self, t: float, rng: random.Random) -> dict:
        lat, lon = to_latlon(self.x + rng.gauss(0, self.gps_sigma), self.y + rng.gauss(0, self.gps_sigma))
        msl = (self.z + rng.gauss(0, 2.0)) / FT
        return {"type": "OWNSHIP", "ac_id": self.id, "t": t, "lat": lat, "lon": lon, "alt_msl_ft": msl,
                "alt_press_ft": msl - 10.0, "agl_ft": (self.z - _ground_m(self.x, self.y, self.pat.elev_m)) / FT, "gs_kt": self.v / KT,
                "track_deg": self.hdg, "hdg_deg": self.hdg, "bank_deg": self.bank, "vs_fpm": self.vs / FT * 60.0,
                "ias_kt": self.v / KT, "ap_equipped": self.ap, "stick_active": self.stick_active, "flaps": 0,
                **(self.autopilot_targets() if self.ap else {})}

    def autopilot_targets(self) -> dict:
        """What this aircraft's autopilot is really commanding (ADS-B Target State & Status source): the heading bug
        is on the next leg from TSS_BUG_LEAD_S before the turn the pilot actually flies, deviations included."""
        f = self.follower
        if f is None or self.maneuver is not None or f.landed:
            return {}
        hdg = f.turn_target if f.turning else None
        if hdg is None:
            g, dev = copy.copy(f), self.deviation
            for _ in range(int(TSS_BUG_LEAD_S)):
                if dev is not None and apply_deviation(g, dev):
                    dev = None
                g.step(1.0)
                if g.turning:
                    hdg = g.turn_target
                    break
        out = {"sel_hdg_deg": round((hdg if hdg is not None else f.hdg) % 360.0, 1)}
        if f.leg in ("UPWIND", "CROSSWIND", "DOWNWIND"):
            out["sel_alt_ft"] = round(f.pat.tpa_msl_m / FT - 10.0, -1)        # pressure alt (sim: MSL - 10 ft)
        return out

    def start_maneuver(self, m: Maneuver) -> None:
        self.maneuver = m
        self.follower = None
        self.deviation = None

    def end_maneuver(self, predictor: Predictor) -> None:
        was = self.maneuver
        self.maneuver = None
        if was is not None and was.go_around:
            st = KState(self.x, self.y, self.z, self.v, self.hdg, self.vs, 0.0, self.z - self.pat.elev_m)
            cls = predictor.classify(st)
            if cls.runway:
                self.follower = PatternFollower(self.patterns_for(cls.runway), "UPWIND", self.x, self.y, self.z, self.v,
                                                self.hdg, bank_deg=self.pilot_bank, lead_scale=self.lead_scale)
                return
        st = KState(self.x, self.y, self.z, self.v, self.hdg, self.vs, 0.0, self.z - self.pat.elev_m)
        st.turn_rate = self.bank_rate_dps()
        cls = predictor.classify(st)
        rwy, leg = cls.runway, cls.leg
        if rwy is None or leg == "UNKNOWN" or cls.conf < 0.35:
            # a pilot always knows roughly where the pattern is: pick the nearest leg that is not behind us
            best = None
            for r, pat in self._patterns.items():
                for nm in ("UPWIND", "CROSSWIND", "DOWNWIND", "BASE", "FINAL", "STRAIGHT_IN"):
                    L = pat.legs[nm]
                    _, cross, out = L.along_cross(self.x, self.y)
                    score = math.hypot(cross, out) + 8.0 * abs(wrap180(self.hdg - L.heading))
                    if best is None or score < best[0]:
                        best = (score, r, nm)
            _, rwy, leg = best
        leg = "UPWIND" if leg == "GO_AROUND" else leg
        if self.stay_straight:
            self.follower = None
            return
        self.follower = PatternFollower(self.patterns_for(rwy), leg, self.x, self.y, self.z, self.v, self.hdg,
                                        bank_deg=self.pilot_bank, lead_scale=self.lead_scale)

    def bank_rate_dps(self) -> float:
        return math.degrees(G * math.tan(math.radians(self.bank)) / max(self.v, 1.0))

    def patterns_for(self, rwy: str) -> Pattern:
        return self._patterns[rwy]

    def step(self, dt: float, t: float, predictor: Predictor) -> None:
        if self.stick_until > 0 and t >= self.stick_until:
            self.stick_until = -1.0
        m = self.maneuver
        if m is not None and t >= m.until:
            self.end_maneuver(predictor)
            m = None
        if m is None and self.follower is not None and self.stick_until < 0:
            f = self.follower
            if self.deviation is not None and apply_deviation(f, self.deviation):
                self.deviation = None
            h0 = f.hdg
            if self.slow_until > 0 and t >= self.slow_until:
                self.slow_until, self.v_cmd = -1.0, None
            tv = self.v_cmd if self.v_cmd is not None else self.v_nominal
            f.v += max(-0.5 * dt, min(0.5 * dt, tv - f.v)) if not f.landed else 0.0
            f.step(dt)
            self.x, self.y, self.z, self.hdg, self.v, self.vs = f.x, f.y, f.z, f.hdg, f.v, f.vz
            w = math.radians(wrap180(f.hdg - h0)) / dt
            self.bank = math.degrees(math.atan(w * max(self.v, 1.0) / G))
            return
        tb = m.bank if m is not None else (self.bank if self.stick_until > 0 else 0.0)
        tv = (m.vs_fpm * FT / 60.0) if m is not None else 0.0
        self.bank += max(-ROLL_RATE * dt, min(ROLL_RATE * dt, tb - self.bank))
        self.vs += max(-1.0 * dt, min(1.0 * dt, tv - self.vs))
        self.hdg = wrap360(self.hdg + math.degrees(G * math.tan(math.radians(self.bank)) / max(self.v, 1.0)) * dt)
        hx, hy = hvec(self.hdg)
        self.x += hx * self.v * dt
        self.y += hy * self.v * dt
        self.z = max(self.pat.elev_m, self.z + self.vs * dt)


# ---------------------------------------------------------------------------------------------
@dataclass
class Result:
    min_h_m: float = 1e9
    min_v_at_min_h_m: float = 1e9
    nmac: bool = False
    t_cpa: float = 0.0
    first_conflict_t: dict = field(default_factory=dict)      # node id -> sim time its tracker first left CLEAR
    first_seen_t: dict = field(default_factory=dict)          # node id -> sim time the predictor first showed a conflict
    first_level_t: dict = field(default_factory=dict)         # (id, level) -> t
    max_bank: dict = field(default_factory=dict)              # id -> largest commanded/advised bank
    maneuvered: set = field(default_factory=set)
    advisories: list = field(default_factory=list)            # (t, id, frame)
    commands: list = field(default_factory=list)
    commits: list = field(default_factory=list)               # (t, id, body)
    tick_ms_max: float = 0.0
    tick_ms_mean: float = 0.0
    min_sep_noavoid_h: float = 0.0


class Sim:
    def __init__(self, patterns: dict, aircraft: list[SimAircraft], node_factory: Optional[Callable] = None,
                 loss: float = 0.1, latency_s: float = 0.3, dt: float = 0.1, seed: int = 0,
                 stick_events: Optional[dict] = None, verbose: bool = False, pair: Optional[tuple] = None,
                 follow_sequence: bool = True, blackout: Optional[tuple] = None):
        self.patterns = patterns
        self.predictor = Predictor(patterns)
        self.aircraft = {a.id: a for a in aircraft}
        for a in aircraft:
            a._patterns = patterns
        self.loss, self.latency, self.dt = loss, latency_s, dt
        self.rng = random.Random(seed * 7919 + 13)
        self.gps_rng = random.Random(seed * 104729 + 7)
        self.t = T0
        self.queue: list = []
        self.seqno: dict[str, int] = {a.id: 0 for a in aircraft}
        self.stick_events = stick_events or {}
        self.verbose = verbose
        self.follow_sequence = follow_sequence
        self.blackout = blackout                          # (t_start, t_end) seconds from T0: the radio delivers nothing
        self.res = Result()
        self.pair = pair or tuple(list(self.aircraft)[:2])
        if node_factory:
            for a in aircraft:
                if a.flock:
                    a.node = node_factory(a.id)

    # --- radio
    def _broadcast(self, src: str, msg: str, body: dict) -> None:
        self.seqno[src] += 1
        env = {"msg": msg, "from": src, "seq": self.seqno[src], "t": self.t, "sig": "", "body": body}
        if msg == "MANEUVER_COMMIT":
            self.res.commits.append((self.t, src, body))
        a = self.aircraft[src]
        if self.blackout and self.blackout[0] <= self.t - T0 < self.blackout[1]:
            return
        for rid, r in self.aircraft.items():
            if rid == src or r.node is None:
                continue
            if math.hypot(a.x - r.x, a.y - r.y) > RADIO_RANGE_M:
                continue
            if self.rng.random() < self.loss:
                continue
            heapq.heappush(self.queue, (self.t + self.latency + self.rng.uniform(-0.1, 0.1), self.rng.random(), rid, env))

    def _deliver(self) -> None:
        while self.queue and self.queue[0][0] <= self.t:
            _, _, rid, env = heapq.heappop(self.queue)
            n = self.aircraft[rid].node
            if n is not None:
                n.on_radio(env)

    # --- world applying node output
    def _apply(self, a: SimAircraft, frames: list, radios: list) -> None:
        for msg, body in radios:
            self._broadcast(a.id, msg, body)
        for f in frames:
            ty = f.get("type")
            if ty == "ADVISORY":
                self.res.advisories.append((self.t, a.id, f))
                key = (a.id, f["level"])
                self.res.first_level_t.setdefault(key, self.t)
                self._pilot_react(a, f)
            elif ty == "COMMAND":
                self.res.commands.append((self.t, a.id, f))
                if f["mode"] == "TAKEOVER" and a.ap and not a.stick_active:
                    a.start_maneuver(Maneuver(max(-30.0, min(30.0, f["bank_cmd_deg"])), f["vs_cmd_fpm"],
                                              self.t + min(f["hold_s"], 10.0), "cmd",
                                              go_around=f.get("reason", {}).get("chosen") == "GO_AROUND"))
                    self._note_bank(a.id, f["bank_cmd_deg"])
                elif f["mode"] == "RELEASE" and a.maneuver is not None and a.maneuver.src == "cmd":
                    a.end_maneuver(self.predictor)

    def _note_bank(self, ac_id: str, bank: float) -> None:
        self.res.maneuvered.add(ac_id)
        self.res.max_bank[ac_id] = max(self.res.max_bank.get(ac_id, 0.0), abs(bank))

    def _pilot_react(self, a: SimAircraft, adv: dict) -> None:
        tgt = adv.get("target_id")
        if adv["level"] == "SEQUENCE" and self.follow_sequence and a.human and a.follower is not None:
            ext = adv.get("reason", {}).get("extend_s", {}).get(a.id)
            if ext and self.t - a.responded_to.get((tgt, "seq"), -1e9) > 60.0:
                a.responded_to[(tgt, "seq")] = self.t
                if self.rng.random() <= a.comply:
                    if a.follower.leg in ("FINAL", "STRAIGHT_IN"):
                        a.v_cmd, a.slow_until = max(a.v_nominal * 0.78, 38.0), self.t + 45.0
                    else:
                        a.follower.extend_s = max(a.follower.extend_s, float(ext))
            return
        if adv["level"] != "RESOLVE" or not a.human or a.maneuver is not None or a.pending is not None:
            return
        chosen = adv.get("reason", {}).get("chosen")
        if not chosen or self.t - a.responded_to.get((tgt, "res"), -1e9) < 15.0:
            return
        a.responded_to[(tgt, "res")] = self.t
        if self.rng.random() > a.comply:
            return
        bank, vs = 0.0, 0.0
        if chosen[0] in "LR" and chosen[1:].isdigit():
            bank = (1.0 if chosen[0] == "R" else -1.0) * float(chosen[1:])
        elif chosen == "CLIMB":
            vs = 500.0
        elif chosen == "DESCEND":
            vs = -500.0
        elif chosen == "GO_AROUND":
            vs = 500.0
        else:
            return
        a.pending = (self.t + a.react_s, Maneuver(max(-45.0, min(45.0, bank)), vs,
                                                  20.0 if chosen == "GO_AROUND" else float(adv.get("reason", {}).get("hold_s", 10.0)),
                                                  "pilot", go_around=(chosen == "GO_AROUND")))

    # --- main loop
    def run(self, duration: float) -> Result:
        res = self.res
        steps = int(duration / self.dt)
        ids = list(self.aircraft)
        for k in range(steps):
            self.t = T0 + k * self.dt
            self._deliver()
            for a in self.aircraft.values():
                if a.pending is not None and self.t >= a.pending[0]:
                    m = a.pending[1]
                    m.until = self.t + m.until            # until was the maneuver duration while pending
                    a.pending = None
                    a.start_maneuver(m)
                    self._note_bank(a.id, m.bank)
            ev = self.stick_events
            for aid, ts in list(ev.items()):
                if self.t - T0 >= ts and aid in self.aircraft:
                    del ev[aid]
                    a = self.aircraft[aid]
                    a.stick_until = self.t + 1.0
                    a.bank = a.bank
                    if a.maneuver is not None and a.maneuver.src == "cmd":
                        a.maneuver = None
                    if a.node is not None:
                        frames = a.node.on_stick({"type": "STICK", "ac_id": aid, "t": self.t})
                        self._apply(a, frames, [])
                        res.stick_release_t = self.t
            for a in self.aircraft.values():
                a.step(self.dt, self.t, self.predictor)
            for a in self.aircraft.values():
                if a.node is not None:
                    frames, radios = a.node.tick(a.ownship(self.t, self.gps_rng))
                    self._apply(a, frames, radios)
                    if a.id not in res.first_conflict_t and any(t.level != "CLEAR" for t in a.node.trackers.values()):
                        res.first_conflict_t[a.id] = self.t - T0
                    if a.id not in res.first_seen_t and a.node.first_conflict_seen is not None:
                        res.first_seen_t[a.id] = a.node.first_conflict_seen - T0
            # truth separation for the pair of interest and all other pairs
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    p, q = self.aircraft[ids[i]], self.aircraft[ids[j]]
                    if p.z < p.pat.elev_m + 6.0 or q.z < q.pat.elev_m + 6.0:
                        continue                                    # on the runway: not an airborne encounter
                    dh = math.hypot(p.x - q.x, p.y - q.y)
                    dz = abs(p.z - q.z)
                    if max(dh / NMAC_H_M, dz / NMAC_V_M) < max(res.min_h_m / NMAC_H_M, res.min_v_at_min_h_m / NMAC_V_M):
                        res.min_h_m, res.min_v_at_min_h_m, res.t_cpa = dh, dz, self.t - T0
        res.nmac = res.min_h_m < NMAC_H_M and res.min_v_at_min_h_m < NMAC_V_M
        ticks = [ms for a in self.aircraft.values() if a.node for ms in a.node.tick_ms]
        if ticks:
            res.tick_ms_max, res.tick_ms_mean = max(ticks), sum(ticks) / len(ticks)
        return res


# ---------------------------------------------------------------------------------------------
def load_scenario(path: str, patterns: dict, speed_kt: float = 90.0, comply: float = 0.7) -> list[SimAircraft]:
    """Place aircraft per harness/scenarios/*.json (leg, runway, offset_s along the leg, optional agl_ft)."""
    sc = json.load(open(path))
    out = []
    for spec in sc["aircraft"]:
        st = spec["start"]
        pat = patterns[st.get("runway", "25L")]
        pl = pat.place(st["leg"], st.get("offset_s", 0.0), speed_kt, st.get("agl_ft"))     # shared pattern.place() semantics
        x, y, h = pl["x_m"], pl["y_m"], pl["hdg_deg"]
        z = pat.elev_m + pl["agl_ft"] * FT
        out.append(SimAircraft(spec["id"], pat, pl["leg"], x, y, z, h, speed_kt, ap=spec.get("ap", False),
                               flock=spec.get("flock", True), human=True, comply=comply))
    return out
