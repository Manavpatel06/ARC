"""
world/world_server.py — Lane A. Authoritative simulation world + WebSocket hub (INTERFACE.md §1).

  python world/world_server.py --scenario harness/scenarios/judges.json [--port 8765]
         [--http 8080] [--time-scale 1] [--da 4980] [--weather metar|calm_morning|hot_gusty_afternoon|haboob|low_ceiling|pressure_drop]

Clients connect to ws://<host>:8765/?role=<role>:
  node:<id>        OWNSHIP at 10 Hz for its own aircraft only; STICK during a takeover
  cockpit:<id>     own OWNSHIP at 20 Hz (+ mode / active command / AP state) and the ADVISORY / TRUST /
  cockpitA|B       COMMAND / STICK of its own node. Nothing about other aircraft.
                   A = first human aircraft in the scenario, B = second.
                   Cockpit -> world: INPUT, and (Lane A, proposed for v1.2) the AP button
                   {"type":"AP","ac_id","engage":true|false|null(toggle)} -> reply AP_STATUS.
  god              TRUTH (all aircraft) at 10 Hz, every ADVISORY/TRUST/COMMAND/PREDICTION/CRYSTAL,
                   WX (weather state) on change + WX_FIELD (thermal positions) every 2 s.
                   God -> world: SET_DA, and (Lane A) SET_WX {"preset"?, field: value...} /
                   {"type":"SET_WX","update_altimeters":true} (everyone dials the current QNH).
  log              LOG frames: decisions, radio (from channel), camera, world events
                   (incl. WORLD_EVENT NMAC / COLLISION / NMAC_END from world/separation.py)
  channel          TRUTH at 10 Hz (radio emulator decides delivery from it)
  camera, data     accepted; frames mirrored to log
Every client gets a HELLO frame on connect ({"type":"HELLO","role","ac_id",...}); god, log and
cockpits also get the static picture (airport, pattern legs, METAR) — charts, not traffic.

Also serves web/ over HTTP on --http (ES modules do not load from file://):
  http://<host>:8080/index.html?role=cockpitA | cockpitB | god
"""
from __future__ import annotations
import argparse, asyncio, functools, http.server, json, os, sys, threading, time
from collections import defaultdict
from urllib.parse import parse_qs, urlparse

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pydantic import ValidationError
from websockets.asyncio.server import broadcast, serve
from websockets.exceptions import ConnectionClosed

import schemas
from world.scenario import World
from world.flight_model import HARD_LANDING_FPM
from world.separation import SeparationMonitor
from world.taws import Taws

PHYS_HZ = 20
VALIDATE = {"ADVISORY": schemas.Advisory, "COMMAND": schemas.Command, "TRUST": schemas.Trust,
            "INPUT": schemas.Input}

def dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))

