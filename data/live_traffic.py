"""
data/live_traffic.py — Lane D. REAL aircraft around Deer Valley, live, into the god view.

Pulls ADS-B positions within --radius NM of KDVT every --every seconds from a free public feed
(airplanes.live -> adsb.lol -> OpenSky, first one that answers), classifies each real aircraft's
traffic-pattern leg with the SAME geometry FLOCK uses (pattern.py), and sends one LIVE_TRAFFIC frame
to the world as role `data`. The world forwards it to the god view (overlay) and the log.

Honesty rule: live aircraft are DISPLAY + PREDICTION ONLY. FLOCK nodes never see them and never act
on them. (They carry no FLOCK radio; in the real system they would be ADS-B-only targets.)

    python data/live_traffic.py --print                 # no world needed: prints a table every 5 s
    python data/live_traffic.py --world ws://localhost:8765
    python data/live_traffic.py --world ws://localhost:8765 --record      # also saves harness/out/live_*.jsonl
    python data/live_traffic.py --world ws://localhost:8765 --replay harness/out/live_XXXX.jsonl   # offline fallback

Frame (INTERFACE.md v1.2, additive):
{"type":"LIVE_TRAFFIC","t":...,"source":"airplanes.live","radius_nm":25,
 "aircraft":[{"id":"a1b2c3","callsign":"N123AB","type":"C172","lat":..,"lon":..,"alt_msl_ft":2500,
              "gs_kt":92,"track_deg":86,"vs_fpm":0,"on_ground":false,"age_s":1.2,
              "leg":"DOWNWIND","runway":"07R","leg_conf":0.82,"next_leg":"BASE","in_pattern":true}]}
"""
from __future__ import annotations
import argparse, asyncio, json, math, os, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from pattern import legs, straight_in, to_enu, ELEV_FT, TPA_AGL_FT   # noqa: E402

LAT, LON = 33.688301, -112.083000
RUNWAYS = ("25L", "25R", "07L", "07R")
NEXT = {"UPWIND": "CROSSWIND", "CROSSWIND": "DOWNWIND", "DOWNWIND": "BASE", "BASE": "FINAL",
        "FINAL": "LANDING", "STRAIGHT_IN": "LANDING"}
CORRIDOR_M = 900.0          # how far off a leg's centre line still counts as "on" it
MAX_HDG_ERR = 40.0          # degrees
PATTERN_TOP_FT = ELEV_FT + TPA_AGL_FT + 800

# ---------------- feeds ----------------
def _get(url: str, timeout=6):
    import requests
    r = requests.get(url, timeout=timeout, headers={"User-Agent": "FLOCK-hackathon/1.0 (ASU Devils Invent)"})
    r.raise_for_status()
    return r.json()

def _from_readsb(js: dict, src: str) -> list[dict]:
    """airplanes.live / adsb.lol v2 format (readsb JSON)."""
    out = []
    for a in js.get("ac", []):
        if a.get("lat") is None or a.get("lon") is None:
            continue
        alt_baro = a.get("alt_baro")
        on_ground = alt_baro == "ground"
        alt = a.get("alt_geom") if isinstance(a.get("alt_geom"), (int, float)) else (alt_baro if isinstance(alt_baro, (int, float)) else ELEV_FT)
        out.append({"id": a.get("hex", "?"), "callsign": (a.get("flight") or a.get("r") or a.get("hex", "")).strip(),
                    "type": a.get("t", ""), "lat": a["lat"], "lon": a["lon"], "alt_msl_ft": float(alt),
                    "gs_kt": float(a.get("gs") or 0), "track_deg": float(a.get("track") or a.get("true_heading") or 0),
                    "vs_fpm": float(a.get("baro_rate") or a.get("geom_rate") or 0), "on_ground": on_ground,
                    "age_s": float(a.get("seen_pos", a.get("seen", 0)) or 0)})
    return out

def _from_opensky(js: dict) -> list[dict]:
    out = []
    now = js.get("time", time.time())
    for s in js.get("states") or []:
        icao, cs, _, tpos, _, lon, lat, baro_m, gnd, vel, trk, vr, _, geo_m = s[:14]
        if lat is None or lon is None:
            continue
        alt_m = geo_m if geo_m is not None else baro_m
        out.append({"id": icao, "callsign": (cs or icao).strip(), "type": "", "lat": lat, "lon": lon,
                    "alt_msl_ft": (alt_m or ELEV_FT * 0.3048) / 0.3048, "gs_kt": (vel or 0) / 0.514444,
                    "track_deg": trk or 0, "vs_fpm": (vr or 0) * 196.85, "on_ground": bool(gnd),
                    "age_s": max(0.0, now - (tpos or now))})
    return out

def fetch(radius_nm: float) -> tuple[str, list[dict]]:
    errors = []
    for name, url in (("airplanes.live", f"https://api.airplanes.live/v2/point/{LAT}/{LON}/{int(radius_nm)}"),
                      ("adsb.lol", f"https://api.adsb.lol/v2/point/{LAT}/{LON}/{int(radius_nm)}")):
        try:
            return name, _from_readsb(_get(url), name)
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}")
    d = radius_nm / 60.0
    try:
        js = _get(f"https://opensky-network.org/api/states/all?lamin={LAT-d}&lamax={LAT+d}"
                  f"&lomin={LON-d/0.83}&lomax={LON+d/0.83}", timeout=10)
        return "opensky", _from_opensky(js)
    except Exception as e:
        errors.append(f"opensky: {type(e).__name__}")
    raise RuntimeError("; ".join(errors))

