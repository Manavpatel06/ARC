"""
node/airwitness.py — FLOCK AirWitness: camera-free anti-spoofing trust engine (Lane B, Reya).

Rule: every radio message is a CLAIM, not truth.  A broadcast may create an advisory track; it can only take
part in coordination or trigger automatic maneuvering once independent evidence supports it.  Low trust never
makes a target disappear: it makes its claims distrusted and its uncertainty (Threat Tube) larger.

Evidence, per target (all deterministic; randomness only in challenge nonces):
  signature      Ed25519 from radio/client.py ("_auth": ok | unsigned | unknown_key); legacy/unsigned is NOT rejected
  freshness      claimed time vs receive time, +-2 s
  replay         duplicate (seq, t) or a sequence number going backwards -> packet ignored, REPLAY flagged
  challenge      signed liveness: we put a nonce in our HEARTBEAT ("c"), the peer echoes it in its HEARTBEAT ("r")
  kinematics     broad GA limits (speed, acceleration, turn rate, climb, teleport)
  continuity     residual of each new position against the track's own dead-reckoning
  intent         a MANEUVER_COMMIT (R / L / CLIMB / DESCEND) must show up in the following STATEs
  witnesses      peer corroboration from radio/evidence.py (signed HEARTBEAT neighbour reports)
  rf             RFMeasurement: range (RSSI or ranging), bearing, TDOA, Doppler vs the CLAIMED position
  duplicate id   one identity at two places at once
  sybil          several identities that the RF says come from one transmitter

Trust is not a weighted average: HARD GATES first (unsigned / replay can never be VERIFIED; replay, duplicate
identity, sybil, impossible kinematics, RF position conflict -> SUSPICIOUS minimum; two independent hard
failures -> QUARANTINED), then soft evidence moves the score.

  VERIFIED     may negotiate (MANEUVER_COMMIT / SEQ), may drive RESOLVE and an automatic TAKEOVER
  UNVERIFIED   tracked, displayed, warned about, conservative tube (sigma x1.5); advice to OUR pilot only;
               no coordination, no automatic takeover        (e.g. legacy ADS-B, or a new peer still being checked)
  SUSPICIOUS   tracked + TRAFFIC warning only, claimed intent ignored, physical-envelope tube (sigma x2.5)
  QUARANTINED  display only (radar draws it hollow red); every claim ignored; never acted on

Also: OwnshipPNT checks our own GNSS against heading/speed/bank dead-reckoning and baro altitude; on a jump it
does NOT drop GPS, it grows our own position uncertainty and reports DEGRADED.

Pure logic, no I/O.  node/node.py feeds it envelopes and OWNSHIP; tests in node/tests/test_airwitness.py.
"""
from __future__ import annotations

import collections
import math
import secrets
from dataclasses import dataclass, field
from typing import Callable, Optional

VERIFIED, UNVERIFIED, SUSPICIOUS, QUARANTINED = "VERIFIED", "UNVERIFIED", "SUSPICIOUS", "QUARANTINED"
WIRE_STATE = {VERIFIED: "TRUSTED", UNVERIFIED: "SUSPICIOUS", SUSPICIOUS: "SUSPICIOUS", QUARANTINED: "FAKE"}
LEVEL_CAP = {VERIFIED: "TAKEOVER", UNVERIFIED: "RESOLVE", SUSPICIOUS: "TRAFFIC", QUARANTINED: None}
SIGMA_SCALE = {VERIFIED: 1.0, UNVERIFIED: 1.5, SUSPICIOUS: 2.5, QUARANTINED: 4.0}
ACTION = {VERIFIED: "coordination + automatic avoidance allowed",
          UNVERIFIED: "track + warn + conservative tube; no coordination, no automatic takeover",
          SUSPICIOUS: "track + warn only; claimed intent ignored; no coordination; no automatic takeover",
          QUARANTINED: "display only; all claims ignored; never acted on"}

KT = 0.514444
FT = 0.3048
C = 299_792_458.0
# broad GA limits: a legitimate unusual maneuver must not trip them
MAX_SPEED_KT = 250.0
MAX_ACCEL_KT_S = 12.0
MAX_TURN_DPS = 15.0
MAX_VS_FPM = 3000.0
JUMP_M = 400.0                    # position vs own dead-reckoning over one STATE period (real aircraft: ~50 m)
FRESH_S = 2.0
REPLAY_WINDOW = 32
FLAG_HOLD_S = 20.0
CHALLENGE_EVERY_S = 10.0
CHALLENGE_TIMEOUT_S = 5.0
CHALLENGE_VALID_S = 25.0
DUP_ID_M = 800.0
RF_Z_CONFLICT = 4.0
RF_Z_STRONG = 6.0
UPGRADE_HOLD_S = 2.0
NEAR_GROUND_FT = 1700.0           # KDVT field 1,478 ft + ~200 ft
POPIN_M = 2000.0                  # a target first heard this close, already airborne, "popped into existence"
POPIN_MIN_ALT_FT = 2000.0         # (KDVT field 1,478 ft: anything above ~500 ft AGL is airborne)
BARO_GEO_SOFT_FT = 250.0          # same air mass: (GNSS alt - baro alt) of a real nearby aircraft matches ours
BARO_GEO_HARD_FT = 400.0
NEG_ANSWER_S = 6.0                # a live FLOCK peer answers our MANEUVER_COMMIT / SEQ_PROPOSE within this


