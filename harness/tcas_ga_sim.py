"""
Peer-to-peer TCAS for General Aviation -- physics starter simulation.

Devils Invent / Honeywell "Future-Ready Avionics", PS 2.
Two aircraft, flat-earth kinematics, GPS + pressure-altitude noise, a 1 Hz
P2P radio link with packet loss and latency, tau/CPA threat logic, and three
resolution strategies: none, warn-only (pilot may or may not react), and
automated avoid with negotiated complementary maneuvers.

Run:  python tcas_ga_sim.py            (Monte Carlo + headline numbers)
      python tcas_ga_sim.py --table    (closed-form kinematics table only)

Everything is SI internally. 1 kt = 0.514444 m/s, 1 ft = 0.3048 m, 1 NM = 1852 m,
3 statute miles = 4828 m (the PS says "miles").
"""
import math, random, argparse, statistics

KT = 0.514444
FT = 0.3048
G = 9.80665
RANGE_LINK_M = 3 * 1609.344          # "ranges up to 3 miles" (statute)
NMAC_H = 500 * FT                    # near mid-air collision box (TCAS standard)
NMAC_V = 100 * FT

# ---------- closed-form physics the pitch can quote ----------
def head_on_time_budget(v1_kt, v2_kt, range_m=RANGE_LINK_M):
    closure = (v1_kt + v2_kt) * KT
    return range_m / closure

def turn_radius(v_kt, bank_deg):
    v = v_kt * KT
    return v * v / (G * math.tan(math.radians(bank_deg)))

def turn_rate_deg_s(v_kt, bank_deg):
    v = v_kt * KT
    return math.degrees(G * math.tan(math.radians(bank_deg)) / v)

def lateral_offset_after(v_kt, bank_deg, t_s, roll_in_s=2.0):
    """Lateral displacement from original track after a level turn held for t_s,
    with a linear roll-in. Exact for a constant-rate turn after roll-in."""
    v = v_kt * KT
    omega = G * math.tan(math.radians(bank_deg)) / v
    t_turn = max(0.0, t_s - roll_in_s)
    # roll-in approximated as half-rate turn
    psi = omega * (t_turn + 0.5 * roll_in_s)
    R = v / omega
    return R * (1 - math.cos(psi))

def vertical_sep_after(climb_fpm_each, t_s, pitch_in_s=2.0):
    rate = 2 * climb_fpm_each * FT / 60.0     # complementary: one up, one down
    return rate * max(0.0, t_s - pitch_in_s / 2)

def print_table():
    print("=== Closed-form numbers (GA, 3 statute mile link) ===")
    for v in (100, 120, 150):
        print(f"head-on {v}+{v} kt: closure {2*v} kt -> {head_on_time_budget(v, v):5.1f} s from first detection to impact")
    print()
    for v in (90, 120, 150):
        print(f"{v} kt @30deg bank: R = {turn_radius(v,30):5.0f} m, rate {turn_rate_deg_s(v,30):4.1f} deg/s, "
              f"90deg turn in {90/turn_rate_deg_s(v,30):4.1f} s")
    print()
    print("Separation gained t seconds after maneuver start (120 kt, 30 deg bank vs 500 fpm each):")
    for t in (5, 10, 15, 20):
        print(f"  t={t:2d}s  horizontal {lateral_offset_after(120,30,t)/FT:6.0f} ft   vertical {vertical_sep_after(500,t)/FT:6.0f} ft")
    print()

# ---------- Monte Carlo ----------
class Aircraft:
    def __init__(self, x, y, z, hdg, v, aid):
        self.x, self.y, self.z = x, y, z
        self.hdg, self.v, self.id = hdg, v, aid
        self.vz = 0.0
        self.turn_rate = 0.0           # rad/s commanded
        self.cmd_time = None           # when a maneuver was commanded
        self.last_rx = None            # last peer state received (x,y,z,vx,vy,vz,t)

    def vel(self):
        return self.v * math.sin(self.hdg), self.v * math.cos(self.hdg)

    def step(self, dt):
        self.hdg += self.turn_rate * dt
        vx, vy = self.vel()
        self.x += vx * dt; self.y += vy * dt; self.z += self.vz * dt

def noisy_state(ac, t, gps_sigma_h=3.0, vel_sigma=0.3, alt_sigma=5.0):
    vx, vy = ac.vel()
    return (ac.x + random.gauss(0, gps_sigma_h), ac.y + random.gauss(0, gps_sigma_h),
            ac.z + random.gauss(0, alt_sigma),
            vx + random.gauss(0, vel_sigma), vy + random.gauss(0, vel_sigma), ac.vz, t)