# ---------------- leg classification (same geometry as the nodes) ----------------
_SEGS = [(rw, seg) for rw in RUNWAYS for seg in legs(rw) + [straight_in(rw)]]

def _ang(a, b):
    return abs((a - b + 180) % 360 - 180)

def classify(a: dict) -> dict:
    """Best-matching pattern leg for a real aircraft, with a 0-1 confidence. Display/prediction only."""
    if a["on_ground"]:
        return {"leg": "GROUND", "runway": None, "leg_conf": 1.0, "next_leg": None, "in_pattern": False}
    if a["alt_msl_ft"] > PATTERN_TOP_FT:
        return {"leg": "ENROUTE", "runway": None, "leg_conf": 1.0, "next_leg": None, "in_pattern": False}
    x, y = to_enu(a["lat"], a["lon"])
    best = None
    for rw, seg in _SEGS:
        (ax, ay), (bx, by) = seg["start"], seg["end"]
        L = max(1.0, seg["length_m"])
        ux, uy = (bx - ax) / L, (by - ay) / L
        along = (x - ax) * ux + (y - ay) * uy
        cross = abs((x - ax) * uy - (y - ay) * ux)
        if along < -500 or along > L + 500 or cross > CORRIDOR_M:
            continue
        herr = _ang(a["track_deg"], seg["hdg_deg"])
        if herr > MAX_HDG_ERR:
            continue
        f = min(1.0, max(0.0, along / L))
        want_ft = ELEV_FT + seg["agl0_ft"] + f * (seg["agl1_ft"] - seg["agl0_ft"])
        aerr = abs(a["alt_msl_ft"] - want_ft)
        score = (1 - cross / CORRIDOR_M) * 0.4 + (1 - herr / MAX_HDG_ERR) * 0.4 + max(0.0, 1 - aerr / 800) * 0.2
        if best is None or score > best[0]:
            best = (score, rw, seg["name"])
    if best is None or best[0] < 0.35:
        return {"leg": "UNKNOWN", "runway": None, "leg_conf": 0.0, "next_leg": None, "in_pattern": False}
    s, rw, name = best
    return {"leg": name, "runway": rw, "leg_conf": round(s, 2), "next_leg": NEXT.get(name), "in_pattern": True}

def frame(source: str, radius_nm: float, raw: list[dict]) -> dict:
    ac = []
    for a in raw:
        a = dict(a); a.update(classify(a))
        for k in ("alt_msl_ft", "gs_kt", "track_deg", "vs_fpm", "age_s"):
            a[k] = round(a[k], 1)
        ac.append(a)
    ac.sort(key=lambda a: (not a["in_pattern"], a["leg"] == "ENROUTE", a["callsign"]))
    return {"type": "LIVE_TRAFFIC", "t": time.time(), "source": source, "radius_nm": radius_nm, "aircraft": ac}

def table(f: dict) -> str:
    rows = [f"{time.strftime('%H:%M:%S')}  {len(f['aircraft'])} real aircraft within {f['radius_nm']} NM of KDVT ({f['source']}), "
            f"{sum(a['in_pattern'] for a in f['aircraft'])} in a KDVT pattern"]
    for a in f["aircraft"][:25]:
        leg = f"{a['leg']}{' ' + a['runway'] if a['runway'] else ''}" + (f" ({a['leg_conf']:.2f}) next {a['next_leg']}" if a["in_pattern"] else "")
        rows.append(f"  {a['callsign'][:8]:8s} {a['type'][:4]:4s} {a['alt_msl_ft']:6.0f} ft {a['gs_kt']:4.0f} kt trk {a['track_deg']:3.0f}  {leg}")
    return "\n".join(rows)

# ---------------- main ----------------
async def run(args):
    ws = None
    if args.world:
        import websockets
        ws = await websockets.connect(f"{args.world}?role=data")
        print(f"[live] connected to {args.world} as data")
    rec = None
    if args.record:
        os.makedirs(os.path.join(ROOT, "harness", "out"), exist_ok=True)
        rec = open(os.path.join(ROOT, "harness", "out", f"live_{int(time.time())}.jsonl"), "a")
    replay = [json.loads(l) for l in open(args.replay)] if args.replay else None
    i = 0
    while True:
        try:
            if replay:
                f = replay[i % len(replay)]; f = dict(f, t=time.time(), source=f"replay:{f.get('source')}"); i += 1
            else:
                src, raw = await asyncio.to_thread(fetch, args.radius)
                f = frame(src, args.radius, raw)
            if rec:
                rec.write(json.dumps(f) + "\n"); rec.flush()
            if ws:
                await ws.send(json.dumps(f))
            print(table(f) if (args.print or not ws) else f"[live] {len(f['aircraft'])} aircraft, {sum(a['in_pattern'] for a in f['aircraft'])} in pattern ({f['source']})")
        except Exception as e:
            print(f"[live] fetch failed: {e} — retrying (use --replay <file> for an offline demo)")
        await asyncio.sleep(args.every)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", help="ws://<world-ip>:8765 (omit to just print)")
    ap.add_argument("--radius", type=float, default=25, help="NM around KDVT")
    ap.add_argument("--every", type=float, default=5, help="seconds between polls (keep >= 5: free feeds)")
    ap.add_argument("--print", action="store_true", help="print the full table even when sending to the world")
    ap.add_argument("--record", action="store_true", help="append every frame to harness/out/live_<ts>.jsonl")
    ap.add_argument("--replay", help="replay a recorded live_*.jsonl instead of fetching (offline fallback)")
    try:
        asyncio.run(run(ap.parse_args()))
    except KeyboardInterrupt:
        pass
