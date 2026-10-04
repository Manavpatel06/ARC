"""
verify/fusion.py - combine check results into one trust score, state and reasons.

Weighted log-odds (naive-Bayes style) from a neutral prior:
    L_obs = logit(prior) + sum_i  w_i * conf_i * logit(clip(score_i))       (checks with score None add 0)
then smoothed over time so scores don't flicker, but a big change in evidence moves fast:
    L = L_prev + a * (L_obs - L_prev),   a = smooth_alpha, or fast_alpha when |L_obs - L_prev| >= fast_jump
trust = 100 * sigmoid(L);  VERIFIED >= verified_min, SUSPECT <= suspect_max, else UNVERIFIED;
guardrails.py may then override the state. Reasons = the checks that pushed hardest toward the state.
"""
from __future__ import annotations
import math

from verify import guardrails

def logit(p: float) -> float:
    return math.log(p / (1.0 - p))

def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))

def fuse(track, results: dict, cfg: dict) -> dict:
    f = cfg["fusion"]
    clip = f["score_clip"]
    contrib = {}
    obs = logit(min(0.99, max(0.01, f["prior_trust"] / 100.0)))
    for name, r in results.items():
        if r.score is None:
            contrib[name] = 0.0
            continue
        c = f["weights"].get(name, 1.0) * r.confidence * logit(min(1 - clip, max(clip, r.score)))
        contrib[name] = c
        obs += c
    prev = track.logodds
    if prev is None:
        L = obs
    else:
        a = f["fast_alpha"] if abs(obs - prev) >= f["fast_jump"] else f["smooth_alpha"]
        L = prev + a * (obs - prev)
    track.logodds = L
    trust = int(round(100 * sigmoid(L)))
    state = "VERIFIED" if trust >= f["verified_min"] else "SUSPECT" if trust <= f["suspect_max"] else "UNVERIFIED"
    state, notes = guardrails.apply(state, results, cfg)
    track.state = state
    # reasons: strongest contributions in the direction of the verdict (both directions when UNVERIFIED)
    if state == "VERIFIED":
        order = sorted((n for n in contrib if contrib[n] > 0), key=lambda n: -contrib[n])
    elif state == "SUSPECT":
        order = sorted((n for n in contrib if contrib[n] < 0), key=lambda n: contrib[n])
    else:
        order = sorted((n for n in contrib if contrib[n] != 0), key=lambda n: -abs(contrib[n]))
    reasons = notes + [results[n].reason for n in order]
    if not reasons:
        reasons = [r.reason for r in results.values()][:1]
    return {"trust": trust, "state": state, "reasons": reasons[:3], "contrib": contrib,
            "tcas_confirmed": guardrails.is_tcas_confirmed(results)}
