"""
verify/schema.py - normalized messages (world sensors -> onboard unit) and the unit's output.

Sensor frame, world -> role avionics:<own id>, ~10 Hz:
  {"type": "SENSORS", "t": <world s>, "ac_id": <own id>, "msgs": [SensorMsg, ...]}
Output, unit -> world -> own cockpit + god + log, 1 Hz:
  {"type": "VERIFY", "ac_id", "t", "targets": [VerifyTarget, ...], "banners": [...]}
Ground truth (simulation only) never travels on these: world -> god/log as GROUND_TRUTH.
"""
from __future__ import annotations
from typing import Literal, Optional

from pydantic import BaseModel

Kind = Literal["ADSB_POSITION", "ADSB_VELOCITY", "MODES_REPLY", "INTERROGATION_1030", "TCAS_TRACK",
               "OWNSHIP_STATE", "BAND_STATS"]
State = Literal["VERIFIED", "UNVERIFIED", "SUSPECT"]

class SensorMsg(BaseModel):
    kind: Kind
    t_ns: int                          # receive time, ns (world clock)
    source: str = "sim"                # sim | sdr | replay | opensky ...
    icao: Optional[str] = None         # 24-bit address, 6 hex chars
    callsign: Optional[str] = None
    # ADS-B claims
    lat: Optional[float] = None
    lon: Optional[float] = None
    alt_ft: Optional[float] = None     # barometric (pressure) altitude as broadcast
    gs_kt: Optional[float] = None
    trk_deg: Optional[float] = None
    vs_fpm: Optional[float] = None
    # measurements made by OUR equipment
    rssi_dbm: Optional[float] = None
    range_m: Optional[float] = None    # TCAS: from reply round-trip time
    brg_deg: Optional[float] = None    # TCAS: TRUE bearing own -> target (sim reports true, not relative)
    df: Optional[int] = None           # Mode S downlink format (4/5/11/20/21)
    raw: Optional[str] = None          # raw frame hex when available (SDR)
    # own-ship / band
    hdg_deg: Optional[float] = None
    gps_ok: Optional[bool] = None
    tcas_ok: Optional[bool] = None
    site: Optional[str] = None         # INTERROGATION_1030: which ground radar
    msgs_per_s: Optional[float] = None
    noise_dbm: Optional[float] = None

class CheckOut(BaseModel):
    score: Optional[float]             # 0 = evidence of spoofing, 1 = evidence it is real, None = no data
    confidence: float
    reason: str
    contribution: float = 0.0          # signed log-odds this check added

class Rel(BaseModel):
    brg_deg: float
    rng_m: float
    dalt_ft: float
    trk_deg: Optional[float] = None
    vs_fpm: Optional[float] = None

class VerifyTarget(BaseModel):
    icao: str
    id: str                            # callsign, else ICAO hex
    callsign: Optional[str] = None
    trust: int                         # 0-100
    state: State
    reasons: list[str]
    checks: dict[str, CheckOut]
    rel: Optional[Rel] = None
    tcas_confirmed: bool = False
    sources: list[str] = []

class Verify(BaseModel):
    type: Literal["VERIFY"] = "VERIFY"
    ac_id: str
    t: float
    targets: list[VerifyTarget]
    banners: list[str] = []
