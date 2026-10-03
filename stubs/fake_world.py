"""
stubs/fake_world.py — stand-in for Lane A's world server so Lanes B, C, D can start NOW.

Speaks the INTERFACE.md v1 hub protocol on ws://0.0.0.0:8765 with ?role=...:
  node:<id>    -> receives OWNSHIP at 10 Hz for its own aircraft only
  cockpit:<id> -> receives own OWNSHIP + the latest ADVISORY/TRUST from its node
  god          -> receives all truth states (TRUTH frames) at 10 Hz
  log          -> receives every ADVISORY/COMMAND/TRUST (kind=decision) and anything
                  posted by role channel/camera (kind=radio/camera) as LOG frames
  channel      -> receives TRUTH at 10 Hz (so radio/channel.py can decide delivery)
  camera, data -> accepted; their frames are mirrored to log

Physics: straight-line point mass, 20 Hz, constant speed; COMMAND TAKEOVER applies
bank (turn rate = g*tan(bank)/V) and vs if ap_equipped and no stick. INPUT roll
sets bank directly (±30°) and marks stick_active (sends STICK during a takeover).

Default scenario (override with --scenario harness/scenarios/*.json for ids only):
  N101  downwind 25L at 2,500 ft MSL, 90 kt, track 070, ap_equipped, human
  N204  on base heading 160 (will cut in front of N101 ~70 s in), 95 kt, AI
Replace with the real world/world_server.py when Lane A passes the 1 PM test.

Run:  python stubs/fake_world.py [--port 8765] [--speed 1.0]
"""
from __future__ import annotations
import argparse, asyncio, json, math, time
from dataclasses import dataclass, field
from urllib.parse import urlparse, parse_qs
import websockets

G = 9.80665
KT = 0.514444           # m/s per knot
FT = 0.3048
KDVT_LAT, KDVT_LON, KDVT_ELEV_FT = 33.6883, -112.0826, 1478.0

