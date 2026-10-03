"""
radio/watch.py — live, readable view of all FLOCK communication in your terminal (Lane C).
Connects to the world as role `log` and prints every radio packet (delivered / dropped + reason), every
rejection, lost/restored link, fault, trust change and advisory — colour-coded, one line each — plus a
summary line every 10 s. Reconnects by itself when the world restarts.

    python radio/watch.py --world ws://192.168.137.1:8765
    python radio/watch.py --world ws://192.168.137.1:8765 --only N101        # one aircraft (sent or received)
    python radio/watch.py --world ws://192.168.137.1:8765 --no-state         # hide routine STATE/HEARTBEAT
    python radio/watch.py --world ws://192.168.137.1:8765 --drops            # only drops, rejections, faults
Radio lines appear only while radio/channel.py is running against that world (it mirrors the air to the log).
"""
from __future__ import annotations
import argparse, asyncio, collections, json, os, sys, time

try:
    import websockets
except ImportError:
    sys.exit("websockets missing: pip install -r requirements.txt")

USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
C = {"dim": "2", "red": "31", "green": "32", "yellow": "33", "blue": "34", "magenta": "35", "cyan": "36", "bold": "1"}


def col(s, *names):
    if not USE_COLOR:
        return s
    return "".join(f"\033[{C[n]}m" for n in names) + s + "\033[0m"


MSG_COLOR = {"STATE": "dim", "HEARTBEAT": "dim", "INTENT": "cyan", "SEQ_PROPOSE": "magenta",
             "SEQ_ACCEPT": "magenta", "MANEUVER_COMMIT": "yellow", "SIGHTING": "blue"}
LEVEL_COLOR = {"SEQUENCE": "cyan", "TRAFFIC": "yellow", "RESOLVE": "red", "TAKEOVER": "red",
               "RELEASE": "green", "NO_SOLUTION": "red", "CLEAR": "green"}
TRUST_COLOR = {"TRUSTED": "green", "SUSPICIOUS": "yellow", "FAKE": "red", "CAMERA_ONLY": "blue"}
REJECT_WORDS = ("sig", "seq", "replay", "time", "window", "schema", "key", "unsigned")


def body_text(msg, b):
    if msg == "STATE":
        return f"{b.get('leg', '')} {b.get('alt_press_ft', 0):.0f} ft {b.get('gs_kt', 0):.0f} kt trk {b.get('track_deg', 0):.0f}"
    if msg == "HEARTBEAT":
        nb = b.get("nb")
        heard = ",".join(k + ("✓" if v else "✗") for k, v in (nb or {}).items())
        return "alive" + (" hears " + heard if heard else "")
    if msg == "MANEUVER_COMMIT":
        return f"commits {b.get('sense')} {b.get('bank_deg', 0):.0f}° vs {b.get('target')} hold {b.get('hold_s', 0):.0f} s"
    if msg == "INTENT":
        return f"{b.get('leg')} → {b.get('intent')}"
    if msg == "SEQ_PROPOSE":
        return f"{b.get('runway')} order {' → '.join(b.get('order', []))}"
    if msg == "SEQ_ACCEPT":
        return f"accepts #{b.get('proposal_seq')}"
    return json.dumps(b)[:80]


