"""
node/trust.py — Lane B (B11).

Interface between Lane C's trust evidence and the node's decisions.

  score >= 0.7        TRUSTED       may trigger RESOLVE / TAKEOVER, may negotiate
  0.4 <= score < 0.7  SUSPICIOUS    TRAFFIC advisories only
  score < 0.4         FAKE          ignored for conflict logic (still listed in TRUST for the cockpit badge)
  camera sighting     CAMERA_ONLY   TRAFFIC + unilateral right-of-way avoidance, never negotiation

Until Lane C's radio/evidence.py lands every radio peer is hard-coded TRUSTED (lane brief, Phase 1).
When it lands, call `table.set_scorer(fn)` with fn(peer_id, track_dict) -> (score, [evidence strings]);
or push results straight in with `table.update(...)`.  `track_dict` carries lat/lon/alt/gs/track/seq/age_s.
"""
from __future__ import annotations

from typing import Callable, Optional

TRUSTED_MIN = 0.7
SUSPICIOUS_MIN = 0.4

# highest advisory level a target in this state may drive
LEVEL_CAP = {"TRUSTED": "TAKEOVER", "SUSPICIOUS": "TRAFFIC", "CAMERA_ONLY": "TRAFFIC", "FAKE": None}


def state_for(score: float, camera_only: bool = False) -> str:
    if camera_only:
        return "CAMERA_ONLY"
    if score >= TRUSTED_MIN:
        return "TRUSTED"
    if score >= SUSPICIOUS_MIN:
        return "SUSPICIOUS"
    return "FAKE"


class TrustTable:
    def __init__(self, default_score: float = 1.0):
        self.default_score = default_score
        self._ev: dict[str, tuple[float, list, bool]] = {}
        self._scorer: Optional[Callable] = None

    def set_scorer(self, fn: Callable) -> None:
        self._scorer = fn

    def update(self, peer: str, score: float, evidence: Optional[list] = None, camera_only: bool = False) -> None:
        self._ev[peer] = (max(0.0, min(1.0, score)), list(evidence or []), camera_only)

    @property
    def has_scorer(self) -> bool:
        return self._scorer is not None

    def refresh(self, peer: str, track: dict) -> None:
        if self._scorer is not None:
            score, ev = self._scorer(peer, track)
            cam = self._ev.get(peer, (0, [], False))[2]
            self.update(peer, score, ev, cam)

    def get(self, peer: str) -> tuple[float, list, bool]:
        return self._ev.get(peer, (self.default_score, ["plausible"] if self._scorer is None else [], False))

    def state(self, peer: str) -> str:
        score, _, cam = self.get(peer)
        return state_for(score, cam)

    def cap(self, peer: str) -> Optional[str]:
        return LEVEL_CAP[self.state(peer)]

    def may_negotiate(self, peer: str) -> bool:
        return self.state(peer) == "TRUSTED"

    def frame(self, ac_id: str, now: float, peers: list[str], rels: Optional[dict] = None) -> dict:
        """TRUST frame (contract v1.1): every target carries `rel` when the node has a position for it."""
        targets = []
        for pid in peers:
            score, ev, cam = self.get(pid)
            t = {"id": pid, "score": round(score, 2), "state": state_for(score, cam), "evidence": ev}
            if rels and pid in rels:
                t["rel"] = rels[pid]
            targets.append(t)
        return {"type": "TRUST", "ac_id": ac_id, "t": now, "targets": targets}
