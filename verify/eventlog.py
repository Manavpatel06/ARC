"""
verify/eventlog.py - FLOCK's own security: a tamper-evident detection record and a signed data link.

EventLog: append-only JSON lines, each entry hash-chained to the one before and signed with the unit's
Ed25519 key (PyNaCl):
    {"seq", "t", "kind", "data", "prev": <sha256 of the previous entry>, "hash": <sha256 of this entry
     without hash/sig>, "sig": <Ed25519 over hash>, "pub": <signer public key>}
Edit, delete, reorder or insert a line and verify_file() names the first entry that breaks.

Signed link: the unit signs every VERIFY frame (sign_frame); the world pins the unit's key on the first
frame it receives (trust on first use) and drops frames that do not verify (check_frame). A production
system would provision the key at installation and authenticate the display end as well - see
docs/limitations.md.

    python -m verify.eventlog harness/out/verify/N101_events.jsonl      # verify a log
"""
from __future__ import annotations
import base64, hashlib, json, os, sys

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY_DIR = os.path.join(ROOT, "harness", "out", "keys")
GENESIS = "0" * 64

def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()

def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()

def load_key(unit_id: str, key_dir: str = KEY_DIR) -> SigningKey:
    """The unit's signing key, created on first use (never committed: harness/out is git-ignored)."""
    os.makedirs(key_dir, exist_ok=True)
    path = os.path.join(key_dir, f"{unit_id}.ed25519")
    if os.path.exists(path):
        return SigningKey(open(path, "rb").read())
    sk = SigningKey.generate()
    with open(path, "wb") as f:
        f.write(bytes(sk))
    return sk

def pub_of(sk: SigningKey) -> str:
    return b64(bytes(sk.verify_key))

# ---------------------------------------------------------------- signed link (VERIFY frames)
def sign_frame(frame: dict, sk: SigningKey) -> dict:
    body = {k: v for k, v in frame.items() if k not in ("sig", "pub")}
    return dict(body, pub=pub_of(sk), sig=b64(sk.sign(canonical(body)).signature))

def check_frame(frame: dict, pinned_pub: str | None) -> tuple[bool, str]:
    """(ok, why). pinned_pub None = first frame from this unit (trust on first use)."""
    pub, sig = frame.get("pub"), frame.get("sig")
    if not pub or not sig:
        return False, "unsigned"
    if pinned_pub is not None and pub != pinned_pub:
        return False, "key changed"
    body = {k: v for k, v in frame.items() if k not in ("sig", "pub")}
    try:
        VerifyKey(base64.b64decode(pub)).verify(canonical(body), base64.b64decode(sig))
    except (BadSignatureError, ValueError):
        return False, "bad signature"
    return True, "ok"

# ---------------------------------------------------------------- hash-chained event log
class EventLog:
    def __init__(self, path: str, sk: SigningKey):
        self.path, self.sk = path, sk
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.seq, self.prev = 0, GENESIS
        if os.path.exists(path):                       # continue an existing chain
            for line in open(path, encoding="utf-8"):
                if line.strip():
                    e = json.loads(line)
                    self.seq, self.prev = e["seq"] + 1, e["hash"]

    def append(self, kind: str, data: dict, t: float) -> dict:
        e = {"seq": self.seq, "t": round(t, 3), "kind": kind, "data": data, "prev": self.prev}
        h = hashlib.sha256(canonical(e)).hexdigest()
        e.update(hash=h, sig=b64(self.sk.sign(h.encode()).signature), pub=pub_of(self.sk))
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")
        self.seq, self.prev = self.seq + 1, h
        return e

def verify_file(path: str, pub: str | None = None) -> tuple[bool, str]:
    """Check the whole chain: hashes, links and signatures. pub pins the expected signer."""
    prev, n = GENESIS, 0
    for i, line in enumerate(open(path, encoding="utf-8")):
        if not line.strip():
            continue
        e = json.loads(line)
        body = {k: e[k] for k in ("seq", "t", "kind", "data", "prev")}
        if e["seq"] != n or e["prev"] != prev:
            return False, f"entry {i}: chain broken (seq {e['seq']}, expected {n})"
        h = hashlib.sha256(canonical(body)).hexdigest()
        if h != e["hash"]:
            return False, f"entry {i}: content changed (hash mismatch)"
        if pub is not None and e["pub"] != pub:
            return False, f"entry {i}: signed by a different key"
        try:
            VerifyKey(base64.b64decode(e["pub"])).verify(h.encode(), base64.b64decode(e["sig"]))
        except BadSignatureError:
            return False, f"entry {i}: bad signature"
        prev, n = h, n + 1
    return True, f"{n} entries, chain and signatures OK"

if __name__ == "__main__":
    ok, why = verify_file(sys.argv[1])
    print(("OK  " if ok else "FAIL ") + why)
    sys.exit(0 if ok else 1)
