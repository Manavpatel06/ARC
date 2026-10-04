"""
node/airwitness.py — ARC AirWitness-Hybrid: PASSIVE, camera-free anti-spoofing (Lane B, Reya).

Every received broadcast is a CLAIM.  Collision avoidance starts on it immediately; AirWitness evaluates it in
parallel and only ever changes what the claim is ALLOWED to do (trust state, Threat Tube width, authority).
Nothing here sends a packet, asks a question, or waits for an answer:

                     TARGET BROADCAST
                            |
                FAST PASSIVE PACKET GUARD  (signature / session / sequence / replay / duplicate / freshness)
                            |
              +-------------+--------------+
              v                            v
       COLLISION ENGINE             AIRWITNESS-HYBRID (2 Hz)
       runs on the claim     flight physics | independent surveillance | RF + peers
                             (motion, trajectory, intent, identity) (TCAS, Mode-S, 1030/1090)
                             (Doppler trend, RSSI trend, RF location, peer witness digests)
                                            |
                              P(REAL) / P(SPOOF) / P(FAULTY)  + hard gates
                                            |
                      VERIFIED / UNVERIFIED / SUSPICIOUS / QUARANTINED -> Verified CLOF / Shadow CLOF

Evidence sources are modular.  A source that has nothing to say returns UNKNOWN, never FAIL: missing TCAS, missing
Mode-S, no witnesses, no RF are not evidence of spoofing.  Absence is weak; positive contradiction is strong.

Inference: a likelihood update over three hypotheses (REAL, SPOOF, FAULTY_OR_UNVERIFIED), one factor per evidence
source, then hard security gates that override it.  Not a flat weighted average.

Pure logic, no I/O.  node/node.py feeds envelopes, OWNSHIP and optional surveillance; tests in
node/tests/test_airwitness.py; attacks in harness/redarc_spoof.py; simulated surveillance in harness/surveillance_sim.py.
"""
from __future__ import annotations

import collections
import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

# ------------------------------------------------------------------ public states and what they may do
VERIFIED, UNVERIFIED, SUSPICIOUS, QUARANTINED = "VERIFIED", "UNVERIFIED", "SUSPICIOUS", "QUARANTINED"
WIRE_STATE = {VERIFIED: "TRUSTED", UNVERIFIED: "SUSPICIOUS", SUSPICIOUS: "SUSPICIOUS", QUARANTINED: "FAKE"}
LEVEL_CAP = {VERIFIED: "TAKEOVER", UNVERIFIED: "RESOLVE", SUSPICIOUS: "TRAFFIC", QUARANTINED: None}
SIGMA_SCALE = {VERIFIED: 1.0, UNVERIFIED: 1.5, SUSPICIOUS: 2.5, QUARANTINED: 4.0}
ACTION = {VERIFIED: "verified CLOF: coordination + automatic avoidance allowed",
          UNVERIFIED: "shadow CLOF: warn + conservative tube; no negotiation, no 4D contract, no takeover from its data",
          SUSPICIOUS: "shadow CLOF: warn only; claimed intent ignored; physical envelope; no negotiation, no takeover",
          QUARANTINED: "identity and claims distrusted; display only unless an independent sensor confirms a hazard"}

# ------------------------------------------------------------------ configuration (all thresholds live here)
PRIOR = (0.90, 0.05, 0.05)                 # P(REAL), P(SPOOF), P(FAULTY) before any evidence
P_SPOOF_QUARANTINE = 0.85
P_SPOOF_SUSPICIOUS = 0.45
P_REAL_VERIFIED = 0.85
VERIFY_MIN_TRACK_S = 3.0                   # a VERIFIED peer needs at least this much consistent, signed track
UPGRADE_HOLD_S = 2.0                       # upgrades must hold this long; downgrades are immediate

KT = 0.514444
FT = 0.3048
C = 299_792_458.0
MAX_SPEED_KT = 250.0                       # broad GA limits: legitimate emergency maneuvers must pass
MAX_ACCEL_KT_S = 12.0
MAX_TURN_DPS = 15.0
MAX_VS_FPM = 3000.0
MAX_VS_CHANGE_FPM_S = 3000.0               # ~1.5 g change: an abrupt push-over / go-around must still pass
JUMP_M = 400.0
NEAR_GROUND_FT = 1700.0                    # KDVT field 1,478 ft + ~200 ft: altitude checks unreliable below
FRESH_S = 2.0
REPLAY_WINDOW = 32
SEQ_JUMP_MAX = 100                         # a live sender cannot skip this many numbers between packets
SESSION_CHURN_S = 60.0                     # three new session ids inside this window = appear/vanish spoofing
FLAG_HOLD_S = 20.0
DUP_ID_M = 800.0
POPIN_M = 2000.0
POPIN_MIN_ALT_FT = 2000.0
BARO_GEO_SOFT_FT = 250.0
BARO_GEO_HARD_FT = 400.0
NEG_ANSWER_S = 6.0
RF_Z_CONFLICT = 4.0
RF_Z_STRONG = 6.0
TCAS_Z_CONFLICT = 4.0
TCAS_FRESH_S = 6.0
MODES_FRESH_S = 10.0
IREP_FRESH_S = 20.0

PASS, WEAK, CONFLICT, UNKNOWN = "PASS", "WEAK", "CONFLICT", "UNKNOWN"

# P(evidence | REAL), P(evidence | SPOOF), P(evidence | FAULTY) for each source and verdict.  UNKNOWN is always 1:1:1.
LIK = {
    "signature":    {PASS: (0.95, 0.05, 0.80), CONFLICT: (0.30, 0.90, 0.60)},
    "packet":       {CONFLICT: (0.20, 0.80, 0.40)},            # replay / duplicate / stale / expired session seen
    "motion":       {PASS: (0.98, 0.60, 0.80), CONFLICT: (0.01, 0.60, 0.40)},
    "trajectory":   {PASS: (0.90, 0.50, 0.60), WEAK: (0.50, 0.50, 0.60), CONFLICT: (0.10, 0.60, 0.50)},
    "intent":       {PASS: (0.80, 0.40, 0.50), CONFLICT: (0.40, 0.60, 0.70)},
    "identity":     {CONFLICT: (0.02, 0.70, 0.40)},            # one id in two places
    "sybil":        {CONFLICT: (0.02, 0.90, 0.20)},
    "pop_in":       {CONFLICT: (0.30, 0.70, 0.50)},
    "baro_geo":     {PASS: (0.90, 0.40, 0.60), WEAK: (0.40, 0.60, 0.60), CONFLICT: (0.05, 0.70, 0.50)},
    "tcas":         {PASS: (0.90, 0.03, 0.50), WEAK: (0.60, 0.40, 0.60), CONFLICT: (0.02, 0.90, 0.30)},
    "mode_s":       {PASS: (0.80, 0.20, 0.60)},
    "interrogation": {PASS: (0.85, 0.10, 0.50)},
    "rf_location":  {PASS: (0.80, 0.30, 0.60), CONFLICT: (0.05, 0.80, 0.30)},
    "doppler_trend": {PASS: (0.80, 0.30, 0.60), WEAK: (0.55, 0.45, 0.55), CONFLICT: (0.08, 0.75, 0.40)},
    "rssi_trend":   {PASS: (0.60, 0.40, 0.50), CONFLICT: (0.35, 0.60, 0.50)},      # low weight on purpose
    "witnesses":    {PASS: (0.90, 0.10, 0.50), CONFLICT: (0.05, 0.80, 0.40)},
    "multi_observer": {PASS: (0.85, 0.15, 0.50), CONFLICT: (0.05, 0.85, 0.35)},
    "negotiation":  {PASS: (0.85, 0.05, 0.40)},                # passive: only a positive is ever used
}


