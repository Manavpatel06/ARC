"""
radio/client.py — the ARC radio library nodes use (Lane C, tasks C1 + C4 + C5).
Same API as stubs/loopback_radio.RadioClient, so Lane B swaps by changing one import:

    from radio.client import RadioClient
    radio = RadioClient(ac_id="N101")                       # multicast via channel.py (default)
    radio = RadioClient(ac_id="N101", via_channel="ws://192.168.137.1:8766")   # Wi-Fi blocks multicast
    radio = RadioClient(ac_id="N101", direct=True)          # no channel at all (like the stub)
    # slots=True by default: STATE/HEARTBEAT go out in this node's AIS-style slot (<= 1 s wait),
    # INTENT/SEQ_*/MANEUVER_COMMIT in the next own-or-burst slot. slots=False = transmit immediately.
    await radio.start()
    radio.on_message(lambda env: ...)        # validated envelope dict; own messages never echoed
    env = await radio.send("STATE", body)    # wraps envelope, numbers seq, timestamps, signs
    radio.stats                              # {"tx","rx","rejected"}
    await radio.stop()

Extras (optional, Lane B may ignore):
    radio.on_link(lambda peer, state, age_s: ...)      # "LOST" / "RESTORED" (no authenticated packet for 3 s)
    radio.on_reject(lambda env, reason: ...)
    radio.links()                                      # {peer: {"state": "UP"|"LOST", "age_s": 1.2}}
    radio.set_neighbor_report(fn)                      # fn() -> {"N204":1,"GHOST7":0}; rides in HEARTBEAT as "nb"

Every delivered envelope carries three receive-side annotations (underscore = not on the air):
    env["_auth"]  "ok" | "unsigned" | "unknown_key"      ("bad signature" is rejected, never delivered)
    env["_rf"]    {"rssi_dbm", "doppler_hz"} emulated by the channel, or None in --direct mode
    env["_rx_t"]  receiver clock at delivery
Unsigned / unknown-key messages ARE delivered (so radio/evidence.py can flag them SUSPICIOUS) unless
accept_unauthenticated=False. Nodes must never act on a target whose _auth != "ok".

Receive checks, in order, each rejection logged with its reason:
    schema (RadioMsg + body model) -> time window ±2 s -> signature -> seq strictly increasing.
An unsigned message claiming an id that has a registered key is rejected as "unsigned_impersonation".

Run a smoke test:   python radio/client.py --id N101 [--via-channel ws://IP:8766 | --direct] [--no-slots]
"""
from __future__ import annotations
import argparse, asyncio, collections, json, os, sys, time
from typing import Callable, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from schemas import (RadioMsg, StateBody, IntentBody, SeqProposeBody, SeqAcceptBody,   # noqa: E402
                     ManeuverCommitBody, SightingBody, HeartbeatBody)
from radio.crypto import KeyRing, ReplayGuard, verify, node_key, initial_seq                          # noqa: E402
from radio.slots import SlotScheduler, URGENT_MSGS                                      # noqa: E402
from radio import wire                                                                  # noqa: E402

BODY_MODELS = {"STATE": StateBody, "INTENT": IntentBody, "SEQ_PROPOSE": SeqProposeBody,
               "SEQ_ACCEPT": SeqAcceptBody, "MANEUVER_COMMIT": ManeuverCommitBody,
               "SIGHTING": SightingBody, "HEARTBEAT": HeartbeatBody}
MAX_BODY_BYTES = 200


