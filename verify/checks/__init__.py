"""
verify/checks - one module per verification check, all with the same interface:

    check(track: Track, ctx: Ctx, cfg: dict) -> CheckResult

score in [0, 1] (0 = evidence the target is spoofed, 1 = evidence it is real) or None when the check
does not apply / has too little data. None never counts against a target ("unverified" is not "spoofed").
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

from verify.checks import kinematics, modes_presence, tcas_consistency   # noqa: E402

CHECKS = {
    "tcas_consistency": tcas_consistency.check,
    "modes_presence": modes_presence.check,
    "kinematics": kinematics.check,
}
