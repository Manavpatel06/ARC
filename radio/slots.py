"""
radio/slots.py — AIS-style self-organizing TDMA ("avoiding clash of signals"). Lane C, task C7.

Frame = 1 s, split into 20 slots of 50 ms. A packet occupies AIRTIME_S = 30 ms at the start of
its slot (20 ms guard for clock skew, like AIS guard time). Slot 19 is a reserved BURST slot for
event-driven INTENT / SEQ_* / MANEUVER_COMMIT; slots 0–18 carry STATE + HEARTBEAT.

Self-organization (SOTDMA-lite):
  1. Node starts on slot = sha256(id) % 19 (stable across processes; Python's hash() is not).
  2. It LISTENS one full frame before its first transmission and builds an occupancy map from
     every packet it decodes (slot = claimed tx time `t` mod 1 s) plus slots it hears GARBLED
     (energy but no decode = somebody collided there).
  3. If its slot is taken by a lower id it moves to a random free slot. If the occupant has a
     higher id, that node moves (deterministic tie-break, so they don't both jump).
  4. Slot timeout: every 8–15 frames the node re-selects a random free slot (AIS does the same).
     This is what eventually separates two nodes that collided and therefore never heard each
     other (half-duplex radios cannot hear their own slot).
Slot timing comes from the node's clock; pass the world clock (`OWNSHIP.t`, i.e. GPS time — AIS
syncs slots to GPS UTC the same way).

Capacity is honest: 19 STATE slots at 1 Hz = 19 aircraft in mutual range. 30 aircraft need 40
slots (25 ms) or a lower STATE rate for far aircraft. The metric below prints both.

Metric (one number for the slide):
    python radio/slots.py                 # collision rate at 8 and 30 senders, unslotted vs slotted
    python radio/slots.py --n 30 --slots 40
"""
from __future__ import annotations
import argparse, hashlib, math, random, time
from typing import Optional

N_SLOTS = 20
FRAME_S = 1.0
AIRTIME_FRAC = 0.6          # 30 ms of a 50 ms slot
TIMEOUT_FRAMES = (8, 15)
URGENT_MSGS = {"INTENT", "SEQ_PROPOSE", "SEQ_ACCEPT", "MANEUVER_COMMIT"}


def stable_hash(s: str) -> int:
    return int.from_bytes(hashlib.sha256(s.encode()).digest()[:4], "big")


class SlotScheduler:
    def __init__(self, ac_id: str, n_slots: int = N_SLOTS, frame_s: float = FRAME_S,
                 burst: bool = True, clock=time.time, seed: Optional[int] = None):
        self.ac_id = ac_id
        self.n_slots, self.frame_s = n_slots, frame_s
        self.slot_s = frame_s / n_slots
        self.burst_slot = n_slots - 1 if burst else None
        self.usable = [s for s in range(n_slots) if s != self.burst_slot]
        self.clock = clock
        self.rng = random.Random(stable_hash(ac_id) if seed is None else seed)
        self.slot = self.usable[stable_hash(ac_id) % len(self.usable)]
        self.first_frame: Optional[int] = None
        self.last_planned: Optional[int] = None
        self.timeout = self.rng.randint(*TIMEOUT_FRAMES)
        self.occ: dict[int, dict[str, int]] = {}     # slot -> {sender: frame last heard}
        self.garbled: dict[int, int] = {}            # slot -> frame last garbled
        self.moves: list[tuple[int, int, int, str]] = []   # (frame, from, to, why)

    # ---------- observation ----------
    def frame_of(self, t: float) -> int:
        return math.floor(t / self.frame_s)

    def slot_of(self, t: float) -> int:
        return int((t % self.frame_s) / self.slot_s) % self.n_slots

    def observe(self, sender: str, t_tx: float):
        if sender == self.ac_id:
            return
        s = self.slot_of(t_tx + 1e-6)
        if s == self.burst_slot:
            return
        self.occ.setdefault(s, {})[sender] = self.frame_of(t_tx)

    def observe_slot(self, sender: str, slot: int, frame: int):
        """Frame-index form used by the simulator."""
        if sender != self.ac_id and slot != self.burst_slot:
            self.occ.setdefault(slot, {})[sender] = frame

    def mark_garbled(self, slot: int, frame: int):
        self.garbled[slot] = frame

    # ---------- planning ----------
    def _occupants(self, slot: int, frame: int, horizon: int = 3) -> list[str]:
        return [s for s, f in self.occ.get(slot, {}).items() if frame - f <= horizon]

    def _free(self, frame: int) -> list[int]:
        return [s for s in self.usable if s != self.slot and not self._occupants(s, frame)
                and frame - self.garbled.get(s, -99) > 3]

    def _move(self, frame: int, why: str):
        free = self._free(frame)
        if free:
            new = self.rng.choice(free)
            self.moves.append((frame, self.slot, new, why))
            self.slot = new
        self.timeout = self.rng.randint(*TIMEOUT_FRAMES)

    def slot_for_frame(self, frame: int) -> Optional[int]:
        """Slot to transmit in during `frame`, or None while still listening."""
        if self.first_frame is None:
            self.first_frame = frame
        if frame <= self.first_frame + 1:         # partial first frame + one full listening frame
            return None
        if self.last_planned != frame:
            self.last_planned = frame
            others = self._occupants(self.slot, frame, horizon=2)
            if any(o < self.ac_id for o in others):
                self._move(frame, f"taken_by:{min(others)}")
            else:
                self.timeout -= 1
                if self.timeout <= 0:
                    self._move(frame, "timeout")
        return self.slot

    def next_tx_time(self, now: Optional[float] = None, urgent: bool = False) -> float:
        """Absolute time of the next slot this node may transmit in (>= now)."""
        now = self.clock() if now is None else now
        f = self.frame_of(now)
        for frame in range(f, f + 4):
            cands = []
            s = self.slot_for_frame(frame)
            if s is not None:
                cands.append(frame * self.frame_s + s * self.slot_s)
            if urgent and self.burst_slot is not None and frame > self.first_frame:
                cands.append(frame * self.frame_s + self.burst_slot * self.slot_s)
            cands = [c for c in cands if c >= now - 1e-4]
            if cands:
                return min(cands)
        return now + self.frame_s


