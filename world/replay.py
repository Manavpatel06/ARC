"""
world/replay.py - REAL recorded traffic around KDVT as transmitters in the simulated radio environment.

data/live_traffic.py --record saves LIVE_TRAFFIC frames (real ADS-B from airplanes.live / adsb.lol /
OpenSky, one frame per line) to harness/out/live_*.jsonl. ReplaySource plays one back on the world clock:
every recorded aircraft becomes a real, transponder-equipped emitter (label "real") at its recorded,
interpolated position - so the onboard unit hears it on ADS-B, its TCAS tracks it, the ground radar gets
replies from it. Demo step 1: "show replayed real traffic - everything green".

    scenario: "replay": "harness/out/live_1791054736.jsonl"      or  world_server.py --replay <file>
Recordings are not committed (feed terms - see docs/data_sources.md); record your own on the venue Wi-Fi.
"""
from __future__ import annotations
import bisect
import json

from world.attacks import Emitter

class ReplaySource:
    def __init__(self, path: str | list, start_t: float, loop: bool = True, max_nm: float = 15.0):
        frames = path if isinstance(path, list) else [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
        frames = [f for f in frames if f.get("type") == "LIVE_TRAFFIC" and f.get("aircraft") is not None]
        if not frames:
            raise ValueError("recording has no LIVE_TRAFFIC frames")
        self.t_rec0 = float(frames[0]["t"])
        self.span = max(1.0, float(frames[-1]["t"]) - self.t_rec0)
        self.start_t, self.loop = start_t, loop
        self.tracks: dict[str, list] = {}                      # id -> [(rec_t, lat, lon, alt, gs, trk, vs, on_ground, callsign)]
        for f in frames:
            for a in f["aircraft"]:
                if a.get("dist_nm", 0) > max_nm or a.get("lat") is None:
                    continue
                self.tracks.setdefault(str(a["id"]).lower(), []).append(
                    (float(f["t"]) - float(a.get("age_s", 0) or 0), a["lat"], a["lon"], float(a.get("alt_msl_ft") or 0),
                     float(a.get("gs_kt") or 0), float(a.get("track_deg") or 0), float(a.get("vs_fpm") or 0),
                     bool(a.get("on_ground")), (a.get("callsign") or "").strip() or None))
        for v in self.tracks.values():
            v.sort(key=lambda x: x[0])

    def rec_time(self, now: float) -> float | None:
        dt = now - self.start_t
        if dt < 0:
            return None
        if self.loop:
            dt %= self.span
        elif dt > self.span:
            return None
        return self.t_rec0 + dt

    def emitters(self, now: float) -> list[Emitter]:
        rt = self.rec_time(now)
        if rt is None:
            return []
        out = []
        for icao, s in self.tracks.items():
            i = bisect.bisect_left([x[0] for x in s], rt)
            if i == 0 and s[0][0] - rt > 6 or i >= len(s) and rt - s[-1][0] > 6:
                continue                                      # not in the air (on this recording) right now
            a, b = (s[max(0, i - 1)], s[min(i, len(s) - 1)])
            f = 0.0 if b[0] == a[0] else max(0.0, min(1.0, (rt - a[0]) / (b[0] - a[0])))
            lerp = lambda k: a[k] + (b[k] - a[k]) * f
            dtrk = ((b[5] - a[5] + 540) % 360) - 180
            lat, lon, alt = lerp(1), lerp(2), lerp(3)
            out.append(Emitter(icao[-6:].rjust(6, "0"), b[8] or icao.upper(), lat, lon, alt, lerp(4), (a[5] + dtrk * f) % 360,
                               lerp(6), lat, lon, alt, transponder=True, label="real", ac_id=None))
        return out
