"""
world/attacks.py - spoofing attacks injected into the SIMULATED radio environment (never broadcast).

An attack is a transmitter at one (true) place that broadcasts claims about another, or something that
degrades what our own equipment receives. world/sensors.py turns them into what each aircraft's equipment
receives; the ground-truth labels go to the god view, log and eval only - never to the onboard units.

  ghost        ADS-B-only aircraft that does not exist, from a ground transmitter, plausible straight track
  swarm        several ghosts from ONE transmitter (same true position -> same signal strength)
  drift        a real aircraft's ADS-B position slowly walks away from the truth ("frog-boiling"); its
               transponder (TCAS, ground radar) still answers from the true position
  replay       an old recording of a real aircraft's ADS-B (its address, its positions ~60 s ago)
               re-broadcast from a ground transmitter
  masquerade   a fake aircraft using a REAL aircraft's ICAO address and callsign, elsewhere
  jamming      raised noise floor + heavy message loss on 1090 MHz around the victim
  gps          own-ship GPS spoofing: the victim's own reported position drifts; its integrity monitor
               flags it once the error is large
Interface (all optional per attack): step(now), emitters() -> [Emitter], adjust(em) (change a real
aircraft's claims), band(own_id) -> {"noise_dbm", "loss"}, own_offset(own_id) -> (east_m, north_m, gps_ok).
"""
from __future__ import annotations
import math
import random
from dataclasses import dataclass, replace

from world.flight_model import KT, move

NM = 1852.0

@dataclass
class Emitter:
    """What sensors.py needs from anything that transmits on 1090."""
    icao: str
    callsign: str
    lat: float                 # claimed position
    lon: float
    alt_ft: float              # claimed pressure altitude
    gs_kt: float
    trk_deg: float
    vs_fpm: float
    tx_lat: float              # where the transmitter really is (RSSI is measured from here)
    tx_lon: float
    tx_alt_ft: float
    transponder: bool          # answers TCAS / ground radar interrogations (real aircraft only)
    label: str                 # ground truth: "real" | "ghost" | "swarm" | "drift" | "replay" | "masquerade"
    ac_id: str | None = None   # world aircraft id for real aircraft
    attack_id: str | None = None

def _victim_frame(victim, now):
    return victim.ownship(now)["alt_press_ft"]

def _offset(lat, lon, brg, dist_m):
    return move(lat, lon, dist_m * math.sin(math.radians(brg)), dist_m * math.cos(math.radians(brg)))

class Attack:
    kind = "attack"
    label = "attack"

    def __init__(self, attack_id: str, victim, now: float, rng: random.Random, sensors=None):
        self.id, self.victim_id, self.t0, self.t, self.rng, self.sensors = attack_id, victim.id, now, now, rng, sensors

    def step(self, now: float) -> None:
        self.t = now

    def emitters(self) -> list[Emitter]:
        return []

    def public(self) -> dict:
        return {"id": self.id, "kind": self.kind, "victim": self.victim_id, "since_s": round(self.t - self.t0, 1)}

class _Fake:
    """One fake aircraft flying a straight line (shared by ghost / swarm / masquerade)."""
    def __init__(self, icao, callsign, lat, lon, alt_ft, trk, gs):
        self.icao, self.callsign, self.lat, self.lon, self.alt_ft, self.trk, self.gs = icao, callsign, lat, lon, alt_ft, trk, gs

    def step(self, dt: float) -> None:
        v = self.gs * KT * dt
        self.lat, self.lon = move(self.lat, self.lon, v * math.sin(math.radians(self.trk)), v * math.cos(math.radians(self.trk)))

def _new_icao(rng) -> str:
    return f"{rng.randrange(0xA00000, 0xADF7C7):06x}"

def _new_callsign(rng) -> str:
    return f"N{rng.randint(100, 999)}G{rng.choice('ABCDEFGHJKLMNPRSTUVWXYZ')}"

def _crossing_fake(victim, now, rng, ahead_nm, side_deg, dalt_ft, gs_kt, icao=None, callsign=None) -> _Fake:
    """A fake placed ahead of the victim, flying toward where the victim will be in ~90 s (plausible threat)."""
    brg = (victim.track_deg + side_deg) % 360
    lat, lon = _offset(victim.lat, victim.lon, brg, ahead_nm * NM)
    vlat, vlon = _offset(victim.lat, victim.lon, victim.track_deg, victim.gs_kt * KT * 90)
    de = (vlon - lon) * 111_320.0 * math.cos(math.radians(lat))
    dn = (vlat - lat) * 111_320.0
    return _Fake(icao or _new_icao(rng), callsign or _new_callsign(rng), lat, lon, _victim_frame(victim, now) + dalt_ft,
                 math.degrees(math.atan2(de, dn)) % 360, gs_kt)

