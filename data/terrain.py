"""
data/terrain.py — Lane D. elev_at(lat, lon) in meters MSL.
Until the real grid is downloaded (D4), this returns the flat sample so Lane B can import it today.
"""
from __future__ import annotations
import json, os
import numpy as np

_HERE = os.path.dirname(__file__)
_CACHE = os.path.join(_HERE, "cache")
_grid = None
_meta = None

def _load():
    global _grid, _meta
    if _meta is not None:
        return
    real_meta, real_npy = os.path.join(_CACHE, "terrain_meta.json"), os.path.join(_CACHE, "terrain_kdvt.npy")
    if os.path.exists(real_meta) and os.path.exists(real_npy):
        _meta = json.load(open(real_meta)); _grid = np.load(real_npy)
    else:
        _meta = json.load(open(os.path.join(_CACHE, "terrain_meta.sample.json"))); _grid = None

def elev_at(lat: float, lon: float) -> float:
    """Terrain elevation in meters MSL at lat/lon. Flat sample until the real grid exists."""
    _load()
    if _grid is None:
        return float(_meta["elev_m_default"])
    r = int((lat - _meta["lat0"]) / _meta["dlat"]); c = int((lon - _meta["lon0"]) / _meta["dlon"])
    r = min(max(r, 0), _meta["rows"] - 1); c = min(max(c, 0), _meta["cols"] - 1)
    return float(_grid[r, c])

def elev_at_ft(lat: float, lon: float) -> float:
    return elev_at(lat, lon) / 0.3048

if __name__ == "__main__":
    print("elev at KDVT:", round(elev_at_ft(33.6883, -112.0826)), "ft", "(sample)" if _grid is None else "(real grid)")