class Hub:
    def __init__(self, world: World):
        self.w = world
        self.roles: dict[str, set] = defaultdict(set)      # role -> websockets
        self.latest: dict[str, dict] = {}                  # "<TYPE>:<ac_id>" -> last frame (replayed to new cockpits)
        self.t0 = time.time()
        self.sim_s = 0.0
        self.stats = {"ticks": 0, "busy_s": 0.0}
        self.sep = SeparationMonitor()                     # truth NMAC / collision events
        self.taws = Taws(world.airport)                    # GPWS-style terrain alerts for judge aircraft
        self.taws_state: dict[str, str | None] = {}

    # ---------- clock / io ----------
    def now(self) -> float:
        return self.t0 + self.sim_s

    def send(self, role: str, obj) -> None:
        conns = self.roles.get(role)
        if conns:
            broadcast(conns, obj if isinstance(obj, str) else dumps(obj))

    def log(self, src: str, kind: str, payload: dict) -> None:
        self.send("log", {"type": "LOG", "src": src, "kind": kind, "t": round(self.now(), 3), "payload": payload})

    # ---------- connections ----------
    def resolve_role(self, raw: str) -> tuple[str | None, str | None]:
        """Return (role, ac_id) or (None, error)."""
        if raw.startswith("cockpit"):
            ac_id = self.w.cockpit_id(raw[len("cockpit"):].lstrip(":"))
            return (f"cockpit:{ac_id}", ac_id) if ac_id else (None, f"no aircraft for {raw}; humans={self.w.humans}")
        if raw.startswith("node:"):
            ac_id = raw[5:]
            return (raw, ac_id) if ac_id in self.w.fleet else (None, f"unknown aircraft {ac_id}; fleet={list(self.w.fleet)}")
        if raw in ("god", "log", "channel", "camera", "data"):
            return raw, None
        return None, f"unknown role {raw}"

    async def handler(self, ws):
        raw = parse_qs(urlparse(ws.request.path).query).get("role", ["god"])[0]
        role, ac_id = self.resolve_role(raw)
        if role is None:
            await ws.send(dumps({"type": "ERROR", "error": ac_id}))
            await ws.close(code=4000, reason=ac_id[:120])
            print(f"[world] rejected role={raw}: {ac_id}")
            return
        hello = {"type": "HELLO", "role": role, "ac_id": ac_id, "scenario": self.w.scenario.name,
                 "aircraft": self.w.roster(), "t": round(self.now(), 3)}
        if role in ("god", "log") or role.startswith("cockpit:"):
            hello["static"] = self.w.static()
        if role == "log":   # log consumers expect only LOG frames (stubs/tail_log formats `kind`)
            hello = {"type": "LOG", "src": "world", "kind": "world", "t": hello["t"], "payload": hello}
        await ws.send(dumps(hello))
        if role.startswith("cockpit:"):
            for k in ("ADVISORY", "TRUST"):
                if f"{k}:{ac_id}" in self.latest:
                    await ws.send(dumps(self.latest[f"{k}:{ac_id}"]))
        self.roles[role].add(ws)
        print(f"[world] + {role} ({sum(len(s) for s in self.roles.values())} clients)")
        try:
            async for msg in ws:
                try:
                    m = json.loads(msg)
                except ValueError:
                    continue
                if isinstance(m, dict):
                    self.on_message(role, ac_id, m)
        except ConnectionClosed:
            pass
        finally:
            self.roles[role].discard(ws)
            print(f"[world] - {role}")

    # ---------- message routing ----------
    def check(self, role: str, m: dict) -> None:
        model = VALIDATE.get(m.get("type"))
        if model:
            try:
                model.model_validate(m)
            except ValidationError as e:
                err = {"type": "SCHEMA_ERROR", "from_role": role, "msg_type": m.get("type"),
                       "error": str(e).splitlines()[0:3]}
                print(f"[world] schema warning from {role}: {err['error']}")
                self.log(role, "world", err)

    def on_message(self, role: str, own_id: str | None, m: dict) -> None:
        t = m.get("type")
        self.check(role, m)

        if role.startswith("node:"):
            ac_id = m.get("ac_id", own_id)
            if ac_id != own_id:
                print(f"[world] {role} sent {t} for {ac_id}; using its own id {own_id}")
                ac_id = own_id
            cockpit = f"cockpit:{ac_id}"
            if t in ("ADVISORY", "TRUST"):
                self.latest[f"{t}:{ac_id}"] = m
                self.send(cockpit, m)
                self.send("god", m)
                self.log(ac_id, "decision", m)
                if t == "ADVISORY":
                    print(f"[world] ADVISORY {ac_id} {m.get('level')} '{m.get('text')}' ttc={m.get('ttc_s')}")
            elif t == "COMMAND":
                ac = self.w.fleet[ac_id]
                applied = ac.apply_command(m, self.now())
                why = None if applied else ("not ap_equipped" if not ac.ap_equipped else "stick active")
                m2 = dict(m, applied=applied, **({"rejected_by_world": why} if why else {}))
                self.send(cockpit, m2)
                self.send("god", m2)
                self.log(ac_id, "decision", m2)
                print(f"[world] COMMAND {ac_id} {m.get('mode')} bank={m.get('bank_cmd_deg')} applied={applied}" + (f" ({why})" if why else ""))
            elif t == "PREDICTION":
                self.send("god", m)
            elif t == "CRYSTAL":
                self.send("god", m)
            else:
                self.log(ac_id, "world", m)
            return

        if role.startswith("cockpit:"):
            if t == "INPUT":
                ac = self.w.fleet[own_id]
                moving = ac.apply_input(float(m.get("roll", 0)), float(m.get("pitch", 0)), float(m.get("throttle", 0.5)), self.now())
                if moving and ac.cmd is not None and ac.cmd.get("mode") == "TAKEOVER":
                    ac.cmd = None                     # pilot always wins; node must RELEASE within one tick
                    stick = {"type": "STICK", "ac_id": own_id, "t": round(self.now(), 3)}
                    self.send(f"node:{own_id}", stick)
                    self.send(f"cockpit:{own_id}", stick)
                    self.send("god", stick)
                    self.log(own_id, "world", stick)
                    print(f"[world] STICK {own_id} - pilot took it back")
            elif t == "AP":
                ac = self.w.fleet[own_id]
                want = m.get("engage")
                want = (not ac.ap_engaged) if want is None else bool(want)
                ok, why = ac.engage_ap(want)
                st = {"type": "AP_STATUS", "ac_id": own_id, "t": round(self.now(), 3), "ok": ok,
                      "engaged": ac.ap_engaged, "phase": ac.ap_phase, "reason": why}
                self.send(f"cockpit:{own_id}", st)
                self.send("god", st)
                self.log(own_id, "world", st)
                print(f"[world] AP {own_id} {'ON' if ac.ap_engaged else 'OFF'} ({why})")
            return

        if role == "god":
            if t == "SET_DA":
                self.w.env.da_field_ft = float(m.get("ft", self.w.env.da_field_ft))
                ev = {"type": "ENV", "da_field_ft": self.w.env.da_field_ft}
                self.send("god", ev)
                self.log("god", "world", m)
                self.publish_wx()
                print(f"[world] density altitude at field -> {self.w.env.da_field_ft:.0f} ft")
            elif t == "SET_WX":
                what = []
                if m.get("preset"):
                    try:
                        self.w.set_preset(str(m["preset"]))
                        what.append(f"preset {m['preset']}")
                    except KeyError as e:
                        print(f"[world] SET_WX: {e}")
                fields = {k: v for k, v in m.items() if k not in ("type", "preset", "update_altimeters")}
                if fields:
                    what += self.w.set_weather(**fields)
                if m.get("update_altimeters"):
                    n = self.w.update_altimeters()
                    what.append(f"altimeters updated on {n} aircraft")
                self.log("god", "world", m)
                self.publish_wx()
                print(f"[world] weather: {', '.join(what) or 'no change'} -> {self.w.env.wx.metar_style()}")
            return

        if role in ("channel", "camera", "data"):
            kind = "radio" if role == "channel" else "camera" if role == "camera" else "world"
            if t == "CRYSTAL":
                self.send("god", m)
            self.log(m.get("from", m.get("src", role)), kind, m)

    def publish_wx(self) -> None:
        """Weather to god (full) and cockpits (what a pilot knows: METAR/ATIS, visibility, cloud)."""
        st = self.w.wx_state()
        self.send("god", {"type": "WX", **st})
        pilot = {k: st[k] for k in ("name", "metar_style", "wind_from_deg", "wind_kt", "gust_kt", "visibility_sm",
                                    "ceiling_ft_agl", "qnh_inhg", "turbulence_name")}
        for ac_id in self.w.fleet:
            self.send(f"cockpit:{ac_id}", {"type": "WX", **pilot})

    # ---------- physics loop ----------
    def cockpit_frame(self, ac, now: float) -> str:
        d = ac.ownship(now)
        d["mode"] = ac.mode
        d["alt_ind_ft"] = round(ac.indicated_ft, 1)
        d["baro_set_inhg"] = round(ac.baro_set_inhg, 2)
        d["turb"] = round(ac.turb_now, 2)
        d["taws"] = getattr(ac, "taws_alert", None)
        d["ap"] = ac.ap_engaged
        d["ap_phase"] = ac.ap_phase
        d["on_ground"] = ac.on_ground
        if ac.cmd is not None:
            d["cmd"] = {"bank_cmd_deg": ac.cmd.get("bank_cmd_deg"), "vs_cmd_fpm": ac.cmd.get("vs_cmd_fpm"),
                        "bounds": ac.cmd.get("bounds"), "left_s": round(ac.cmd_until - now, 1)}
        return dumps(d)

    def publish_aircraft_events(self, now: float) -> None:
        """TOUCHDOWN / LIFTOFF / AP_DISCONNECT from the flight model: log always; own cockpit;
        god for judge aircraft and hard landings (AI touch-and-goes would flood the ticker)."""
        for ac in self.w.fleet.values():
            while ac.events:
                kind, val = ac.events.pop(0)
                ev = {"type": "WORLD_EVENT", "event": kind, "a": ac.id, "t": round(now, 3),
                      "lat": round(ac.lat, 6), "lon": round(ac.lon, 6), "alt_msl_ft": round(ac.alt_msl_ft)}
                if kind == "TOUCHDOWN":
                    surface = self.taws.touchdown(ac, val)
                    ev.update(vs_fpm=val, hard=val < -HARD_LANDING_FPM or surface == "TERRAIN_IMPACT",
                              surface=surface, ias_kt=round(ac.ias_kt))
                elif kind == "LIFTOFF":
                    ev.update(ias_kt=val)
                else:
                    ev.update(reason=val)
                self.log(ac.id, "world", ev)
                self.send(f"cockpit:{ac.id}", ev)
                if ac.human or ev.get("hard"):
                    self.send("god", ev)
                if ac.human or ev.get("hard") or kind == "AP_DISCONNECT":
                    print(f"[world] {kind} {ac.id} {val}" + (" HARD" if ev.get("hard") else ""))

    def check_taws(self) -> None:
        """Own-aircraft terrain alerts for judge aircraft; publish on change."""
        for ac in self.w.fleet.values():
            if not ac.human:
                continue
            al = self.taws.alert(ac, 0.1)
            ac.taws_alert = al
            if al != self.taws_state.get(ac.id):
                self.taws_state[ac.id] = al
                ev = {"type": "WORLD_EVENT", "event": "TAWS", "a": ac.id, "t": round(self.now(), 3), "alert": al,
                      "agl_ft": round(ac.agl_ft), "vs_fpm": round(ac.vsi_fpm)}
                self.send(f"cockpit:{ac.id}", ev)
                self.send("god", ev)
                self.log(ac.id, "world", ev)
                if al:
                    print(f"[world] TAWS {ac.id} {al} ({ac.agl_ft:.0f} ft AGL, {ac.vsi_fpm:.0f} fpm)")

    async def loop(self):
        period = 1.0 / PHYS_HZ
        next_t = time.perf_counter()
        last_report = time.perf_counter()
        while True:
            t_start = time.perf_counter()
            # physics: one real tick = time_scale sim seconds, in 50 ms substeps
            sim_dt = period * self.w.time_scale
            n = max(1, round(self.w.time_scale))
            for _ in range(n):
                self.sim_s += sim_dt / n
                now = self.now()
                for ac in self.w.fleet.values():
                    ac.step(sim_dt / n, self.w.env, now)
            now = self.now()
            tick = self.stats["ticks"]
            for ac in self.w.fleet.values():                    # 20 Hz to cockpits
                if self.roles.get(f"cockpit:{ac.id}"):
                    self.send(f"cockpit:{ac.id}", self.cockpit_frame(ac, now))
            if tick % 2 == 0:                                   # 10 Hz
                for ac in self.w.fleet.values():
                    if self.roles.get(f"node:{ac.id}"):
                        self.send(f"node:{ac.id}", ac.ownship(now))
                if self.roles.get("god") or self.roles.get("channel"):
                    truth = dumps({"type": "TRUTH", "t": round(now, 3),
                                   "aircraft": [ac.truth(now) for ac in self.w.fleet.values()]})
                    self.send("god", truth)
                    self.send("channel", truth)
                # ground-truth separation: god + log only (never nodes; they must not see truth)
                self.check_taws()
                self.publish_aircraft_events(now)
                if tick % 40 == 0 and self.roles.get("god"):          # every 2 s: thermal field for the map
                    self.send("god", {"type": "WX_FIELD", "t": round(now, 3), "thermals":
                                      [[round(x), round(y), round(r), round(pk)] for x, y, r, pk in self.w.env.wx.thermal_list(now)]})
                for ev in self.sep.check(self.w.fleet, now):
                    self.send("god", ev)
                    self.log("world", "world", ev)
                    if ev["event"] != "NMAC_END":
                        print(f"[world] {ev['event']} {ev['a']}-{ev['b']} {ev['h_ft']} ft / {ev['v_ft']} ft ({ev['legs'][0]}/{ev['legs'][1]})")
                    else:
                        print(f"[world] NMAC_END {ev['a']}-{ev['b']} min {ev['min_h_ft']} ft / {ev['min_v_ft']} ft"
                              + (" COLLIDED" if ev["collided"] else ""))
            self.stats["ticks"] += 1
            self.stats["busy_s"] += time.perf_counter() - t_start
            if time.perf_counter() - last_report > 30:
                busy = 100 * self.stats["busy_s"] / (time.perf_counter() - last_report)
                print(f"[world] t+{self.sim_s:.0f}s loop busy {busy:.1f}% | clients "
                      + ", ".join(f"{r}:{len(s)}" for r, s in self.roles.items() if s))
                self.stats["busy_s"] = 0.0
                last_report = time.perf_counter()
            next_t += period
            delay = next_t - time.perf_counter()
            if delay < -1.0:                                    # fell far behind: resync, don't spiral
                next_t = time.perf_counter()
            await asyncio.sleep(max(0.0, delay))