class RadioClient:
    def __init__(self, ac_id: str, mcast_group: str = wire.GROUP, port: int = wire.UPLINK_PORT,
                 via_channel: Optional[str] = None, *, direct: bool = False, iface: Optional[str] = None,
                 slots: bool = True, heartbeat_s: Optional[float] = 2.0, link_timeout_s: float = 3.0,
                 accept_unauthenticated: bool = True, clock: Callable[[], float] = time.time,
                 verbose: bool = True):
        self.ac_id = ac_id
        self.group, self.port = mcast_group, port
        self.via_channel = via_channel
        self.direct = direct
        self.iface = iface
        self.heartbeat_s = heartbeat_s
        self.link_timeout_s = link_timeout_s
        self.accept_unauth = accept_unauthenticated
        self.clock = clock
        self.verbose = verbose
        self.seq = initial_seq()
        self.stats = {"tx": 0, "rx": 0, "rejected": 0}
        self.reasons: collections.Counter = collections.Counter()
        self.key = node_key(ac_id)
        self.keyring = KeyRing()
        self.guard = ReplayGuard(clock=clock)
        self.unauth_seq: dict[str, int] = {}
        self.sched = SlotScheduler(ac_id, clock=clock) if slots else None
        self._queue: list[tuple[float, dict]] = []
        self._last_t = 0.0
        self._queue_evt: Optional[asyncio.Event] = None
        self._cb: list[Callable[[dict], None]] = []
        self._reject_cb: list[Callable[[dict, str], None]] = []
        self._link_cb: list[Callable[[str, str, float], None]] = []
        self._nb_fn: Optional[Callable[[], dict]] = None
        self._last_ok: dict[str, float] = {}
        self._link_state: dict[str, str] = {}
        self._transport = None
        self._ws = None
        self._tasks: list[asyncio.Task] = []
        self._mode = "?"
        self._running = False

    # ---------------- public API ----------------
    def on_message(self, cb: Callable[[dict], None]):
        self._cb.append(cb)

    def on_reject(self, cb: Callable[[dict, str], None]):
        self._reject_cb.append(cb)

    def on_link(self, cb: Callable[[str, str, float], None]):
        self._link_cb.append(cb)

    def set_neighbor_report(self, fn: Callable[[], dict]):
        self._nb_fn = fn

    def links(self) -> dict:
        now = self.clock()
        return {p: {"state": self._link_state.get(p, "UP"), "age_s": round(now - t, 2)} for p, t in self._last_ok.items()}

    async def start(self):
        self._running = True
        self._queue_evt = asyncio.Event()
        if self.via_channel:
            import websockets
            url = self.via_channel.rstrip("/") + f"/?id={self.ac_id}"
            self._ws = await websockets.connect(url)
            self._mode = f"via-channel {self.via_channel}"
            self._tasks.append(asyncio.create_task(self._ws_reader()))
        else:
            listen_port = self.port if self.direct else wire.DOWNLINK_PORT
            sock, dest_host, mode = wire.open_udp(listen_port, self.group, self.iface)
            self._dest = (dest_host, self.port)
            self._transport = await wire.udp_endpoint(sock, lambda f, a: self._on_frame(f))
            self._mode = ("direct " if self.direct else "channel ") + mode
        self._log(f"up ({self._mode}) slots={'on' if self.sched else 'off'}")
        self._tasks.append(asyncio.create_task(self._announce_loop()))
        self._tasks.append(asyncio.create_task(self._link_loop()))
        if self.heartbeat_s:
            self._tasks.append(asyncio.create_task(self._heartbeat_loop()))
        if self.sched:
            self._tasks.append(asyncio.create_task(self._slot_loop()))

    async def send(self, msg: str, body: dict) -> dict:
        model = BODY_MODELS.get(msg)
        if model is None:
            raise ValueError(f"unknown radio msg {msg}")
        model.model_validate(body)                                      # fail loudly on a bad body
        if len(wire.dumps(body)) > MAX_BODY_BYTES and self.verbose:
            self._log(f"WARNING {msg} body {len(wire.dumps(body))} B > {MAX_BODY_BYTES} B budget")
        self.seq += 1
        now = self.clock()
        t_tx = self.sched.next_tx_time(now, urgent=msg in URGENT_MSGS) if self.sched else now
        t_tx = max(t_tx, self._last_t + 1e-4)          # seq order == time order, even after a slot move
        self._last_t = t_tx
        env = {"msg": msg, "from": self.ac_id, "seq": self.seq, "t": round(t_tx, 4), "body": body}
        env["sig"] = self.key.sign(env)
        RadioMsg.model_validate(env)
        if self.sched:
            if msg == "STATE":                                          # newest STATE replaces a queued one
                self._queue = [(t, e) for t, e in self._queue if e["msg"] != "STATE"]
            self._queue.append((t_tx, env))
            self._queue.sort(key=lambda x: x[0])
            self._queue_evt.set()
        else:
            await self._transmit(env)
        return env

    async def stop(self):
        self._running = False
        for t in self._tasks:
            t.cancel()
        if self._transport:
            self._transport.close()
        if self._ws:
            await self._ws.close()

    # ---------------- transmit ----------------
    async def _uplink(self, frame: dict):
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps(frame))
            except Exception as e:
                self._log(f"uplink error {e}")
        elif self._transport is not None:
            self._transport.sendto(wire.dumps(frame), self._dest)

    async def _transmit(self, env: dict):
        await self._uplink({"ctl": "TX", "tx": self.ac_id, "env": env})
        self.stats["tx"] += 1

    async def _slot_loop(self):
        while self._running:
            if not self._queue:
                self._queue_evt.clear()
                await self._queue_evt.wait()
                continue
            t_next = self._queue[0][0]
            delay = t_next - self.clock()
            if delay > 0:
                try:
                    self._queue_evt.clear()
                    await asyncio.wait_for(self._queue_evt.wait(), timeout=delay)
                    continue                                            # queue changed; re-plan
                except asyncio.TimeoutError:
                    pass
            now = self.clock()
            due = [e for t, e in self._queue if t <= now + 1e-3]
            self._queue = [(t, e) for t, e in self._queue if t > now + 1e-3]
            for env in due:
                await self._transmit(env)

    async def _announce_loop(self):
        n = 0
        while self._running:
            await self._uplink({"ctl": "HELLO", "id": self.ac_id})
            await self._uplink({"ctl": "KEYS", "id": self.ac_id, "pub": self.key.pub_b64})
            n += 1
            await asyncio.sleep(2.0 if n < 8 else 10.0)

    async def _heartbeat_loop(self):
        while self._running:
            await asyncio.sleep(self.heartbeat_s)
            body = {"alive": True}
            if self._nb_fn:
                try:
                    nb = self._nb_fn() or {}
                    if nb:
                        body["nb"] = dict(list(nb.items())[:12])
                except Exception as e:
                    self._log(f"neighbor report error {e}")
            try:
                await self.send("HEARTBEAT", body)
            except Exception as e:
                self._log(f"heartbeat error {e}")

    async def _link_loop(self):
        while self._running:
            await asyncio.sleep(0.2)
            now = self.clock()
            for peer, t in list(self._last_ok.items()):
                age = now - t
                if age > self.link_timeout_s and self._link_state.get(peer) != "LOST":
                    self._set_link(peer, "LOST", age)

    def _set_link(self, peer: str, state: str, age: float):
        self._link_state[peer] = state
        self._log(f"LINK {peer} {state} (last heard {age:.1f} s ago)")
        for cb in self._link_cb:
            try:
                cb(peer, state, age)
            except Exception as e:
                self._log(f"link callback error {e}")
        asyncio.ensure_future(self._uplink({"ctl": "LINK", "by": self.ac_id, "peer": peer, "state": state,
                                            "age_s": round(age, 2), "t": now_s(self)}))

    # ---------------- receive ----------------
    async def _ws_reader(self):
        try:
            async for raw in self._ws:
                try:
                    self._on_frame(json.loads(raw))
                except Exception:
                    pass
        except Exception as e:
            if self._running:
                self._log(f"channel connection lost: {e}")

    def _on_frame(self, f: dict):
        ctl = f.get("ctl")
        if ctl == "DELIVER":
            if f.get("to") == self.ac_id:
                self._on_env(f.get("env") or {}, f.get("rf"))
        elif ctl == "KEYS":
            if "keys" in f and not self.direct:                         # registrar (channel) is authoritative
                self.keyring.keys.update(f["keys"])
            elif self.direct and f.get("id") and f.get("pub") and f["id"] != self.ac_id:
                self.keyring.add(f["id"], f["pub"])                     # direct mode: trust on first use
        elif ctl == "GARBLE":
            if self.sched and f.get("to") == self.ac_id:
                self.sched.mark_garbled(int(f["slot"]), int(f["frame"]))
        elif ctl == "TX" and self.direct:
            self._on_env(f.get("env") or {}, None)
        elif "msg" in f and self.direct:
            self._on_env(f, None)

    def _reject(self, env: dict, reason: str):
        self.stats["rejected"] += 1
        self.reasons[reason.split("(")[0]] += 1
        self._log(f"REJECT {env.get('msg')} from {env.get('from')} seq {env.get('seq')}: {reason}")
        for cb in self._reject_cb:
            try:
                cb(env, reason)
            except Exception:
                pass
        asyncio.ensure_future(self._uplink({"ctl": "REJECT", "by": self.ac_id, "from": env.get("from"),
                                            "msg": env.get("msg"), "seq": env.get("seq"), "reason": reason,
                                            "t": now_s(self)}))

    def _on_env(self, env: dict, rf: Optional[dict]):
        if env.get("from") == self.ac_id:
            return
        try:
            RadioMsg.model_validate(env)
            BODY_MODELS[env["msg"]].model_validate(env["body"])
        except Exception as e:
            return self._reject(env, f"schema({type(e).__name__})")
        why = self.guard.check_time(env)
        if why:
            return self._reject(env, why)
        sender = env["from"]
        pub = self.keyring.get(sender)
        if not env.get("sig"):
            if pub:
                return self._reject(env, "unsigned_impersonation")
            auth = "unsigned"
        elif pub is None:
            auth = "unknown_key"
        elif verify(pub, env):
            auth = "ok"
        else:
            return self._reject(env, "bad_signature")
        if auth == "ok":
            why = self.guard.check_seq(env)
            if why:
                return self._reject(env, why)
            self.guard.accept(env)
            now = self.clock()
            if self._link_state.get(sender) == "LOST":
                self._set_link(sender, "RESTORED", now - self._last_ok.get(sender, now))
            self._link_state[sender] = "UP"
            self._last_ok[sender] = now
        else:
            if not self.accept_unauth:
                return self._reject(env, auth)
            # no seq check: an unsigned seq proves nothing; radio/evidence.py scores these senders
        if self.sched:
            self.sched.observe(sender, env["t"])
        out = dict(env, _auth=auth, _rf=rf, _rx_t=self.clock())
        self.stats["rx"] += 1
        for cb in self._cb:
            try:
                cb(out)
            except Exception as e:
                self._log(f"callback error: {e}")

    def _log(self, s: str):
        if self.verbose:
            print(f"[radio {self.ac_id}] {s}", flush=True)


