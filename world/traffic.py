"""
world/traffic.py — Lane A. Pattern autopilot for AI aircraft.

Geometry comes from the shared /pattern.py (one definition for world, nodes and god view):
UPWIND -> CROSSWIND -> DOWNWIND -> BASE -> FINAL -> touch-and-go -> UPWIND, plus STRAIGHT_IN,
each leg a straight ENU segment with AGL at both ends. 25L = left traffic (south), 25R = right
traffic (north); headings 086/266 true.

The autopilot steers a ground TRACK along each leg (cross-track correction, so wind cannot make
it drift), banks 20-24 deg in turns, leads each turn by r*tan(dpsi/2) + roll-in distance, and
follows the leg's altitude profile with a feed-forward descent rate on descending legs. It only
produces targets; the limits (bank rate, climb vs density altitude, speed envelope) live in
world/flight_model.py.

Phases (PatternPilot.phase). AI traffic stays in PATTERN and does touch-and-goes. The cockpit AP
button (engage()) flies ONE circuit to a full stop:
  in the air:   LEVEL (wings level, hold altitude) -> JOIN (take the leg it is already lined up
                with, else fly direct to a downwind entry point at TPA) -> PATTERN -> after the
                threshold ROLLOUT (brake on the centreline) -> STOPPED
  on the ground: TAKEOFF (full power on the centreline, rotate at 55 kt) -> PATTERN -> ... -> STOPPED
Extra phases for live traffic (TrafficGenerator below) and free-flying judges:
  INBOUND    fly to waypoints (the 45-degree entry point) at an altitude, then JOIN
  DEPART     after takeoff: climb out on the centreline, turn to a departure heading, leave the area
  TRANSIT    judge start: straight and level on the inbound track until 2 NM past the field, then JOIN
  GO_AROUND  climb on runway heading (offset left/right if advised), then the circuit again from UPWIND
AI pilots also listen to their FLOCK node (advise()): ~3 s after a SEQUENCE / RESOLVE they do what it says,
70 % of the time - EXTEND (later base turn), SLOW AND SPACE, TURN LEFT/RIGHT n, CLIMB, DESCEND - and go
around if the advice reaches them on final.

TrafficGenerator: AI aircraft that come and go - arrivals (45-degree entry or straight-in, full stop),
departures, touch-and-go circuits - keeping 5-8 airborne on the runway the wind favours. A fixed seed
gives the same spawns every run.
"""
from __future__ import annotations
import math, random, re, zlib
from dataclasses import dataclass

import pattern as P
from world.flight_model import BANK_RATE_DPS, FT, G, KT, Aircraft, Env, clamp, tas_kt, wrap180

PATTERN_BANK = 22.0
XTRK_GAIN = 0.15               # deg of track correction per metre off course
GROUND_ROLL_M = 300.0          # touch-and-go: roll this far before rotating
AIM_POINT_M = 200.0            # final approach aiming point past the threshold
JOIN_ENTRY_FRAC = 0.35         # JOIN aims at this fraction along the downwind (midfield-ish)
JOIN_LEG_XTRK_M = 450.0        # already this close to a leg and roughly aligned -> join it directly
JOIN_LEG_HDG_DEG = 50.0
LEVEL_IAS = (80.0, 100.0)
TAKEOFF_IAS, ROTATE_IAS = 75.0, 55.0
LEG_IAS = {"UPWIND": 75, "CROSSWIND": 85, "DOWNWIND": 95, "BASE": 85, "FINAL": 72, "STRAIGHT_IN": 80}
CIRCUIT = ["UPWIND", "CROSSWIND", "DOWNWIND", "BASE", "FINAL"]
NM_M = 1852.0
INBOUND_IAS = 100.0
FOLLOW_P = 0.7                 # AI pilots follow their node's advice this often ...
REACT_S = (2.5, 3.5)           # ... this long after it arrives
MANEUVER_S = 12.0              # a TURN / CLIMB / DESCEND is flown this long, then back to the pattern
VACATE_KT = 15.0               # landing traffic turns off the runway (despawns) below this
DEPART_EXIT_NM = 5.5           # departures are gone past this

@dataclass
class Leg:
    name: str
    a: tuple[float, float]
    b: tuple[float, float]
    agl0_ft: float
    agl1_ft: float

    @property
    def brg(self) -> float:
        return math.degrees(math.atan2(self.b[0] - self.a[0], self.b[1] - self.a[1])) % 360.0

    @property
    def length(self) -> float:
        return math.hypot(self.b[0] - self.a[0], self.b[1] - self.a[1])

    def project(self, p) -> tuple[float, float]:
        """(along-track m from a, cross-track m; + = right of course)."""
        ue, un = math.sin(math.radians(self.brg)), math.cos(math.radians(self.brg))
        de, dn = p[0] - self.a[0], p[1] - self.a[1]
        return de * ue + dn * un, de * un - dn * ue

    def agl_at(self, along: float) -> float:
        f = clamp(along / self.length, 0.0, 1.0) if self.length else 1.0
        return self.agl0_ft + f * (self.agl1_ft - self.agl0_ft)

    @property
    def slope(self) -> float:
        """Descent gradient (ft of AGL lost per ft flown), >= 0."""
        return max(0.0, (self.agl0_ft - self.agl1_ft) / (self.length / FT)) if self.length else 0.0