# ------------------------------------------------------------------ evidence input types (sim and hardware share them)
@dataclass
class RFMeasurement:
    """Radio physics of one transmission (channel emulator RSSI/Doppler today; ranging/AoA/TDOA hardware later)."""
    observer_id: str
    target_id: str
    t: float
    observer_pos: tuple
    estimated_range_m: Optional[float] = None
    range_sigma_m: Optional[float] = None
    range_log_sigma_db: Optional[float] = None
    bearing_deg: Optional[float] = None
    bearing_sigma_deg: Optional[float] = None
    tdoa_ns: Optional[float] = None
    tdoa_sigma_ns: Optional[float] = None
    ref_pos: Optional[tuple] = None
    doppler_hz: Optional[float] = None
    doppler_sigma_hz: Optional[float] = None
    observer_vel: Optional[tuple] = None
    source_id: Optional[str] = None
    rssi_dbm: Optional[float] = None


@dataclass
class TCASMeasurement:
    """Read-only surveillance the aircraft ALREADY has (ARC never interrogates).  target_id None = unassociated."""
    t: float
    range_m: float
    bearing_deg: Optional[float] = None
    relative_altitude_m: Optional[float] = None
    range_sigma_m: float = 60.0
    bearing_sigma_deg: float = 10.0
    alt_sigma_m: float = 30.0
    target_id: Optional[str] = None


@dataclass
class ModeSObservation:
    """Passive 1090 MHz activity associated with a target (address match / timing).  Supporting evidence only."""
    t: float
    target_id: str
    rssi_dbm: Optional[float] = None
    freq_offset_hz: Optional[float] = None


@dataclass
class InterrogationReplyEvidence:
    """A 1030 MHz interrogation (by someone else) followed by a compatible 1090 MHz reply.  We only listen."""
    interrogation_time: float
    reply_time: float
    target_id: Optional[str] = None
    confidence: float = 0.8


@dataclass
class Evidence:
    source: str
    verdict: str
    detail: str = ""

    @property
    def likelihood(self) -> tuple:
        return LIK.get(self.source, {}).get(self.verdict, (1.0, 1.0, 1.0))


@dataclass
class MotionEvidence:
    plausible: bool
    position_residual: float
    velocity_residual: float
    turn_residual: float
    vertical_residual: float
    score: float
    violations: list = field(default_factory=list)


@dataclass
class RFWaveEvidence:
    expected_radial_motion: str
    observed_frequency_trend: str
    doppler_consistency: float
    confidence: float


@dataclass
class MultiObserverEvidence:
    observer_count: int
    consistency_score: float
    contradictions: list
    confidence: float


@dataclass
class TrustResult:
    target: str
    score: float                                   # P(REAL)
    state: str
    checks: dict = field(default_factory=dict)     # source -> "PASS ..." / "CONFLICT ..." / "WEAK ..." / "UNKNOWN"
    reasons: list = field(default_factory=list)
    gates: list = field(default_factory=list)
    sigma_extra: float = 1.0
    probs: dict = field(default_factory=dict)      # {"real","spoof","faulty"}
    physical_support: bool = False                 # an independent sensor (TCAS / interrogation reply) sees it

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
        if self.state == QUARANTINED and self.physical_support:
            return "TRAFFIC"                       # identity distrusted, independently-seen hazard kept
        return LEVEL_CAP[self.state]

    @property
    def clof_layer(self) -> Optional[str]:
        if self.state == VERIFIED:
            return "verified"
        if self.state in (UNVERIFIED, SUSPICIOUS) or self.physical_support:
            return "shadow"
        return None

    @property
    def sigma_scale(self) -> float:
        return SIGMA_SCALE[self.state] * self.sigma_extra

    @property
    def use_claimed_intent(self) -> bool:
        return self.state in (VERIFIED, UNVERIFIED)

    @property
    def authority(self) -> dict:
        v = self.state == VERIFIED
        return {"traffic_warning": "YES" if self.cap else "NO",
                "shadow_CLOF": "YES" if self.clof_layer == "shadow" else "NO",
                "negotiation": "ALLOWED" if v else "BLOCKED",
                "4D_contract": "ALLOWED" if v else "BLOCKED",
                "auto_command_from_target": "ALLOWED" if v else "BLOCKED"}

    def evidence_strings(self) -> list[str]:
        out = [f"aw:{self.state}",
               f"p:real={self.probs.get('real', 0):.2f},spoof={self.probs.get('spoof', 0):.2f},"
               f"faulty={self.probs.get('faulty', 0):.2f}"]
        out += [f"{k}:{v}" for k, v in self.checks.items() if not v.startswith(UNKNOWN)]
        out.append("authority:" + ",".join(f"{k}={v}" for k, v in self.authority.items()))
        return out

    def explain(self) -> str:
        lines = [f"TARGET: {self.target}", "", f"STATE: {self.state}", "",
                 f"P(real):   {self.probs.get('real', 0):.2f}", f"P(spoof):  {self.probs.get('spoof', 0):.2f}",
                 f"P(faulty): {self.probs.get('faulty', 0):.2f}", "", "EVIDENCE"]
        lines += [f"{k + ':':18s}{v}" for k, v in self.checks.items()]
        if self.gates:
            lines += ["", "HARD GATES"] + [f"  {g}" for g in self.gates]
        lines += ["", "AUTHORITY"] + [f"{k + ':':26s}{v}" for k, v in self.authority.items()]
        return "\n".join(lines)


