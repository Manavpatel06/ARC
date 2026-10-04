"""
harness/baseline.py — Lane B.

The comparison logic for the Monte Carlo: the *same* node (same thresholds, same sigma growth, same radio,
same bounds monitor, same layer timings) with the two things ARC adds taken away:

  * prediction is straight-line only  (Predictor(conf_min=2.0) can never reach the turn-aware branch)
  * the maneuver is fixed: turn right 30 deg, no Escape Field, no negotiation, no sequencing

So any difference in the chart is attributable to turn-aware prediction + escape/negotiation.
"""
from __future__ import annotations

from node import authority, escape, layers
from node.node import Node
from node.predict import Predictor

FIXED = escape.Candidate("R30", "turn", 30.0, 0.0, 30.0 / 45.0)


class BaselineNode(Node):
    def __init__(self, ac_id: str, **kw):
        super().__init__(ac_id, **kw)
        # same sigma growth as ARC's turn-aware track, so only the geometry model differs
        self.predictor = Predictor(self.patterns, conf_min=2.0, line_sigma_k=0.8)

    def _do_sequence(self, now, pid, tr, c, reason) -> None:        # no sequencing layer in the baseline
        return

    def _do_resolve(self, now, pid, tr, c, level, reason, paths, ctx) -> None:
        why = dict(reason, chosen=FIXED.name, rejected={}, basis="fixed-maneuver", peer_sense=None)
        eligible = (level == "TAKEOVER" and ctx["ap_equipped"] and not ctx["stick_active"]
                    and now >= self._no_takeover_until and not self.auth.engaged)
        if eligible:
            cmd = {"type": "COMMAND", "ac_id": self.id, "t": now, "mode": "TAKEOVER", "bank_cmd_deg": 30.0,
                   "vs_cmd_fpm": 0.0, "hold_s": 10.0, "reason": why}
            v = authority.vet(cmd, ctx)
            if v.ok:
                self.auth.grant(v.command, now)
                self._takeover_target = pid
                self._emit_world(v.command)
                self._advise("TAKEOVER", "ARC HAS THE AIRCRAFT - RIGHT 30", "arc has the aircraft", pid, c.ttc_s, why, force=True)
                return
        if self.auth.engaged:
            return
        text, speak = layers.maneuver_text("R30", pid, None, urgent=(level == "TAKEOVER"))
        self._advise("RESOLVE", text, speak, pid, c.ttc_s, why)
