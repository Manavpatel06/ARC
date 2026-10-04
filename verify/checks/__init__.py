"""
verify/checks - one module per verification check, all with the same interface:

    check(track: Track, ctx: Ctx, cfg: dict) -> CheckResult

score in [0, 1] (0 = evidence the target is spoofed, 1 = evidence it is real) or None when the check
does not apply / has too little data. None never counts against a target ("unverified" is not "spoofed").

Spec checks: tcas_consistency (1), modes_presence (2), timing_1030 (3), rssi (4), emitter_cluster (5),
kinematics (6), replay_detect (7). popin is ported from Lane B's AirWitness (weak on its own).
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class CheckResult:
    score: Optional[float]
    confidence: float
    reason: str
    evidence: dict = field(default_factory=dict)

    @staticmethod
    def none(reason: str, **ev) -> "CheckResult":
        return CheckResult(None, 0.0, reason, ev)

@dataclass
class Ctx:
    t: float                     # evaluation time (world s)
    own: object                  # tracks.Own
    radar_t: float               # last ground-radar activity heard
    tcas_t: float                # last TCAS track of anyone
    tm: object = None            # tracks.TrackManager (checks that compare targets with each other)
    t0: float = 0.0              # when this unit started listening
    own_uncertain: bool = False  # own-ship GPS integrity degraded: widen position tolerances
    band_degraded: bool = False  # 1090 jammed / congested: absence of a signal proves nothing

    @property
    def pos_scale(self) -> float:
        return 4.0 if self.own_uncertain else 2.0 if self.band_degraded else 1.0      # sparse reports when jammed

from verify.checks import (emitter_cluster, kinematics, modes_presence, popin, replay_detect,  # noqa: E402
                           rssi, tcas_consistency, timing_1030)

CHECKS = {
    "tcas_consistency": tcas_consistency.check,
    "modes_presence": modes_presence.check,
    "timing_1030": timing_1030.check,
    "rssi": rssi.check,
    "emitter_cluster": emitter_cluster.check,
    "kinematics": kinematics.check,
    "replay_detect": replay_detect.check,
    "popin": popin.check,
}
