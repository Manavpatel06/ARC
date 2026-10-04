"""
stubs/loopback_radio.py — stand-in for Lane C's radio/client.py so Lane B can code the
node against the FINAL radio API on day one. No signing, no channel, no loss: every
message reaches every other process on the same machine/LAN via UDP multicast
(239.1.1.1:5005); falls back to UDP broadcast on 127.0.0.1 if multicast is refused.

API (this is the contract Lane C implements in radio/client.py — same names, same types):

    radio = RadioClient(ac_id="N101")             # optional: mcast_group, port, via_channel
    await radio.start()
    radio.on_message(callback)                    # callback(msg: RadioMsg-like dict) -> None
    await radio.send("STATE", {"lat":..., ...})   # wraps envelope {msg,from,seq,t,sig,body}
    await radio.stop()

Delivered dict shape = docs/interface.md envelope, validated with schemas.RadioMsg:
    {"msg":"STATE","from":"N204","seq":17,"t":...,"sig":"","body":{...}}
Own messages are NOT delivered back to the sender.

Run a two-node smoke test:
    python stubs/loopback_radio.py --id N101 &  python stubs/loopback_radio.py --id N204
"""
from __future__ import annotations
import argparse, asyncio, json, socket, struct, sys, time
from typing import Callable, Optional

sys.path.insert(0, __file__.rsplit("/stubs/", 1)[0]) if "/stubs/" in __file__ else None
try:
    from schemas import RadioMsg
except Exception:                      # schemas.py must be importable from repo root
    RadioMsg = None

MCAST_GROUP, MCAST_PORT = "239.1.1.1", 5005

class _Proto(asyncio.DatagramProtocol):
    def __init__(self, owner): self.owner = owner
    def datagram_received(self, data, addr): self.owner._rx(data)

class RadioClient:
    def __init__(self, ac_id: str, mcast_group: str = MCAST_GROUP, port: int = MCAST_PORT, via_channel: Optional[str] = None):
        self.ac_id = ac_id
        self.group, self.port = mcast_group, port
        self.via_channel = via_channel          # unused in the stub; Lane C: ws URL of channel.py
        self.seq = 0
        self._cb: list[Callable[[dict], None]] = []
        self._transport = None
        self._mode = "multicast"
        self.stats = {"tx": 0, "rx": 0, "rejected": 0}

    def on_message(self, cb: Callable[[dict], None]):
        self._cb.append(cb)

    async def start(self):
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        try:
            sock.bind(("", self.port))
            mreq = struct.pack("4sl", socket.inet_aton(self.group), socket.INADDR_ANY)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            self._dest = (self.group, self.port)
        except OSError:
            # hotspot / Wi-Fi blocking multicast: localhost broadcast fallback
            sock.close()
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind(("", self.port))
            self._dest = ("127.255.255.255", self.port)
            self._mode = "broadcast-localhost"
        sock.setblocking(False)
        self._transport, _ = await loop.create_datagram_endpoint(lambda: _Proto(self), sock=sock)
        print(f"[radio {self.ac_id}] up ({self._mode}) {self._dest}")

    async def send(self, msg: str, body: dict):
        self.seq += 1
        env = {"msg": msg, "from": self.ac_id, "seq": self.seq, "t": time.time(), "sig": "", "body": body}
        if RadioMsg:
            RadioMsg.model_validate(env)             # fail loudly on a bad body
        self._transport.sendto(json.dumps(env).encode(), self._dest)
        self.stats["tx"] += 1
        return env

    def _rx(self, data: bytes):
        try:
            env = json.loads(data)
            if RadioMsg:
                RadioMsg.model_validate(env)
        except Exception:
            self.stats["rejected"] += 1
            return
        if env.get("from") == self.ac_id:
            return
        self.stats["rx"] += 1
        for cb in self._cb:
            try:
                cb(env)
            except Exception as e:
                print(f"[radio {self.ac_id}] callback error: {e}")

    async def stop(self):
        if self._transport:
            self._transport.close()

async def _smoke(ac_id: str):
    r = RadioClient(ac_id)
    r.on_message(lambda m: print(f"[radio {ac_id}] <- {m['msg']} from {m['from']} seq {m['seq']}"))
    await r.start()
    while True:
        await r.send("STATE", {"lat": 33.69, "lon": -112.08, "alt_press_ft": 2490, "gs_kt": 90, "track_deg": 70, "vs_fpm": 0, "leg": "DOWNWIND", "intent": "BASE_IN_20S", "ap_equipped": True})
        await asyncio.sleep(1)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--id", default="N101")
    asyncio.run(_smoke(ap.parse_args().id))
