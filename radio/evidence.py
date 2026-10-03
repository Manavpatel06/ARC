"""
radio/evidence.py — trust EVIDENCE per radio target (Lane C, task C8), consumed by node/trust.py.
"Trust by evidence, not by broadcast" — idea from the Onboard RF Consistency Monitor.

Four independent checks, each producing evidence strings:
  1. signature        "signed" | "unsigned" | "unknown_key"     (unsigned -> score capped at 0.55 = SUSPICIOUS)
  2. kinematics       speed <= 200 kt, accel <= 0.5 g, turn rate <= 10 deg/s, climb/descent <= 2,000 fpm,
                      and the claimed positions must be reachable from each other ("position_jump")
                      -> "plausible" | "kinematics_violation:<which>"
  3. RF consistency   the channel attaches an emulated RSSI + Doppler measured from the TRUE transmitter;
                      we predict both from the CLAIMED position/velocity and our own state. Median error over
                      the last 5 packets: > 6 dB or > 25 Hz -> "rf_rssi_mismatch:9dB" / "rf_doppler_mismatch:80Hz"
  4. peer corroboration  every node puts {target: 1|0} for what it heard (1 = RF + kinematics agree) in its
                      HEARTBEAT as "nb". Signed peers that report the target 1 from different bearings
                      (>= 30 deg apart, seen from the target) -> "corroborated:2". Signed peers that should
                      hear it (claimed range < 3.9 km) but report 0 or nothing -> "refuted_by:N204".
Score starts at 1.0: unsigned caps at 0.55, each kinematic flag -0.35, RF mismatch -0.35, each failed
corroboration -0.25 (max two). Thresholds live in node/trust.py: TRUSTED >= 0.7, SUSPICIOUS >= 0.4, else FAKE.
So a lone unsigned GHOST7 is SUSPICIOUS; once two peers fail to corroborate it (or its RF disagrees) it is FAKE.
Camera corroboration: schema field exists, NOT implemented (CV is cut).

Known limit (say it in the pitch): a Sybil attacker with several VALID keys and transmitters placed to fake
geometry could corroborate itself. Registration-bound keys + RF checks from several receivers make that
expensive, not impossible.

Wiring for Lane B (node/node.py), three lines:
    from radio.evidence import TrustEvidence
    ev = TrustEvidence.attach(radio)          # hooks on_message, own STATE, neighbor report in HEARTBEAT
    node.trust.set_scorer(ev.scorer)          # TrustTable calls ev.scorer(peer_id, track) -> (score, [evidence])
  and, for best RF accuracy, on every OWNSHIP:  ev.on_ownship(own)
"""
from __future__ import annotations
import collections, math, statistics, time
from typing import Optional

from radio import rf

MAX_SPEED_KT = 200.0
MAX_ACCEL_KT_S = 0.5 * 9.80665 / rf.KT          # 0.5 g ~ 9.5 kt/s
MAX_TURN_DEG_S = 10.0
MAX_VS_FPM = 2000.0
RSSI_TOL_DB = 6.0
DOPPLER_TOL_HZ = 25.0
RF_MIN_SAMPLES = 3
CORROB_RANGE_M = 3900.0          # peers closer than this (claimed) to a target are expected to hear it
FLAG_HOLD_S = 15.0
NB_FRESH_S = 5.0
STALE_S = 5.0
BEARING_SEP_DEG = 30.0


class _Track:
    def __init__(self):
        self.states: collections.deque = collections.deque(maxlen=10)   # (t, body, rx_t)
        self.auth = "unsigned"
        self.flags: dict[str, float] = {}
        self.rf_err: collections.deque = collections.deque(maxlen=5)    # (|drssi|, |ddop|)
        self.nb: Optional[tuple[float, dict]] = None                    # this peer's latest neighbor report
        self.last_rx = 0.0


