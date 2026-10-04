"""
world/sensors.py - what each judge aircraft's OWN equipment receives (input to the FLOCK onboard unit).

The world knows the truth; this module turns it into the evidence a real aircraft would have, with
realistic rates, ranges and noise. Nothing here is a claim the unit can't make itself:
  ADSB_POSITION / ADSB_VELOCITY  every 1090 emitter (real aircraft + attacks), 2 Hz each, heard within
                                 RX_RANGE_NM if the received power clears the receiver floor; RSSI from a
                                 free-space path-loss model measured from the TRANSMITTER's true position
  TCAS_TRACK                     own TCAS, 1 Hz: only aircraft with a transponder (they answer interrogations),
                                 inside TCAS range / altitude window; range from round-trip timing (small
                                 error), bearing with several degrees of error, Mode S altitude in 25 ft steps
  MODES_REPLY                    DF11 acquisition squitter ~1 Hz per transponder; DF4/5 replies when the
                                 rotating ground radar's beam sweeps the aircraft
  INTERROGATION_1030             when that radar's beam sweeps OUR aircraft (radar is active here)
  OWNSHIP_STATE                  own GPS position / velocity, 5 Hz, gps_ok / tcas_ok flags
  BAND_STATS                     messages per second and noise floor, 1 Hz (jamming: milestone 3)
Ground truth (labels) is in truth(), for the god view / log / evaluation only.
Numbers below are reasonable estimates, not taken from a standard - see docs/limitations.md.
"""
from __future__ import annotations
import collections
import math
import random
import zlib

from world.attacks import AttackSet, Emitter

NM = 1852.0
FT = 0.3048
TX_DBM = 51.0              # ~125 W transponder
CABLE_DB = 3.0
RX_FLOOR_DBM = -90.0
RSSI_SIGMA_DB = 2.0
RX_RANGE_NM = 100.0
LOSS_P = 0.02              # random message loss
ADSB_PERIOD_S = 0.5        # position and velocity squitters, each
DF11_PERIOD_S = 1.0
TCAS_PERIOD_S = 1.0
TCAS_RANGE_NM = 12.0
TCAS_ALT_FT = 9900.0
TCAS_RANGE_SIGMA_M = 15.0
TCAS_BRG_SIGMA_DEG = 5.0
OWN_PERIOD_S = 0.2
BAND_PERIOD_S = 1.0
SSR_SITE = ("PHX-ASR", 33.4343, -112.0116)   # Phoenix approach radar (approximate position)
SSR_PERIOD_S = 4.8
SSR_RANGE_NM = 60.0

def icao_of(ac_id: str) -> str:
    """Stable 24-bit address per simulated aircraft (US block)."""
    return f"{0xA00001 + zlib.crc32(ac_id.encode()) % 0xDF7C6:06x}"

def _enu(lat0, lon0, lat, lon):
    return (lon - lon0) * 111_320.0 * math.cos(math.radians(lat0)), (lat - lat0) * 111_320.0

def _rng_brg(lat0, lon0, lat, lon):
    e, n = _enu(lat0, lon0, lat, lon)
    return math.hypot(e, n), math.degrees(math.atan2(e, n)) % 360.0

