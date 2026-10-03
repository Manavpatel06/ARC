"""
radio/wire.py — transport plumbing shared by client.py, channel.py, spoofer.py, faults.py.

Two hops, so the channel (the "air") can decide what each receiver hears:
  UPLINK   node -> channel : UDP multicast 239.1.1.1:5005   (or WebSocket ws://<channel-ip>:8766/?id=<id>)
  DOWNLINK channel -> node : UDP multicast 239.1.1.1:5006   (or the same WebSocket)
`--direct` mode (no channel process, no loss): nodes send and listen on 5005 like the loopback stub.

Frames are JSON. Radio envelopes (INTERFACE.md §2) travel inside control frames:
  uplink   {"ctl":"TX","tx":"<physical radio id>","env":{envelope}}
           {"ctl":"KEYS","id":"N101","pub":"<b64>"}          public key at boot
           {"ctl":"HELLO","id":"N101"}                        "I am a receiver"
           {"ctl":"REJECT"|"LINK", ...}                       mirrored to the world log
           {"ctl":"SITE","tx":"SPOOF1","lat","lon","alt_ft"}  physical antenna of a non-world transmitter
           {"ctl":"FAULT", ...}                               radio/faults.py
  downlink {"ctl":"DELIVER","to":"N204","rf":{"rssi_dbm","doppler_hz"},"env":{envelope}}
           {"ctl":"KEYS","keys":{"N101":"<b64>", ...}}       registrar's pinned keys
           {"ctl":"GARBLE","to":"N204","slot":7,"frame":123}  energy heard, nothing decodable (collision)
`tx` is the PHYSICAL transmitter, used only by the channel for propagation — a spoofer can claim
any `from`, but its signal still comes from where its antenna really is.
"""
from __future__ import annotations
import asyncio, json, socket, struct
from typing import Callable, Optional

GROUP = "239.1.1.1"
UPLINK_PORT = 5005
DOWNLINK_PORT = 5006
CHANNEL_WS_PORT = 8766


def dumps(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode()


def open_udp(port: int, group: str = GROUP, iface: Optional[str] = None, listen: bool = True):
    """Multicast socket bound to `port` and joined to `group`. Falls back to localhost broadcast if the
    OS refuses multicast. Returns (sock, dest_for_that_port_family, mode)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    try:
        sock.bind(("", port if listen else 0))
        if_addr = socket.inet_aton(iface) if iface else struct.pack("=I", socket.INADDR_ANY)
        if listen:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(group) + if_addr)
        if iface:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        mode = "multicast"
        bcast = group
    except OSError:
        sock.close()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", port if listen else 0))
        mode = "broadcast-localhost"
        bcast = "127.255.255.255"
    sock.setblocking(False)
    return sock, bcast, mode


class _Proto(asyncio.DatagramProtocol):
    def __init__(self, cb: Callable[[dict, tuple], None]):
        self.cb = cb

    def datagram_received(self, data, addr):
        try:
            frame = json.loads(data)
        except Exception:
            return
        if isinstance(frame, dict):
            self.cb(frame, addr)


async def udp_endpoint(sock, cb: Callable[[dict, tuple], None]):
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(lambda: _Proto(cb), sock=sock)
    return transport


def ws_path(ws) -> str:
    """Request path for websockets >= 13 (ws.request.path) and 12 (ws.path)."""
    req = getattr(ws, "request", None)
    return getattr(req, "path", None) or getattr(ws, "path", "") or ""
