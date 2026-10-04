"""
node/conflict.py — Lane B (B5).

Pairwise predicted closest approach, own vs each of the k nearest peers.

A conflict is a predicted miss < 500 ft horizontal AND < 100 ft vertical at the same instant
inside the horizon, after taking the prediction sigma off both separations (so a wide, low-confidence
track needs to be "probably" in the box, not just "possibly").  Both paths are sampled on a 0.25 s grid
by linear interpolation, so closing speeds of 100+ m/s still resolve a 150 m box.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from node.geometry import FT
from node.predict import Prediction

NMAC_H_M = 500 * FT
NMAC_V_M = 100 * FT
HORIZON_S = 105.0      # layers act at <= 90 s; the extra 15 s only feeds hysteresis at the edge
GRID_DT = 0.25
K_NEAREST = 6
N_SIGMA = 1.0


@dataclass
class Conflict:
    target: str
    ttc_s: float                  # time until the predicted conflict begins (first instant inside the box)
    tcpa_s: float                 # time of closest approach inside the box
    miss_h_m: float               # raw horizontal separation at that instant
    miss_v_m: float
    sigma_h_m: float              # combined sigma at that instant
    t_enter_s: float              # == ttc_s, kept for readability at call sites
    method: str                   # weakest method of the two predictions
    confidence: float             # min of the two prediction confidences
    ground_only: bool = False     # only both landing rolls overlap on the runway: a spacing problem, not airborne

    @property
    def miss_h_ft(self) -> float:
        return self.miss_h_m / FT

    @property
    def miss_v_ft(self) -> float:
        return self.miss_v_m / FT

    def reason(self) -> dict:
        return {"predicted_miss_ft": round(self.miss_h_ft), "predicted_vsep_ft": round(self.miss_v_ft),
                "sigma_ft": round(self.sigma_h_m / FT), "ttc_s": round(self.ttc_s, 1),
                "tcpa_s": round(self.tcpa_s, 1),
                "method": self.method, "confidence": round(self.confidence, 2)}


def separation_series(own: Prediction, peer: Prediction, now: float, horizon: float = HORIZON_S,
                      dt: float = GRID_DT):
    """Return (tau, dh, dz, sigma_h, sigma_v) on a common grid starting at `now`."""
    tau = np.arange(0.0, horizon + 1e-9, dt)
    a = own.at(now + tau)
    b = peer.at(now + tau)
    dh = np.hypot(a[:, 0] - b[:, 0], a[:, 1] - b[:, 1])
    dz = np.abs(a[:, 2] - b[:, 2])
    sh = np.hypot(np.interp(now + tau, own.pts[:, 0] + own.t0, own.pts[:, 4]),
                  np.interp(now + tau, peer.pts[:, 0] + peer.t0, peer.pts[:, 4]))
    sv = np.hypot(a[:, 4], b[:, 4])
    return tau, dh, dz, sh, sv


def assess(target: str, own: Prediction, peer: Prediction, now: float, horizon: float = HORIZON_S,
           n_sigma: float = N_SIGMA, ground_z: Optional[float] = None) -> Optional[Conflict]:
    """ground_z: altitude (m MSL) at or below which a predicted point is rolling on the runway.  A conflict whose
    every in-box instant has BOTH aircraft on the runway (two landing rolls overlapping) is marked ground_only: the
    node still sequences on it (spacing advice early helps) but never alerts TRAFFIC or maneuvers for it."""
    tau, dh, dz, sh, sv = separation_series(own, peer, now, horizon)
    h_eff = np.maximum(0.0, dh - n_sigma * sh)
    v_eff = np.maximum(0.0, dz - n_sigma * sv)
    inside = (h_eff < NMAC_H_M) & (v_eff < NMAC_V_M)
    if not inside.any():
        return None
    ground_only = False
    if ground_z is not None:
        za = np.interp(now + tau, own.pts[:, 0] + own.t0, own.pts[:, 3])
        zb = np.interp(now + tau, peer.pts[:, 0] + peer.t0, peer.pts[:, 3])
        ground_only = not (inside & ((za > ground_z) | (zb > ground_z))).any()
    raw = np.where(inside, np.maximum(dh / NMAC_H_M, dz / NMAC_V_M), np.inf)       # true closest approach in the box
    margin = raw
    # Escalation runs on the time the conflict *begins* (monotone, well-conditioned even when two aircraft
    # stay inside the box together); the miss reported is at the closest approach inside the box.
    i = int(np.argmin(margin))
    t_enter = float(tau[int(np.argmax(inside))])
    method = "turn-aware" if own.method == peer.method == "turn-aware" else "straight-line"
    return Conflict(target, t_enter, float(tau[i]), float(dh[i]), float(dz[i]), float(sh[i]), t_enter, method,
                    min(own.confidence, peer.confidence), ground_only)


def k_nearest(own_xy: tuple[float, float], peers: dict[str, tuple[float, float]], k: int = K_NEAREST) -> list[str]:
    ranked = sorted(peers, key=lambda pid: (peers[pid][0] - own_xy[0]) ** 2 + (peers[pid][1] - own_xy[1]) ** 2)
    return ranked[:k]
