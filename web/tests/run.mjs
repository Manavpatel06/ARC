// web/tests/run.mjs — Lane A browser-logic tests that run in plain Node (no browser):
//   node web/tests/run.mjs
// Covers haptics.plan() (rumble patterns) and input.js (reset hold, brakes, buttons, throttle) with a mocked
// gamepad, keyboard and clock. Exit code 1 on any failure.

let failures = 0;
const check = (name, ok, detail = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
  if (!ok) failures++;
};

// ---------- mocks shared by input.js ----------
let now = 0, tick = null;
const listeners = {}, fired = [];
globalThis.performance = { now: () => now };
globalThis.location = { search: "" };
globalThis.setInterval = (fn) => { tick = fn; return 1; };
globalThis.addEventListener = (ev, fn) => { (listeners[ev] ||= []).push(fn); };
globalThis.dispatchEvent = (e) => { fired.push([e.type, now]); };
globalThis.Event = class { constructor(type) { this.type = type; } };
const btn = (v) => ({ pressed: v > 0.5, value: v });
const pad = { id: "mock pad", axes: [0, 0, 0, 0], buttons: Array.from({ length: 17 }, () => btn(0)) };
Object.defineProperty(globalThis, "navigator", { value: { getGamepads: () => [pad] }, configurable: true });
const key = (type, code) => (listeners[type] || []).forEach((f) => f({ code, preventDefault() {} }));
const run = (secs) => { for (let i = 0; i < secs * 30; i++) { now += 1000 / 30; tick(); } };

// ---------- haptics.plan ----------
const { plan } = await import("../haptics.js");
function simulate(timeline, ms) {
  const mem = { level: null, next: 0 }, out = [];
  for (let t = 0; t <= ms; t += 50) for (const e of plan(timeline(t), t, mem)) out.push([t + (e.delay || 0), e.strong, e.weak]);
  return out;
}
const rate = (ev, a, b) => ev.filter(([t]) => t >= a && t < b).length / ((b - a) / 1000);
const peak = (ev, a, b) => Math.max(0, ...ev.filter(([t]) => t >= a && t < b).map((e) => Math.max(e[1], e[2])));
const lv = (level, at, extra = {}) => ({ level, levelAt: at, takeover: false, nearestTrustedM: null, turb: 0, taws: null, ...extra });
{
  const traffic = simulate((t) => lv("TRAFFIC", 0), 10000);
  const resolve = simulate((t) => lv("RESOLVE", 0), 10000);
  const takeover = simulate((t) => lv("TAKEOVER", 0, { takeover: true }), 10000);
  const quiet = simulate(() => lv(null, 0), 10000);
  check("haptics: quiet when no advisory", quiet.length === 0);
  check("haptics: escalation TRAFFIC < RESOLVE < TAKEOVER (rate)", rate(traffic, 0, 10000) < rate(resolve, 0, 10000) && rate(resolve, 0, 10000) < rate(takeover, 0, 10000),
        `${rate(traffic, 0, 1e4)}/s, ${rate(resolve, 0, 1e4)}/s, ${rate(takeover, 0, 1e4)}/s`);
  check("haptics: takeover is the strongest pattern", peak(takeover, 0, 1e4) >= peak(resolve, 0, 1e4));
  const fake = simulate(() => lv(null, 0, { nearestTrustedM: null }), 5000);   // cockpit passes null for FAKE-only traffic
  check("haptics: FAKE traffic never rumbles", fake.length === 0);
  const pullup = simulate(() => lv("SEQUENCE", 0, { taws: "PULL UP" }), 4000);
  check("haptics: PULL UP overrides and is strong", peak(pullup, 0, 4000) >= 0.9 && rate(pullup, 0, 4000) >= 2);
  const stale = simulate(() => lv("TRAFFIC", -20000), 5000);                   // advisory older than 12 s
  check("haptics: stale advisory stops rumbling", stale.length === 0);
}

// ---------- input.js ----------
const { startInput } = await import("../input.js");
const sent = [];
const st = startInput((v) => sent.push(v));
run(0.5);
{
  const thr0 = st.throttle;
  pad.buttons[6] = btn(1); pad.buttons[7] = btn(1); run(2.5);
  const mid = st.resetProgress, thrBoth = st.throttle;
  pad.buttons[6] = btn(0); pad.buttons[7] = btn(0); run(0.5);
  check("input: reset bar at ~50 % after 2.5 s of L2+R2", Math.abs(mid - 0.5) < 0.05, mid.toFixed(2));
  check("input: L2+R2 together leave the throttle alone", Math.abs(thrBoth - thr0) < 1e-9);
  check("input: letting go cancels the reset", st.resetProgress === 0 && !fired.some((f) => f[0] === "flock:reset"));
  pad.buttons[6] = btn(1); pad.buttons[7] = btn(1); run(6); pad.buttons[6] = btn(0); pad.buttons[7] = btn(0); run(0.3);
  check("input: a 5 s hold fires exactly one reset", fired.filter((f) => f[0] === "flock:reset").length === 1);
  key("keydown", "KeyR"); run(5.5); key("keyup", "KeyR"); run(0.2);
  check("input: holding R also resets", fired.filter((f) => f[0] === "flock:reset").length === 2);
}
{
  const before = fired.length;
  pad.buttons[0] = btn(1); run(1.0); pad.buttons[0] = btn(0); run(0.2);
  pad.buttons[3] = btn(1); run(0.2); pad.buttons[3] = btn(0); run(0.2);
  pad.buttons[12] = btn(1); run(0.1); pad.buttons[12] = btn(0); run(0.1);
  const evs = fired.slice(before).map((f) => f[0]);
  check("input: Cross/A = one AP toggle per press (held 1 s)", evs.filter((e) => e === "flock:ap").length === 1, evs.join(","));
  check("input: Triangle/Y = chase, D-pad up = map range", evs.includes("flock:chase") && evs.includes("flock:map-out"));
}
{
  pad.axes[0] = 0.9; run(0.2); pad.axes[0] = 0;               // move the stick so INPUT is being sent
  pad.buttons[1] = btn(1); run(0.2);
  const last = sent[sent.length - 1];
  check("input: Circle/B sends brake=true", last && last.brake === true);
  pad.buttons[1] = btn(0); run(0.2);
  check("input: brake released", sent[sent.length - 1].brake === false);
  pad.axes[0] = 0.05; run(0.2);
  check("input: stick inside the deadzone sends roll 0", sent[sent.length - 1].roll === 0);
}

console.log(failures ? `\n${failures} FAILED` : "\nall web logic tests passed");
process.exit(failures ? 1 : 0);