@dataclass
class RFMeasurement:
    """One radio-physics observation of a transmission.  Today it comes from the channel emulator (RSSI, Doppler)
    or harness/rf_sim.py (ranging, bearing, TDOA); real RF hardware fills the same fields later."""
    observer_id: str
    target_id: str
    t: float
    observer_pos: tuple                      # (x, y, z) metres ENU of the receiver at t
    estimated_range_m: Optional[float] = None
    range_sigma_m: Optional[float] = None
    range_log_sigma_db: Optional[float] = None   # RSSI-derived ranges are log-normal: sigma in dB
    bearing_deg: Optional[float] = None
    bearing_sigma_deg: Optional[float] = None
    tdoa_ns: Optional[float] = None          # (range to observer - range to ref) / c
    tdoa_sigma_ns: Optional[float] = None
    ref_pos: Optional[tuple] = None
    doppler_hz: Optional[float] = None
    doppler_sigma_hz: Optional[float] = None
    observer_vel: Optional[tuple] = None
    source_id: Optional[str] = None          # simulated emitter fingerprint (sybil prototype only)


@dataclass
class TrustResult:
    target: str
    score: float
    state: str
    checks: dict = field(default_factory=dict)    # check name -> "PASS" / "FAIL ..." / "n/a" / text
    reasons: list = field(default_factory=list)
    gates: list = field(default_factory=list)
    sigma_extra: float = 1.0                          # extra tube width from soft evidence (e.g. intent mismatch)

    @property
    def wire_state(self) -> str:
        return WIRE_STATE[self.state]

    @property
    def action(self) -> str:
        return ACTION[self.state]

    @property
    def may_coordinate(self) -> bool:
        return self.state == VERIFIED

    @property
    def cap(self) -> Optional[str]:
        return LEVEL_CAP[self.state]

    @property
    def sigma_scale(self) -> float:
        return SIGMA_SCALE[self.state] * self.sigma_extra

    @property
    def use_claimed_intent(self) -> bool:
        return self.state in (VERIFIED, UNVERIFIED)

    def evidence_strings(self) -> list[str]:
        """Compact form for TRUST.targets[].evidence (the cockpit badge / explain panel)."""
        out = [f"aw:{self.state}"]
        for k in ("signature", "freshness", "replay", "challenge", "negotiation", "kinematics", "continuity", "intent",
                  "baro_geo", "pop_in", "witnesses", "rf_range", "rf_bearing", "tdoa", "doppler", "duplicate_id", "sybil"):
            v = self.checks.get(k)
            if v is not None and v != "n/a":
                out.append(f"{k}:{v}")
        out.append(f"action:{self.action}")
        return out

    def explain(self) -> str:
        lines = [f"TARGET: {self.target}", f"Trust state: {self.state}", f"Trust score: {self.score:.2f}", "Checks:"]
        lines += [f"  {k:13s} {v}" for k, v in self.checks.items()]
        if self.gates:
            lines.append("Hard gates: " + "; ".join(self.gates))
        lines.append("Action: " + self.action.upper())
        return "\n".join(lines)


@dataclass
class _Track:
    states: collections.deque = field(default_factory=lambda: collections.deque(maxlen=12))  # (t, rx, x, y, alt_ft, gs, trk, vs)
    seen: collections.deque = field(default_factory=lambda: collections.deque(maxlen=64))    # (seq, t)
    last_seq: int = -1
    last_t: float = -1e18
    auth: Optional[str] = None
    first_rx: float = 0.0
    last_rx: float = 0.0
    flags: dict = field(default_factory=dict)          # name -> last time seen
    residuals: collections.deque = field(default_factory=lambda: collections.deque(maxlen=8))
    intents: list = field(default_factory=list)        # [start, end, sense, good, bad, judged]
    intent_bad_t: float = -1e9
    intent_bad_n: int = 0
    chal_pending: dict = field(default_factory=dict)   # nonce -> sent time
    chal_ok_t: float = -1e9
    chal_misses: int = 0
    chal_last: float = -1e9
    pop_in: Optional[str] = None
    baro_geo: collections.deque = field(default_factory=lambda: collections.deque(maxlen=8))   # |offset diff| ft
    neg_sent: Optional[float] = None
    neg_ok_t: float = -1e9
    neg_latency: float = 0.0
    neg_miss: int = 0
    rf: collections.deque = field(default_factory=lambda: collections.deque(maxlen=20))      # (RFMeasurement, z dict)
    state: str = UNVERIFIED
    upgrade_since: Optional[float] = None