def _leg(seg: dict) -> Leg:
    return Leg(seg["name"], tuple(seg["start"]), tuple(seg["end"]), seg["agl0_ft"], seg["agl1_ft"])

class Pattern:
    """Pattern geometry for one landing runway ident (e.g. '25L'), from pattern.py."""
    def __init__(self, ident: str):
        self.ident = ident
        self.side = P._RW["ends"][ident]["pattern"]
        self.elev_ft = P.ELEV_FT
        self.tpa_ft = P.ELEV_FT + P.TPA_AGL_FT
        self.legs = {s["name"]: _leg(s) for s in P.legs(ident)}
        self.legs["STRAIGHT_IN"] = _leg(P.straight_in(ident))
        thr, far = self.legs["FINAL"].b, self.legs["UPWIND"].a
        self.runway_len_m = math.hypot(far[0] - thr[0], far[1] - thr[1])
        self.legs["RUNWAY"] = Leg("RUNWAY", thr, far, 0.0, 0.0)       # centreline: rollout / takeoff

    @staticmethod
    def to_enu(lat: float, lon: float) -> tuple[float, float]:
        return P.to_enu(lat, lon)

    def next_leg(self, name: str) -> str:
        if name in ("FINAL", "STRAIGHT_IN", "RUNWAY"):
            return "UPWIND"
        return CIRCUIT[CIRCUIT.index(name) + 1]

    def geometry(self) -> dict:
        """Lat/lon leg segments for the god view."""
        ll = lambda p: [round(v, 6) for v in P.from_enu(*p)]
        return {"runway": self.ident, "side": self.side, "tpa_ft": self.tpa_ft,
                "legs": {n: [ll(L.a), ll(L.b)] for n, L in self.legs.items() if n != "RUNWAY"}}