class TrustEvidence:
    def __init__(self, own_id: str, clock=time.time):
        self.own_id = own_id
        self.clock = clock
        self.tracks: dict[str, _Track] = collections.defaultdict(_Track)
        self.own: Optional[dict] = None          # rf-state dict
        self.own_t = 0.0

    # ---------------- wiring ----------------
    @classmethod
    def attach(cls, radio) -> "TrustEvidence":
        ev = cls(radio.ac_id, clock=getattr(radio, "clock", time.time))
        radio.on_message(ev.on_message)
        if hasattr(radio, "set_neighbor_report"):
            radio.set_neighbor_report(ev.neighbor_report)
        orig_send = radio.send

        async def send(msg, body):
            env = await orig_send(msg, body)
            if msg == "STATE" and ev.own_t < ev.clock() - 0.5:    # fall back to our own STATE if no OWNSHIP feed
                ev.own, ev.own_t = rf.from_state_body(body), env["t"]
            return env
        radio.send = send
        return ev

    def on_ownship(self, own: dict):
        self.own, self.own_t = rf.from_ownship(own), own.get("t", self.clock())

    # ---------------- ingest ----------------
    def on_message(self, env: dict):
        frm = env.get("from")
        if not frm or frm == self.own_id:
            return
        tr = self.tracks[frm]
        tr.auth = env.get("_auth", "ok")
        tr.last_rx = env.get("_rx_t", self.clock())
        if env["msg"] == "HEARTBEAT" and isinstance(env["body"].get("nb"), dict):
            tr.nb = (tr.last_rx, env["body"]["nb"])
        if env["msg"] != "STATE":
            return
        b = env["body"]
        if tr.states:
            self._kinematics(tr, tr.states[-1][0], tr.states[-1][1], env["t"], b)
        tr.states.append((env["t"], b, tr.last_rx))
        if env.get("_rf") and self.own is not None:
            self._rf(tr, env["t"], b, env["_rf"], tr.last_rx)

    def _kinematics(self, tr: _Track, t0: float, b0: dict, t1: float, b1: dict):
        now = self.clock()
        dt = t1 - t0
        if b1.get("gs_kt", 0) > MAX_SPEED_KT:
            tr.flags["speed"] = now
        if abs(b1.get("vs_fpm", 0)) > MAX_VS_FPM:
            tr.flags["climb_rate"] = now
        if dt < 0.3:
            return
        dist = rf.horiz_range_m(rf.from_state_body(b0), rf.from_state_body(b1))
        if dist / dt / rf.KT > MAX_SPEED_KT * 1.15 + 20:
            tr.flags["position_jump"] = now
        if abs(b1.get("gs_kt", 0) - b0.get("gs_kt", 0)) / dt > MAX_ACCEL_KT_S * 1.2:
            tr.flags["accel"] = now
        dtrk = (b1.get("track_deg", 0) - b0.get("track_deg", 0) + 540) % 360 - 180
        if abs(dtrk) / dt > MAX_TURN_DEG_S * 1.2:
            tr.flags["turn_rate"] = now
        dalt_fpm = (b1.get("alt_press_ft", 0) - b0.get("alt_press_ft", 0)) / dt * 60
        if abs(dalt_fpm) > MAX_VS_FPM * 1.5 + 300:
            tr.flags["climb_rate"] = now

    def _own_at(self, t: float) -> dict:
        o = self.own
        lat, lon, alt = rf.extrapolate(o["lat"], o["lon"], o["alt_ft"], o["gs_kt"], o["track_deg"], o["vs_fpm"],
                                       max(-2.0, min(2.0, t - self.own_t)))
        return dict(o, lat=lat, lon=lon, alt_ft=alt)

    def _rf(self, tr: _Track, t: float, b: dict, meas: dict, rx_t: float):
        claimed = rf.from_state_body(b)
        slant, rdot = rf.geometry(claimed, self._own_at(t))
        d_rssi = meas["rssi_dbm"] - rf.rssi_dbm(slant)
        d_dop = meas["doppler_hz"] - rf.doppler_hz(rdot)
        tr.rf_err.append((abs(d_rssi), abs(d_dop)))

    # ---------------- outputs ----------------
    def _rf_verdict(self, tr: _Track) -> tuple[Optional[bool], list[str]]:
        if len(tr.rf_err) < RF_MIN_SAMPLES:
            return None, []
        m_rssi = statistics.median(e[0] for e in tr.rf_err)
        m_dop = statistics.median(e[1] for e in tr.rf_err)
        ev = []
        if m_rssi > RSSI_TOL_DB:
            ev.append(f"rf_rssi_mismatch:{m_rssi:.0f}dB")
        if m_dop > DOPPLER_TOL_HZ:
            ev.append(f"rf_doppler_mismatch:{m_dop:.0f}Hz")
        return (not ev), (ev or ["rf_consistent"])

    def _active_flags(self, tr: _Track) -> list[str]:
        now = self.clock()
        return sorted(k for k, t in tr.flags.items() if now - t < FLAG_HOLD_S)

    def neighbor_report(self) -> dict:
        """{target: 1|0} for targets heard in the last 3 s; 1 = RF and kinematics agree with the claim."""
        now = self.clock()
        out = {}
        for tid, tr in sorted(self.tracks.items(), key=lambda kv: -kv[1].last_rx):
            if not tr.states or now - tr.states[-1][2] > 3.0:
                continue
            rf_ok, _ = self._rf_verdict(tr)
            out[tid] = 0 if (rf_ok is False or self._active_flags(tr)) else 1
        return out

    def _corroboration(self, tid: str, tr: _Track) -> tuple[int, list[str], list[str]]:
        now = self.clock()
        if not tr.states:
            return 0, [], []
        tpos = rf.from_state_body(tr.states[-1][1])
        bearings, refuters, silent = [], [], []
        for pid, ptr in self.tracks.items():
            if pid == tid or ptr.auth != "ok" or ptr.nb is None or now - ptr.nb[0] > NB_FRESH_S or not ptr.states:
                continue
            ppos = rf.from_state_body(ptr.states[-1][1])
            nb = ptr.nb[1]
            if nb.get(tid) == 1:
                bearings.append(rf.bearing_deg(tpos, ppos))
            elif nb.get(tid) == 0:
                refuters.append(pid)
            elif rf.horiz_range_m(tpos, ppos) < CORROB_RANGE_M:
                silent.append(pid)
        clusters: list[float] = []
        for b in sorted(bearings):
            if all(abs((b - c + 180) % 360 - 180) >= BEARING_SEP_DEG for c in clusters):
                clusters.append(b)
        return len(clusters), refuters, silent

    def assess(self, tid: str) -> tuple[float, list[str]]:
        tr = self.tracks.get(tid)
        if tr is None:
            return 0.5, ["no_data"]
        score, ev = 1.0, []
        if tr.auth == "ok":
            ev.append("signed")
        else:
            ev.append(tr.auth)
            score = min(score, 0.55)
        flags = self._active_flags(tr)
        if flags:
            ev += [f"kinematics_violation:{f}" for f in flags]
            score -= 0.35 * len(flags)
        elif len(tr.states) >= 2:
            ev.append("plausible")
        rf_ok, rf_ev = self._rf_verdict(tr)
        ev += rf_ev
        if rf_ok is False:
            score -= 0.35
        n, refuters, silent = self._corroboration(tid, tr)
        if n:
            ev.append(f"corroborated:{n}")
        fails = refuters + silent
        if refuters:
            ev.append("refuted_by:" + ",".join(sorted(refuters)))
        if silent:
            ev.append("not_heard_by:" + ",".join(sorted(silent)))
        if fails and n < 2:
            score -= 0.25 * min(2, len(fails))
        if fails and not n:
            ev.append("no_corroboration")
        if tr.states and self.clock() - tr.states[-1][2] > STALE_S:
            ev.append("stale")
        return round(max(0.0, min(1.0, score)), 2), ev

    def scorer(self, peer_id: str, track: Optional[dict] = None) -> tuple[float, list[str]]:
        """Signature node/trust.TrustTable.set_scorer expects."""
        return self.assess(peer_id)

    def assess_all(self) -> dict[str, tuple[float, list[str]]]:
        return {tid: self.assess(tid) for tid in self.tracks}


def state_for(score: float) -> str:
    """Same thresholds as node/trust.py (kept here only for radio-side tests and logs)."""
    return "TRUSTED" if score >= 0.7 else "SUSPICIOUS" if score >= 0.4 else "FAKE"
