"""
node/authority.py — Lane B (B9).  The bounds monitor.

Deliberately small and independent: it imports nothing from predict/escape/layers.  The smart logic
proposes a COMMAND; this module is the only thing allowed to let it out, and the only thing that ends it.

Printed bounds (schemas.Bounds): max bank 30 deg, speed floor 1.3*Vs (62 kt), no automatic action below
300 ft AGL on final, no automatic descent below pattern altitude - 300 ft, max hold 10 s, release on any
stick input.  Anything outside the bounds is clipped and logged; if it cannot be made legal it is rejected,
and the caller then falls through to the next candidate or to the NO_SOLUTION advisory.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from schemas import Bounds

FT = 0.3048
MAX_CLIMB_FPM = 1000.0
MAX_DESCENT_FPM = 500.0


@dataclass
class Verdict:
    ok: bool
    command: Optional[dict]
    clipped: list = field(default_factory=list)       # human-readable list of what was clipped
    rejected: Optional[str] = None


def vet(cmd: dict, ctx: dict, bounds: Optional[Bounds] = None) -> Verdict:
    """
    cmd: COMMAND dict (mode TAKEOVER) with bank_cmd_deg, vs_cmd_fpm, hold_s.
    ctx: ias_kt, agl_ft, alt_msl_ft, leg, tpa_msl_ft, ap_equipped, stick_active.
    """
    b = bounds or Bounds()
    out = dict(cmd)
    clipped: list[str] = []
    if cmd.get("mode") == "RELEASE":
        return Verdict(True, out)
    if not ctx.get("ap_equipped", False):
        return Verdict(False, None, rejected="no autopilot equipped")
    if ctx.get("stick_active", False):
        return Verdict(False, None, rejected="pilot is on the stick")
    if ctx.get("ias_kt", 999.0) < b.min_ias_kt:
        return Verdict(False, None, rejected=f"IAS {ctx['ias_kt']:.0f} kt below floor {b.min_ias_kt:.0f} kt")
    if ctx.get("leg") in ("FINAL", "GO_AROUND") and ctx.get("agl_ft", 9999.0) < b.min_agl_ft:
        return Verdict(False, None, rejected=f"below {b.min_agl_ft:.0f} ft AGL on final - no automatic action")

    bank = float(cmd.get("bank_cmd_deg", 0.0))
    if abs(bank) > b.max_bank_deg:
        clipped.append(f"bank {bank:+.0f} -> {max(-b.max_bank_deg, min(b.max_bank_deg, bank)):+.0f} (max {b.max_bank_deg:.0f})")
        bank = max(-b.max_bank_deg, min(b.max_bank_deg, bank))
    vs = float(cmd.get("vs_cmd_fpm", 0.0))
    if vs > MAX_CLIMB_FPM or vs < -MAX_DESCENT_FPM:
        new = max(-MAX_DESCENT_FPM, min(MAX_CLIMB_FPM, vs))
        clipped.append(f"vs {vs:+.0f} -> {new:+.0f} fpm")
        vs = new
    hold = float(cmd.get("hold_s", b.max_hold_s))
    if hold > b.max_hold_s or hold <= 0.0:
        new = min(max(hold, 1.0), b.max_hold_s)
        clipped.append(f"hold {hold:.0f} -> {new:.0f} s (max {b.max_hold_s:.0f})")
        hold = new
    if vs < 0.0:
        tpa = ctx.get("tpa_msl_ft")
        if tpa is not None:
            floor = tpa - 300.0
            if ctx.get("alt_msl_ft", 1e9) + vs * hold / 60.0 < floor:
                return Verdict(False, None, rejected=f"descent would go below pattern altitude - 300 ft ({floor:.0f} ft MSL)")
    out.update(bank_cmd_deg=bank, vs_cmd_fpm=vs, hold_s=hold, bounds=b.model_dump())
    return Verdict(True, out, clipped)


class AuthorityMonitor:
    """Tracks one granted TAKEOVER and says when it must end."""

    def __init__(self, ac_id: str, bounds: Optional[Bounds] = None):
        self.ac_id = ac_id
        self.bounds = bounds or Bounds()
        self.active: Optional[dict] = None
        self.granted_t: float = 0.0

    @property
    def engaged(self) -> bool:
        return self.active is not None

    def grant(self, cmd: dict, now: float) -> None:
        self.active, self.granted_t = cmd, now

    def check(self, now: float, ctx: dict, conflict_active: bool) -> Optional[tuple[dict, str]]:
        """Return (RELEASE command, cause) when the takeover must end, else None.  Call every tick."""
        if self.active is None:
            return None
        if ctx.get("stick_active", False):
            cause = "stick"
        elif now - self.granted_t >= min(float(self.active.get("hold_s", self.bounds.max_hold_s)), self.bounds.max_hold_s):
            cause = "hold timeout"
        elif ctx.get("ias_kt", 999.0) < self.bounds.min_ias_kt:
            cause = "speed floor"
        elif ctx.get("leg") in ("FINAL", "GO_AROUND") and ctx.get("agl_ft", 9999.0) < self.bounds.min_agl_ft:
            cause = "min AGL on final"
        elif not conflict_active:
            cause = "conflict clear"
        else:
            return None
        return self.release(now, cause, ctx)

    def on_stick(self, now: float, ctx: Optional[dict] = None) -> Optional[tuple[dict, str]]:
        if self.active is None:
            return None
        return self.release(now, "stick", ctx or {})

    def release(self, now: float, cause: str, ctx: dict) -> tuple[dict, str]:
        self.active = None
        cmd = {"type": "COMMAND", "ac_id": self.ac_id, "t": now, "mode": "RELEASE", "bank_cmd_deg": 0.0,
               "vs_cmd_fpm": 0.0, "hold_s": 0.0, "bounds": self.bounds.model_dump(), "reason": {"cause": cause}}
        return cmd, cause


def release_text(bank_deg: float) -> tuple[str, str]:
    """Announcement at release: continue the turn the pilot now has, or just hand back."""
    if bank_deg > 3.0:
        return "YOUR AIRCRAFT - CONTINUE RIGHT TURN", "your aircraft, continue right turn"
    if bank_deg < -3.0:
        return "YOUR AIRCRAFT - CONTINUE LEFT TURN", "your aircraft, continue left turn"
    return "YOUR AIRCRAFT", "your aircraft"


NO_SOLUTION_TEXT = ("NO SAFE MANEUVER - YOUR AIRCRAFT", "no safe maneuver, your aircraft")
