"""
pattern.py — shared KDVT traffic-pattern geometry. ONE definition used by:
  Lane A  world/traffic.py + world/scenario.py  (spawn aircraft, fly the legs)
  Lane B  node/geometry.py + node/predict.py    (classify legs, predict the next turn)
  Lane A  web god view                          (draw the pattern: `python pattern.py --geojson`)
Airport layout is public knowledge, so nodes may import this without breaking "nodes see only
what a real node would see".

Conventions
  * Headings are TRUE degrees (lat/lon space). KDVT runways are 086/266 true (074/254 magnetic).
  * Local frame ENU in meters around the airport reference point (east x, north y, up z).
  * Per-runway frame: u = distance along the landing direction from the landing threshold,
    v = distance to the RIGHT of the landing direction. Left traffic flies at v < 0.

Pattern (C172 at ~90 kt; tune constants below, nowhere else):
  UPWIND      runway far end -> +UPWIND_M past it, climbing to TPA
  CROSSWIND   turn toward the pattern side, out to the downwind offset
  DOWNWIND    opposite the landing direction at TPA, to BASE_TURN_U before the threshold
  BASE        back toward the centerline, descending
  FINAL       centerline to threshold, ~3.5° path
  STRAIGHT_IN centerline from STRAIGHT_IN_U at TPA, 3° path, joins FINAL
  GO_AROUND   same path as UPWIND, from the threshold
For 25L (left traffic, south side): downwind 086, base 356, final 266.
For 25R (right traffic, north side): downwind 086, base 176, final 266.

    from pattern import place, legs, to_enu, from_enu
    p = place("DOWNWIND", "25L", offset_s=0, gs_kt=90)   # -> {"lat","lon","alt_msl_ft","agl_ft","hdg_deg","leg","runway"}
    p = place("FINAL", "25L", offset_s=0, agl_ft=350)    # agl_ft overrides the leg's altitude
    p = place("BASE", "25L", offset_s=-5)                # negative: that many seconds BEFORE the leg starts (on DOWNWIND)
    for leg in legs("25L"): leg["name"], leg["start"], leg["end"], leg["hdg_deg"]   # ENU segments for the classifier

offset_s = seconds already flown along the circuit from the START of that leg, at gs_kt.
Positive past the leg's end continues onto the next leg; negative goes back onto the previous leg.

Run:  python pattern.py              # prints every leg for 25L and 25R + self-checks
      python pattern.py --geojson    # writes web/pattern_kdvt.geojson for the god view
"""
from __future__ import annotations
import json, math, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from data.runways import load as _load_runways

KT = 0.514444
FT = 0.3048

# ---- tunables (meters) --------------------------------------------------------------
DOWNWIND_OFFSET_M = 1400.0     # ~0.75 NM abeam the runway
UPWIND_M = 900.0               # climb-out past the departure end before crosswind
BASE_TURN_U = -2200.0          # downwind extends this far past the threshold before base
STRAIGHT_IN_U = -5800.0        # straight-in joins at TPA ~3.1 NM out on a 3° path
BASE_START_AGL_FT = 1000.0
FINAL_START_AGL_FT = 600.0

_RW = _load_runways()
REF_LAT, REF_LON = _RW["lat"], _RW["lon"]
ELEV_FT = _RW["elev_ft"]
TPA_AGL_FT = _RW["tpa_msl_ft"] - ELEV_FT   # ~1,022 ft

# ---- geo ----------------------------------------------------------------------------
_M_PER_DEG_LAT = 111_320.0
_M_PER_DEG_LON = 111_320.0 * math.cos(math.radians(REF_LAT))

def to_enu(lat: float, lon: float) -> tuple[float, float]:
    return (lon - REF_LON) * _M_PER_DEG_LON, (lat - REF_LAT) * _M_PER_DEG_LAT

def from_enu(x: float, y: float) -> tuple[float, float]:
    return REF_LAT + y / _M_PER_DEG_LAT, REF_LON + x / _M_PER_DEG_LON

def hdg_of(dx: float, dy: float) -> float:
    return math.degrees(math.atan2(dx, dy)) % 360

# ---- per-runway frame -----------------------------------------------------------------
def _frame(runway: str):
    e = _RW["ends"][runway]
    tx, ty = to_enu(e["lat"], e["lon"])
    fx, fy = to_enu(e["far_lat"], e["far_lon"])
    L = math.hypot(fx - tx, fy - ty)
    ux, uy = (fx - tx) / L, (fy - ty) / L          # landing direction
    rx, ry = uy, -ux                               # unit vector to the right of landing direction
    side = -1.0 if e["pattern"] == "left" else 1.0
    return tx, ty, ux, uy, rx, ry, L, side

def _pt(fr, u: float, v: float) -> tuple[float, float]:
    tx, ty, ux, uy, rx, ry, _, _ = fr
    return tx + u * ux + v * rx, ty + u * uy + v * ry

