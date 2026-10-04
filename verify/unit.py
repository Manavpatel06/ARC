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
    def __init__(self, own_id: str, cfg: dict | None = None, log_path: str | None = None, sign: bool = False,
                 checks: dict | None = None):
        self.own_id = own_id
        self.cfg = cfg or load_config()
        self.checks = checks if checks is not None else CHECKS          # eval/ablation runs pass a subset
        self.tm = TrackManager(self.cfg["track_timeout_s"])
        self.t = 0.0
        self.t0 = None
        self.last_eval = -1e9
        self.states: dict[str, str] = {}               # last published state per target (alerts / event log)
        self.banners_prev: list[str] = []
        self.rate_base = None                          # normal message rate (jamming detection)
        self.sk = None
        self.log = None
        if sign or log_path:
            from verify.eventlog import EventLog, load_key
            self.sk = load_key(own_id)
            if log_path:
                self.log = EventLog(log_path, self.sk)
                self.log.append("UNIT_START", {"unit": own_id, "receive_only": True, "advisory_only": True}, 0.0)

    def ingest(self, frame: dict) -> None:
        """A SENSORS frame (or a bare list of sensor messages)."""
        msgs = frame.get("msgs", []) if isinstance(frame, dict) else frame
        for m in msgs:
            self.tm.ingest(m)
            self.t = max(self.t, m["t_ns"] / 1e9)
        if isinstance(frame, dict) and frame.get("t") is not None:
            self.t = max(self.t, float(frame["t"]))
        if self.t0 is None and self.t > 0:
            self.t0 = self.t

    def due(self) -> bool:
        return self.t - self.last_eval >= 1.0 / self.cfg["evaluate_hz"] - 1e-6

    # ---------- whole-picture guards ----------
    def _band_degraded(self, t: float) -> tuple[bool, str]:
        """Jamming / congestion on 1090: raised noise floor, or the message rate collapsing."""
        g, b = self.cfg["guards"], self.tm.band
        if not b or t - b.get("t", -1e9) > 5.0:
            return False, ""
        rate, noise = b.get("msgs_per_s") or 0.0, b.get("noise_dbm")
        if noise is not None and noise > g["jam_noise_dbm"]:
            return True, f"1090 noise floor {noise:.0f} dBm"
        if self.rate_base is not None and rate < g["jam_rate_drop"] * self.rate_base:
            return True, f"1090 message rate down to {rate:.0f}/s from {self.rate_base:.0f}/s"
        self.rate_base = rate if self.rate_base is None else 0.9 * self.rate_base + 0.1 * rate
        return False, ""

    def _own_uncertain(self, ctx: Ctx, tracks) -> tuple[bool, str]:
        """Own position doubtful: the integrity monitor says so, or several targets that TCAS DOES track all
        disagree with their ADS-B claims - more likely our GPS is wrong than all of them are spoofed."""
        own = self.tm.own
        if own.t > -1e8 and not own.gps_ok:
            return True, "GPS integrity monitor"
        bad = n = 0
        for tr in tracks:
            if tr.parent is not None or tr.branch is not None or not tr.has_adsb or                     not tr.last_tcas(ctx.t, self.cfg["tcas"]["fresh_s"]):
                continue
            r = CHECKS["tcas_consistency"](tr, ctx, self.cfg)
            if r.score is None:
                continue
            n += 1
            bad += r.score < 0.2                                            # a real mismatch, not "starting to"
        g = self.cfg["guards"]
        if bad >= g["own_tcas_mismatch_n"] and bad >= g["own_tcas_mismatch_frac"] * n:
            return True, f"{bad} of {n} TCAS-tracked aircraft disagree with their ADS-B"
        return False, ""

    def evaluate(self, t: float | None = None) -> dict:
        t = self.t if t is None else t
        self.last_eval = t
        self.tm.prune(t)
        own = self.tm.own
        stale = self.cfg["display_stale_s"]
        tracks = [tr for _, tr in sorted(self.tm.tracks.items())       # replies-only / gone quiet: not displayed
                  if (tr.has_adsb and t - tr.pos[-1][0] <= stale) or
                  (tr.parent is None and tr.last_tcas(t, self.cfg["tcas"]["fresh_s"]))]
        ctx = Ctx(t=t, own=own, radar_t=self.tm.radar_t, tcas_t=self.tm.tcas_t, tm=self.tm, t0=self.t0 or t)
        ctx.band_degraded, band_why = self._band_degraded(t)
        ctx.own_uncertain, own_why = self._own_uncertain(ctx, tracks)
        targets = []
        for tr in tracks:
            results = {name: fn(tr, ctx, self.cfg) for name, fn in self.checks.items()}
            out = fuse(tr, results, self.cfg)
            targets.append(self._target(tr, results, out, t))
        banners = []
        if ctx.band_degraded:
            banners.append(f"TRAFFIC PICTURE DEGRADED - increase lookout, notify ATC ({band_why})")
        if ctx.own_uncertain:
            banners.append(f"OWN POSITION UNCERTAIN - position checks widened ({own_why})")
        if own.t > -1e8 and not own.tcas_ok:
            banners.append("own TCAS unavailable - fewer checks, more targets UNVERIFIED")
        frame = {"type": "VERIFY", "ac_id": self.own_id, "t": round(t, 3), "targets": targets, "banners": banners,
                 "alerts": self._alerts(targets, banners, t)}
        return frame

    def _alerts(self, targets: list, banners: list, t: float) -> list[dict]:
        """One alert per change worth telling the pilot (new SUSPECT, banner on); every change goes to the log."""
        alerts = []
        for x in targets:
            prev = self.states.get(x["icao"])
            if prev != x["state"]:
                self.states[x["icao"]] = x["state"]
                if self.log:
                    self.log.append("TARGET_STATE", {"icao": x["icao"], "id": x["id"], "from": prev, "to": x["state"],
                                                     "trust": x["trust"], "reasons": x["reasons"]}, t)
                if x["state"] == "SUSPECT":
                    alerts.append({"text": f"SUSPECT TRAFFIC {x['id']}: {x['reasons'][0] if x['reasons'] else 'likely spoofed'}",
                                   "speak": f"suspect traffic, {x['id']}, likely spoofed"})
        for b in banners:
            if b not in self.banners_prev:
                key = b.split(" - ")[0]
                alerts.append({"text": b, "speak": key.lower()})
                if self.log:
                    self.log.append("BANNER_ON", {"text": b}, t)
        for b in self.banners_prev:
            if b not in banners and self.log:
                self.log.append("BANNER_OFF", {"text": b}, t)
        self.banners_prev = banners
        return alerts

    def signed(self, frame: dict) -> dict:
        if self.sk is None:
            return frame
        from verify.eventlog import sign_frame
        return sign_frame(frame, self.sk)

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
        name = tr.callsign or tr.address.upper()
        if tr.parent is not None:
            name += " (2)"                                  # the second place its address is heard from
        sources = sources if tr.parent is None else [s for s in sources if s == "ADS-B"]
        return {"icao": tr.icao, "id": name, "callsign": tr.callsign,
                "trust": out["trust"], "state": out["state"], "reasons": out["reasons"],
                "checks": {n: {"score": None if r.score is None else round(r.score, 2), "confidence": round(r.confidence, 2),
                               "reason": r.reason, "contribution": round(out["contrib"].get(n, 0.0), 2)}
                           for n, r in results.items()},
                "rel": rel, "tcas_confirmed": out["tcas_confirmed"], "sources": sources}

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True, help="own aircraft id (a judge aircraft)")
    ap.add_argument("--world", default="ws://localhost:8765")
    ap.add_argument("--log", default=None, help="signed, hash-chained event log (default harness/out/verify/<id>_events.jsonl)")
    a = ap.parse_args()
    import websockets
    log = a.log or os.path.join(ROOT, "harness", "out", "verify", f"{a.id}_events.jsonl")
    unit = VerifyUnit(a.id, log_path=log, sign=True)
    print(f"[verify {a.id}] event log {log} (verify: python -m verify.eventlog {log})", flush=True)
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
                        await ws.send(json.dumps(unit.signed(out), separators=(",", ":")))
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
