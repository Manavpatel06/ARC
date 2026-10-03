"""
stubs/fake_world.py — stand-in for Lane A's world server so Lanes B, C, D can start NOW.
Reference implementation of the INTERFACE.md v1.1 hub protocol on ws://0.0.0.0:8765 with ?role=...:
  node:<id>          -> HELLO, then OWNSHIP at 10 Hz for its own aircraft only
  cockpit:<id>|A|B   -> HELLO (tells the page which aircraft it flies), own OWNSHIP + its node's ADVISORY/TRUST
  god                -> HELLO, TRUTH 10 Hz, ADVISORY/COMMAND/PREDICTION/CRYSTAL
  log                -> LOG frames for every ADVISORY/COMMAND/TRUST (decision) and channel traffic (radio)
  channel            -> TRUTH at 10 Hz (so radio/channel.py can decide delivery)
  data               -> accepted; frames mirrored to log

What this stub does NOT do (Lane A's real world does): fly the pattern legs, turn at corners,
land/touch-and-go. AI aircraft here hold their spawn heading. Spawn geometry is REAL (pattern.py).

Physics (simple but matches the v1.1 INPUT meaning):
  * TAKEOVER COMMAND: bank -> clipped ±30°, vs -> commanded, applied only if ap_equipped and no stick.
  * INPUT: roll -> target bank roll*45° (stick centred = wings level), pitch -> target vs
    (up: climb capability at density altitude; down: 1,000 fpm), throttle -> target IAS 60-120 kt.
    |roll|/|pitch| > 0.1 sets stick_active; clears after 1 s without input; first such INPUT during a
    takeover sends STICK to the node.
  * bank rate ≤ 15°/s, turn rate = g·tan(bank)/V, ground velocity = air velocity + METAR wind,
    agl = msl − terrain elevation.

Default fleet (no --scenario): a realistic midfield 45° entry conflict on 25L left downwind
  N101  left downwind 25L (086°T, south of field, TPA 2,500 MSL), 90 kt, ap_equipped, human  -> cockpit:A
  N204  45° entry heading 041°T, 95 kt, AI — reaches the same downwind point ~55 s in
With --scenario harness/scenarios/judges.json every aircraft spawns at pattern.place(start).

Run:  python stubs/fake_world.py [--port 8765] [--speed 1.0] [--scenario harness/scenarios/judges.json]
"""
from __future__ import annotations
import argparse, asyncio, json, math, os, sys, time
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs
import websockets

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from pattern import place, to_enu, from_enu          # noqa: E402
from data.metar import load as load_metar, climb_fpm, wind_vector_ms   # noqa: E402
from data.terrain import elev_at_ft                  # noqa: E402