# ---------- offline collision-rate metric ----------
def _ids(n: int, rng: random.Random) -> list[str]:
    return [f"N{rng.randint(100, 999)}{chr(65 + i % 26)}{i}" for i in range(n)]


def simulate(n: int, mode: str, n_slots: int = N_SLOTS, frames: int = 300, warmup: int = 40, seed: int = 0) -> float:
    """Fraction of STATE packets lost to collisions, all senders in mutual range, 1 packet/s each.
    mode: 'unslotted' (free-running 1 Hz timers, pure ALOHA) | 'hash' (fixed hashed slot, no
    listening) | 'sotdma' (this module)."""
    rng = random.Random(seed)
    ids = _ids(n, rng)
    slot_s = FRAME_S / n_slots
    air = slot_s * AIRTIME_FRAC
    tx = lost = 0
    if mode == "unslotted":
        phase = [rng.random() for _ in ids]
        drift = [rng.gauss(0, 2e-3) for _ in ids]       # crystal drift, s per frame
        for f in range(frames):
            ts = sorted(((phase[i] + drift[i] * f) % FRAME_S, i) for i in range(n))
            if f < warmup:
                continue
            for k, (t, i) in enumerate(ts):
                prev_t = ts[k - 1][0] - (FRAME_S if k == 0 else 0)
                next_t = ts[(k + 1) % n][0] + (FRAME_S if k == n - 1 else 0)
                tx += 1
                if n > 1 and (t - prev_t < air or next_t - t < air):
                    lost += 1
        return lost / max(tx, 1)
    if mode == "hash":
        usable = n_slots - 1
        slots = [stable_hash(i) % usable for i in ids]
        for s in slots:
            tx += 1
            lost += slots.count(s) > 1
        return lost / max(tx, 1)
    # sotdma
    nodes = [SlotScheduler(i, n_slots=n_slots, seed=rng.randint(0, 1 << 30)) for i in ids]
    join = [rng.randint(0, 5) for _ in ids]
    for f in range(frames):
        plan: dict[int, list[int]] = {}
        for k, nd in enumerate(nodes):
            if f < join[k]:
                continue
            s = nd.slot_for_frame(f)
            if s is not None:
                plan.setdefault(s, []).append(k)
        for s, senders in plan.items():
            for j, nd in enumerate(nodes):
                if f < join[j] or j in senders:
                    continue                          # half duplex
                if len(senders) == 1:
                    nd.observe_slot(ids[senders[0]], s, f)
                else:
                    nd.mark_garbled(s, f)
            if f >= warmup:
                tx += len(senders)
                if len(senders) > 1:
                    lost += len(senders)
    return lost / max(tx, 1)


def report(ns=(8, 30), slot_counts=(20, 40), seeds: int = 10) -> list[dict]:
    rows = []
    for n_slots in slot_counts:
        for n in ns:
            row = {"senders": n, "slots": n_slots}
            for mode in ("unslotted", "hash", "sotdma"):
                row[mode] = sum(simulate(n, mode, n_slots, seed=s) for s in range(seeds)) / seeds
            rows.append(row)
    return rows


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, nargs="*", default=[8, 30])
    ap.add_argument("--slots", type=int, nargs="*", default=[20, 40])
    ap.add_argument("--seeds", type=int, default=10)
    a = ap.parse_args()
    print(f"STATE packet collision rate, all senders in mutual range, 1 Hz, {AIRTIME_FRAC*50:.0f}-ms-of-50 airtime")
    print(f"{'senders':>7} {'slots':>5} {'unslotted':>10} {'hash only':>10} {'SOTDMA':>8}")
    for r in report(a.n, a.slots, a.seeds):
        print(f"{r['senders']:>7} {r['slots']:>5} {r['unslotted']:>10.1%} {r['hash']:>10.1%} {r['sotdma']:>8.1%}")
