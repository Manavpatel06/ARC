"""sources/sdr_beast.py: Beast framing, Mode S CRC, address recovery (receive-only SDR input)."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sources.sdr_beast import crc24, frame_to_msgs, parse_beast   # noqa: E402

DF17 = bytes.fromhex("8D4840D6202CC371C32CE0576098")              # a published ADS-B identification frame (ICAO 4840D6)

def beast(frame: bytes, ts: int = 0x0102031A0405, sig: int = 0x80) -> bytes:
    typ = {7: 0x32, 14: 0x33}[len(frame)]
    body = ts.to_bytes(6, "big") + bytes([sig]) + frame
    return bytes([0x1A, typ]) + body.replace(b"\x1a", b"\x1a\x1a")      # 0x1A inside the frame is doubled

def test_crc_of_a_valid_df17_is_its_parity():
    assert crc24(DF17[:-3]) == int.from_bytes(DF17[-3:], "big")

def test_beast_stream_with_escapes_and_a_partial_frame():
    df4 = bytes([0x20, 0x00, 0x17, 0x18])
    addr = 0xABCDEF
    df4 += (crc24(df4) ^ addr).to_bytes(3, "big")
    stream = bytearray(b"\x00junk" + beast(DF17) + beast(df4, sig=0x1A) + beast(DF17)[:9])
    frames, rest = parse_beast(stream)
    assert [f[2] for f in frames] == [DF17, df4] and frames[0][0] == 0x0102031A0405
    assert len(rest) == 9                                              # the incomplete frame waits for more bytes
    m17 = frame_to_msgs(*frames[0], t_ns=1)
    m4 = frame_to_msgs(*frames[1], t_ns=1)
    assert m17[0]["icao"] == "4840d6" and m17[0]["kind"] in ("MODES_REPLY", "ADSB_POSITION", "ADSB_VELOCITY")
    assert m4 == [dict(m4[0], kind="MODES_REPLY", df=4, icao="abcdef")]  # address recovered from the parity field
    assert m4[0]["rssi_dbm"] < 0                                       # dBFS

def test_corrupted_df17_is_dropped():
    bad = bytearray(DF17); bad[5] ^= 0x40
    assert frame_to_msgs(0, 0.5, bytes(bad), t_ns=1) == []
