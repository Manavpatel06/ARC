"""
radio/spoofer.py — rogue radio for the trust demo (Lane C, task C6). Its antenna sits on the ground
(--site, default 2.5 km south of KDVT); it CLAIMS to be an aircraft on final 25L.

Attacks (pick one with --mode; default unsigned):
  unsigned     GHOST7 STATE with no signature                       -> nodes: SUSPICIOUS, then FAKE (RF/peers)
  unknown-key  GHOST7 signed with its own key; it also tries to register the key -> channel refuses it
  impossible   GHOST7 at 400 kt with 1 km position jumps             -> kinematics_violation
  impersonate  unsigned STATE claiming to be a REAL aircraft (--as N204) -> rejected "unsigned_impersonation"
  replay       eavesdrops the downlink and re-transmits real packets: immediately (-> "replay_seq")
               and 3 s later (-> "time_window"); also flips a field in a copy (-> "bad_signature")
  --sybil 3    three mutually consistent fakes (GHOST7/8/9) 300 m apart that vouch for each other in their
               HEARTBEAT neighbor reports — the known limit, shown honestly.

Run:  python radio/spoofer.py [--mode unsigned] [--sybil 3] [--via-channel ws://<ip>:8766] [--iface <my-ip>]
"""
from __future__ import annotations
import argparse, asyncio, json, os, random, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pattern import place, from_enu             # noqa: E402
from radio import wire                          # noqa: E402
from radio.crypto import KeyPair, initial_seq                # noqa: E402

TX_ID = "SPOOF1"          # physical transmitter id (only the channel uses it)


class Spoofer:
    def __init__(self, a):
        self.a = a
        self.key = KeyPair()
        self.seq: dict[str, int] = {}
        self.ws = None
        self.transport = None
        lat, lon = (a.site_lat, a.site_lon) if a.site_lat is not None else from_enu(0.0, -2500.0)
        self.site = {"ctl": "SITE", "tx": TX_ID, "lat": lat, "lon": lon, "alt_ft": 1500}
        self.ids = [f"GHOST{7 + i}" for i in range(max(1, a.sybil))]
        self.captured: list[dict] = []
        self.rng = random.Random(a.seed)              # fixed seed: the same attack every run
        self.sent = 0

    async def up(self, frame: dict):
        if self.ws is not None:
            await self.ws.send(json.dumps(frame))
        else:
            self.transport.sendto(wire.dumps(frame), (self.dest, wire.UPLINK_PORT))

    def envelope(self, frm: str, msg: str, body: dict, sign: bool) -> dict:
        self.seq[frm] = self.seq.get(frm, initial_seq()) + 1
        env = {"msg": msg, "from": frm, "seq": self.seq[frm], "t": round(time.time(), 4), "body": body}
        env["sig"] = self.key.sign(env) if sign else ""
        return env

    async def tx(self, env: dict):
        await self.up({"ctl": "TX", "tx": TX_ID, "env": env})
        self.sent += 1

    def on_downlink(self, f: dict):
        if self.a.mode == "replay" and f.get("ctl") == "DELIVER" and f.get("env", {}).get("msg") == "STATE":
            self.captured.append(f["env"])
            now = time.time()
            self.captured = [e for e in self.captured if now - e["t"] < 6.0][-300:]

    async def start(self):
        if self.a.via_channel:
            import websockets
            self.ws = await websockets.connect(self.a.via_channel.rstrip("/") + f"/?id={TX_ID}")
            asyncio.ensure_future(self._ws_reader())
        else:
            sock, self.dest, mode = wire.open_udp(wire.DOWNLINK_PORT, wire.GROUP, self.a.iface)
            self.transport = await wire.udp_endpoint(sock, lambda f, addr: self.on_downlink(f))
        print(f"[spoofer] up: mode={self.a.mode} ids={self.ids} site=({self.site['lat']:.5f},{self.site['lon']:.5f})", flush=True)

    async def _ws_reader(self):
        async for raw in self.ws:
            try:
                self.on_downlink(json.loads(raw))
            except Exception:
                pass

    def ghost_body(self, i: int, k: int) -> dict:
        offset = (k * 1.0) % 45.0                                   # fly final 25L for 45 s, then repeat
        p = place("FINAL", "25L", offset_s=offset, gs_kt=70)
        lat, lon = p["lat"] + 0.0027 * i, p["lon"]                  # sybils 300 m apart
        body = {"lat": round(lat, 6), "lon": round(lon, 6), "alt_press_ft": round(p["alt_msl_ft"]), "gs_kt": 70,
                "track_deg": round(p["hdg_deg"], 1), "vs_fpm": -500, "leg": "FINAL", "intent": "LANDING_25L",
                "ap_equipped": False}
        if self.a.mode == "impossible":
            body.update(gs_kt=400, lat=round(lat + self.rng.choice([-1, 1]) * 0.009, 6))
        return body

    async def run(self):
        await self.start()
        k = 0
        while True:
            await self.up(self.site)
            mode = self.a.mode
            if mode == "unknown-key" and k % 3 == 0:
                for gid in self.ids:
                    await self.up({"ctl": "KEYS", "id": gid, "pub": self.key.pub_b64})
            if mode in ("unsigned", "unknown-key", "impossible"):
                for i, gid in enumerate(self.ids):
                    await self.tx(self.envelope(gid, "STATE", self.ghost_body(i, k), sign=(mode == "unknown-key")))
                if k % 2 == 0:
                    for gid in self.ids:
                        nb = {o: 1 for o in self.ids if o != gid}
                        await self.tx(self.envelope(gid, "HEARTBEAT", {"alive": True, **({"nb": nb} if nb else {})},
                                                    sign=(mode == "unknown-key")))
            elif mode == "impersonate":
                body = self.ghost_body(0, k)
                await self.tx(self.envelope(self.a.as_id, "STATE", body, sign=False))
            elif mode == "replay" and self.captured:
                env = dict(self.captured[-1])
                await self.tx(env)                                   # same seq again -> replay_seq
                forged = dict(env, body=dict(env["body"], lat=env["body"]["lat"] + 0.01))
                await self.tx(forged)                                # body changed, old sig -> bad_signature
                old = [e for e in self.captured if time.time() - e["t"] > 2.6]
                if old:
                    await self.tx(old[0])                            # stale -> time_window
            if k % 5 == 0:
                print(f"[spoofer] sent {self.sent} frames ({mode})", flush=True)
            k += 1
            await asyncio.sleep(1.0)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["unsigned", "unknown-key", "impossible", "impersonate", "replay"], default="unsigned")
    ap.add_argument("--sybil", type=int, default=1, help="number of mutually consistent fake ids (3 = known-limit demo)")
    ap.add_argument("--as", dest="as_id", default="N204", help="real id to impersonate (--mode impersonate)")
    ap.add_argument("--site-lat", type=float)
    ap.add_argument("--site-lon", type=float)
    ap.add_argument("--via-channel")
    ap.add_argument("--iface")
    ap.add_argument("--world", help="accepted for run_demo.sh compatibility; not used")
    ap.add_argument("--seed", type=int, default=7)
    try:
        asyncio.run(Spoofer(ap.parse_args()).run())
    except KeyboardInterrupt:
        pass
