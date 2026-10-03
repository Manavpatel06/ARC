"""
FLOCK shared schemas (pydantic v2). Import this everywhere; do not fork it.
Change only by team agreement. See INTERFACE.md for semantics.
v1.1 (Sat 12:20): additive only — TrustTarget.rel, Hello, Truth, Prediction, SetDA, Crystal.
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

class Rel(BaseModel):
    """Where the NODE believes the target is, relative to own aircraft (from the peer's STATE). v1.1"""
    brg_deg: float              # TRUE bearing own -> target, 0-360 (cockpit rotates by own heading)
    rng_m: float                # horizontal range
    dalt_ft: float              # target altitude minus own (pressure altitude)
    trk_deg: Optional[float] = None   # target track, for a trend line
    vs_fpm: Optional[float] = None    # target vertical speed, for the up/down arrow

class TrustTarget(BaseModel):
    id: str
    score: float = Field(ge=0, le=1)
    state: Literal["TRUSTED", "SUSPICIOUS", "FAKE", "CAMERA_ONLY"]
    evidence: list[str] = []
    rel: Optional[Rel] = None   # v1.1: radar position; None only if the node has no position for it

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

class Hello(BaseModel):
    """World -> any client, first frame after connect. v1.1. Tells a cockpit page which aircraft it flies."""
    type: Literal["HELLO"] = "HELLO"
    role: str                   # resolved role, e.g. "cockpit:N101"
    ac_id: Optional[str] = None
    scenario: str = ""
    aircraft: list[dict] = []   # [{"id","human","ap","flock"}] — ids/flags only, no positions

class Truth(BaseModel):
    """World -> god + channel ONLY (never node/cockpit), 10 Hz. v1.1"""
    type: Literal["TRUTH"] = "TRUTH"
    t: float
    aircraft: list[Ownship]

class PredPoint(BaseModel):
    t: float                    # seconds ahead of the frame time
    lat: float
    lon: float
    alt_msl_ft: float
    sigma_m: float = 0

class Prediction(BaseModel):
    """Node -> world -> god, optional, <= 1 Hz per node. v1.1"""
    type: Literal["PREDICTION"] = "PREDICTION"
    ac_id: str                  # whose prediction (the node that made it)
    target_id: Optional[str] = None   # None = own path; else the peer being predicted
    t: float
    method: str                 # "turn-aware" | "straight-line" | ...
    confidence: float = Field(ge=0, le=1)
    leg: Optional[str] = None
    path: list[PredPoint]

class SetDA(BaseModel):
    """God view -> world. Overrides density altitude for climb capability. v1.1"""
    type: Literal["SET_DA"] = "SET_DA"
    ft: float

class CrystalPoint(BaseModel):
    bank_deg: float
    vs_fpm: float
    lat: float
    lon: float
    alt_msl_ft: float
    safe: bool
    why: str = ""               # "", "traffic", "terrain", "obstacle", "performance", "bounds"

class Crystal(BaseModel):
    """Node -> world -> god, Phase 3 (Escape Crystal). v1.1"""
    type: Literal["CRYSTAL"] = "CRYSTAL"
    ac_id: str
    t: float
    mfi: float = Field(ge=0, le=1)    # Maneuver Freedom Index = safe / total
    points: list[CrystalPoint] = []

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
