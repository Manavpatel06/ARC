"""
data/build_terrain.py — Lane D, one-time builder. USGS 3DEP 1-arc-second GeoTIFFs -> the small grid every
node and the world load through data/terrain.py.

    python data/build_terrain.py ..\\USGS_1_n34w113.tif ..\\USGS_1_n34w112.tif     # writes data/cache/terrain_kdvt.npy + terrain_meta.json

Grid: ±15 NM around KDVT, ~90 m cells. Each cell holds the MAXIMUM elevation of the 3x3 source cells it
covers (conservative: a terrain-floor check can only err on the safe side). Source: USGS 3D Elevation
Program 1 arc-second DEM tiles n34w113 + n34w112 (downloaded Sat Oct 3 2026).
Needs: pip install tifffile imagecodecs
"""
from __future__ import annotations
import json, math, os, sys
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
LAT0, LON0 = 33.688301, -112.083000
HALF_DEG_LAT = 15 / 60.0
HALF_DEG_LON = 15 / 60.0 / math.cos(math.radians(LAT0))
POOL = 3

def read_tile(path):
    import tifffile
    with tifffile.TiffFile(path) as t:
        p = t.pages[0]
        arr = p.asarray().astype(np.float32)
        sx, sy, _ = p.tags[33550].value
        tie = p.tags[33922].value
        west, north = tie[3], tie[4]
        nod = p.tags.get(42113)
        if nod is not None:
            arr[arr <= float(nod.value) + 1] = np.nan
    return arr, west, north, sx, sy

def build(paths):
    tiles = [read_tile(p) for p in paths]
    sx = tiles[0][3]; sy = tiles[0][4]
    south, north = LAT0 - HALF_DEG_LAT, LAT0 + HALF_DEG_LAT
    west, east = LON0 - HALF_DEG_LON, LON0 + HALF_DEG_LON
    rows = int(round((north - south) / sy)); cols = int(round((east - west) / sx))
    mosaic = np.full((rows, cols), np.nan, np.float32)
    for arr, tw, tn, _, _ in tiles:
        # source pixel (i, j) centre: lat = tn - (i+0.5)*sy, lon = tw + (j+0.5)*sx ; mosaic row 0 = north edge
        i0 = int(round((tn - north) / sy)); j0 = int(round((west - tw) / sx))
        r_src = slice(max(0, i0), min(arr.shape[0], i0 + rows))
        c_src = slice(max(0, j0), min(arr.shape[1], j0 + cols))
        r_dst = slice(r_src.start - i0, r_src.stop - i0)
        c_dst = slice(c_src.start - j0, c_src.stop - j0)
        if r_dst.stop > r_dst.start and c_dst.stop > c_dst.start:
            blk = arr[r_src, c_src]
            cur = mosaic[r_dst, c_dst]
            mosaic[r_dst, c_dst] = np.where(np.isnan(cur), blk, np.fmax(cur, blk))
    if np.isnan(mosaic).any():
        raise SystemExit(f"{np.isnan(mosaic).mean():.1%} of the area has no data — are both tiles given?")
    R, C = rows // POOL, cols // POOL
    pooled = mosaic[:R * POOL, :C * POOL].reshape(R, POOL, C, POOL).max(axis=(1, 3))
    grid = pooled[::-1].copy()                    # row 0 = SOUTH edge (data/terrain.py convention)
    meta = {"grid": "USGS 3DEP 1 arc-second, max-pooled 3x3", "lat0": south, "lon0": west,
            "dlat": sy * POOL, "dlon": sx * POOL, "rows": R, "cols": C, "units": "m MSL",
            "elev_m_default": float(np.nanmedian(grid)), "source_tiles": [os.path.basename(p) for p in paths]}
    return grid, meta

if __name__ == "__main__":
    paths = sys.argv[1:] or [os.path.join(HERE, "..", "..", f) for f in ("USGS_1_n34w113.tif", "USGS_1_n34w112.tif")]
    grid, meta = build(paths)
    os.makedirs(CACHE, exist_ok=True)
    np.save(os.path.join(CACHE, "terrain_kdvt.npy"), grid)
    json.dump(meta, open(os.path.join(CACHE, "terrain_meta.json"), "w"), indent=1)
    print(f"terrain {meta['rows']}x{meta['cols']} cells ~{meta['dlat']*111320:.0f} m, "
          f"{grid.min()/0.3048:.0f}-{grid.max()/0.3048:.0f} ft MSL -> data/cache/terrain_kdvt.npy")