G = 9.80665
KT = 0.514444

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
    flock: bool = True
    bank_deg: float = 0.0
    vs_fpm: float = 0.0
    tgt_bank: float = 0.0
    tgt_vs: float = 0.0
    tgt_ias: float = 0.0
    stick_active: bool = False
    cmd: dict | None = None
    cmd_until: float = 0.0
    last_input_t: float = 0.0

    def __post_init__(self):
        self.tgt_ias = self.ias_kt

    def step(self, dt: float, now: float, wind_en: tuple[float, float]):
        if self.cmd and now > self.cmd_until:
            self.cmd = None
        if self.stick_active and now - self.last_input_t > 1.0:
            self.stick_active = False
            self.tgt_bank, self.tgt_vs = 0.0, 0.0
        if self.cmd and self.ap_equipped and not self.stick_active:
            tb = max(-30.0, min(30.0, float(self.cmd.get("bank_cmd_deg", 0.0))))
            self.vs_fpm = float(self.cmd.get("vs_cmd_fpm", 0.0))
        else:
            tb = self.tgt_bank
            self.vs_fpm = self.tgt_vs
        self.bank_deg += max(-15 * dt, min(15 * dt, tb - self.bank_deg))
        self.ias_kt += max(-2 * dt, min(2 * dt, self.tgt_ias - self.ias_kt))
        v = self.ias_kt * KT
        self.hdg_deg = (self.hdg_deg + math.degrees(G * math.tan(math.radians(self.bank_deg)) / v) * dt) % 360
        ve = v * math.sin(math.radians(self.hdg_deg)) + wind_en[0]
        vn = v * math.cos(math.radians(self.hdg_deg)) + wind_en[1]
        x, y = to_enu(self.lat, self.lon)
        self.lat, self.lon = from_enu(x + ve * dt, y + vn * dt)
        self.alt_msl_ft += self.vs_fpm * dt / 60.0
        self._gs_kt = math.hypot(ve, vn) / KT
        self._trk = math.degrees(math.atan2(ve, vn)) % 360

    def ownship(self, now: float) -> dict:
        return {"type": "OWNSHIP", "ac_id": self.id, "t": now,
                "lat": round(self.lat, 6), "lon": round(self.lon, 6),
                "alt_msl_ft": round(self.alt_msl_ft, 1),
                "alt_press_ft": round(self.alt_msl_ft - 10, 1),
                "agl_ft": round(self.alt_msl_ft - elev_at_ft(self.lat, self.lon), 1),
                "gs_kt": round(getattr(self, "_gs_kt", self.ias_kt), 1),
                "track_deg": round(getattr(self, "_trk", self.hdg_deg), 1),
                "hdg_deg": round(self.hdg_deg, 1), "bank_deg": round(self.bank_deg, 1),
                "vs_fpm": round(self.vs_fpm), "ias_kt": round(self.ias_kt, 1),
                "ap_equipped": self.ap_equipped, "stick_active": self.stick_active, "flaps": 0}

def spawn(spec: dict) -> Aircraft:
    s = spec.get("start", {})
    gs = 95.0 if spec.get("human") is False and not spec.get("ap") else 90.0
    p = place(s.get("leg", "DOWNWIND"), s.get("runway", "25L"), s.get("offset_s", 0), gs, s.get("agl_ft"))
    return Aircraft(spec["id"], p["lat"], p["lon"], p["alt_msl_ft"], gs, p["hdg_deg"],
                    ap_equipped=spec.get("ap", False), human=spec.get("human", False), flock=spec.get("flock", True))

def default_fleet() -> dict[str, Aircraft]:
    n101 = place("DOWNWIND", "25L", offset_s=0, gs_kt=90)
    # conflict point 2,530 m further along downwind; N204 flies a 45° entry (041°T) into it
    x0, y0 = n101["x_m"], n101["y_m"]
    t_conf = 2530 / (90 * KT)
    cx, cy = x0 + 2530 * math.sin(math.radians(86)), y0 + 2530 * math.cos(math.radians(86))
    back = 95 * KT * t_conf
    sx, sy = cx - back * math.sin(math.radians(41)), cy - back * math.cos(math.radians(41))
    lat2, lon2 = from_enu(sx, sy)
    return {
        "N101": Aircraft("N101", n101["lat"], n101["lon"], n101["alt_msl_ft"], 90.0, n101["hdg_deg"], ap_equipped=True, human=True),
        "N204": Aircraft("N204", lat2, lon2, n101["alt_msl_ft"] - 30, 95.0, 41.0),
    }