def serve_http(port: int) -> None:
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
        def end_headers(self):                     # always serve fresh JS/CSS during the hackathon
            self.send_header("Cache-Control", "no-store")
            super().end_headers()
    handler = functools.partial(Quiet, directory=os.path.join(ROOT, "web"))
    httpd = http.server.ThreadingHTTPServer(("0.0.0.0", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"[world] web views on http://0.0.0.0:{port}/index.html?role=cockpitA|cockpitB|god")

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", help="harness/scenarios/*.json")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--http", type=int, default=8080, help="serve web/ on this port (0 = off)")
    ap.add_argument("--time-scale", type=float, help="1 for judges, 10 for A/B runs (overrides scenario)")
    ap.add_argument("--da", type=float, help="density altitude at the field, ft (overrides METAR/scenario)")
    ap.add_argument("--weather", help="weather preset (world/weather.py): metar, calm_morning, hot_gusty_afternoon, haboob, low_ceiling, pressure_drop")
    a = ap.parse_args()
    world = World(a.scenario, da_override=a.da, time_scale=a.time_scale, weather=a.weather)
    hub = Hub(world)
    if a.http:
        serve_http(a.http)
    async with serve(hub.handler, "0.0.0.0", a.port, compression=None):
        print(f"[world] '{world.scenario.name}' on ws://0.0.0.0:{a.port}  aircraft={list(world.fleet)}  "
              f"humans={world.humans}  DA={world.env.da_field_ft:.0f} ft  x{world.time_scale:g}  wx {world.env.wx.name}: {world.env.wx.metar_style()}")
        await hub.loop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
