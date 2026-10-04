"""
world/flight_model.py — Lane A. 3-DOF point-mass model for a C172S-class aircraft.

State: lat, lon, alt_msl_ft, ias_kt, hdg_deg, bank_deg, vs_fpm (+ derived gs/track from wind).
Every aircraft (human, AI, under takeover) flies through the same physics; only the source of
the targets (bank, vs, ias) differs:
  1. COMMAND TAKEOVER  — if ap_equipped and the stick is not active
  2. HUMAN input       — once a cockpit has sent INPUT for this aircraft
  3. autopilot          — the pattern pilot from world/traffic.py (AI aircraft, and human
                          aircraft until their pilot first touches the controller)
Limits applied to every source: bank rate 15 deg/s, speed envelope 48-140 kt,
climb capability from density altitude (data.metar.climb_fpm: 730 / 500 / 300 fpm at 0 / 5,000 / 8,000 ft).
INPUT meaning is pinned in INTERFACE.md v1.1 (target bank = roll x 45 deg, target vs, target IAS 60-120 kt).

Ground: below 0.5 ft AGL the aircraft is on its wheels: wings level, the bank target steers the
nosewheel, IAS may fall to 0 (brakes), wind only adds along the heading, and it can only lift off
at or above ROTATE_KT. Touchdown / liftoff / stop are queued in `events` for the world to publish.
Pilot on the ground: on a RUNWAY, idle throttle (< 15 %) or the brake input stops the aircraft
(wheel brakes), more throttle rolls for takeoff. OFF the runway (env.runway_at says no) rough
ground drags it to a stop and it cannot take off again; reset it from the cockpit.

Weather (world/weather.py, env.wx): wind varies with height + low-level shear, gusts, turbulence and
thermals. A change in the along-heading wind changes IAS at once (inertia) and the pilot/autopilot
then recovers it; turbulence adds bank / airspeed upsets and vertical air motion; thermals lift.
Altimeter: pilots and the autopilot hold INDICATED altitude with their own setting (baro_set_inhg);
alt_press_ft (transponder) uses the 29.92 datum, so it shows true separation regardless.

Autopilot button (cockpit AP message): engage_ap() hands a human aircraft back to its pattern
autopilot (world/traffic.py), which levels, joins the circuit and lands to a full stop; engaging
again on the ground takes off. Any stick movement disconnects it (pilot always wins).
"""
from __future__ import annotations
import math, zlib
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from data.metar import climb_fpm
from world.weather import Turbulence, Weather

G = 9.80665
KT = 0.514444            # m/s per knot
FT = 0.3048              # m per ft
M_PER_DEG_LAT = 111_320.0

BANK_RATE_DPS = 15.0
VS1_KT, VMAX_KT = 48.0, 140.0
HUMAN_MAX_BANK = 45.0     # judges may bank harder than the automation's 30 deg bound
CMD_MAX_BANK = 30.0
CMD_MAX_HOLD_S = 10.0
MAX_DESCENT_FPM = 1_500.0
ACCEL_KT_S = 2.0          # how fast IAS follows its target
VS_RATE_FPM_S = 600.0     # how fast VS follows its target
STICK_DEADZONE = 0.1       # |roll| or |pitch| above this = pilot on the stick (INTERFACE v1.1)
THROTTLE_IAS = (60.0, 120.0)
STICK_TIMEOUT_S = 1.0
ROTATE_KT = 55.0           # no liftoff below this
GROUND_ACCEL_KT_S = 3.0    # full-power takeoff roll
BRAKE_KT_S = 5.0           # braking / rollout
STEER_DPS = 12.0           # nosewheel steering at full deflection (taxi speed and above)
HARD_LANDING_FPM = 800.0
IDLE_THROTTLE = 0.15       # pilot throttle below this on a runway = idle + wheel brakes
ROUGH_GROUND_KT_S = 6.0    # off-runway deceleration (no takeoff possible)
AP_REQUIRES_EQUIPMENT = True   # AI aircraft need ap_equipped for the AP button; judge (human) aircraft always get it
                               # as a sim convenience. ARC takeover (apply_command) still requires ap_equipped.

def climb_capability_fpm(da_ft: float) -> float:
    """Max sustained climb (fpm) vs density altitude — Lane D's table, one source for everyone."""
    return climb_fpm(da_ft)

def tas_kt(ias_kt: float, da_ft: float) -> float:
    """Rule of thumb: TAS = IAS + 2 % per 1,000 ft of density altitude."""
    return ias_kt * (1.0 + 0.02 * max(0.0, da_ft) / 1_000.0)

