"""
sources/sdr_beast.py - live 1090 MHz input from an RTL-SDR running readsb / dump1090-fa (RECEIVE ONLY).

readsb's Beast binary output (TCP 30005) carries every raw Mode S frame with a 12 MHz timestamp and a signal
level - exactly what the physical-layer checks need. This module turns that stream into ARC's normalized
sensor messages (verify/schema.py SensorMsg):
  DF4/5/20/21 (replies to ground radar)  -> MODES_REPLY   address recovered from the parity (CRC) field
  DF11 (acquisition squitter)            -> MODES_REPLY
  DF17/18 (ADS-B extended squitter)      -> ADSB_POSITION / ADSB_VELOCITY when pyModeS is installed
                                            (local position decoding against our own position), else
                                            MODES_REPLY df 17 with the raw frame
RSSI: dBFS from the Beast signal byte (relative, not dBm - calibrate per installation).
We never transmit. Frames with a bad CRC are dropped.

    python sources/sdr_beast.py --host 127.0.0.1 --port 30005 --lat 33.688 --lon -112.083      # print messages
Feeding a live unit from an SDR instead of the simulator is the next step (verify/unit.py takes the same
messages); see docs/data_sources.md.
"""
from __future__ import annotations
import argparse, math, socket, sys, time

ESC = 0x1A
LENGTHS = {0x31: 2, 0x32: 7, 0x33: 14}            # '1' Mode A/C, '2' Mode S short, '3' Mode S long
GENERATOR = 0xFFF409                             # Mode S CRC-24 polynomial

def crc24(data: bytes) -> int:
    """Mode S CRC over all bits of `data` (the frame without its 24 parity bits)."""
    c = 0
    for byte in data:
        c ^= byte << 16
        for _ in range(8):
            c = ((c << 1) ^ GENERATOR) & 0xFFFFFF if c & 0x800000 else (c << 1) & 0xFFFFFF
    return c

def parse_beast(buf: bytearray) -> tuple[list[tuple[int, float, bytes]], bytearray]:
    """Consume complete Beast frames from buf -> ([(timestamp_12mhz, signal 0..1, frame bytes)], rest)."""
    out, i, n = [], 0, len(buf)
    while i < n:
        if buf[i] != ESC:
            i += 1
            continue
        if i + 1 >= n:
            break
        typ = buf[i + 1]
        if typ not in LENGTHS:
            i += 1
            continue
        need, j, body = 6 + 1 + LENGTHS[typ], i + 2, bytearray()
        while len(body) < need and j < n:          # un-escape doubled 0x1A
            if buf[j] == ESC:
                if j + 1 >= n:
                    break
                if buf[j + 1] != ESC:              # a new frame started: this one is broken
                    body = None
                    break
                j += 1
            body.append(buf[j])
            j += 1
        if body is None:
            i = j
            continue
        if len(body) < need:
            break                                  # incomplete: wait for more bytes
        ts = int.from_bytes(body[:6], "big")
        sig = body[6] / 255.0
        out.append((ts, sig, bytes(body[7:])))
        i = j
    return out, buf[i:]

def frame_to_msgs(ts: int, sig: float, frame: bytes, t_ns: int, own: tuple[float, float] | None = None) -> list[dict]:
    """One Mode S frame -> ARC sensor messages (possibly none)."""
    if len(frame) not in (7, 14):
        return []
    df = frame[0] >> 3
    rssi = round(20 * math.log10(max(sig, 1e-4)), 1)          # dBFS
    raw = frame.hex()
    if df in (11, 17, 18):
        if crc24(frame[:-3]) != int.from_bytes(frame[-3:], "big") and df != 11:
            return []                                          # DF11 parity may be XORed with an interrogator id
        icao = frame[1:4].hex()
    elif df in (4, 5, 20, 21):
        icao = f"{crc24(frame[:-3]) ^ int.from_bytes(frame[-3:], 'big'):06x}"   # address/parity field
    else:
        return []
    base = {"t_ns": t_ns, "source": "sdr", "icao": icao, "rssi_dbm": rssi, "raw": raw}
    if df in (17, 18):
        dec = _decode_adsb(raw, own)
        if dec:
            return [dict(base, **dec)]
    return [dict(base, kind="MODES_REPLY", df=df)]

def _decode_adsb(raw: str, own) -> dict | None:
    """Position / velocity / callsign via pyModeS when available (optional dependency)."""
    try:
        import pyModeS as pms
    except ImportError:
        return None
    tc = pms.adsb.typecode(raw)
    if 9 <= tc <= 18 and own is not None:
        lat, lon = pms.adsb.position_with_ref(raw, own[0], own[1])
        return {"kind": "ADSB_POSITION", "lat": lat, "lon": lon, "alt_ft": pms.adsb.altitude(raw)}
    if tc == 19:
        v = pms.adsb.velocity(raw)
        if v and v[0] is not None:
            return {"kind": "ADSB_VELOCITY", "gs_kt": v[0], "trk_deg": v[1], "vs_fpm": v[2]}
    return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=30005)
    ap.add_argument("--lat", type=float, help="our position (local CPR decoding reference)")
    ap.add_argument("--lon", type=float)
    a = ap.parse_args()
    own = (a.lat, a.lon) if a.lat is not None else None
    s = socket.create_connection((a.host, a.port), timeout=10)
    buf = bytearray()
    print(f"[sdr] receiving Beast from {a.host}:{a.port} (receive only)", flush=True)
    while True:
        data = s.recv(65536)
        if not data:
            break
        buf += data
        frames, buf = parse_beast(buf)
        for ts, sig, fr in frames:
            for m in frame_to_msgs(ts, sig, fr, time.time_ns(), own):
                print(m, flush=True)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