class OwnshipPNT:
    """GNSS consistency for OUR aircraft: GNSS position vs dead-reckoning from heading/speed/bank, GNSS altitude vs baro."""

    def __init__(self, to_enu: Callable):
        self.to_enu = to_enu
        self.prev: Optional[dict] = None
        self.extra_sigma_m = 0.0
        self.degraded_until = -1e9
        self.reasons: list[str] = []

    @property
    def state(self) -> str:
        return "DEGRADED" if self.extra_sigma_m > 1.0 else "NORMAL"

    def on_ownship(self, own: dict) -> None:
        t = float(own["t"])
        x, y = self.to_enu(own["lat"], own["lon"])
        cur = {"t": t, "x": x, "y": y, "gs": own["gs_kt"] * KT, "trk": own["track_deg"],
               "geo_minus_baro": own["alt_msl_ft"] - own.get("alt_press_ft", own["alt_msl_ft"])}
        p = self.prev
        self.prev = cur
        if p is None:
            return
        dt = t - p["t"]
        self.extra_sigma_m *= max(0.0, 1.0 - dt / 30.0)           # an old inconsistency fades over ~30 s
        if dt <= 0 or dt > 3.0:
            return
        r = math.radians(p["trk"])
        px, py = p["x"] + math.sin(r) * p["gs"] * dt, p["y"] + math.cos(r) * p["gs"] * dt
        res = math.hypot(x - px, y - py)
        allow = 40.0 + 0.5 * p["gs"] * dt                      # turns, wind, GPS noise
        if res > allow:
            self.extra_sigma_m = max(self.extra_sigma_m, 50.0 + res)
            self.reasons = [f"GNSS jump {res:.0f} m with no matching motion (dead-reckoning allows {allow:.0f} m)"]
        dalt = abs(cur["geo_minus_baro"] - p["geo_minus_baro"])
        if dalt > 150.0:
            self.extra_sigma_m = max(self.extra_sigma_m, 60.0)
            self.reasons = [f"GNSS altitude moved {dalt:.0f} ft against baro"]