class PatternPilot:
    """Autopilot that flies the circuit forever (touch-and-goes)."""
    def __init__(self, pattern: Pattern, leg: str, ac_id: str):
        self.p = pattern
        self.leg = "UPWIND" if leg == "GO_AROUND" else leg
        h = zlib.crc32(ac_id.encode())
        self.ias_bias = (h % 9) - 4.0                 # +-4 kt per aircraft
        self.bank = PATTERN_BANK + ((h >> 8) % 5) - 2  # 20-24 deg
        self.phase = "STOPPED" if leg == "RUNWAY" else "PATTERN"
        self.full_stop = False                         # True once the AP button engaged it
        self.touched = True                            # wheels have touched since the last final
        # live traffic and free-flying judges
        self.vacate = False                            # AI: done once slowed on the runway (taxied off)
        self.circuits_left: int | None = None          # touch-and-goes left before the full stop
        self.route: list[tuple[float, float, float]] = []   # INBOUND waypoints (x, y, alt MSL ft)
        self.depart: tuple[float, float] | None = None # DEPART: (heading deg true, climb to alt MSL ft)
        self.transit: tuple[float, float] | None = None     # TRANSIT: (track deg, alt MSL ft)
        self.done = False                              # the generator removes it
        self.extend_s = 0.0                            # SEQUENCE: extra downwind before turning base
        self.over: dict[str, Leg] = {}                 # legs moved by an extension (shifted BASE)
        self.slow_until = -1e9
        self.man: tuple[float, float, float] | None = None   # flying advice: (until, bank, vs)
        self.ga_offset = 0.0
        self.pending: list[tuple[float, tuple]] = []   # advice about to be acted on: (when, action)
        self.seen: dict[tuple, float] = {}
        self.advice_log: list[dict] = []
        self.rng = random.Random(h)
        self.follow_p = FOLLOW_P

    # ---------- AP button ----------
    def engage(self, ac: Aircraft) -> str:
        """Cockpit AP button: fly one circuit to a full stop (or take off first if on the ground)."""
        self.full_stop = True
        if ac.on_ground:
            self.leg, self.phase = "RUNWAY", ("TAKEOFF" if ac.ias_kt < ROTATE_IAS else "ROLLOUT")
        else:
            self.phase = "LEVEL"
        return self.phase

    # ---------- FLOCK advice (AI pilots) ----------
    def advise(self, m: dict, now: float) -> dict | None:
        """An ADVISORY from this aircraft's node. Decides (FOLLOW_P) whether the pilot complies and queues the
        action ~3 s out. Returns a log entry, or None when there is nothing to act on or it is a repeat."""
        lvl, text = m.get("level"), str(m.get("text") or "")
        if lvl not in ("SEQUENCE", "RESOLVE", "TAKEOVER") or self.phase in ("ROLLOUT", "STOPPED", "TAKEOFF"):
            return None
        act = self._parse(lvl, text, (m.get("reason") or {}).get("chosen"))
        if act is None:
            return None
        key = (lvl, text)
        if now - self.seen.get(key, -1e9) < 30.0:              # the node repeats itself: one decision each
            return None
        self.seen[key] = now
        follow = self.rng.random() < self.follow_p
        entry = {"t": round(now, 1), "level": lvl, "text": text, "action": list(act), "follow": follow}
        self.advice_log.append(entry)
        if follow:
            self.pending.append((now + self.rng.uniform(*REACT_S), act))
        return entry

    def _parse(self, lvl: str, text: str, chosen: str | None) -> tuple | None:
        T = text.upper()
        on_final = self.phase == "PATTERN" and self.leg in ("FINAL", "STRAIGHT_IN")
        if "GO AROUND" in T:
            return ("GA", 0.0)
        if lvl == "SEQUENCE":
            mm = re.search(r"EXTEND \w+ (\d+) S", T)
            if mm:
                return ("EXT", float(mm.group(1)))
            mm = re.search(r"SLOW AND SPACE (\d+) S", T)
            return ("SLOW", float(mm.group(1))) if mm else None
        if not chosen:
            mm = re.search(r"TURN (LEFT|RIGHT) (\d+)", T)
            chosen = (mm.group(1)[0] + mm.group(2)) if mm else ("CLIMB" if T.startswith("CLIMB")
                                                                 else "DESCEND" if T.startswith("DESCEND") else None)
        if not chosen or chosen in ("HOLD", "CONTINUE"):
            return None
        if on_final and chosen != "DESCEND":                  # advice on final: go around (sidestep for a turn)
            return ("GA", (-20.0 if chosen[0] == "L" else 20.0) if chosen[0] in "LR" else 0.0)
        if chosen[0] in "LR" and chosen[1:].isdigit():
            return ("TURN", (-1.0 if chosen[0] == "L" else 1.0) * min(30.0, float(chosen[1:])))
        if chosen == "CLIMB":
            return ("VS", 700.0)
        if chosen == "DESCEND":
            return ("VS", -500.0)
        return None

    def _react(self, ac: Aircraft, now: float) -> None:
        due = [a for t, a in self.pending if t <= now]
        self.pending = [(t, a) for t, a in self.pending if t > now]
        for kind, arg in due:
            if kind == "EXT":
                self.extend_s = min(60.0, self.extend_s + arg)
            elif kind == "SLOW":
                self.slow_until = now + 2.0 * arg
            elif kind == "TURN":
                self.man = (now + MANEUVER_S, arg, 0.0)
            elif kind == "VS":
                self.man = (now + MANEUVER_S, 0.0, arg)
            elif kind == "GA" and not ac.on_ground and self.phase not in ("ROLLOUT", "STOPPED", "TAKEOFF"):
                self.go_around(arg)

    def go_around(self, offset_deg: float = 0.0) -> None:
        self.phase, self.leg, self.ga_offset, self.man = "GO_AROUND", "UPWIND", offset_deg, None
        self.over.clear()
        self.touched = True

    def leg_geom(self, name: str) -> Leg:
        return self.over.get(name) or self.p.legs[name]

    def _choose_join(self, ac: Aircraft, pos) -> None:
        """Join the leg we are already lined up with, else head for the downwind entry point."""
        for name in ("FINAL", "STRAIGHT_IN", "BASE", "DOWNWIND"):
            L = self.p.legs[name]
            along, xtrk = L.project(pos)
            if 0 < along < L.length * 0.8 and abs(xtrk) < JOIN_LEG_XTRK_M and abs(wrap180(ac.track_deg - L.brg)) < JOIN_LEG_HDG_DEG:
                self.leg, self.phase = name, "PATTERN"
                return
        self.phase = "JOIN"

    def _steer_to(self, ac: Aircraft, brg: float) -> float:
        return clamp(1.6 * wrap180(brg - ac.track_deg), -self.bank, self.bank)

    def _ground(self, ac: Aircraft, pos) -> tuple[float, float, float]:
        """ROLLOUT / STOPPED / TAKEOFF on the runway centreline (bank target = nosewheel steering)."""
        L = self.p.legs["RUNWAY"]
        along, xtrk = L.project(pos)
        steer = clamp(1.6 * wrap180(L.brg - clamp(xtrk * 0.5, -20, 20) - ac.hdg_deg), -30, 30)
        if self.phase == "TAKEOFF":
            if ac.agl_ft > 100:
                self.leg, self.phase, self.touched = "UPWIND", ("DEPART" if self.depart else "PATTERN"), True
                return 0.0, 9_999, LEG_IAS["UPWIND"] + self.ias_bias
            return steer if ac.on_ground else 0.0, (9_999 if ac.ias_kt >= ROTATE_IAS else 0.0), TAKEOFF_IAS
        if self.phase == "ROLLOUT" and not ac.on_ground:
            # crossed the threshold still airborne: flare and settle onto the centreline
            return clamp(2.5 * wrap180(L.brg - clamp(xtrk * 0.45, -20, 20) - ac.track_deg), -15, 15), -250.0, 60.0
        if self.vacate and self.phase == "ROLLOUT" and ac.on_ground and ac.ias_kt < VACATE_KT:
            self.done = True                                     # turned off onto a taxiway
        if self.phase == "ROLLOUT" and ac.ias_kt < 1.0:
            self.phase = "STOPPED"
        return steer, 0.0, 0.0                                   # brake to a stop, hold

    def targets(self, ac: Aircraft, env: Env, now: float) -> tuple[float, float, float]:
        p = self.p
        pos = p.to_enu(ac.lat, ac.lon)
        if self.pending:
            self._react(ac, now)
        if self.phase in ("ROLLOUT", "STOPPED", "TAKEOFF"):
            return self._ground(ac, pos)
        if self.man is not None:                                  # flying what the node advised
            until, bank, vs = self.man
            if now < until:
                if vs < 0 and ac.agl_ft < 700:
                    vs = 0.0
                return bank, vs, max(LEG_IAS.get(self.leg, 85) + self.ias_bias, 75.0)
            self.man = None
        if self.phase == "GO_AROUND":
            R = p.legs["RUNWAY"]
            along, xtrk = R.project(pos)
            if along > p.runway_len_m:                            # past the far end: the circuit again
                self.leg, self.phase, self.ga_offset = "UPWIND", "PATTERN", 0.0
            else:
                # sidestep (if advised) out to 300 m, then parallel the runway
                hold = math.copysign(300.0, self.ga_offset) if self.ga_offset else 0.0
                want = R.brg - clamp((xtrk - hold) * XTRK_GAIN, -abs(self.ga_offset) or -35, abs(self.ga_offset) or 35)
                return self._steer_to(ac, want), 9_999, LEG_IAS["UPWIND"] + self.ias_bias
        if self.phase == "INBOUND":
            x, y, alt = self.route[0]
            de, dn = x - pos[0], y - pos[1]
            if math.hypot(de, dn) < 700:
                self.route.pop(0)
                if not self.route:
                    self.phase = "JOIN"
            else:
                vs = clamp(6.0 * (alt - ac.indicated_ft), -700, 9_999)
                return self._steer_to(ac, math.degrees(math.atan2(de, dn)) % 360), vs, INBOUND_IAS + self.ias_bias
        if self.phase == "TRANSIT":
            trk, alt = self.transit
            if pos[0] * math.sin(math.radians(trk)) + pos[1] * math.cos(math.radians(trk)) > 2.0 * NM_M:
                self.phase = "JOIN"                               # 2 NM past the field: join the pattern
            else:
                return self._steer_to(ac, trk), clamp(6.0 * (alt - ac.indicated_ft), -800, 9_999), 95.0
        if self.phase == "DEPART":
            hdg, alt = self.depart
            R = p.legs["RUNWAY"]
            along, xtrk = R.project(pos)
            if math.hypot(*pos) > DEPART_EXIT_NM * NM_M:
                self.done = True
            if along < p.runway_len_m + 800 or ac.agl_ft < 700:   # straight out to 700 ft and past the end
                want = R.brg - clamp(xtrk * XTRK_GAIN, -35, 35)
            else:
                want = hdg
            return self._steer_to(ac, want), clamp(6.0 * (alt - ac.indicated_ft), -500, 9_999), 85.0 + self.ias_bias
        if self.phase == "LEVEL":
            if abs(ac.bank_ctl_deg) < 3 and abs(ac.vs_fpm) < 150:      # held bank, not turbulence roll
                self._choose_join(ac, pos)
            else:
                return 0.0, 0.0, clamp(ac.ias_kt, *LEVEL_IAS)
        if self.phase == "JOIN":
            D = p.legs["DOWNWIND"]
            ue, un = math.sin(math.radians(D.brg)), math.cos(math.radians(D.brg))
            entry = (D.a[0] + ue * D.length * JOIN_ENTRY_FRAC, D.a[1] + un * D.length * JOIN_ENTRY_FRAC)
            de, dn = entry[0] - pos[0], entry[1] - pos[1]
            v = max(ac.gs_kt, 40) * KT
            r = v * v / (G * math.tan(math.radians(self.bank)))
            if math.hypot(de, dn) < 1.5 * r:                     # close enough: roll onto downwind
                self.leg, self.phase = "DOWNWIND", "PATTERN"
            else:
                vs = clamp(6.0 * (p.tpa_ft - ac.indicated_ft), -800, 9_999)      # TPA on the altimeter
                return self._steer_to(ac, math.degrees(math.atan2(de, dn)) % 360), vs, LEG_IAS["DOWNWIND"] + self.ias_bias

        L = self.leg_geom(self.leg)
        along, xtrk = L.project(pos)

        # leg sequencing: lead the turn by r*tan(dpsi/2) plus the distance flown while rolling in
        # (radius in the air mass uses true airspeed)
        nxt = p.next_leg(self.leg)
        dpsi = abs(wrap180(p.legs[nxt].brg - L.brg))
        v = max(tas_kt(ac.ias_kt, env.da_at(ac.alt_msl_ft)), 40) * KT
        r = v * v / (G * math.tan(math.radians(self.bank)))
        lead = r * math.tan(math.radians(min(dpsi, 150) / 2))
        if dpsi > 5:
            lead += v * (self.bank / BANK_RATE_DPS) / 2
        ext_m = self.extend_s * max(ac.gs_kt, 40) * KT if self.leg == "DOWNWIND" else 0.0
        if L.length + ext_m - along <= lead:
            if self.full_stop and self.leg in ("FINAL", "STRAIGHT_IN"):
                self.leg, self.phase = "RUNWAY", "ROLLOUT"        # full stop instead of touch-and-go
                return self._ground(ac, pos)
            if ext_m > 0:                                         # extended downwind: base moves out with it
                B = p.legs["BASE"]
                ue, un = math.sin(math.radians(L.brg)), math.cos(math.radians(L.brg))
                self.over["BASE"] = Leg("BASE", (B.a[0] + ue * ext_m, B.a[1] + un * ext_m),
                                        (B.b[0] + ue * ext_m, B.b[1] + un * ext_m), B.agl0_ft, B.agl1_ft)
                self.extend_s = 0.0
            if nxt == "UPWIND":
                self.over.clear()
            self.leg = nxt
            if nxt in ("FINAL", "STRAIGHT_IN"):
                self.touched = False
                if self.circuits_left is not None:
                    self.circuits_left -= 1
                    if self.circuits_left <= 0:
                        self.full_stop = True                     # last circuit: land
            L = self.leg_geom(self.leg)
            along, xtrk = L.project(pos)

        # lateral: desired ground track = leg bearing corrected for cross-track error
        short_final = (self.leg in ("FINAL", "STRAIGHT_IN") and ac.agl_ft < 400) or (self.leg == "UPWIND" and along < 0)
        gain = XTRK_GAIN * (3.0 if short_final else 1.0)           # tighter on short final: land on the centreline
        want_trk = L.brg - clamp(xtrk * gain, -35, 35)
        bank = clamp((2.5 if short_final else 1.6) * wrap180(want_trk - ac.track_deg), -self.bank, self.bank)

        # vertical: follow the leg's AGL profile, feed-forward the descent on descending legs
        ground = ac.alt_msl_ft - ac.agl_ft
        if self.leg in ("CROSSWIND", "DOWNWIND", "BASE"):
            # flown by the altimeter: published altitudes, read with this aircraft's setting
            tgt_alt, cur = p.elev_ft + L.agl_at(along), ac.indicated_ft
        else:
            # upwind / final are flown visually against the ground
            # final aims ~200 m past the threshold (crosses it at ~50 ft), like a real approach
            aim = AIM_POINT_M if self.leg in ("FINAL", "STRAIGHT_IN") else 0.0
            tgt_alt, cur = ground + L.agl_at(along - aim), ac.alt_msl_ft
        vs = 6.0 * (tgt_alt - cur) - (ac.gs_kt * 101.27 * L.slope if along >= 0 else 0.0)   # level until the leg starts
        if self.leg == "UPWIND" and along < 0:
            # over the runway after final: flare onto the wheels, roll, then climb (touch-and-go)
            on_runway_m = along + p.runway_len_m
            self.touched = self.touched or ac.on_ground
            if not self.touched:
                vs = -150.0 - 5.0 * ac.agl_ft
            else:
                vs = 0.0 if ac.agl_ft < 5 and on_runway_m < GROUND_ROLL_M else 9_999
        elif self.leg in ("UPWIND", "CROSSWIND") and cur < tgt_alt - 50:
            vs = 9_999                                             # best climb (capped by DA)
        if self.leg in ("FINAL", "STRAIGHT_IN") and ac.agl_ft < 40:
            vs = max(vs, -150.0 - 5.0 * ac.agl_ft)                # flare: ~-350 fpm at 40 ft to -150 at the wheels

        ias = LEG_IAS[self.leg] + self.ias_bias
        if now < self.slow_until:                                  # SEQUENCE: slow and space
            ias = max(65.0, ias - 15.0)
        return bank, vs, ias


