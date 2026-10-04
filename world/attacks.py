"""
world/attacks.py - spoofing attacks injected into the SIMULATED radio environment (never broadcast).

An attack is an emitter: a transmitter at one (true) place that broadcasts claims about another.
world/sensors.py turns emitters into what each aircraft's equipment receives; the ground-truth label
(real / ghost / ...) goes to the god view and log only, never to the onboard units.

Milestone 1-2: single ghost. A ground transmitter broadcasts ADS-B for an aircraft that does not exist,
flying a plausible straight track that crosses the victim's path. It has no transponder: it cannot answer
TCAS or ground radar. (Ghost flock, position drift, replay, ICAO masquerade, jamming, own-ship GPS
spoofing: milestone 3, same Emitter interface.)
"""
from __future__ import annotations
import math
import random
from dataclasses import dataclass

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
    label: str                 # ground truth: "real" | "ghost" | ...
    ac_id: str | None = None   # world aircraft id for real aircraft
    attack_id: str | None = None

class Ghost:
    """ADS-B-only ghost aircraft, transmitted from a fixed ground site."""
    kind = "ghost"

    def __init__(self, attack_id: str, victim, now: float, rng: random.Random, ahead_nm: float = 3.0,
                 side_deg: float = 35.0, dalt_ft: float = 0.0, gs_kt: float = 100.0, tx_lat=None, tx_lon=None,
                 tx_alt_ft: float = 1500.0):
        self.id = attack_id
        self.icao = f"{rng.randrange(0xA00000, 0xADF7C7):06x}"
        self.callsign = f"N{rng.randint(100, 999)}G{rng.choice('ABCDEFGHJKLMNPRSTUVWXYZ')}"
        brg = (victim.track_deg + side_deg) % 360
        self.lat, self.lon = move(victim.lat, victim.lon, ahead_nm * NM * math.sin(math.radians(brg)),
                                  ahead_nm * NM * math.cos(math.radians(brg)))
        self.alt_ft = victim.ownship(now)["alt_press_ft"] + dalt_ft      # claims a pressure altitude, like ADS-B
        # fly a plausible track across the victim's path: toward where the victim will be in ~90 s
        vlat, vlon = move(victim.lat, victim.lon, victim.gs_kt * KT * 90 * math.sin(math.radians(victim.track_deg)),
                          victim.gs_kt * KT * 90 * math.cos(math.radians(victim.track_deg)))
        de = (vlon - self.lon) * 111_320.0 * math.cos(math.radians(self.lat))
        dn = (vlat - self.lat) * 111_320.0
        self.trk = math.degrees(math.atan2(de, dn)) % 360
        self.gs = gs_kt
        self.t = now
        # the transmitter: on the ground near the victim's path unless given (a car, a rooftop)
        if tx_lat is None:
            tx_lat, tx_lon = move(victim.lat, victim.lon, 1500.0 * math.sin(math.radians(brg - 60)),
                                  1500.0 * math.cos(math.radians(brg - 60)))
        self.tx = (tx_lat, tx_lon, tx_alt_ft)

    def step(self, now: float) -> None:
        dt = now - self.t
        self.t = now
        v = self.gs * KT * dt
        self.lat, self.lon = move(self.lat, self.lon, v * math.sin(math.radians(self.trk)), v * math.cos(math.radians(self.trk)))

    def emitters(self) -> list[Emitter]:
        return [Emitter(self.icao, self.callsign, self.lat, self.lon, self.alt_ft, self.gs, self.trk, 0.0,
                        *self.tx, transponder=False, label="ghost", attack_id=self.id)]

    def public(self) -> dict:
        return {"id": self.id, "kind": self.kind, "icao": self.icao, "callsign": self.callsign,
                "lat": round(self.lat, 6), "lon": round(self.lon, 6), "alt_ft": round(self.alt_ft),
                "tx_lat": round(self.tx[0], 6), "tx_lon": round(self.tx[1], 6)}

KINDS = {"ghost": Ghost}

class AttackSet:
    def __init__(self, seed: int = 99):
        self.rng = random.Random(seed)
        self.active: dict[str, object] = {}
        self.n = 0

    def start(self, kind: str, victim, now: float, **params) -> object:
        if kind not in KINDS:
            raise KeyError(f"unknown attack {kind}; have {sorted(KINDS)}")
        self.n += 1
        a = KINDS[kind](f"{kind}-{self.n}", victim, now, self.rng, **params)
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

    def public(self) -> list[dict]:
        return [a.public() for a in self.active.values()]
