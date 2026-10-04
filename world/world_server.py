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
                   {"type":"AP","ac_id","engage":true|false|null(toggle)} -> reply AP_STATUS, and
                   {"type":"RESET","ac_id"} -> aircraft back to its scenario start (WORLD_EVENT RESET).
  god              TRUTH (all aircraft) at 10 Hz, every ADVISORY/TRUST/COMMAND/PREDICTION/CRYSTAL,
                   WX (weather state) on change + WX_FIELD (thermal positions) every 2 s.
                   God -> world: SET_DA, and (Lane A) SET_WX {"preset"?, field: value...} /
                   {"type":"SET_WX","update_altimeters":true} (everyone dials the current QNH) /
                   {"type":"RESET_DEMO"}: every judge aircraft back to its scenario start, AI traffic on
                   those starts removed (WORLD_EVENT RESET per judge + RESET_DEMO).
FLOCK verification (advisory only, receive only - FLOCK_claude_code_prompt.md):
  avionics:<id>    the onboard unit (verify/unit.py) of judge aircraft <id>. Gets ONLY what that aircraft's
                   own equipment receives (world/sensors.py): {"type":"SENSORS","t","ac_id","msgs":[...]} at
                   10 Hz - ADS-B it hears, own TCAS tracks, Mode S replies, 1030 interrogations, own-ship
                   state, band stats. Sends back VERIFY (trust per target) -> own cockpit + god + log.
  god              also gets GROUND_TRUTH (1 Hz: every emitter with its label real/ghost, attack list -
                   simulation truth, never to units or cockpits) and may send SET_ATTACK
                   {"attack":"ghost","on":true|false,"victim"?}. Scenario key "attacks": [{"type","at_s",...}].
  COMMAND          rejected (rejected_by_world "advisory_only"): FLOCK never flies the aircraft. The legacy
                   collision-avoidance demo can still be run with --allow-takeover.
Live traffic (scenario "traffic", world/traffic.py TrafficGenerator): AI aircraft come and go; each
  gets WORLD_EVENT SPAWN / DESPAWN (log; god prunes TRUTH). With --traffic-nodes ws://<channel> the
  world starts a FLOCK node (node/node.py) for every new AI aircraft and stops it when it leaves; logs
  in harness/out/nodes/<id>.log. AI pilots act on their own node's ADVISORY (LOG kind decision,
  {"ai_pilot": ...}). LIVE_TRAFFIC frames also feed the generator when "live_seed" is on.
  log              LOG frames: decisions, radio (from channel), camera, world events, LIVE_TRAFFIC
  ENV              {"type":"ENV","t","da_field_ft"} to god AND every node on connect and whenever density
                   altitude changes (SET_DA / SET_WX) - nodes use it for climb capability. Density altitude
                   only: nodes never get the world's internal wind / turbulence model.
  LIVE_TRAFFIC     (v1.2) from role data (data/live_traffic.py): real ADS-B around KDVT -> god + log
                   (+ cockpits in the live demo, v1.3: display only, ADS-B In style). Never to nodes,
                   never acted on.
                   (incl. WORLD_EVENT NMAC / COLLISION / NMAC_END from world/separation.py)
  channel          TRUTH at 10 Hz (radio emulator decides delivery from it)
  camera, data     accepted; frames mirrored to log
Every client gets a HELLO frame on connect ({"type":"HELLO","role","ac_id",...}); god, log and
cockpits also get the static picture (airport, pattern legs, METAR) — charts, not traffic.