# ====================================================================================================
# Live traffic: AI aircraft that come and go
# ====================================================================================================
MISSIONS = {"arrival_45": 0.35, "straight_in": 0.15, "departure": 0.25, "circuits": 0.25}
SPAWN_RING_NM = (5.5, 6.5)        # arrivals appear this far out ...
SPAWN_CLEAR_NM, SPAWN_CLEAR_FT = 1.5, 1000.0   # ... with nobody this close
FINAL_CLEAR_NM = 1.5              # departures wait while someone is on final inside this
MAX_AGE_S = 1500.0                # a stuck AI is removed after 25 min
LOST_NM = 9.0

def _unit(brg: float) -> tuple[float, float]:
    return math.sin(math.radians(brg)), math.cos(math.radians(brg))

class TrafficGenerator:
    """Spawns and removes AI traffic. The world calls step(now) about once a sim second and handles what it
    returns (spawned ids, removed ids): nodes, events. Everything random comes from one seeded RNG, so a
    fixed seed replays the same spawns (live_seed - real ADS-B positions - is the exception)."""

    def __init__(self, world, cfg: dict, seed: int | None = None):
        self.w = world
        self.seed = int(cfg.get("seed", 7) if seed is None else seed)
        self.rng = random.Random(self.seed)
        self.lo, self.hi = (int(v) for v in cfg.get("airborne", [5, 8]))
        self.mix = {**MISSIONS, **cfg.get("mix", {})}
        self.runways: dict[str, float] = world.traffic_runways        # e.g. {"07R": 0.75, "07L": 0.25}
        self.n_initial = int(cfg.get("initial", self.lo))
        self.live_seed = bool(cfg.get("live_seed", False))
        self.live: dict | None = None                                 # last LIVE_TRAFFIC frame
        self.mirrored: set[str] = set()
        self.used: set[str] = set(world.fleet)
        self.active: dict[str, dict] = {}                             # id -> {"mission", "born", "rwy", "live"}
        self.next_t: float | None = None
        self.queued: tuple[str, str, int] | None = None
        self.counts = {k: 0 for k in MISSIONS}
        self.counts["live"] = 0

    # ---------- bookkeeping ----------
    def airborne(self) -> int:
        return sum(1 for i in self.active if i in self.w.fleet and not self.w.fleet[i].on_ground)

    def describe(self, ac_id: str) -> dict:
        a = self.active.get(ac_id, {})
        return {"mission": a.get("mission"), "runway": a.get("rwy"), "live": a.get("live")}

    def step(self, now: float) -> tuple[list[str], list[str]]:
        spawned, removed = [], []
        if self.next_t is None:
            spawned += self._initial(now)
            self.next_t = now + self.rng.uniform(15.0, 30.0)
        for i in list(self.active):
            ac = self.w.fleet.get(i)
            if ac is None:
                self.active.pop(i)
                continue
            x, y = P.to_enu(ac.lat, ac.lon)
            gone = (ac.autopilot.done or (ac.on_ground and ac.surface == "OFF" and ac.ias_kt < 1.0)
                    or now - self.active[i]["born"] > MAX_AGE_S or math.hypot(x, y) > LOST_NM * NM_M)
            if gone:
                self.w.remove_traffic(i)
                self.active.pop(i)
                removed.append(i)
        self._runway_watch()
        self._see_and_avoid(now)
        if now >= self.next_t:
            got = self._spawn_one(now) if len(self.active) < self.hi else None
            if got:
                spawned.append(got)
            n = len(self.active)
            if got is None and n < self.hi:
                self.next_t = now + 5.0                               # airspace busy: try again shortly
            else:
                self.next_t = now + (self.rng.uniform(5.0, 12.0) if self.airborne() < self.lo else
                                     self.rng.uniform(30.0, 70.0) if n < self.hi else 15.0)
        return spawned, removed

    def clear_near(self, x: float, y: float, alt_ft: float, r_m: float, dz_ft: float) -> list[str]:
        """Remove AI traffic near a point (demo reset: nothing sitting on a judge's start)."""
        out = []
        for i in list(self.active):
            ac = self.w.fleet.get(i)
            if ac is None:
                continue
            ax, ay = P.to_enu(ac.lat, ac.lon)
            if math.hypot(ax - x, ay - y) < r_m and abs(ac.alt_msl_ft - alt_ft) < dz_ft:
                self.w.remove_traffic(i)
                self.active.pop(i)
                out.append(i)
        return out

    # ---------- spawning ----------
    def _new_id(self) -> str:
        while True:
            i = f"N{self.rng.randint(200, 989)}"
            if i not in self.used and i not in self.w.fleet:
                self.used.add(i)
                return i

    def _pick(self, table: dict[str, float]) -> str:
        r, acc = self.rng.random() * sum(table.values()), 0.0
        for k, v in table.items():
            acc += v
            if r <= acc:
                return k
        return k

    def _clear(self, x: float, y: float, alt_ft: float) -> bool:
        for ac in self.w.fleet.values():
            ax, ay = P.to_enu(ac.lat, ac.lon)
            if math.hypot(ax - x, ay - y) < SPAWN_CLEAR_NM * NM_M and abs(ac.alt_msl_ft - alt_ft) < SPAWN_CLEAR_FT:
                return False
        return True

    def _runway_free(self, pat: Pattern) -> bool:
        """Nobody on this runway, lifting off ahead, or on final inside FINAL_CLEAR_NM."""
        R = pat.legs["RUNWAY"]
        for ac in self.w.fleet.values():
            along, xtrk = R.project(P.to_enu(ac.lat, ac.lon))
            if abs(xtrk) > 150:                                                             # KDVT parallels are 214 m apart
                continue
            if -FINAL_CLEAR_NM * NM_M < along < 0 and ac.agl_ft < 900:                      # on final
                return False
            if 0 <= along < pat.runway_len_m + 600 and ac.agl_ft < 400:                     # on / just off the runway
                return False
        return True

    def _inbound_spaced(self, rwy: str, dist_m: float, point: tuple[float, float]) -> bool:
        """Arrivals to the same 45-degree point: at least 2 NM apart in distance to go."""
        for i, a in self.active.items():
            ac = self.w.fleet.get(i)
            if ac is None or a["rwy"] != rwy or ac.autopilot.phase not in ("INBOUND", "JOIN"):
                continue
            x, y = P.to_enu(ac.lat, ac.lon)
            if abs(math.hypot(point[0] - x, point[1] - y) - dist_m) < 2.0 * NM_M:
                return False
        return True

    def _pilot(self, ac_id: str, pat: Pattern, leg: str) -> PatternPilot:
        pl = PatternPilot(pat, leg, ac_id)
        pl.rng = random.Random(f"{self.seed}:{ac_id}")            # advice compliance replays with the seed
        pl.vacate = True
        return pl

    def _add(self, ac_id: str, mission: str, rwy: str, now: float, lat: float, lon: float, alt: float, hdg: float,
             ias: float, pilot: PatternPilot, live: str | None = None) -> str:
        self.w.add_traffic(ac_id, lat, lon, alt, hdg, ias, pilot, ap_equipped=self.rng.random() < 0.5)
        self.active[ac_id] = {"mission": mission, "born": now, "rwy": rwy, "live": live}
        self.counts[mission] += 1
        if live:
            self.counts["live"] += 1
        return ac_id

    def _runway_watch(self) -> None:
        """Landing AI below 300 ft go around if the runway ahead is still occupied (what a real pilot does)."""
        for i in self.active:
            ac = self.w.fleet.get(i)
            pl = ac.autopilot if ac else None
            if pl is None or pl.phase != "PATTERN" or pl.leg not in ("FINAL", "STRAIGHT_IN") or ac.agl_ft > 300:
                continue
            R = pl.p.legs["RUNWAY"]
            for o in self.w.fleet.values():
                if o is ac or o.agl_ft > 60:
                    continue
                along, xtrk = R.project(P.to_enu(o.lat, o.lon))
                if -30 < along < pl.p.runway_len_m and abs(xtrk) < 60 and (o.ias_kt > VACATE_KT or not o.on_ground):
                    pl.go_around()
                    break

    def _see_and_avoid(self, now: float) -> None:
        """Last resort, what any pilot does without FLOCK: traffic ahead inside 0.5 NM that will pass within
        150 m / 300 ft in the next 30 s -> turn right 25 deg for a while (14 CFR 91.113), or go around if on
        final. (Parallel finals 214 m apart never trigger it.)"""
        pts = {i: (P.to_enu(a.lat, a.lon), _unit(a.track_deg), a.gs_kt * KT) for i, a in self.w.fleet.items()
               if not a.on_ground}
        for i in self.active:
            ac = self.w.fleet.get(i)
            if ac is None or i not in pts or ac.autopilot.man is not None or ac.cmd is not None:
                continue
            pl = ac.autopilot
            (x, y), (ue, un), v = pts[i]
            for j, ((ox, oy), (oe, on), ov) in pts.items():
                o = self.w.fleet[j]
                if j == i or abs(o.alt_msl_ft - ac.alt_msl_ft) > 600:
                    continue
                dx, dy = ox - x, oy - y
                r = math.hypot(dx, dy)
                if r > 0.5 * NM_M or r < 1.0 or (dx * ue + dy * un) / r < math.cos(math.radians(70)):
                    continue                                          # not close, or not ahead (can't see it)
                rvx, rvy = oe * ov - ue * v, on * ov - un * v
                vv = rvx * rvx + rvy * rvy
                tc = clamp(-(dx * rvx + dy * rvy) / vv, 0.0, 30.0) if vv > 1e-6 else 0.0
                miss = math.hypot(dx + rvx * tc, dy + rvy * tc)
                dz = (o.alt_msl_ft - ac.alt_msl_ft) + (o.vs_fpm - ac.vs_fpm) / 60.0 * tc
                if miss > 150.0 or abs(dz) > 300.0:
                    continue
                if pl.phase == "PATTERN" and pl.leg in ("FINAL", "STRAIGHT_IN"):
                    pl.go_around(20.0)
                else:
                    pl.man = (now + 10.0, 25.0, 0.0)
                pl.advice_log.append({"t": round(now, 1), "level": "VISUAL", "text": f"traffic {j} ahead", "follow": True})
                break

    def _spawn_one(self, now: float) -> str | None:
        if self.queued:                                              # a departure waiting for the runway
            mission, rwy, tries = self.queued
            got = getattr(self, "_" + mission)(rwy, now)
            if got or tries >= 12:
                self.queued = None
                got = got or self._arrival_45(rwy, now)
            else:
                self.queued = (mission, rwy, tries + 1)
        else:
            mission, rwy = self._pick(self.mix), self._pick(self.runways)
            got = getattr(self, "_" + mission)(rwy, now)
            if got is None and mission in ("departure", "circuits"):
                self.queued = (mission, rwy, 1)                      # holding short: retry every 5 s, ~60 s max
        if got is None and self.airborne() < self.lo:                # short of traffic: anything that fits now
            for alt in ("arrival_45", "straight_in"):
                for r in sorted(self.runways, key=lambda k: -self.runways[k]):
                    got = getattr(self, "_" + alt)(r, now)
                    if got:
                        return got
        return got

    def _arrival_45(self, rwy: str, now: float) -> str | None:
        """From a random direction on the pattern side to the 45-degree entry point, then downwind."""
        pat = self.w.pattern(rwy)
        D, C = pat.legs["DOWNWIND"], pat.legs["CROSSWIND"]
        dw, out = _unit(D.brg), _unit(C.brg)
        frac = D.length * JOIN_ENTRY_FRAC
        entry = (D.a[0] + dw[0] * frac, D.a[1] + dw[1] * frac)
        w45 = (entry[0] - dw[0] * 1500 + out[0] * 1500, entry[1] - dw[1] * 1500 + out[1] * 1500)
        live = self._live_candidate() if self.live_seed else None
        ac_id = None
        if live is not None:
            x, y = P.to_enu(live["lat"], live["lon"])
            alt = clamp(float(live["alt_msl_ft"]), pat.tpa_ft, pat.tpa_ft + 2500)
            cs = live.get("callsign") or ""
            if re.fullmatch(r"N\d{2,5}", cs) and cs not in self.used and cs not in self.w.fleet:
                ac_id = cs
        else:
            brg = math.degrees(math.atan2(*out)) + self.rng.uniform(-100.0, 100.0)
            d = self.rng.uniform(*SPAWN_RING_NM) * NM_M
            x, y = d * math.sin(math.radians(brg)), d * math.cos(math.radians(brg))
            alt = pat.tpa_ft + self.rng.choice([500.0, 1000.0, 1500.0])
        if not self._clear(x, y, alt) or not self._inbound_spaced(rwy, math.hypot(w45[0] - x, w45[1] - y), w45):
            return None
        if live is not None:
            self.mirrored.add(live.get("callsign") or live["id"])
        if ac_id:
            self.used.add(ac_id)
        else:
            ac_id = self._new_id()
        pl = self._pilot(ac_id, pat, "DOWNWIND")
        pl.phase, pl.route, pl.full_stop = "INBOUND", [(w45[0], w45[1], pat.tpa_ft)], True
        lat, lon = P.from_enu(x, y)
        hdg = math.degrees(math.atan2(w45[0] - x, w45[1] - y)) % 360
        return self._add(ac_id, "arrival_45", rwy, now, lat, lon, alt, hdg, INBOUND_IAS + pl.ias_bias, pl,
                         live=(live.get("callsign") or live["id"]) if live else None)

    def _straight_in(self, rwy: str, now: float) -> str | None:
        pat = self.w.pattern(rwy)
        S = pat.legs["STRAIGHT_IN"]
        ue, un = _unit(S.brg)
        back = self.rng.uniform(1500.0, 4500.0)                    # before the 3.8 NM straight-in fix
        x, y = S.a[0] - ue * back, S.a[1] - un * back
        alt = pat.elev_ft + S.agl0_ft
        if not self._clear(x, y, alt):
            return None
        ac_id = self._new_id()
        pl = self._pilot(ac_id, pat, "STRAIGHT_IN")
        pl.full_stop, pl.touched = True, False
        lat, lon = P.from_enu(x, y)
        return self._add(ac_id, "straight_in", rwy, now, lat, lon, alt, S.brg, LEG_IAS["STRAIGHT_IN"] + pl.ias_bias, pl)

    def _takeoff(self, rwy: str, now: float, mission: str) -> str | None:
        pat = self.w.pattern(rwy)
        if not self._runway_free(pat):
            return None
        ac_id = self._new_id()
        pl = self._pilot(ac_id, pat, "RUNWAY")
        pl.phase = "TAKEOFF"
        if mission == "departure":
            R, C = pat.legs["RUNWAY"], pat.legs["CROSSWIND"]
            turn = 45.0 if wrap180(C.brg - R.brg) > 0 else -45.0   # 45-degree turn out on the pattern side ...
            pl.depart = ((R.brg + self.rng.choice([0.0, turn])) % 360, pat.tpa_ft + 1000)   # ... or straight out
        else:
            pl.circuits_left = self.rng.randint(1, 3)
        pos = self.w.runway_start(pat, 2.0)
        return self._add(ac_id, mission, rwy, now, pos["lat"], pos["lon"], pos["alt_msl_ft"], pos["hdg_deg"], 0.0, pl)

    def _departure(self, rwy: str, now: float) -> str | None:
        return self._takeoff(rwy, now, "departure")

    def _circuits(self, rwy: str, now: float) -> str | None:
        return self._takeoff(rwy, now, "circuits")

    def _initial(self, now: float) -> list[str]:
        """Start with traffic already flying: circuits spaced around the main pattern, one on the other
        runway, an arrival on its way to the 45."""
        main = max(self.runways, key=self.runways.get)
        other = [r for r in self.runways if r != main]
        plan = [(main, "UPWIND", 10.0), (main, "UPWIND", 95.0), (main, "UPWIND", 180.0)]
        if other:
            plan.append((other[0], "DOWNWIND", 30.0))
        out = []
        for rwy, leg, off in plan[: max(0, self.n_initial - 1)]:
            pat = self.w.pattern(rwy)
            pos = P.place(leg, rwy, off, 90.0)
            ac_id = self._new_id()
            pl = self._pilot(ac_id, pat, pos["leg"])
            pl.circuits_left = self.rng.randint(1, 2)
            out.append(self._add(ac_id, "circuits", rwy, now, pos["lat"], pos["lon"], pos["alt_msl_ft"], pos["hdg_deg"],
                                 LEG_IAS[pos["leg"]] + pl.ias_bias, pl))
        if self.n_initial > len(out):
            got = self._arrival_45(main, now)
            if got:
                out.append(got)
        return out

    def _live_candidate(self) -> dict | None:
        """A real aircraft from the last LIVE_TRAFFIC frame that could be inbound: airborne, 3-12 NM out,
        not too high, not mirrored yet."""
        ac = (self.live or {}).get("aircraft") or []
        cands = [a for a in ac if not a.get("on_ground") and 3.0 <= float(a.get("dist_nm", 0)) <= 12.0
                 and float(a.get("alt_msl_ft", 0)) < P.ELEV_FT + 6000
                 and (a.get("callsign") or a.get("id")) not in self.mirrored]
        if not cands or self.rng.random() > 0.6:
            return None
        cands.sort(key=lambda a: str(a.get("id", "")))
        return self.rng.choice(cands)