# ------------------------------------------------------------------ per-target state
@dataclass
class _Track:
    states: collections.deque = field(default_factory=lambda: collections.deque(maxlen=16))  # (t, rx, x, y, alt_ft, gs, trk, vs)
    seen: collections.deque = field(default_factory=lambda: collections.deque(maxlen=64))    # (sid, seq)
    hashes: collections.deque = field(default_factory=lambda: collections.deque(maxlen=64))
    sessions: dict = field(default_factory=dict)       # sid -> first-seen claimed time
    sid: Optional[str] = None
    last_seq: int = -1
    last_t: float = -1e18
    last_arrival: float = -1e18                        # local monotonic arrival
    auth: Optional[str] = None
    first_rx: float = 0.0
    last_rx: float = 0.0
    flags: dict = field(default_factory=dict)
    motion: Optional[MotionEvidence] = None
    traj_res: collections.deque = field(default_factory=lambda: collections.deque(maxlen=10))
    intents: list = field(default_factory=list)
    intent_bad_t: float = -1e9
    intent_bad_n: int = 0
    intent_ok_n: int = 0
    rf: collections.deque = field(default_factory=lambda: collections.deque(maxlen=24))       # (RFMeasurement, z)
    dop: collections.deque = field(default_factory=lambda: collections.deque(maxlen=16))      # (t, observed, expected)
    rssi: collections.deque = field(default_factory=lambda: collections.deque(maxlen=16))     # (t, rssi, claimed range)
    tcas: collections.deque = field(default_factory=lambda: collections.deque(maxlen=12))     # (TCASMeasurement, z dict)
    modes: collections.deque = field(default_factory=lambda: collections.deque(maxlen=12))
    ireps: collections.deque = field(default_factory=lambda: collections.deque(maxlen=12))
    digests: dict = field(default_factory=dict)        # observer -> (t, digest list, observer pos/vel)
    pop_in: Optional[str] = None
    baro_geo: collections.deque = field(default_factory=lambda: collections.deque(maxlen=8))
    neg_sent: Optional[float] = None
    neg_ok_t: float = -1e9
    neg_latency: float = 0.0
    state: str = UNVERIFIED
    upgrade_since: Optional[float] = None


class OwnshipPNT:
    """GNSS consistency for OUR aircraft: GNSS vs heading/speed dead-reckoning, GNSS altitude vs baro.  GPS is kept;
    on an inconsistency our own position uncertainty grows and the state reads DEGRADED."""

    def __init__(self, to_enu: Callable):
        self.to_enu = to_enu
        self.prev: Optional[dict] = None
        self.extra_sigma_m = 0.0
        self.reasons: list[str] = []

    @property
    def state(self) -> str:
        return "DEGRADED" if self.extra_sigma_m > 1.0 else "NORMAL"

    def on_ownship(self, own: dict) -> None:
        t = float(own["t"])
        x, y = self.to_enu(own["lat"], own["lon"])
        cur = {"t": t, "x": x, "y": y, "gs": own["gs_kt"] * KT, "trk": own["track_deg"],
               "geo_minus_baro": own["alt_msl_ft"] - own.get("alt_press_ft", own["alt_msl_ft"])}
        p, self.prev = self.prev, cur
        if p is None:
            return
        dt = t - p["t"]
        self.extra_sigma_m *= max(0.0, 1.0 - dt / 30.0)
        if dt <= 0 or dt > 3.0:
            return
        r = math.radians(p["trk"])
        res = math.hypot(x - (p["x"] + math.sin(r) * p["gs"] * dt), y - (p["y"] + math.cos(r) * p["gs"] * dt))
        allow = 40.0 + 0.5 * p["gs"] * dt
        if res > allow:
            self.extra_sigma_m = max(self.extra_sigma_m, 50.0 + res)
            self.reasons = [f"GNSS jump {res:.0f} m with no matching motion (dead-reckoning allows {allow:.0f} m)"]
        dalt = abs(cur["geo_minus_baro"] - p["geo_minus_baro"])
        if dalt > 150.0:
            self.extra_sigma_m = max(self.extra_sigma_m, 60.0)
            self.reasons = [f"GNSS altitude moved {dalt:.0f} ft against baro"]


