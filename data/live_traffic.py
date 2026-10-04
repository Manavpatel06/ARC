"""
data/live_traffic.py — Lane D. REAL aircraft around Deer Valley, live, into the god view.

Pulls ADS-B positions within --radius NM of KDVT every --every seconds from a free public feed
(airplanes.live -> adsb.lol -> OpenSky, first one that answers), classifies each real aircraft's
traffic-pattern leg with the SAME geometry ARC uses (pattern.py), and sends one LIVE_TRAFFIC frame
to the world as role `data`. The world forwards it to the god view (overlay) and the log.

Honesty rule: live aircraft are DISPLAY + PREDICTION ONLY. ARC nodes never see them and never act
on them. (They carry no ARC radio; in the real system they would be ADS-B-only targets.)

    python data/live_traffic.py --print                 # no world needed: prints a table every 5 s
    python data/live_traffic.py --world ws://localhost:8765
    python data/live_traffic.py --world ws://localhost:8765 --record      # also saves harness/out/live_*.jsonl
    python data/live_traffic.py --world ws://localhost:8765 --replay harness/out/live_XXXX.jsonl   # offline fallback

Frame (docs/interface.md v1.2, additive):
{"type":"LIVE_TRAFFIC","t":...,"source":"airplanes.live","radius_nm":25,
 "aircraft":[{"id":"a1b2c3","callsign":"N123AB","type":"C172","lat":..,"lon":..,"alt_msl_ft":2500,"dist_nm":3.2,
              "gs_kt":92,"track_deg":86,"vs_fpm":0,"on_ground":false,"age_s":1.2,
              "leg":"DOWNWIND","runway":"07R","leg_conf":0.82,"next_leg":"BASE","in_pattern":true}]}
"""
from __future__ import annotations
import argparse, asyncio, json, math, os, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from pattern import legs, straight_in, to_enu, ELEV_FT, TPA_AGL_FT   # noqa: E402

LAT, LON = 33.688301, -112.083000
GROUND_KEEP_NM = 2.0        # keep taxiing aircraft only at KDVT itself (drops Sky Harbor's ramp)

def _altimeter_inhg() -> float:
    try:
        from data.metar import load
        return float(load().get("altimeter_inhg", 29.92))
    except Exception:
        return 29.92

def _dist_nm(lat, lon) -> float:
    return math.hypot((lat - LAT) * 60.0, (lon - LON) * 60.0 * math.cos(math.radians(LAT)))
RUNWAYS = ("25L", "25R", "07L", "07R")
NEXT = {"UPWIND": "CROSSWIND", "CROSSWIND": "DOWNWIND", "DOWNWIND": "BASE", "BASE": "FINAL",
        "FINAL": "LANDING", "STRAIGHT_IN": "LANDING"}
CORRIDOR_M = 900.0          # how far off a leg's centre line still counts as "on" it
MAX_HDG_ERR = 40.0          # degrees
PATTERN_TOP_FT = ELEV_FT + TPA_AGL_FT + 800

# ---------------- feeds ----------------
def _get(url: str, timeout=6):
    import requests
    r = requests.get(url, timeout=timeout, headers={"User-Agent": "ARC-hackathon/1.0 (ASU Devils Invent)"})
    r.raise_for_status()
    return r.json()

def _from_readsb(js: dict, src: str) -> list[dict]:
    """airplanes.live / adsb.lol v2 format (readsb JSON).
    Altitude: barometric (what pilots fly) corrected to MSL with the KDVT altimeter setting.
    GPS alt_geom is height above the WGS84 ellipsoid (~100 ft below MSL here), used only as a fallback."""
    out = []
    corr = (_altimeter_inhg() - 29.92) * 1000.0
    for a in js.get("ac", []):
        if a.get("lat") is None or a.get("lon") is None:
            continue
        alt_baro = a.get("alt_baro")
        on_ground = alt_baro == "ground"
        if isinstance(alt_baro, (int, float)):
            alt = alt_baro + corr
        elif isinstance(a.get("alt_geom"), (int, float)):
            alt = a["alt_geom"] + 100.0          # ellipsoid -> MSL (geoid ~ -31 m at Phoenix)
        else:
            alt = ELEV_FT
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
        a = dict(a)
        a["dist_nm"] = round(_dist_nm(a["lat"], a["lon"]), 1)
        if a["on_ground"] and a["dist_nm"] > GROUND_KEEP_NM:
            continue
        a.update(classify(a))
        for k in ("alt_msl_ft", "gs_kt", "track_deg", "vs_fpm", "age_s"):
            a[k] = round(a[k], 1)
        ac.append(a)
    ac.sort(key=lambda a: (not a["in_pattern"], a["on_ground"], a["dist_nm"]))
    return {"type": "LIVE_TRAFFIC", "t": time.time(), "source": source, "radius_nm": radius_nm, "aircraft": ac}

def table(f: dict) -> str:
    rows = [f"{time.strftime('%H:%M:%S')}  {len(f['aircraft'])} real aircraft within {f['radius_nm']} NM of KDVT ({f['source']}), "
            f"{sum(a['in_pattern'] for a in f['aircraft'])} in a KDVT pattern"]
    for a in f["aircraft"][:25]:
        leg = f"{a['leg']}{' ' + a['runway'] if a['runway'] else ''}" + (f" ({a['leg_conf']:.2f}) next {a['next_leg']}" if a["in_pattern"] else "")
        rows.append(f"  {a['callsign'][:8]:8s} {a['type'][:4]:4s} {a['dist_nm']:4.1f} NM {a['alt_msl_ft']:6.0f} ft {a['gs_kt']:4.0f} kt trk {a['track_deg']:3.0f}  {leg}")
    return "\n".join(rows)