class SensorSim:
    def __init__(self, world, seed: int = 1090):
        self.w = world
        self.rng = random.Random(seed)
        self.attacks = AttackSet(seed + 1, sensors=self)
        self.history: dict[str, collections.deque] = {}    # icao -> recent true ADS-B claims (replay attacks)
        self.replay_src = None                             # world/replay.py: recorded real aircraft (emitters)
        self.tcas_off: set[str] = set()                    # own-ships whose TCAS is unavailable (eval / demo)
        self.next: dict[tuple, float] = {}
        self.ssr_prev: float | None = None
        self.band_n: dict[str, int] = {}
        self.out: dict[str, list] = {}

    # ---------- who transmits ----------
    def emitters(self, now: float) -> list[Emitter]:
        out = []
        for ac in self.w.fleet.values():
            o = ac.ownship(now)
            em = Emitter(icao_of(ac.id), ac.id, ac.lat, ac.lon, o["alt_press_ft"], ac.gs_kt, ac.track_deg,
                         o["vs_fpm"], ac.lat, ac.lon, ac.alt_msl_ft, transponder=True, label="real", ac_id=ac.id)
            h = self.history.setdefault(em.icao, collections.deque(maxlen=600))
            if not h or now - h[-1][0] >= 0.5:
                h.append((now, em.lat, em.lon, em.alt_ft, em.gs_kt, em.trk_deg, em.vs_fpm))
            out.append(self.attacks.adjust(em))
        if self.replay_src is not None:
            out += self.replay_src.emitters(now)
        return out + self.attacks.emitters()

    def own_ships(self) -> list:
        return [ac for ac in self.w.fleet.values() if ac.human and ac.flock]

    # ---------- per tick ----------
    def step(self, now: float, own_ids=None) -> dict[str, list[dict]]:
        """Advance attacks and produce the messages each own-ship received since the last call."""
        self.attacks.step(now)
        owns = [a for a in self.own_ships() if own_ids is None or a.id in own_ids]
        out = {a.id: [] for a in owns}
        if not owns:
            self.ssr_prev = now
            return out
        ems = self.emitters(now)
        tns = int(now * 1e9)

        def due(key, period):
            nxt = self.next.get(key)
            if nxt is None:                                  # random phase so emitters don't all fire together
                nxt = self.next[key] = now + self.rng.uniform(0, period)
            if now >= nxt:
                self.next[key] = nxt + period if now - nxt < period else now + period
                return True
            return False

        bands = {own.id: self.attacks.band(own.id) for own in owns}       # jamming around an own-ship

        def hear(own, em_lat, em_lon, em_alt_ft) -> float | None:
            """Received power (dBm) at own-ship from a transmitter, or None if not received."""
            b = bands.get(own.id)
            floor = max(RX_FLOOR_DBM, b["noise_dbm"] + 6.0) if b else RX_FLOOR_DBM
            loss = max(LOSS_P, b["loss"]) if b else LOSS_P
            d, _ = _rng_brg(own.lat, own.lon, em_lat, em_lon)
            d3 = math.hypot(d, (em_alt_ft * FT - own.alt_msl_ft * FT))
            if d3 > RX_RANGE_NM * NM or self.rng.random() < loss:
                return None
            fspl = 20 * math.log10(max(d3, 30.0) / 1000.0) + 20 * math.log10(1090.0) + 32.44
            p = TX_DBM - CABLE_DB - fspl + self.rng.gauss(0, RSSI_SIGMA_DB)
            return round(p, 1) if p >= floor else None

        # ADS-B squitters + DF11 (broadcasts: every own-ship may hear the same transmission)
        for em in ems:
            src = (em.icao, em.attack_id or "own")           # per TRANSMITTER: two boxes may share one address
            send_pos = due(src + ("pos",), ADSB_PERIOD_S)
            send_vel = due(src + ("vel",), ADSB_PERIOD_S)
            send_df11 = em.transponder and due(src + ("df11",), DF11_PERIOD_S)
            if not (send_pos or send_vel or send_df11):
                continue
            for own in owns:
                if em.ac_id == own.id:
                    continue
                if send_pos and (p := hear(own, em.tx_lat, em.tx_lon, em.tx_alt_ft)) is not None:
                    out[own.id].append({"kind": "ADSB_POSITION", "t_ns": tns, "icao": em.icao, "callsign": em.callsign,
                                        "lat": round(em.lat, 6), "lon": round(em.lon, 6),
                                        "alt_ft": round(em.alt_ft / 25) * 25, "rssi_dbm": p})
                if send_vel and (p := hear(own, em.tx_lat, em.tx_lon, em.tx_alt_ft)) is not None:
                    out[own.id].append({"kind": "ADSB_VELOCITY", "t_ns": tns, "icao": em.icao, "gs_kt": round(em.gs_kt, 1),
                                        "trk_deg": round(em.trk_deg, 1), "vs_fpm": round(em.vs_fpm / 64) * 64, "rssi_dbm": p})
                if send_df11 and (p := hear(own, em.tx_lat, em.tx_lon, em.tx_alt_ft)) is not None:
                    out[own.id].append({"kind": "MODES_REPLY", "t_ns": tns, "icao": em.icao, "df": 11, "rssi_dbm": p})

        # own TCAS: interrogates transponders around it, measures range by round-trip time
        for own in owns:
            if own.id in self.tcas_off or not due((own.id, "tcas"), TCAS_PERIOD_S):
                continue
            o_press = own.ownship(now)["alt_press_ft"]
            for em in ems:
                if not em.transponder or em.ac_id == own.id:
                    continue
                d, brg = _rng_brg(own.lat, own.lon, em.tx_lat, em.tx_lon)        # physics: where the box really is
                b = bands.get(own.id)
                if d > TCAS_RANGE_NM * NM or abs(em.alt_ft - o_press) > TCAS_ALT_FT or                         self.rng.random() < (max(LOSS_P, b["loss"]) if b else LOSS_P):
                    continue
                out[own.id].append({"kind": "TCAS_TRACK", "t_ns": tns, "icao": em.icao,
                                    "range_m": round(max(0.0, d + self.rng.gauss(0, TCAS_RANGE_SIGMA_M)), 1),
                                    "brg_deg": round((brg + self.rng.gauss(0, TCAS_BRG_SIGMA_DEG)) % 360, 1),
                                    "alt_ft": round(em.alt_ft / 25) * 25})

        # ground radar: rotating beam; swept transponders reply (DF4/5), we hear the replies
        site, slat, slon = SSR_SITE
        az = (now / SSR_PERIOD_S * 360.0) % 360.0
        prev = self.ssr_prev if self.ssr_prev is not None else now
        sweep = min(360.0, (now - prev) / SSR_PERIOD_S * 360.0)
        a0 = (az - sweep) % 360.0
        self.ssr_prev = now

        def swept(brg: float) -> bool:
            return sweep > 0 and ((brg - a0) % 360.0) <= sweep

        for em in ems:
            if not em.transponder:
                continue
            d, brg = _rng_brg(slat, slon, em.tx_lat, em.tx_lon)
            if d > SSR_RANGE_NM * NM or not swept(brg):
                continue
            df = 4 if int(now / SSR_PERIOD_S) % 2 == 0 else 5
            for own in owns:
                if em.ac_id == own.id:
                    out[own.id].append({"kind": "INTERROGATION_1030", "t_ns": tns, "site": site})
                elif (p := hear(own, em.tx_lat, em.tx_lon, em.tx_alt_ft)) is not None:
                    out[own.id].append({"kind": "MODES_REPLY", "t_ns": tns, "icao": em.icao, "df": df, "rssi_dbm": p})

        # own-ship state and band stats
        for own in owns:
            self.band_n[own.id] = self.band_n.get(own.id, 0) + len(out[own.id])
            if due((own.id, "own"), OWN_PERIOD_S):
                o = own.ownship(now)
                lat, lon, gps_ok = own.lat, own.lon, True
                off = self.attacks.own_offset(own.id)                       # own GPS being spoofed
                if off:
                    lat, lon = lat + off[1] / 111_320.0, lon + off[0] / (111_320.0 * math.cos(math.radians(lat)))
                    gps_ok = off[2]
                out[own.id].append({"kind": "OWNSHIP_STATE", "t_ns": tns, "lat": round(lat, 7), "lon": round(lon, 7),
                                    "alt_ft": o["alt_press_ft"], "gs_kt": o["gs_kt"], "trk_deg": o["track_deg"],
                                    "hdg_deg": o["hdg_deg"], "vs_fpm": o["vs_fpm"], "gps_ok": gps_ok, "tcas_ok": own.id not in self.tcas_off})
            if due((own.id, "band"), BAND_PERIOD_S):
                b = bands.get(own.id)
                out[own.id].append({"kind": "BAND_STATS", "t_ns": tns, "msgs_per_s": float(self.band_n[own.id]),
                                    "noise_dbm": b["noise_dbm"] if b else -100.0})
                self.band_n[own.id] = 0
        return out

    # ---------- truth (god view / log / evaluation only) ----------
    def truth(self, now: float) -> list[dict]:
        return [{"icao": e.icao, "id": e.ac_id or e.callsign, "callsign": e.callsign, "label": e.label,
                 "lat": round(e.lat, 6), "lon": round(e.lon, 6), "alt_ft": round(e.alt_ft), "trk_deg": round(e.trk_deg, 1),
                 "tx_lat": round(e.tx_lat, 6), "tx_lon": round(e.tx_lon, 6), "attack": e.attack_id}
                for e in self.emitters(now)]