def move(lat: float, lon: float, east_m: float, north_m: float) -> tuple[float, float]:
    """Flat-earth displacement (fine within ~15 mi of KDVT)."""
    return (lat + north_m / M_PER_DEG_LAT,
            lon + east_m / (M_PER_DEG_LAT * math.cos(math.radians(lat))))

def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x

def wrap180(a: float) -> float:
    return (a + 180.0) % 360.0 - 180.0

@dataclass
class Env:
    """World conditions shared by all aircraft."""
    field_elev_ft: float = 1_478.0
    da_field_ft: float = 4_083.0          # density altitude at field elevation
    wind_e_ms: float = 0.0                # wind velocity (blowing TOWARD), data.metar.wind_vector_ms
    wind_n_ms: float = 0.0
    terrain_ft: Callable[[float, float], float] = lambda lat, lon: 1_478.0
    wx: Optional[Weather] = None          # full weather model; None = steady wind_e/n_ms only
    ref_lat: float = 33.688301            # local frame origin for thermals
    ref_lon: float = -112.083
    runway_at: Optional[Callable[["Aircraft"], Optional[str]]] = None   # runway ident under the aircraft, if any

    @property
    def qnh_inhg(self) -> float:
        return self.wx.qnh_inhg if self.wx else 29.92

    def da_at(self, alt_msl_ft: float) -> float:
        # DA rises ~1:1 with altitude above the field (standard lapse assumed above the field)
        return self.da_field_ft + (alt_msl_ft - self.field_elev_ft)

class Autopilot(Protocol):
    def targets(self, ac: "Aircraft", env: Env, now: float) -> tuple[float, float, float]:
        """Return (bank_deg, vs_fpm, ias_kt) targets."""
        ...

