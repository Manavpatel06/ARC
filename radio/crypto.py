"""
radio/crypto.py — Ed25519 signing + replay protection for the FLOCK radio envelope (Lane C).

What is signed: the canonical JSON of {msg, from, seq, t, body} (sorted keys, no spaces).
`sig` is base64 Ed25519 over those bytes (64 bytes -> 88 chars).

Key distribution (hackathon simplification — SAY SO IN THE PITCH): every node makes a fresh
keypair at startup and announces its public key once at boot (`KEYS` control frame). The
channel acts as the registrar and only pins keys for aircraft the world knows about; in a real
system this is a certificate issued with the aircraft registration (like ADS-B's ICAO address,
but signed). A signature proves WHO sent a message, not that what it says is TRUE — that is
what radio/evidence.py is for.

Replay protection (ReplayGuard): per-sender last accepted `seq` must strictly increase, and `t`
must be within ±2 s of the receiver's clock. Each check returns a reason string so every
rejection can be logged.

    kp = KeyPair()                     # fresh Ed25519 key
    sig = kp.sign(env)                 # env without/with sig; returns base64 string
    ok = verify(kp.pub_b64, env)       # True/False
"""
from __future__ import annotations
import base64, json, os, time
from typing import Optional

from nacl.signing import SigningKey, VerifyKey
from nacl.exceptions import BadSignatureError

TIME_WINDOW_S = 2.0
SIGNED_FIELDS = ("msg", "from", "seq", "t", "body")


def canonical(env: dict) -> bytes:
    """The exact bytes that are signed. Floats go through json so both ends agree."""
    return json.dumps({k: env[k] for k in SIGNED_FIELDS}, sort_keys=True, separators=(",", ":")).encode()


class KeyPair:
    def __init__(self, seed: Optional[bytes] = None):
        self._sk = SigningKey(seed) if seed else SigningKey.generate()
        self.pub_b64 = base64.b64encode(bytes(self._sk.verify_key)).decode()

    def sign(self, env: dict) -> str:
        return base64.b64encode(self._sk.sign(canonical(env)).signature).decode()


def verify(pub_b64: str, env: dict) -> bool:
    try:
        vk = VerifyKey(base64.b64decode(pub_b64))
        vk.verify(canonical(env), base64.b64decode(env.get("sig", "")))
        return True
    except (BadSignatureError, ValueError, TypeError, KeyError):
        return False


def node_key(ac_id: str, key_dir: Optional[str] = None) -> "KeyPair":
    """Per-aircraft key kept on THIS device (~/.flock/keys/<id>.seed), so a restarted node keeps its identity
    (stand-in for a key provisioned at install). A different laptop gets a different key -> registrar refuses it
    until the channel restarts."""
    key_dir = key_dir or os.environ.get("FLOCK_KEY_DIR") or os.path.join(os.path.expanduser("~"), ".flock", "keys")
    path = os.path.join(key_dir, f"{ac_id}.seed")
    try:
        with open(path, "rb") as f:
            seed = f.read()
        if len(seed) == 32:
            return KeyPair(seed)
    except OSError:
        pass
    seed = os.urandom(32)
    try:
        os.makedirs(key_dir, exist_ok=True)
        with open(path, "wb") as f:
            f.write(seed)
    except OSError:
        pass
    return KeyPair(seed)


def initial_seq() -> int:
    """Start seq from the clock (0.1 s units) so a restarted sender is never mistaken for a replay."""
    return int(time.time() * 10)


class KeyRing:
    """Pinned public keys by aircraft id. First key wins; a different key for a pinned id is refused."""
    def __init__(self):
        self.keys: dict[str, str] = {}

    def add(self, ac_id: str, pub_b64: str) -> str:
        """Returns 'added' | 'same' | 'conflict'."""
        cur = self.keys.get(ac_id)
        if cur is None:
            self.keys[ac_id] = pub_b64
            return "added"
        return "same" if cur == pub_b64 else "conflict"

    def get(self, ac_id: str) -> Optional[str]:
        return self.keys.get(ac_id)


class ReplayGuard:
    """Per-sender monotonic seq + ±2 s time window."""
    def __init__(self, window_s: float = TIME_WINDOW_S, clock=time.time):
        self.window_s = window_s
        self.clock = clock
        self.last_seq: dict[str, int] = {}

    def check_time(self, env: dict) -> Optional[str]:
        skew = env["t"] - self.clock()
        if abs(skew) > self.window_s:
            return f"time_window({skew:+.2f}s)"
        return None

    def check_seq(self, env: dict) -> Optional[str]:
        last = self.last_seq.get(env["from"])
        if last is not None and env["seq"] <= last:
            return f"replay_seq({env['seq']}<={last})"
        return None

    def accept(self, env: dict):
        self.last_seq[env["from"]] = env["seq"]


if __name__ == "__main__":
    kp = KeyPair()
    env = {"msg": "STATE", "from": "N101", "seq": 1, "t": time.time(), "body": {"lat": 33.68}}
    env["sig"] = kp.sign(env)
    assert verify(kp.pub_b64, env)
    bad = dict(env, body={"lat": 33.70})
    assert not verify(kp.pub_b64, bad)
    g = ReplayGuard()
    assert g.check_time(env) is None and g.check_seq(env) is None
    g.accept(env)
    assert g.check_seq(env).startswith("replay_seq")
    assert g.check_time(dict(env, t=env["t"] - 5)).startswith("time_window")
    print(f"crypto ok: sig {len(env['sig'])} chars, envelope {len(json.dumps(env))} bytes")
