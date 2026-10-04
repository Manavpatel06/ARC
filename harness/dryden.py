"""
harness/dryden.py — Dryden continuous gust model (MIL-F-8785C / MIL-HDBK-1797, low altitude).  ADD-ON: nothing in
world/ imports it.  Used by the offline Monte Carlo (harness/simworld.py, opt-in `turbulence=`) and by the optional live
launcher harness/dryden_world.py, which layers it on top of the world's own weather at run time.

Low-altitude form (h = height AGL in ft, clamped to 10..1000 ft; above 1000 ft the 1000 ft values are kept, which is
the conservative end for a traffic pattern):

    sigma_w = 0.1 * W20                     L_w = h
    sigma_u = sigma_v = sigma_w / (0.177 + 0.000823 h) ** 0.4
    L_u = L_v = h / (0.177 + 0.000823 h) ** 1.2

    W20 (wind speed at 20 ft) = 15 kt light, 30 kt moderate, 45 kt severe.

Spectra -> time filters at true airspeed V (tau = L / V):
    u      first-order Gauss-Markov, exact discretisation
    v, w   (1 + sqrt(3) tau s) / (1 + tau s)^2  =  sqrt(3) / (1 + tau s) + (1 - sqrt(3)) / (1 + tau s)^2,
           driven by white noise of intensity sigma^2 tau (variance of the output = sigma^2).

Deterministic per seed.  Returns body-axis gusts in m/s: u along the flight path, v to the right, w up.
"""
from __future__ import annotations

import math
import random

FT = 0.3048
KT = 0.514444
W20_KT = {0: 0.0, 1: 15.0, 2: 30.0, 3: 45.0}
SQ3 = math.sqrt(3.0)


def scales(h_ft: float, level: int) -> dict:
    """sigma (m/s) and length scale (m) per axis at this height and intensity."""
    h = max(10.0, min(1000.0, h_ft))
    k = 0.177 + 0.000823 * h
    sw = 0.1 * W20_KT.get(int(level), 0.0) * KT
    su = sw / k ** 0.4
    lu = h / k ** 1.2 * FT
    return {"sigma_u": su, "sigma_v": su, "sigma_w": sw, "L_u": lu, "L_v": lu, "L_w": h * FT}


class Dryden:
    def __init__(self, seed: int = 0):
        self.rng = random.Random(seed)
        self.u = 0.0
        self.v1 = self.v2 = 0.0
        self.w1 = self.w2 = 0.0

    def _second(self, y1: float, y2: float, sigma: float, tau: float, dt: float) -> tuple[float, float, float]:
        a = math.exp(-dt / tau)
        n = self.rng.gauss(0.0, 1.0) * sigma * math.sqrt(tau / dt)      # white noise, intensity sigma^2 tau
        y1 = a * y1 + (1.0 - a) * n
        y2 = a * y2 + (1.0 - a) * y1
        return y1, y2, SQ3 * y1 + (1.0 - SQ3) * y2

    def step(self, dt: float, v_ms: float, h_ft: float, level: int) -> tuple[float, float, float]:
        if not level:
            self.u = self.v1 = self.v2 = self.w1 = self.w2 = 0.0
            return 0.0, 0.0, 0.0
        s = scales(h_ft, level)
        v = max(10.0, v_ms)
        tau_u = max(2.0 * dt, s["L_u"] / v)
        a = math.exp(-dt / tau_u)
        self.u = a * self.u + s["sigma_u"] * math.sqrt(1.0 - a * a) * self.rng.gauss(0.0, 1.0)
        self.v1, self.v2, gv = self._second(self.v1, self.v2, s["sigma_v"], max(2.0 * dt, s["L_v"] / v), dt)
        self.w1, self.w2, gw = self._second(self.w1, self.w2, s["sigma_w"], max(2.0 * dt, s["L_w"] / v), dt)
        return self.u, gv, gw