@dataclass
class Aircraft:
    id: str
    lat: float
    lon: float
    alt_msl_ft: float
    ias_kt: float
    hdg_deg: float
    ap_equipped: bool = False
    human: bool = False
    camera: bool = False
    arc: bool = True
    bank_deg: float = 0.0
    vs_fpm: float = 0.0
    flaps: int = 0
    autopilot: Optional[Autopilot] = None
    # derived each step
    gs_kt: float = 0.0
    track_deg: float = 0.0
    agl_ft: float = 0.0
    # human input
    has_pilot: bool = False          # True after the first INPUT; until then the autopilot flies it
    stick_active: bool = False
    last_stick_t: float = -1e9
    inp_roll: float = 0.0
    inp_pitch: float = 0.0
    inp_throttle: float = 0.5
    inp_brake: bool = False
    surface: Optional[str] = None          # runway ident while on the ground on a runway, "OFF" off it
    # autopilot button + ground state
    ap_engaged: bool = False
    on_ground: bool = False
    events: list = field(default_factory=list)     # ("TOUCHDOWN", vs_fpm) / ("LIFTOFF", ias) / ("AP_DISCONNECT", why)
    _last_air_vs: float = 0.0                      # sink rate just before touchdown
    # takeover
    cmd: Optional[dict] = None
    cmd_until: float = 0.0
    # per-aircraft altimeter-setting error (deterministic from the id), +-50 ft
    alt_err_ft: float = field(default=0.0)
    # weather state
    baro_set_inhg: float = 29.92          # altimeter setting dialled in (stale if QNH changes)
    qnh_inhg: float = 29.92               # actual QNH last seen by the physics
    vs_air_fpm: float = 0.0               # vertical air motion (turbulence + thermals)
    vsi_fpm: float = 0.0                  # vertical speed indicator: lags ~1 s like a real VSI
    bank_ctl_deg: float = 0.0             # bank the pilot / autopilot is holding; bank_deg = this + turbulence roll
    _bank_upset: float = 0.0
    taws_alert: Optional[str] = None      # set by the world (world/taws.py) for judge aircraft
    turb_now: float = 0.0                 # 0..1 how rough it is right now (cockpit rumble)
    _turb: Optional[Turbulence] = field(default=None, repr=False)
    _prev_tail_ms: Optional[float] = None
    _prev_upset: tuple = (0.0, 0.0)

    def __post_init__(self):
        if self.alt_err_ft == 0.0:
            self.alt_err_ft = (zlib.crc32(self.id.encode()) % 101) - 50.0
        self.gs_kt, self.track_deg = self.ias_kt, self.hdg_deg
        self._turb = Turbulence(self.id)
        self.bank_ctl_deg = self.bank_deg

    @property
    def indicated_ft(self) -> float:
        """What this aircraft's altimeter shows (pilot / autopilot hold this)."""
        return self.alt_msl_ft + (self.baro_set_inhg - self.qnh_inhg) * 1000.0

    # ---------- inputs ----------
    def apply_input(self, roll: float, pitch: float, throttle: float, now: float, brake: bool = False) -> bool:
        """Store cockpit input. Returns True if this input means 'pilot is on the stick'."""
        self.has_pilot = True
        self.inp_brake = bool(brake)
        self.inp_roll, self.inp_pitch, self.inp_throttle = clamp(roll, -1, 1), clamp(pitch, -1, 1), clamp(throttle, 0, 1)
        moving = abs(self.inp_roll) > STICK_DEADZONE or abs(self.inp_pitch) > STICK_DEADZONE
        if moving:
            self.last_stick_t = now
            self.stick_active = True
            if self.ap_engaged:                      # like a real autopilot: stick input disconnects
                self.ap_engaged = False
                self.events.append(("AP_DISCONNECT", "stick"))
        return moving

    def engage_ap(self, on: bool) -> tuple[bool, str]:
        """Cockpit AP button. Returns (ok, reason)."""
        if not on:
            was = self.ap_engaged
            self.ap_engaged = False
            return True, "disconnected" if was else "already off"
        if self.autopilot is None or not hasattr(self.autopilot, "engage"):
            return False, "no autopilot for this aircraft"
        if AP_REQUIRES_EQUIPMENT and not self.ap_equipped and not self.human:
            return False, "no autopilot installed (ap_equipped=false)"
        self.has_pilot = True
        self.ap_engaged = True
        self.stick_active = False
        return True, self.autopilot.engage(self)

    def apply_command(self, cmd: dict, now: float) -> bool:
        """TAKEOVER is applied only if ap_equipped and no stick. Returns applied."""
        if cmd.get("mode") == "RELEASE":
            self.cmd = None
            return True
        if not self.ap_equipped or self.stick_active:
            return False
        bounds = cmd.get("bounds") or {}
        hold = min(float(cmd.get("hold_s", CMD_MAX_HOLD_S)), float(bounds.get("max_hold_s", CMD_MAX_HOLD_S)), CMD_MAX_HOLD_S)
        self.cmd = cmd
        self.cmd_until = now + hold
        return True

    @property
    def mode(self) -> str:
        if self.cmd is not None:
            return "COMMAND"
        if self.human and self.has_pilot and not self.ap_engaged:
            return "HUMAN"
        return "AUTOPILOT" if self.autopilot else "HOLD"

    # ---------- physics ----------
    def _targets(self, env: Env, now: float) -> tuple[float, float, float]:
        if self.cmd is not None:
            b = self.cmd.get("bounds") or {}
            max_bank = min(CMD_MAX_BANK, float(b.get("max_bank_deg", CMD_MAX_BANK)))
            min_ias = float(b.get("min_ias_kt", 62.0))
            bank = clamp(float(self.cmd.get("bank_cmd_deg", 0.0)), -max_bank, max_bank)
            vs = float(self.cmd.get("vs_cmd_fpm", 0.0))
            return bank, vs, max(self.ias_kt, min_ias)
        if self.human and self.has_pilot and not self.ap_engaged:
            bank = self.inp_roll * HUMAN_MAX_BANK
            vs = self.inp_pitch * (climb_capability_fpm(env.da_at(self.alt_msl_ft)) if self.inp_pitch > 0 else 1_000.0)
            ias = THROTTLE_IAS[0] + self.inp_throttle * (THROTTLE_IAS[1] - THROTTLE_IAS[0])   # 0.5 -> 90 kt
            return bank, vs, ias
        if self.autopilot is not None:
            return self.autopilot.targets(self, env, now)
        return 0.0, 0.0, self.ias_kt

    def step(self, dt: float, env: Env, now: float) -> None:
        if self.cmd is not None and now >= self.cmd_until:
            self.cmd = None
        if self.stick_active and now - self.last_stick_t > STICK_TIMEOUT_S:
            self.stick_active = False

        bank_t, vs_t, ias_t = self._targets(env, now)
        prev_on_ground = self.on_ground                        # for TOUCHDOWN / LIFTOFF transitions
        was_on_ground = self.agl_ft <= 0.5 and self.vs_fpm <= 0.0   # wheels carry the aircraft this step
        da = env.da_at(self.alt_msl_ft)
        hu, hn = math.sin(math.radians(self.hdg_deg)), math.cos(math.radians(self.hdg_deg))
        wx = env.wx
        self.qnh_inhg = env.qnh_inhg
        w_air = 0.0
        if wx is not None:
            we, wn = wx.wind_vec(self.agl_ft)
            if not was_on_ground:
                tb, tw, tu, gust, rough = self._turb.step(wx, dt, now, self.agl_ft)
                wmag = math.hypot(we, wn)
                if gust and wmag > 0.1:                              # gusts along the mean wind
                    we, wn = we * (1 + gust * KT / wmag), wn * (1 + gust * KT / wmag)
                e = (self.lon - env.ref_lon) * M_PER_DEG_LAT * math.cos(math.radians(env.ref_lat))
                n = (self.lat - env.ref_lat) * M_PER_DEG_LAT
                w_air = tw + wx.thermal_fpm(e, n, self.agl_ft, now)
                # upsets: apply the change in the noise so the pilot / autopilot can correct it
                pb, pu = self._prev_upset
                self._bank_upset = tb               # rolls the aircraft on top of what the pilot holds
                self.ias_kt = max(VS1_KT - 4.0, self.ias_kt + tu - pu)     # no stall model: floor just below Vs1
                self._prev_upset = (tb, tu)
                self.turb_now = rough
            else:
                self._prev_upset, self.turb_now, self._bank_upset = (0.0, 0.0), 0.0, 0.0
        else:
            we, wn = env.wind_e_ms, env.wind_n_ms
        # inertia: a sudden headwind loss / tailwind gain costs airspeed until the engine recovers it
        tail = we * hu + wn * hn
        if not was_on_ground and self._prev_tail_ms is not None:
            self.ias_kt = max(VS1_KT - 4.0, self.ias_kt - (tail - self._prev_tail_ms) / KT)
        self._prev_tail_ms = tail
        self.vs_air_fpm = w_air

        if was_on_ground:
            # wheels on the runway: wings level, bank target = nosewheel steering, brakes to 0 kt
            self.bank_ctl_deg += clamp(-self.bank_ctl_deg, -BANK_RATE_DPS * dt, BANK_RATE_DPS * dt)
            self.bank_deg = self.bank_ctl_deg
            steer = clamp(bank_t / 30.0, -1.0, 1.0) * STEER_DPS * clamp(self.ias_kt / 15.0, 0.0, 1.0)
            self.hdg_deg = (self.hdg_deg + steer * dt) % 360.0
            rwy = env.runway_at(self) if env.runway_at else "RWY"
            self.surface = rwy or "OFF"
            decel, can_fly = BRAKE_KT_S, True
            if not rwy:                                       # off the runway: rough ground, nobody takes off
                ias_t, decel, can_fly = 0.0, ROUGH_GROUND_KT_S, False
            elif self.mode == "HUMAN" and (self.inp_brake or self.inp_throttle < IDLE_THROTTLE):
                ias_t = 0.0                                   # idle / brakes: stop on the runway
            ias_t = clamp(ias_t, 0.0, VMAX_KT)
            was_rolling = self.ias_kt >= 0.5
            self.ias_kt = max(0.0, self.ias_kt + clamp(ias_t - self.ias_kt, -decel * dt, GROUND_ACCEL_KT_S * dt))
            if was_rolling and self.ias_kt < 0.5:
                self.ias_kt = 0.0
                self.events.append(("STOPPED", rwy))
            vs_t = clamp(vs_t, 0.0, climb_capability_fpm(da)) if (self.ias_kt >= ROTATE_KT and can_fly) else 0.0
            self.vs_fpm = max(0.0, self.vs_fpm + clamp(vs_t - self.vs_fpm, -VS_RATE_FPM_S * dt, VS_RATE_FPM_S * dt))
            # rolling along the heading; wind only adds its along-heading component
            g = max(0.0, tas_kt(self.ias_kt, da) * KT + we * hu + wn * hn)
            ge, gn = g * hu, g * hn
            self.gs_kt, self.track_deg = g / KT, self.hdg_deg
        else:
            # bank: rate limited
            self.bank_ctl_deg += clamp(bank_t - self.bank_ctl_deg, -BANK_RATE_DPS * dt, BANK_RATE_DPS * dt)
            self.bank_deg = self.bank_ctl_deg + self._bank_upset
            # speed envelope (airborne)
            ias_t = clamp(ias_t, VS1_KT, VMAX_KT)
            self.ias_kt += clamp(ias_t - self.ias_kt, -ACCEL_KT_S * dt, ACCEL_KT_S * dt)
            # vertical speed limited by climb capability at current density altitude
            vs_t = clamp(vs_t, -MAX_DESCENT_FPM, climb_capability_fpm(da))
            self.vs_fpm += clamp(vs_t - self.vs_fpm, -VS_RATE_FPM_S * dt, VS_RATE_FPM_S * dt)

            # turn: rate = g * tan(bank) / V (true airspeed)
            v = tas_kt(self.ias_kt, da) * KT
            turn_rate = math.degrees(G * math.tan(math.radians(self.bank_deg)) / v) if v > 1 else 0.0
            self.hdg_deg = (self.hdg_deg + turn_rate * dt) % 360.0

            # ground velocity = air velocity + wind vector
            ae, an = v * math.sin(math.radians(self.hdg_deg)), v * math.cos(math.radians(self.hdg_deg))
            ge, gn = ae + we, an + wn
            self.gs_kt = math.hypot(ge, gn) / KT
            self.track_deg = math.degrees(math.atan2(ge, gn)) % 360.0
        self.lat, self.lon = move(self.lat, self.lon, ge * dt, gn * dt)

        self.alt_msl_ft += (self.vs_fpm + w_air) * dt / 60.0
        ground = env.terrain_ft(self.lat, self.lon)
        sink_fpm = self.vs_fpm + w_air
        if was_on_ground and self.vs_fpm <= 0.0:
            # rolling: the wheels follow the ground, also downhill (real runways slope - 25L drops ~38 ft),
            # otherwise each terrain step leaves the aircraft "airborne" and the airborne speed floor kicks in
            self.alt_msl_ft = min(self.alt_msl_ft, ground)
        if self.alt_msl_ft <= ground:         # no sinking into terrain
            self.alt_msl_ft = ground
            self.vs_fpm = max(0.0, self.vs_fpm)
        self.agl_ft = self.alt_msl_ft - ground
        self.on_ground = self.agl_ft <= 0.5
        if not self.on_ground:
            self.surface = None
        if self.on_ground and not prev_on_ground:
            self.events.append(("TOUCHDOWN", round(min(sink_fpm, self._last_air_vs))))
        elif prev_on_ground and not self.on_ground:
            self.events.append(("LIFTOFF", round(self.ias_kt)))
        if not self.on_ground:
            self._last_air_vs = self.vs_fpm + w_air
        self.vsi_fpm += (self.vs_fpm + w_air - self.vsi_fpm) * min(1.0, dt / 1.0)

    @property
    def ap_phase(self) -> Optional[str]:
        """Autopilot phase while the AP button is engaged (LEVEL/JOIN/PATTERN/ROLLOUT/STOPPED/TAKEOFF)."""
        if not self.ap_engaged or self.cmd is not None:
            return None
        return getattr(self.autopilot, "phase", None)

    # ---------- output ----------
    def ownship(self, now: float) -> dict:
        """OWNSHIP frame (schemas.Ownship)."""
        return {"type": "OWNSHIP", "ac_id": self.id, "t": round(now, 3),
                "lat": round(self.lat, 6), "lon": round(self.lon, 6),
                "alt_msl_ft": round(self.alt_msl_ft, 1),
                "alt_press_ft": round(self.alt_msl_ft + (29.92 - self.qnh_inhg) * 1000.0 + self.alt_err_ft, 1),
                "agl_ft": round(self.agl_ft, 1),
                "gs_kt": round(self.gs_kt, 1), "track_deg": round(self.track_deg, 1),
                "hdg_deg": round(self.hdg_deg, 1), "bank_deg": round(self.bank_deg, 1),
                "vs_fpm": round(self.vsi_fpm), "ias_kt": round(self.ias_kt, 1),
                "ap_equipped": self.ap_equipped, "stick_active": self.stick_active, "flaps": self.flaps}

    def truth(self, now: float) -> dict:
        """TRUTH entry for god/channel: OWNSHIP + world-only fields."""
        d = self.ownship(now)
        leg = getattr(self.autopilot, "leg", None)
        d.update({"human": self.human, "camera": self.camera, "arc": self.arc,
                  "mode": self.mode, "leg": leg, "on_ground": self.on_ground,
                  "ap_phase": self.ap_phase})
        return d
