"""
FLOCK shared schemas (pydantic v2). Import this everywhere; do not fork it.
Change only by team agreement. See INTERFACE.md for semantics.
"""
from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel, Field

# ---------- world <-> node ----------
class Ownship(BaseModel):
    type: Literal["OWNSHIP"] = "OWNSHIP"
    ac_id: str
    t: float
    lat: float
    lon: float
    alt_msl_ft: float
    alt_press_ft: float
    agl_ft: float
    gs_kt: float
    track_deg: float
    hdg_deg: float
    bank_deg: float
    vs_fpm: float
    ias_kt: float
    ap_equipped: bool = False
    stick_active: bool = False
    flaps: int = 0

Level = Literal["SEQUENCE", "TRAFFIC", "RESOLVE", "TAKEOVER", "RELEASE", "NO_SOLUTION", "CLEAR"]

class Advisory(BaseModel):
    type: Literal["ADVISORY"] = "ADVISORY"
    ac_id: str
    t: float
    layer: int = Field(ge=0, le=4)
    level: Level
    text: str
    speak: Optional[str] = None
    target_id: Optional[str] = None
    ttc_s: Optional[float] = None
    reason: dict = {}

class Bounds(BaseModel):
    max_bank_deg: float = 30.0
    min_ias_kt: float = 62.0       # 1.3 * Vs1 (48 kt) for a C172S-class aircraft
    min_agl_ft: float = 300.0      # no automatic action below this on final
    max_hold_s: float = 10.0

class Command(BaseModel):
    type: Literal["COMMAND"] = "COMMAND"
    ac_id: str
    t: float
    mode: Literal["TAKEOVER", "RELEASE"]
    bank_cmd_deg: float = 0.0
    vs_cmd_fpm: float = 0.0
    hold_s: float = 0.0
    bounds: Bounds = Bounds()
    reason: dict = {}

class TrustTarget(BaseModel):
    id: str
    score: float = Field(ge=0, le=1)
    state: Literal["TRUSTED", "SUSPICIOUS", "FAKE", "CAMERA_ONLY"]
    evidence: list[str] = []

class Trust(BaseModel):
    type: Literal["TRUST"] = "TRUST"
    ac_id: str
    t: float
    targets: list[TrustTarget] = []

class Stick(BaseModel):
    type: Literal["STICK"] = "STICK"
    ac_id: str
    t: float

class Input(BaseModel):
    type: Literal["INPUT"] = "INPUT"
    ac_id: str
    roll: float = Field(ge=-1, le=1)
    pitch: float = Field(ge=-1, le=1)
    throttle: float = Field(ge=0, le=1)

class Log(BaseModel):
    type: Literal["LOG"] = "LOG"
    src: str
    kind: Literal["radio", "decision", "world", "camera"]
    t: float
    payload: dict

# ---------- radio ----------
Msg = Literal["STATE", "INTENT", "SEQ_PROPOSE", "SEQ_ACCEPT", "MANEUVER_COMMIT", "SIGHTING", "HEARTBEAT"]
Leg = Literal["UPWIND", "CROSSWIND", "DOWNWIND", "BASE", "FINAL", "STRAIGHT_IN", "GO_AROUND", "UNKNOWN"]

class StateBody(BaseModel):
    lat: float
    lon: float
    alt_press_ft: float
    gs_kt: float
    track_deg: float
    vs_fpm: float
    leg: Leg = "UNKNOWN"
    intent: str = ""
    ap_equipped: bool = False

class IntentBody(BaseModel):
    leg: Leg
    intent: str
    valid_for_s: float = 20

class SeqProposeBody(BaseModel):
    runway: str
    order: list[str]
    extend_s: dict[str, float] = {}

class SeqAcceptBody(BaseModel):
    proposal_seq: int

class ManeuverCommitBody(BaseModel):
    target: str
    sense: Literal["L", "R", "CLIMB", "DESCEND", "HOLD"]
    bank_deg: float = 0
    vs_fpm: float = 0
    start_t: float
    hold_s: float = 10

class SightingBody(BaseModel):
    observer: str
    az_deg: float
    el_deg: float = 0
    size_px: float
    growth_px_s: float
    ttc_s: Optional[float] = None
    conf: float = Field(ge=0, le=1)

class HeartbeatBody(BaseModel):
    alive: bool = True

class RadioMsg(BaseModel):
    msg: Msg
    from_: str = Field(alias="from")
    seq: int
    t: float
    sig: str = ""
    body: dict

    model_config = {"populate_by_name": True}

# ---------- scenarios ----------
class AircraftSpec(BaseModel):
    id: str
    start: dict
    ap: bool = False
    human: bool = False
    camera: bool = False
    flock: bool = True          # False = aircraft with no FLOCK node at all

class Scenario(BaseModel):
    name: str
    aircraft: list[AircraftSpec]
    channel: dict = {"loss": 0.1, "latency_s": 0.3}
    weather: str = "cached"
    spoofer: bool = False
    camera_target: bool = False
