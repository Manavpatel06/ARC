"""
data/obstacles.py — Lane D. FAA Digital Obstacle File (DOF) around KDVT: towers, antennas, buildings,
power lines, cranes — every charted man-made obstacle, with its height above ground and above sea level.

    python data/obstacles.py ..\\DOF_260927.zip       # one-time: writes data/cache/obstacles_kdvt.csv (±15 NM)

    from data.obstacles import load, top_at
    obs = load()                     # list of dicts: id, type, lat, lon, agl_ft, amsl_ft, lit
    top_at(lat, lon)                 # highest obstacle top (m MSL) within the protection radius, or None
                                     # -> node/escape.py obstacle_fn (positions in lat/lon)

Protection radius: 600 m horizontally (a maneuver path inside this cylinder must clear the top by the
same 300 ft the terrain floor uses). Source: FAA DOF cycle 260927 (Sept 27 2026), file 04-AZ.Dat.
"""
from __future__ import annotations
import csv, io, math, os, re, sys, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
OUT = os.path.join(CACHE, "obstacles_kdvt.csv")
LAT0, LON0 = 33.688301, -112.083000
RADIUS_NM = 15.0
PROTECT_M = 600.0
FT = 0.3048
_LINE = re.compile(r"^(\d\d-\d{6})\s+\S\s+US\s+AZ\s+(.{16})\s+(\d+)\s+(\d+)\s+([\d.]+)([NS])\s+(\d+)\s+(\d+)\s+([\d.]+)([EW])\s+(.{18})\s+(\d+)\s+(\d{5})\s+(\d{5})\s+(\S)")

def _dist_nm(lat, lon):
    return math.hypot((lat - LAT0) * 60.0, (lon - LON0) * 60.0 * math.cos(math.radians(LAT0)))

def parse_dof(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        (oid, city, la_d, la_m, la_s, la_h, lo_d, lo_m, lo_s, lo_h, typ, qty, agl, amsl, lit) = m.groups()
        lat = (int(la_d) + int(la_m) / 60 + float(la_s) / 3600) * (1 if la_h == "N" else -1)
        lon = (int(lo_d) + int(lo_m) / 60 + float(lo_s) / 3600) * (-1 if lo_h == "W" else 1)
        if _dist_nm(lat, lon) > RADIUS_NM:
            continue
        out.append({"id": oid, "type": typ.strip(), "city": city.strip(), "lat": round(lat, 6), "lon": round(lon, 6),
                    "agl_ft": int(agl), "amsl_ft": int(amsl), "lit": lit})
    return out

def build(src: str) -> list[dict]:
    if src.lower().endswith(".zip"):
        with zipfile.ZipFile(src) as z:
            name = next(n for n in z.namelist() if n.endswith("04-AZ.Dat"))
            text = z.read(name).decode("latin-1")
    else:
        text = open(src, encoding="latin-1").read()
    return parse_dof(text)

_cache = None
def load() -> list[dict]:
    global _cache
    if _cache is None:
        _cache = []
        if os.path.exists(OUT):
            with open(OUT) as f:
                for r in csv.DictReader(f):
                    _cache.append({**r, "lat": float(r["lat"]), "lon": float(r["lon"]),
                                   "agl_ft": int(r["agl_ft"]), "amsl_ft": int(r["amsl_ft"])})
    return _cache

_CELL = 0.01                      # degrees (~1.1 km) spatial buckets
_index = None
def _build_index():
    global _index
    _index = {}
    for o in load():
        _index.setdefault((int(o["lat"] // _CELL), int(o["lon"] // _CELL)), []).append(o)

def top_at(lat: float, lon: float, protect_m: float = PROTECT_M):
    """Highest obstacle top in metres MSL within protect_m of (lat, lon), or None.
    Bucketed by 0.01° cells, so a lookup touches only a handful of obstacles (node-loop safe)."""
    if _index is None:
        _build_index()
    ci, cj = int(lat // _CELL), int(lon // _CELL)
    cos = math.cos(math.radians(lat)); r2 = protect_m * protect_m
    best = None
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            for o in _index.get((ci + di, cj + dj), ()):
                dn = (o["lat"] - lat) * 111_320.0
                de = (o["lon"] - lon) * 111_320.0 * cos
                if dn * dn + de * de <= r2:
                    top = o["amsl_ft"] * FT
                    if best is None or top > best:
                        best = top
    return best

def obstacle_fn_enu(to_latlon):
    """Adapter for node/escape.py, which calls obstacle_fn(x_m, y_m) in the node's ENU frame:
        from node.geometry import to_latlon; obstacle_fn = obstacle_fn_enu(to_latlon)"""
    return lambda x, y: top_at(*to_latlon(x, y))

if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "..", "DOF_260927.zip")
    obs = build(src)
    os.makedirs(CACHE, exist_ok=True)
    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["id", "type", "city", "lat", "lon", "agl_ft", "amsl_ft", "lit"])
        w.writeheader(); w.writerows(obs)
    tall = sorted(obs, key=lambda o: -o["agl_ft"])[:5]
    print(f"{len(obs)} obstacles within {RADIUS_NM:.0f} NM of KDVT -> {OUT}")
    for o in tall:
        print(f"  {o['type']:18s} {o['agl_ft']:5d} ft AGL  {o['amsl_ft']:5d} ft MSL  {_dist_nm(o['lat'], o['lon']):4.1f} NM  {o['city']}")
