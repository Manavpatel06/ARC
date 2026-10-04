"""
verify/guardrails.py - rules the trust state may never break, applied after fusion.

1. A target our own TCAS currently tracks where it claims to be is never SUSPECT (floor: UNVERIFIED).
   ARC may only downgrade targets known solely through ADS-B / passive data.
2. Not enough evidence (no check with real confidence) -> UNVERIFIED, never SUSPECT and never VERIFIED.
3. VERIFIED needs physical evidence (TCAS agreement or Mode S replies), not just plausible motion.
4. SUSPECT needs at least one strong negative check, not an accumulation of weak doubts.
Own-ship GPS integrity and band-health guardrails hook in here (milestones 3 / 7).
"""
from __future__ import annotations

PHYSICAL = ("tcas_consistency", "modes_presence", "timing_1030")

def apply(state: str, results: dict, cfg: dict) -> tuple[str, list[str]]:
    """results: name -> CheckResult. Returns (state, notes explaining any override)."""
    f = cfg["fusion"]
    notes = []
    confirmed = is_tcas_confirmed(results)
    informative = [r for r in results.values() if r.score is not None and r.confidence >= f["evidence_conf"]]
    if not informative:
        if state != "UNVERIFIED":
            notes.append("not enough evidence yet")
        return "UNVERIFIED", notes or ["not enough evidence yet"]
    if state == "SUSPECT" and confirmed:
        return "UNVERIFIED", ["TCAS tracks it where it claims to be - never marked spoofed"]
    if state == "SUSPECT" and not any(r.score is not None and r.score < 0.5 and r.confidence >= f["strong_conf"]
                                      for r in results.values()):
        return "UNVERIFIED", ["doubts, but no strong evidence of spoofing"]
    if state == "VERIFIED" and not any((results.get(n) and results[n].score is not None and results[n].score >= 0.5)
                                       for n in PHYSICAL):
        return "UNVERIFIED", ["no independent confirmation yet (TCAS / Mode S)"]
    return state, notes

def is_tcas_confirmed(results: dict) -> bool:
    t = results.get("tcas_consistency")
    return bool(t and t.score is not None and (t.evidence.get("tcas_confirmed") or t.evidence.get("tcas_only")))
