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
    # one ICAO address heard from two places: the second place becomes its own branch (icao + "~2")
    parent: Optional["Track"] = None         # set on the branch: TCAS / Mode S evidence lives with the parent
    branch: Optional["Track"] = None         # set on the parent
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
        p = self.pos[-1]
        if p[0] > t:                                       # asked about the past: the report just before t
            earlier = [q for q in self.pos if q[0] <= t]
            p = earlier[-1] if earlier else self.pos[0]
        pt, lat, lon, alt, _ = p
        dt = max(0.0, min(5.0, t - pt))
        vel = [v for v in self.vel if v[0] <= max(t, pt)]
        if vel and dt > 0:
            _, gs, trk, vs = vel[-1]
            v = (gs or 0.0) * KT
            lat, lon = move(lat, lon, v * math.sin(math.radians(trk or 0.0)) * dt, v * math.cos(math.radians(trk or 0.0)) * dt)
            alt = alt + (vs or 0.0) * dt / 60.0
        return lat, lon, alt

    def last_tcas(self, t: float, fresh_s: float):
        """TCAS tracks the TRANSPONDER with this address: a branch is judged against its parent's TCAS track."""
        src = self.parent.tcas if self.parent is not None else self.tcas
        if src and t - src[-1][0] <= fresh_s:
            return src[-1]
        return None

    @property
    def address(self) -> str:
        return self.icao.split("~")[0]

class TrackManager:
    def __init__(self, timeout_s: float = 30.0):
        self.timeout_s = timeout_s
        self.tracks: dict[str, Track] = {}
        self.own = Own()
        self.radar_t = -1e9            # last time we saw ground radar activity (interrogation or DF4/5/20/21)
        self.tcas_t = -1e9             # last TCAS track of anyone (TCAS alive)
        self.own_hist: collections.deque = collections.deque(maxlen=100)   # (t, lat, lon, alt_ft), ~20 s
        self.band: dict = {}
        self.interrogations: collections.deque = collections.deque(maxlen=20)   # (t, site): beam swept US

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
            self.own_hist.append((t, o.lat, o.lon, o.alt_ft))
        elif k == "BAND_STATS":
            self.band = {"t": t, "msgs_per_s": m.get("msgs_per_s"), "noise_dbm": m.get("noise_dbm")}
        elif k == "INTERROGATION_1030":
            self.radar_t = t
            if not self.interrogations or t - self.interrogations[-1][0] > 1.0:     # one per beam pass
                self.interrogations.append((t, m.get("site")))
        elif m.get("icao"):
            tr = self._track(m["icao"], t)
            if m.get("callsign"):
                tr.callsign = m["callsign"]
            if k == "ADSB_POSITION":
                tr = self._branch_for(tr, t, m["lat"], m["lon"])
                tr.last_t = max(tr.last_t, t)
                tr.pos.append((t, m["lat"], m["lon"], m.get("alt_ft"), m.get("rssi_dbm")))
            elif k == "ADSB_VELOCITY":
                if tr.branch is not None and tr.branch.pos:
                    tr = self._velocity_branch(tr, m.get("trk_deg"))
                tr.vel.append((t, m.get("gs_kt"), m.get("trk_deg"), m.get("vs_fpm")))
            elif k == "TCAS_TRACK":
                tr.tcas.append((t, m["range_m"], m["brg_deg"], m.get("alt_ft")))
                self.tcas_t = t
            elif k == "MODES_REPLY":
                tr.replies.append((t, m.get("df"), m.get("rssi_dbm")))
                if m.get("df") in (4, 5, 20, 21):
                    self.radar_t = t

    SPLIT_M = 800.0                 # same address, this far from where it should be -> a second transmitter

    def _branch_for(self, tr: Track, t: float, lat: float, lon: float) -> Track:
        """Which of an address's (up to two) tracks a position report belongs to; opens the branch."""
        cands = [x for x in (tr, tr.branch) if x is not None and x.pos]
        if not cands:
            return tr
        def miss(x):
            c = x.claimed_at(t)
            return math.hypot(*enu(c[0], c[1], lat, lon))
        best = min(cands, key=miss)
        if miss(best) <= self.SPLIT_M:
            return best
        established = len([p for p in tr.pos if t - p[0] <= 6.0]) >= 3
        if tr.branch is None and established:
            b = self.tracks[tr.icao + "~2"] = Track(tr.icao + "~2", first_t=t, callsign=tr.callsign, parent=tr)
            tr.branch = b
            return b
        return best if tr.branch is not None else tr

    @staticmethod
    def _velocity_branch(tr: Track, trk) -> Track:
        """A velocity report has no position: give it to the branch whose motion it matches."""
        def dirn(x):
            if x.vel and x.vel[-1][2] is not None:
                return x.vel[-1][2]
            if len(x.pos) >= 2:
                e, n = enu(x.pos[-2][1], x.pos[-2][2], x.pos[-1][1], x.pos[-1][2])
                return math.degrees(math.atan2(e, n)) % 360
            return None
        if trk is None:
            return tr
        opts = [(angdiff(d, trk), x) for x in (tr, tr.branch) if (d := dirn(x)) is not None]
        return min(opts, key=lambda o: o[0])[1] if opts else tr

    def own_at(self, t: float) -> tuple[float, float, float]:
        """Own (lat, lon, alt_ft) at time t, interpolated from the recent OWNSHIP_STATE history."""
        h = self.own_hist
        if not h:
            return self.own.lat, self.own.lon, self.own.alt_ft
        if t >= h[-1][0]:
            return h[-1][1:]
        for a, b in zip(reversed(list(h)[:-1]), reversed(h)):
            if a[0] <= t <= b[0]:
                f = (t - a[0]) / (b[0] - a[0]) if b[0] > a[0] else 0.0
                return tuple(x + (y - x) * f for x, y in zip(a[1:], b[1:]))
        return h[0][1:]

    def prune(self, t: float) -> list[str]:
        gone = [i for i, tr in self.tracks.items() if t - tr.last_t > self.timeout_s]
        for i in gone:
            tr = self.tracks.pop(i)
            if tr.parent is not None and tr.parent.branch is tr:
                tr.parent.branch = None
            if tr.branch is not None:
                tr.branch.parent = None
        return gone
