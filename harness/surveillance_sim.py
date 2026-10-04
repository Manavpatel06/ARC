"""
harness/surveillance_sim.py — SIMULATED independent surveillance for AirWitness-Hybrid (Lane B).

FLOCK never interrogates anything.  On real aircraft these inputs would come read-only from avionics the aircraft
already has (a TCAS / ACAS unit's traffic file, a 1090 MHz receiver).  The current stack has none of that, so this
module produces the SAME objects from the TRUE position of a real transmitter (or from nothing, for a ghost), and
node/airwitness.py consumes them through add_tcas / add_mode_s / add_interrogation_reply unchanged.

    m = tcas(own_pos, truth_pos, t, rng, target_id="N204")     # what an on-board TCAS would measure
    node.on_surveillance(m)
"""
from __future__ import annotations

import math
import random
from typing import Optional

from node.airwitness import InterrogationReplyEvidence, ModeSObservation, TCASMeasurement

TCAS_RANGE_SIGMA_M = 60.0          # ~0.03 NM: TCAS range from reply timing
TCAS_BEARING_SIGMA_DEG = 10.0      # TCAS directional antenna: coarse bearing
TCAS_ALT_SIGMA_M = 30.0            # Mode C 100 ft / Mode S 25 ft quantization
TCAS_MAX_RANGE_M = 14 * 1852.0
REPLY_DELAY_S = 128e-6             # Mode S reply turnaround (128 us)


def tcas(own_pos: tuple, truth_pos: tuple, t: float, rng: Optional[random.Random] = None,
         target_id: Optional[str] = None) -> Optional[TCASMeasurement]:
    """TCAS measurement of a REAL transponder at truth_pos (a ghost has no transponder: call nothing)."""
    rng = rng or random.Random(0)
    dx, dy, dz = (truth_pos[i] - own_pos[i] for i in range(3))
    r = math.sqrt(dx * dx + dy * dy + dz * dz)
    if r > TCAS_MAX_RANGE_M:
        return None
    return TCASMeasurement(t=t, range_m=r + rng.gauss(0, TCAS_RANGE_SIGMA_M),
                           bearing_deg=(math.degrees(math.atan2(dx, dy)) + rng.gauss(0, TCAS_BEARING_SIGMA_DEG)) % 360.0,
                           relative_altitude_m=dz + rng.gauss(0, TCAS_ALT_SIGMA_M), range_sigma_m=TCAS_RANGE_SIGMA_M,
                           bearing_sigma_deg=TCAS_BEARING_SIGMA_DEG, alt_sigma_m=TCAS_ALT_SIGMA_M, target_id=target_id)


def mode_s(target_id: str, t: float, rssi_dbm: float = -75.0) -> ModeSObservation:
    """Passive 1090 MHz activity from a real transponder whose address matches target_id."""
    return ModeSObservation(t=t, target_id=target_id, rssi_dbm=rssi_dbm)


def interrogation_reply(t: float, target_id: str, confidence: float = 0.8) -> InterrogationReplyEvidence:
    """Someone else's 1030 MHz interrogation heard at t, followed by the target's compatible 1090 MHz reply."""
    return InterrogationReplyEvidence(interrogation_time=t, reply_time=t + REPLY_DELAY_S, target_id=target_id,
                                      confidence=confidence)