class AirWitness:
    def __init__(self, own_id: str, to_enu: Callable, signed_radio: bool = True, nonce_fn: Callable = None):
        self.own_id = own_id
        self.to_enu = to_enu
        self.signed_radio = signed_radio        # False: the transport carries no signatures (sim/stub): signature n/a
        self.tracks: dict[str, _Track] = collections.defaultdict(_Track)
        self.peer_evidence: Optional[Callable[[str], tuple]] = None   # radio/evidence.TrustEvidence.assess
        self.own_pos: Optional[tuple] = None    # (x, y, z_m)
        self.own_vel: Optional[tuple] = None
        self._respond: dict[str, int] = {}
        self._nonce = nonce_fn or (lambda: secrets.randbelow(10 ** 9))
        self.pnt = OwnshipPNT(to_enu)
        self.own_geo_minus_baro: Optional[float] = None

    # ------------------------------------------------------------------ inputs
    def on_ownship(self, own: dict) -> None:
        x, y = self.to_enu(own["lat"], own["lon"])
        self.own_pos = (x, y, own.get("alt_press_ft", own["alt_msl_ft"]) * FT)
        r = math.radians(own["track_deg"])
        v = own["gs_kt"] * KT
        self.own_vel = (math.sin(r) * v, math.cos(r) * v, own.get("vs_fpm", 0.0) * FT / 60.0)
        self.own_geo_minus_baro = own["alt_msl_ft"] - own.get("alt_press_ft", own["alt_msl_ft"])
        self.pnt.on_ownship(own)

    def on_message(self, env: dict, now: float) -> bool:
        """Ingest one envelope.  Returns False if the node must NOT use it (replay / duplicate / stale)."""
        frm = env.get("from") or env.get("from_")
        if not frm or frm == self.own_id:
            return False
        tr = self.tracks[frm]
        rx = float(env.get("_rx_t", now))
        if not tr.first_rx:
            tr.first_rx = rx
        auth = env.get("_auth")
        tr.auth = auth if self.signed_radio else "n/a"
        seq, t_claim = int(env.get("seq", 0)), float(env.get("t", rx))
        if self.signed_radio and abs(rx - t_claim) > FRESH_S:
            tr.flags["stale"] = now
            return False
        # anti-replay sliding window (as in IPsec): radios reorder packets, so an older sequence number is fine
        # while it is new to us, recent, and inside the window; a duplicate or anything outside the window is not
        if any(s == seq for s, _ in tr.seen):
            tr.flags["replay"] = now                          # this sequence number was already delivered
            return False
        if tr.last_seq >= 0 and (seq < tr.last_seq - REPLAY_WINDOW or t_claim < tr.last_t - FRESH_S):
            tr.flags["replay"] = now                          # far behind the newest packet: recorded / re-transmitted
            return False
        tr.seen.append((seq, round(t_claim, 3)))
        tr.last_seq = max(tr.last_seq, seq)
        tr.last_t = max(tr.last_t, t_claim)
        tr.last_rx = rx
        msg, b = env.get("msg"), env.get("body", {}) or {}
        if msg == "STATE":
            self._state(frm, tr, t_claim, rx, b, now)
            rfm = env.get("_rf")
            if rfm and self.own_pos is not None:
                self.ingest_rf(self._rssi_measurement(frm, t_claim, rfm))
        elif msg == "HEARTBEAT":
            w = b.get("w") if isinstance(b.get("w"), dict) else {}
            if w and tr.auth in ("ok", "n/a") and tr.states and tr.state == VERIFIED:
                # collective radio location: a verified peer tells us how far (by its own RSSI) each target is from
                # IT.  Several receivers = software multilateration of the transmitter, no second antenna needed.
                for tgt, rng in list(w.items())[:6]:
                    if tgt in (self.own_id, frm) or tgt not in self.tracks:
                        continue
                    pos = self._claimed_at(tr, t_claim)
                    if pos is not None:
                        self.ingest_rf(RFMeasurement(frm, tgt, t_claim, pos[:3], estimated_range_m=float(rng),
                                                     range_log_sigma_db=3.0))
            c = b.get("c") if isinstance(b.get("c"), dict) else {}
            if self.own_id in c:
                self._respond[frm] = int(c[self.own_id])
            r = b.get("r") if isinstance(b.get("r"), dict) else {}
            if self.own_id in r:
                n = int(r[self.own_id])
                if n in tr.chal_pending and (tr.auth in ("ok", "n/a")):
                    tr.chal_pending.pop(n, None)
                    tr.chal_ok_t, tr.chal_misses = now, 0
        elif msg == "SEQ_ACCEPT":
            self._answered(tr, now)
        elif msg == "MANEUVER_COMMIT":
            if b.get("target") == self.own_id:
                self._answered(tr, now)
            sense = b.get("sense")
            if sense in ("L", "R", "CLIMB", "DESCEND"):
                st = float(b.get("start_t", t_claim))
                tr.intents.append([st + 3.0, st + float(b.get("hold_s", 10.0)), sense, 0, 0, False])
                tr.intents = tr.intents[-4:]
        return True

    def note_sent(self, msg: str, body: dict, now: float) -> None:
        """Our own negotiation messages: a live FLOCK peer must answer them (negotiation as liveness evidence)."""
        ids = []
        if msg == "MANEUVER_COMMIT":
            ids = [body.get("target")]
        elif msg == "SEQ_PROPOSE":
            ids = [i for i in body.get("order", []) if i != self.own_id]
        for i in ids:
            if i in self.tracks and self.tracks[i].neg_sent is None:
                self.tracks[i].neg_sent = now

    def _answered(self, tr: "_Track", now: float) -> None:
        if tr.neg_sent is not None:
            tr.neg_latency = now - tr.neg_sent
            tr.neg_ok_t, tr.neg_sent, tr.neg_miss = now, None, 0

    def witness_report(self, now: float, n: int = 4) -> dict:
        """{target: our RSSI-derived range m} for targets we measured in the last 3 s, nearest first (HEARTBEAT "w")."""
        out = []
        for tid, tr in self.tracks.items():
            mine = [m for m, _ in tr.rf if m.observer_id == self.own_id and m.estimated_range_m and now - m.t < 3.0]
            if mine:
                out.append((mine[-1].estimated_range_m, tid))
        return {tid: int(round(r, -1)) for r, tid in sorted(out)[:n]}

    def ingest_rf(self, m: RFMeasurement) -> None:
        tr = self.tracks[m.target_id]
        z = self._rf_z(tr, m)
        if z is not None:
            tr.rf.append((m, z))

    def tick(self, now: float) -> dict:
        """Extra HEARTBEAT fields to send now: {"c": {peer: nonce}, "r": {peer: nonce}} (may be empty)."""
        out_c, out_r = {}, dict(self._respond)
        self._respond.clear()
        for pid, tr in self.tracks.items():
            if tr.neg_sent is not None and now - tr.neg_sent > NEG_ANSWER_S:
                tr.neg_sent = None
                if now - tr.last_rx < 3.0:                    # it is talking, but not to our negotiation
                    tr.neg_miss += 1
            for n, ts in list(tr.chal_pending.items()):
                if now - ts > CHALLENGE_TIMEOUT_S:
                    tr.chal_pending.pop(n)
                    tr.chal_misses += 1                       # could just be packet loss: costs confidence only
            heard = now - tr.last_rx < 3.0
            due = now - tr.chal_last >= CHALLENGE_EVERY_S and now - tr.chal_ok_t > CHALLENGE_EVERY_S
            if heard and due and not tr.chal_pending and len(out_c) < 4:
                n = int(self._nonce())
                tr.chal_pending[n], tr.chal_last = now, now
                out_c[pid] = n
        out = {}
        if out_c:
            out["c"] = out_c
        if out_r:
            out["r"] = out_r
        return out

    # ------------------------------------------------------------------ per-check helpers
    def _state(self, tid: str, tr: _Track, t: float, rx: float, b: dict, now: float) -> None:
        x, y = self.to_enu(b["lat"], b["lon"])
        cur = (t, rx, x, y, float(b.get("alt_press_ft", 0.0)), float(b.get("gs_kt", 0.0)),
               float(b.get("track_deg", 0.0)), float(b.get("vs_fpm", 0.0)))
        if cur[5] > MAX_SPEED_KT:
            tr.flags["speed"] = now
        near_ground = cur[4] < NEAR_GROUND_FT                # flare / touchdown / takeoff: altitude checks unreliable
        if abs(cur[7]) > MAX_VS_FPM and not near_ground:
            tr.flags["climb_rate"] = now
        if tr.states:
            p = tr.states[-1]
            dt = t - p[0]
            if 0 < dt <= 6.0:
                dte = max(dt, 1.0)
                r = math.radians(p[6])
                ex, ey = p[2] + math.sin(r) * p[5] * KT * dt, p[3] + math.cos(r) * p[5] * KT * dt
                res = math.hypot(x - ex, y - ey)
                tr.residuals.append(res)
                if res > JUMP_M + 0.15 * p[5] * KT * dt:
                    tr.flags["position_jump"] = now
                if abs(cur[5] - p[5]) / dte > MAX_ACCEL_KT_S:
                    tr.flags["accel"] = now
                if abs(((cur[6] - p[6] + 540) % 360) - 180) / dte > MAX_TURN_DPS:
                    tr.flags["turn_rate"] = now
                if abs(cur[4] - p[4]) / dte * 60.0 > MAX_VS_FPM * 1.3 and not near_ground and p[4] >= NEAR_GROUND_FT:
                    tr.flags["altitude_jump"] = now
                # intent: compare the claimed maneuver with what the track actually does
                trk_rate = (((cur[6] - p[6] + 540) % 360) - 180) / dte
                for it in tr.intents:
                    if it[0] <= t <= it[1]:
                        # consistent = flying it; inconsistent = flying the OPPOSITE way.  Not acting (yet) is
                        # neither: a pilot may ignore advice and an autopilot acts later, at takeover.
                        good = {"R": trk_rate > 1.0, "L": trk_rate < -1.0, "CLIMB": cur[7] > 150.0,
                                "DESCEND": cur[7] < -150.0}[it[2]]
                        bad = {"R": trk_rate < -1.0, "L": trk_rate > 1.0, "CLIMB": cur[7] < -150.0,
                               "DESCEND": cur[7] > 150.0}[it[2]]
                        if good:
                            it[3] += 1
                        elif bad:
                            it[4] += 1
                    elif t > it[1] and not it[5]:
                        it[5] = True
                        if it[4] > it[3] and it[4] >= 2:
                            tr.intent_bad_n += 1
                            tr.intent_bad_t = now
        if not tr.states and self.own_pos is not None and tr.pop_in is None:
            rng0 = math.hypot(x - self.own_pos[0], y - self.own_pos[1])
            if rng0 < POPIN_M and cur[4] > POPIN_MIN_ALT_FT:
                tr.pop_in = f"appeared {rng0 / 1000:.1f} km away, already airborne"
        geo = b.get("alt_geo_ft")
        if geo is not None and self.own_geo_minus_baro is not None:
            tr.baro_geo.append(abs((float(geo) - cur[4]) - self.own_geo_minus_baro))
        tr.states.append(cur)
        # one identity at two places: dead-reckon the last few seconds of claims to now and look for 2 clusters
        recent = [s for s in tr.states if t - s[0] <= 6.0]
        if len(recent) >= 4:
            pts = []
            for s in recent:
                r = math.radians(s[6])
                pts.append((s[2] + math.sin(r) * s[5] * KT * (t - s[0]), s[3] + math.cos(r) * s[5] * KT * (t - s[0])))
            a = pts[-1]
            far = [p for p in pts if math.hypot(p[0] - a[0], p[1] - a[1]) > DUP_ID_M]
            near = [p for p in pts if math.hypot(p[0] - a[0], p[1] - a[1]) <= DUP_ID_M]
            if len(far) >= 2 and len(near) >= 2:
                tr.flags["duplicate_identity"] = now

    def _claimed_at(self, tr: _Track, t: float) -> Optional[tuple]:
        if not tr.states:
            return None
        s = tr.states[-1]
        dt = max(-3.0, min(3.0, t - s[0]))
        r = math.radians(s[6])
        v = s[5] * KT
        return (s[2] + math.sin(r) * v * dt, s[3] + math.cos(r) * v * dt, (s[4] + s[7] * dt / 60.0) * FT,
                (math.sin(r) * v, math.cos(r) * v, s[7] * FT / 60.0))

    def _rssi_measurement(self, tid: str, t: float, rfm: dict) -> RFMeasurement:
        try:
            from radio import rf as _rf
            fspl = _rf.PTX_DBM - float(rfm["rssi_dbm"])
            rng = 10 ** ((fspl - 20 * math.log10(_rf.FREQ_HZ) + 147.55) / 20.0)
            lsig = _rf.RSSI_SIGMA_DB
            dsig = _rf.DOPPLER_SIGMA_HZ
        except Exception:
            rng, lsig, dsig = None, 3.0, 3.0
        return RFMeasurement(self.own_id, tid, t, self.own_pos, estimated_range_m=rng, range_log_sigma_db=lsig,
                             doppler_hz=rfm.get("doppler_hz"), doppler_sigma_hz=dsig, observer_vel=self.own_vel)

    def _rf_z(self, tr: _Track, m: RFMeasurement) -> Optional[dict]:
        c = self._claimed_at(tr, m.t)
        if c is None or m.observer_pos is None:
            return None
        cx, cy, cz, cv = c
        ox, oy, oz = m.observer_pos
        dx, dy, dz = cx - ox, cy - oy, cz - oz
        rng = math.sqrt(dx * dx + dy * dy + dz * dz) or 1.0
        z = {}
        if m.estimated_range_m is not None:
            if m.range_log_sigma_db:
                z["range"] = abs(20 * math.log10(max(m.estimated_range_m, 1.0) / rng)) / m.range_log_sigma_db
            elif m.range_sigma_m:
                z["range"] = abs(m.estimated_range_m - rng) / m.range_sigma_m
            z["range_err_m"] = abs(m.estimated_range_m - rng)
        if m.bearing_deg is not None and m.bearing_sigma_deg:
            b = math.degrees(math.atan2(dx, dy)) % 360.0
            z["bearing"] = abs(((m.bearing_deg - b + 540) % 360) - 180) / m.bearing_sigma_deg
        if m.tdoa_ns is not None and m.ref_pos is not None and m.tdoa_sigma_ns:
            rx_, ry_, rz_ = m.ref_pos
            r_ref = math.sqrt((cx - rx_) ** 2 + (cy - ry_) ** 2 + (cz - rz_) ** 2)
            pred = (rng - r_ref) / C * 1e9
            z["tdoa"] = abs(m.tdoa_ns - pred) / m.tdoa_sigma_ns
        if m.doppler_hz is not None and m.doppler_sigma_hz:
            ov = m.observer_vel or (0.0, 0.0, 0.0)
            rdot = (dx * (cv[0] - ov[0]) + dy * (cv[1] - ov[1]) + dz * (cv[2] - ov[2])) / rng
            try:
                from radio import rf as _rf
                pred_dop = _rf.doppler_hz(rdot)
            except Exception:
                pred_dop = -915e6 * rdot / C
            z["doppler"] = abs(m.doppler_hz - pred_dop) / m.doppler_sigma_hz
        return z or None

    def _rf_verdicts(self, tr: _Track) -> dict:
        """Median z per kind over recent measurements -> 'PASS' / 'CONFLICT ...' / None."""
        out = {}
        for kind, label in (("range", "rf_range"), ("bearing", "rf_bearing"), ("tdoa", "tdoa"), ("doppler", "doppler")):
            zs = [z[kind] for _, z in tr.rf if kind in z]
            if len(zs) < 2:
                continue
            zs.sort()
            med = zs[len(zs) // 2]
            if len(zs) < 3 and med < RF_Z_STRONG + 2:          # two samples only decide an overwhelming conflict
                continue
            if med > RF_Z_CONFLICT:
                extra = ""
                if kind == "range":
                    errs = sorted(z.get("range_err_m", 0) for _, z in tr.rf if "range" in z)
                    extra = f" {errs[len(errs) // 2] / 1000:.1f} km"
                out[label] = (f"CONFLICT{extra} (z {med:.0f})", med)
            else:
                out[label] = ("PASS", med)
        return out

    def _sybil(self, tid: str, now: float) -> list[str]:
        """Other identities that look like the same transmitter: same simulated emitter id, or RSSI-derived ranges
        that track each other within 1.5 dB while their claimed positions are hundreds of metres apart."""
        tr = self.tracks.get(tid)
        if tr is None or not tr.rf:
            return []
        mine = [(m.t, m) for m, _ in tr.rf if now - m.t < 15.0]
        same = []
        for oid, otr in self.tracks.items():
            if oid == tid or not otr.rf:
                continue
            other = [(m.t, m) for m, _ in otr.rf if now - m.t < 15.0]
            if any(a.source_id and a.source_id == b.source_id for _, a in mine for _, b in other):
                same.append(oid)
                continue
            pairs = [(a, b) for ta, a in mine for tb, b in other if abs(ta - tb) < 0.6
                     and a.observer_id == b.observer_id and a.estimated_range_m and b.estimated_range_m]
            if len(pairs) >= 3:
                d = sorted(abs(20 * math.log10(a.estimated_range_m / b.estimated_range_m)) for a, b in pairs)
                ca, cb = self._claimed_at(tr, now), self._claimed_at(otr, now)
                apart = ca is not None and cb is not None and math.hypot(ca[0] - cb[0], ca[1] - cb[1]) > 250.0
                rf_bad = any(z.get("range", 0) > RF_Z_CONFLICT for _, z in tr.rf)
                if d[len(d) // 2] < 1.5 and apart and rf_bad:
                    same.append(oid)
        return sorted(same)

    def _witness(self, tid: str) -> tuple[int, list, list, list]:
        if self.peer_evidence is None:
            return 0, [], [], []
        try:
            _, ev = self.peer_evidence(tid)
        except Exception:
            return 0, [], [], []
        n, ref, silent, other = 0, [], [], []
        for e in ev:
            if e.startswith("corroborated:"):
                n = int(e.split(":")[1])
            elif e.startswith("refuted_by:"):
                ref = e.split(":")[1].split(",")
            elif e.startswith("not_heard_by:"):
                silent = e.split(":")[1].split(",")
            elif e.startswith("rf_") or e.startswith("kinematics_violation"):
                other.append(e)
        return n, ref, silent, other

    # ------------------------------------------------------------------ verdict
    def assess(self, tid: str, now: float) -> TrustResult:
        tr = self.tracks.get(tid)
        if tr is None or not tr.states:
            return TrustResult(tid, 0.5, UNVERIFIED, {"data": "no position yet"}, ["no data"], [])
        ck, reasons, gates = {}, [], []
        sigma_extra = 1.0
        strikes = 0
        score = 1.0
        active = {k for k, t in tr.flags.items() if now - t < FLAG_HOLD_S}

        # signature (hard: unsigned/unknown can never be VERIFIED; it is NOT rejected — legacy traffic stays)
        if tr.auth in ("ok",):
            ck["signature"] = "PASS"
        elif tr.auth in (None, "n/a"):
            ck["signature"] = "n/a" if not self.signed_radio else "FAIL (none)"
        else:
            ck["signature"] = f"FAIL ({tr.auth})"
        sig_fail = ck["signature"].startswith("FAIL")
        if sig_fail:
            score = min(score, 0.55)
            strikes += 1
            gates.append("unsigned/unknown key -> cannot be VERIFIED")

        ck["freshness"] = "FAIL (stale packets)" if "stale" in active else "PASS"
        if "stale" in active:
            score -= 0.2
        # A replay is always dropped.  If the real owner of the identity still answers a fresh signed challenge,
        # the replay is somebody else's attack on it: record it, but do not let an attacker demote a live peer.
        replay = "replay" in active and not (now - tr.chal_ok_t < CHALLENGE_VALID_S and tr.auth == "ok")
        if "replay" in active and not replay:
            ck["replay"] = "BLOCKED (replayed packets dropped; live peer confirmed by challenge)"
        else:
            ck["replay"] = "DETECTED" if replay else "PASS"
        if replay:
            strikes += 1
            gates.append("replay -> cannot be VERIFIED, SUSPICIOUS minimum")

        # challenge-response liveness (loss is possible: a miss only costs confidence)
        if now - tr.chal_ok_t < CHALLENGE_VALID_S:
            ck["challenge"] = "PASS"
        elif tr.chal_misses:
            ck["challenge"] = f"NO RESPONSE x{tr.chal_misses}"
            score -= min(0.15, 0.05 * tr.chal_misses)       # packet loss is likely: a light cost only
        else:
            ck["challenge"] = "PENDING" if tr.chal_pending else "n/a"

        kin = sorted(active & {"speed", "accel", "turn_rate", "climb_rate", "position_jump", "altitude_jump"})
        n_w, refuters, silent, other = self._witness(tid)
        kin_lane_c = [e.split(":", 1)[1] for e in other if e.startswith("kinematics_violation")]
        kin_all = sorted(set(kin) | set(kin_lane_c))
        if kin_all:
            ck["kinematics"] = "FAIL (" + ", ".join(kin_all) + ")"
            score -= 0.35 * min(len(kin_all), 2)
            strikes += 1
            gates.append("impossible kinematics -> SUSPICIOUS minimum")
        else:
            ck["kinematics"] = "PASS" if len(tr.states) >= 2 else "n/a"

        if tr.residuals:
            mean_res = sum(tr.residuals) / len(tr.residuals)
            cont = max(0.0, min(1.0, 1.0 - mean_res / 300.0))
            ck["continuity"] = f"{cont:.2f}"
            score -= 0.3 * (1.0 - cont)
        else:
            ck["continuity"] = "n/a"

        if now - tr.intent_bad_t < 60.0 and tr.intent_bad_n:
            ck["intent"] = f"INCONSISTENT x{tr.intent_bad_n}"
            # a pilot who does not fly the advice is not a spoofer: this widens the target's tube, it does not
            # by itself change its trust state (and never demotes a peer in the middle of a conflict)
            sigma_extra = 1.0 + min(0.5, 0.2 * tr.intent_bad_n)
        elif any(it[5] for it in tr.intents):
            ck["intent"] = "PASS"
        else:
            ck["intent"] = "n/a"

        if self.peer_evidence is not None:
            w = f"{n_w} corroborating"
            if refuters:
                w += f", refuted by {','.join(refuters)}"
            if silent:
                w += f", not heard by {','.join(silent)}"
            ck["witnesses"] = w
            score += 0.05 * min(n_w, 2)
            if refuters and n_w < 2:
                score -= min(0.5, 0.25 * len(refuters))
            if silent and n_w < 2:
                score -= min(0.3, (0.1 if not sig_fail else 0.2) * len(silent))

        rfv = self._rf_verdicts(tr)
        rf_conflict = False
        for label in ("rf_range", "rf_bearing", "tdoa", "doppler"):
            if label in rfv:
                ck[label] = rfv[label][0]
                if rfv[label][0].startswith("CONFLICT") and label != "doppler":
                    rf_conflict = True
        lane_c_rf_bad = [e for e in other if "mismatch" in e]
        if lane_c_rf_bad and "rf_range" not in rfv:
            ck["rf_range"] = "CONFLICT (" + ", ".join(lane_c_rf_bad) + ")"
            rf_conflict = True
        dop_strong = "doppler" in rfv and rfv["doppler"][1] > 3 * RF_Z_STRONG
        if "doppler" in rfv and rfv["doppler"][0].startswith("CONFLICT"):
            score -= 0.1                                          # Doppler is supporting evidence only
        if rf_conflict:
            score -= 0.35
            strikes += 1
            gates.append("claimed position contradicted by radio physics -> SUSPICIOUS minimum")

        if "duplicate_identity" in active:
            ck["duplicate_id"] = "DETECTED"
            strikes += 1
            gates.append("one identity in two places -> SUSPICIOUS minimum")
        else:
            ck["duplicate_id"] = "PASS"
        if tr.pop_in:
            ck["pop_in"] = tr.pop_in
            score -= 0.15
        if len(tr.baro_geo) >= 2:
            bg = sorted(tr.baro_geo)[len(tr.baro_geo) // 2]
            if bg > BARO_GEO_HARD_FT:
                ck["baro_geo"] = f"FAIL (GNSS-baro offset differs from ours by {bg:.0f} ft)"
                strikes += 1
                gates.append("altitude claim inconsistent with the local atmosphere -> SUSPICIOUS minimum")
            elif bg > BARO_GEO_SOFT_FT:
                ck["baro_geo"] = f"MARGINAL ({bg:.0f} ft)"
                score -= 0.2
            else:
                ck["baro_geo"] = "PASS"
        if now - tr.neg_ok_t < 60.0:
            ck["negotiation"] = f"PASS (answered in {tr.neg_latency:.1f} s)"
        elif tr.neg_miss:
            ck["negotiation"] = f"NO ANSWER x{tr.neg_miss}"
            score -= min(0.2, 0.1 * tr.neg_miss)
        syb = self._sybil(tid, now)
        if syb:
            ck["sybil"] = "SAME TRANSMITTER AS " + ",".join(syb)
            strikes += 1
            gates.append("sybil cluster -> SUSPICIOUS minimum")

        if dop_strong and strikes >= 1:
            # never decides alone, but an overwhelming Doppler contradiction confirms another hard failure
            strikes += 1
            gates.append("Doppler contradicts the claimed motion and confirms another failure")
        score = round(max(0.0, min(1.0, score)), 2)
        positive = (ck.get("challenge") == "PASS" or n_w >= 1 or rfv.get("rf_range", ("", 9))[0] == "PASS"
                    or ck.get("negotiation", "").startswith("PASS")
                    or (not self.signed_radio and len(tr.states) >= 3))
        if strikes >= 2:
            state = QUARANTINED
            gates.append(f"{strikes} independent hard failures -> QUARANTINED")
        elif (kin_all or rf_conflict or replay or "duplicate_identity" in active or syb
              or (sig_fail and n_w == 0 and (refuters or len(silent) >= 2 or tr.pop_in))
              or ck.get("baro_geo", "").startswith("FAIL")):   # unsigned and nobody signed hears it
            state = SUSPICIOUS
        elif sig_fail or replay or score < 0.7 or not positive:
            state = UNVERIFIED
            if not positive and not sig_fail:
                reasons.append("no independent confirmation yet (challenge / witness / RF)")
        else:
            state = VERIFIED

        # hysteresis: downgrades are immediate, an upgrade to VERIFIED must hold for UPGRADE_HOLD_S
        order = {QUARANTINED: 0, SUSPICIOUS: 1, UNVERIFIED: 2, VERIFIED: 3}
        if order[state] > order[tr.state]:
            if tr.upgrade_since is None:
                tr.upgrade_since = now
            if now - tr.upgrade_since < UPGRADE_HOLD_S:
                state = tr.state
            else:
                tr.upgrade_since = None
        else:
            tr.upgrade_since = None
        tr.state = state
        reasons = [f"{k}: {v}" for k, v in ck.items() if v not in ("PASS", "n/a")] + reasons
        return TrustResult(tid, score, state, ck, reasons, gates, sigma_extra)
