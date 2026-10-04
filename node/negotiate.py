"""
node/negotiate.py — Lane B (B7, B12).

Pairwise maneuver negotiation over MANEUVER_COMMIT.

  * Lower ID commits first with the best sense assuming the peer keeps its predicted path.
  * Higher ID picks the best sense given the lower ID's commit.  Because both nodes run the same
    deterministic evaluator on the same shared tracks, the higher ID also *pre-computes* what the lower ID
    will choose and commits straight away (basis "precomputed"); if the real commit later disagrees it
    re-decides and sends a revised commit.  This is what keeps the two commits inside one radio latency.
  * Lost link (nothing heard from the peer for LINK_TIMEOUT_S; any radio message counts as an ack of life):
    both fall back to sense R, 30 deg (14 CFR 91.113 head-on rule), so both sides reach the same answer
    without talking.

Decisions are sticky: once a commit is out for a target it only changes on new information
(peer commit arrived/changed, link lost).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from node.escape import MARGIN_OK, EscapeResult, Evaluation

LINK_TIMEOUT_S = 3.0
FALLBACK_NAME = "R30"


@dataclass
class Decision:
    target: str
    cand: str                       # escape candidate name, e.g. "L30"
    sense: str                      # L | R | CLIMB | DESCEND | HOLD
    basis: str                      # lower-first | responding | precomputed | fallback-link-lost | fallback-no-ack
    peer_sense: Optional[str]
    result: Optional[EscapeResult]
    commit: Optional[dict]          # MANEUVER_COMMIT body to send now (None = nothing new)


@dataclass
class _Pair:
    peer_commit: Optional[dict] = None
    peer_version: int = 0
    seen_version: int = -1
    my_commit: Optional[dict] = None
    my_cand: Optional[str] = None
    my_basis: str = ""
    committed_at: float = 0.0
    last_tx: float = -1e9
    fallback: Optional[str] = None


class Negotiator:
    def __init__(self, own_id: str):
        self.own_id = own_id
        self.pairs: dict[str, _Pair] = {}
        self.last_rx: dict[str, float] = {}

    def pair(self, target: str) -> _Pair:
        return self.pairs.setdefault(target, _Pair())

    def note_rx(self, peer: str, now: float) -> None:
        self.last_rx[peer] = now

    def link_lost(self, peer: str, now: float) -> bool:
        t = self.last_rx.get(peer)
        return t is None or now - t > LINK_TIMEOUT_S

    def on_commit(self, peer: str, body: dict, now: float) -> None:
        p = self.pair(peer)
        if p.peer_commit is None or p.peer_commit.get("sense") != body.get("sense") or \
                p.peer_commit.get("bank_deg") != body.get("bank_deg"):
            p.peer_version += 1
        p.peer_commit = dict(body, _rx=now)

    def reset(self, target: str) -> None:
        self.pairs.pop(target, None)

    @staticmethod
    def _body(target: str, ev: Evaluation, now: float, hold_s: float) -> dict:
        c = ev.cand
        return {"target": target, "sense": c.sense, "bank_deg": abs(c.bank), "vs_fpm": c.vs_fpm,
                "start_t": now, "hold_s": hold_s}

    def decide(self, now: float, target: str, evaluate: Callable[[Optional[dict]], EscapeResult],
               expected_peer: Callable[[], Optional[dict]], hold_s: float = 10.0,
               i_go_first: Optional[bool] = None, stand_on: bool = False) -> Optional[Decision]:
        """
        evaluate(peer_commit_or_None) -> EscapeResult for me given the peer's (assumed or real) maneuver.
        expected_peer() -> the commit body the lower-ID peer is expected to choose (higher ID only), or None.
        Returns None when there is no feasible candidate at all (caller raises NO_SOLUTION).
        """
        p = self.pair(target)
        # 14 CFR 91.113: the give-way aircraft commits first, the stand-on aircraft responds; when the rules do not
        # give one clear answer (head-on, ambiguous) both fall back to the old lower-ID-first ordering
        lower = (self.own_id < target) if i_go_first is None else i_go_first
        lost = self.link_lost(target, now)
        if lost:
            basis = "fallback-link-lost"
            if p.my_cand in (FALLBACK_NAME, "HOLD") and p.my_basis in (basis, basis + "-stand-on"):
                return Decision(target, p.my_cand, p.my_commit["sense"] if p.my_commit else "R", p.my_basis, "R", None, None)
            res = evaluate({"sense": "R", "bank_deg": 30.0, "start_t": now, "hold_s": hold_s})
            if stand_on:
                # the give-way peer falls back to R30 by the same rule; the stand-on aircraft keeps its course if that
                # is safe with full margin against the peer's fallback turn, else it turns right too
                h = next((e for e in res.evals if e.cand.kind == "hold"), None)
                if h is not None and h.feasible and h.margin >= MARGIN_OK:
                    return self._finish(p, target, h, res, basis + "-stand-on", "R", now, hold_s)
            ev = next((e for e in res.evals if e.cand.name == FALLBACK_NAME), None)
            if ev is None or not ev.feasible:
                return self._finish(p, target, None, res, basis, "R", now, hold_s)
            return self._finish(p, target, ev, res, basis, "R", now, hold_s)

        if p.my_commit is not None and p.seen_version == p.peer_version:
            return Decision(target, p.my_cand, p.my_commit["sense"], p.my_basis,
                            (p.peer_commit or {}).get("sense"), None, None)

        if p.peer_commit is not None:
            peer_c, basis = p.peer_commit, ("responding" if not lower else "lower-first")
            if p.my_commit is not None:
                # we already advised/committed: keep it if it still clears with the peer's maneuver on top,
                # so the pilot is not handed a different instruction every time a commit arrives
                res_keep = evaluate(peer_c)
                mine = next((e for e in res_keep.evals if e.cand.name == p.my_cand), None)
                p.seen_version = p.peer_version
                if mine is not None and mine.feasible and mine.margin >= MARGIN_OK:
                    return Decision(target, p.my_cand, p.my_commit["sense"], p.my_basis, peer_c.get("sense"), res_keep, None)
        elif not lower:
            peer_c, basis = expected_peer(), "precomputed"
        else:
            peer_c, basis = None, "lower-first"
        res = evaluate(peer_c)
        p.seen_version = p.peer_version
        ev = res.chosen
        return self._finish(p, target, ev, res, basis, (peer_c or {}).get("sense"), now, hold_s)

    def _finish(self, p: _Pair, target: str, ev: Optional[Evaluation], res: EscapeResult, basis: str,
                peer_sense: Optional[str], now: float, hold_s: float) -> Optional[Decision]:
        if ev is None:
            return None
        body = self._body(target, ev, now, hold_s)
        changed = p.my_commit is None or p.my_commit.get("sense") != body["sense"] or \
            p.my_commit.get("bank_deg") != body["bank_deg"]
        if changed:
            p.my_commit, p.my_cand, p.my_basis, p.committed_at = body, ev.cand.name, basis, now
        return Decision(target, p.my_cand, p.my_commit["sense"], basis, peer_sense, res, body if changed else None)
