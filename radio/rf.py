"""
radio/rf.py — the ONE emulated RF model shared by channel.py (which adds noise to truth) and
evidence.py (which predicts what the measurement SHOULD be from a peer's *claimed* state).

Emulated link: 915 MHz ISM band, 20 dBm (100 mW) transmitter, 0 dBi antennas, free-space path
loss. RSSI noise ~ N(0, 3 dB) (fading/shadowing). Doppler noise ~ N(0, 3 Hz) — honest note: a
real radio needs a TCXO and per-peer frequency-offset calibration to measure Doppler this well;
RSSI is the more robust check on cheap hardware.
"""
from __future__ import annotations
import math

FREQ_HZ = 915e6
PTX_DBM = 20.0
C = 299_792_458.0
KT = 0.514444
FT = 0.3048
RSSI_SIGMA_DB = 3.0
DOPPLER_SIGMA_HZ = 3.0
M_PER_DEG_LAT = 111_320.0


def enu(lat0: float, lon0: float, lat: float, lon: float) -> tuple[float, float]:
    """Flat-earth east/north metres of (lat, lon) from (lat0, lon0). Fine for 15 mi."""
    return ((lon - lon0) * M_PER_DEG_LAT * math.cos(math.radians(lat0)), (lat - lat0) * M_PER_DEG_LAT)


def velocity(gs_kt: float, track_deg: float, vs_fpm: float) -> tuple[float, float, float]:
    v = gs_kt * KT
    return (v * math.sin(math.radians(track_deg)), v * math.cos(math.radians(track_deg)), vs_fpm * FT / 60.0)


def extrapolate(lat: float, lon: float, alt_ft: float, gs_kt: float, track_deg: float, vs_fpm: float, dt: float):
    ve, vn, vu = velocity(gs_kt, track_deg, vs_fpm)
    return (lat + vn * dt / M_PER_DEG_LAT,
            lon + ve * dt / (M_PER_DEG_LAT * math.cos(math.radians(lat))),
            alt_ft + vu * dt / FT)


def geometry(tx: dict, rx: dict) -> tuple[float, float]:
    """tx/rx: {lat, lon, alt_ft, gs_kt, track_deg, vs_fpm}. Returns (slant range m, range rate m/s; + = opening)."""
    e, n = enu(rx["lat"], rx["lon"], tx["lat"], tx["lon"])
    u = (tx["alt_ft"] - rx["alt_ft"]) * FT
    r = math.sqrt(e * e + n * n + u * u) or 1.0
    vt = velocity(tx.get("gs_kt", 0), tx.get("track_deg", 0), tx.get("vs_fpm", 0))
    vr = velocity(rx.get("gs_kt", 0), rx.get("track_deg", 0), rx.get("vs_fpm", 0))
    rel_v = (vt[0] - vr[0], vt[1] - vr[1], vt[2] - vr[2])
    rdot = (e * rel_v[0] + n * rel_v[1] + u * rel_v[2]) / r
    return r, rdot


def horiz_range_m(a: dict, b: dict) -> float:
    e, n = enu(a["lat"], a["lon"], b["lat"], b["lon"])
    return math.hypot(e, n)


def bearing_deg(frm: dict, to: dict) -> float:
    e, n = enu(frm["lat"], frm["lon"], to["lat"], to["lon"])
    return math.degrees(math.atan2(e, n)) % 360


def rssi_dbm(range_m: float) -> float:
    fspl = 20 * math.log10(max(range_m, 1.0)) + 20 * math.log10(FREQ_HZ) - 147.55
    return PTX_DBM - fspl


def doppler_hz(range_rate_ms: float) -> float:
    """Closing (negative range rate) -> positive shift."""
    return -FREQ_HZ * range_rate_ms / C


def from_ownship(o: dict) -> dict:
    """OWNSHIP/TRUTH aircraft -> rf state dict."""
    return {"lat": o["lat"], "lon": o["lon"], "alt_ft": o.get("alt_press_ft", o.get("alt_msl_ft", 0)),
            "gs_kt": o.get("gs_kt", 0), "track_deg": o.get("track_deg", 0), "vs_fpm": o.get("vs_fpm", 0)}


def from_state_body(b: dict) -> dict:
    return {"lat": b["lat"], "lon": b["lon"], "alt_ft": b.get("alt_press_ft", 0),
            "gs_kt": b.get("gs_kt", 0), "track_deg": b.get("track_deg", 0), "vs_fpm": b.get("vs_fpm", 0)}