def legs(runway: str = "25L") -> list[dict]:
    """Circuit legs as straight ENU segments with altitudes AGL at each end (in circuit order)."""
    fr = _frame(runway)
    L, s, d = fr[6], fr[7], DOWNWIND_OFFSET_M
    tpa = TPA_AGL_FT
    spec = [
        ("UPWIND",    (L, 0),                    (L + UPWIND_M, 0),            400.0,             tpa * 0.85),
        ("CROSSWIND", (L + UPWIND_M, 0),         (L + UPWIND_M, s * d),        tpa * 0.85,        tpa),
        ("DOWNWIND",  (L + UPWIND_M, s * d),     (BASE_TURN_U, s * d),         tpa,               tpa),
        ("BASE",      (BASE_TURN_U, s * d),      (BASE_TURN_U, 0),             BASE_START_AGL_FT, FINAL_START_AGL_FT),
        ("FINAL",     (BASE_TURN_U, 0),          (0, 0),                       FINAL_START_AGL_FT, 0.0),
    ]
    out = []
    for name, a, b, z0, z1 in spec:
        ax, ay = _pt(fr, *a); bx, by = _pt(fr, *b)
        out.append({"name": name, "runway": runway, "start": (ax, ay), "end": (bx, by),
                    "agl0_ft": z0, "agl1_ft": z1, "hdg_deg": round(hdg_of(bx - ax, by - ay), 1),
                    "length_m": math.hypot(bx - ax, by - ay)})
    return out

def straight_in(runway: str = "25L") -> dict:
    fr = _frame(runway)
    ax, ay = _pt(fr, STRAIGHT_IN_U, 0); bx, by = _pt(fr, 0, 0)
    return {"name": "STRAIGHT_IN", "runway": runway, "start": (ax, ay), "end": (bx, by),
            "agl0_ft": TPA_AGL_FT, "agl1_ft": 0.0, "hdg_deg": round(hdg_of(bx - ax, by - ay), 1),
            "length_m": math.hypot(bx - ax, by - ay)}

def _on_segment(seg: dict, dist_m: float) -> tuple[float, float, float]:
    f = 0.0 if seg["length_m"] == 0 else max(0.0, min(1.0, dist_m / seg["length_m"]))
    (ax, ay), (bx, by) = seg["start"], seg["end"]
    return ax + f * (bx - ax), ay + f * (by - ay), seg["agl0_ft"] + f * (seg["agl1_ft"] - seg["agl0_ft"])

def place(leg: str, runway: str = "25L", offset_s: float = 0.0, gs_kt: float = 90.0, agl_ft: float | None = None) -> dict:
    """Scenario start {leg, runway, offset_s[, agl_ft]} -> position, heading, altitude."""
    leg = leg.upper()
    dist = offset_s * gs_kt * KT
    if leg in ("STRAIGHT_IN",):
        seg = straight_in(runway)
        d = max(0.0, min(seg["length_m"], dist))
        x, y, z = _on_segment(seg, d)
        name = "STRAIGHT_IN"
    else:
        circuit = legs(runway)
        names = [c["name"] for c in circuit]
        if leg == "GO_AROUND":
            leg = "UPWIND"
        i = names.index(leg)
        # walk forward/backward around the closed circuit
        while dist < 0:
            i = (i - 1) % len(circuit); dist += circuit[i]["length_m"]
        while dist > circuit[i]["length_m"]:
            dist -= circuit[i]["length_m"]; i = (i + 1) % len(circuit)
        seg = circuit[i]
        x, y, z = _on_segment(seg, dist)
        name = seg["name"]
    if agl_ft is not None:
        z = agl_ft
    lat, lon = from_enu(x, y)
    return {"lat": round(lat, 6), "lon": round(lon, 6), "agl_ft": round(z, 1), "alt_msl_ft": round(ELEV_FT + z, 1),
            "hdg_deg": seg["hdg_deg"], "leg": name, "runway": runway, "x_m": round(x, 1), "y_m": round(y, 1)}

def geojson(runways=("25L", "25R")) -> dict:
    feats = []
    for rid, e in _RW["ends"].items():
        if rid in ("07L", "07R"):
            feats.append({"type": "Feature", "properties": {"kind": "runway", "id": f"{rid}/{'25R' if rid=='07L' else '25L'}"},
                          "geometry": {"type": "LineString", "coordinates": [[e["lon"], e["lat"]], [e["far_lon"], e["far_lat"]]]}})
    for rw in runways:
        for seg in legs(rw) + [straight_in(rw)]:
            a = from_enu(*seg["start"]); b = from_enu(*seg["end"])
            feats.append({"type": "Feature", "properties": {"kind": "leg", "runway": rw, "leg": seg["name"], "hdg_deg": seg["hdg_deg"]},
                          "geometry": {"type": "LineString", "coordinates": [[a[1], a[0]], [b[1], b[0]]]}})
    return {"type": "FeatureCollection", "features": feats}

if __name__ == "__main__":
    if "--geojson" in sys.argv:
        os.makedirs("web", exist_ok=True)
        json.dump(geojson(), open("web/pattern_kdvt.geojson", "w"))
        print("wrote web/pattern_kdvt.geojson"); sys.exit()
    for rw in ("25L", "25R"):
        print(f"runway {rw} ({_RW['ends'][rw]['pattern']} traffic)")
        for g in legs(rw) + [straight_in(rw)]:
            print(f"  {g['name']:11s} hdg {g['hdg_deg']:5.1f}T  {g['length_m']:6.0f} m  {g['agl0_ft']:5.0f}->{g['agl1_ft']:5.0f} ft AGL")
    # self-checks
    dw = place("DOWNWIND", "25L"); assert abs(dw["hdg_deg"] - 86) < 2, dw
    rr = place("DOWNWIND", "25R")
    assert dw["y_m"] < 0 < rr["y_m"], "25L pattern must be south of field, 25R north"
    assert abs(place("BASE", "25L")["hdg_deg"] - 356) < 2 and abs(place("BASE", "25R")["hdg_deg"] - 176) < 2
    assert place("BASE", "25L", offset_s=-5)["leg"] == "DOWNWIND"
    assert place("FINAL", "25L", agl_ft=350)["agl_ft"] == 350
    print("self-checks OK:", dw)
