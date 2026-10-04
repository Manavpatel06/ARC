"""
harness/fit_learned.py — Lane D (D13). Learn ARC's prediction numbers from REAL KDVT traffic, offline,
and freeze them in a versioned file. Runtime stays deterministic: the node only reads the frozen numbers.

Input : recorded real ADS-B around KDVT (data/live_traffic.py --record -> harness/out/live_*.jsonl)
Output: node/learned_values.json  (numbers + data fingerprint + sample counts + date; review before adopting)
        harness/out/learned_report.md (tables a judge can read)

What is learned (self-supervised: the label is where the aircraft REALLY went next):
  1. sigma growth of the straight-line fallback  (line_sigma_k, m/s)       now 2.5
  2. sigma growth of turn-aware prediction        (k = a + b * (1 - conf)) now a=0.8, b=1.5
     -> each fitted so that 68 % of real errors fall inside sigma (1-sigma coverage)
  3. CONF_MIN: the leg-confidence above which turn-aware beats straight-line on real tracks   now 0.6
Every candidate value is scored on the SAME real samples with the node's own Predictor (node/predict.py),
so the numbers are the node's error on real pilots, not a model's error on its own simulation.

    python harness/fit_learned.py                       # all harness/out/live_*.jsonl
    python harness/fit_learned.py --files a.jsonl b.jsonl
"""
from __future__ import annotations
import argparse, collections, glob, hashlib, json, math, os, sys, time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from node.geometry import FT, KT, NM, build_patterns, to_enu, wrap180     # noqa: E402
from node.predict import CONF_MIN, KState, Predictor, SIGMA0_M            # noqa: E402

HORIZONS = (10, 20, 30, 45, 60)       # seconds ahead that are scored
MAX_GAP_S = 20.0                      # a track breaks if two reports are further apart than this
WITHIN_NM = 5.0                       # ARC's working area: the KDVT pattern and its approaches
MAX_AGL_FT = 3000.0
MIN_GS_KT = 40.0
MIN_AGL_FT = 200.0                    # below this: takeoff / landing roll, not ARC's problem
COVER = 0.68                          # 1-sigma coverage target


def load_tracks(files):
    """{id: [(t, lat, lon, alt_ft, gs_kt, trk, vs_fpm)]} sorted, de-duplicated (age-corrected report time)."""
    tr = collections.defaultdict(dict)
    for f in files:
        for line in open(f):
            if not line.strip():
                continue
            m = json.loads(line)
            if str(m.get("source", "")).startswith(("replay", "offline", "test")):
                continue
            for a in m.get("aircraft", []):
                if a.get("on_ground"):
                    continue
                t = round(m["t"] - (a.get("age_s") or 0.0), 1)
                tr[a["id"]][t] = (t, a["lat"], a["lon"], a["alt_msl_ft"], a["gs_kt"], a["track_deg"], a.get("vs_fpm", 0.0))
    return {k: sorted(v.values()) for k, v in tr.items()}


def clean(track):
    """Drop stale repeats (same position re-sent with a newer time) and glitches (implied speed far from the
    reported ground speed: feed mixing receivers / MLAT). Returns the kept reports and how many were dropped."""
    kept, dropped = [], 0
    for p in track:
        if kept:
            q = kept[-1]
            dt = p[0] - q[0]
            if (p[1], p[2]) == (q[1], q[2]) or dt < 2.0:
                dropped += 1
                continue
            if dt <= MAX_GAP_S:
                x0, y0 = to_enu(q[1], q[2]); x1, y1 = to_enu(p[1], p[2])
                v = math.hypot(x1 - x0, y1 - y0) / dt
                ref = max(1.0, 0.5 * (p[4] + q[4]) * KT)
                if not (0.6 <= v / ref <= 1.6):
                    dropped += 1
                    continue
        kept.append(p)
    return kept, dropped


def segments(track):
    track, _ = clean(track)
    if not track:
        return
    seg = [track[0]]
    for p in track[1:]:
        if p[0] - seg[-1][0] > MAX_GAP_S:
            yield seg
            seg = []
        seg.append(p)
    yield seg


