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

// ---------- radio altimeter callouts ----------
{
  const { createCallouts, raDisplay } = await import("../callouts.js");
  const fly = (u, from, to, vs, step = 3) => {           // 20 Hz-ish samples from `from` to `to` ft
    const said = [];
    for (let a = from; vs < 0 ? a >= to : a <= to; a += vs < 0 ? -step : step) { const w = u(a, vs, false); if (w) said.push(w); }
    return said;
  };
  let u = createCallouts();
  const descent = fly(u, 1200, 0, -600);
  check("callouts: full descent in order", descent.join(",") === "one thousand,five hundred,four hundred,three hundred,two hundred,one hundred,fifty,forty,thirty,twenty,ten",
        descent.join(","));
  u = createCallouts();
  check("callouts: none while climbing", fly(u, 0, 1500, 700).length === 0);
  u = createCallouts();
  fly(u, 600, 60, -600); fly(u, 60, 700, 800);            // go-around from 60 ft, climb back to 700
  const again = fly(u, 700, 30, -500);
  check("callouts: re-armed after a go-around", again.includes("five hundred") && again.includes("one hundred") && again.includes("fifty"), again.join(","));
  u = createCallouts();
  u(1200, -600, false);
  check("callouts: big jump says the lowest height crossed", u(80, -3000, false) === "one hundred");
  u = createCallouts();
  u(300, -500, false);
  check("callouts: silent on the ground", u(0, -500, true) === null);
  check("RA display: 10 ft steps high, 5 ft under 200, 1 ft under 50",
        raDisplay(1234) === 1230 && raDisplay(173) === 175 && raDisplay(37.4) === 37 && raDisplay(-4) === 0);
}

// ---------- 3D placement: aircraft drawn on the ground it is actually over (no sinking) ----------
{
  const { aircraftHeightM, surfaceM, heightM, MODEL_WHEELS_M, EYE_M } = await import("../cesium3d.js");
  const C = { Cartographic: { fromDegrees: (lon, lat) => ({ lon, lat }) } };
  const viewerAt = (h) => ({ scene: { globe: { getHeight: () => h } } });
  const FT = 0.3048;
  // flat mode: the drawn ground is the ellipsoid (0 m) everywhere, so height = AGL exactly
  check("3D flat: on the runway the wheels are at the drawn ground", aircraftHeightM(C, null, false, 33.69, -112.08, 1450, 0) === 0);
  check("3D flat: 20 ft AGL over low ground is drawn 20 ft up (old code drew it underground)",
        Math.abs(aircraftHeightM(C, null, false, 33.69, -112.08, 1470, 20) - 20 * FT) < 1e-9 && heightM(1470, false, 1478) === 0);
  // terrain mode: sits on Cesium's rendered terrain, whatever our elevation grid says
  check("3D terrain: wheels on Cesium's ground when it is loaded", aircraftHeightM(C, viewerAt(412.5), true, 33.69, -112.08, 1450, 0) === 412.5);
  check("3D terrain: AGL added on top of Cesium's ground",
        Math.abs(aircraftHeightM(C, viewerAt(412.5), true, 33.69, -112.08, 1550, 100) - (412.5 + 100 * FT)) < 1e-9);
  check("3D terrain: falls back to sim ground + geoid before tiles load",
        Math.abs(surfaceM(C, viewerAt(undefined), true, 0, 0, 1450) - (1450 * FT - 31)) < 1e-9);
  check("3D: negative AGL never drawn below ground", aircraftHeightM(C, null, false, 0, 0, 1440, -3) === 0);
  check("3D: model lifted so wheels touch, eye above wheels", MODEL_WHEELS_M > 0.8 && MODEL_WHEELS_M < 1.3 && EYE_M >= 1.8);
}

console.log(failures ? `\n${failures} FAILED` : "\nall web logic tests passed");
process.exit(failures ? 1 : 0);