Also serves web/ over HTTP on --http (ES modules do not load from file://):
  http://<host>:8080/index.html?role=cockpitA | cockpitB | god
"""
from __future__ import annotations
import argparse, asyncio, atexit, functools, http.server, json, os, subprocess, sys, threading, time
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
from world.sensors import SensorSim
from verify.schema import Verify

PHYS_HZ = 20
VALIDATE = {"ADVISORY": schemas.Advisory, "COMMAND": schemas.Command, "TRUST": schemas.Trust,
            "INPUT": schemas.Input}

def dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))

class NodeLauncher:
    """One node/node.py process per live-traffic aircraft (background, no window, log to a file)."""
    def __init__(self, world_url: str, channel_url: str, pidfile: str | None = None):
        self.world_url, self.channel_url, self.pidfile = world_url, channel_url, pidfile
        self.procs: dict[str, subprocess.Popen] = {}
        self.logdir = os.path.join(ROOT, "harness", "out", "nodes")
        os.makedirs(self.logdir, exist_ok=True)
        atexit.register(self.stop_all)

    def start(self, ac_id: str) -> None:
        log = open(os.path.join(self.logdir, f"{ac_id}.log"), "w")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        p = subprocess.Popen([sys.executable, "-u", os.path.join("node", "node.py"), "--id", ac_id,
                              "--world", self.world_url, "--via-channel", self.channel_url],
                             cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
        log.close()
        self.procs[ac_id] = p
        if self.pidfile and os.path.exists(self.pidfile):           # run_demo.ps1 -Stop kills these too
            with open(self.pidfile, "a") as f:
                f.write(f"{p.pid}\n")

    def stop(self, ac_id: str) -> None:
        p = self.procs.pop(ac_id, None)
        if p is not None and p.poll() is None:
            p.terminate()

    def stop_all(self) -> None:
        for i in list(self.procs):
            self.stop(i)

class Hub:
    def __init__(self, world: World, nodes: NodeLauncher | None = None, advisory_only: bool = True):
        self.w = world
        self.nodes = nodes
        self.advisory_only = advisory_only                 # FLOCK never flies the aircraft (spec hard constraint 1)
        self.sensors = SensorSim(world)                    # what each judge aircraft's own equipment receives
        self.sensor_buf: dict[str, list] = defaultdict(list)
        self.scripted = sorted(world.raw.get("attacks", []), key=lambda a: a.get("at_s", 0))   # scenario attacks
        self.sim_start = None
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
        if raw.startswith("avionics:"):                    # FLOCK onboard unit (verify/unit.py) of a judge aircraft
            ac_id = raw[9:]
            ok = ac_id in self.w.fleet and self.w.fleet[ac_id].human
            return (raw, ac_id) if ok else (None, f"no judge aircraft {ac_id}; humans={self.w.humans}")
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
        if role.startswith("node:"):
            await ws.send(dumps(self.env_frame()))         # current density altitude for climb capability
        if role.startswith("cockpit:"):
            for k in ("ADVISORY", "TRUST", "VERIFY"):
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

        if role.startswith("avionics:"):                   # onboard unit: it may only report what it concluded
            if t == "VERIFY":
                try:
                    Verify.model_validate(m)
                except ValidationError as e:
                    print(f"[world] VERIFY schema warning from {role}: {str(e).splitlines()[0]}")
                m = dict(m, ac_id=own_id)
                self.latest[f"VERIFY:{own_id}"] = m
                self.send(f"cockpit:{own_id}", m)
                self.send("god", m)
                self.log(own_id, "decision", m)
            return

        if role.startswith("node:"):
            if own_id not in self.w.fleet:                 # its aircraft has left (live traffic)
                return
            ac_id = m.get("ac_id", own_id)
            if ac_id != own_id:
                print(f"[world] {role} sent {t} for {ac_id}; using its own id {own_id}")
                ac_id = own_id
                m = dict(m, ac_id=own_id)          # forward / log under the sender's real id, never the claimed one
            cockpit = f"cockpit:{ac_id}"
            if t in ("ADVISORY", "TRUST"):
                self.latest[f"{t}:{ac_id}"] = m
                self.send(cockpit, m)
                self.send("god", m)
                self.log(ac_id, "decision", m)
                if t == "ADVISORY":
                    print(f"[world] ADVISORY {ac_id} {m.get('level')} '{m.get('text')}' ttc={m.get('ttc_s')}")
                    self.ai_pilot_hears(ac_id, m)
            elif t == "COMMAND":
                ac = self.w.fleet[ac_id]
                if self.advisory_only:                     # advisory only: FLOCK never takes the controls
                    applied, why = False, "advisory_only"
                else:
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
                moving = ac.apply_input(float(m.get("roll", 0)), float(m.get("pitch", 0)), float(m.get("throttle", 0.5)), self.now(),
                                        brake=bool(m.get("brake", False)))
                if moving and ac.cmd is not None and ac.cmd.get("mode") == "TAKEOVER":
                    ac.cmd = None                     # pilot always wins; node must RELEASE within one tick
                    stick = {"type": "STICK", "ac_id": own_id, "t": round(self.now(), 3)}
                    self.send(f"node:{own_id}", stick)
                    self.send(f"cockpit:{own_id}", stick)
                    self.send("god", stick)
                    self.log(own_id, "world", stick)
                    print(f"[world] STICK {own_id} - pilot took it back")
            elif t == "RESET":
                old = self.w.fleet[own_id]
                self.announce_reset(self.w.reset_aircraft(own_id), f"{old.mode} {old.agl_ft:.0f} ft AGL")
            elif t == "AP":
                ac = self.w.fleet[own_id]
                want = m.get("engage")
                want = (not ac.ap_engaged) if want is None else bool(want)
                ok, why = self.w.engage_ap(own_id, want)       # always lands with the active runway flow
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
                self.publish_env()
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
                self.publish_env()
                print(f"[world] weather: {', '.join(what) or 'no change'} -> {self.w.env.wx.metar_style()}")
            elif t == "RESET_DEMO":
                self.reset_demo()
            elif t == "SET_ATTACK":
                self.set_attack(m)
            return

        if role == "data" and t == "LIVE_TRAFFIC":
            # real ADS-B overlay (contract v1.2): god + log only - never nodes / cockpits, never acted on
            try:
                schemas.LiveTraffic.model_validate(m)
            except ValidationError as e:
                print(f"[world] LIVE_TRAFFIC schema warning: {str(e).splitlines()[0]}")
            if self.w.traffic is not None:
                self.w.traffic.live = m                    # live_seed: real inbound aircraft become AI arrivals
            self.send("god", m)
            if self.w.traffic is not None or self.w.raw.get("live_sky_in_cockpit"):
                # live demo (live_kdvt): judges see the real aircraft on their radar + 3D view too (ADS-B In style)
                for r in [r for r in self.roles if r.startswith("cockpit:")]:
                    self.send(r, m)
            self.log("live", "world", m)
            return

        if role in ("channel", "camera", "data"):
            kind = "radio" if role == "channel" else "camera" if role == "camera" else "world"
            if t == "CRYSTAL":
                self.send("god", m)
            self.log(m.get("from", m.get("src", role)), kind, m)

    # ---------- resets ----------
    def announce_reset(self, ac, was: str) -> None:
        """An aircraft is back at its scenario start: clear its alert state, tell its cockpit, god, log."""
        i = ac.id
        self.sep.forget(i)
        self.taws.forget(i)
        self.taws_state.pop(i, None)
        ev = {"type": "WORLD_EVENT", "event": "RESET", "a": i, "t": round(self.now(), 3),
              "lat": round(ac.lat, 6), "lon": round(ac.lon, 6), "alt_msl_ft": round(ac.alt_msl_ft),
              "leg": ac.autopilot.leg, "was": was}
        self.send(f"cockpit:{i}", ev)
        self.send("god", ev)
        self.log(i, "world", ev)
        print(f"[world] RESET {i} -> scenario start ({ac.autopilot.leg}); was {was}")

    def reset_demo(self) -> None:
        """God view RESET DEMO: both judges back to their starts at once, traffic on those starts removed."""
        was = {i: f"{self.w.fleet[i].mode} {self.w.fleet[i].agl_ft:.0f} ft AGL" for i in self.w.humans}
        reset, removed = self.w.reset_demo()
        for ac in reset:
            self.announce_reset(ac, was[ac.id])
        self.despawned(removed, "demo reset")
        ev = {"type": "WORLD_EVENT", "event": "RESET_DEMO", "t": round(self.now(), 3),
              "aircraft": [a.id for a in reset], "removed": removed}
        self.send("god", ev)
        self.log("god", "world", ev)
        print(f"[world] RESET_DEMO judges {[a.id for a in reset]} back at their starts; removed traffic {removed}")

    # ---------- spoofing attacks (simulated radio environment only) ----------
    def set_attack(self, m: dict) -> None:
        """God view SET_ATTACK {"attack": "ghost", "on": true|false, "victim"?: id, params...}."""
        kind, on = str(m.get("attack", "ghost")), m.get("on", True) is not False
        if on:
            victim = self.w.fleet.get(m.get("victim") or (self.w.humans[0] if self.w.humans else ""))
            if victim is None:
                print(f"[world] SET_ATTACK: no victim aircraft")
                return
            params = {k: v for k, v in m.items() if k not in ("type", "attack", "on", "victim")}
            try:
                a = self.sensors.attacks.start(kind, victim, self.now(), **params)
            except (KeyError, TypeError) as e:
                print(f"[world] SET_ATTACK refused: {e}")
                return
            ev = {"type": "WORLD_EVENT", "event": "ATTACK_ON", "t": round(self.now(), 3), "attack": a.public(), "a": victim.id}
        else:
            ids = self.sensors.attacks.stop(kind=kind if kind != "all" else None)
            ev = {"type": "WORLD_EVENT", "event": "ATTACK_OFF", "t": round(self.now(), 3), "stopped": ids}
        self.send("god", ev)
        self.log("god", "world", ev)
        print(f"[world] {ev['event']} {kind} {ev.get('attack', ev.get('stopped'))}")

    def sensor_step(self, now: float, tick: int) -> None:
        """Feed every connected onboard unit what its aircraft's equipment received (10 Hz frames)."""
        if self.sim_start is None:
            self.sim_start = now
        while self.scripted and now - self.sim_start >= float(self.scripted[0].get("at_s", 0)):
            a = self.scripted.pop(0)
            self.set_attack({"attack": a.get("type", "ghost"), **{k: v for k, v in a.items() if k not in ("type", "at_s")}})
        listening = [r[9:] for r, s in self.roles.items() if r.startswith("avionics:") and s]
        for own_id, msgs in self.sensors.step(now, own_ids=listening).items():
            self.sensor_buf[own_id] += msgs
        if tick % 2 == 0:
            for own_id in listening:
                msgs = self.sensor_buf.pop(own_id, [])
                self.send(f"avionics:{own_id}", {"type": "SENSORS", "t": round(now, 3), "ac_id": own_id, "msgs": msgs})
        if tick % PHYS_HZ == 0 and self.roles.get("god"):          # 1 Hz: who is real, who is an attack (truth)
            self.send("god", {"type": "GROUND_TRUTH", "t": round(now, 3), "emitters": self.sensors.truth(now),
                              "attacks": self.sensors.attacks.public()})

    # ---------- live traffic ----------
    def ai_pilot_hears(self, ac_id: str, m: dict) -> None:
        """An AI aircraft's node gave advice: its pilot decides whether / how to follow (world/traffic.py)."""
        ac = self.w.fleet.get(ac_id)
        if ac is None or ac.human or not hasattr(ac.autopilot, "advise"):
            return
        entry = ac.autopilot.advise(m, self.now())
        if entry:
            self.log(ac_id, "decision", {"ai_pilot": ac_id, **entry})
            print(f"[world] AI {ac_id} {'will follow' if entry['follow'] else 'ignores'} {entry['level']} '{entry['text']}'")

    def traffic_step(self, now: float) -> None:
        spawned, removed = self.w.traffic.step(now)
        for i in spawned:
            ac, d = self.w.fleet[i], self.w.traffic.describe(i)
            ev = {"type": "WORLD_EVENT", "event": "SPAWN", "a": i, "t": round(now, 3), **d,
                  "lat": round(ac.lat, 6), "lon": round(ac.lon, 6), "alt_msl_ft": round(ac.alt_msl_ft),
                  "ap_equipped": ac.ap_equipped}
            self.log(i, "world", ev)
            if self.nodes is not None:
                self.nodes.start(i)
            print(f"[world] + {i} {d['mission']} {d['runway']}" + (f" (mirrors live {d['live']})" if d.get("live") else "")
                  + f"  | {len(self.w.traffic.active)} AI, {self.w.traffic.airborne()} airborne")
        self.despawned(removed, "left")

    def despawned(self, ids: list[str], why: str) -> None:
        for i in ids:
            self.sep.forget(i)
            for k in ("ADVISORY", "TRUST"):
                self.latest.pop(f"{k}:{i}", None)
            if self.nodes is not None:
                self.nodes.stop(i)
            ev = {"type": "WORLD_EVENT", "event": "DESPAWN", "a": i, "t": round(self.now(), 3), "why": why}
            self.log(i, "world", ev)
            print(f"[world] - {i} {why}")

    def env_frame(self) -> dict:
        return {"type": "ENV", "t": round(self.now(), 3), "da_field_ft": round(self.w.env.da_field_ft, 1)}

    def publish_env(self) -> None:
        """Density altitude to the god view and every node (Reya's node.on_env reads da_field_ft)."""
        ev = dumps(self.env_frame())
        self.send("god", ev)
        for role in list(self.roles):
            if role.startswith("node:"):
                self.send(role, ev)

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
        d["surface"] = ac.surface if ac.on_ground else None
        if ac.mode == "AUTOPILOT" and ac.autopilot is not None:      # own AP's leg, for the moving map
            d["ap_leg"], d["ap_rwy"] = ac.autopilot.leg, ac.autopilot.p.ident
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
                elif kind == "STOPPED":
                    ev.update(runway=val)
                else:
                    ev.update(reason=val)
                self.log(ac.id, "world", ev)
                self.send(f"cockpit:{ac.id}", ev)
                if ac.human or ev.get("hard"):
                    self.send("god", ev)          # (judge STOPPED events land in the god ticker too)
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
            self.sensor_step(now, tick)                         # onboard units: what their equipment received
            if tick % 4 == 0:                                   # 5 Hz: autopilot landers / departures check the runway
                for ev in self.w.runway_watch(now):
                    for role in (f"cockpit:{ev['a']}", "god"):
                        self.send(role, ev)
                    self.log(ev["a"], "world", ev)
                    print(f"[world] {ev['event']} {ev['a']} {ev['runway']}: {ev['reason']}")
            if self.w.traffic is not None and tick % PHYS_HZ == 0:      # 1 Hz: live traffic comes and goes
                self.traffic_step(now)
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
    ap.add_argument("--seed", type=int, help="live-traffic seed (overrides the scenario's traffic.seed)")
    ap.add_argument("--traffic-nodes", metavar="CHANNEL_WS", help="start a FLOCK node for every live-traffic aircraft, "
                    "radio via this channel (e.g. ws://localhost:8766)")
    ap.add_argument("--pidfile", help="append node PIDs here (run_demo.ps1 .demo_pids)")
    ap.add_argument("--allow-takeover", action="store_true", help="legacy collision-avoidance demo: let nodes fly the aircraft (COMMAND). Off by default: FLOCK is advisory only")
    a = ap.parse_args()
    world = World(a.scenario, da_override=a.da, time_scale=a.time_scale, weather=a.weather, seed=a.seed)
    nodes = NodeLauncher(f"ws://localhost:{a.port}", a.traffic_nodes, a.pidfile) if a.traffic_nodes and world.traffic else None
    hub = Hub(world, nodes, advisory_only=not a.allow_takeover)
    if a.http:
        serve_http(a.http)
    async with serve(hub.handler, "0.0.0.0", a.port, compression=None):
        print(f"[world] '{world.scenario.name}' on ws://0.0.0.0:{a.port}  aircraft={list(world.fleet)}  "
              f"humans={world.humans}  DA={world.env.da_field_ft:.0f} ft  x{world.time_scale:g}  wx {world.env.wx.name}: {world.env.wx.metar_style()}")
        if world.traffic is not None:
            print(f"[world] live traffic: flow {world.flow} runways {world.traffic_runways}  seed {world.traffic.seed}  "
                  f"airborne {world.traffic.lo}-{world.traffic.hi}  nodes {'on via ' + a.traffic_nodes if nodes else 'off'}")
        await hub.loop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
