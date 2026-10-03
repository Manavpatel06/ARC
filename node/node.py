"""
node/node.py — Lane B (B1).  One process per aircraft.

Reads ONLY its own OWNSHIP from the world; everything about other aircraft arrives over the radio.
Emits ADVISORY / COMMAND / TRUST / PREDICTION to the world and STATE / INTENT / SEQ_* / MANEUVER_COMMIT /
HEARTBEAT to the radio.  Deterministic: no randomness, no LLM, every decision carries a `reason` dict.

The logic lives in `Node` (synchronous, clock-free: time is whatever OWNSHIP.t says) so the same code runs
against the real world, the stubs, and the in-process Monte Carlo harness.  `run()` is the asyncio shell.

Run:  python node/node.py --id N101 --world ws://localhost:8765 [--via-channel ws://<ip>:8765] [-v]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from node import authority, conflict as conflict_mod, escape, layers
from node.geometry import FT, KT, bearing_deg, build_patterns, hvec, load_metar, to_enu, to_latlon, wrap180
from node.negotiate import Negotiator
from node.predict import CONF_MIN, KState, Predictor, Prediction
from node.trust import TrustTable

STATE_PERIOD_S = 1.0
HEARTBEAT_PERIOD_S = 2.0
TRUST_PERIOD_S = 1.0          # contract v1.1: the cockpit radar draws from TRUST, send at >= 1 Hz
PRED_PERIOD_S = 1.0
PEER_TIMEOUT_S = 30.0          # a silent peer stays on the table (sigma growing with age) so a link loss cannot erase a conflict
LATENCY_EST_S = 0.3
PEER_HORIZON_S = 140.0
OWN_HORIZON_S = 105.0
PILOT_MAX_BANK = 45.0
PILOT_HOLD_S = 15.0
ESC_GRID = np.arange(0.0, escape.HORIZON_S + 1e-9, escape.DT)
RELEVANT_RADIUS_M = 6000.0
STICK_INHIBIT_S = 5.0
ADV_MIN_PERIOD_S = 3.0


@dataclass
class PeerTrack:
    id: str
    lat: float = 0.0
    lon: float = 0.0
    x: float = 0.0
    y: float = 0.0
    alt_press_ft: float = 0.0
    gs: float = 0.0
    track: float = 0.0
    vs: float = 0.0
    leg: str = "UNKNOWN"
    intent: str = ""
    ap_equipped: bool = False
    seq: int = -1
    rx_t: float = -1e9
    prev_track: Optional[float] = None
    prev_rx: float = 0.0
    turn_rate: float = 0.0
    intent_turn: Optional[tuple] = None          # (leg, absolute node-clock time)
    pred: Optional[Prediction] = None
    pred_key: tuple = ()
    z: float = 0.0                               # metres MSL, set from baro offset at prediction time


class Node:
    def __init__(self, ac_id: str, patterns: Optional[dict] = None, trust: Optional[TrustTable] = None,
                 metar: Optional[dict] = None, terrain_fn: Optional[Callable] = None,
                 obstacle_fn: Optional[Callable] = None, verbose: bool = False, record: bool = True,
                 latency_est_s: float = LATENCY_EST_S):
        self.id = ac_id
        self.patterns = patterns or build_patterns()
        self.predictor = Predictor(self.patterns)
        self.trust = trust or TrustTable()
        self.metar = metar or load_metar()
        self.da_ft = float(self.metar.get("density_altitude_ft", 4980))
        self.terrain_fn = terrain_fn or self._default_terrain()
        self.obstacle_fn = obstacle_fn if obstacle_fn is not None else self._default_obstacles()
        self.verbose, self.record, self.latency_est = verbose, record, latency_est_s

        self.negotiator = Negotiator(ac_id)
        self.auth = authority.AuthorityMonitor(ac_id)
        self.trackers: dict[str, layers.LayerTracker] = {}
        self.peers: dict[str, PeerTrack] = {}
        self.sightings: list[tuple[float, dict]] = []
        self.sent: list[tuple[str, float, dict]] = []        # ("world"|"radio", t, frame/(msg, body))
        self.tick_ms: list[float] = []

        self.now = 0.0
        self.own: dict = {}
        self.own_st: Optional[KState] = None
        self.own_cls = None
        self.own_pred: Optional[Prediction] = None
        self.baro_offset_m = 0.0

        self._radio_out: list[tuple[str, dict]] = []
        self._world_out: list[dict] = []
        self._last = {"state": -1e9, "hb": -1e9, "trust": -1e9, "pred": -1e9, "leglog": -1e9}
        self._adv = {"level": "CLEAR", "text": "", "t": -1e9, "target": None}
        self._no_takeover_until = -1e9
        self._seq_plans: dict[str, layers.SeqPlan] = {}
        self._seq_proposed: dict[str, float] = {}
        self._seq_own: dict[str, layers.SeqPlan] = {}
        self._last_intent_leg: Optional[str] = None
        self._esc_cache: dict[str, tuple[float, escape.EscapeResult]] = {}
        self._no_solution_sent: dict[str, float] = {}
        self._takeover_target: Optional[str] = None
        self.first_conflict_seen: Optional[float] = None            # first time the predictor showed any conflict
        self._near_ids: list[str] = []
        self._tk_retry_at = -1e9
        self._seq_extend: Optional[tuple[float, float, str]] = None  # (time advised, seconds, leg it applies to)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _default_obstacles() -> Optional[Callable[[float, float], Optional[float]]]:
        """data/obstacles.py (Lane D): obstacle_fn_enu(to_latlon) -> fn(x_m, y_m) = highest obstacle top in
        metres MSL within the 600 m protection radius, or None."""
        try:
            from data.obstacles import obstacle_fn_enu
            return obstacle_fn_enu(to_latlon)
        except Exception:
            return None

    @staticmethod
    def _default_terrain() -> Callable[[float, float], float]:
        try:
            from data.terrain import elev_at
            return lambda x, y: elev_at(*to_latlon(x, y))
        except Exception:
            return lambda x, y: 1478.0 * FT

    def _emit_world(self, frame: dict) -> dict:
        if self.record:
            self.sent.append(("world", self.now, frame))
        self._world_out.append(frame)
        return frame

    def _emit_radio(self, msg: str, body: dict) -> None:
        self._radio_out.append((msg, body))
        if self.record:
            self.sent.append(("radio", self.now, {"msg": msg, "body": body}))

    def _own_ctx(self) -> dict:
        o, cls = self.own, self.own_cls
        tpa = None
        if cls is not None and cls.runway in self.patterns:
            tpa = self.patterns[cls.runway].tpa_msl_m / FT
        return {"ias_kt": o["ias_kt"], "agl_ft": o["agl_ft"], "alt_msl_ft": o["alt_msl_ft"],
                "leg": cls.leg if cls else "UNKNOWN", "tpa_msl_ft": tpa,
                "ap_equipped": bool(o.get("ap_equipped")), "stick_active": bool(o.get("stick_active"))}

    # ------------------------------------------------------------------ radio in
    def on_radio(self, env: dict) -> None:
        peer = env.get("from") or env.get("from_")
        if not peer or peer == self.id:
            return
        msg, body, now = env.get("msg"), env.get("body", {}), self.now
        self.negotiator.note_rx(peer, now)
        tr = self.peers.get(peer)
        if msg == "STATE":
            if tr is None:
                tr = self.peers[peer] = PeerTrack(peer)
            x, y = to_enu(body["lat"], body["lon"])
            if tr.prev_track is not None and now > tr.prev_rx:
                tr.turn_rate = wrap180(body["track_deg"] - tr.prev_track) / max(now - tr.prev_rx, 0.2)
            tr.prev_track, tr.prev_rx = body["track_deg"], now
            tr.lat, tr.lon, tr.x, tr.y = body["lat"], body["lon"], x, y
            tr.alt_press_ft, tr.gs, tr.track = body["alt_press_ft"], body["gs_kt"] * KT, body["track_deg"]
            tr.vs = body.get("vs_fpm", 0.0) * FT / 60.0
            tr.leg, tr.ap_equipped = body.get("leg", "UNKNOWN"), bool(body.get("ap_equipped", False))
            tr.seq, tr.rx_t = int(env.get("seq", 0)), now
            self._set_intent(tr, body.get("intent", ""), now)
        elif tr is None:
            return
        elif msg == "INTENT":
            self._set_intent(tr, body.get("intent", ""), now, body.get("valid_for_s", 20.0))
            tr.pred_key = ()
        elif msg == "SEQ_PROPOSE":
            order = body.get("order", [])
            if self.id in order:
                self._seq_plans[peer] = layers.SeqPlan(body.get("runway", ""), list(order),
                                                       {k: float(v) for k, v in body.get("extend_s", {}).items()}, 0.0)
                self._emit_radio("SEQ_ACCEPT", {"proposal_seq": int(env.get("seq", 0))})
        elif msg == "MANEUVER_COMMIT":
            if body.get("target") == self.id:
                self.negotiator.on_commit(peer, body, now)
        elif msg == "SIGHTING":
            self.sightings.append((now, dict(body, _from=peer)))

    def _set_intent(self, tr: PeerTrack, intent: str, now: float, valid_for: float = 20.0) -> None:
        pi = Predictor.parse_intent(intent)
        if pi is None:
            tr.intent, tr.intent_turn = "", None
            return
        tr.intent = intent
        tr.intent_turn = (pi[0], now - self.latency_est + pi[1])

    # ------------------------------------------------------------------ world in
    def on_env(self, msg: dict) -> None:
        """ENV frame from the world (god's SET_DA, live weather): density altitude drives climb capability."""
        if "da_field_ft" in msg:
            self.da_ft = float(msg["da_field_ft"])

    def on_stick(self, msg: dict) -> list[dict]:
        self._world_out = []
        self.now = msg.get("t", self.now)
        self._no_takeover_until = self.now + STICK_INHIBIT_S
        r = self.auth.on_stick(self.now, self._own_ctx() if self.own else {})
        if r:
            self._do_release(*r)
        return self._world_out

    # ------------------------------------------------------------------ main tick
    def tick(self, own: dict) -> tuple[list[dict], list[tuple[str, dict]]]:
        t_start = time.perf_counter()
        self._world_out, self._radio_out = [], list(self._radio_out)
        self.own, self.now = own, float(own["t"])
        now = self.now
        x, y = to_enu(own["lat"], own["lon"])
        v = own["gs_kt"] * KT
        omega = math.degrees(9.80665 * math.tan(math.radians(own.get("bank_deg", 0.0))) / v) if v > 1 else 0.0
        self.baro_offset_m = (own["alt_msl_ft"] - own["alt_press_ft"]) * FT
        self.own_st = KState(x, y, own["alt_msl_ft"] * FT, v, own["track_deg"], own.get("vs_fpm", 0.0) * FT / 60.0,
                             omega, own["agl_ft"] * FT)
        self.own_cls = self.predictor.classify(self.own_st)
        ext = 0.0
        if self._seq_extend is not None:
            if now - self._seq_extend[0] < 150.0 and not (self.own_cls.leg != self._seq_extend[2]
                                                          and self.own_cls.conf >= CONF_MIN):
                ext = self._seq_extend[1]
            else:
                self._seq_extend = None
        self.own_pred = self.predictor.predict(self.own_st, now, self.own_cls, horizon=OWN_HORIZON_S, extend_s=ext)

        self._periodic_radio(now)
        self._drop_stale(now)
        conflicts, paths = self._assess_peers(now)
        if conflicts and self.first_conflict_seen is None:
            self.first_conflict_seen = now
        self._update_layers(now, conflicts)
        self._act(now, conflicts, paths)
        self._periodic_world(now)
        self.tick_ms.append((time.perf_counter() - t_start) * 1000.0)
        radio_out, self._radio_out = self._radio_out, []
        return self._world_out, radio_out

    # ------------------------------------------------------------------ periodic output
    def _periodic_radio(self, now: float) -> None:
        o, cls, pred = self.own, self.own_cls, self.own_pred
        leg = cls.leg if cls.conf >= CONF_MIN else "UNKNOWN"
        nt = pred.next_turn if pred and pred.method == "turn-aware" else None
        intent = f"{nt[0]}_IN_{int(round(nt[1]))}S" if nt else ""
        if now - self._last["state"] >= STATE_PERIOD_S:
            self._last["state"] = now
            self._emit_radio("STATE", {"lat": o["lat"], "lon": o["lon"], "alt_press_ft": o["alt_press_ft"],
                                       "gs_kt": o["gs_kt"], "track_deg": o["track_deg"], "vs_fpm": o.get("vs_fpm", 0.0),
                                       "leg": leg, "intent": intent, "ap_equipped": bool(o.get("ap_equipped"))})
        if nt and nt[1] <= 20.0 and nt[0] != self._last_intent_leg:
            self._last_intent_leg = nt[0]
            self._emit_radio("INTENT", {"leg": leg, "intent": intent, "valid_for_s": 20.0})
        elif not nt or nt[1] > 25.0:
            self._last_intent_leg = None if not nt else self._last_intent_leg
        if now - self._last["hb"] >= HEARTBEAT_PERIOD_S:
            self._last["hb"] = now
            self._emit_radio("HEARTBEAT", {"alive": True})
        if self.verbose and now - self._last["leglog"] >= 1.0:
            self._last["leglog"] = now
            print(f"[node {self.id}] t={now:.1f} leg={cls.leg} rwy={cls.runway} conf={cls.conf:.2f} "
                  f"method={pred.method} peers={len(self.peers)} level={self._adv['level']}", flush=True)

    def _periodic_world(self, now: float) -> None:
        if now - self._last["trust"] >= TRUST_PERIOD_S:
            self._last["trust"] = now
            ids = sorted(self.peers) + sorted({f"CAM-{s['observer']}" for t_s, s in self.sightings if now - t_s < 6.0})
            self._emit_world(self.trust.frame(self.id, now, ids, self._rels(now)))
        if now - self._last["pred"] >= PRED_PERIOD_S:
            self._last["pred"] = now
            self._emit_world(self.own_pred.to_frame(self.id, to_latlon, now=now))
            for pid in self._near_ids:                  # what this node believes about each peer: the god view draws the turn
                tr = self.peers.get(pid)
                if tr is not None and tr.pred is not None:
                    self._emit_world(tr.pred.to_frame(self.id, to_latlon, target_id=pid, now=now))

    def _rels(self, now: float) -> dict:
        """v1.1 `rel` per peer, from its last STATE dead-reckoned to now: TRUE bearing own->target, range, dalt."""
        out = {}
        o = self.own_st
        for pid, tr in self.peers.items():
            age = max(0.0, now - tr.rx_t + self.latency_est)
            hx, hy = hvec(tr.track)
            x, y = tr.x + hx * tr.gs * age, tr.y + hy * tr.gs * age
            dx, dy = x - o.x, y - o.y
            out[pid] = {"brg_deg": round(bearing_deg(dx, dy), 1), "rng_m": round(math.hypot(dx, dy)),
                        "dalt_ft": round(tr.alt_press_ft + tr.vs / FT * age - self.own["alt_press_ft"]),
                        "trk_deg": round(tr.track, 1), "vs_fpm": round(tr.vs / FT * 60.0)}
        return out

    def _drop_stale(self, now: float) -> None:
        for pid in [p for p, t in self.peers.items() if now - t.rx_t > PEER_TIMEOUT_S]:
            self.peers.pop(pid)
            self.trackers.pop(pid, None)
            self.negotiator.reset(pid)
        self.sightings = [(t, s) for t, s in self.sightings if now - t < 6.0]

    # ------------------------------------------------------------------ peers
    def _on_ground(self, gs: float, z_msl: float) -> bool:
        field = self.patterns[next(iter(self.patterns))].elev_m
        return z_msl < field + 6.0

    def _peer_state(self, tr: PeerTrack) -> KState:
        return KState(tr.x, tr.y, tr.alt_press_ft * FT + self.baro_offset_m, tr.gs, tr.track, tr.vs, tr.turn_rate)

    def _peer_pred(self, tr: PeerTrack) -> Prediction:
        age = max(0.0, self.now - tr.rx_t)
        key = (tr.seq, tr.intent_turn, int(age // 2))
        if tr.pred is None or tr.pred_key != key:
            st = self._peer_state(tr)
            t0 = tr.rx_t - self.latency_est
            declared = tr.leg if tr.leg != "UNKNOWN" else None
            cls = self.predictor.classify(st, declared)
            intent = None
            if tr.intent_turn is not None:
                rel = tr.intent_turn[1] - t0
                if rel > -6.0:
                    intent = f"{tr.intent_turn[0]}_IN_{max(0.0, rel):.1f}S"
            tr.pred = self.predictor.predict(st, t0, cls, horizon=PEER_HORIZON_S, intent=intent,
                                             age_s=max(0.0, age - 1.0))        # >1 s silent: widen sigma
            tr.pred_key = key
        return tr.pred

    def _assess_peers(self, now: float):
        conflicts: dict[str, conflict_mod.Conflict] = {}
        if self._on_ground(self.own_st.gs, self.own_st.z):          # rolling out / parked: nothing airborne to avoid
            return conflicts, {}
        own_xy = (self.own_st.x, self.own_st.y)
        if self.trust.has_scorer:                        # Lane C evidence (signature, kinematics, RF, corroboration)
            for pid in self.peers:
                self.trust.refresh(pid, {})
        usable = {pid: (t.x, t.y) for pid, t in self.peers.items()
                  if self.trust.cap(pid) is not None and not self._on_ground(t.gs, t.alt_press_ft * FT + self.baro_offset_m)}
        near = conflict_mod.k_nearest(own_xy, usable)
        self._near_ids = list(near)
        for pid in near:
            c = conflict_mod.assess(pid, self.own_pred, self._peer_pred(self.peers[pid]), now)
            if c is not None:
                conflicts[pid] = c
        paths = {}
        for pid in usable:
            if math.hypot(usable[pid][0] - own_xy[0], usable[pid][1] - own_xy[1]) <= RELEVANT_RADIUS_M:
                paths[pid] = self._peer_pred(self.peers[pid])
        return conflicts, paths

    def _update_layers(self, now: float, conflicts: dict) -> None:
        for pid in set(self.trackers) | set(conflicts) | set(self.peers):
            trk = self.trackers.setdefault(pid, layers.LayerTracker())
            cap = self.trust.cap(pid) or "CLEAR"
            c = conflicts.get(pid)
            prev = trk.level
            ttc = c.ttc_s if c else None
            pc = self.negotiator.pairs.get(pid)
            if ttc is not None and ttc <= 40.0 and pc is not None and pc.peer_commit is not None                     and now - pc.peer_commit.get("_rx", -1e9) < 5.0:
                ttc = min(ttc, 19.0)          # peer already committed: answer now, do not wait for our own 20 s mark
            trk.update(now, ttc, cap)
            if trk.level == "CLEAR" and prev != "CLEAR":
                self.negotiator.reset(pid)
                self._esc_cache.pop(pid, None)
                self._seq_own.pop(pid, None)
                self._seq_plans.pop(pid, None)

    # ------------------------------------------------------------------ decisions
    def _pick_top(self, conflicts: dict):
        best = None
        for pid, trk in self.trackers.items():
            if trk.level == "CLEAR":
                continue
            c = conflicts.get(pid)
            key = (-layers.RANK[trk.level], c.ttc_s if c else 1e9, pid)
            if best is None or key < best[0]:
                best = (key, pid, trk.level, c)
        return best

    def _act(self, now: float, conflicts: dict, paths: dict) -> None:
        ctx = self._own_ctx()
        top = self._pick_top(conflicts)
        top_pid = top[1] if top else None

        if self.auth.engaged:
            tgt = self._takeover_target
            active = self._engaged_conflict(now, tgt, paths)
            r = self.auth.check(now, ctx, active)
            if r:
                self._do_release(*r)
                top = None if not active else top

        if top is None:
            self._camera_only(now)
            lvl = self._adv["level"]
            if lvl != "CLEAR" and not (lvl == "RELEASE" and now - self._adv["t"] < 3.0):
                self._advise("CLEAR", "CLEAR OF CONFLICT", "clear of conflict", None, None, {"cause": "no predicted conflict"})
            return
        _, pid, level, c = top
        tr = self.peers.get(pid)
        if tr is None or c is None:                    # level held by hysteresis; keep the last advisory
            return
        ttc = c.ttc_s
        reason = c.reason()

        if level == "SEQUENCE":
            self._do_sequence(now, pid, tr, c, reason)
        elif level == "TRAFFIC":
            dx, dy = tr.x - self.own_st.x, tr.y - self.own_st.y
            dz = tr.alt_press_ft * FT + self.baro_offset_m - self.own_st.z
            text, speak = layers.traffic_text(self.own["hdg_deg"], self.own_st.z, dx, dy, dz)
            if self.trust.state(pid) != "TRUSTED":
                text += f" ({self.trust.state(pid)})"
            self._advise("TRAFFIC", text, speak, pid, ttc, reason)
        else:
            self._do_resolve(now, pid, tr, c, level, reason, paths, ctx)
        self._camera_only(now)

    # --- sequencing
    def _do_sequence(self, now, pid, tr, c, reason) -> None:
        own_cls = self.own_cls
        peer_cls = self.predictor.classify(self._peer_state(tr), tr.leg if tr.leg != "UNKNOWN" else None)
        same_pattern = (own_cls.runway is not None and own_cls.runway == peer_cls.runway
                        and own_cls.leg != "UNKNOWN" and peer_cls.leg != "UNKNOWN"
                        and own_cls.conf >= CONF_MIN and peer_cls.conf >= CONF_MIN)
        if not same_pattern:
            self._advise("SEQUENCE", f"POSSIBLE CONFLICT - {pid} - MONITOR",
                         f"possible conflict, {layers.spoken_callsign(pid)}, monitor", pid, c.ttc_s, reason)
            return
        pat = self.patterns[own_cls.runway]
        plan = self._seq_plans.get(pid)
        if plan is None or self.id not in plan.order:
            plan = self._seq_own.get(pid)
        if plan is None:
            plan = layers.sequence_plan(own_cls.runway,
                                        self.id, pat.remaining_path_m(self.own_st.x, self.own_st.y, own_cls.leg), self.own_st.gs,
                                        pid, pat.remaining_path_m(tr.x, tr.y, peer_cls.leg), tr.gs)
            self._seq_own[pid] = plan                        # keep the advice stable until the conflict clears
        if self.id < pid and now - self._seq_proposed.get(pid, -1e9) > 20.0:
            self._seq_proposed[pid] = now
            self._emit_radio("SEQ_PROPOSE", {"runway": plan.runway, "order": plan.order, "extend_s": plan.extend_s})
        text, speak = layers.sequence_text(self.id, plan, own_cls.leg, pid)
        if self.id in plan.extend_s and own_cls.leg in ("UPWIND", "CROSSWIND", "DOWNWIND", "BASE")                 and self._seq_extend is None:
            self._seq_extend = (now, plan.extend_s[self.id], own_cls.leg)   # plan our own path assuming it is flown
        reason = dict(reason, order=plan.order, extend_s=plan.extend_s, gap_m=round(plan.gap_m), method=c.method)
        self._advise("SEQUENCE", text, speak, pid, c.ttc_s, reason)

    # --- resolve / takeover
    def _escape_inputs(self, now: float, paths: dict):
        own = self.own_st
        hold = self.own_pred.at(now + ESC_GRID)[:, :3]
        peers, sig = {}, {}
        for pid, pr in paths.items():
            a = pr.at(now + ESC_GRID)
            peers[pid] = a[:, :3]
            sig[pid] = (a[:, 3], a[:, 4])
        cls = self.own_cls
        tpa = self.patterns[cls.runway].tpa_msl_m if cls.runway in self.patterns else None
        o = escape.OwnState(own.x, own.y, own.z, self.own["hdg_deg"], own.gs, self.own.get("bank_deg", 0.0), own.vs,
                            self.own["ias_kt"], self.own["agl_ft"] * FT, self.da_ft, tpa)
        return o, hold, peers, sig

    def _do_resolve(self, now, pid, tr, c, level, reason, paths, ctx) -> None:
        o, hold, peers, sig = self._escape_inputs(now, paths)
        if pid not in peers:
            return
        peer_state = {"x": tr.x, "y": tr.y, "z": tr.alt_press_ft * FT + self.baro_offset_m,
                      "hdg": tr.track, "gs": tr.gs, "vs": tr.vs}

        # What the bounds monitor would refuse to fly is not offered to an AP-equipped pilot either: the RESOLVE
        # advice, the commit sent to the peer and the later TAKEOVER are then the same maneuver.
        blocked = {}
        neutral = authority.vet({"mode": "TAKEOVER", "bank_cmd_deg": 0.0, "vs_cmd_fpm": 0.0, "hold_s": 10.0}, ctx)
        if ctx["ap_equipped"] and not ctx["stick_active"] and neutral.ok:     # situation-level refusals (e.g. below 300 ft
            for cand in escape.candidates(escape.climb_rate_fpm(self.da_ft), o.bank):
                if cand.kind == "hold":
                    continue
                v = authority.vet({"mode": "TAKEOVER", "bank_cmd_deg": cand.bank, "vs_cmd_fpm": cand.vs_fpm, "hold_s": 10.0}, ctx)
                if not v.ok:
                    blocked[cand.name] = f"bounds: {v.rejected}"

        # FLOCK flies at most 30 deg for 10 s on its own authority; a pilot can be advised up to 45 deg for 15 s
        pilot_mode = not (ctx["ap_equipped"] and not ctx["stick_active"])
        hold_s = PILOT_HOLD_S if pilot_mode else 10.0

        def evaluate(peer_commit):
            pp = dict(peers)
            alt = None
            if peer_commit and not tr.ap_equipped:
                # a pilot may or may not follow its advisory: the main case assumes it holds, and the candidate must
                # also stay safe if it does fly it (two aircraft turning the same way can cancel each other out)
                alt = {pid: escape.peer_commit_path(peer_state, peer_commit, now, peers[pid])}
                peer_commit = None
            if peer_commit:
                pp[pid] = escape.peer_commit_path(peer_state, peer_commit, now, peers[pid])
            comp = bool(peer_commit) and peer_commit.get("sense", "HOLD") != "HOLD"
            return escape.evaluate(o, hold, pp, self.terrain_fn, self.obstacle_fn, sig, hold_s=hold_s,
                                   require_maneuver=comp, blocked=blocked, peers_alt=alt,
                                   max_bank=PILOT_MAX_BANK if pilot_mode else 30.0)

        def expected_peer():
            po = escape.OwnState(tr.x, tr.y, peer_state["z"], tr.track, tr.gs, 0.0, tr.vs, tr.gs / KT, 300.0, self.da_ft,
                                 o.tpa_msl_m)
            php = self._peer_pred(tr).at(now + ESC_GRID)[:, :3]
            oa = self.own_pred.at(now + ESC_GRID)
            r = escape.evaluate(po, php, {self.id: hold}, self.terrain_fn, self.obstacle_fn,
                                {self.id: (oa[:, 3], oa[:, 4])}, hold_s=10.0)
            ch = r.chosen
            return None if ch is None else {"sense": ch.cand.sense, "bank_deg": abs(ch.cand.bank), "start_t": now,
                                            "hold_s": 10.0, "vs_fpm": ch.cand.vs_fpm}

        dec = self.negotiator.decide(now, pid, evaluate, expected_peer, hold_s=hold_s)
        if dec is None:
            self._no_solution(now, pid, c, evaluate(None))
            return
        if dec.commit is not None:
            self._emit_radio("MANEUVER_COMMIT", dec.commit)
        res = dec.result or self._esc_cache.get(pid, (0.0, None))[1]
        if dec.result is not None:
            self._esc_cache[pid] = (now, dec.result)
        ttc = c.ttc_s
        why = dict(res.reason(ttc) if res else {"chosen": dec.cand}, basis=dec.basis, peer_sense=dec.peer_sense, hold_s=hold_s,
                   method=c.method, predicted_miss_ft=round(c.miss_h_ft), confidence=round(c.confidence, 2))
        eligible = (level == "TAKEOVER" and ctx["ap_equipped"] and not ctx["stick_active"]
                    and now >= self._no_takeover_until and self.trust.may_negotiate(pid) and not self.auth.engaged)
        if eligible:
            if now < self._tk_retry_at:          # a refused takeover is re-tried once a second, not every tick
                return
            self._tk_retry_at = now + 1.0
            self._try_takeover(now, pid, c, dec, res, why, ctx, evaluate)
            return
        if self.auth.engaged:
            return
        text, speak = layers.maneuver_text(dec.cand, pid, dec.peer_sense, urgent=(level == "TAKEOVER"))
        self._advise("RESOLVE", text, speak, pid, ttc, why)

    def _try_takeover(self, now, pid, c, dec, res, why, ctx, evaluate) -> None:
        # fresh evaluation at the moment of grant.  We only rely on the peer's committed maneuver if it can fly it
        # by itself (AP-equipped); a pilot may or may not follow an advisory, so otherwise assume it holds course.
        peer_commit = self.negotiator.pair(pid).peer_commit if self.peers[pid].ap_equipped else None
        res = evaluate(peer_commit)
        order = [e for e in res.ranked]
        ranked_first = [e for e in order if e.cand.name == dec.cand] + [e for e in order if e.cand.name != dec.cand]
        rejected_by_auth = {}
        for ev in ranked_first:
            if ev.cand.kind == "hold":
                continue
            cmd = {"type": "COMMAND", "ac_id": self.id, "t": now, "mode": "TAKEOVER", "bank_cmd_deg": ev.cand.bank,
                   "vs_cmd_fpm": ev.cand.vs_fpm, "hold_s": 10.0, "reason": {}}
            verdict = authority.vet(cmd, ctx)
            if not verdict.ok:
                rejected_by_auth[ev.cand.name] = verdict.rejected
                continue
            cmd = verdict.command
            cmd["reason"] = dict(why, chosen=ev.cand.name, rejected=dict(res.rejected, **rejected_by_auth),
                                 clipped=verdict.clipped, ttc_s=round(c.ttc_s, 1))
            if ev.cand.name != dec.cand:         # say so when conditions changed between the advice and the takeover
                cmd["reason"]["changed_from"] = {"advised": dec.cand,
                                                 "why": (res.rejected.get(dec.cand) or rejected_by_auth.get(dec.cand) or "re-evaluated")}
            if ev.cand.name != dec.cand:                   # authority changed the plan: tell the peer
                p = self.negotiator.pair(pid)
                body = {"target": pid, "sense": ev.cand.sense, "bank_deg": abs(ev.cand.bank), "vs_fpm": ev.cand.vs_fpm,
                        "start_t": now, "hold_s": 10.0}
                p.my_commit, p.my_cand = body, ev.cand.name
                self._emit_radio("MANEUVER_COMMIT", body)
            self.auth.grant(cmd, now)
            self._takeover_target = pid
            self._emit_world(cmd)
            side = "LEFT" if cmd["bank_cmd_deg"] < 0 else "RIGHT"
            if ev.cand.kind == "turn":
                text, speak = f"FLOCK HAS THE AIRCRAFT - {side} {abs(cmd['bank_cmd_deg']):.0f}", "flock has the aircraft"
            else:
                text, speak = f"FLOCK HAS THE AIRCRAFT - {ev.cand.name}", "flock has the aircraft"
            self._advise("TAKEOVER", text, speak, pid, c.ttc_s, cmd["reason"], force=True)
            return
        res.rejected.update(rejected_by_auth)
        if res.ranked:
            # a maneuver exists but the bounds monitor will not let FLOCK fly it: warn the pilot, say why
            text, speak = layers.maneuver_text(dec.cand, pid, dec.peer_sense, urgent=True)
            self._advise("RESOLVE", text, speak, pid, c.ttc_s,
                         dict(why, takeover_inhibited=rejected_by_auth, ttc_s=round(c.ttc_s, 1)))
        else:
            self._no_solution(now, pid, c, res)

    def _no_solution(self, now, pid, c, res) -> None:
        if now - self._no_solution_sent.get(pid, -1e9) > 2.0:
            self._no_solution_sent[pid] = now
            self._emit_radio("MANEUVER_COMMIT", {"target": pid, "sense": "HOLD", "bank_deg": 0, "vs_fpm": 0,
                                                 "start_t": now, "hold_s": 10.0})
        text, speak = authority.NO_SOLUTION_TEXT
        why = {"rejected": dict(res.rejected) if res else {}, "ttc_s": round(c.ttc_s, 1), "predicted_miss_ft": round(c.miss_h_ft),
               "method": c.method, "chosen": None}
        self._advise("NO_SOLUTION", text, speak, pid, c.ttc_s, why)

    def _engaged_conflict(self, now: float, target: Optional[str], paths: dict) -> bool:
        """Is the takeover still needed?  Only 'no' once the threat is actually clear *now* (current separation
        >= 1.5x the NMAC box, not converging) -- never because the maneuver is predicted to work, otherwise
        the monitor would hand back control before the maneuver has done anything."""
        cmd = self.auth.active
        if cmd is None or target not in paths:
            return False
        own = self.own_st
        a = paths[target].at(now + np.array([0.0, 0.5]))
        dh = math.hypot(a[0, 0] - own.x, a[0, 1] - own.y)
        dz = abs(a[0, 2] - own.z)
        m_now = max(max(0.0, dh - 0.5 * a[0, 3]) / escape.NMAC_H_M, max(0.0, dz - 0.5 * a[0, 4]) / escape.NMAC_V_M)
        opening = math.hypot(a[1, 0] - own.x, a[1, 1] - own.y) >= dh
        return not (opening and m_now >= escape.MARGIN_OK)

    def _do_release(self, cmd: dict, cause: str) -> None:
        self._emit_world(cmd)
        # never re-grab straight after a release: 5 s after stick/timeout/bounds, 4 s after "conflict clear"
        self._no_takeover_until = max(self._no_takeover_until,
                                      self.now + (4.0 if cause == "conflict clear" else STICK_INHIBIT_S))
        text, speak = authority.release_text(self.own.get("bank_deg", 0.0))
        self._advise("RELEASE", text, speak, self._takeover_target, None, {"cause": cause}, force=True)
        self._takeover_target = None

    # --- camera-only targets
    def _camera_only(self, now: float) -> None:
        for t_rx, s in self.sightings:
            cid = f"CAM-{s['observer']}"
            self.trust.update(cid, s.get("conf", 0.5), [f"sighting:{s['observer']}", f"az {s.get('az_deg', 0):.0f}"], True)
            ttc = s.get("ttc_s")
            obs = self.peers.get(s.get("_from") or s["observer"])
            if ttc is None or ttc > 35.0 or obs is None or now - t_rx > 2.0:
                continue
            brg = (obs.track + s.get("az_deg", 0.0)) % 360.0
            rel = (brg - self.own["hdg_deg"]) % 360.0
            clock = layers.clock_position(self.own["hdg_deg"], math.sin(math.radians(brg)), math.cos(math.radians(brg)))
            right = 30.0 < rel < 170.0
            tail = ("YOU HAVE RIGHT OF WAY - MONITOR" if right else "GIVE WAY - TURN RIGHT")
            self._advise("TRAFFIC", f"TRAFFIC (CAMERA ONLY) - {clock} O'CLOCK - {int(ttc)} S - {tail}",
                         f"traffic, camera only, {clock} o'clock, " + ("monitor" if right else "give way, turn right"),
                         cid, ttc, {"state": "CAMERA_ONLY", "right_of_way": right, "sighting_conf": s.get("conf"),
                                    "method": "camera-bearing"})

    # --- advisory emission
    def _advise(self, level: str, text: str, speak: str, target: Optional[str], ttc: Optional[float],
                reason: dict, force: bool = False) -> None:
        a = self._adv
        changed_level = level != a["level"]
        if not (force or changed_level or (text != a["text"] and self.now - a["t"] >= ADV_MIN_PERIOD_S)):
            return
        a.update(level=level, text=text, t=self.now, target=target)
        self._emit_world({"type": "ADVISORY", "ac_id": self.id, "t": self.now, "layer": layers.LAYER_NUM[level],
                          "level": level, "text": text, "speak": speak, "target_id": target,
                          "ttc_s": None if ttc is None else round(ttc, 1), "reason": reason})


# ---------------------------------------------------------------------- asyncio shell
def _radio_client(ac_id: str, via_channel: Optional[str], direct: bool = False):
    """Lane C's radio/client.py when it exists (signed, slotted, via channel.py), else the loopback stub."""
    try:
        from radio.client import RadioClient
    except Exception:
        from stubs.loopback_radio import RadioClient
        return RadioClient(ac_id=ac_id, via_channel=via_channel)
    return RadioClient(ac_id=ac_id, via_channel=via_channel, direct=direct)


async def run(ac_id: str, world: str, via_channel: Optional[str] = None, verbose: bool = False,
              direct: bool = False) -> None:
    import websockets
    node = Node(ac_id, verbose=verbose)
    radio = _radio_client(ac_id, via_channel, direct)
    evidence = None
    if hasattr(radio, "set_neighbor_report"):                       # the real radio: trust by evidence, not broadcast
        from radio.evidence import TrustEvidence
        evidence = TrustEvidence.attach(radio)
        node.trust.set_scorer(evidence.scorer)
    radio.on_message(node.on_radio)
    await radio.start()
    url = f"{world}?role=node:{ac_id}"
    latest: dict = {}
    async with websockets.connect(url) as ws:
        print(f"[node {ac_id}] connected {url}", flush=True)

        async def send_frames(frames, radios):
            for f in frames:
                await ws.send(json.dumps(f))
            for msg, body in radios:
                await radio.send(msg, body)

        async def reader():
            async for raw in ws:
                m = json.loads(raw)
                if m.get("type") == "OWNSHIP":
                    latest["own"] = m
                elif m.get("type") == "ENV":
                    node.on_env(m)
                elif m.get("type") == "STICK":
                    await send_frames(node.on_stick(m), [])        # release inside the same event, not next tick

        reader_task = asyncio.create_task(reader())
        last_t = None
        try:
            while True:
                await asyncio.sleep(0.1)
                own = latest.get("own")
                if own is None or own["t"] == last_t:
                    continue
                last_t = own["t"]
                if evidence is not None:
                    evidence.on_ownship(own)
                frames, radios = node.tick(own)
                await send_frames(frames, radios)
        finally:
            reader_task.cancel()
            await radio.stop()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", default="N101")
    ap.add_argument("--world", default="ws://localhost:8765")
    ap.add_argument("--via-channel", default=None, help="ws://<ip>:8765 when multicast is blocked")
    ap.add_argument("--direct", action="store_true", help="real radio without channel.py (like the stub; no RF emulation)")
    ap.add_argument("-v", "--verbose", action="store_true", help="log leg classification once per second")
    a = ap.parse_args()
    try:
        asyncio.run(run(a.id, a.world, a.via_channel, a.verbose, a.direct))
    except KeyboardInterrupt:
        pass