def _ground_tx(victim, side_deg, dist_m=1500.0, tx_lat=None, tx_lon=None, tx_alt_ft=1500.0):
    if tx_lat is None:
        tx_lat, tx_lon = _offset(victim.lat, victim.lon, (victim.track_deg + side_deg - 60) % 360, dist_m)
    return tx_lat, tx_lon, tx_alt_ft

class Ghost(Attack):
    """ADS-B-only ghost aircraft, transmitted from a fixed ground site."""
    kind = label = "ghost"

    def __init__(self, attack_id, victim, now, rng, sensors=None, ahead_nm: float = 3.0, side_deg: float = 35.0,
                 dalt_ft: float = 0.0, gs_kt: float = 100.0, tx_lat=None, tx_lon=None, tx_alt_ft: float = 1500.0):
        super().__init__(attack_id, victim, now, rng, sensors)
        self.fakes = [_crossing_fake(victim, now, rng, ahead_nm, side_deg, dalt_ft, gs_kt)]
        self.tx = _ground_tx(victim, side_deg, tx_lat=tx_lat, tx_lon=tx_lon, tx_alt_ft=tx_alt_ft)

    @property
    def icao(self) -> str:
        return self.fakes[0].icao

    @property
    def callsign(self) -> str:
        return self.fakes[0].callsign

    def step(self, now: float) -> None:
        dt, self.t = now - self.t, now
        for f in self.fakes:
            f.step(dt)

    def emitters(self) -> list[Emitter]:
        return [Emitter(f.icao, f.callsign, f.lat, f.lon, f.alt_ft, f.gs, f.trk, 0.0, *self.tx,
                        transponder=False, label=self.label, attack_id=self.id) for f in self.fakes]

    def public(self) -> dict:
        f = self.fakes[0]
        return dict(super().public(), icao=f.icao, callsign=f.callsign, n=len(self.fakes),
                    lat=round(f.lat, 6), lon=round(f.lon, 6), alt_ft=round(f.alt_ft),
                    tx_lat=round(self.tx[0], 6), tx_lon=round(self.tx[1], 6))

class Swarm(Ghost):
    """Several ghosts, one transmitter: they all arrive with the same signal strength."""
    kind = label = "swarm"

    def __init__(self, attack_id, victim, now, rng, sensors=None, n: int = 4, **kw):
        super().__init__(attack_id, victim, now, rng, sensors, **kw)
        base = self.fakes[0]
        for i in range(1, int(n)):
            side = rng.uniform(-70, 70)
            self.fakes.append(_crossing_fake(victim, now, rng, rng.uniform(2.0, 4.5), side, rng.choice([-500, 0, 500]),
                                             rng.uniform(90, 130)))
        self.fakes[0] = base

def _nearest_real(sensors, victim, now, exclude=(), max_nm: float = 8.0):
    """The real aircraft the attack picks on: nearest one to the victim inside TCAS range (not a judge)."""
    best = None
    for ac in sensors.w.fleet.values():
        if ac.id == victim.id or ac.human or ac.id in exclude or ac.on_ground:
            continue
        d = math.hypot((ac.lon - victim.lon) * 111_320 * math.cos(math.radians(victim.lat)), (ac.lat - victim.lat) * 111_320)
        if d <= max_nm * NM and abs(ac.alt_msl_ft - victim.alt_msl_ft) < 3000 and (best is None or d < best[0]):
            best = (d, ac)
    return best[1] if best else None

class Drift(Attack):
    """A real aircraft's ADS-B position walks away from the truth at `rate_mps`, in one direction."""
    kind = label = "drift"

    def __init__(self, attack_id, victim, now, rng, sensors=None, target: str | None = None, rate_mps: float = 12.0,
                 brg_deg: float | None = None):
        super().__init__(attack_id, victim, now, rng, sensors)
        ac = sensors.w.fleet.get(target) if target else _nearest_real(sensors, victim, now)
        if ac is None:
            raise KeyError("no real aircraft near the victim to attack")
        self.target, self.rate = ac.id, rate_mps
        self.brg = brg_deg if brg_deg is not None else rng.uniform(0, 360)

    def adjust(self, em: Emitter) -> Emitter:
        if em.ac_id != self.target:
            return em
        lat, lon = _offset(em.lat, em.lon, self.brg, self.rate * (self.t - self.t0))
        return replace(em, lat=lat, lon=lon, label="drift", attack_id=self.id)

    def public(self) -> dict:
        return dict(super().public(), target=self.target, offset_m=round(self.rate * (self.t - self.t0)))

