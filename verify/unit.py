"""
verify/unit.py - the FLOCK onboard unit: receive only, advisory only.

    python verify/unit.py --id N101 [--world ws://localhost:8765]

Connects to the world as role avionics:<id> and receives ONLY what this aircraft's own equipment would:
ADS-B it hears, its own TCAS tracks, Mode S replies, 1030 interrogations, own-ship state, band stats
(SENSORS frames). Once a second it runs every check on every target, fuses them, applies the guardrails and
sends one VERIFY frame (trust 0-100, VERIFIED / UNVERIFIED / SUSPECT, reasons) back for the cockpit
display. It never sends anything else: no commands, no maneuvers, no transmissions.

VerifyUnit is the pure, clock-free core (time = the frames' timestamps) used by tests and eval too.
"""
from __future__ import annotations
import argparse, asyncio, json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from verify import load_config                                     # noqa: E402
from verify.checks import CHECKS, Ctx                              # noqa: E402
from verify.fusion import fuse                                     # noqa: E402
from verify.tracks import TrackManager, rng_brg                    # noqa: E402

class VerifyUnit:
    def __init__(self, own_id: str, cfg: dict | None = None):
        self.own_id = own_id
        self.cfg = cfg or load_config()
        self.tm = TrackManager(self.cfg["track_timeout_s"])
        self.t = 0.0
        self.last_eval = -1e9

    def ingest(self, frame: dict) -> None:
        """A SENSORS frame (or a bare list of sensor messages)."""
        msgs = frame.get("msgs", []) if isinstance(frame, dict) else frame
        for m in msgs:
            self.tm.ingest(m)
            self.t = max(self.t, m["t_ns"] / 1e9)
        if isinstance(frame, dict) and frame.get("t") is not None:
            self.t = max(self.t, float(frame["t"]))

    def due(self) -> bool:
        return self.t - self.last_eval >= 1.0 / self.cfg["evaluate_hz"] - 1e-6

    def evaluate(self, t: float | None = None) -> dict:
        t = self.t if t is None else t
        self.last_eval = t
        self.tm.prune(t)
        own = self.tm.own
        ctx = Ctx(t=t, own=own, radar_t=self.tm.radar_t, tcas_t=self.tm.tcas_t)
        targets = []
        for icao, tr in sorted(self.tm.tracks.items()):
            if not tr.has_adsb and not tr.last_tcas(t, self.cfg["tcas"]["fresh_s"]):
                continue                        # replies only (no ADS-B, no TCAS): nothing to put on a display
            results = {name: fn(tr, ctx, self.cfg) for name, fn in CHECKS.items()}
            out = fuse(tr, results, self.cfg)
            targets.append(self._target(tr, results, out, t))
        banners = []
        if own.t > -1e8 and not own.tcas_ok:
            banners.append("own TCAS unavailable - fewer checks, more targets UNVERIFIED")
        return {"type": "VERIFY", "ac_id": self.own_id, "t": round(t, 3), "targets": targets, "banners": banners}

    def _target(self, tr, results: dict, out: dict, t: float) -> dict:
        own = self.tm.own
        rel = None
        claim = tr.claimed_at(t)
        if own.t > -1e8 and claim is not None:
            rng, brg = rng_brg(own.lat, own.lon, claim[0], claim[1])
            vel = tr.vel[-1] if tr.vel else (None, None, None, None)
            rel = {"brg_deg": round(brg, 1), "rng_m": round(rng, 1),
                   "dalt_ft": round((claim[2] or own.alt_ft) - own.alt_ft, 0),
                   "trk_deg": vel[2], "vs_fpm": vel[3]}
        elif own.t > -1e8 and tr.tcas:                                   # TCAS-only target
            _, rng, brg, alt = tr.tcas[-1]
            rel = {"brg_deg": round(brg, 1), "rng_m": round(rng, 1),
                   "dalt_ft": round((alt if alt is not None else own.alt_ft) - own.alt_ft, 0)}
        sources = (["ADS-B"] if tr.has_adsb else []) + (["TCAS"] if tr.last_tcas(t, 5.0) else []) + \
                  (["Mode S"] if any(t - r[0] <= 20 for r in tr.replies) else [])
        return {"icao": tr.icao, "id": tr.callsign or tr.icao.upper(), "callsign": tr.callsign,
                "trust": out["trust"], "state": out["state"], "reasons": out["reasons"],
                "checks": {n: {"score": None if r.score is None else round(r.score, 2), "confidence": round(r.confidence, 2),
                               "reason": r.reason, "contribution": round(out["contrib"].get(n, 0.0), 2)}
                           for n, r in results.items()},
                "rel": rel, "tcas_confirmed": out["tcas_confirmed"], "sources": sources}

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True, help="own aircraft id (a judge aircraft)")
    ap.add_argument("--world", default="ws://localhost:8765")
    a = ap.parse_args()
    import websockets
    unit = VerifyUnit(a.id)
    url = f"{a.world}/?role=avionics:{a.id}"
    while True:
        try:
            async with websockets.connect(url, max_size=None) as ws:
                print(f"[verify {a.id}] connected {url} (receive only)", flush=True)
                n = 0
                async for raw in ws:
                    m = json.loads(raw)
                    if m.get("type") == "ERROR":
                        print(f"[verify {a.id}] world refused: {m.get('error')}", flush=True)
                        return
                    if m.get("type") != "SENSORS":
                        continue
                    unit.ingest(m)
                    if unit.due():
                        out = unit.evaluate()
                        await ws.send(json.dumps(out, separators=(",", ":")))
                        n += 1
                        if n % 10 == 1:
                            summary = ", ".join(f"{x['id']}:{x['state'][0]}{x['trust']}" for x in out["targets"])
                            print(f"[verify {a.id}] t={out['t']:.0f} {summary or 'no traffic'}", flush=True)
        except (OSError, Exception) as e:                # world restarting: retry
            print(f"[verify {a.id}] link down ({e}); retry in 2 s", flush=True)
            await asyncio.sleep(2)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
