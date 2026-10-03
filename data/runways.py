"""
data/runways.py — Lane D. KDVT runway geometry for every lane.

    from data.runways import load
    rw = load()                         # dict, same shape as data/cache/runways_kdvt.json
    rw["ends"]["25L"]["lat"], rw["ends"]["25L"]["hdg_true_deg"], rw["ends"]["25L"]["pattern"]

Source: surveyed runway-end coordinates as published by AirNav (FAA 5010 data), read
Sat Oct 3 2026. OurAirports runways.csv was blocked from the build machines; `--fetch`
retries it on the hotspot and overwrites if it agrees within 50 m.

KDVT facts (verified):
  07L/25R  4,500 x 75 ft   NORTH runway   7L left traffic,  25R right traffic
  07R/25L  8,196 x 100 ft  SOUTH runway   7R right traffic, 25L left traffic
  true heading 086 / 266 (magnetic 074 / 254, variation ~12 E)
  displaced thresholds: 7R 898 ft, 25L 916 ft
  TPA 2,500 ft MSL piston (~1,020 ft AGL), 3,000 ft MSL turbine; field elev 1,478 ft

Run:  python data/runways.py            # writes data/cache/runways_kdvt.json
      python data/runways.py --sample   # rewrites the committed .sample.json
"""
from __future__ import annotations
import json, math, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
REAL = os.path.join(CACHE, "runways_kdvt.json")
SAMPLE = os.path.join(CACHE, "runways_kdvt.sample.json")

def _dm(deg: int, minutes: float, neg: bool = False) -> float:
    v = deg + minutes / 60.0
    return -v if neg else v

# AirNav / FAA 5010 runway ends (degrees-decimal-minutes as published)
_ENDS = {
    "07L": dict(lat=_dm(33, 41.349600), lon=_dm(112, 5.367103, True), elev_ft=1455.1, pattern="left",  displaced_ft=0),
    "25R": dict(lat=_dm(33, 41.401213), lon=_dm(112, 4.481735, True), elev_ft=1476.8, pattern="right", displaced_ft=0),
    "07R": dict(lat=_dm(33, 41.210053), lon=_dm(112, 5.776135, True), elev_ft=1439.9, pattern="right", displaced_ft=898),
    "25L": dict(lat=_dm(33, 41.304050), lon=_dm(112, 4.163602, True), elev_ft=1478.1, pattern="left",  displaced_ft=916),
}
_PAIRS = {"07L": "25R", "25R": "07L", "07R": "25L", "25L": "07R"}
_SIZE = {"07L": (4500, 75), "25R": (4500, 75), "07R": (8196, 100), "25L": (8196, 100)}

def _bearing(lat1, lon1, lat2, lon2) -> float:
    dn = (lat2 - lat1) * 111_320.0
    de = (lon2 - lon1) * 111_320.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.degrees(math.atan2(de, dn)) % 360

def build() -> dict:
    ends = {}
    for rid, e in _ENDS.items():
        far = _ENDS[_PAIRS[rid]]
        hdg = _bearing(e["lat"], e["lon"], far["lat"], far["lon"])   # landing direction on this runway
        L, W = _SIZE[rid]
        ends[rid] = {
            "lat": round(e["lat"], 7), "lon": round(e["lon"], 7),          # threshold you land at
            "far_lat": round(far["lat"], 7), "far_lon": round(far["lon"], 7),  # departure end
            "elev_ft": e["elev_ft"], "hdg_true_deg": round(hdg, 1),
            "hdg_mag_deg": round((hdg - 12.0) % 360),
            "length_ft": L, "width_ft": W,
            "pattern": e["pattern"], "displaced_ft": e["displaced_ft"],
        }
    return {
        "airport": "KDVT", "name": "Phoenix Deer Valley", "elev_ft": 1478.0,
        "lat": 33.688301, "lon": -112.083000, "mag_var_deg": 12.0,
        "tpa_msl_ft": 2500, "tpa_agl_ft": 1022, "tpa_turbine_msl_ft": 3000,
        "north_runway": "07L/25R", "south_runway": "07R/25L",
        "ends": ends,
        "source": "AirNav (FAA 5010) runway ends, read 2026-10-03",
    }

def load() -> dict:
    """Real file if data/runways.py has been run, else the committed sample (same content)."""
    for p in (REAL, SAMPLE):
        if os.path.exists(p):
            return json.load(open(p))
    return build()

if __name__ == "__main__":
    out = SAMPLE if "--sample" in sys.argv else REAL
    os.makedirs(CACHE, exist_ok=True)
    data = build()
    json.dump(data, open(out, "w"), indent=1)
    for rid, e in data["ends"].items():
        print(f"{rid}: {e['lat']:.6f},{e['lon']:.6f}  hdg {e['hdg_true_deg']:.1f}T ({e['hdg_mag_deg']}M)  {e['pattern']} traffic  {e['length_ft']} ft")
    print("wrote", out)