# ---------------- main ----------------
FAILS_BEFORE_FALLBACK = 3      # consecutive failed polls before replaying a recording
LIVE_RETRY_S = 30.0            # while replaying, try the live feeds again this often

def newest_recording() -> str | None:
    d = os.path.join(ROOT, "harness", "out")
    try:
        files = [os.path.join(d, f) for f in os.listdir(d) if f.startswith("live_") and f.endswith(".jsonl")]
    except OSError:
        return None
    files = [f for f in files if os.path.getsize(f) > 0]
    return max(files, key=os.path.getmtime) if files else None

class WorldLink:
    """Connection to the world as role `data` that survives the world restarting (run_demo -Stop / start)."""
    def __init__(self, url: str | None):
        self.url, self.ws, self.warned = url, None, False

    async def send(self, f: dict) -> bool:
        if not self.url:
            return False
        import websockets
        for attempt in (1, 2):
            if self.ws is None:
                try:
                    self.ws = await websockets.connect(f"{self.url}?role=data", open_timeout=3)
                    print(f"[live] connected to {self.url} as data", flush=True)
                    self.warned = False
                except Exception as e:
                    if not self.warned:
                        print(f"[live] world not reachable at {self.url} ({type(e).__name__}) - will keep retrying", flush=True)
                        self.warned = True
                    return False
            try:
                await self.ws.send(json.dumps(f))
                return True
            except Exception:
                self.ws = None          # world restarted: reconnect once and resend
        return False

async def run(args):
    link = WorldLink(args.world)
    rec = None
    if args.record:
        os.makedirs(os.path.join(ROOT, "harness", "out"), exist_ok=True)
        rec = open(os.path.join(ROOT, "harness", "out", f"live_{int(time.time())}.jsonl"), "a")
    replay_path = args.replay
    replay, i = None, 0
    fails, last_live_try = 0, 0.0
    while True:
        f = None
        # 1. live feeds (unless a replay was asked for); while in automatic fallback, retry live every LIVE_RETRY_S
        if not args.replay and (replay is None or time.time() - last_live_try >= LIVE_RETRY_S):
            last_live_try = time.time()
            try:
                src, raw = await asyncio.to_thread(fetch, args.radius)
                f = frame(src, args.radius, raw)
                if replay is not None:
                    print("[live] live feed is back - stopped replaying", flush=True)
                fails, replay = 0, None
                if rec:
                    rec.write(json.dumps(f) + "\n"); rec.flush()
            except Exception as e:
                fails += 1
                print(f"[live] fetch failed ({fails}x): {e}", flush=True)
                if replay is None and fails >= FAILS_BEFORE_FALLBACK and args.fallback != "off":
                    replay_path = args.fallback if args.fallback not in (None, "auto") else newest_recording()
                    if replay_path and os.path.exists(replay_path):
                        print(f"[live] no internet feed - replaying {os.path.basename(replay_path)} (labelled 'replay' on the god view)", flush=True)
                        replay = [json.loads(l) for l in open(replay_path) if l.strip()]
                    elif fails == FAILS_BEFORE_FALLBACK:
                        print("[live] no recording to fall back to yet (run once with internet: frames are recorded to harness/out/)", flush=True)
                if replay is None:
                    # tell the god view WHY there is nothing, instead of a silent "no feed"
                    f = {"type": "LIVE_TRAFFIC", "t": time.time(), "source": f"offline - {str(e)[:80]}",
                         "radius_nm": args.radius, "aircraft": []}
        # 2. replay (asked for with --replay, or automatic fallback)
        if f is None:
            if replay is None and args.replay:
                replay = [json.loads(l) for l in open(args.replay) if l.strip()]
            if replay:
                g = replay[i % len(replay)]; i += 1
                f = dict(g, t=time.time(), source=f"replay:{g.get('source')}")
        if f is not None:
            sent = await link.send(f)
            if args.print or not args.world:
                print(table(f), flush=True)
            elif sent:
                print(f"[live] {len(f['aircraft'])} aircraft, {sum(a.get('in_pattern', False) for a in f['aircraft'])} in pattern ({f['source']})", flush=True)
        await asyncio.sleep(args.every)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", help="ws://<world-ip>:8765 (omit to just print)")
    ap.add_argument("--radius", type=float, default=12, help="NM around KDVT (12 keeps Sky Harbor's ramp out)")
    ap.add_argument("--every", type=float, default=5, help="seconds between polls (keep >= 5: free feeds)")
    ap.add_argument("--print", action="store_true", help="print the full table even when sending to the world")
    ap.add_argument("--record", action="store_true", help="append every frame to harness/out/live_<ts>.jsonl")
    ap.add_argument("--replay", help="replay a recorded live_*.jsonl instead of fetching (offline fallback)")
    ap.add_argument("--fallback", default="auto", help="when the live feeds fail 3x: 'auto' = replay the newest "
                    "harness/out/live_*.jsonl, a file path, or 'off'")
    try:
        asyncio.run(run(ap.parse_args()))
    except KeyboardInterrupt:
        pass