class Watch:
    def __init__(self, a):
        self.a = a
        self.st = collections.Counter()
        self.trust: dict[tuple, str] = {}
        self.lat: list[float] = []

    def keep(self, ids) -> bool:
        return not self.a.only or self.a.only in ids

    def line(self, t, tag, color, text):
        ts = time.strftime("%H:%M:%S", time.localtime(t or time.time()))
        print(f"{col(ts, 'dim')} {col(f'{tag:<9}', color, 'bold')} {text}", flush=True)

    def radio(self, p):
        frm, to = p.get("from") or "?", p.get("to") or "*"
        if p.get("type") == "LINK":
            if self.keep((frm, to)):
                ok = p.get("state") == "RESTORED"
                self.line(p.get("t"), "LINK", "green" if ok else "red",
                          f"{to} {'regained' if ok else 'LOST'} link to {frm} (silent {p.get('age_s', 0):.1f} s)")
            self.st["link_" + str(p.get("state")).lower()] += 1
            return
        if p.get("type") == "FAULT":
            self.line(p.get("t"), "FAULT", "magenta", f"injected {json.dumps(p.get('fault', {}))[:110]}")
            return
        if p.get("type") == "KEY_REFUSED":
            self.line(p.get("t"), "KEY", "red", f"registrar refused key for {frm}: {p.get('reason')}")
            return
        msg = p.get("msg") or "?"
        if not self.keep((frm, to)):
            return
        delivered = p.get("delivered")
        reason = str(p.get("reason") or "")
        if delivered:
            self.st["delivered"] += 1
            if isinstance(p.get("latency_s"), (int, float)):
                self.lat.append(p["latency_s"])
            if self.a.drops or (self.a.no_state and msg in ("STATE", "HEARTBEAT")):
                return
            extra = f"{p.get('rng_m', 0) / 1852:.1f} NM {p.get('latency_s', 0) * 1000:.0f} ms"
            if p.get("rssi_dbm") is not None:
                extra += f" {p['rssi_dbm']:.0f} dBm"
            self.line(p.get("t"), msg[:9], MSG_COLOR.get(msg, "dim"),
                      f"{frm} → {to}  #{p.get('seq')}  {body_text(msg, p.get('body') or {})}  {col(extra, 'dim')}")
            return
        rejected = p.get("rejected_by") or any(w in reason for w in REJECT_WORDS)
        kind = "REJECTED" if rejected else "DROPPED"
        self.st[kind.lower()] += 1
        self.st["why:" + reason.split("(")[0]] += 1
        if self.a.no_state and not rejected and msg in ("STATE", "HEARTBEAT") and reason.startswith(("range", "loss")):
            return
        color = "red" if rejected or reason.startswith(("fault", "collision")) else "yellow"
        where = f" by {p['rejected_by']}" if p.get("rejected_by") else f" → {to}"
        self.line(p.get("t"), kind, color, f"{msg} from {frm}{where}  #{p.get('seq')}  {col(reason, color)}")

    def decision(self, src, p):
        if p.get("type") == "TRUST":
            for t in p.get("targets", []):
                key = (p.get("ac_id"), t["id"])
                if self.trust.get(key) != t["state"] and self.keep(key):
                    ev = ", ".join(e for e in t.get("evidence", []) if e not in ("signed", "plausible", "rf_consistent"))
                    self.line(p.get("t"), "TRUST", TRUST_COLOR.get(t["state"], "dim"),
                              f"{p.get('ac_id')} sees {t['id']} {col(t['state'], TRUST_COLOR.get(t['state'], 'dim'))} "
                              f"{t.get('score', 0):.2f}" + (f"  ({ev})" if ev else ""))
                self.trust[key] = t["state"]
        elif p.get("type") == "ADVISORY" and self.keep((p.get("ac_id"), p.get("target_id"))):
            lvl = p.get("level", "")
            self.line(p.get("t"), lvl[:9], LEVEL_COLOR.get(lvl, "dim"), f"{p.get('ac_id')}: {p.get('text')}"
                      + (f"  ttc {p['ttc_s']:.0f} s" if isinstance(p.get("ttc_s"), (int, float)) else ""))
        elif p.get("type") == "COMMAND" and self.keep((p.get("ac_id"),)):
            self.line(p.get("t"), p.get("mode", "COMMAND")[:9], "red" if p.get("mode") == "TAKEOVER" else "green",
                      f"{p.get('ac_id')}: bank {p.get('bank_cmd_deg', 0):.0f}° vs {p.get('vs_cmd_fpm', 0):.0f} fpm "
                      f"hold {p.get('hold_s', 0):.0f} s applied={p.get('applied')}")

    def summary(self):
        s = self.st
        drops = {k[4:]: v for k, v in s.items() if k.startswith("why:")}
        lat = sorted(self.lat[-500:])
        med = f"{lat[len(lat) // 2] * 1000:.0f} ms" if lat else "–"
        txt = (f"delivered {s['delivered']}  dropped {s['dropped']}  rejected {s['rejected']}  "
               f"links lost {s['link_lost']}  median latency {med}  reasons {dict(drops) if drops else '–'}")
        print(col(f"── {time.strftime('%H:%M:%S')}  {txt}", "blue"), flush=True)

    async def run(self):
        url = f"{self.a.world}?role=log"
        last_sum = time.time()
        while True:
            try:
                async with websockets.connect(url, max_size=None) as ws:
                    print(col(f"connected to {self.a.world} — waiting for traffic (Ctrl+C to stop)", "green"), flush=True)
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            raw = None
                        if raw:
                            try:
                                m = json.loads(raw)
                            except Exception:
                                continue
                            kind, p = m.get("kind"), m.get("payload") or {}
                            if kind == "radio":
                                self.radio(p)
                            elif kind == "decision" and not self.a.drops:
                                self.decision(m.get("src"), p)
                            elif m.get("type") == "HELLO" or p.get("type") == "HELLO":
                                h = p if p.get("type") == "HELLO" else m
                                ids = [x.get("id") for x in h.get("aircraft", [])]
                                self.line(time.time(), "WORLD", "green", f"scenario {h.get('scenario', '?')}  aircraft {' '.join(ids)}")
                            elif p.get("type") == "STICK":
                                self.line(p.get("t"), "STICK", "green", f"{p.get('ac_id')}: pilot took the controls back")
                        if time.time() - last_sum >= 10:
                            last_sum = time.time()
                            self.summary()
            except (OSError, websockets.exceptions.WebSocketException) as e:
                print(col(f"world not reachable ({e.__class__.__name__}); retrying in 3 s — check the IP with nc -vz", "red"), flush=True)
                await asyncio.sleep(3)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Live view of all FLOCK communication")
    ap.add_argument("--world", default="ws://localhost:8765")
    ap.add_argument("--only", help="show only lines involving this aircraft id")
    ap.add_argument("--no-state", action="store_true", help="hide routine delivered STATE/HEARTBEAT and range/loss drops")
    ap.add_argument("--drops", action="store_true", help="only drops, rejections, link and fault events")
    try:
        asyncio.run(Watch(ap.parse_args()).run())
    except KeyboardInterrupt:
        print()