def samples(tracks, elev_m):
    """Every report inside ARC's area that has the real future available: (KState, [(dt, x, y)])."""
    out = []
    for tid, track in tracks.items():
        for seg in segments(track):
            for i in range(1, len(seg) - 1):
                t, lat, lon, alt, gs, trk, vs = seg[i]
                x, y = to_enu(lat, lon)
                agl_m = alt * FT - elev_m
                if math.hypot(x, y) > WITHIN_NM * NM or agl_m > MAX_AGL_FT * FT or agl_m < MIN_AGL_FT * FT or gs < MIN_GS_KT:
                    continue
                tp = seg[i - 1]
                turn = wrap180(trk - tp[5]) / max(1.0, t - tp[0])
                st = KState(x=x, y=y, z=alt * FT, gs=gs * KT, track=trk, vs=vs * FT / 60.0, turn_rate=turn)
                fut = []
                for q in seg[i + 1:]:
                    dt = q[0] - t
                    if dt > max(HORIZONS) + 8:
                        break
                    fx, fy = to_enu(q[1], q[2])
                    fut.append((dt, fx, fy))
                if fut and fut[-1][0] >= HORIZONS[0] - 3:
                    out.append((tid, t, st, fut))
    return out


def errors(pred_obj, samples_, conf_min):
    """Per sample: method used, confidence, and horizontal error at each future report (dt, err_m)."""
    pr = Predictor(pred_obj.patterns, conf_min=conf_min)
    rows = []
    for tid, t, st, fut in samples_:
        cls = pr.classify(st)
        p = pr.predict(st, 0.0, cls, horizon=max(HORIZONS) + 10, dt=1.0)
        lp = pr.straight_line(st, 0.0, max(HORIZONS) + 10, 1.0, cls.conf, cls.leg, cls.runway)
        dts = np.array([f[0] for f in fut])
        act = np.array([[f[1], f[2]] for f in fut])
        e_used = np.hypot(*(p.at(dts)[:, :2] - act).T)
        e_line = np.hypot(*(lp.at(dts)[:, :2] - act).T)
        rows.append({"id": tid, "conf": cls.conf, "leg": cls.leg, "method": p.method, "dt": dts, "e": e_used, "e_line": e_line})
    return rows


def at_h(rows, h, key="e", pick=lambda r: True):
    v = []
    for r in rows:
        if not pick(r):
            continue
        i = np.argmin(np.abs(r["dt"] - h))
        if abs(r["dt"][i] - h) <= 3.5:
            v.append(r[key][i])
    return np.array(v)


