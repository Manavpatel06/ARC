"""
verify/tracks.py - per-aircraft track manager keyed by ICAO 24-bit address.

Everything we hear about one address lands in one Track: its ADS-B position / velocity claims, the
TCAS track our own TCAS holds on it, its Mode S replies. Own-ship state is kept alongside (needed to turn
claims into range / bearing). Pure data: no thresholds here.
"""
from __future__ import annotations
import collections
import math
from dataclasses import dataclass, field
from typing import Optional

M_PER_DEG = 111_320.0
FT = 0.3048
KT = 0.514444

def enu(lat0: float, lon0: float, lat: float, lon: float) -> tuple[float, float]:
    """East / north metres of (lat, lon) from (lat0, lon0); flat earth (fine inside ~60 NM)."""
    return (lon - lon0) * M_PER_DEG * math.cos(math.radians(lat0)), (lat - lat0) * M_PER_DEG

def rng_brg(lat0: float, lon0: float, lat: float, lon: float) -> tuple[float, float]:
    e, n = enu(lat0, lon0, lat, lon)
    return math.hypot(e, n), math.degrees(math.atan2(e, n)) % 360.0

def angdiff(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)

def move(lat: float, lon: float, e_m: float, n_m: float) -> tuple[float, float]:
    return lat + n_m / M_PER_DEG, lon + e_m / (M_PER_DEG * math.cos(math.radians(lat)))

@dataclass
class Own:
    t: float = -1e9
    lat: float = 0.0
    lon: float = 0.0
    alt_ft: float = 0.0
    gs_kt: float = 0.0
    trk_deg: float = 0.0
    hdg_deg: float = 0.0
    vs_fpm: float = 0.0
    gps_ok: bool = True
    tcas_ok: bool = True

@dataclass
class Track:
    icao: str
    first_t: float
    last_t: float = 0.0
    callsign: Optional[str] = None
    pos: collections.deque = field(default_factory=lambda: collections.deque(maxlen=60))   # (t, lat, lon, alt_ft, rssi)
    vel: collections.deque = field(default_factory=lambda: collections.deque(maxlen=60))   # (t, gs_kt, trk_deg, vs_fpm)
    tcas: collections.deque = field(default_factory=lambda: collections.deque(maxlen=30))  # (t, range_m, brg_deg, alt_ft)
    replies: collections.deque = field(default_factory=lambda: collections.deque(maxlen=200))  # (t, df, rssi)
    # fusion state
    logodds: Optional[float] = None
    state: str = "UNVERIFIED"

    @property
    def has_adsb(self) -> bool:
        return bool(self.pos)

    def claimed_at(self, t: float) -> Optional[tuple[float, float, float]]:
        """Claimed (lat, lon, alt_ft) at time t: last ADS-B position moved on by the last claimed velocity."""
        if not self.pos:
            return None
        pt, lat, lon, alt, _ = self.pos[-1]
        dt = max(0.0, min(5.0, t - pt))
        if self.vel and dt > 0:
            _, gs, trk, vs = self.vel[-1]
            v = (gs or 0.0) * KT
            lat, lon = move(lat, lon, v * math.sin(math.radians(trk or 0.0)) * dt, v * math.cos(math.radians(trk or 0.0)) * dt)
            alt = alt + (vs or 0.0) * dt / 60.0
        return lat, lon, alt

    def last_tcas(self, t: float, fresh_s: float):
        if self.tcas and t - self.tcas[-1][0] <= fresh_s:
            return self.tcas[-1]
        return None

class TrackManager:
    def __init__(self, timeout_s: float = 30.0):
        self.timeout_s = timeout_s
        self.tracks: dict[str, Track] = {}
        self.own = Own()
        self.radar_t = -1e9            # last time we saw ground radar activity (interrogation or DF4/5/20/21)
        self.tcas_t = -1e9             # last TCAS track of anyone (TCAS alive)
        self.band: dict = {}

    def _track(self, icao: str, t: float) -> Track:
        tr = self.tracks.get(icao)
        if tr is None:
            tr = self.tracks[icao] = Track(icao, first_t=t)
        tr.last_t = max(tr.last_t, t)
        return tr

    def ingest(self, m: dict) -> None:
        k, t = m["kind"], m["t_ns"] / 1e9
        if k == "OWNSHIP_STATE":
            o = self.own
            o.t, o.lat, o.lon, o.alt_ft = t, m["lat"], m["lon"], m["alt_ft"]
            o.gs_kt, o.trk_deg, o.vs_fpm = m.get("gs_kt") or 0.0, m.get("trk_deg") or 0.0, m.get("vs_fpm") or 0.0
            o.hdg_deg = m.get("hdg_deg") if m.get("hdg_deg") is not None else o.trk_deg
            o.gps_ok = m.get("gps_ok", True) is not False
            o.tcas_ok = m.get("tcas_ok", True) is not False
        elif k == "BAND_STATS":
            self.band = {"t": t, "msgs_per_s": m.get("msgs_per_s"), "noise_dbm": m.get("noise_dbm")}
        elif k == "INTERROGATION_1030":
            self.radar_t = t
        elif m.get("icao"):
            tr = self._track(m["icao"], t)
            if m.get("callsign"):
                tr.callsign = m["callsign"]
            if k == "ADSB_POSITION":
                tr.pos.append((t, m["lat"], m["lon"], m.get("alt_ft"), m.get("rssi_dbm")))
            elif k == "ADSB_VELOCITY":
                tr.vel.append((t, m.get("gs_kt"), m.get("trk_deg"), m.get("vs_fpm")))
            elif k == "TCAS_TRACK":
                tr.tcas.append((t, m["range_m"], m["brg_deg"], m.get("alt_ft")))
                self.tcas_t = t
            elif k == "MODES_REPLY":
                tr.replies.append((t, m.get("df"), m.get("rssi_dbm")))
                if m.get("df") in (4, 5, 20, 21):
                    self.radar_t = t

    def prune(self, t: float) -> list[str]:
        gone = [i for i, tr in self.tracks.items() if t - tr.last_t > self.timeout_s]
        for i in gone:
            del self.tracks[i]
        return gone