def now_s(rc: RadioClient) -> float:
    return round(rc.clock(), 3)


async def _smoke(a):
    r = RadioClient(a.id, via_channel=a.via_channel, direct=a.direct, iface=a.iface, slots=not a.no_slots)
    r.on_message(lambda m: m["msg"] != "HEARTBEAT" and print(
        f"[radio {a.id}] <- {m['msg']} from {m['from']} seq {m['seq']} auth={m['_auth']} rf={m['_rf']}"))
    await r.start()
    from pattern import place
    p = place("DOWNWIND", "25L", offset_s=0)
    while True:
        await r.send("STATE", {"lat": p["lat"], "lon": p["lon"], "alt_press_ft": p["alt_msl_ft"], "gs_kt": 90,
                               "track_deg": p["hdg_deg"], "vs_fpm": 0, "leg": "DOWNWIND", "intent": "", "ap_equipped": True})
        await asyncio.sleep(1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", default="N101")
    ap.add_argument("--via-channel", help="ws://<channel-ip>:8766 when Wi-Fi blocks multicast")
    ap.add_argument("--direct", action="store_true", help="no channel process: peer-to-peer multicast, no loss")
    ap.add_argument("--iface", help="local IP of the hotspot interface (multi-NIC Windows laptops)")
    ap.add_argument("--no-slots", action="store_true", help="transmit immediately instead of in an AIS-style slot")
    try:
        asyncio.run(_smoke(ap.parse_args()))
    except KeyboardInterrupt:
        pass
