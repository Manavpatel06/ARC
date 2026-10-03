"""
radio/channel.py — the emulated air between FLOCK nodes (Lane C, task C2).

The ONLY process that knows true positions (world role `channel`, TRUTH frames), and it uses them
only to decide delivery and to attach emulated RSSI/Doppler. Nodes never see truth.

For every transmitted packet and every receiver:
  1. range   — drop if horizontal range sender->receiver > 4,828 m (3 statute miles)  reason "range(5.1km)"
  2. loss    — drop with probability --loss (default 0.10)                               reason "loss"
  3. latency — deliver after --latency ± --jitter s (default 0.3 ± 0.1)
  4. collision — two different transmitters on the air at overlapping times (30 ms airtime), both
     heard by this receiver -> both lost, receiver gets a GARBLE (energy, no decode)    reason "collision(N204)"
  5. faults  — radio/faults.py rules (drop a COMMIT, silence a node, latency spike)      reason "fault:..."
Every delivered AND dropped packet is mirrored to the world log as
  {"msg","from","seq","t","body", "to","delivered","reason","rng_m","latency_s","rssi_dbm"}
(the world wraps it as LOG kind=radio; web/log.html and stubs/tail_log.py read these fields).

Key registrar (hackathon simplification): nodes announce Ed25519 public keys at boot; the channel
pins them only for aircraft the world knows (HELLO/TRUTH list) and rebroadcasts the key table.
In a real system this is a certificate tied to the aircraft registration.

Transports (both at once): UDP multicast uplink 239.1.1.1:5005 / downlink :5006, and a WebSocket
server on :8766 for nodes on Wi-Fi that blocks multicast (`RadioClient(via_channel="ws://<ip>:8766")`).

Run:  python radio/channel.py --world ws://<world-ip>:8765 [--loss 0.1] [--latency 0.3] [--jitter 0.1]
      [--iface <my-hotspot-ip>] [--log all|drops|none] [--no-world]   (--no-world: everyone in range, for tests)
"""
from __future__ import annotations
import argparse, asyncio, collections, json, os, random, sys, time
from typing import Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from radio import wire, rf                      # noqa: E402
from radio.crypto import KeyRing                # noqa: E402
from radio.slots import N_SLOTS, FRAME_S, AIRTIME_FRAC   # noqa: E402

RANGE_M = 4828.0
AIRTIME_S = FRAME_S / N_SLOTS * AIRTIME_FRAC     # 30 ms