def fit_growth(rows, key, pick, sigma0=SIGMA0_M):
    """Smallest k (m/s) such that COVER of real errors satisfy e <= sigma0 + k * dt, pooled over horizons."""
    ratios = []
    for r in rows:
        if pick(r):
            ratios += [max(0.0, (e - sigma0) / dt) for e, dt in zip(r[key], r["dt"]) if dt >= 5]
    return (float(np.quantile(ratios, COVER)), len(ratios)) if ratios else (None, 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", nargs="*")
    ap.add_argument("--out", default=os.path.join(ROOT, "node", "learned_values.json"))
    a = ap.parse_args()
    files = a.files or sorted(glob.glob(os.path.join(ROOT, "harness", "out", "live_*.jsonl")))
    files = [f for f in files if os.path.getsize(f) > 0]
    if not files:
        sys.exit("no recordings: run  python data/live_traffic.py --record  first")
    h = hashlib.sha256()
    for f in files:
        h.update(open(f, "rb").read())
    pats = build_patterns()
    elev_m = next(iter(pats.values())).elev_m
    tracks = load_tracks(files)
    S = samples(tracks, elev_m)
    if len(S) < 30:
        sys.exit(f"only {len(S)} usable samples - record longer")
    base = Predictor(pats)
    rows = errors(base, S, CONF_MIN)
    t_span = (min(s[1] for s in S), max(s[1] for s in S))

    # 1-2. sigma growth
    k_line, n_line = fit_growth(rows, "e_line", lambda r: True)
    ta = [r for r in rows if r["method"] == "turn-aware"]
    k_hi, n_hi = fit_growth(ta, "e", lambda r: r["conf"] >= 0.8)
    k_lo, n_lo = fit_growth(ta, "e", lambda r: r["conf"] < 0.8)
    # k = a + b * (1 - conf): through the two bucket centres (conf ~0.9 and ~0.7); fall back to current if thin
    if k_hi is not None and k_lo is not None and n_hi >= 40 and n_lo >= 40:
        b = max(0.0, (k_lo - k_hi) / 0.2)
        a_ = max(0.0, k_hi - b * 0.1)
    else:
        a_, b = None, None

    # 3. CONF_MIN: lowest threshold at which turn-aware is at least as good as straight-line (median error, 30 s)
    conf_rows = []
    best_cm = None
    for cm in (0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
        sel = [r for r in rows if r["conf"] >= cm and r["leg"] != "UNKNOWN"]
        e_ta = at_h(sel, 30); e_ln = at_h(sel, 30, "e_line")
        # turn-aware prediction for these samples at this threshold
        pr = Predictor(pats, conf_min=0.0)
        conf_rows.append((cm, len(e_ta), float(np.median(e_ta)) if len(e_ta) else None, float(np.median(e_ln)) if len(e_ln) else None))
    for cm, n, mta, mln in conf_rows:
        # band [cm, cm+0.1): is turn-aware better there?
        band = [r for r in rows if cm <= r["conf"] < cm + 0.1 and r["leg"] != "UNKNOWN"]
        bt, bl = at_h(band, 30), at_h(band, 30, "e_line")
        if len(bt) >= 15 and np.median(bt) <= np.median(bl) and best_cm is None:
            best_cm = cm

    # error table
    table = []
    for hz in HORIZONS:
        e_all = at_h(rows, hz); e_ln = at_h(rows, hz, "e_line")
        e_ta = at_h(rows, hz, pick=lambda r: r["method"] == "turn-aware")
        e_ta_line = at_h(rows, hz, "e_line", pick=lambda r: r["method"] == "turn-aware")
        table.append({"h_s": hz, "n": int(len(e_all)),
                      "flock_median_m": round(float(np.median(e_all)), 1) if len(e_all) else None,
                      "line_median_m": round(float(np.median(e_ln)), 1) if len(e_ln) else None,
                      "turn_aware_n": int(len(e_ta)),
                      "turn_aware_median_m": round(float(np.median(e_ta)), 1) if len(e_ta) else None,
                      "same_samples_line_median_m": round(float(np.median(e_ta_line)), 1) if len(e_ta_line) else None})

    # 4. per leg: does turn-aware beat straight-line on the SAME real samples? (30 s ahead, median)
    per_leg, ta_legs = [], []
    for leg in ("UPWIND", "CROSSWIND", "DOWNWIND", "BASE", "FINAL", "STRAIGHT_IN"):
        sel = [r for r in rows if r["leg"] == leg and r["method"] == "turn-aware"]
        t30, l30 = at_h(sel, 30), at_h(sel, 30, "e_line")
        if len(t30) == 0:
            per_leg.append({"leg": leg, "n": 0}); continue
        mt, ml = float(np.median(t30)), float(np.median(l30))
        verdict = "turn-aware" if mt <= ml else "straight-line until the turn is seen"
        if len(t30) < 15:
            verdict += " (too few samples)"
        elif mt <= ml:
            ta_legs.append(leg)
        per_leg.append({"leg": leg, "n": int(len(t30)), "turn_aware_30s_m": round(mt), "straight_30s_m": round(ml), "use": verdict})

    legs = collections.Counter(r["leg"] for r in rows)
    out = {
        "version": time.strftime("%Y%m%d-%H%M", time.localtime()),
        "adopted": False,
        "note": "Fitted offline from real KDVT ADS-B; review the report, run accept_b + montecarlo, then set adopted=true.",
        "data": {"files": [os.path.basename(f) for f in files], "sha256": h.hexdigest()[:16],
                 "aircraft": len(tracks), "samples": len(S),
                 "from": time.strftime("%Y-%m-%d %H:%M", time.localtime(t_span[0])),
                 "to": time.strftime("%Y-%m-%d %H:%M", time.localtime(t_span[1])),
                 "legs_classified": dict(legs.most_common())},
        "current": {"line_sigma_k": 2.5, "turn_sigma_a": 0.8, "turn_sigma_b": 1.5, "conf_min": CONF_MIN},
        "fitted": {"line_sigma_k": round(k_line, 2) if k_line is not None else None, "line_n": n_line,
                   "turn_sigma_a": round(a_, 2) if a_ is not None else None,
                   "turn_sigma_b": round(b, 2) if b is not None else None,
                   "turn_k_conf_ge_0.8": round(k_hi, 2) if k_hi is not None else None, "n_hi": n_hi,
                   "turn_k_conf_lt_0.8": round(k_lo, 2) if k_lo is not None else None, "n_lo": n_lo,
                   "conf_min": best_cm,
                   "turn_aware_legs": ta_legs},
        "per_leg_30s": per_leg,
        "error_table": table,
        "conf_threshold_scan_30s": [{"conf_min": c, "n": n, "turn_aware_median_m": round(x, 1) if x else None,
                                     "line_median_m": round(y, 1) if y else None} for c, n, x, y in conf_rows],
    }
    with open(a.out, "w") as f:
        json.dump(out, f, indent=1)
    rep = [f"# Learned values from real KDVT traffic ({out['version']})", "",
           f"Data: {len(files)} recording(s), {len(tracks)} real aircraft, {len(S)} scored positions inside {WITHIN_NM} NM / {MAX_AGL_FT:.0f} ft AGL, "
           f"{out['data']['from']} to {out['data']['to']}. Fingerprint {out['data']['sha256']}.", "",
           "## Prediction error on real aircraft (median, metres)", "",
           "| ahead | samples | ARC (as run) | straight-line | turn-aware only (n) | same samples, straight-line |", "|---|---|---|---|---|---|"]
    for r in table:
        rep.append(f"| {r['h_s']} s | {r['n']} | {r['flock_median_m']} | {r['line_median_m']} | {r['turn_aware_median_m']} ({r['turn_aware_n']}) | {r['same_samples_line_median_m']} |")
    rep += ["", "## Fitted numbers (68 % of real errors inside sigma)", "", "| value | now | fitted | samples |", "|---|---|---|---|",
            f"| straight-line sigma growth (m/s) | 2.5 | {out['fitted']['line_sigma_k']} | {n_line} |",
            f"| turn-aware sigma growth, conf >= 0.8 (m/s) | {0.8 + 1.5 * 0.1:.2f} | {out['fitted']['turn_k_conf_ge_0.8']} | {n_hi} |",
            f"| turn-aware sigma growth, conf < 0.8 (m/s) | {0.8 + 1.5 * 0.3:.2f} | {out['fitted']['turn_k_conf_lt_0.8']} | {n_lo} |",
            f"| CONF_MIN | {CONF_MIN} | {best_cm} | |", "",
            "", "## Per leg: is turn-aware prediction better than straight-line on real pilots? (30 s ahead, same samples)", "",
            "| leg | samples | turn-aware median (m) | straight-line median (m) | use |", "|---|---|---|---|---|"]
    for r in per_leg:
        if r["n"]:
            rep.append(f"| {r['leg']} | {r['n']} | {r['turn_aware_30s_m']} | {r['straight_30s_m']} | {r['use']} |")
    rep += ["", "Deterministic at runtime: these are fixed numbers in node/learned_values.json; nothing is learned while flying.",
            "Sigma here is the error of POSITION-ONLY prediction (ADS-B-only traffic). ARC aircraft also send INTENT at 1 Hz,",
            "which removes most of the turn-timing error measured here."]
    rp = os.path.join(ROOT, "harness", "out", "learned_report.md")
    open(rp, "w").write("\n".join(rep) + "\n")
    print("\n".join(rep))
    print(f"\nwrote {a.out} and {rp}")


if __name__ == "__main__":
    main()
