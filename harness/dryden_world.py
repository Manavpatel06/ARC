"""
harness/dryden_world.py — ADD-ON live launcher: the normal world server with Dryden gusts layered in at run time.

Nothing in world/ is edited.  This script imports the world, swaps the CONTINUOUS part of its turbulence for the
MIL-HDBK-1797 Dryden model (harness/dryden.py) and then runs world/world_server.py's own main() with the same
arguments.  The world's bank swell, discrete bumps, METAR gusts, wind shear and thermals stay exactly as they are.
Turbulence level comes from the world's own weather (preset, or the god view's Turbulence selector).

    python harness/dryden_world.py --scenario harness/scenarios/judges.json --weather hot_gusty_afternoon
    (then radio/channel.py, node/node.py ... exactly as run_demo.ps1 starts them)

Without this launcher the world behaves exactly as before.
"""
from __future__ import annotations

import asyncio
import math
import os
import sys
import zlib

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from harness.dryden import Dryden

FPM_PER_MS = 60.0 / 0.3048
KT = 0.514444
PATTERN_TAS_MS = 46.0             # ~90 kt: the Dryden time scales use the airspeed of a pattern aircraft
RESPONSE_TAU_S = 1.5              # airframe response to horizontal gusts (inertia), as the world's own model assumes


def install() -> None:
    """Patch world.weather.Turbulence (continuous w / u from Dryden) and world.flight_model.Aircraft (side gust)."""
    import world.flight_model as fm
    import world.weather as wxm

    Turb = wxm.Turbulence
    if getattr(Turb, "_dryden_installed", False):
        return
    orig_step, orig_lag2, orig_ac_step = Turb.step, Turb._lag2, fm.Aircraft.step

    def step(self, wx, dt, t, agl_ft, *a, **k):
        self._dry_ctx = (wx.turbulence, agl_ft, dt)
        return orig_step(self, wx, dt, t, agl_ft, *a, **k)

    def _lag2(self, key, sigma, dt):
        if key == "bank" or not hasattr(self, "_dry_ctx"):
            return orig_lag2(self, key, sigma, dt)
        lvl, agl, _ = self._dry_ctx
        if not hasattr(self, "_dry"):
            self._dry = Dryden(zlib.crc32(str(id(self)).encode()) ^ 0x5EED)
        if key == "w":                                  # Turbulence.step asks for bank, w, u in that order
            gu, gv, gw = self._dry.step(dt, PATTERN_TAS_MS, agl, lvl)
            k = min(1.0, dt / RESPONSE_TAU_S)            # the airframe's mass smooths horizontal gusts
            self._dry_u = getattr(self, "_dry_u", 0.0) + (gu - getattr(self, "_dry_u", 0.0)) * k
            self._dry_v = getattr(self, "_dry_v", 0.0) + (gv - getattr(self, "_dry_v", 0.0)) * k
            return gw * FPM_PER_MS
        if key == "u":
            return getattr(self, "_dry_u", 0.0) / KT
        return orig_lag2(self, key, sigma, dt)

    def ac_step(self, dt, env, now):
        orig_ac_step(self, dt, env, now)
        tb = self._turb
        side = getattr(tb, "_dry_v", 0.0) if tb is not None else 0.0
        if side and not self.on_ground and env.wx is not None and env.wx.turbulence:
            h = math.radians(self.hdg_deg)
            self.lat, self.lon = fm.move(self.lat, self.lon, side * math.cos(h) * dt, -side * math.sin(h) * dt)

    Turb.step, Turb._lag2, fm.Aircraft.step = step, _lag2, ac_step
    Turb._dryden_installed = True


if __name__ == "__main__":
    install()
    from world import world_server
    print("[world] Dryden gust add-on active (harness/dryden_world.py)")
    try:
        asyncio.run(world_server.main())
    except KeyboardInterrupt:
        pass
