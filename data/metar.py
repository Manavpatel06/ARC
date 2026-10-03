"""
data/metar.py — Lane D. Live KDVT weather → density altitude, climb capability, wind.

    from data.metar import load, climb_fpm, wind_vector_ms
    wx = load()               # real cache if fetched, else committed sample
    wx["density_altitude_ft"], wx["wind_dir_deg"], wx["wind_kt"]
    climb_fpm(wx["density_altitude_ft"])     # C172S-class: 730 @ 0, 500 @ 5,000, 300 @ 8,000 ft DA
    we, wn = wind_vector_ms(wx)              # wind velocity (blowing TOWARD) east/north, m/s

Ground velocity = air velocity + wind vector. So gs/track differ from ias/heading.

Run:  python data/metar.py            # fetch aviationweather.gov, write cache, print summary
      python data/metar.py --sample   # recompute the committed sample from its own temp/altimeter
"""
from __future__ import annotations
import json, math, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
REAL = os.path.join(CACHE, "metar_kdvt.json")
SAMPLE = os.path.join(CACHE, "metar_kdvt.sample.json")
FIELD_ELEV_FT = 1478.0
URL = "https://aviationweather.gov/api/data/metar?ids=KDVT&format=json"

def pressure_altitude_ft(elev_ft: float, altimeter_inhg: float) -> float:
    return elev_ft + (29.92 - altimeter_inhg) * 1000.0

def density_altitude_ft(elev_ft: float, altimeter_inhg: float, temp_c: float) -> float:
    pa = pressure_altitude_ft(elev_ft, altimeter_inhg)
    isa_c = 15.0 - 1.98 * pa / 1000.0
    return pa + 118.8 * (temp_c - isa_c)

def climb_fpm(da_ft: float) -> float:
    pts = [(0, 730.0), (5000, 500.0), (8000, 300.0), (12000, 100.0)]
    da = max(0.0, da_ft)
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if da <= x1:
            return y0 + (y1 - y0) * (da - x0) / (x1 - x0)
    return pts[-1][1]

def wind_vector_ms(wx: dict) -> tuple[float, float]:
    """METAR wind is the direction it blows FROM; return the vector it blows TOWARD (east, north) in m/s."""
    spd = wx.get("wind_kt", 0) * 0.514444
    to = math.radians((wx.get("wind_dir_deg", 0) + 180) % 360)
    return spd * math.sin(to), spd * math.cos(to)

def enrich(temp_c, dewp_c, alt_inhg, wdir, wkt, observed, raw="") -> dict:
    da = density_altitude_ft(FIELD_ELEV_FT, alt_inhg, temp_c)
    return {"station": "KDVT", "observed": observed, "raw": raw,
            "temp_c": temp_c, "dewpoint_c": dewp_c, "altimeter_inhg": round(alt_inhg, 2),
            "wind_dir_deg": wdir, "wind_kt": wkt, "field_elev_ft": FIELD_ELEV_FT,
            "pressure_altitude_ft": round(pressure_altitude_ft(FIELD_ELEV_FT, alt_inhg)),
            "density_altitude_ft": round(da), "climb_fpm_c172s": round(climb_fpm(da)),
            "fetched_unix": time.time()}

def fetch() -> dict:
    import requests
    r = requests.get(URL, timeout=10)
    r.raise_for_status()
    m = r.json()[0]
    alt = m.get("altim")                       # hPa in the JSON API
    alt_inhg = alt / 33.8639 if alt and alt > 100 else (alt or 29.92)
    wdir = m.get("wdir") if isinstance(m.get("wdir"), (int, float)) else 0   # "VRB" -> 0
    return enrich(m["temp"], m.get("dewp"), alt_inhg, wdir, m.get("wspd", 0), m.get("reportTime", ""), m.get("rawOb", ""))

def load(mode: str = "live") -> dict:
    """Weather for the sim. Never raises.
    mode="live"   (default) latest fetched METAR, falling back to the committed sample.
    mode="cached" ALWAYS the committed sample (34 °C, 29.92, wind 250/8). Conflict scenarios are tuned
                  under this weather; with live wind the tuned encounter can miss by thousands of feet.
                  World: load(scenario.weather) — scenario files say "weather": "cached" or "live"."""
    paths = (SAMPLE,) if str(mode).startswith("cached") else (REAL, SAMPLE)
    for p in paths:
        try:
            return json.load(open(p))
        except Exception:
            continue
    return enrich(34, 6, 29.92, 250, 8, "default")

if __name__ == "__main__":
    os.makedirs(CACHE, exist_ok=True)
    if "--sample" in sys.argv:
        wx = enrich(34, 6, 29.92, 250, 8, "2026-10-03T21:53Z")
        wx["note"] = "SAMPLE for development. `python data/metar.py` writes metar_kdvt.json with the live observation."
        json.dump(wx, open(SAMPLE, "w"), indent=1)
    else:
        try:
            wx = fetch()
            json.dump(wx, open(REAL, "w"), indent=1)
        except Exception as e:
            print(f"fetch failed ({e}); using last good / sample")
            wx = load()
    print(f"KDVT {wx['temp_c']}°C {wx['altimeter_inhg']} → DA {wx['density_altitude_ft']:,} ft → C172 climb ≈ {wx['climb_fpm_c172s']} fpm; wind {wx['wind_dir_deg']:03d}/{wx['wind_kt']} kt")