class Replay(Attack):
    """Old ADS-B of a real aircraft (its address, its positions `delay_s` ago) re-broadcast from the ground."""
    kind = label = "replay"

    def __init__(self, attack_id, victim, now, rng, sensors=None, target: str | None = None, delay_s: float = 60.0):
        super().__init__(attack_id, victim, now, rng, sensors)
        ac = sensors.w.fleet.get(target) if target else _nearest_real(sensors, victim, now)
        if ac is None:
            raise KeyError("no real aircraft near the victim to replay")
        self.target, self.delay = ac.id, delay_s
        self.tx = _ground_tx(victim, rng.uniform(-90, 90))

    def emitters(self) -> list[Emitter]:
        from world.sensors import icao_of
        hist = self.sensors.history.get(icao_of(self.target), [])
        old = [h for h in hist if h[0] <= self.t - self.delay]
        if not old:
            return []
        _, lat, lon, alt, gs, trk, vs = old[-1]
        return [Emitter(icao_of(self.target), self.target, lat, lon, alt, gs, trk, vs, *self.tx,
                        transponder=False, label="replay", attack_id=self.id)]

    def public(self) -> dict:
        return dict(super().public(), target=self.target, delay_s=self.delay, tx_lat=round(self.tx[0], 6),
                    tx_lon=round(self.tx[1], 6))

class Masquerade(Ghost):
    """A fake aircraft using a REAL aircraft's ICAO address and callsign, somewhere else."""
    kind = label = "masquerade"

    def __init__(self, attack_id, victim, now, rng, sensors=None, target: str | None = None, **kw):
        super().__init__(attack_id, victim, now, rng, sensors, **kw)
        from world.sensors import icao_of
        ac = sensors.w.fleet.get(target) if target else _nearest_real(sensors, victim, now)
        if ac is None:
            raise KeyError("no real aircraft to impersonate")
        self.target = ac.id
        self.fakes[0].icao, self.fakes[0].callsign = icao_of(ac.id), ac.id

    def public(self) -> dict:
        return dict(super().public(), target=self.target)

class Jamming(Attack):
    """Noise on 1090 MHz near the victim: raised noise floor, most messages lost."""
    kind = label = "jamming"

    def __init__(self, attack_id, victim, now, rng, sensors=None, noise_dbm: float = -72.0, loss: float = 0.75):
        super().__init__(attack_id, victim, now, rng, sensors)
        self.noise, self.loss = noise_dbm, loss

    def band(self, own_id: str) -> dict | None:
        return {"noise_dbm": self.noise, "loss": self.loss} if own_id == self.victim_id else None

class GpsSpoof(Attack):
    """Own-ship GPS spoofing: the victim's reported position drifts; integrity flags it past `detect_m`."""
    kind = label = "gps"

    def __init__(self, attack_id, victim, now, rng, sensors=None, rate_mps: float = 15.0, brg_deg: float | None = None,
                 detect_m: float = 400.0):
        super().__init__(attack_id, victim, now, rng, sensors)
        self.rate, self.detect = rate_mps, detect_m
        self.brg = brg_deg if brg_deg is not None else rng.uniform(0, 360)

    def own_offset(self, own_id: str):
        if own_id != self.victim_id:
            return None
        d = self.rate * (self.t - self.t0)
        return d * math.sin(math.radians(self.brg)), d * math.cos(math.radians(self.brg)), d < self.detect

    def public(self) -> dict:
        return dict(super().public(), offset_m=round(self.rate * (self.t - self.t0)))

KINDS = {"ghost": Ghost, "swarm": Swarm, "drift": Drift, "replay": Replay, "masquerade": Masquerade,
         "jamming": Jamming, "gps": GpsSpoof}

class AttackSet:
    KIND_ONCE = ("jamming", "gps")

    def __init__(self, seed: int = 99, sensors=None):
        self.rng = random.Random(seed)
        self.sensors = sensors
        self.active: dict[str, Attack] = {}
        self.n = 0

    def start(self, kind: str, victim, now: float, **params) -> Attack:
        if kind not in KINDS:
            raise KeyError(f"unknown attack {kind}; have {sorted(KINDS)}")
        self.n += 1
        a = KINDS[kind](f"{kind}-{self.n}", victim, now, self.rng, self.sensors, **params)
        self.active[a.id] = a
        return a

    def stop(self, attack_id: str | None = None, kind: str | None = None) -> list[str]:
        ids = [i for i, a in self.active.items() if (attack_id is None or i == attack_id) and (kind is None or a.kind == kind)]
        for i in ids:
            del self.active[i]
        return ids

    def step(self, now: float) -> None:
        for a in self.active.values():
            a.step(now)

    def emitters(self) -> list[Emitter]:
        out = []
        for a in self.active.values():
            out += a.emitters()
        return out

    def adjust(self, em: Emitter) -> Emitter:
        for a in self.active.values():
            if hasattr(a, "adjust"):
                em = a.adjust(em)
        return em

    def band(self, own_id: str) -> dict | None:
        for a in self.active.values():
            b = a.band(own_id) if hasattr(a, "band") else None
            if b:
                return b
        return None

    def own_offset(self, own_id: str):
        for a in self.active.values():
            o = a.own_offset(own_id) if hasattr(a, "own_offset") else None
            if o:
                return o
        return None

    def public(self) -> list[dict]:
        return [a.public() for a in self.active.values()]