class AirWitness:
    def __init__(self, own_id: str, to_enu: Callable, signed_radio: bool = True, clock: Callable = time.monotonic):
        self.own_id = own_id
        self.to_enu = to_enu
        self.signed_radio = signed_radio             # False: transport without signatures (sim/stub): signature UNKNOWN
        self.clock = clock                           # local monotonic arrival clock (not GPS time)
        self.tracks: dict[str, _Track] = collections.defaultdict(_Track)
        self.peer_evidence: Optional[Callable[[str], tuple]] = None    # radio/evidence.TrustEvidence.assess (Lane C)
        self.own_pos: Optional[tuple] = None
        self.own_vel: Optional[tuple] = None
        self.own_geo_minus_baro: Optional[float] = None
        self.pnt = OwnshipPNT(to_enu)
        self.stats = {"guard_us": collections.deque(maxlen=500), "assess_us": collections.deque(maxlen=500)}

    # ================================================================== inputs
    def on_ownship(self, own: dict) -> None:
        x, y = self.to_enu(own["lat"], own["lon"])
        self.own_pos = (x, y, own.get("alt_press_ft", own["alt_msl_ft"]) * FT)
        r = math.radians(own["track_deg"])
        v = own["gs_kt"] * KT
        self.own_vel = (math.sin(r) * v, math.cos(r) * v, own.get("vs_fpm", 0.0) * FT / 60.0)
        self.own_geo_minus_baro = own["alt_msl_ft"] - own.get("alt_press_ft", own["alt_msl_ft"])
        self.pnt.on_ownship(own)

    def on_message(self, env: dict, now: float) -> bool:
        """FAST PASSIVE PACKET GUARD + evidence intake.  Returns False when the packet must not update the track
        (replay / duplicate / stale / expired session).  Never sends anything, never waits."""
        t_start = time.perf_counter()
        try:
            return self._guard_and_ingest(env, now)
        finally:
            self.stats["guard_us"].append((time.perf_counter() - t_start) * 1e6)

    def add_tcas(self, m: TCASMeasurement) -> None:
        tid = m.target_id or self._associate(m)
        if tid is None:
            return
        tr = self.tracks.get(tid)
        if tr is None:
            return
        z = self._tcas_z(tr, m)
        if z is not None:
            tr.tcas.append((m, z))

    def add_mode_s(self, m: ModeSObservation) -> None:
        if m.target_id in self.tracks:
            self.tracks[m.target_id].modes.append(m)

    def add_interrogation_reply(self, m: InterrogationReplyEvidence) -> None:
        if m.target_id in self.tracks and 0.0 < m.reply_time - m.interrogation_time < 0.001:
            self.tracks[m.target_id].ireps.append(m)

    def ingest_rf(self, m: RFMeasurement) -> None:
        tr = self.tracks[m.target_id]
        z = self._rf_z(tr, m)
        if z is not None:
            tr.rf.append((m, z))

    def note_sent(self, msg: str, body: dict, now: float) -> None:
        """Our own normal negotiation traffic.  A peer's ordinary answer is positive evidence; no answer is NOT
        held against anyone (passive: we never wait on it)."""
        ids = [body.get("target")] if msg == "MANEUVER_COMMIT" else \
            [i for i in body.get("order", []) if i != self.own_id] if msg == "SEQ_PROPOSE" else []
        for i in ids:
            if i in self.tracks and self.tracks[i].neg_sent is None:
                self.tracks[i].neg_sent = now

    def witness_digest(self, now: float, n: int = 3) -> dict:
        """WITNESS_DIGEST piggybacked on our normal HEARTBEAT ("w"): what WE observe of up to n targets, unasked.
        {target: [age_s, rssi_range_m, radial_sign(+1 closing | -1 opening | 0), rssi_trend(-1|0|1)]}"""
        out = []
        for tid, tr in self.tracks.items():
            mine = [m for m, _ in tr.rf if m.observer_id == self.own_id and now - m.t < 3.0]
            if not mine:
                continue
            last = mine[-1]
            out.append((last.estimated_range_m or 1e9, tid, [round(now - last.t, 1),
                                                              int(round(last.estimated_range_m or 0, -1)),
                                                              self._radial_sign(tr),
                                                              self._trend_sign([r[1] for r in tr.rssi])]))
        return {tid: d for _, tid, d in sorted(out)[:n]}

    # ================================================================== packet guard
    def _guard_and_ingest(self, env: dict, now: float) -> bool:
        frm = env.get("from") or env.get("from_")
        if not frm or frm == self.own_id:
            return False
        tr = self.tracks[frm]
        rx = float(env.get("_rx_t", now))
        arrival = self.clock()
        if not tr.first_rx:
            tr.first_rx = rx
        tr.auth = env.get("_auth") if self.signed_radio else "n/a"
        seq, t_claim = int(env.get("seq", 0)), float(env.get("t", rx))
        body = env.get("body", {}) or {}
        sid = str(body.get("sid")) if body.get("sid") is not None else "-"
        # freshness against OUR receive time
        if self.signed_radio and abs(rx - t_claim) > FRESH_S:
            tr.flags["stale"] = now
            return False
        # exact duplicate (same bytes) -> drop
        h = hashlib.sha1(json.dumps([env.get("msg"), seq, round(t_claim, 4), body], sort_keys=True,
                                    default=str).encode()).hexdigest()
        if h in tr.hashes:
            tr.flags["duplicate"] = now
            return False
        # sessions: a new session id is a restart (fine); a packet from a session older than the current one is not
        if sid != "-":
            if sid not in tr.sessions:
                if tr.sid is not None and t_claim <= tr.last_t:
                    tr.flags["expired_session"] = now           # "new" session that is older than what we have
                    return False
                tr.sessions[sid] = t_claim
                if sum(1 for ts in tr.sessions.values() if t_claim - ts < SESSION_CHURN_S) >= 3:
                    tr.flags["session_churn"] = now              # a real node restarts rarely: appear/vanish spoofing
                if tr.sid is not None and tr.sid != sid:
                    tr.last_seq, tr.seen = -1, collections.deque(maxlen=64)
                tr.sid = sid
            elif sid != tr.sid:
                tr.flags["expired_session"] = now
                return False
        # sequence window (IPsec style): reordering is normal, re-use and far-behind are not
        if any(s == seq for s_id, s in tr.seen if s_id == sid):
            tr.flags["replay"] = now
            return False
        if tr.last_seq >= 0 and (seq < tr.last_seq - REPLAY_WINDOW or t_claim < tr.last_t - FRESH_S):
            tr.flags["replay"] = now
            return False
        if tr.last_seq >= 0 and seq > tr.last_seq + SEQ_JUMP_MAX and arrival - tr.last_arrival < 30.0:
            tr.flags["seq_jump"] = now                         # identity hijack attempt: kept, but blocks VERIFIED
        if arrival < tr.last_arrival:
            tr.flags["ordering"] = now                         # local clock went backwards: log only
        tr.seen.append((sid, seq))
        tr.hashes.append(h)
        tr.last_seq = max(tr.last_seq, seq)
        tr.last_t = max(tr.last_t, t_claim)
        tr.last_arrival = arrival
        tr.last_rx = rx
        msg = env.get("msg")
        if msg == "STATE":
            self._state(frm, tr, t_claim, b=body, now=now)
            rfm = env.get("_rf")
            if rfm and self.own_pos is not None:
                self._ingest_channel_rf(frm, tr, t_claim, rfm)
        elif msg == "HEARTBEAT":
            w = body.get("w") if isinstance(body.get("w"), dict) else {}
            if w and tr.auth in ("ok", "n/a") and tr.states and tr.state == VERIFIED:
                pos = self._claimed_at(tr, t_claim)
                if pos is not None:
                    for tgt, d in list(w.items())[:6]:
                        if tgt in (self.own_id, frm) or tgt not in self.tracks:
                            continue
                        self.tracks[tgt].digests[frm] = (t_claim, d, pos)
                        rng = d[1] if isinstance(d, list) and len(d) > 1 else d
                        if rng:
                            self.ingest_rf(RFMeasurement(frm, tgt, t_claim, pos[:3], estimated_range_m=float(rng),
                                                         range_log_sigma_db=3.0))
        elif msg == "SEQ_ACCEPT":
            self._answered(tr, now)
        elif msg == "MANEUVER_COMMIT":
            if body.get("target") == self.own_id:
                self._answered(tr, now)
            sense = body.get("sense")
            if sense in ("L", "R", "CLIMB", "DESCEND"):
                st = float(body.get("start_t", t_claim))
                tr.intents.append([st + 3.0, st + float(body.get("hold_s", 10.0)), sense, 0, 0, False])
                tr.intents = tr.intents[-4:]
        return True

    def _answered(self, tr: _Track, now: float) -> None:
        if tr.neg_sent is not None and now - tr.neg_sent <= NEG_ANSWER_S:
            tr.neg_latency = now - tr.neg_sent
            tr.neg_ok_t = now
        tr.neg_sent = None

    # ================================================================== flight physics source
    def _state(self, tid: str, tr: _Track, t: float, b: dict, now: float) -> None:
        x, y = self.to_enu(b["lat"], b["lon"])
        cur = (t, now, x, y, float(b.get("alt_press_ft", 0.0)), float(b.get("gs_kt", 0.0)),
               float(b.get("track_deg", 0.0)), float(b.get("vs_fpm", 0.0)))
        near_ground = cur[4] < NEAR_GROUND_FT
        viol = []
        pos_res = vel_res = turn_res = vert_res = 0.0
        if cur[5] > MAX_SPEED_KT:
            viol.append("speed")
        if abs(cur[7]) > MAX_VS_FPM and not near_ground:
            viol.append("climb_rate")
        if tr.states:
            p = tr.states[-1]
            dt = t - p[0]
            if 0 < dt <= 6.0:
                dte = max(dt, 1.0)
                r = math.radians(p[6])
                ex, ey = p[2] + math.sin(r) * p[5] * KT * dt, p[3] + math.cos(r) * p[5] * KT * dt
                pos_res = math.hypot(x - ex, y - ey)
                vel_res = abs(cur[5] - p[5]) / dte
                turn_res = abs(((cur[6] - p[6] + 540) % 360) - 180) / dte
                vert_res = abs(cur[7] - p[7]) / dte
                if pos_res > JUMP_M + 0.15 * p[5] * KT * dt:
                    viol.append("position_jump")
                if vel_res > MAX_ACCEL_KT_S:
                    viol.append("accel")
                if turn_res > MAX_TURN_DPS:
                    viol.append("turn_rate")
                if not near_ground and p[4] >= NEAR_GROUND_FT:
                    if abs(cur[4] - p[4]) / dte * 60.0 > MAX_VS_FPM * 1.3:
                        viol.append("altitude_jump")
                    if vert_res > MAX_VS_CHANGE_FPM_S:
                        viol.append("vertical_accel")
                # trajectory consistency: constant-turn-rate prediction from the two previous states
                if len(tr.states) >= 2:
                    q = tr.states[-2]
                    dtq = p[0] - q[0]
                    if dtq > 0:
                        w = (((p[6] - q[6] + 540) % 360) - 180) / dtq
                        h = p[6] + w * dt / 2.0
                        rr = math.radians(h)
                        tx = p[2] + math.sin(rr) * p[5] * KT * dt
                        ty = p[3] + math.cos(rr) * p[5] * KT * dt
                        tr.traj_res.append(math.hypot(x - tx, y - ty) / max(dt, 0.5))
                # intent: flying the OPPOSITE of a commit is inconsistent; not acting (yet) is neither
                trk_rate = (((cur[6] - p[6] + 540) % 360) - 180) / dte
                for it in tr.intents:
                    if it[0] <= t <= it[1]:
                        good = {"R": trk_rate > 1.0, "L": trk_rate < -1.0, "CLIMB": cur[7] > 150.0, "DESCEND": cur[7] < -150.0}[it[2]]
                        bad = {"R": trk_rate < -1.0, "L": trk_rate > 1.0, "CLIMB": cur[7] < -150.0, "DESCEND": cur[7] > 150.0}[it[2]]
                        it[3] += good
                        it[4] += bad
                    elif t > it[1] and not it[5]:
                        it[5] = True
                        if it[4] > it[3] and it[4] >= 2:
                            tr.intent_bad_n += 1
                            tr.intent_bad_t = now
                        elif it[3] >= 2:
                            tr.intent_ok_n += 1
        for v in viol:
            tr.flags[v] = now
        score = max(0.0, 1.0 - pos_res / 1000.0 - vel_res / 50.0 - turn_res / 60.0)
        tr.motion = MotionEvidence(not viol, pos_res, vel_res, turn_res, vert_res, round(score, 2), viol)
        if not tr.states and self.own_pos is not None and tr.pop_in is None:
            rng0 = math.hypot(x - self.own_pos[0], y - self.own_pos[1])
            if rng0 < POPIN_M and cur[4] > POPIN_MIN_ALT_FT:
                tr.pop_in = f"appeared {rng0 / 1000:.1f} km away, already airborne"
        geo = b.get("alt_geo_ft")
        if geo is not None and self.own_geo_minus_baro is not None:
            tr.baro_geo.append(abs((float(geo) - cur[4]) - self.own_geo_minus_baro))
        tr.states.append(cur)
        # identity: one id at two places at once
        recent = [s for s in tr.states if t - s[0] <= 6.0]
        if len(recent) >= 4:
            pts = []
            for s in recent:
                r = math.radians(s[6])
                pts.append((s[2] + math.sin(r) * s[5] * KT * (t - s[0]), s[3] + math.cos(r) * s[5] * KT * (t - s[0])))
            a = pts[-1]
            far = [p for p in pts if math.hypot(p[0] - a[0], p[1] - a[1]) > DUP_ID_M]
            if len(far) >= 2 and len(pts) - len(far) >= 2:
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

    # ================================================================== RF source (passive)
    def _ingest_channel_rf(self, tid: str, tr: _Track, t: float, rfm: dict) -> None:
        try:
            from radio import rf as _rf
            fspl = _rf.PTX_DBM - float(rfm["rssi_dbm"])
            rng = 10 ** ((fspl - 20 * math.log10(_rf.FREQ_HZ) + 147.55) / 20.0)
            lsig, dsig = _rf.RSSI_SIGMA_DB, _rf.DOPPLER_SIGMA_HZ
        except Exception:
            rng, lsig, dsig = None, 3.0, 3.0
        self.ingest_rf(RFMeasurement(self.own_id, tid, t, self.own_pos, estimated_range_m=rng, range_log_sigma_db=lsig,
                                     doppler_hz=rfm.get("doppler_hz"), doppler_sigma_hz=dsig, observer_vel=self.own_vel,
                                     rssi_dbm=rfm.get("rssi_dbm")))

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
            pred = (rng - math.sqrt((cx - rx_) ** 2 + (cy - ry_) ** 2 + (cz - rz_) ** 2)) / C * 1e9
            z["tdoa"] = abs(m.tdoa_ns - pred) / m.tdoa_sigma_ns
        if m.doppler_hz is not None and m.observer_id == self.own_id:
            ov = m.observer_vel or (0.0, 0.0, 0.0)
            rdot = (dx * (cv[0] - ov[0]) + dy * (cv[1] - ov[1]) + dz * (cv[2] - ov[2])) / rng
            tr.dop.append((m.t, float(m.doppler_hz), -915e6 * rdot / C))
        if m.rssi_dbm is not None:
            tr.rssi.append((m.t, float(m.rssi_dbm), rng))
        return z or None

    @staticmethod
    def _radial_sign(tr: _Track) -> int:
        """Observed radial motion from our own carrier measurements: +1 closing, -1 opening, 0 unclear."""
        v = sorted(d[1] for d in list(tr.dop)[-3:])
        if not v or abs(v[len(v) // 2]) < 2.0:
            return 0
        return 1 if v[len(v) // 2] > 0 else -1

    @staticmethod
    def _trend_sign(vals: list) -> int:
        if len(vals) < 3:
            return 0
        d = vals[-1] - vals[0]
        return 0 if abs(d) < 1e-6 else (1 if d > 0 else -1)

    def _rf_location(self, tr: _Track) -> Evidence:
        verdicts, worst = [], None
        for kind in ("range", "bearing", "tdoa"):
            zs = sorted(z[kind] for _, z in tr.rf if kind in z)
            if len(zs) < 2:
                continue
            med = zs[len(zs) // 2]
            if len(zs) < 3 and med < RF_Z_STRONG + 2:
                continue
            verdicts.append((kind, med))
            if med > RF_Z_CONFLICT and (worst is None or med > worst[1]):
                worst = (kind, med)
        if not verdicts:
            return Evidence("rf_location", UNKNOWN, "no RF position data")
        if worst:
            extra = ""
            if worst[0] == "range":
                errs = sorted(z.get("range_err_m", 0) for _, z in tr.rf if "range" in z)
                extra = f", {errs[len(errs) // 2] / 1000:.1f} km off"
            return Evidence("rf_location", CONFLICT, f"{worst[0]} z {worst[1]:.0f}{extra}")
        return Evidence("rf_location", PASS, ", ".join(f"{k} z {m:.1f}" for k, m in verdicts))

    def _doppler_trend(self, tr: _Track) -> tuple:
        """Trend, not absolute frequency: does the measured shift move with the claimed closing / opening?"""
        d = list(tr.dop)
        if len(d) < 4:
            return Evidence("doppler_trend", UNKNOWN, "fewer than 4 samples"), None
        sig = [x for x in d if abs(x[2]) > 2.0]                      # only where the claim predicts a clear shift
        if len(sig) < 3:
            return Evidence("doppler_trend", UNKNOWN, "claimed radial motion too small to judge"), None
        agree = sum(1 for _, o, e in sig if (o > 0) == (e > 0)) / len(sig)
        de = [b[2] - a[2] for a, b in zip(sig, sig[1:])]
        do = [b[1] - a[1] for a, b in zip(sig, sig[1:])]
        trend_ok = sum(1 for a, b in zip(de, do) if abs(a) < 1.0 or (a > 0) == (b > 0)) / max(1, len(de))
        cons = 0.7 * agree + 0.3 * trend_ok
        ev = RFWaveEvidence("closing" if sig[-1][2] > 0 else "opening",
                            "rising" if do and sum(do) > 0 else "falling", round(cons, 2), min(1.0, len(sig) / 8.0))
        if cons >= 0.75:
            return Evidence("doppler_trend", PASS, f"consistency {cons:.2f}"), ev
        if cons <= 0.3:
            return Evidence("doppler_trend", CONFLICT, f"claimed {ev.expected_radial_motion}, carrier says otherwise "
                                                       f"(consistency {cons:.2f})"), ev
        return Evidence("doppler_trend", WEAK, f"consistency {cons:.2f}"), ev

    def _rssi_trend(self, tr: _Track) -> Evidence:
        r = list(tr.rssi)
        if len(r) < 5:
            return Evidence("rssi_trend", UNKNOWN, "fewer than 5 samples")
        exp = [-20 * math.log10(max(x[2], 1.0)) for x in r]
        obs = [x[1] for x in r]
        if max(exp) - min(exp) < 3.0:
            return Evidence("rssi_trend", UNKNOWN, "claimed range nearly constant")
        me, mo = sum(exp) / len(exp), sum(obs) / len(obs)
        cov = sum((a - me) * (b - mo) for a, b in zip(exp, obs))
        sd = math.sqrt(sum((a - me) ** 2 for a in exp) * sum((b - mo) ** 2 for b in obs)) or 1.0
        corr = cov / sd
        if corr > 0.5:
            return Evidence("rssi_trend", PASS, f"corr {corr:.2f}")
        if corr < -0.3:
            return Evidence("rssi_trend", CONFLICT, f"corr {corr:.2f} (signal weakens as the claim closes)")
        return Evidence("rssi_trend", UNKNOWN, f"corr {corr:.2f}")

    # ================================================================== independent surveillance sources
    def _associate(self, m: TCASMeasurement) -> Optional[str]:
        best = None
        for tid, tr in self.tracks.items():
            z = self._tcas_z(tr, m)
            if z is None:
                continue
            score = z.get("range", 9) + z.get("bearing", 0) * 0.5
            if best is None or score < best[0]:
                best = (score, tid)
        return best[1] if best and best[0] < 3.0 else None

    def _tcas_z(self, tr: _Track, m: TCASMeasurement) -> Optional[dict]:
        c = self._claimed_at(tr, m.t)
        if c is None or self.own_pos is None:
            return None
        dx, dy, dz = c[0] - self.own_pos[0], c[1] - self.own_pos[1], c[2] - self.own_pos[2]
        rng = math.sqrt(dx * dx + dy * dy + dz * dz)
        z = {"range": abs(m.range_m - rng) / m.range_sigma_m, "range_err_m": abs(m.range_m - rng)}
        if m.bearing_deg is not None:
            b = math.degrees(math.atan2(dx, dy)) % 360.0
            z["bearing"] = abs(((m.bearing_deg - b + 540) % 360) - 180) / m.bearing_sigma_deg
        if m.relative_altitude_m is not None:
            z["alt"] = abs(m.relative_altitude_m - dz) / m.alt_sigma_m
        return z

    def _tcas(self, tr: _Track, now: float) -> tuple:
        rec = [(m, z) for m, z in tr.tcas if now - m.t < TCAS_FRESH_S]
        if not rec:
            return Evidence("tcas", UNKNOWN, "no TCAS track (absence is not evidence)"), False
        worst = max(max(v for k, v in z.items() if k != "range_err_m") for _, z in rec[-3:])
        if worst > TCAS_Z_CONFLICT:
            err = max(z.get("range_err_m", 0) for _, z in rec[-3:])
            return Evidence("tcas", CONFLICT, f"independent range/bearing disagree (z {worst:.0f}, {err / 1852:.1f} NM)"), True
        if worst < 2.0:
            return Evidence("tcas", PASS, f"agrees (z {worst:.1f})"), True
        return Evidence("tcas", WEAK, f"partial agreement (z {worst:.1f})"), True

    # ================================================================== peer witnesses (passive digests)
    def _witnesses(self, tid: str) -> tuple:
        n, ref, silent = 0, [], []
        if self.peer_evidence is not None:
            try:
                _, ev = self.peer_evidence(tid)
            except Exception:
                ev = []
            for e in ev:
                if e.startswith("corroborated:"):
                    n = int(e.split(":")[1])
                elif e.startswith("refuted_by:"):
                    ref = e.split(":")[1].split(",")
                elif e.startswith("not_heard_by:"):
                    silent = e.split(":")[1].split(",")
        if n >= 1:
            return Evidence("witnesses", PASS, f"{n} corroborating"), n, ref, silent
        if ref:
            return Evidence("witnesses", CONFLICT, f"refuted by {','.join(ref)}"), n, ref, silent
        detail = "no witness reports" + (f"; not heard by {','.join(silent)}" if silent else "")
        return Evidence("witnesses", UNKNOWN, detail), n, ref, silent

    def _multi_observer(self, tr: _Track, now: float) -> tuple:
        """Each verified peer's digest: does the claimed position/velocity give the radial-motion SIGN that peer
        actually observed?  Consistency checking across separated observers, not precise multilateration."""
        obs = [(o, d) for o, d in tr.digests.items() if now - d[0] < 6.0]
        if not obs:
            return Evidence("multi_observer", UNKNOWN, "no peer digests"), None
        checked, bad = 0, []
        for o, (t, dig, opos) in obs:
            if not isinstance(dig, list) or len(dig) < 3 or dig[2] == 0:
                continue
            c = self._claimed_at(tr, t)
            if c is None:
                continue
            dx, dy, dz = c[0] - opos[0], c[1] - opos[1], c[2] - opos[2]
            ov = opos[3] if len(opos) > 3 else (0.0, 0.0, 0.0)
            rdot = (dx * (c[3][0] - ov[0]) + dy * (c[3][1] - ov[1]) + dz * (c[3][2] - ov[2])) / \
                (math.sqrt(dx * dx + dy * dy + dz * dz) or 1.0)
            if abs(rdot) < 3.0:
                continue
            exp_sign = 1 if rdot < 0 else -1                  # closing -> Doppler shift rising
            checked += 1
            if exp_sign != dig[2]:
                bad.append(o)
        if not checked:
            return Evidence("multi_observer", UNKNOWN, "digests carry no usable trend"), None
        cons = 1.0 - len(bad) / checked
        ev = MultiObserverEvidence(checked, round(cons, 2), bad, min(1.0, checked / 3.0))
        if cons >= 0.75:
            return Evidence("multi_observer", PASS, f"{checked} observers consistent"), ev
        if cons <= 0.34:
            return Evidence("multi_observer", CONFLICT, f"contradicted by {','.join(bad)}"), ev
        return Evidence("multi_observer", UNKNOWN, f"mixed ({cons:.2f})"), ev

    def _sybil(self, tid: str, now: float) -> list[str]:
        tr = self.tracks.get(tid)
        if tr is None or not tr.rf:
            return []
        mine = [m for m, _ in tr.rf if now - m.t < 15.0]
        same = []
        for oid, otr in self.tracks.items():
            if oid == tid or not otr.rf:
                continue
            other = [m for m, _ in otr.rf if now - m.t < 15.0]
            if any(a.source_id and a.source_id == b.source_id for a in mine for b in other):
                same.append(oid)
                continue
            pairs = [(a, b) for a in mine for b in other if abs(a.t - b.t) < 0.6 and a.observer_id == b.observer_id
                     and a.estimated_range_m and b.estimated_range_m]
            if len(pairs) >= 3:
                d = sorted(abs(20 * math.log10(a.estimated_range_m / b.estimated_range_m)) for a, b in pairs)
                ca, cb = self._claimed_at(tr, now), self._claimed_at(otr, now)
                apart = ca is not None and cb is not None and math.hypot(ca[0] - cb[0], ca[1] - cb[1]) > 250.0
                rf_bad = any(z.get("range", 0) > RF_Z_CONFLICT for _, z in tr.rf)
                if d[len(d) // 2] < 1.5 and apart and rf_bad:
                    same.append(oid)
        return sorted(same)

    # ================================================================== fusion
    def assess(self, tid: str, now: float) -> TrustResult:
        t_start = time.perf_counter()
        try:
            return self._assess(tid, now)
        finally:
            self.stats["assess_us"].append((time.perf_counter() - t_start) * 1e6)

    def _assess(self, tid: str, now: float) -> TrustResult:
        tr = self.tracks.get(tid)
        if tr is None or not tr.states:
            return TrustResult(tid, PRIOR[0], UNVERIFIED, {"data": "UNKNOWN (no position yet)"}, ["no data"], [],
                               probs={"real": PRIOR[0], "spoof": PRIOR[1], "faulty": PRIOR[2]})
        active = {k for k, t in tr.flags.items() if now - t < FLAG_HOLD_S}
        ev: list[Evidence] = []
        gates: list[str] = []
        sigma_extra = 1.0

        # --- packet guard
        if tr.auth == "ok":
            ev.append(Evidence("signature", PASS, "valid ARC signature"))
        elif tr.auth in (None, "n/a") and not self.signed_radio:
            ev.append(Evidence("signature", UNKNOWN, "transport carries no signatures"))
        else:
            ev.append(Evidence("signature", CONFLICT, f"no valid ARC signature ({tr.auth or 'none'}) - legacy or forged"))
        pkt = sorted(active & {"replay", "duplicate", "stale", "expired_session", "seq_jump", "session_churn"})
        ev.append(Evidence("packet", CONFLICT, "dropped: " + ",".join(pkt)) if pkt else Evidence("packet", UNKNOWN, "clean"))

        # --- flight physics
        kin = sorted(active & {"speed", "accel", "turn_rate", "climb_rate", "position_jump", "altitude_jump", "vertical_accel"})
        if self.peer_evidence is not None:
            try:
                for e in self.peer_evidence(tid)[1]:
                    if e.startswith("kinematics_violation:"):
                        kin = sorted(set(kin) | {e.split(":", 1)[1]})
            except Exception:
                pass
        m = tr.motion
        if kin:
            ev.append(Evidence("motion", CONFLICT, ", ".join(kin)))
        elif len(tr.states) >= 2:
            ev.append(Evidence("motion", PASS, f"residuals pos {m.position_residual:.0f} m, turn {m.turn_residual:.1f} deg/s"))
        else:
            ev.append(Evidence("motion", UNKNOWN, "one state so far"))
        if len(tr.traj_res) >= 3:
            rms = math.sqrt(sum(r * r for r in tr.traj_res) / len(tr.traj_res))
            ev.append(Evidence("trajectory", PASS if rms < 40 else WEAK if rms < 120 else CONFLICT, f"rms residual {rms:.0f} m/s"))
        else:
            ev.append(Evidence("trajectory", UNKNOWN, "history too short"))
        if now - tr.intent_bad_t < 60.0 and tr.intent_bad_n:
            ev.append(Evidence("intent", CONFLICT, f"flew opposite of its own commit x{tr.intent_bad_n}"))
            sigma_extra = 1.0 + min(0.5, 0.2 * tr.intent_bad_n)       # uncertainty first, not a verdict
        elif tr.intent_ok_n:
            ev.append(Evidence("intent", PASS, f"flew its commit x{tr.intent_ok_n}"))
        else:
            ev.append(Evidence("intent", UNKNOWN, "no commit to compare"))
        if "duplicate_identity" in active:
            ev.append(Evidence("identity", CONFLICT, "one identity in two places"))
        if tr.pop_in:
            ev.append(Evidence("pop_in", CONFLICT, tr.pop_in))
        if len(tr.baro_geo) >= 2:
            bg = sorted(tr.baro_geo)[len(tr.baro_geo) // 2]
            v = CONFLICT if bg > BARO_GEO_HARD_FT else WEAK if bg > BARO_GEO_SOFT_FT else PASS
            ev.append(Evidence("baro_geo", v, f"GNSS-baro offset differs from ours by {bg:.0f} ft"))

        # --- independent surveillance (optional)
        tcas_ev, has_tcas = self._tcas(tr, now)
        ev.append(tcas_ev)
        modes = [x for x in tr.modes if now - x.t < MODES_FRESH_S]
        ev.append(Evidence("mode_s", PASS, f"{len(modes)} 1090 MHz observations") if modes
                  else Evidence("mode_s", UNKNOWN, "no Mode-S data (absence is not evidence)"))
        ireps = [x for x in tr.ireps if now - x.reply_time < IREP_FRESH_S and x.confidence >= 0.5]
        ev.append(Evidence("interrogation", PASS, f"{len(ireps)} 1030/1090 reply pairs") if ireps
                  else Evidence("interrogation", UNKNOWN, "none observed (absence is not evidence)"))

        # --- RF + peers
        ev.append(self._rf_location(tr))
        dop_ev, _ = self._doppler_trend(tr)
        ev.append(dop_ev)
        ev.append(self._rssi_trend(tr))
        w_ev, n_w, refuters, silent = self._witnesses(tid)
        ev.append(w_ev)
        mo_ev, _ = self._multi_observer(tr, now)
        ev.append(mo_ev)
        syb = self._sybil(tid, now)
        if syb:
            ev.append(Evidence("sybil", CONFLICT, "same transmitter as " + ",".join(syb)))
        ev.append(Evidence("negotiation", PASS, f"answered our negotiation in {tr.neg_latency:.1f} s")
                  if now - tr.neg_ok_t < 60.0 else Evidence("negotiation", UNKNOWN, "no negotiation exchanged"))

        # --- likelihood update over REAL / SPOOF / FAULTY
        lp = [math.log(p) for p in PRIOR]
        for e in ev:
            lk = e.likelihood
            for i in range(3):
                lp[i] += math.log(lk[i])
        mx = max(lp)
        ps = [math.exp(v - mx) for v in lp]
        s = sum(ps)
        p_real, p_spoof, p_faulty = (v / s for v in ps)
        by = {e.source: e for e in ev}
        sig_fail = by["signature"].verdict == CONFLICT

        # --- hard gates (override the probabilities)
        strikes = 0
        if pkt and tr.auth == "ok" and set(pkt) <= {"replay", "duplicate", "stale", "expired_session"} and self._clean_live_track(tr, now):
            gates.append("replays of this identity dropped; its own live signed track is clean (attack on it, not by it)")
            pkt = []
        elif pkt:
            strikes += 1
            gates.append("replayed / duplicate / stale / expired-session packets dropped -> cannot be VERIFIED")
        if sig_fail:
            gates.append("no valid ARC signature -> cannot be VERIFIED (still tracked)")
        for k in ("motion", "identity", "sybil", "rf_location", "tcas", "multi_observer", "baro_geo"):
            if by.get(k) and by[k].verdict == CONFLICT:
                strikes += 1
                gates.append(f"{k} contradiction -> SUSPICIOUS minimum")
        if by["doppler_trend"].verdict == CONFLICT and (strikes or sig_fail):
            strikes += 1
            gates.append("Doppler trend contradicts the claimed motion and confirms another failure")
        if sig_fail and strikes:
            strikes += 1
        quarantine = strikes >= 2 or p_spoof >= P_SPOOF_QUARANTINE
        if quarantine and strikes >= 2:
            gates.append(f"{strikes} independent hard failures -> QUARANTINED")
        suspicious = (strikes >= 1 or p_spoof >= P_SPOOF_SUSPICIOUS
                      or (sig_fail and n_w == 0 and (refuters or len(silent) >= 2 or tr.pop_in)))
        long_enough = tr.states[-1][0] - tr.states[0][0] >= VERIFY_MIN_TRACK_S
        can_verify = (not sig_fail) and not pkt and long_enough and p_real >= P_REAL_VERIFIED
        if quarantine:
            state = QUARANTINED
        elif suspicious:
            state = SUSPICIOUS
        elif can_verify:
            state = VERIFIED
        else:
            state = UNVERIFIED

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
        checks = {e.source: (e.verdict if not e.detail else f"{e.verdict} ({e.detail})") for e in ev}
        reasons = [f"{e.source}: {e.verdict} ({e.detail})" for e in ev if e.verdict in (CONFLICT, WEAK)]
        support = (has_tcas and tcas_ev.verdict != CONFLICT) or bool(ireps)
        return TrustResult(tid, round(p_real, 2), state, checks, reasons, gates, sigma_extra,
                           {"real": round(p_real, 2), "spoof": round(p_spoof, 2), "faulty": round(p_faulty, 2)},
                           physical_support=support)

    def _clean_live_track(self, tr: _Track, now: float) -> bool:
        """The identity's own signed traffic is fresh and physically clean right now."""
        return (now - tr.last_rx < 3.0 and len(tr.states) >= 3 and tr.motion is not None and tr.motion.plausible
                and not ({"position_jump", "duplicate_identity"} & {k for k, t in tr.flags.items() if now - t < FLAG_HOLD_S}))