class Hub:
    def __init__(self, fleet: dict[str, Aircraft], speed: float, scenario: str):
        self.fleet, self.speed, self.scenario = fleet, speed, scenario
        self.clients: dict = {}
        self.t0 = time.time()
        self.wx = load_metar()
        self.da_ft = self.wx["density_altitude_ft"]
        self.wind = wind_vector_ms(self.wx)
        humans = [a.id for a in fleet.values() if a.human]
        self.cockpit_map = {"A": humans[0] if humans else None, "B": humans[1] if len(humans) > 1 else None}

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

    def resolve(self, role: str) -> tuple[str, str | None]:
        if role.startswith("cockpit:"):
            key = role.split(":", 1)[1]
            ac = self.cockpit_map.get(key, key)
            return f"cockpit:{ac}", ac
        if role.startswith("node:"):
            return role, role.split(":", 1)[1]
        return role, None

    async def handler(self, ws):
        q = parse_qs(urlparse(ws.request.path).query)
        role, ac_id = self.resolve(q.get("role", ["god"])[0])
        self.clients[ws] = role
        hello = {"type": "HELLO", "role": role, "ac_id": ac_id, "scenario": self.scenario,
                 "aircraft": [{"id": a.id, "human": a.human, "ap": a.ap_equipped, "flock": a.flock} for a in self.fleet.values()]}
        if role in ("god", "log") or role.startswith("cockpit:"):
            hello["static"] = {"metar": self.wx, "da_field_ft": self.da_ft, "scenario": self.scenario}
        if role == "log":     # same as world/world_server.py: log consumers only get LOG frames
            hello = {"type": "LOG", "src": "world", "kind": "world", "t": self.now(), "payload": hello}
        await self.send(ws, hello)
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
            await self.to_role("", m, exact=f"cockpit:{m['ac_id']}")
            await self.to_role("god", m)
            await self.log(m["ac_id"], "decision", m)
            print(f"[world] ADVISORY {m['ac_id']} {m.get('level')} '{m.get('text')}' ttc={m.get('ttc_s')}")
        elif t == "TRUST":
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
            ac = self.fleet.get(m.get("ac_id"))
            if not ac:
                return
            roll, pitch, thr = float(m.get("roll", 0)), float(m.get("pitch", 0)), float(m.get("throttle", 0.5))
            ac.tgt_bank = roll * 45.0
            ac.tgt_vs = pitch * (climb_fpm(self.da_ft) if pitch > 0 else 1000.0)
            ac.tgt_ias = 60.0 + 60.0 * max(0.0, min(1.0, thr))
            if abs(roll) > 0.1 or abs(pitch) > 0.1:
                ac.last_input_t = self.now()
                if not ac.stick_active and ac.cmd:
                    stick = {"type": "STICK", "ac_id": ac.id, "t": self.now()}
                    await self.to_role("", stick, exact=f"node:{ac.id}")
                    await self.log(ac.id, "world", stick)
                    print(f"[world] STICK {ac.id} — pilot took it back")
                ac.stick_active = True
        elif t in ("PREDICTION", "CRYSTAL"):
            await self.to_role("god", m)
        elif t == "SET_DA":
            self.da_ft = float(m.get("ft", self.da_ft))
            await self.log("god", "world", m)
            print(f"[world] density altitude -> {self.da_ft:.0f} ft (climb {climb_fpm(self.da_ft):.0f} fpm)")
        elif role in ("channel", "camera", "data"):
            await self.log(m.get("from", m.get("src", role)), "radio" if role == "channel" else role, m)

    async def loop(self):
        dt, tick = 0.05, 0
        while True:
            now = self.now()
            for ac in self.fleet.values():
                ac.step(dt * self.speed, now, self.wind)
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
    ap.add_argument("--scenario", help="harness/scenarios/*.json — spawns every aircraft at pattern.place(start)")
    a = ap.parse_args()
    if a.scenario:
        sc = json.load(open(a.scenario))
        fleet = {s["id"]: spawn(s) for s in sc["aircraft"]}
        name = sc.get("name", os.path.basename(a.scenario))
    else:
        fleet, name = default_fleet(), "stub_45_entry"
    hub = Hub(fleet, a.speed, name)
    async with websockets.serve(hub.handler, "0.0.0.0", a.port):
        print(f"[world] fake world on ws://0.0.0.0:{a.port}  scenario={name}  aircraft={list(fleet)}  "
              f"cockpit A={hub.cockpit_map['A']} B={hub.cockpit_map['B']}  DA={hub.da_ft} ft  wind {hub.wx['wind_dir_deg']}/{hub.wx['wind_kt']}")
        await hub.loop()

if __name__ == "__main__":
    asyncio.run(main())