def threat(own, peer_state, t):
    """Return (tau_s, dcpa_m, dz_m) using the peer's last broadcast, dead-reckoned to now."""
    px, py, pz, pvx, pvy, pvz, pt = peer_state
    age = t - pt
    px += pvx * age; py += pvy * age; pz += pvz * age
    ovx, ovy = own.vel()
    rx, ry = px - own.x, py - own.y
    vx, vy = pvx - ovx, pvy - ovy
    r = math.hypot(rx, ry)
    rdot = (rx * vx + ry * vy) / max(r, 1e-6)       # negative = closing
    vrel2 = vx * vx + vy * vy
    tcpa = -(rx * vx + ry * vy) / vrel2 if vrel2 > 1e-6 else 1e9
    dcpa = math.hypot(rx + vx * tcpa, ry + vy * tcpa) if tcpa > 0 else r
    tau = r / -rdot if rdot < -0.1 else 1e9        # range tau, TCAS-style
    return tau, dcpa, pz - own.z, r

def best_turn_sense(own, other, bank, peer_plan, delay, horizon=40.0, dt=0.5):
    """Forward-simulate both turn senses for `horizon` seconds and return the sign
    (+1 right, -1 left) that gives the larger minimum separation. Uses the peer's
    committed plan if it has broadcast one, otherwise assumes it flies straight."""
    best, best_d = 1.0, -1.0
    for sign in (1.0, -1.0):
        ox, oy, oh = own.x, own.y, own.hdg
        px, py, ph = other.x, other.y, other.hdg
        own_rate = sign * G * math.tan(math.radians(bank)) / own.v
        peer_rate = 0.0
        if peer_plan and peer_plan[0] == 'h':
            peer_rate = peer_plan[1] * G * math.tan(math.radians(bank)) / other.v
        dmin, t = 1e9, 0.0
        while t < horizon:
            if t >= delay:
                oh += own_rate * dt; ph += peer_rate * dt
            ox += own.v * math.sin(oh) * dt; oy += own.v * math.cos(oh) * dt
            px += other.v * math.sin(ph) * dt; py += other.v * math.cos(ph) * dt
            dmin = min(dmin, math.hypot(ox - px, oy - py)); t += dt
        if dmin > best_d:
            best, best_d = sign, dmin
    return best

def run_encounter(mode, v_range=(80, 160), p_loss=0.1, latency=0.3,
                  pilot_delay=5.0, pilot_comply=0.7, bank=30.0, climb_fpm=500,
                  ta_tau=30.0, ra_tau=20.0, dmod=0.3 * 1852, dt=0.2, seed=None):
    """mode in {'none','warn','auto_h','auto_v'}. Returns dict with min separations."""
    if seed is not None: random.seed(seed)
    # Build a random encounter that is roughly on a collision course at t0.
    v1 = random.uniform(*v_range) * KT; v2 = random.uniform(*v_range) * KT
    hdg1 = 0.0
    rel_bearing = random.uniform(-math.pi, math.pi)      # where the intruder starts
    hdg2 = random.uniform(-math.pi, math.pi)
    # place intruder at link range, then nudge heading so CPA is small (collision course)
    R0 = RANGE_LINK_M * 1.05
    a2 = Aircraft(R0 * math.sin(rel_bearing), R0 * math.cos(rel_bearing),
                  random.uniform(-50, 50) * FT, hdg2, v2, 2)
    a1 = Aircraft(0, 0, 0, hdg1, v1, 1)
    # exact intercept: find t such that |own(t) - P| = v2*t, then point the intruder there
    ox, oy = v1 * math.sin(hdg1), v1 * math.cos(hdg1)
    px, py = a2.x, a2.y
    A = ox * ox + oy * oy - v2 * v2
    B = -2 * (ox * px + oy * py)
    C = px * px + py * py
    disc = B * B - 4 * A * C
    if abs(A) < 1e-6 or disc < 0:
        a2.hdg = math.atan2(-px, -py)                 # fall back: aim at origin
    else:
        roots = [(-B + s * math.sqrt(disc)) / (2 * A) for s in (1, -1)]
        pos = [r for r in roots if r > 0]
        t_meet = min(pos) if pos else C ** 0.5 / (v1 + v2)
        a2.hdg = math.atan2(ox * t_meet - px, oy * t_meet - py)
    a2.x += random.gauss(0, 60); a2.y += random.gauss(0, 60)   # slightly imperfect collision course

    t = 0.0; min_h = 1e9; min_v = 1e9; ta_t = None; ra_t = None
    negotiated = False
    while t < 120:
        # 1 Hz broadcast with loss + latency
        if abs((t / 1.0) - round(t / 1.0)) < dt / 2:
            if random.random() > p_loss:
                a1.last_rx = noisy_state(a2, t - latency)
            if random.random() > p_loss:
                a2.last_rx = noisy_state(a1, t - latency)
        for own, other in ((a1, a2), (a2, a1)):
            if own.last_rx is None or own.cmd_time is not None:
                continue
            tau, dcpa, dz, r = threat(own, own.last_rx, t)
            if (tau < ta_tau or r < dmod) and ta_t is None:
                ta_t = t
            if (tau < ra_tau or r < dmod) and dcpa < 0.5 * 1852 and abs(dz) < 600 * FT:
                if ra_t is None: ra_t = t
                if mode == 'none':
                    continue
                # Sense selection: deterministic tie-break on ID when both can talk;
                # fallback = both turn right (14 CFR 91.113 head-on rule).
                delay = 0.0 if mode.startswith('auto') else pilot_delay
                if mode == 'warn':
                    if not hasattr(own, '_complies'):
                        own._complies = random.random() < pilot_comply   # decided once per pilot
                    if not own._complies:
                        continue  # this pilot ignores the warning for the whole encounter
                own.cmd_time = t + delay
                if mode in ('warn', 'auto_h'):
                    # Negotiated horizontal maneuver.
                    # Lower ID commits first: pick the turn sense that maximizes predicted
                    # miss distance assuming the peer flies straight, and broadcast intent.
                    # Higher ID then picks the sense that maximizes miss distance GIVEN the
                    # peer's committed maneuver.  If the link is unusable, fall back to
                    # "both turn right" (14 CFR 91.113 head-on rule) so behavior stays predictable.
                    if p_loss > 0.5:
                        sign = 1.0
                    else:
                        peer_plan = getattr(other, '_pending', None) if own.id > other.id else None
                        sign = best_turn_sense(own, other, bank, peer_plan, delay)
                    own._pending = ('h', sign)
                else:
                    sign = 1.0 if own.id < other.id else -1.0
                    own._pending = ('v', sign)
        for ac in (a1, a2):
            if ac.cmd_time is not None and t >= ac.cmd_time and hasattr(ac, '_pending'):
                kind, sign = ac._pending
                if kind == 'h':
                    ac.turn_rate = sign * G * math.tan(math.radians(bank)) / ac.v
                else:
                    ac.vz = sign * climb_fpm * FT / 60.0
                del ac._pending
        a1.step(dt); a2.step(dt)
        h = math.hypot(a1.x - a2.x, a1.y - a2.y); vsep = abs(a1.z - a2.z)
        if h < min_h: min_h, min_v = h, vsep
        t += dt
    nmac = min_h < NMAC_H and min_v < NMAC_V
    return dict(nmac=nmac, min_h_ft=min_h / FT, ta_t=ta_t, ra_t=ra_t, v1=v1 / KT, v2=v2 / KT)

