"""
harness/rf_sim.py — simulated radio-physics measurements for AirWitness (V2 prototype, Lane B).

Real RF (two-way ranging, angle of arrival, synchronized TDOA, stable Doppler) needs hardware we do not have; laptops
and ESP32s do not give aviation-grade multilateration.  This module produces the SAME RFMeasurement objects that
real hardware would, from the TRUE transmitter position plus realistic noise, so the trust engine can be built and
tested now and the simulator swapped for hardware later without touching node/airwitness.py.

    m = measure(observer="N101", obs_pos=(x, y, z), target="GHOST7", tx_pos=(x, y, z), t=now, kinds=("range", "bearing"))
    aw.ingest_rf(m)
"""
from __future__ import annotations

import math
import random
from typing import Iterable, Optional

from node.airwitness import RFMeasurement

C = 299_792_458.0
RANGE_SIGMA_M = 15.0          # authenticated two-way ranging (UWB-class; aspirational for aviation links)
BEARING_SIGMA_DEG = 5.0       # small antenna array angle of arrival
TDOA_SIGMA_NS = 30.0          # GNSS-disciplined receivers (~9 m)
DOPPLER_SIGMA_HZ = 3.0
RSSI_SIGMA_DB = 3.0
FREQ_HZ = 915e6


def _dist(a, b) -> float:
    return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))


def measure(observer: str, obs_pos: tuple, target: str, tx_pos: tuple, t: float, kinds: Iterable[str] = ("range",),
            rng: Optional[random.Random] = None, ref_pos: Optional[tuple] = None, tx_vel: tuple = (0.0, 0.0, 0.0),
            obs_vel: tuple = (0.0, 0.0, 0.0), source_id: Optional[str] = None) -> RFMeasurement:
    """One measurement by `observer` of a transmission physically sent from tx_pos (whatever the packet claims)."""
    rng = rng or random.Random(0)
    d = _dist(obs_pos, tx_pos)
    m = RFMeasurement(observer, target, t, obs_pos, observer_vel=obs_vel, source_id=source_id)
    kinds = set(kinds)
    if "range" in kinds:
        m.estimated_range_m, m.range_sigma_m = d + rng.gauss(0, RANGE_SIGMA_M), RANGE_SIGMA_M
    if "rssi" in kinds:                                   # free-space RSSI turned back into a (log-normal) range
        m.estimated_range_m = d * 10 ** (rng.gauss(0, RSSI_SIGMA_DB) / 20.0)
        m.range_log_sigma_db = RSSI_SIGMA_DB
    if "bearing" in kinds:
        b = math.degrees(math.atan2(tx_pos[0] - obs_pos[0], tx_pos[1] - obs_pos[1])) % 360.0
        m.bearing_deg, m.bearing_sigma_deg = (b + rng.gauss(0, BEARING_SIGMA_DEG)) % 360.0, BEARING_SIGMA_DEG
    if "tdoa" in kinds and ref_pos is not None:
        m.tdoa_ns = (d - _dist(ref_pos, tx_pos)) / C * 1e9 + rng.gauss(0, TDOA_SIGMA_NS)
        m.tdoa_sigma_ns, m.ref_pos = TDOA_SIGMA_NS, ref_pos
    if "doppler" in kinds:
        u = [(tx_pos[i] - obs_pos[i]) / (d or 1.0) for i in range(3)]
        rdot = sum(u[i] * (tx_vel[i] - obs_vel[i]) for i in range(3))
        m.doppler_hz, m.doppler_sigma_hz = -FREQ_HZ * rdot / C + rng.gauss(0, DOPPLER_SIGMA_HZ), DOPPLER_SIGMA_HZ
    return m