class Channel:
    def __init__(self, a):
        self.a = a
        self.rng = random.Random(a.seed)
        self.truth: dict[str, dict] = {}
        self.world_ids: set[str] = set()
        self.sites: dict[str, dict] = {}
        self.receivers: set[str] = set()
        self.ws_clients: dict[str, object] = {}
        self.keyring = KeyRing()
        self.air: list[dict] = []                 # recent transmissions for collision checks
        self.faults: list[dict] = []
        self.link_next: dict[tuple, float] = {}
        self.stats = collections.Counter()
        self.world_ws = None
        self.log_q: asyncio.Queue = asyncio.Queue(maxsize=5000)
        self.transport = None

    # ------------------------------------------------------------ world link
    async def world_loop(self):
        import websockets
        url = f"{self.a.world}?role=channel"
        while True:
            try:
                async with websockets.connect(url, max_size=None) as ws:
                    self.world_ws = ws
                    print(f"[channel] connected to world {url}", flush=True)
                    async for raw in ws:
                        m = json.loads(raw)
                        if m.get("type") == "TRUTH":
                            for ac in m.get("aircraft", []):
                                self.truth[ac["ac_id"]] = ac
                                self.world_ids.add(ac["ac_id"])
                        elif m.get("type") == "HELLO":
                            self.world_ids.update(x["id"] for x in m.get("aircraft", []) if x.get("flock", True))
            except Exception as e:
                self.world_ws = None
                print(f"[channel] world link down ({e}); retrying in 2 s", flush=True)
                await asyncio.sleep(2)

    async def log_loop(self):
        while True:
            item = await self.log_q.get()
            if self.world_ws is not None:
                try:
                    await self.world_ws.send(json.dumps(item))
                except Exception:
                    pass

    def log(self, item: dict):
        if item.get("from") is None:
            item["from"] = "channel"
        if self.a.log == "none" or (self.a.log == "drops" and item.get("delivered", False)):
            return
        try:
            self.log_q.put_nowait(item)
        except asyncio.QueueFull:
            self.stats["log_overflow"] += 1

    # ------------------------------------------------------------ downlink
    async def downlink(self, frame: dict, to: Optional[str] = None):
        if to and to in self.ws_clients:
            try:
                await self.ws_clients[to].send(json.dumps(frame))
            except Exception:
                self.ws_clients.pop(to, None)
            return
        if to is None:                                   # broadcast frame (KEYS)
            for ws in list(self.ws_clients.values()):
                try:
                    await ws.send(json.dumps(frame))
                except Exception:
                    pass
        if self.transport is not None:
            self.transport.sendto(wire.dumps(frame), (self.dest_host, wire.DOWNLINK_PORT))

    async def keys_loop(self):
        while True:
            if self.keyring.keys:
                await self.downlink({"ctl": "KEYS", "keys": self.keyring.keys})
            await asyncio.sleep(2.0)

    # ------------------------------------------------------------ uplink
    def on_uplink(self, f: dict, ws_id: Optional[str] = None):
        ctl = f.get("ctl")
        if ctl == "TX":
            asyncio.ensure_future(self.on_tx(f))
        elif ctl == "HELLO" and f.get("id"):
            self.receivers.add(f["id"])
        elif ctl == "KEYS" and f.get("id") and f.get("pub"):
            self.on_keys(f["id"], f["pub"])
        elif ctl == "SITE" and f.get("tx"):
            self.sites[f["tx"]] = {"lat": f["lat"], "lon": f["lon"], "alt_ft": f.get("alt_ft", 1500),
                                   "gs_kt": 0, "track_deg": 0, "vs_fpm": 0}
        elif ctl == "REJECT":
            self.stats["rejected_by_nodes"] += 1
            self.log({"msg": f.get("msg"), "from": f.get("from"), "seq": f.get("seq"), "t": f.get("t", time.time()),
                      "to": f.get("by"), "delivered": False, "reason": f.get("reason"), "rejected_by": f.get("by")})
        elif ctl == "LINK":
            self.log({"type": "LINK", "from": f.get("peer"), "to": f.get("by"), "state": f.get("state"),
                      "age_s": f.get("age_s"), "t": f.get("t", time.time())})
        elif ctl == "FAULT":
            self.add_fault(f)

    def on_keys(self, ac_id: str, pub: str):
        if self.a.world and not self.a.no_world and self.world_ids and ac_id not in self.world_ids:
            if self.stats[f"key_refused:{ac_id}"] == 0:
                print(f"[channel] KEY REFUSED {ac_id}: not a registered aircraft", flush=True)
                self.log({"type": "KEY_REFUSED", "from": ac_id, "delivered": False, "reason": "key_not_in_registry",
                          "t": time.time()})
            self.stats[f"key_refused:{ac_id}"] += 1
            return
        res = self.keyring.add(ac_id, pub)
        if res == "added":
            print(f"[channel] key pinned for {ac_id}", flush=True)
            asyncio.ensure_future(self.downlink({"ctl": "KEYS", "keys": self.keyring.keys}))
        elif res == "conflict" and self.stats[f"key_conflict:{ac_id}"] == 0:
            print(f"[channel] KEY CONFLICT for {ac_id}: a second key was refused", flush=True)
            self.log({"type": "KEY_REFUSED", "from": ac_id, "delivered": False, "reason": "key_conflict", "t": time.time()})
            self.stats[f"key_conflict:{ac_id}"] += 1

    # ------------------------------------------------------------ faults
    def add_fault(self, f: dict):
        if f.get("id") and any(r.get("id") == f["id"] for r in self.faults):
            return                                        # faults.py sends each UDP frame 3 times
        rule = dict(f)
        rule["added"] = time.time()
        if "duration_s" in rule:
            rule["until"] = time.time() + float(rule["duration_s"])
        self.faults.append(rule)
        print(f"[channel] FAULT armed {rule}", flush=True)
        self.log({"type": "FAULT", "from": rule.get("target", "channel"), "t": time.time(), "fault": rule})

    def fault_for(self, env: dict, now: float) -> tuple[Optional[str], float]:
        """Returns (drop_reason or None, extra_latency_s)."""
        extra = 0.0
        self.faults = [r for r in self.faults if r.get("until", 1e18) > now and r.get("count", 1) > 0]
        for r in self.faults:
            kind = r.get("kind")
            if kind == "latency":
                extra = max(extra, float(r.get("value_s", 2.0)) - self.a.latency)
            elif kind == "drop":
                if (r.get("msg") in (None, env["msg"])) and (r.get("target") in (None, env["from"])):
                    r["count"] = r.get("count", 1) - 1
                    return f"fault:drop_{env['msg'].lower()}", 0.0
            elif kind == "silence":
                if r.get("target") == env["from"] and r.get("msg") in (None, "*", env["msg"]):
                    return "fault:silenced", 0.0
        return None, extra

    # ------------------------------------------------------------ physics
    def position(self, radio_id: str) -> Optional[dict]:
        if radio_id in self.truth:
            return rf.from_ownship(self.truth[radio_id])
        return self.sites.get(radio_id)

    async def on_tx(self, f: dict):
        now = time.time()
        env, tx_id = f.get("env") or {}, f.get("tx") or (f.get("env") or {}).get("from")
        if not isinstance(env, dict) or "msg" not in env or "from" not in env:
            self.stats["malformed"] += 1
            return
        if tx_id in self.truth:
            self.receivers.add(tx_id)
        self.stats["tx"] += 1
        tx_pos = self.position(tx_id)
        rec = {"tx": tx_id, "start": now, "end": now + AIRTIME_S, "pos": tx_pos}
        self.air = [r for r in self.air if now - r["start"] < 2.0]
        self.air.append(rec)
        base = {k: env.get(k) for k in ("msg", "from", "seq", "t", "body")}
        fault, extra_lat = self.fault_for(env, now)
        for rx in sorted(self.receivers - {tx_id}):
            rx_pos = self.position(rx)
            if rx_pos is None and not self.a.no_world:
                continue
            if tx_pos is None and not self.a.no_world:
                self.drop(base, rx, "unknown_transmitter_position", None)
                continue
            rng_m = rf.horiz_range_m(tx_pos, rx_pos) if (tx_pos and rx_pos) else 0.0
            if fault:
                self.drop(base, rx, fault, rng_m)
            elif rng_m > self.a.range_m:
                self.drop(base, rx, f"range({rng_m/1000:.1f}km)", rng_m)
            elif self.rng.random() < self.a.loss:
                self.drop(base, rx, "loss", rng_m)
            else:
                lat = max(0.05, self.a.latency + self.rng.uniform(-self.a.jitter, self.a.jitter)) + extra_lat
                # a link never reorders: back-to-back packets in one slot arrive in the order sent
                key = (tx_id, rx)
                lat = max(lat, self.link_next.get(key, 0.0) - now + 0.002)
                self.link_next[key] = now + lat
                rfm = None
                if tx_pos and rx_pos:
                    slant, rdot = rf.geometry(tx_pos, rx_pos)
                    rfm = {"rssi_dbm": round(rf.rssi_dbm(slant) + self.rng.gauss(0, rf.RSSI_SIGMA_DB), 1),
                           "doppler_hz": round(rf.doppler_hz(rdot) + self.rng.gauss(0, rf.DOPPLER_SIGMA_HZ), 1)}
                asyncio.get_running_loop().call_later(lat, lambda rx=rx, rng_m=rng_m, lat=lat, rfm=rfm:
                                                      asyncio.ensure_future(self.deliver(env, base, rec, rx, rng_m, lat, rfm)))

    async def deliver(self, env, base, rec, rx, rng_m, lat, rfm):
        rx_pos = self.position(rx)
        hits = [] if self.a.collisions == "off" else [r for r in self.air if r is not rec and r["tx"] != rec["tx"]
                and r["start"] < rec["end"] and r["end"] > rec["start"]
                and (rx_pos is None or r["pos"] is None or rf.horiz_range_m(r["pos"], rx_pos) <= self.a.range_m)]
        if hits:
            slot = int((rec["start"] % FRAME_S) / (FRAME_S / N_SLOTS))
            await self.downlink({"ctl": "GARBLE", "to": rx, "slot": slot, "frame": int(rec["start"] // FRAME_S)}, rx)
            return self.drop(base, rx, f"collision({hits[0]['tx']})", rng_m)
        await self.downlink({"ctl": "DELIVER", "to": rx, "rf": rfm, "env": env}, rx)
        self.stats["delivered"] += 1
        self.log(dict(base, to=rx, delivered=True, reason="", rng_m=round(rng_m), latency_s=round(lat, 3),
                      rssi_dbm=rfm and rfm["rssi_dbm"]))

    def drop(self, base, rx, reason, rng_m):
        self.stats["drop:" + reason.split("(")[0]] += 1
        self.log(dict(base, to=rx, delivered=False, reason=reason, rng_m=None if rng_m is None else round(rng_m)))

    # ------------------------------------------------------------ servers
    async def ws_handler(self, ws):
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(wire.ws_path(ws)).query)
        rid = q.get("id", [None])[0]
        if rid:
            self.ws_clients[rid] = ws
            self.receivers.add(rid)
            print(f"[channel] + ws radio {rid}", flush=True)
        try:
            async for raw in ws:
                try:
                    self.on_uplink(json.loads(raw), rid)
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            if rid and self.ws_clients.get(rid) is ws:
                self.ws_clients.pop(rid, None)
                print(f"[channel] - ws radio {rid}", flush=True)

    async def stats_loop(self):
        while True:
            await asyncio.sleep(5)
            s = self.stats
            drops = {k[5:]: v for k, v in s.items() if k.startswith("drop:")}
            print(f"[channel] tx={s['tx']} delivered={s['delivered']} drops={drops} "
                  f"node_rejects={s['rejected_by_nodes']} radios={sorted(self.receivers)} keys={sorted(self.keyring.keys)}",
                  flush=True)

    async def run(self):
        import websockets
        sock, self.dest_host, mode = wire.open_udp(wire.UPLINK_PORT, wire.GROUP, self.a.iface)
        self.transport = await wire.udp_endpoint(sock, lambda f, addr: self.on_uplink(f))
        tasks = [self.log_loop(), self.keys_loop(), self.stats_loop()]
        if not self.a.no_world:
            tasks.append(self.world_loop())
        async with websockets.serve(self.ws_handler, "0.0.0.0", self.a.ws_port):
            print(f"[channel] up: UDP {mode} uplink :{wire.UPLINK_PORT} downlink :{wire.DOWNLINK_PORT}, "
                  f"ws :{self.a.ws_port} | loss {self.a.loss} latency {self.a.latency}±{self.a.jitter} s "
                  f"range {self.a.range_m:.0f} m", flush=True)
            await asyncio.gather(*tasks)


def parse(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--world", default="ws://localhost:8765")
    ap.add_argument("--loss", type=float, default=0.10)
    ap.add_argument("--latency", type=float, default=0.30)
    ap.add_argument("--jitter", type=float, default=0.10)
    ap.add_argument("--range-m", type=float, default=RANGE_M)
    ap.add_argument("--ws-port", type=int, default=wire.CHANNEL_WS_PORT)
    ap.add_argument("--iface", help="this laptop's hotspot IP, if multicast picks the wrong network card")
    ap.add_argument("--collisions", choices=["on", "off"], default="on", help="emulate packets overlapping on the air")
    ap.add_argument("--log", choices=["all", "drops", "none"], default="all")
    ap.add_argument("--no-world", action="store_true", help="no world: everyone in range, no RSSI (tests)")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--scenario", help="read loss/latency from a scenario file's 'channel' block")
    a = ap.parse_args(argv)
    if a.scenario:
        ch = json.load(open(a.scenario)).get("channel", {})
        a.loss = ch.get("loss", a.loss)
        a.latency = ch.get("latency_s", a.latency)
    return a


if __name__ == "__main__":
    try:
        asyncio.run(Channel(parse()).run())
    except KeyboardInterrupt:
        pass