def monte_carlo(n=400, **kw):
    out = {}
    base = [run_encounter('none', seed=i, **kw) for i in range(n)]
    danger = [i for i, r in enumerate(base) if r['nmac']]      # encounters that WOULD collide
    for mode in ('none', 'warn', 'auto_h', 'auto_v'):
        res = [run_encounter(mode, seed=i, **kw) for i in danger]
        n_d = max(len(res), 1)
        nmac = sum(r['nmac'] for r in res) / n_d
        ra_times = [r['ra_t'] for r in res if r['ra_t'] is not None]
        out[mode] = dict(nmac_rate=nmac, n=len(res),
                         median_ra_t=statistics.median(ra_times) if ra_times else None,
                         min_h_ft_median=statistics.median(r['min_h_ft'] for r in res) if res else 0)
    return out

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--table', action='store_true')
    ap.add_argument('--n', type=int, default=400)
    args = ap.parse_args()
    print_table()
    if args.table:
        raise SystemExit
    print("=== Monte Carlo: random collision-course encounters, 80-160 kt, 3 mi link ===")
    for p_loss in (0.0, 0.3):
        print(f"\n-- packet loss {int(p_loss*100)}%, latency 0.3 s, GPS sigma 3 m --")
        res = monte_carlo(n=args.n, p_loss=p_loss)
        print(f"({res['none']['n']} of {args.n} random encounters are true collision courses; stats below are on those)")
        for mode, r in res.items():
            print(f"{mode:7s} NMAC rate {r['nmac_rate']*100:5.1f}%   median horiz min-sep {r['min_h_ft_median']:6.0f} ft"
                  + (f"   median RA at t={r['median_ra_t']:.1f}s" if r['median_ra_t'] else ""))
    print("\n-- link range sweep (auto_h, 0% loss): how much does the 3 mile requirement matter? --")
    for miles in (1, 2, 3, 5):
        RANGE_LINK_M = miles * 1609.344
        globals()['RANGE_LINK_M'] = RANGE_LINK_M
        base = [run_encounter('none', seed=i) for i in range(args.n)]
        danger = [i for i, r in enumerate(base) if r['nmac']]
        res = [run_encounter('auto_h', seed=i) for i in danger]
        print(f"{miles} mi: residual NMAC rate {sum(r['nmac'] for r in res)/max(len(res),1)*100:5.1f}%  (of {len(res)} collision courses)")