def dest(lat, lon, brg_deg, dist_m):
    """Move dist_m along bearing on a flat-earth approximation (fine for 15 mi)."""
    dlat = dist_m * math.cos(math.radians(brg_deg)) / 111_320.0
    dlon = dist_m * math.sin(math.radians(brg_deg)) / (111_320.0 * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon

@dataclass
class Aircraft:
    id: str
    lat: float
    lon: float
    alt_msl_ft: float
    ias_kt: float
    hdg_deg: float
    ap_equipped: bool = False
    human: bool = False
    bank_deg: float = 0.0
    vs_fpm: float = 0.0
    stick_active: bool = False
    cmd: dict | None = None          # active TAKEOVER command
    cmd_until: float = 0.0
    last_input_t: float = 0.0

    def step(self, dt: float, now: float):
        # takeover expiry
        if self.cmd and now > self.cmd_until:
            self.cmd = None
        if self.cmd and self.ap_equipped and not self.stick_active:
            target_bank = max(-30.0, min(30.0, self.cmd.get("bank_cmd_deg", 0.0)))
            self.vs_fpm = self.cmd.get("vs_cmd_fpm", 0.0)
        elif self.stick_active:
            target_bank = self.bank_deg          # set by INPUT
        else:
            target_bank = 0.0
            self.vs_fpm = 0.0 if not self.cmd else self.vs_fpm
        # bank rate limit 15 deg/s
        db = max(-15 * dt, min(15 * dt, target_bank - self.bank_deg))
        self.bank_deg += db
        v = self.ias_kt * KT
        turn_rate = math.degrees(G * math.tan(math.radians(self.bank_deg)) / v) if v > 1 else 0.0
        self.hdg_deg = (self.hdg_deg + turn_rate * dt) % 360
        self.lat, self.lon = dest(self.lat, self.lon, self.hdg_deg, v * dt)
        self.alt_msl_ft += self.vs_fpm * dt / 60.0
        if self.stick_active and now - self.last_input_t > 1.0:
            self.stick_active = False

    def ownship(self, now: float) -> dict:
        return {"type": "OWNSHIP", "ac_id": self.id, "t": now,
                "lat": round(self.lat, 6), "lon": round(self.lon, 6),
                "alt_msl_ft": round(self.alt_msl_ft, 1),
                "alt_press_ft": round(self.alt_msl_ft - 10, 1),
                "agl_ft": round(self.alt_msl_ft - KDVT_ELEV_FT, 1),
                "gs_kt": self.ias_kt, "track_deg": round(self.hdg_deg, 1),
                "hdg_deg": round(self.hdg_deg, 1), "bank_deg": round(self.bank_deg, 1),
                "vs_fpm": self.vs_fpm, "ias_kt": self.ias_kt,
                "ap_equipped": self.ap_equipped, "stick_active": self.stick_active, "flaps": 0}

def default_fleet() -> dict[str, Aircraft]:
    # N101 abeam the numbers on left downwind for 25L (east-bound, track 070), 1,000 AGL
    lat1, lon1 = dest(KDVT_LAT, KDVT_LON, 350, 1_600)      # ~1 mi north of the field
    # N204 on a base leg from the NE, heading 160, converging on the downwind-to-base corner
    lat2, lon2 = dest(lat1, lon1, 40, 2_900)
    return {
        "N101": Aircraft("N101", lat1, lon1, 2_500.0, 90.0, 70.0, ap_equipped=True, human=True),
        "N204": Aircraft("N204", lat2, lon2, 2_450.0, 95.0, 160.0, ap_equipped=False),
    }

class Hub:
    def __init__(self, fleet: dict[str, Aircraft], speed: float):
        self.fleet = fleet
        self.speed = speed
        self.clients: dict[websockets.ServerConnection, str] = {}
        self.latest_adv: dict[str, dict] = {}
        self.latest_trust: dict[str, dict] = {}
        self.t0 = time.time()

    def now(self) -> float:
        return self.t0 + (time.time() - self.t0) * self.speed

    async def send(self, ws, obj):
        try:
            await ws.send(json.dumps(obj))
        except Exception:
            pass

    async def to_role(self, role_prefix: str, obj, exact: str | None = None):
        for ws, role in list(self.clients.items()):
            if (exact and role == exact) or (not exact and role.startswith(role_prefix)):
                await self.send(ws, obj)

    async def log(self, src: str, kind: str, payload: dict):
        await self.to_role("log", {"type": "LOG", "src": src, "kind": kind, "t": self.now(), "payload": payload})

    async def handler(self, ws):
        q = parse_qs(urlparse(ws.request.path).query)
        role = q.get("role", ["god"])[0]
        self.clients[ws] = role
        print(f"[world] + {role}")
        try:
            async for raw in ws:
                try:
                    m = json.loads(raw)
                except Exception:
                    continue
                await self.on_message(role, m)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.clients.pop(ws, None)
            print(f"[world] - {role}")

    async def on_message(self, role: str, m: dict):
        t = m.get("type")
        if t == "ADVISORY":
            self.latest_adv[m["ac_id"]] = m
            await self.to_role("", m, exact=f"cockpit:{m['ac_id']}")
            await self.to_role("god", m)
            await self.log(m["ac_id"], "decision", m)
            print(f"[world] ADVISORY {m['ac_id']} {m.get('level')} '{m.get('text')}' ttc={m.get('ttc_s')}")
        elif t == "TRUST":
            self.latest_trust[m["ac_id"]] = m
            await self.to_role("", m, exact=f"cockpit:{m['ac_id']}")
            await self.log(m["ac_id"], "decision", m)
        elif t == "COMMAND":
            ac = self.fleet.get(m["ac_id"])
            applied = False
            if ac and m.get("mode") == "TAKEOVER" and ac.ap_equipped and not ac.stick_active:
                ac.cmd = m
                ac.cmd_until = self.now() + min(float(m.get("hold_s", 10)), 10.0)
                applied = True
            elif ac and m.get("mode") == "RELEASE":
                ac.cmd = None
                applied = True
            m2 = dict(m, applied=applied)
            await self.to_role("god", m2)
            await self.log(m["ac_id"], "decision", m2)
            print(f"[world] COMMAND {m['ac_id']} {m.get('mode')} bank={m.get('bank_cmd_deg')} applied={applied}")
        elif t == "INPUT":
            ac = self.fleet.get(m["ac_id"])
            if ac:
                ac.bank_deg = max(-30.0, min(30.0, float(m.get("roll", 0)) * 30.0))
                ac.vs_fpm = float(m.get("pitch", 0)) * 500.0
                ac.last_input_t = self.now()
                if not ac.stick_active and ac.cmd:
                    ac.stick_active = True
                    stick = {"type": "STICK", "ac_id": ac.id, "t": self.now()}
                    await self.to_role("", stick, exact=f"node:{ac.id}")
                    await self.log(ac.id, "world", stick)
                    print(f"[world] STICK {ac.id} — pilot took it back")
                ac.stick_active = True
        elif t == "PREDICTION":
            await self.to_role("god", m)
        elif role in ("channel", "camera", "data"):
            await self.log(m.get("from", m.get("src", role)), "radio" if role == "channel" else role, m)
        elif t == "SET_DA":
            await self.log("god", "world", m)

    async def loop(self):
        dt = 0.05
        tick = 0
        while True:
            now = self.now()
            for ac in self.fleet.values():
                ac.step(dt * self.speed, now)
            tick += 1
            if tick % 2 == 0:                       # 10 Hz
                truth = {"type": "TRUTH", "t": now, "aircraft": [ac.ownship(now) for ac in self.fleet.values()]}
                await self.to_role("god", truth)
                await self.to_role("channel", truth)
                for ac in self.fleet.values():
                    own = ac.ownship(now)
                    await self.to_role("", own, exact=f"node:{ac.id}")
                    await self.to_role("", own, exact=f"cockpit:{ac.id}")
            await asyncio.sleep(dt)

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--speed", type=float, default=1.0, help="time scale")
    ap.add_argument("--scenario", help="use aircraft ids from a scenario file (positions stay default)")
    a = ap.parse_args()
    fleet = default_fleet()
    if a.scenario:
        ids = [s["id"] for s in json.load(open(a.scenario))["aircraft"]]
        base = list(fleet.values())
        fleet = {}
        for i, ac_id in enumerate(ids):
            src = base[i % len(base)]
            lat, lon = dest(src.lat, src.lon, 90, 400 * (i // len(base)))
            fleet[ac_id] = Aircraft(ac_id, lat, lon, src.alt_msl_ft, src.ias_kt, src.hdg_deg, src.ap_equipped, src.human)
    hub = Hub(fleet, a.speed)
    async with websockets.serve(hub.handler, "0.0.0.0", a.port):
        print(f"[world] fake world on ws://0.0.0.0:{a.port}  aircraft={list(fleet)}  (Ctrl-C to stop)")
        await hub.loop()

if __name__ == "__main__":
    asyncio.run(main())
